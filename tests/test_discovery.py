"""Regression tests for mod file discovery (--list-files / --which / --check-missing).

Observed: removing "GTA - JoyRide" by hand meant searching the install by name, and mod
files are NOT named after their mod. It spanned 8 archive files, an r6/scripts dir, an
r6/audioware dir AND r6/storages/CustomMapMarkers/joyride_icons.json — filed under ANOTHER
mod's storage folder. A first pass found half; the rest only surfaced on an untruncated
re-scan, and missing them would have left dead archives loading forever.

Going the other way, the on-disk name is often an abbreviation with no relation to the Nexus
name — NCestate is "Night City Estate" (mod 12857) — so the user could not identify their
own installed mods.

The manifest already records every path deploy placed, so both directions are EXACT lookups.
The only heuristic here is the sweep for files the manifest cannot know about (runtime state,
hand-installs), and most of what follows guards that heuristic from either over-reaching or
being read as authoritative. Pure stdlib — no PySide6, no network.
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

REPO = str(Path(__file__).resolve().parent.parent)

JOYRIDE = "GTA - JoyRide-9999-1-0-1700000000"
# What the manifest actually recorded for it: eight archives with four different spellings,
# plus two script roots. Nothing here is spelled "GTA - JoyRide".
JOYRIDE_FILES = [
    "archive/pc/mod/JoyRide_01.archive",
    "archive/pc/mod/JoyRide_02.archive",
    "archive/pc/mod/joyride_1.0.xl",
    "archive/pc/mod/JoyRideVideo.archive",
    "archive/pc/mod/joyridetunnel2.xl",
    "r6/scripts/JoyRide/main.reds",
    "r6/audioware/joyride/sounds.yaml",
]


def _mk(game: Path, rel: str, size: int = 8) -> Path:
    p = game / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def _joyride_install(game: Path) -> dict:
    for rel in JOYRIDE_FILES:
        _mk(game, rel)
    # The one the by-hand pass missed: written at runtime, under another mod's storage dir.
    _mk(game, "r6/storages/CustomMapMarkers/joyride_icons.json")
    _mk(game, "r6/storages/CustomMapMarkers/markers.json")
    return {
        JOYRIDE: list(JOYRIDE_FILES),
        "Custom Map Markers-8888-2-0-1700000000": [
            "bin/x64/plugins/cyber_engine_tweaks/mods/CustomMapMarkers/init.lua"],
    }


def _out(fn, *a, **kw) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


# ── the reported case: the full footprint, exactly ───────────────────────────

def test_every_owned_file_is_listed_regardless_of_spelling(tmp_path):
    game = tmp_path / "game"
    mods = _joyride_install(game)
    found = D.find_mod_files(game, mods, JOYRIDE)
    assert {r["rel"] for r in found["owned"]} == set(JOYRIDE_FILES)


def test_footprint_spans_every_root_the_mod_touched(tmp_path):
    game = tmp_path / "game"
    mods = _joyride_install(game)
    found = D.find_mod_files(game, mods, JOYRIDE)
    roots = {r.split("/")[0] for r in (x["rel"] for x in found["owned"])}
    assert roots == {"archive", "r6"}


def test_the_runtime_file_under_another_mods_storage_is_found(tmp_path):
    """The one a by-hand search missed. The manifest cannot know it — the mod wrote it at
    runtime — so it can only come from the name sweep, and it lands in `loose`."""
    game = tmp_path / "game"
    mods = _joyride_install(game)
    found = D.find_mod_files(game, mods, JOYRIDE)
    assert "r6/storages/CustomMapMarkers/joyride_icons.json" in {r["rel"] for r in found["loose"]}


def test_the_other_mods_own_storage_file_is_not_claimed(tmp_path):
    """markers.json sits in the same folder and belongs to Custom Map Markers. Sweeping a
    whole directory because one file in it matched would be exactly the over-reach that
    makes a by-hand removal delete someone else's data."""
    game = tmp_path / "game"
    mods = _joyride_install(game)
    found = D.find_mod_files(game, mods, JOYRIDE)
    everything = {r["rel"] for r in found["loose"] + found["related"]}
    assert "r6/storages/CustomMapMarkers/markers.json" not in everything


def test_loose_and_owned_are_separate_buckets(tmp_path):
    game = tmp_path / "game"
    mods = _joyride_install(game)
    found = D.find_mod_files(game, mods, JOYRIDE)
    assert {r["rel"] for r in found["owned"]} & {r["rel"] for r in found["loose"]} == set()


