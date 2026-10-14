"""Regression tests for unmanaged-file detection in nexus_core.deployer.

Observed: r6/scripts/REDCheatMenu2.reds, 238 KB, sat in the install belonging to no manifest
entry at all. The manager's only record of the install is the manifest, so a file it did not
write is invisible to it: --remove never takes it away, an update never replaces it, and it
happily shadows a managed copy of the same name.

The detector's real difficulty is not finding unowned files — it is NOT drowning the report in
the runtime state that legitimately lives under the same roots (CET per-mod settings, a
RED4ext plugin's .log beside its .dll). Most of what follows guards that line. Pure stdlib —
no PySide6, no network.
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402


def _mk(game: Path, rel: str, size: int = 8) -> Path:
    p = game / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


# ── the reported case ────────────────────────────────────────────────────────

def test_hand_installed_reds_in_no_entry_is_found(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/Managed/thing.reds")
    _mk(game, "r6/scripts/REDCheatMenu2.reds", 238 * 1024)
    mods = {"Some Mod-100-1-0-1700000000": ["r6/scripts/Managed/thing.reds"]}
    found = D.find_unmanaged_files(game, mods)
    assert {u["rel"] for u in found["units"]} == {"r6/scripts/REDCheatMenu2.reds"}
    assert found["payload"] == 1
    assert found["units"][0]["bytes"] == 238 * 1024


def test_a_fully_managed_install_reports_nothing(tmp_path):
    game = tmp_path / "game"
    for rel in ("r6/scripts/A/a.reds", "archive/pc/mod/b.archive", "r6/tweaks/c.tweak"):
        _mk(game, rel)
    mods = {"M-1-1-0-1700000000": ["r6/scripts/A/a.reds", "archive/pc/mod/b.archive",
                                   "r6/tweaks/c.tweak"]}
    found = D.find_unmanaged_files(game, mods)
    assert found["units"] == [] and found["payload"] == 0


# ── the false-positive line: runtime state must stay quiet ───────────────────

def test_a_plugins_log_beside_its_managed_dll_is_not_reported(tmp_path):
    """Every RED4ext plugin writes its own .log next to its .dll on first launch. Reporting
    those would put a line under nearly every installed framework, every run, forever."""
    game = tmp_path / "game"
    _mk(game, "red4ext/plugins/Codeware/Codeware.dll")
    _mk(game, "red4ext/plugins/Codeware/Codeware.log", 50000)
    mods = {"Codeware-7780-1-0-1700000000": ["red4ext/plugins/Codeware/Codeware.dll"]}
    found = D.find_unmanaged_files(game, mods)
    assert found["units"] == []          # nothing named
    assert found["other"] == 1           # but still counted, not pretended away


def test_cet_per_mod_settings_are_not_reported(tmp_path):
    """CET writes each mod's saved settings into that mod's own folder. The .lua payload is
    managed; the json the game wrote next to it never appears in any manifest."""
    game = tmp_path / "game"
    root = "bin/x64/plugins/cyber_engine_tweaks/mods/SomeMod"
    _mk(game, f"{root}/init.lua")
    _mk(game, f"{root}/settings.json")
    _mk(game, f"{root}/db.sqlite3")
    mods = {"Some Mod-100-1-0-1700000000": [f"{root}/init.lua"]}
    found = D.find_unmanaged_files(game, mods)
    assert found["units"] == []
    assert found["other"] == 2


def test_a_hand_installed_cet_mod_is_reported_despite_its_own_generated_files(tmp_path):
    """The mirror of the previous test: same shape, but nothing here is managed. The unit
    must surface, and its generated files count toward its size rather than hiding it."""
    game = tmp_path / "game"
    root = "bin/x64/plugins/cyber_engine_tweaks/mods/HandMod"
    _mk(game, f"{root}/init.lua", 1000)
    _mk(game, f"{root}/settings.json", 200)
    found = D.find_unmanaged_files(game, {"Other-1-1-0-1700000000": ["archive/pc/mod/x.archive"]})
    assert [u["rel"] for u in found["units"]] == [root]
    assert found["units"][0]["files"] == 2 and found["units"][0]["bytes"] == 1200
    assert found["units"][0]["payload"] == 1


def test_a_hand_installed_tweakxl_yaml_is_payload(tmp_path):
    """r6/tweaks exists only to hold TweakXL mods and their payload is plain .yaml. Judging
    that root by extension is exactly how this check would miss a whole class of mod."""
    game = tmp_path / "game"
    _mk(game, "r6/tweaks/handmod/prices.yaml", 4000)
    found = D.find_unmanaged_files(game, {"M-1-1-0-1700000000": ["archive/pc/mod/x.archive"]})
    assert [u["rel"] for u in found["units"]] == ["r6/tweaks/handmod"]
    assert found["payload"] == 1


def test_a_redmod_info_json_is_payload(tmp_path):
    """A REDmod's info.json is content, not runtime state — mods/ holds nothing else."""
    game = tmp_path / "game"
    _mk(game, "mods/HandRedmod/info.json", 200)
    _mk(game, "mods/HandRedmod/archive/mod.archive", 9000)
    found = D.find_unmanaged_files(game, {"M-1-1-0-1700000000": ["archive/pc/mod/x.archive"]})
    assert [u["rel"] for u in found["units"]] == ["mods/HandRedmod"]
    assert found["payload"] == 2 and found["other"] == 0


