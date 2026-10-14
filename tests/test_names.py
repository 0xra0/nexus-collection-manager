"""Regression tests for human mod names in nexus_core.deployer.

A manifest key is whatever the download happened to be called, so a mod can be effectively
unidentifiable: "City of Dreams Menu Backgrounds" is keyed
`v2_All in one Vortex Installer-8333-1-5-1716634855` — the FOMOD installer's filename, which
says nothing about a menu. Finding it meant tracing an archive filename
(!CityOfDreamsLogo.archive) back through the manifest by hand, and it defeated every
name-based domain rule.

The manifest key is deliberately NOT changed: it is unique, stable, depended on throughout,
and it is good PROVENANCE. What was missing is the NAME, so that is what gets cached. Pure
stdlib — no PySide6, no network (the API is stubbed).
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

CITY = "v2_All in one Vortex Installer-8333-1-5-1716634855"
CITY_NAME = "City of Dreams Menu Backgrounds"


class _Cfg:
    """Point CONFIG_DIR at a scratch dir — the name cache must never touch the real one."""

    def __enter__(self):
        self.old = D.CONFIG_DIR
        D.CONFIG_DIR = Path(tempfile.mkdtemp())
        return D.CONFIG_DIR

    def __exit__(self, *a):
        D.CONFIG_DIR = self.old


def _quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


# ── the reported case ────────────────────────────────────────────────────────

def test_the_filename_alone_says_nothing_about_the_mod():
    """Baseline: this is the problem. The key contains no word a user would search for."""
    assert D.stem_display_name(CITY) == "v2 All in one Vortex Installer"
    assert "menu" not in CITY.lower() and "dreams" not in CITY.lower()


def test_a_cached_name_replaces_the_filename():
    names = {"8333": CITY_NAME}
    assert D.mod_display_name(CITY, names) == CITY_NAME
    assert D.describe_mod(CITY, names) == f"{CITY_NAME}   [{CITY}]"


def test_provenance_is_kept_alongside_the_name():
    """The archive filename is the record of exactly which file was installed, so it stays
    visible — the name is for reading, the stem is for acting on."""
    assert CITY in D.describe_mod(CITY, {"8333": CITY_NAME})


def test_searching_by_the_real_name_finds_the_mod():
    """The direct payoff: --disable 'City of Dreams' now works. Before, only the mod-id did."""
    names = {"8333": CITY_NAME}
    assert D._matches_needle(CITY, "City of Dreams", names)
    assert D._matches_needle(CITY, "menu backgrounds", names)
    assert not D._matches_needle(CITY, "City of Dreams", {})     # without the cache
    assert D._matches_needle(CITY, "8333", {})                   # id always works
    assert D._matches_needle(CITY, "Vortex", {})                 # so does the filename


def test_a_name_does_not_match_an_unrelated_mod():
    names = {"8333": CITY_NAME, "25272": "Lucy Walk Moon"}
    assert not D._matches_needle("Lucy Walk Moon-25272-1-0-1700000000", "City of Dreams", names)


# ── the point of the fix: name-based detection starts working ────────────────

def test_a_real_name_makes_domain_detection_work_where_the_filename_could_not():
    """"This also defeats any name-based conflict/domain detection." Directly tested: the
    same mod, undetectable by filename, classified correctly once its name is known."""
    with _Cfg():
        D.save_mod_names({})
        assert D.mod_domain(CITY, {}) is None
        D.save_mod_names({"8333": CITY_NAME})
        assert D.mod_domain(CITY, {}) == ("main-menu", "name")


def test_it_then_groups_with_the_other_menu_mods():
    with _Cfg():
        D.save_mod_names({"8333": CITY_NAME})
        mods = {CITY: ["a"], "Edgerunner Splash Screen-24294-1-0-1700000000": ["b"]}
        conflicts = D.find_domain_conflicts(mods, {})
        assert len(conflicts) == 1
        assert {m["stem"] for m in conflicts[0]["mods"]} == set(mods)


def test_names_are_read_once_per_sweep_not_once_per_mod():
    """1442 stems re-reading the cache would be 1442 JSON parses. Caught in rehearsal."""
    with _Cfg():
        D.save_mod_names({str(i): f"Mod {i}" for i in range(1500)})
        calls = []
        real = D.load_mod_names
        D.load_mod_names = lambda: (calls.append(1), real())[1]
        try:
            D.find_domain_conflicts({f"A {i}-{i}-1-0-1700000000": [f"f{i}"]
                                     for i in range(200)}, {})
        finally:
            D.load_mod_names = real
        assert len(calls) == 1, f"loaded the name cache {len(calls)} times"


# ── graceful degradation ─────────────────────────────────────────────────────

def test_everything_works_with_no_cache_at_all():
    """Offline and pre-fetch is the normal state; nothing may depend on names existing."""
    assert D.mod_display_name(CITY, {}) == "v2 All in one Vortex Installer"
    assert D.mod_display_name(CITY, None) is not None
    assert D.describe_mod("some hand named thing", {}) == "some hand named thing"


def test_a_number_inside_the_name_is_not_mistaken_for_the_mod_id():
    """Caught in rehearsal: a "strip everything after the first number" rule turned
    `Frame Mod 13-20013-1-0-1700000000` into "Frame Mod", because the 13 in the NAME looks
    exactly like a mod-id. Same mis-split the version regex is anchored against."""
    assert D.stem_display_name("Frame Mod 13-20013-1-0-1700000000") == "Frame Mod 13"
    assert D.stem_display_name("Frame Mod 9-20009-1-0-1700000000") == "Frame Mod 9"
    assert D.stem_display_name("Additional Stuff 01-11077-1-0-1700000000") == \
        "Additional Stuff 01"


def test_an_unparseable_stem_still_gets_a_name():
    assert D.mod_display_name("e6_07_60_e607608f-1-0", {}) != ""
    assert D.stem_display_name("random folder name") == "random folder name"


def test_a_corrupt_name_cache_is_ignored():
    with _Cfg() as cfg:
        (cfg / D.MOD_NAMES_NAME).write_text("{ not json")
        assert D.load_mod_names() == {}
        (cfg / D.MOD_NAMES_NAME).write_text('{"names": ["a", "list"]}')
        assert D.load_mod_names() == {}


def test_the_cache_lives_in_config_dir_not_the_game_folder(tmp_path):
    """mod-id -> name is a fact about Nexus, not about one install, and it must survive
    reinstalling the game."""
    game = tmp_path / "game"
    game.mkdir()
    with _Cfg() as cfg:
        D.save_mod_names({"8333": CITY_NAME})
        assert (cfg / D.MOD_NAMES_NAME).is_file()
        assert not any(game.iterdir())
        assert json.loads((cfg / D.MOD_NAMES_NAME).read_text())["names"]["8333"] == CITY_NAME


# ── fetching ─────────────────────────────────────────────────────────────────

def test_fetch_only_requests_ids_it_does_not_already_have():
    asked = []

    def fake(mod_id, api_key):
        asked.append(mod_id)
        return {"name": f"Name {mod_id}"}

    real = D.fetch_nexus_metadata
    D.fetch_nexus_metadata = fake
    try:
        names, fetched, failed = D.fetch_mod_names(["1", "2", "3"], "key", {"2": "Cached"})
        assert asked == ["1", "3"] and fetched == 2 and failed == []
        assert names["2"] == "Cached" and names["1"] == "Name 1"
        asked.clear()
        D.fetch_mod_names(["1", "2"], "key", names, refresh=True)
        assert asked == ["1", "2"]
    finally:
        D.fetch_nexus_metadata = real


def test_a_failed_lookup_is_reported_not_cached():
    """Deleted and hidden mod pages return nothing; caching an empty name would make the
    listing permanently wrong with no way to notice."""
    real = D.fetch_nexus_metadata
    D.fetch_nexus_metadata = lambda mod_id, api_key: None
    try:
        names, fetched, failed = D.fetch_mod_names(["999"], "key", {})
        assert names == {} and fetched == 0 and failed == ["999"]
    finally:
        D.fetch_nexus_metadata = real


def test_manifest_mod_ids_collects_what_needs_fetching():
    mods = {CITY: ["a"], "Lucy Walk Moon-25272-1-0-1700000000": ["b"],
            "some hand named folder": ["c"]}
    assert D.manifest_mod_ids(mods) == {"8333", "25272"}


def test_fetch_without_an_api_key_says_so_and_does_nothing(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_manifest(game, {CITY: ["a"]})
    with _Cfg():
        _r, out = _quiet(D.action_fetch_names, game, "")
        assert "No Nexus API key" in out
        assert D.load_mod_names() == {}


def test_fetch_populates_and_saves(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_manifest(game, {CITY: ["a"]})
    real = D.fetch_nexus_metadata
    D.fetch_nexus_metadata = lambda mod_id, api_key: {"name": CITY_NAME}
    try:
        with _Cfg():
            _r, out = _quiet(D.action_fetch_names, game, "some-key")
            assert "Fetched 1 name(s)" in out
            assert D.load_mod_names() == {"8333": CITY_NAME}
    finally:
        D.fetch_nexus_metadata = real


# ── listings ─────────────────────────────────────────────────────────────────

def test_list_deployed_shows_names_and_hints_when_it_cannot(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_manifest(game, {CITY: ["archive/pc/mod/a.archive"]})
    with _Cfg():
        D.save_mod_names({})
        _r, out = _quiet(D.action_list, game, None)
        assert CITY in out
        assert "--fetch-names" in out          # tells the user how to improve it
        D.save_mod_names({"8333": CITY_NAME})
        _r, out = _quiet(D.action_list, game, None)
        assert CITY_NAME in out and CITY in out
        assert "--fetch-names" not in out


def test_list_deployed_filter_accepts_the_real_name(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_manifest(game, {CITY: ["a"], "Other-99-1-0-1700000000": ["b"]})
    with _Cfg():
        D.save_mod_names({"8333": CITY_NAME})
        _r, out = _quiet(D.action_list, game, "City of Dreams")
        assert CITY_NAME in out and "Other-99" not in out


def test_fetch_names_cli(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_manifest(game, {CITY: ["a"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--fetch-names"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "mod-id(s) installed" in r.stdout


if __name__ == "__main__":
    import inspect
    n = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        kw = {"tmp_path": Path(tempfile.mkdtemp())} \
            if "tmp_path" in inspect.signature(fn).parameters else {}
        fn(**kw)
        print("PASS", name); n += 1
    print(f"{n} mod-name tests passed")
