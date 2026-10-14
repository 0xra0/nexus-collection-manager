"""Regression tests for uninstall/prune in nexus_core.deployer.

Deploy only ever ADDED. Removing a mod dropped its manifest entry, but the archive was still
in Downloads, so the next deploy saw "not installed" and put it back — every hand-fix undone.
Observed 2026-07-17: the user switched the main menu to Lucy Walk Moon, but City of Dreams
(mod 8333, `v2_All in one Vortex Installer-8333-1-5-1716634855`) was still fully deployed and
still in the manifest, its 84 MB !v2_Altered Carbon1.archive still winning the menu, while its
!CityOfDreamsLogo.archive had been hand-removed the day before and was still claimed by the
entry. The 33 items in _removed_duplicate_mods/ are what doing this by hand looks like.

These tests are unusually safety-heavy because this is the first code here that DELETES from a
live install. Pure stdlib — no PySide6, no network.
"""
import contextlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

CITY = "v2_All in one Vortex Installer-8333-1-5-1716634855"
CITY_NEWER = "v2_All in one Vortex Installer-8333-1-6-1780000000"


def _mk(game: Path, rel: str, size: int = 16) -> Path:
    p = game / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def _quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


# ── the reported case, end to end ────────────────────────────────────────────

def test_disabling_city_of_dreams_removes_its_files_and_sticks(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/!v2_Altered Carbon1.archive", 4096)
    _mk(game, "archive/pc/mod/Keep.archive")
    D.save_manifest(game, {
        # the logo was already removed by hand — the entry still claims it
        CITY: ["archive/pc/mod/!v2_Altered Carbon1.archive",
               "archive/pc/mod/!CityOfDreamsLogo.archive"],
        "Lucy Walk Moon-25272-1-0-1700000000": ["archive/pc/mod/Keep.archive"],
    })
    # Matched by mod-id: the manifest key is "v2_All in one Vortex Installer-8333-...",
    # which contains none of the words a user would type for "City of Dreams".
    _quiet(D.action_disable, game, "8333", dry_run=False)

    assert not (game / "archive/pc/mod/!v2_Altered Carbon1.archive").exists()
    assert (game / "archive/pc/mod/Keep.archive").exists()
    mods = D.load_manifest(game)["mods"]
    assert CITY not in mods                                  # entry gone
    assert "Lucy Walk Moon-25272-1-0-1700000000" in mods
    assert D.is_disabled(CITY, D.load_disabled(game))        # intent recorded


def test_a_disabled_mod_is_not_reinstalled_by_the_next_deploy(tmp_path):
    """The heart of the bug: dropping the manifest entry was never enough, because the
    archive is still in Downloads and 'not installed' reads as 'install it'."""
    game, dl = tmp_path / "game", tmp_path / "dl"
    game.mkdir()
    dl.mkdir()
    for stem, member in ((CITY, "archive/pc/mod/city.archive"),
                         ("Wanted Mod-100-1-0-1700000000", "archive/pc/mod/wanted.archive")):
        with zipfile.ZipFile(dl / f"{stem}.zip", "w") as z:
            z.writestr(member, "x")
    repo = str(Path(__file__).resolve().parent.parent)
    base = [sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
            "--downloads", str(dl), "--tmp", str(tmp_path / "tmp"), "--skip-hash"]

    r = subprocess.run(base, cwd=repo, capture_output=True, text=True)
    assert (game / "archive/pc/mod/city.archive").exists(), r.stdout + r.stderr

    r = subprocess.run(base[:7] + ["--disable", "v2_All in one"],
                       cwd=repo, capture_output=True, text=True)
    assert not (game / "archive/pc/mod/city.archive").exists(), r.stdout + r.stderr

    r = subprocess.run(base, cwd=repo, capture_output=True, text=True)
    assert not (game / "archive/pc/mod/city.archive").exists(), \
        "the disabled mod was reinstalled — this is the bug"
    assert "disabled" in r.stdout
    assert (game / "archive/pc/mod/wanted.archive").exists()   # everything else still deploys


def test_disabling_survives_a_new_version_of_the_same_mod(tmp_path):
    """Keyed by mod identity, not archive stem: 'I don't want City of Dreams' has to keep
    holding when the author uploads 1.6, which is a different stem entirely."""
    game = tmp_path / "game"
    game.mkdir()
    D.save_disabled(game, {D.disable_key(CITY): {"stem": CITY, "when": "now"}})
    disabled = D.load_disabled(game)
    assert D.is_disabled(CITY, disabled)
    assert D.is_disabled(CITY_NEWER, disabled)
    assert not D.is_disabled("Lucy Walk Moon-25272-1-0-1700000000", disabled)


def test_enable_undoes_it(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_disabled(game, {D.disable_key(CITY): {"stem": CITY, "when": "now"}})
    _quiet(D.action_enable, game, "8333", dry_run=False)
    assert D.load_disabled(game) == {}


def test_hand_removed_files_are_reported_not_treated_as_an_error(tmp_path):
    """!CityOfDreamsLogo.archive was gone from disk but still claimed. Dropping the entry is
    the point — that claim is what made the next deploy restore it."""
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/present.archive")
    mods = {CITY: ["archive/pc/mod/present.archive", "archive/pc/mod/already-gone.archive"]}
    plan = D.plan_prune(game, mods, {D.disable_key(CITY): {"stem": CITY}})
    assert plan["paths"] == ["archive/pc/mod/present.archive"]
    assert plan["missing"] == ["archive/pc/mod/already-gone.archive"]


# ── the safety guards ────────────────────────────────────────────────────────

def test_a_file_another_enabled_mod_owns_is_never_deleted(tmp_path):
    """The guard that makes this safe at all. The manifest is known to carry bad attribution
    (entries whittled down to files that were never theirs), so 'this entry claims it' is not
    grounds to delete anything — 'no mod you still want claims it' is."""
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/shared.archive")
    _mk(game, "archive/pc/mod/solo.archive")
    mods = {CITY: ["archive/pc/mod/shared.archive", "archive/pc/mod/solo.archive"],
            "Other Mod-999-1-0-1700000000": ["archive/pc/mod/shared.archive"]}
    _quiet(D.sync_prune, game, mods, {D.disable_key(CITY): {"stem": CITY}}, False)
    assert (game / "archive/pc/mod/shared.archive").exists()
    assert not (game / "archive/pc/mod/solo.archive").exists()


def test_case_drift_does_not_make_a_live_file_look_unwanted(tmp_path):
    """Wine reads Scripts/ and scripts/ as one path. An exact compare would delete a file an
    enabled mod is actively using because the two entries disagree about capitalisation."""
    game = tmp_path / "game"
    _mk(game, "red4ext/plugins/Codeware/Scripts/Api.reds")
    mods = {CITY: ["red4ext/plugins/Codeware/Scripts/Api.reds"],
            "Codeware-7780-1-0-1700000000": ["red4ext/plugins/codeware/scripts/api.reds"]}
    plan = D.plan_prune(game, mods, {D.disable_key(CITY): {"stem": CITY}})
    assert plan["paths"] == []


def test_unmanaged_files_are_never_touched(tmp_path):
    """Hand-installed files belong to no entry, so nothing can put them in a prune set. The
    user may well have put them there on purpose."""
    game = tmp_path / "game"
    _mk(game, "r6/scripts/REDCheatMenu2.reds", 2048)
    _mk(game, "archive/pc/mod/city.archive")
    mods = {CITY: ["archive/pc/mod/city.archive"]}
    _quiet(D.sync_prune, game, mods, {D.disable_key(CITY): {"stem": CITY}}, False)
    assert (game / "r6/scripts/REDCheatMenu2.reds").exists()


def _big_install(game: Path, n: int = 200):
    for i in range(n):
        _mk(game, f"archive/pc/mod/m{i}.archive")
    return {f"Mod {i}-{i}-1-0-1700000000": [f"archive/pc/mod/m{i}.archive"] for i in range(n)}


def test_a_runaway_prune_is_refused(tmp_path):
    """A manifest bug that emptied the keep-set would make every managed file look unwanted
    and delete the whole install in one run. Costs nothing to check; the alternative is the
    user's collection."""
    game = tmp_path / "game"
    mods = _big_install(game)
    disabled = {D.disable_key(s): {"stem": s} for s in list(mods)[:180]}   # 90%
    plan, _out = _quiet(D.sync_prune, game, mods, disabled, False)
    assert plan["refused"] is True
    assert all((game / f"archive/pc/mod/m{i}.archive").exists() for i in range(200))
    _q, msg = _quiet(D.report_prune_plan, plan, False)
    assert "REFUSED" in msg and "--force-prune" in msg


def test_the_guard_needs_scale_not_just_a_fraction(tmp_path):
    """Disabling two mods out of three is an ordinary thing to do and must not be blocked.
    A fraction-only rule fires instantly at small scale while missing nothing real: the bug
    it guards against would target thousands of files on a 1442-entry install."""
    game = tmp_path / "game"
    for i in range(3):
        _mk(game, f"archive/pc/mod/m{i}.archive")
    mods = {f"Mod {i}-{i}-1-0-1700000000": [f"archive/pc/mod/m{i}.archive"] for i in range(3)}
    disabled = {D.disable_key(s): {"stem": s} for s in list(mods)[:2]}    # 67%, but only 2 files
    plan, _ = _quiet(D.sync_prune, game, mods, disabled, False)
    assert plan["refused"] is False
    assert not (game / "archive/pc/mod/m0.archive").exists()
    assert (game / "archive/pc/mod/m2.archive").exists()


def test_force_prune_overrides_the_runaway_guard(tmp_path):
    game = tmp_path / "game"
    mods = _big_install(game)
    disabled = {D.disable_key(s): {"stem": s} for s in list(mods)[:180]}
    plan, _ = _quiet(D.sync_prune, game, mods, disabled, False, force=True)
    assert plan["refused"] is False
    assert not (game / "archive/pc/mod/m0.archive").exists()
    assert (game / "archive/pc/mod/m199.archive").exists()


def test_removed_files_go_to_trash_not_oblivion(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/city.archive", 4096)
    mods = {CITY: ["archive/pc/mod/city.archive"]}
    _quiet(D.sync_prune, game, mods, {D.disable_key(CITY): {"stem": CITY}}, False)
    trashed = list((game / ".mod_trash").rglob("city.archive"))
    assert len(trashed) == 1 and trashed[0].stat().st_size == 4096


def test_dry_run_changes_nothing_at_all(tmp_path):
    import hashlib
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/city.archive", 4096)
    mods = {CITY: ["archive/pc/mod/city.archive"]}
    D.save_manifest(game, dict(mods))

    def fp():
        return {str(p): (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in sorted(game.rglob("*")) if p.is_file()}
    before = fp()
    plan, _ = _quiet(D.sync_prune, game, mods, {D.disable_key(CITY): {"stem": CITY}}, True)
    assert plan["paths"] == ["archive/pc/mod/city.archive"]     # it still tells you
    assert fp() == before                                       # and changes nothing
    assert CITY in mods


def test_disable_dry_run_still_plans_against_the_state_it_would_create(tmp_path):
    """Caught in rehearsal: the dry run only planned against the ALREADY-disabled set, so it
    reported "nothing deployed for these yet" for the very mod being disabled — the one
    output a user checks before letting it delete anything."""
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/city.archive", 4096)
    D.save_manifest(game, {CITY: ["archive/pc/mod/city.archive"]})
    _r, out = _quiet(D.action_disable, game, "8333", dry_run=True)
    assert "Would remove 1 disabled mod(s): 1 file(s)" in out
    assert "Nothing deployed for these yet" not in out
    assert (game / "archive/pc/mod/city.archive").exists()   # and still changed nothing


def test_disable_dry_run_records_nothing(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/city.archive")
    D.save_manifest(game, {CITY: ["archive/pc/mod/city.archive"]})
    _quiet(D.action_disable, game, "8333", dry_run=True)
    assert D.load_disabled(game) == {}
    assert (game / "archive/pc/mod/city.archive").exists()


def test_nothing_disabled_means_nothing_is_ever_removed(tmp_path):
    game = tmp_path / "game"
    for i in range(5):
        _mk(game, f"archive/pc/mod/m{i}.archive")
    mods = {f"Mod {i}-{i}-1-0-1700000000": [f"archive/pc/mod/m{i}.archive"] for i in range(5)}
    plan, _ = _quiet(D.sync_prune, game, mods, {}, False)
    assert plan["stems"] == [] and plan["paths"] == []
    assert all((game / f"archive/pc/mod/m{i}.archive").exists() for i in range(5))


# ── state file ───────────────────────────────────────────────────────────────

def test_the_disabled_file_lives_beside_the_manifest(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_disabled(game, {D.disable_key(CITY): {"stem": CITY, "when": "now"}})
    p = game / D.DISABLED_NAME
    assert p.is_file()
    assert json.loads(p.read_text())["disabled"]


def test_a_corrupt_disabled_file_disables_nothing(tmp_path):
    """Failing open matters here: failing closed would mean an unreadable state file silently
    stops the whole collection from deploying."""
    game = tmp_path / "game"
    game.mkdir()
    (game / D.DISABLED_NAME).write_text("{ not json")
    assert D.load_disabled(game) == {}
    (game / D.DISABLED_NAME).write_text('["a", "list"]')
    assert D.load_disabled(game) == {}


def test_a_missing_disabled_file_is_normal(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    assert D.load_disabled(game) == {}


def test_a_mod_is_findable_by_its_nexus_id_when_the_name_gives_nothing_away():
    """Mod 8333 is "City of Dreams Menu Backgrounds" on Nexus, but its archive — and so its
    manifest key — is `v2_All in one Vortex Installer-8333-...`. Vortex-style all-in-one
    names are common enough that without id matching those mods cannot be disabled at all."""
    assert D._matches_needle(CITY, "8333")
    assert D._matches_needle(CITY, "Vortex")            # substring still works
    assert not D._matches_needle(CITY, "25272")         # a different mod's id
    assert not D._matches_needle("Lucy Walk Moon-25272-1-0-1700000000", "8333")


def test_disable_key_falls_back_for_unparseable_stems():
    assert D.disable_key(CITY) == D.disable_key(CITY_NEWER)
    assert D.disable_key("some hand named folder").startswith("stem:")


# ── reporting ────────────────────────────────────────────────────────────────

def test_health_reports_an_unfinished_uninstall(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/city.archive")
    D.save_disabled(game, {D.disable_key(CITY): {"stem": CITY, "when": "now"}})
    mods = {CITY: ["archive/pc/mod/city.archive"]}
    n, out = _quiet(D.report_disabled, game, mods)
    assert n == 1
    assert "[disabled] 1 mod(s) disabled; 1 file(s) awaiting removal" in out
    assert "still loaded by the game" in out


def test_health_is_quiet_when_the_uninstall_is_complete(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_disabled(game, {D.disable_key(CITY): {"stem": CITY, "when": "now"}})
    n, out = _quiet(D.report_disabled, game, {})
    assert n == 0
    assert out.strip() == "[disabled] 1 mod(s) disabled; 0 file(s) awaiting removal"


def test_gui_regex_is_the_one_shipped_in_deploy_tab():
    src = (Path(__file__).resolve().parent.parent / "gui" / "deploy_tab.py").read_text()
    assert "RE_DISABLED" in src and "self._disabled" in src


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_prune_and_list_disabled_cli(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/city.archive", 2048)
    D.save_manifest(game, {CITY: ["archive/pc/mod/city.archive"]})
    D.save_disabled(game, {D.disable_key(CITY): {"stem": CITY, "when": "now"}})
    repo = str(Path(__file__).resolve().parent.parent)
    base = [sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game)]

    r = subprocess.run(base + ["--list-disabled"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0 and CITY in r.stdout and "still on disk" in r.stdout

    r = subprocess.run(base + ["--prune", "--dry-run"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0 and "Would remove" in r.stdout
    assert (game / "archive/pc/mod/city.archive").exists()

    r = subprocess.run(base + ["--prune"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (game / "archive/pc/mod/city.archive").exists()
    assert CITY not in D.load_manifest(game)["mods"]


def test_check_health_includes_the_disabled_line(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/m.archive")
    D.save_manifest(game, {"M-1-1-0-1700000000": ["archive/pc/mod/m.archive"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[disabled] 0 mod(s) disabled" in r.stdout


if __name__ == "__main__":
    import inspect
    import tempfile
    n = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        kw = {"tmp_path": Path(tempfile.mkdtemp())} \
            if "tmp_path" in inspect.signature(fn).parameters else {}
        fn(**kw)
        print("PASS", name); n += 1
    print(f"{n} prune/uninstall tests passed")