def test_bytes_totals_only_owned_files(tmp_path):
    """The size shown is what an uninstall would actually reclaim, so a guessed-at file must
    not be counted into it."""
    game = tmp_path / "game"
    mods = _joyride_install(game)
    found = D.find_mod_files(game, mods, JOYRIDE)
    assert found["bytes"] == 8 * len(JOYRIDE_FILES)


# ── a file another mod owns must never be swept up ───────────────────────────

def test_name_match_owned_by_another_mod_is_related_not_loose(tmp_path):
    game = tmp_path / "game"
    mods = _joyride_install(game)
    _mk(game, "archive/pc/mod/joyride_patch.archive")
    mods["JoyRide Patch-7777-1-0-1700000000"] = ["archive/pc/mod/joyride_patch.archive"]
    found = D.find_mod_files(game, mods, JOYRIDE)
    rel = [r for r in found["related"] if r["rel"] == "archive/pc/mod/joyride_patch.archive"]
    assert rel and rel[0]["owner"] == "JoyRide Patch-7777-1-0-1700000000"
    assert "archive/pc/mod/joyride_patch.archive" not in {r["rel"] for r in found["loose"]}


def test_related_files_are_reported_as_do_not_delete(tmp_path):
    game = tmp_path / "game"
    mods = _joyride_install(game)
    _mk(game, "archive/pc/mod/joyride_patch.archive")
    mods["JoyRide Patch-7777-1-0-1700000000"] = ["archive/pc/mod/joyride_patch.archive"]
    text = _out(D.report_mod_files, D.find_mod_files(game, mods, JOYRIDE))
    assert "ANOTHER mod owns them" in text and "do not delete" in text


# ── the abbreviation case: NCestate = "Night City Estate" ────────────────────

def test_abbreviated_cet_folder_yields_a_token_from_the_owned_paths():
    """The name "Night City Estate" gives only `estate` — "night" and "city" identify no mod
    in this game. The abbreviation comes from disk instead: the manifest owns
    .../mods/NCestate/init.lua, which is where `ncestate` comes from. Deriving it beats
    guessing it, which is why there is no acronym rule."""
    owned = ["bin/x64/plugins/cyber_engine_tweaks/mods/NCestate/init.lua"]
    toks = D._discovery_tokens("Night City Estate-12857-1-0-1700000000", None, owned)
    assert "ncestate" in toks
    assert "estate" in toks
    assert "night" not in toks and "city" not in toks


def test_abbreviated_folder_finds_the_mods_storage(tmp_path):
    game = tmp_path / "game"
    _mk(game, "bin/x64/plugins/cyber_engine_tweaks/mods/NCestate/init.lua")
    _mk(game, "r6/storages/NCestate/state.json")
    mods = {"Night City Estate-12857-1-0-1700000000":
            ["bin/x64/plugins/cyber_engine_tweaks/mods/NCestate/init.lua"]}
    found = D.find_mod_files(game, mods, "Night City Estate-12857-1-0-1700000000")
    assert "r6/storages/NCestate/state.json" in {r["rel"] for r in found["loose"]}


def test_tokens_come_from_both_names():
    """The Nexus name and the filename can differ completely — mod 8333 is "City of Dreams
    Menu Backgrounds" but ships as "v2_All in one Vortex Installer". Either may be what the
    files on disk are named after, so both feed the sweep."""
    names = {"8333": "City of Dreams Menu Backgrounds"}
    toks = D._discovery_tokens("v2_All in one Vortex Installer-8333-1-5-1716634855", names)
    assert "dreams" in toks
    assert "backgrounds" in toks


# ── the heuristic must not over-reach ────────────────────────────────────────

def test_short_tokens_are_ignored():
    """"GTA" is three letters; as a substring it would match gtav, gtacars, and anything
    else. Below the length floor it never becomes a search term."""
    toks = D._discovery_tokens("GTA - JoyRide-9999-1-0-1700000000")
    assert "gta" not in toks
    assert "joyride" in toks


def test_pure_digits_are_not_tokens():
    toks = D._discovery_tokens("Some Mod 2077 v2-100-1-0-1700000000")
    assert not any(t.isdigit() for t in toks)


