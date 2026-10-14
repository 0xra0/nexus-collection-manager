"""Regression tests for requirement detection/reporting in nexus_core.deployer.

Covers: framework detection from shipped file types and declared metadata deps, the
marker-file check that isn't fooled by a dependent mod's own folders, keyword matching
against same-batch archive names, and that requirement info is produced for every mod
installed (FOMOD, structured or loose). Pure stdlib.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402


def _write(root, rel, data=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_detect_from_file_types(tmp_path):
    ex = tmp_path
    _write(ex, "r6/scripts/a.reds")
    _write(ex, "red4ext/plugins/b.dll")
    _write(ex, "bin/x64/plugins/cet/mods/m/init.lua")
    _write(ex, "archive/pc/mod/c.xl")
    reqs = D.detect_requirements(ex)
    assert "Redscript" in reqs
    assert "RED4ext" in reqs
    assert "Cyber Engine Tweaks (CET)" in reqs
    assert "ArchiveXL / TweakXL" in reqs


def test_detect_from_metadata(tmp_path):
    ex = tmp_path
    _write(ex, "mods/mymod/info.json",
           b'{"name":"MyMod","dependencies":["Codeware","Some Framework"]}')
    reqs = D.detect_requirements(ex)
    assert "Codeware" in reqs and "Some Framework" in reqs


def test_no_requirements_for_plain_archive(tmp_path):
    ex = tmp_path
    _write(ex, "archive/pc/mod/only.archive")
    assert D.detect_requirements(ex) == []


def test_marker_not_fooled_by_dependents_own_dirs(tmp_path):
    game = tmp_path
    # a dependent mod's own footprint — must NOT count as the framework being present
    _write(game, "red4ext/plugins/somemod.dll")
    _write(game, "r6/scripts/somemod.reds")
    assert D.requirement_satisfied("RED4ext", set(), game) is False
    assert D.requirement_satisfied("Redscript", set(), game) is False
    # the real marker files DO count
    _write(game, "red4ext/RED4ext.dll")
    _write(game, "engine/tools/scc.exe")
    assert D.requirement_satisfied("RED4ext", set(), game) is True
    assert D.requirement_satisfied("Redscript", set(), game) is True


def test_keyword_match_against_batch_name(tmp_path):
    game = tmp_path
    assert D.requirement_satisfied("ArchiveXL / TweakXL", {"ArchiveXL-4198-1-27"}, game) is True
    assert D.requirement_satisfied("RED4ext", {"RED4ext-2380-1-27"}, game) is True
    assert D.requirement_satisfied("Cyber Engine Tweaks (CET)",
                                   {"CyberEngineTweaks-107-1-35"}, game) is True


def test_cet_no_false_positive_on_engine(tmp_path):
    game = tmp_path
    assert D.requirement_satisfied("Cyber Engine Tweaks (CET)", {"Engine Sound Tweaks"}, game) is False


def test_deploy_populates_requirements_and_rollup(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    ex = tmp_path / "ex"
    _write(ex, "r6/scripts/mod.reds")
    dep = D.Deployer(game, dry_run=False, verbose=False)
    assert dep.deploy(ex, "Script Mod") is True
    assert dep.requirements.get("Script Mod") == ["Redscript"]
    # Redscript is genuinely absent (mod's own r6/scripts must not count)
    assert not D.requirement_satisfied("Redscript", {"Script Mod"}, game)


def test_report_requirements_lists_missing(tmp_path):
    game = tmp_path
    dep = D.Deployer(game, dry_run=True, verbose=False)
    dep.requirements = {"Mod X": ["RED4ext"], "Mod Y": ["RED4ext", "ArchiveXL / TweakXL"]}
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        dep.report_requirements(set(dep.requirements))
    out = buf.getvalue()
    assert "Missing requirements" in out
    assert "RED4ext" in out and "ArchiveXL / TweakXL" in out
    assert "Mod X" in out and "Mod Y" in out


if __name__ == "__main__":
    import inspect
    import tempfile
    n = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        params = inspect.signature(fn).parameters
        kw = {}
        if "tmp_path" in params:
            kw["tmp_path"] = Path(tempfile.mkdtemp())
        fn(**kw)
        print("PASS", name); n += 1
    print(f"{n} requirement tests passed")
