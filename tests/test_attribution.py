"""Regression tests for manifest attribution in nexus_core.deployer.

Observed: "Improved Vehicle Persistence patch-27754-1-0-1771803290" owned exactly one file,
bin/x64/plugins/cyber_engine_tweaks/mods/Dedratruckcustom/ra/dedera55.json — nothing to do
with vehicle persistence — and no 27754 archive existed in Downloads. The manager therefore
reported a patch as installed when it was not.

Cause: a path is written by whichever mod deploys last, but the post-deploy merge only
rewrote entries for mods processed in THAT run. An entry from an earlier run kept claiming
paths a later mod had overwritten, and across runs got whittled down to files that were not
its own. Covered here: the reconciliation that prevents it, and the audit that finds existing
cases. Pure stdlib — no PySide6, no network.
"""
import contextlib
import io
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

HAVE_BSDTAR = bool(__import__("shutil").which("bsdtar"))


def _zip(downloads: Path, stem: str, members) -> Path:
    p = downloads / f"{stem}.zip"
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        for m in members:
            z.writestr(m, "x")
    return p


# ── the cause: stale cross-mod claims ────────────────────────────────────────

def test_stale_claim_is_released_when_another_mod_takes_the_path():
    mods = {
        "Old Mod-100-1-0-1700000000": ["archive/pc/mod/shared.archive",
                                       "archive/pc/mod/mine.archive"],
    }
    new_ownership = {"New Mod-200-1-0-1780000000": ["archive/pc/mod/shared.archive"]}
    mods.update(new_ownership)
    released = D._release_stale_claims(mods, new_ownership)
    assert mods["Old Mod-100-1-0-1700000000"] == ["archive/pc/mod/mine.archive"]
    assert released == [("Old Mod-100-1-0-1700000000", 1, False)]


def test_entry_left_owning_nothing_is_dropped_not_reported_installed():
    """The exact end state of the bug: an entry whose bytes are all gone still being listed
    as installed."""
    mods = {"Ghost-100-1-0-1700000000": ["archive/pc/mod/a.archive"]}
    new_ownership = {"Winner-200-1-0-1780000000": ["archive/pc/mod/a.archive"]}
    mods.update(new_ownership)
    released = D._release_stale_claims(mods, new_ownership)
    assert "Ghost-100-1-0-1700000000" not in mods
    assert released == [("Ghost-100-1-0-1700000000", 1, True)]


def test_untouched_paths_and_mods_are_left_alone():
    """Only paths written in THIS run have a known owner; everything else is not guessed at."""
    mods = {
        "Untouched-100-1-0-1700000000": ["archive/pc/mod/x.archive"],
        "Other-300-1-0-1700000000": ["r6/scripts/other.reds"],
    }
    before = {k: list(v) for k, v in mods.items()}
    new_ownership = {"New-200-1-0-1780000000": ["archive/pc/mod/brand_new.archive"]}
    mods.update(new_ownership)
    assert D._release_stale_claims(mods, new_ownership) == []
    for k, v in before.items():
        assert mods[k] == v


# ── the audit: finding entries that already went wrong ───────────────────────

def test_find_archive_matches_by_stem_then_identity(tmp_path):
    dl = tmp_path
    exact = _zip(dl, "Cool Mod-500-1-0-1700000000", ["a.archive"])
    assert D.find_archive_for("Cool Mod-500-1-0-1700000000", dl) == exact
    # a re-download at another version still identifies the same mod
    assert D.find_archive_for("Cool Mod-500-2-0-1780000000", dl) == exact
    assert D.find_archive_for("Unrelated-999-1-0-1700000000", dl) is None


def test_orphan_entries_are_flagged(tmp_path):
    dl = tmp_path / "dl"; dl.mkdir()
    _zip(dl, "Present-100-1-0-1700000000", ["a.archive"])
    mods = {
        "Present-100-1-0-1700000000": ["archive/pc/mod/a.archive"],
        "Improved Vehicle Persistence patch-27754-1-0-1771803290": [
            "bin/x64/plugins/cyber_engine_tweaks/mods/Dedratruckcustom/ra/dedera55.json"],
    }
    assert D.find_orphan_entries(mods, dl) == [
        "Improved Vehicle Persistence patch-27754-1-0-1771803290"]


def test_orphan_check_is_quiet_without_a_downloads_dir(tmp_path):
    mods = {"X-100-1-0-1700000000": ["a"]}
    assert D.find_orphan_entries(mods, None) == []
    assert D.find_orphan_entries(mods, tmp_path / "nope") == []


