"""Regression tests for mutually-conflicting mod detection in nexus_core.deployer.

Observed on a real install: FOUR first-person camera mods (2202, 1786, 2675, 30558) — the
vehicle camera toggle silently stopped working — and FOUR main-menu/intro replacers (25272,
24294, 2621, 11426). Every one of them installed "successfully", none overlapped on disk, and
no existing check saw anything. The mods fight over a subsystem, not over a file path.

Two detectors are covered here. The redscript one is PROOF (two @replaceMethod on one method
means only one of them exists after compilation); the domain one is a GUESS and most of these
tests exist to keep that guess narrow. Pure stdlib — no PySide6, no network.
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402


def _mk(game: Path, rel: str, text: str = "") -> Path:
    p = game / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _domains(mods, overrides=None):
    return {c["domain"]: [m["stem"] for m in c["mods"]]
            for c in D.find_domain_conflicts(mods, overrides or {})}


# ── the two reported cases ───────────────────────────────────────────────────

def test_the_four_first_person_camera_mods_are_caught():
    mods = {
        "Better Vehicle First Person-2202-1-0-1700000000": ["archive/pc/mod/a.archive"],
        "First Person Camera Mod-1786-2-1-1700000001": ["archive/pc/mod/b.archive"],
        "Immersive First Person-2675-1-3-1700000002": ["archive/pc/mod/c.archive"],
        "True First Person Camera 2-30558-2-0-1700000003": ["archive/pc/mod/d.archive"],
    }
    got = _domains(mods)
    assert set(got) == {"first-person-camera"}
    assert len(got["first-person-camera"]) == 4


def test_the_four_main_menu_replacers_are_caught():
    """Three of these have a giveaway phrase in the name; 'Lucy Walk Moon' has nothing at
    all, which is why the curated mod-id table has to exist alongside the name rules."""
    mods = {
        "Lucy Walk Moon-25272-1-0-1700000000": ["archive/pc/mod/a.archive"],
        "Edgerunner Splash Screen-24294-1-0-1700000001": ["archive/pc/mod/b.archive"],
        "Intro Splashscreen and Song Change-2621-1-0-1700000002": ["archive/pc/mod/c.archive"],
        "Official E3 2018 Trailer Intro Cinematic-11426-1-0-1700000003": ["archive/pc/mod/d.archive"],
    }
    got = _domains(mods)
    assert set(got) == {"main-menu"}
    assert len(got["main-menu"]) == 4


def test_lucy_walk_moon_is_only_findable_by_mod_id():
    """Guard the claim above: if someone deletes the curated entry believing the name rules
    cover everything, this fails."""
    assert D.mod_domain("Lucy Walk Moon-25272-1-0-1700000000", {}) == ("main-menu", "known")
    assert D.mod_domain("Lucy Walk Moon-99999-1-0-1700000000", {}) is None


# ── keeping the guess narrow ─────────────────────────────────────────────────

def test_one_mod_in_a_domain_is_not_a_conflict():
    mods = {"Immersive First Person-2675-1-3-1700000002": ["archive/pc/mod/c.archive"],
            "Some Texture Pack-9999-1-0-1700000000": ["archive/pc/mod/t.archive"]}
    assert D.find_domain_conflicts(mods, {}) == []


def test_untagged_mods_are_never_reported():
    """The overwhelming majority of a 1442-entry collection matches no domain, and must
    produce no output at all."""
    mods = {f"Random Mod {i}-{9000 + i}-1-0-1700000000": [f"archive/pc/mod/{i}.archive"]
            for i in range(50)}
    assert D.find_domain_conflicts(mods, {}) == []


def test_two_versions_of_one_mod_are_not_a_subsystem_conflict():
    """That is a version duplicate with its own fixer (--fix-versions). Reporting it here
    would be the same fault wearing a second hat, and the advice would be wrong."""
    mods = {
        "Immersive First Person-2675-1-3-1700000000": ["archive/pc/mod/a.archive"],
        "Immersive First Person-2675-1-4-1800000000": ["archive/pc/mod/b.archive"],
    }
    assert D.find_domain_conflicts(mods, {}) == []


def test_sibling_files_of_one_mod_page_are_not_a_conflict():
    """One mod page shipping several co-installable files (mod 27740 publishes ~20) shares a
    mod-id and name, so identity collapsing must swallow them."""
    mods = {f"First Person Camera Mod-1786-1-0-17000000{i:02d}": [f"archive/pc/mod/{i}.archive"]
            for i in range(5)}
    assert D.find_domain_conflicts(mods, {}) == []


def test_a_bare_word_does_not_match_a_domain():
    """'camera', 'menu' and 'intro' on their own match a large fraction of any mod list; only
    multi-word phrases are rules. A section full of false alarms gets ignored wholesale."""
    for name in ("Photo Mode Camera Tweaks-1-1-0-1700000000",
                 "Better Menu Icons-2-1-0-1700000000",
                 "Introduction Quest Fixes-3-1-0-1700000000"):
        assert D.mod_domain(name, {}) is None


def test_third_person_is_not_first_person():
    a = D.mod_domain("Third Person Mod-501-1-0-1700000000", {})
    b = D.mod_domain("Better First Person-502-1-0-1700000000", {})
    assert a == ("third-person-camera", "name") and b == ("first-person-camera", "name")
    # and therefore installing both is not reported as one contested domain
    assert D.find_domain_conflicts(
        {"Third Person Mod-501-1-0-1700000000": ["a"],
         "Better First Person-502-1-0-1700000000": ["b"]}, {}) == []


def test_a_name_match_is_labelled_as_a_guess():
    mods = {"Cool First Person Thing-8001-1-0-1700000000": ["a"],
            "Another 1st Person Cam-8002-1-0-1700000000": ["b"]}
    c = D.find_domain_conflicts(mods, {})[0]
    assert {m["how"] for m in c["mods"]} == {"name"}
    # a curated id is not a guess and says so
    known = D.mod_domain("Whatever-2202-1-0-1700000000", {})
    assert known is not None and known[1] == "known"


def test_user_overrides_win_over_the_builtin_table():
    """The built-in table is 9 entries against a 1442-mod collection; it is only useful if
    the user can extend and correct it without editing the source."""
    mods = {"Mystery Mod A-40001-1-0-1700000000": ["a"],
            "Mystery Mod B-40002-1-0-1700000000": ["b"]}
    assert D.find_domain_conflicts(mods, {}) == []
    got = _domains(mods, {"40001": "weather", "40002": "weather"})
    assert set(got) == {"weather"}
    # and an override can re-tag a curated mod-id
    assert D.mod_domain("Better Vehicle First Person-2202-1-0-1700000000",
                        {"2202": "vehicle-handling"}) == ("vehicle-handling", "known")


def test_overrides_are_read_from_config_dir_not_the_game_folder(tmp_path):
    """User knowledge about their own collection must survive reinstalling the game."""
    old = D.CONFIG_DIR
    try:
        D.CONFIG_DIR = tmp_path / "cfg"
        D.CONFIG_DIR.mkdir()
        (D.CONFIG_DIR / D.DOMAIN_OVERRIDES_NAME).write_text('{"40001": "weather"}')
        assert D.load_domain_overrides() == {"40001": "weather"}
    finally:
        D.CONFIG_DIR = old


def test_a_corrupt_override_file_does_not_break_the_check(tmp_path):
    old = D.CONFIG_DIR
    try:
        D.CONFIG_DIR = tmp_path / "cfg"
        D.CONFIG_DIR.mkdir()
        (D.CONFIG_DIR / D.DOMAIN_OVERRIDES_NAME).write_text("{not json at all")
        assert D.load_domain_overrides() == {}
    finally:
        D.CONFIG_DIR = old


def test_every_domain_in_the_tables_has_a_label():
    """A domain with no label prints its raw slug at the user, or KeyErrors."""
    for d in set(D.DOMAIN_BY_MOD_ID.values()) | set(D.DOMAIN_NAME_RULES):
        assert d in D.DOMAIN_LABELS, d


# ── the proof layer: redscript @replaceMethod ────────────────────────────────

def test_two_mods_replacing_one_method_is_a_conflict(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/CamA/a.reds",
        "@replaceMethod(VehicleComponent)\nfunc OnCameraToggle() -> Void { }\n")
    _mk(game, "r6/scripts/CamB/b.reds",
        "@replaceMethod(VehicleComponent)\nfunc OnCameraToggle() -> Void { }\n")
    mods = {"Cam A-1-1-0-1700000000": ["r6/scripts/CamA/a.reds"],
            "Cam B-2-1-0-1700000000": ["r6/scripts/CamB/b.reds"]}
    conf = D.find_replace_conflicts(game, mods)
    assert [c["target"] for c in conf] == ["VehicleComponent::OnCameraToggle"]
    assert {m["owner"] for m in conf[0]["mods"]} == {"Cam A-1-1-0-1700000000",
                                                     "Cam B-2-1-0-1700000000"}


def test_wrapmethod_is_not_a_conflict(tmp_path):
    """@wrapMethod chains through wrappedMethod(), so any number of mods can wrap one method
    and all of them run. Counting these would flag most script mods ever written and bury the
    replacements that genuinely do break."""
    game = tmp_path / "game"
    for mod in ("W1", "W2", "W3"):
        _mk(game, f"r6/scripts/{mod}/x.reds",
            "@wrapMethod(VehicleComponent)\nfunc OnCameraToggle() -> Void "
            "{ wrappedMethod(); }\n")
    mods = {f"Wrap {i}-{i}-1-0-1700000000": [f"r6/scripts/W{i}/x.reds"] for i in (1, 2, 3)}
    assert D.find_replace_conflicts(game, mods) == []


def test_one_mod_replacing_a_method_alone_is_fine(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/Solo/a.reds",
        "@replaceMethod(PlayerPuppet)\nfunc OnAction() -> Void { }\n")
    assert D.find_replace_conflicts(game, {"Solo-1-1-0-1700000000": ["r6/scripts/Solo/a.reds"]}) == []


def test_the_same_mod_replacing_in_two_of_its_own_files_is_not_a_conflict(tmp_path):
    """Only DISTINCT mods conflict. A mod is entitled to organise its own sources however it
    likes (and redscript would raise SYM_REDEFINITION itself if this were truly wrong)."""
    game = tmp_path / "game"
    _mk(game, "r6/scripts/Solo/a.reds",
        "@replaceMethod(PlayerPuppet)\nfunc OnAction() -> Void { }\n")
    _mk(game, "r6/scripts/Solo/b.reds",
        "@replaceMethod(PlayerPuppet)\nfunc OnAction() -> Void { }\n")
    mods = {"Solo-1-1-0-1700000000": ["r6/scripts/Solo/a.reds", "r6/scripts/Solo/b.reds"]}
    assert D.find_replace_conflicts(game, mods) == []


def test_different_methods_on_one_class_do_not_conflict(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/A/a.reds",
        "@replaceMethod(VehicleComponent)\nfunc OnMount() -> Void { }\n")
    _mk(game, "r6/scripts/B/b.reds",
        "@replaceMethod(VehicleComponent)\nfunc OnUnmount() -> Void { }\n")
    mods = {"A-1-1-0-1700000000": ["r6/scripts/A/a.reds"],
            "B-2-1-0-1700000000": ["r6/scripts/B/b.reds"]}
    assert D.find_replace_conflicts(game, mods) == []


def test_commented_out_annotations_are_ignored(tmp_path):
    """Half-finished and example code is full of these. Reading a comment as live code
    invents a conflict that does not exist."""
    game = tmp_path / "game"
    _mk(game, "r6/scripts/A/a.reds",
        "@replaceMethod(VehicleComponent)\nfunc OnCameraToggle() -> Void { }\n")
    _mk(game, "r6/scripts/B/b.reds",
        "// @replaceMethod(VehicleComponent)\n// func OnCameraToggle() -> Void { }\n"
        "/* @replaceMethod(VehicleComponent)\n   func OnCameraToggle() -> Void { } */\n"
        "@wrapMethod(Foo)\nfunc Bar() -> Void { wrappedMethod(); }\n")
    mods = {"A-1-1-0-1700000000": ["r6/scripts/A/a.reds"],
            "B-2-1-0-1700000000": ["r6/scripts/B/b.reds"]}
    assert D.find_replace_conflicts(game, mods) == []


def test_replaceglobal_is_covered(tmp_path):
    game = tmp_path / "game"
    for mod in ("A", "B"):
        _mk(game, f"r6/scripts/{mod}/x.reds",
            "@replaceGlobal()\nfunc GetCarryCapacity() -> Float { return 1.0; }\n")
    mods = {"A-1-1-0-1700000000": ["r6/scripts/A/x.reds"],
            "B-2-1-0-1700000000": ["r6/scripts/B/x.reds"]}
    conf = D.find_replace_conflicts(game, mods)
    assert [c["target"] for c in conf] == ["GetCarryCapacity()"]


def test_annotation_separated_from_func_by_blank_lines_still_parses(tmp_path):
    game = tmp_path / "game"
    src = ("@replaceMethod(VehicleComponent)\n\n\n"
           "public final func OnCameraToggle() -> Void { }\n")
    for mod in ("A", "B"):
        _mk(game, f"r6/scripts/{mod}/x.reds", src)
    mods = {"A-1-1-0-1700000000": ["r6/scripts/A/x.reds"],
            "B-2-1-0-1700000000": ["r6/scripts/B/x.reds"]}
    assert [c["target"] for c in D.find_replace_conflicts(game, mods)] == \
        ["VehicleComponent::OnCameraToggle"]


def test_a_framework_bundled_script_is_scanned_too(tmp_path):
    """The compiler reads red4ext/plugins/<Name>/Scripts as well as r6/scripts, so a conflict
    can straddle the two — see the standalone-scc limits that made this non-obvious."""
    game = tmp_path / "game"
    _mk(game, "r6/scripts/A/x.reds",
        "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    _mk(game, "red4ext/plugins/SomeFw/Scripts/y.reds",
        "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    mods = {"A-1-1-0-1700000000": ["r6/scripts/A/x.reds"],
            "Fw-2-1-0-1700000000": ["red4ext/plugins/SomeFw/Scripts/y.reds"]}
    assert len(D.find_replace_conflicts(game, mods)) == 1


def test_an_unmanaged_script_conflicts_too_and_is_named_by_path(tmp_path):
    """A hand-installed script is among the likeliest things to be silently overridden, and
    it has no manifest entry to name it by."""
    game = tmp_path / "game"
    _mk(game, "r6/scripts/A/x.reds", "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    _mk(game, "r6/scripts/REDCheatMenu2.reds", "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    mods = {"A-1-1-0-1700000000": ["r6/scripts/A/x.reds"]}
    conf = D.find_replace_conflicts(game, mods)
    owners = {m["owner"] for m in conf[0]["mods"]}
    assert owners == {"A-1-1-0-1700000000", "(unmanaged) r6/scripts/REDCheatMenu2.reds"}


def test_backup_dirs_are_not_scanned(tmp_path):
    """Retired copies parked by --fix-duplicates still contain their annotations. Scanning
    them would make every fix create a fresh conflict against the copy it just kept."""
    game = tmp_path / "game"
    _mk(game, "r6/scripts/A/x.reds", "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    _mk(game, f"{D.DUP_BACKUP_DIRNAME}/old/x.reds",
        "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    _mk(game, "r6/scripts/.mod_trash/x.reds",
        "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    assert D.find_replace_conflicts(game, {"A-1-1-0-1700000000": ["r6/scripts/A/x.reds"]}) == []


def test_parse_reds_replacements_directly():
    src = ("@addMethod(Foo)\nfunc Added() -> Void { }\n"
           "@wrapMethod(Foo)\nfunc Wrapped() -> Void { }\n"
           "@replaceMethod(Bar)\nfunc Replaced() -> Void { }\n"
           "@replaceGlobal()\nfunc Global() -> Void { }\n")
    assert D.parse_reds_replacements(src) == {("Bar", "Replaced"), ("<global>", "Global")}


# ── report and read-only guarantee ───────────────────────────────────────────

def test_report_emits_a_parseable_line_the_gui_matches(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/A/x.reds", "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    _mk(game, "r6/scripts/B/x.reds", "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    mods = {
        "A-1-1-0-1700000000": ["r6/scripts/A/x.reds"],
        "B-2-1-0-1700000000": ["r6/scripts/B/x.reds"],
        "Better Vehicle First Person-2202-1-0-1700000000": ["archive/pc/mod/a.archive"],
        "Immersive First Person-2675-1-3-1700000002": ["archive/pc/mod/c.archive"],
    }
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_conflicts(game, mods)
    out = buf.getvalue()
    assert n == 2
    assert "[conflicts] 1 contested subsystem(s); 1 contested method(s)" in out
    assert "first-person / vehicle camera" in out
    assert "Foo::Bar" in out
    import re
    pat = re.compile(r"^\[conflicts\]\s+(\d+)\s+contested subsystem\(s\);\s+"
                     r"(\d+)\s+contested method\(s\)")
    line = next(ln for ln in out.splitlines() if ln.startswith("[conflicts]"))
    m = pat.match(line)
    assert m is not None and m.groups() == ("1", "1")


def test_a_clean_install_says_nothing_but_the_summary(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/A/x.reds", "@wrapMethod(Foo)\nfunc Bar() -> Void { }\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_conflicts(game, {"A-1-1-0-1700000000": ["r6/scripts/A/x.reds"]})
    assert n == 0
    assert buf.getvalue().strip() == "[conflicts] 0 contested subsystem(s); 0 contested method(s)"


def test_gui_regex_is_the_one_shipped_in_deploy_tab():
    src = (Path(__file__).resolve().parent.parent / "gui" / "deploy_tab.py").read_text()
    assert "RE_CONFLICTS" in src and "contested subsystem" in src
    assert "self._conflicts" in src


def _fingerprint(root: Path) -> dict:
    import hashlib
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns,
                           hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def test_the_conflict_check_never_mutates_the_install(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/A/x.reds", "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    _mk(game, "r6/scripts/B/x.reds", "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    mods = {"A-1-1-0-1700000000": ["r6/scripts/A/x.reds"],
            "B-2-1-0-1700000000": ["r6/scripts/B/x.reds"]}
    before = _fingerprint(game)
    with contextlib.redirect_stdout(io.StringIO()):
        D.report_conflicts(game, mods)
    assert _fingerprint(game) == before


def test_check_conflicts_cli(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/A/x.reds", "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    _mk(game, "r6/scripts/B/x.reds", "@replaceMethod(Foo)\nfunc Bar() -> Void { }\n")
    D.save_manifest(game, {
        "A-1-1-0-1700000000": ["r6/scripts/A/x.reds"],
        "B-2-1-0-1700000000": ["r6/scripts/B/x.reds"],
        "Better Vehicle First Person-2202-1-0-1700000000": ["archive/pc/mod/a.archive"],
        "First Person Camera Mod-1786-2-1-1700000001": ["archive/pc/mod/b.archive"],
    })
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-conflicts"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[conflicts] 1 contested subsystem(s); 1 contested method(s)" in r.stdout


def test_check_health_includes_the_conflicts_line(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/a.archive", "x")
    D.save_manifest(game, {"Better Vehicle First Person-2202-1-0-1700000000":
                           ["archive/pc/mod/a.archive"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[conflicts] 0 contested subsystem(s)" in r.stdout


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
    print(f"{n} conflict tests passed")
