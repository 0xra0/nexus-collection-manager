"""Regression tests for broken CET cross-mod lookups in nexus_core.deployer.

Observed: Immersive First Person finds its companion with GetMod("BetterVehicleFirstPerson"),
but Better Vehicle First Person 1.4.2 installs as the CET mod folder "EnhancedVehicleCamera".
The lookup returns nil forever, the integration never runs, both mods drive the vehicle camera
uncoordinated — and nothing logs anything, because nil is the ordinary answer meaning "that
mod isn't installed".

That ambiguity is the whole problem, and most of these tests defend the line between it and a
real fault: a missing lookup is only a break when the companion is demonstrably present under
another name. Pure stdlib — no PySide6, no network.
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

CET = "bin/x64/plugins/cyber_engine_tweaks/mods"


def _lua(game: Path, mod: str, text: str, name: str = "init.lua") -> Path:
    p = game / CET / mod / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _verdicts(game: Path, mods=None, renames=None):
    return {(b["caller"], b["target"]): (b["verdict"], b["resolved"])
            for b in D.find_cet_lookup_breaks(game, mods or {}, renames or {})}


# ── the reported case ────────────────────────────────────────────────────────

def test_the_reported_rename_is_caught(tmp_path):
    game = tmp_path / "game"
    _lua(game, "ImmersiveFirstPerson",
         'local bvfp = GetMod("BetterVehicleFirstPerson")\n'
         'if bvfp then bvfp.Register(self) end\n')
    _lua(game, "EnhancedVehicleCamera", "return {}\n")
    got = _verdicts(game)
    assert got[("ImmersiveFirstPerson", "BetterVehicleFirstPerson")] == \
        ("renamed", "EnhancedVehicleCamera")


def test_the_builtin_rename_table_holds_the_reported_pair():
    assert D.CET_FOLDER_RENAMES["BetterVehicleFirstPerson"] == "EnhancedVehicleCamera"


def test_a_rename_whose_new_folder_is_absent_is_only_absent(tmp_path):
    """Knowing a folder was renamed proves nothing on its own. If the new name is not
    installed either, the companion really is missing and the mod is behaving correctly."""
    game = tmp_path / "game"
    _lua(game, "ImmersiveFirstPerson", 'local m = GetMod("BetterVehicleFirstPerson")\n')
    assert _verdicts(game)[("ImmersiveFirstPerson", "BetterVehicleFirstPerson")] == \
        ("absent", "")


# ── the line between "broken" and "working as designed" ──────────────────────

def test_a_resolving_lookup_is_not_reported(tmp_path):
    game = tmp_path / "game"
    _lua(game, "Caller", 'local m = GetMod("Companion")\n')
    _lua(game, "Companion", "return {}\n")
    assert D.find_cet_lookup_breaks(game, {}, {}) == []


def test_an_uninstalled_optional_companion_is_not_counted_as_broken(tmp_path):
    """Most GetMod calls are optional integration; nil is the designed answer. Counting these
    as faults would put a warning on a correctly working install."""
    game = tmp_path / "game"
    _lua(game, "Caller", 'local m = GetMod("SomethingNeverInstalled")\n')
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_cet_links(game, {})
    assert n == 0
    assert "[cet-links] 0 broken cross-mod lookup(s); 1 companion(s) not installed" in buf.getvalue()


def test_a_mod_looking_up_itself_is_ignored(tmp_path):
    game = tmp_path / "game"
    _lua(game, "SelfRef", 'local me = GetMod("SelfRef")\n')
    assert D.find_cet_lookup_breaks(game, {}, {}) == []


# ── case sensitivity: a Linux-only break that looks fine on Windows ──────────

def test_case_only_mismatch_is_a_confirmed_break(tmp_path):
    """CET matches the folder name exactly and this filesystem is case-sensitive, so a
    lookup that works on a Windows install fails here. Exactly the class of fault that gets
    reported as 'works for everyone else'."""
    game = tmp_path / "game"
    _lua(game, "Caller", 'local m = GetMod("companionmod")\n')
    _lua(game, "CompanionMod", "return {}\n")
    got = _verdicts(game)
    assert got[("Caller", "companionmod")] == ("case-mismatch", "CompanionMod")


def test_a_known_rename_outranks_a_case_match(tmp_path):
    """When both could apply, the recorded rename is the better answer — it names the mod the
    author actually meant, not merely a folder that looks similar."""
    game = tmp_path / "game"
    _lua(game, "Caller", 'local m = GetMod("OldName")\n')
    _lua(game, "oldname", "return {}\n")
    _lua(game, "NewName", "return {}\n")
    got = _verdicts(game, renames={"OldName": "NewName"})
    assert got[("Caller", "OldName")] == ("renamed", "NewName")


# ── parsing ──────────────────────────────────────────────────────────────────

def test_commented_out_lookups_are_ignored(tmp_path):
    """Lua uses -- and --[[ ]]; example and disabled code is full of GetMod calls."""
    game = tmp_path / "game"
    _lua(game, "Caller",
         '-- local m = GetMod("GhostOne")\n'
         '--[[ local n = GetMod("GhostTwo")\n     more text ]]\n'
         'local ok = GetMod("Real")\n')
    _lua(game, "Real", "return {}\n")
    assert D.find_cet_lookup_breaks(game, {}, {}) == []


def test_both_quote_styles_and_odd_spacing_parse():
    src = ('GetMod("A")\n'
           "GetMod('B')\n"
           'GetMod ( "C" )\n'
           'local x = GetMod("D") or {}\n')
    assert D.parse_lua_getmod(src) == {"A", "B", "C", "D"}


def test_a_variable_argument_is_skipped():
    """Only literal names can be checked; a computed one is not evidence of anything."""
    assert D.parse_lua_getmod("local n = name; local m = GetMod(n)\n") == set()


def test_lookups_are_found_in_any_lua_file_not_just_init(tmp_path):
    game = tmp_path / "game"
    _lua(game, "Caller", "return {}\n")
    _lua(game, "Caller", 'local m = GetMod("Missing")\n', name="modules/bridge.lua")
    assert [b["target"] for b in D.find_cet_lookup_breaks(game, {}, {})] == ["Missing"]


def test_the_caller_is_the_mod_folder_not_the_file(tmp_path):
    game = tmp_path / "game"
    _lua(game, "TheCaller", 'local m = GetMod("Missing")\n', name="sub/deep/x.lua")
    b = D.find_cet_lookup_breaks(game, {}, {})[0]
    assert b["caller"] == "TheCaller"
    assert b["rel"] == f"{CET}/TheCaller/sub/deep/x.lua"


# ── installed-folder enumeration ─────────────────────────────────────────────

def test_installed_folders_come_from_disk_not_the_manifest(tmp_path):
    """It must match what CET itself enumerates at runtime, which includes hand-installed
    mods no manifest entry owns."""
    game = tmp_path / "game"
    _lua(game, "Caller", 'local m = GetMod("HandInstalled")\n')
    _lua(game, "HandInstalled", "return {}\n")
    assert D.find_cet_lookup_breaks(game, {}, {}) == []          # empty manifest
    assert D.installed_cet_mods(game) == ["Caller", "HandInstalled"]


def test_backup_dirs_are_not_counted_as_installed_mods(tmp_path):
    game = tmp_path / "game"
    (game / CET / D.DUP_BACKUP_DIRNAME).mkdir(parents=True)
    _lua(game, "Real", "return {}\n")
    assert D.installed_cet_mods(game) == ["Real"]


def test_no_cet_folder_at_all_is_not_an_error(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    assert D.installed_cet_mods(game) == []
    assert D.find_cet_lookup_breaks(game, {}, {}) == []


# ── overrides ────────────────────────────────────────────────────────────────

def test_user_renames_are_read_from_config_dir(tmp_path):
    old = D.CONFIG_DIR
    try:
        D.CONFIG_DIR = tmp_path / "cfg"
        D.CONFIG_DIR.mkdir()
        (D.CONFIG_DIR / D.CET_RENAMES_NAME).write_text('{"OldThing": "NewThing"}')
        assert D.load_cet_renames() == {"OldThing": "NewThing"}
    finally:
        D.CONFIG_DIR = old


def test_a_corrupt_rename_file_does_not_break_the_check(tmp_path):
    old = D.CONFIG_DIR
    try:
        D.CONFIG_DIR = tmp_path / "cfg"
        D.CONFIG_DIR.mkdir()
        (D.CONFIG_DIR / D.CET_RENAMES_NAME).write_text("[not, a, dict]")
        assert D.load_cet_renames() == {}
    finally:
        D.CONFIG_DIR = old


def test_a_user_rename_catches_a_pair_the_builtin_table_does_not(tmp_path):
    game = tmp_path / "game"
    _lua(game, "Caller", 'local m = GetMod("SomeOldFolder")\n')
    _lua(game, "SomeNewFolder", "return {}\n")
    assert _verdicts(game)[("Caller", "SomeOldFolder")] == ("absent", "")
    assert _verdicts(game, renames={"SomeOldFolder": "SomeNewFolder"})[
        ("Caller", "SomeOldFolder")] == ("renamed", "SomeNewFolder")


# ── report and read-only guarantee ───────────────────────────────────────────

def test_report_emits_a_parseable_line_the_gui_matches(tmp_path):
    game = tmp_path / "game"
    _lua(game, "ImmersiveFirstPerson", 'local m = GetMod("BetterVehicleFirstPerson")\n')
    _lua(game, "EnhancedVehicleCamera", "return {}\n")
    _lua(game, "Other", 'local m = GetMod("NotInstalledAnywhere")\n')
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_cet_links(game, {})
    out = buf.getvalue()
    assert n == 1
    assert "[cet-links] 1 broken cross-mod lookup(s); 1 companion(s) not installed" in out
    assert 'GetMod("BetterVehicleFirstPerson")' in out
    assert '"EnhancedVehicleCamera"' in out
    import re
    pat = re.compile(r"^\[cet-links\]\s+(\d+)\s+broken cross-mod lookup\(s\);\s+"
                     r"(\d+)\s+companion")
    line = next(ln for ln in out.splitlines() if ln.startswith("[cet-links]"))
    m = pat.match(line)
    assert m is not None and m.groups() == ("1", "1")


def test_a_clean_install_says_nothing_but_the_summary(tmp_path):
    game = tmp_path / "game"
    _lua(game, "A", 'local m = GetMod("B")\n')
    _lua(game, "B", "return {}\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_cet_links(game, {})
    assert n == 0
    assert buf.getvalue().strip() == \
        "[cet-links] 0 broken cross-mod lookup(s); 0 companion(s) not installed"


def test_gui_regex_is_the_one_shipped_in_deploy_tab():
    src = (Path(__file__).resolve().parent.parent / "gui" / "deploy_tab.py").read_text()
    assert "RE_CET_LINKS" in src and "broken cross-mod lookup" in src
    assert "self._cet_links" in src


def _fingerprint(root: Path) -> dict:
    import hashlib
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns,
                           hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def test_the_check_never_mutates_the_install(tmp_path):
    game = tmp_path / "game"
    _lua(game, "ImmersiveFirstPerson", 'local m = GetMod("BetterVehicleFirstPerson")\n')
    _lua(game, "EnhancedVehicleCamera", "return {}\n")
    before = _fingerprint(game)
    with contextlib.redirect_stdout(io.StringIO()):
        D.report_cet_links(game, {})
    assert _fingerprint(game) == before


def test_check_cet_links_cli(tmp_path):
    game = tmp_path / "game"
    _lua(game, "ImmersiveFirstPerson", 'local m = GetMod("BetterVehicleFirstPerson")\n')
    _lua(game, "EnhancedVehicleCamera", "return {}\n")
    D.save_manifest(game, {"Immersive First Person-2675-1-0-1700000000":
                           [f"{CET}/ImmersiveFirstPerson/init.lua"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-cet-links"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[cet-links] 1 broken cross-mod lookup(s)" in r.stdout
    assert "EnhancedVehicleCamera" in r.stdout


def test_check_health_includes_the_cet_links_line(tmp_path):
    game = tmp_path / "game"
    _lua(game, "A", 'local m = GetMod("B")\n')
    _lua(game, "B", "return {}\n")
    D.save_manifest(game, {"A-1-1-0-1700000000": [f"{CET}/A/init.lua"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[cet-links] 0 broken cross-mod lookup(s)" in r.stdout


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
    print(f"{n} CET cross-mod link tests passed")