def test_files_outside_the_managed_roots_are_ignored(tmp_path):
    """Vanilla game content, saves and logs are not the manager's business. Walking the whole
    game root would report tens of thousands of files that no mod manager ever owns."""
    game = tmp_path / "game"
    _mk(game, "archive/pc/content/basegame_1.archive")
    _mk(game, "r6/cache/final.redscripts")
    _mk(game, "r6/logs/redscript_rCURRENT.log")
    _mk(game, "bin/x64/Cyberpunk2077.exe")
    _mk(game, "engine/tools/scc.exe")
    found = D.find_unmanaged_files(game, {"M-1-1-0-1700000000": ["archive/pc/mod/x.archive"]})
    assert found["units"] == [] and found["payload"] == 0 and found["other"] == 0


def test_our_own_backup_dirs_are_skipped(tmp_path):
    """--fix-duplicates and --fix-versions park retired copies inside the game root. Those are
    unowned by construction; reporting them would mean every fix creates a new finding."""
    game = tmp_path / "game"
    _mk(game, f"archive/pc/mod/{D.DUP_BACKUP_DIRNAME}/old/x.archive")
    _mk(game, "r6/scripts/.mod_trash/gone.reds")
    found = D.find_unmanaged_files(game, {"M-1-1-0-1700000000": ["archive/pc/mod/y.archive"]})
    assert found["units"] == []


# ── case-insensitivity: the noise mode that would make it unusable ───────────

def test_case_drift_between_manifest_and_disk_is_not_unmanaged(tmp_path):
    """The game reads this tree through Wine, so Scripts/ and scripts/ are one path to it.
    An exact comparison would report every file of every case-drifted mod as unmanaged —
    thousands of lines, all wrong. See find_version_duplicates, which folds case for the
    same reason."""
    game = tmp_path / "game"
    _mk(game, "red4ext/plugins/Codeware/Scripts/Api.reds")
    mods = {"Codeware-7780-1-0-1700000000": ["red4ext/plugins/Codeware/scripts/api.reds"]}
    assert D.find_unmanaged_files(game, mods)["units"] == []


# ── grouping: one line per mod, not per file ─────────────────────────────────

def test_a_multi_file_mod_folder_is_one_unit(tmp_path):
    game = tmp_path / "game"
    for i in range(40):
        _mk(game, f"archive/pc/mod/BigMod/part{i}.archive", 100)
    found = D.find_unmanaged_files(game, {"M-1-1-0-1700000000": ["r6/scripts/x.reds"]})
    assert [u["rel"] for u in found["units"]] == ["archive/pc/mod/BigMod"]
    assert found["units"][0]["files"] == 40
    assert found["payload"] == 40