def test_mis_attributed_entry_is_identified(tmp_path):
    """The reported shape: owned file's basename appears nowhere in the mod's own archive."""
    if not HAVE_BSDTAR:
        return
    dl = tmp_path / "dl"; dl.mkdir()
    _zip(dl, "Improved Vehicle Persistence patch-27754-1-0-1771803290",
         ["r6/scripts/IVP/persistence.reds"])
    mods = {"Improved Vehicle Persistence patch-27754-1-0-1771803290": [
        "bin/x64/plugins/cyber_engine_tweaks/mods/Dedratruckcustom/ra/dedera55.json"]}
    f = D.check_attribution(mods, dl)[0]
    assert f["verdict"] == "mis-attributed" and f["matched"] == 0


def test_correctly_attributed_entry_is_clean(tmp_path):
    if not HAVE_BSDTAR:
        return
    dl = tmp_path / "dl"; dl.mkdir()
    _zip(dl, "Good Mod-100-1-0-1700000000", ["archive/pc/mod/thing.archive"])
    # deployment relocates but never renames, so the basename still matches
    mods = {"Good Mod-100-1-0-1700000000": ["archive/pc/mod/thing.archive"]}
    assert D.check_attribution(mods, dl) == []


def test_partial_match_is_reported_separately_not_as_bogus(tmp_path):
    """A patch mod legitimately shipping into another mod's folder, or a file another mod
    overwrote, must not be called bogus — only a ZERO match is."""
    if not HAVE_BSDTAR:
        return
    dl = tmp_path / "dl"; dl.mkdir()
    _zip(dl, "Patch Mod-100-1-0-1700000000", ["mine.archive"])
    mods = {"Patch Mod-100-1-0-1700000000": ["archive/pc/mod/mine.archive",
                                             "archive/pc/mod/someone_elses.archive"]}
    f = D.check_attribution(mods, dl)[0]
    assert f["verdict"] == "partial" and f["matched"] == 1

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bogus = D.report_attribution(mods, dl)
    assert bogus == 0                                  # not counted as mis-attributed
    assert "[attribution] 0 mis-attributed; 1 partial" in buf.getvalue()


def test_empty_entry_is_flagged_as_bogus(tmp_path):
    dl = tmp_path / "dl"; dl.mkdir()
    mods = {"Ghost-100-1-0-1700000000": []}
    f = D.check_attribution(mods, dl)[0]
    assert f["verdict"] == "empty"


def test_report_names_the_offender_and_emits_parseable_line(tmp_path):
    if not HAVE_BSDTAR:
        return
    dl = tmp_path / "dl"; dl.mkdir()
    _zip(dl, "IVP patch-27754-1-0-1771803290", ["r6/scripts/IVP/p.reds"])
    mods = {"IVP patch-27754-1-0-1771803290": [
        "bin/x64/plugins/cyber_engine_tweaks/mods/Dedratruckcustom/ra/dedera55.json"]}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_attribution(mods, dl)
    out = buf.getvalue()
    assert n == 1
    assert "IVP patch-27754-1-0-1771803290" in out and "dedera55.json" in out
    assert "[attribution] 1 mis-attributed" in out


def test_foreign_folder_entry_is_detected_without_any_archive():
    """The reported entry's real tell, and the only one available once the download is gone:
    every file it claims lives inside a folder another mod dominates."""
    mods = {
        "Improved Vehicle Persistence patch-27754-1-0-1771803290": [
            "bin/x64/plugins/cyber_engine_tweaks/mods/Dedratruckcustom/ra/dedera55.json"],
        "Dedra Truck Custom-30000-1-0-1770000000": [
            "bin/x64/plugins/cyber_engine_tweaks/mods/Dedratruckcustom/ra/a.json",
            "bin/x64/plugins/cyber_engine_tweaks/mods/Dedratruckcustom/ra/b.json",
            "bin/x64/plugins/cyber_engine_tweaks/mods/Dedratruckcustom/ra/c.json"],
    }
    found = D.find_foreign_folder_entries(mods)
    assert [g["stem"] for g in found] == [
        "Improved Vehicle Persistence patch-27754-1-0-1771803290"]
    assert found[0]["dominators"] == {"Dedra Truck Custom-30000-1-0-1770000000": 1}