def test_path_structure_words_are_not_tokens():
    """Tokenising the managed root itself would make every mod match every other mod."""
    owned = ["archive/pc/mod/thing.archive", "r6/scripts/Thing/init.reds"]
    toks = D._discovery_tokens("Thing-100-1-0-1700000000", None, owned)
    assert "archive" not in toks and "scripts" not in toks and "init" not in toks
    assert "thing" in toks


def test_non_distinctive_token_is_dropped_and_disclosed(tmp_path):
    """A word matching half the install is a common word, not a name. It is dropped, and the
    report says so — a silently narrowed search is worse than a noisy one."""
    game = tmp_path / "game"
    for i in range(D.DISCOVERY_TOKEN_CAP + 5):
        _mk(game, f"archive/pc/mod/weapons_{i}.archive")
    _mk(game, "archive/pc/mod/owned.archive")
    mods = {"Weapons Overhaul-100-1-0-1700000000": ["archive/pc/mod/owned.archive"]}
    found = D.find_mod_files(game, mods, "Weapons Overhaul-100-1-0-1700000000")
    assert "weapons" in found["dropped"]
    assert not any("weapons_" in r["rel"] for r in found["loose"])
    text = _out(D.report_mod_files, found)
    assert "non-distinctive" in text


def test_token_just_under_the_cap_is_kept(tmp_path):
    game = tmp_path / "game"
    for i in range(D.DISCOVERY_TOKEN_CAP):
        _mk(game, f"archive/pc/mod/weapons_{i}.archive")
    _mk(game, "archive/pc/mod/owned.archive")
    mods = {"Weapons Overhaul-100-1-0-1700000000": ["archive/pc/mod/owned.archive"]}
    found = D.find_mod_files(game, mods, "Weapons Overhaul-100-1-0-1700000000")
    assert found["dropped"] == []
    assert len(found["loose"]) == D.DISCOVERY_TOKEN_CAP


def test_base_game_files_are_never_swept(tmp_path):
    """The sweep covers the roots a mod could have written to. Walking the whole install
    would take the base game's tens of thousands of files with it, none of which is a mod's."""
    game = tmp_path / "game"
    _mk(game, "archive/pc/content/basegame_4_gamedata.archive")
    _mk(game, "archive/pc/mod/gamedata_thing.archive")
    mods = {"Gamedata Thing-100-1-0-1700000000": ["archive/pc/mod/gamedata_thing.archive"]}
    swept = set(D._walk_discovery_files(game))
    assert "archive/pc/content/basegame_4_gamedata.archive" not in swept
    assert "archive/pc/mod/gamedata_thing.archive" in swept