def test_units_are_ordered_biggest_first(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/small.archive", 10)
    _mk(game, "archive/pc/mod/huge.archive", 900000)
    _mk(game, "archive/pc/mod/mid.archive", 5000)
    found = D.find_unmanaged_files(game, {"M-1-1-0-1700000000": ["r6/scripts/x.reds"]})
    assert [u["rel"] for u in found["units"]] == [
        "archive/pc/mod/huge.archive", "archive/pc/mod/mid.archive",
        "archive/pc/mod/small.archive"]


def test_a_partly_managed_folder_reports_only_the_extra_payload(tmp_path):
    """A mod folder the manager owns, with a file added by hand. The unit surfaces because
    the stray is payload, and it is the stray that makes the mod un-updatable."""
    game = tmp_path / "game"
    _mk(game, "r6/scripts/Mod/a.reds")
    _mk(game, "r6/scripts/Mod/handmade.reds", 500)
    mods = {"M-1-1-0-1700000000": ["r6/scripts/Mod/a.reds"]}
    found = D.find_unmanaged_files(game, mods)
    assert [u["rel"] for u in found["units"]] == ["r6/scripts/Mod"]
    assert found["units"][0]["files"] == 1 and found["units"][0]["bytes"] == 500


# ── the report ───────────────────────────────────────────────────────────────

def test_empty_manifest_reports_nothing_rather_than_everything(tmp_path):
    """With nothing recorded as installed, every file under every root is technically
    unmanaged. Printing that total is true and useless — it only means the manifest is
    empty, and a first-run user would read it as 'your install is broken'."""
    game = tmp_path / "game"
    for i in range(5):
        _mk(game, f"archive/pc/mod/m{i}.archive")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_unmanaged(game, {})
    assert n == 0
    assert "[unmanaged] 0 unmanaged mod unit(s); 0 payload file(s)" in buf.getvalue()
    assert "manifest is empty" in buf.getvalue()


def test_report_emits_a_parseable_line_the_gui_matches(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/REDCheatMenu2.reds", 238 * 1024)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_unmanaged(game, {"M-1-1-0-1700000000": ["archive/pc/mod/x.archive"]})
    out = buf.getvalue()
    assert n == 1
    assert "[unmanaged] 1 unmanaged mod unit(s); 1 payload file(s)" in out
    assert "r6/scripts/REDCheatMenu2.reds  (238 KB)" in out
    # the GUI's parser must actually accept it — this line has been added twice before and
    # silently ignored by the GUI both times
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import re
    pat = re.compile(r"^\[unmanaged\]\s+(\d+)\s+unmanaged mod unit\(s\);\s+(\d+)\s+payload")
    line = next(ln for ln in out.splitlines() if ln.startswith("[unmanaged]"))
    m = pat.match(line)
    assert m and (m.group(1), m.group(2)) == ("1", "1")


def test_gui_regex_is_the_one_shipped_in_deploy_tab():
    """Reads the GUI source rather than importing it (no PySide6 in the test env) and checks
    the pattern is present. A parser that silently stops matching is the failure mode this
    whole assertion exists for."""
    src = (Path(__file__).resolve().parent.parent / "gui" / "deploy_tab.py").read_text()
    assert "RE_UNMANAGED" in src and "unmanaged mod unit" in src
    assert "self._unmanaged" in src


# ── read-only guarantee ──────────────────────────────────────────────────────

def _fingerprint(root: Path) -> dict:
    import hashlib
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns,
                           hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def test_the_unmanaged_check_never_mutates_the_install(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/Hand.reds", 4096)
    _mk(game, "archive/pc/mod/Managed.archive")
    _mk(game, "red4ext/plugins/P/P.log")
    mods = {"M-1-1-0-1700000000": ["archive/pc/mod/Managed.archive"]}
    before = _fingerprint(game)
    with contextlib.redirect_stdout(io.StringIO()):
        D.report_unmanaged(game, mods)
    assert _fingerprint(game) == before


# ── end to end ───────────────────────────────────────────────────────────────

def test_check_unmanaged_cli(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/REDCheatMenu2.reds", 238 * 1024)
    _mk(game, "archive/pc/mod/Managed.archive")
    D.save_manifest(game, {"M-1-1-0-1700000000": ["archive/pc/mod/Managed.archive"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-unmanaged"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[unmanaged] 1 unmanaged mod unit(s); 1 payload file(s)" in r.stdout
    assert "REDCheatMenu2.reds" in r.stdout


def test_check_health_includes_the_unmanaged_line(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/REDCheatMenu2.reds", 238 * 1024)
    _mk(game, "archive/pc/mod/Managed.archive")
    D.save_manifest(game, {"M-1-1-0-1700000000": ["archive/pc/mod/Managed.archive"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[unmanaged] 1 unmanaged mod unit(s)" in r.stdout


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
    print(f"{n} unmanaged-file tests passed")