def test_a_mod_in_its_own_folder_is_not_foreign():
    mods = {
        "Mine-100-1-0-1700000000": ["r6/scripts/Mine/a.reds", "r6/scripts/Mine/b.reds"],
        "Other-200-1-0-1700000000": ["r6/scripts/Other/a.reds"],
    }
    assert D.find_foreign_folder_entries(mods) == []


def test_patch_mod_with_its_own_files_is_not_flagged():
    """A patch that ships into another mod's folder AND has files of its own is not
    'only living in someone else's folder' — the check must not fire on it."""
    mods = {
        "Patch-100-1-0-1700000000": ["r6/scripts/Patch/main.reds",
                                     "archive/pc/mod/Host/extra.archive"],
        "Host Mod-200-1-0-1700000000": ["archive/pc/mod/Host/a.archive",
                                        "archive/pc/mod/Host/b.archive"],
    }
    assert D.find_foreign_folder_entries(mods) == []


def test_likely_stale_combines_missing_archive_with_foreign_folder(tmp_path):
    dl = tmp_path / "dl"; dl.mkdir()
    _zip(dl, "Dedra Truck Custom-30000-1-0-1770000000", ["ra/a.json"])
    mods = {
        "Improved Vehicle Persistence patch-27754-1-0-1771803290": [
            "bin/x64/plugins/cyber_engine_tweaks/mods/Dedratruckcustom/ra/dedera55.json"],
        "Dedra Truck Custom-30000-1-0-1770000000": [
            "bin/x64/plugins/cyber_engine_tweaks/mods/Dedratruckcustom/ra/a.json",
            "bin/x64/plugins/cyber_engine_tweaks/mods/Dedratruckcustom/ra/b.json"],
    }
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_attribution(mods, dl)
    out = buf.getvalue()
    assert n == 1
    assert "very likely stale" in out
    assert "Improved Vehicle Persistence patch-27754-1-0-1771803290" in out
    assert "1 likely-stale" in out


def test_audit_never_mutates_the_manifest(tmp_path):
    if not HAVE_BSDTAR:
        return
    dl = tmp_path / "dl"; dl.mkdir()
    _zip(dl, "M-100-1-0-1700000000", ["a.archive"])
    mods = {"M-100-1-0-1700000000": ["archive/pc/mod/nope.archive"]}
    snapshot = {k: list(v) for k, v in mods.items()}
    with contextlib.redirect_stdout(io.StringIO()):
        D.report_attribution(mods, dl)
    assert mods == snapshot          # reporting only; removal stays a deliberate act


def test_deploy_reconciles_ownership_end_to_end(tmp_path):
    """Two mods writing the same file across SEPARATE runs: the older entry must stop
    claiming it, which is what never happened before."""
    if not HAVE_BSDTAR:
        return
    game = tmp_path / "game"; game.mkdir()
    dl = tmp_path / "dl"; dl.mkdir()
    for stem in ("First Mod-100-1-0-1700000000", "Second Mod-200-1-0-1780000000"):
        build = tmp_path / "build"; (build / "archive/pc/mod").mkdir(parents=True)
        (build / "archive/pc/mod/contested.archive").write_text(stem)
        z = dl / f"{stem}.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(build / "archive/pc/mod/contested.archive",
                     "archive/pc/mod/contested.archive")
        __import__("shutil").rmtree(build)

    repo = str(Path(__file__).resolve().parent.parent)
    base = [sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
            "--downloads", str(dl), "--tmp", str(tmp_path / "tmp"), "--skip-hash"]
    subprocess.run(base + ["First Mod"], cwd=repo, capture_output=True, text=True)
    assert "First Mod-100-1-0-1700000000" in D.load_manifest(game)["mods"]
    subprocess.run(base + ["Second Mod"], cwd=repo, capture_output=True, text=True)

    mods = D.load_manifest(game)["mods"]
    assert mods.get("Second Mod-200-1-0-1780000000") == ["archive/pc/mod/contested.archive"]
    # the first mod owned only that file, so it is gone rather than falsely 'installed'
    assert "First Mod-100-1-0-1700000000" not in mods


if __name__ == "__main__":
    import inspect
    import tempfile
    if not HAVE_BSDTAR:
        print("NOTE: bsdtar missing — archive-listing tests are skipped")
    n = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        kw = {"tmp_path": Path(tempfile.mkdtemp())} \
            if "tmp_path" in inspect.signature(fn).parameters else {}
        fn(**kw)
        print("PASS", name); n += 1
    print(f"{n} attribution tests passed")