def test_a_name_with_no_distinctive_words_says_so(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/x.archive")
    found = D.find_mod_files(game, {"The Ultimate Mod Pack-100-1-0-1700000000": []},
                             "The Ultimate Mod Pack-100-1-0-1700000000")
    assert found["tokens"] == []
    assert "no distinctive search words" in _out(D.report_mod_files, found)


def test_report_says_the_owned_list_is_the_authoritative_one(tmp_path):
    """The whole point of the split: `loose` is a guess and must never read as fact."""
    game = tmp_path / "game"
    mods = _joyride_install(game)
    text = _out(D.report_mod_files, D.find_mod_files(game, mods, JOYRIDE))
    assert "Name matching is a guess" in text
    assert "authoritative" in text


# ── missing owned files ──────────────────────────────────────────────────────

def test_owned_file_gone_from_disk_is_flagged(tmp_path):
    """The !CityOfDreamsLogo.archive case: hand-deleted, still owned. The mod reports as
    installed and its footprint reads larger than it is."""
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/kept.archive")
    mods = {"M-100-1-0-1700000000": ["archive/pc/mod/kept.archive",
                                     "archive/pc/mod/!CityOfDreamsLogo.archive"]}
    found = D.find_mod_files(game, mods, "M-100-1-0-1700000000")
    assert found["missing"] == 1
    gone = [r for r in found["owned"] if r["missing"]]
    assert gone[0]["rel"] == "archive/pc/mod/!CityOfDreamsLogo.archive"
    assert "MISSING from disk" in _out(D.report_mod_files, found)


def test_find_missing_owned_files_groups_by_mod(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/a.archive")
    mods = {
        "A-100-1-0-1700000000": ["archive/pc/mod/a.archive"],
        "B-200-1-0-1700000000": ["archive/pc/mod/b1.archive", "archive/pc/mod/b2.archive"],
    }
    rows = D.find_missing_owned_files(game, mods)
    assert len(rows) == 1
    assert rows[0]["stem"] == "B-200-1-0-1700000000"
    assert rows[0]["missing"] == 2 and rows[0]["total"] == 2


def test_missing_report_is_quiet_when_everything_is_present(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/a.archive")
    mods = {"A-100-1-0-1700000000": ["archive/pc/mod/a.archive"]}
    text = _out(D.report_missing_files, game, mods)
    assert "[missing] 0 mod(s) with 0 owned file(s) gone from disk" in text
    assert "[!]" not in text


def test_missing_report_returns_mod_count(tmp_path):
    game = tmp_path / "game"
    mods = {"A-100-1-0-1700000000": ["archive/pc/mod/a.archive"]}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_missing_files(game, mods)
    assert n == 1


# ── reverse lookup ───────────────────────────────────────────────────────────

def test_which_resolves_a_bare_cet_folder_name(tmp_path):
    """The user's actual question: "what is NCestate?"."""
    game = tmp_path / "game"
    mods = {"Night City Estate-12857-1-0-1700000000":
            ["bin/x64/plugins/cyber_engine_tweaks/mods/NCestate/init.lua"]}
    hits = D.find_path_owner(game, mods, "NCestate")
    assert len(hits) == 1
    assert hits[0]["stem"] == "Night City Estate-12857-1-0-1700000000"


def test_which_is_case_insensitive(tmp_path):
    game = tmp_path / "game"
    mods = {"Night City Estate-12857-1-0-1700000000":
            ["bin/x64/plugins/cyber_engine_tweaks/mods/NCestate/init.lua"]}
    assert D.find_path_owner(game, mods, "ncESTATE")


def test_which_accepts_a_full_relative_path(tmp_path):
    game = tmp_path / "game"
    mods = _joyride_install(game)
    hits = D.find_path_owner(game, mods, "archive/pc/mod/joyride_1.0.xl")
    assert hits and hits[0]["stem"] == JOYRIDE


def test_which_accepts_a_windows_separator(tmp_path):
    game = tmp_path / "game"
    mods = _joyride_install(game)
    assert D.find_path_owner(game, mods, "archive\\pc\\mod\\joyride_1.0.xl")


def test_which_ranks_the_mod_with_more_matches_first(tmp_path):
    game = tmp_path / "game"
    mods = {"Few-1-1-0-1700000000": ["archive/pc/mod/joyride_patch.archive"],
            JOYRIDE: list(JOYRIDE_FILES)}
    hits = D.find_path_owner(game, mods, "joyride")
    assert hits[0]["stem"] == JOYRIDE


def test_which_reports_total_owned_alongside_matches(tmp_path):
    game = tmp_path / "game"
    mods = _joyride_install(game)
    hits = D.find_path_owner(game, mods, "JoyRide_01")
    assert hits[0]["matches"] == ["archive/pc/mod/JoyRide_01.archive"]
    assert hits[0]["total"] == len(JOYRIDE_FILES)


def test_which_falls_back_to_disk_for_a_hand_install(tmp_path):
    """No manifest entry owns it, but it is right there on disk — the answer is
    "hand-installed", not "does not exist"."""
    game = tmp_path / "game"
    _mk(game, "r6/scripts/REDCheatMenu2.reds")
    game.mkdir(exist_ok=True)
    D.save_manifest(game, {})
    text = _out(D.action_which, game, "REDCheatMenu2")
    assert "No manifest entry owns this" in text
    assert "check-unmanaged" in text
    assert "r6/scripts/REDCheatMenu2.reds" in text


def test_which_says_so_when_nothing_matches(tmp_path):
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    D.save_manifest(game, {})
    assert "Nothing on disk or in the manifest" in _out(D.action_which, game, "nosuchthing")


def test_which_prints_the_nexus_url(tmp_path):
    """The provenance the collection strips out: with the mod-id in the key, the page the
    user could not find is one line away."""
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    D.save_manifest(game, {"Night City Estate-12857-1-0-1700000000":
                           ["bin/x64/plugins/cyber_engine_tweaks/mods/NCestate/init.lua"]})
    assert "cyberpunk2077/mods/12857" in _out(D.action_which, game, "NCestate")


# ── matching a mod by what the user would type ───────────────────────────────

def test_list_files_matches_by_mod_id(tmp_path):
    game = tmp_path / "game"
    mods = _joyride_install(game)
    D.save_manifest(game, mods)
    text = _out(D.action_list_files, game, "9999")
    assert "JoyRideVideo.archive" in text


def test_list_files_reports_no_match_helpfully(tmp_path):
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    D.save_manifest(game, {"A-100-1-0-1700000000": []})
    text = _out(D.action_list_files, game, "nosuchmod")
    assert "No installed mod matches" in text
    assert "--which" in text


# ── CLI surface ──────────────────────────────────────────────────────────────

def test_cli_list_files(tmp_path):
    game = tmp_path / "game"
    mods = _joyride_install(game)
    D.save_manifest(game, mods)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--list-files", "JoyRide"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    for rel in JOYRIDE_FILES:
        assert rel.rsplit("/", 1)[-1] in r.stdout, rel
    assert "joyride_icons.json" in r.stdout


def test_cli_which(tmp_path):
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    D.save_manifest(game, {"Night City Estate-12857-1-0-1700000000":
                           ["bin/x64/plugins/cyber_engine_tweaks/mods/NCestate/init.lua"]})
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--which", "NCestate"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Night City Estate" in r.stdout


def test_cli_check_missing(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_manifest(game, {"M-100-1-0-1700000000": ["archive/pc/mod/gone.archive"]})
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-missing"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[missing] 1 mod(s) with 1 owned file(s) gone from disk" in r.stdout


def test_check_health_includes_the_missing_line(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_manifest(game, {"M-100-1-0-1700000000": ["archive/pc/mod/gone.archive"]})
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[missing] 1 mod(s) with 1 owned file(s) gone from disk" in r.stdout


def test_these_actions_write_nothing(tmp_path):
    """Discovery is read-only. It exists to make a removal safe, not to perform one."""
    game = tmp_path / "game"
    before = {p: p.stat().st_mtime_ns for p in game.rglob("*") if p.is_file()}
    _out(D.action_list_files, game, "JoyRide")
    _out(D.action_which, game, "joyride")
    _out(D.action_check_missing, game)
    after = {p: p.stat().st_mtime_ns for p in game.rglob("*") if p.is_file()}
    assert before == after


# ── the GUI must stay in sync with the machine line ──────────────────────────

def test_gui_parses_the_missing_line():
    """Twice now a new health line has shipped that the GUI silently ignored."""
    src = Path(REPO) / "gui" / "deploy_tab.py"
    import re as _re
    rx = _re.search(r"^RE_MISSING = re\.compile\(\s*\n?\s*(r\"[^\"]+\")",
                    src.read_text(), _re.MULTILINE)
    assert rx, "gui/deploy_tab.py has no RE_MISSING parser"
    pattern = _re.compile(eval(rx.group(1)))
    line = "[missing] 3 mod(s) with 7 owned file(s) gone from disk"
    m = pattern.match(line)
    assert m and (m.group(1), m.group(2)) == ("3", "7")


def test_gui_ranks_measured_perf_above_bookkeeping():
    """The chain had drifted from its own comments: an unmanaged file (harmless, often
    deliberate) was suppressing the per-frame warning that was MEASURED to halve the frame
    rate, plus the changed-pin and unfinished-uninstall warnings."""
    text = (Path(REPO) / "gui" / "deploy_tab.py").read_text()
    order = [text.index(k) for k in
             ("elif self._perf[0] >= PERF_WARN:", "elif self._pins[1]:",
              "elif self._disabled[1]:", "elif domains or meths:",
              "elif missing_mods:", "elif units:", "elif self._fds")]
    assert order == sorted(order), "GUI _on_done chain is out of severity order"


def test_the_tree_is_walked_once_for_a_needle_matching_many_mods(tmp_path):
    """197 matches meant 197 full tree walks — 1.85s on a toy fixture, and the real install
    is 1442 mods over a far bigger tree. Same trap as reloading the name cache per mod."""
    game = tmp_path / "game"
    mods = {}
    for i in range(30):
        rel = f"archive/pc/mod/thing{i}.archive"
        _mk(game, rel)
        mods[f"Thing{i} Mod-{100 + i}-1-0-1700000000"] = [rel]
    D.save_manifest(game, mods)
    calls = []
    real = D._walk_discovery_files
    D._walk_discovery_files = lambda g: (calls.append(1), real(g))[1]
    try:
        _out(D.action_list_files, game, "Thing")
    finally:
        D._walk_discovery_files = real
    assert len(calls) == 1, f"walked the tree {len(calls)} times for 30 mods"


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
    print(f"{n} discovery tests passed")
