"""Regression tests for the framework version policy in nexus_core.deployer.

Frameworks (RED4ext, ArchiveXL, TweakXL, Codeware, Redscript, Audioware, RedData, …) are
libraries every dependent mod loads; the newest build is always the right one and an older
one — or a stale duplicate manifest entry that re-deploys the old files — breaks dependents
(the "Missing native class" RED4ext-version failure). Covered here:
  * identifying a framework by Nexus mod-id, NOT by a name that merely mentions one;
  * version ordering by upload epoch (tolerant of the 'v1-9-2' format);
  * superseding older framework builds unconditionally (no "ambiguous keep");
  * de-duplicating the manifest to the newest build, reversibly;
  * the standalone --fix-frameworks dedupe path.
Pure stdlib — no PySide6, no network, no external tools.
"""
import io
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402


def test_framework_identified_by_mod_id_not_name():
    assert D.framework_name("RED4ext-2380-1-30-0-1773082858") == "RED4ext"
    assert D.framework_name("ArchiveXL-4198-1-26-8-1779256460") == "ArchiveXL"
    assert D.framework_name("Codeware-7780-1-20-3-1777845006") == "Codeware"
    # 'v'-prefixed version must still resolve the mod-id
    assert D.framework_name("Audioware-12001-v1-9-2-1775355328") == "Audioware"
    # mods that merely MENTION a framework in their name are NOT frameworks
    assert D.framework_name("Judy's Cozier Apartment (ArchiveXL)-23287-1-1-1755806936") is None
    assert D.framework_name("kabuki messy apartment - archivexl 22567 1.1.0 2026-06-18T05-43Z x") is None
    assert D.framework_name("1 - Project E3 - Time Dilation (redscript)-25267-2-31-1775398570") is None


def test_version_key_orders_by_epoch():
    old = "RED4ext-2380-1-29-1-1760000000"
    new = "RED4ext-2380-1-30-0-1773082858"
    assert D.framework_version_key(new) > D.framework_version_key(old)
    assert D.framework_version_str(new) == "1.30.0"


def test_installed_frameworks_groups_duplicates_newest_first():
    mods = {
        "ArchiveXL-4198-1-26-2-1769626263": ["red4ext/plugins/ArchiveXL/scripts/a.reds"],
        "ArchiveXL-4198-1-26-8-1779256460": ["red4ext/plugins/ArchiveXL/scripts/a.reds"],
        "Some Mod-999-1-0-1700000000": ["archive/pc/mod/x.archive"],  # not a framework
    }
    fw = D.installed_frameworks(mods)
    assert set(fw) == {"4198"}
    assert [e["ver"] for e in fw["4198"]] == ["1.26.8", "1.26.2"]  # newest first


def test_drop_superseded_force_drops_older_framework():
    old = Path("RED4ext-2380-1-29-1-1760000000.zip")
    new = Path("RED4ext-2380-1-30-0-1773082858.zip")
    kept, superseded, ambiguous = D.drop_superseded([old, new])
    assert kept == [new]                       # only newest kept
    assert [o.name for o, _ in superseded] == [old.name]
    assert ambiguous == []                     # frameworks are never "ambiguous"


def _write(root, rel, data="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8")


def test_dedupe_keeps_newest_and_is_reversible(tmp_path):
    game = tmp_path
    # newest owns the shared DLL; the older duplicate also owns it plus a stale-only file
    _write(game, "red4ext/RED4ext.dll", "v1.30")
    _write(game, "red4ext/plugins/Codeware/legacy_only.reds", "old")
    mods = {
        "Codeware-7780-1-20-3-1777845006": ["red4ext/RED4ext.dll"],
        "Codeware-7780-1-19-0-1769434058": ["red4ext/RED4ext.dll",
                                            "red4ext/plugins/Codeware/legacy_only.reds"],
    }
    D.save_manifest(game, dict(mods))

    # dry run mutates nothing
    dry = dict(mods)
    with contextlib.redirect_stdout(io.StringIO()):
        acts = D.dedupe_framework_manifest(game, dry, dry_run=True)
    assert dry == mods and acts and acts[0]["drop_ver"] == "1.19.0"
    assert (game / "red4ext/plugins/Codeware/legacy_only.reds").exists()

    live = dict(mods)
    with contextlib.redirect_stdout(io.StringIO()):
        acts = D.dedupe_framework_manifest(game, live, dry_run=False)
    assert "Codeware-7780-1-19-0-1769434058" not in live      # stale entry gone
    assert "Codeware-7780-1-20-3-1777845006" in live          # newest kept
    assert acts[0]["orphans_moved"] == 1
    assert not (game / "red4ext/plugins/Codeware/legacy_only.reds").exists()   # stale file archived
    assert list((game / D.DUP_BACKUP_DIRNAME).rglob("legacy_only.reds"))       # reversible
    # a shared file the newest still owns is untouched
    assert (game / "red4ext/RED4ext.dll").read_text() == "v1.30"


def test_newest_framework_downloads_picks_latest(tmp_path):
    dl = tmp_path
    for name in ("ArchiveXL-4198-1-26-2-1769626263.zip",
                 "ArchiveXL-4198-1-26-8-1779256460.zip",
                 "Judy's Apartment (ArchiveXL)-23287-1-1-1755806936.zip"):
        (dl / name).write_bytes(b"PK\x03\x04")   # zip magic so is_archive_file passes
    got = D.newest_framework_downloads(dl)
    assert set(got) == {"4198"}                  # the mod-23287 decoy is not a framework
    assert got["4198"][0].name == "ArchiveXL-4198-1-26-8-1779256460.zip"


def test_fix_frameworks_dedupes_when_nothing_newer(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    _write(game, "red4ext/plugins/ArchiveXL/scripts/a.reds", "x")
    D.save_manifest(game, {
        "ArchiveXL-4198-1-26-2-1769626263": ["red4ext/plugins/ArchiveXL/scripts/a.reds"],
        "ArchiveXL-4198-1-26-8-1779256460": ["red4ext/plugins/ArchiveXL/scripts/a.reds"],
    })
    empty_downloads = tmp_path / "dl"; empty_downloads.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        D.action_fix_frameworks(game, empty_downloads, tmp_path, "", dry_run=False)
    mods = D.load_manifest(game)["mods"]
    assert "ArchiveXL-4198-1-26-2-1769626263" not in mods
    assert "ArchiveXL-4198-1-26-8-1779256460" in mods


def test_report_framework_health_emits_line():
    mods = {
        "Codeware-7780-1-19-0-1769434058": ["red4ext/RED4ext.dll"],
        "Codeware-7780-1-20-3-1777845006": ["red4ext/RED4ext.dll"],
    }
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_framework_health(mods)
    out = buf.getvalue()
    assert n == 1
    assert "[frameworks] 1 framework(s) with duplicate/older builds" in out
    assert "keep v1.20.3, drop stale v1.19.0" in out


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
    print(f"{n} framework tests passed")
