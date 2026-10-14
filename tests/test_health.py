"""Regression tests for the installed-mod health detector / case-duplicate fixer.

These guard the "SYM_REDEFINITION across a whole mod" failure: on a case-sensitive
filesystem an old install's `Core/` and a new install's `core/` are two folders that
both declare the same redscript module, so every type is defined twice and the whole
compile aborts (all script mods then fail to load). Covered here:
  * detection of case-variant sibling folders, redscript ones flagged;
  * owner attribution from the manifest, and skipping our own backup/trash dirs;
  * the coherence decision (keep the version the rest of the mod references);
  * the reversible fix (stray moved to a backup, manifest pruned, dry-run inert);
  * PREVENTION — a later deploy reuses the casing already on disk instead of creating
    a second case-variant.
Pure stdlib — no PySide6, no network, no external tools.
"""
import io
import os
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402


def _write(root: Path, rel: str, data: str = "x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8")


def _touch(path: Path, epoch: int):
    """Stamp a file's mtime — framework builds are told apart by the archive's timestamps,
    which copy2 preserves on deploy."""
    os.utime(path, (epoch, epoch))


def _two_version_mod(game: Path):
    """A DisclosedHiddenGems-style collision: two case-variant Core folders that are
    different designs, plus the rest of the mod which references only the real one."""
    _write(game, "r6/scripts/DHG/Core/events.reds",
           "module DHG.Core\npublic class HourPassedEvent extends Event {}\n")
    _write(game, "r6/scripts/DHG/core/events.reds",
           "module DHG.Core\npublic class TimeTickEvent extends Event {}\n")
    # rest of the mod references the Core (uppercase) design and none of the lowercase one
    _write(game, "r6/scripts/DHG/Annotations/PlayerPuppet.reds",
           "module DHG\nfunc f() { let e = new HourPassedEvent(); }\n")


def test_detects_redscript_case_collision(tmp_path):
    _two_version_mod(tmp_path)
    cols = D.find_case_collisions(tmp_path, {})
    assert len(cols) == 1
    c = cols[0]
    assert c["is_redscript"] is True
    assert c["parent_rel"] == "r6/scripts/DHG"
    assert sorted(v["name"] for v in c["variants"]) == ["Core", "core"]


def test_owner_attribution_and_file_counts(tmp_path):
    _two_version_mod(tmp_path)
    manifest = {
        "DHG-1-0-1000": ["r6/scripts/DHG/Core/events.reds"],
        "DHG-2-0-2000": ["r6/scripts/DHG/core/events.reds"],
    }
    c = D.find_case_collisions(tmp_path, manifest)[0]
    by_name = {v["name"]: v for v in c["variants"]}
    assert by_name["Core"]["owners"] == ["DHG-1-0-1000"]
    assert by_name["core"]["owners"] == ["DHG-2-0-2000"]
    assert by_name["Core"]["files"] == 1 and by_name["core"]["files"] == 1


def test_non_redscript_collision_flagged_but_not_redscript(tmp_path):
    _write(tmp_path, "bin/x64/plugins/cyber_engine_tweaks/mods/Foo/init.lua")
    _write(tmp_path, "bin/x64/plugins/cyber_engine_tweaks/mods/foo/init.lua")
    cols = D.find_case_collisions(tmp_path, {})
    assert len(cols) == 1 and cols[0]["is_redscript"] is False


def test_backup_and_trash_dirs_are_skipped(tmp_path):
    # a collision *inside* our own backup/trash must never be reported
    _write(tmp_path, f"{D.DUP_BACKUP_DIRNAME}/ts/A/x.reds")
    _write(tmp_path, f"{D.DUP_BACKUP_DIRNAME}/ts/a/x.reds")
    _write(tmp_path, ".mod_trash/ts/B/y.reds")
    _write(tmp_path, ".mod_trash/ts/b/y.reds")
    assert D.find_case_collisions(tmp_path, {}) == []


def test_decide_keeps_coherent_version(tmp_path):
    _two_version_mod(tmp_path)
    c = D.find_case_collisions(tmp_path, {})[0]
    keep, drop, reason = D.decide_redscript_duplicate(c)
    assert keep is not None
    assert keep["name"] == "Core" and [d["name"] for d in drop] == ["core"]
    assert "unique to 'Core'" in reason


def test_decide_tie_falls_back_to_larger_copy(tmp_path):
    # neither side's identifiers are referenced by the rest → decide by file count
    _write(tmp_path, "r6/scripts/M/Core/a.reds", "module M.Core\nclass A {}\n")
    _write(tmp_path, "r6/scripts/M/Core/b.reds", "module M.Core\nclass B {}\n")
    _write(tmp_path, "r6/scripts/M/core/a.reds", "module M.Core\nclass C {}\n")
    _write(tmp_path, "r6/scripts/M/other.reds", "module M\nfunc f() {}\n")  # references neither
    c = D.find_case_collisions(tmp_path, {})[0]
    keep, _drop, reason = D.decide_redscript_duplicate(c)
    assert keep is not None and keep["name"] == "Core" and "larger copy" in reason


def test_decide_fully_inconclusive_returns_none(tmp_path):
    _write(tmp_path, "r6/scripts/M/Core/a.reds", "module M.Core\nclass A {}\n")
    _write(tmp_path, "r6/scripts/M/core/a.reds", "module M.Core\nclass B {}\n")  # equal size, no rest
    c = D.find_case_collisions(tmp_path, {})[0]
    keep, _drop, reason = D.decide_redscript_duplicate(c)
    assert keep is None and "inconclusive" in reason


def test_fix_is_reversible_and_prunes_manifest(tmp_path):
    _two_version_mod(tmp_path)
    D.save_manifest(tmp_path, {
        "DHG-Core": ["r6/scripts/DHG/Core/events.reds", "r6/scripts/DHG/Annotations/PlayerPuppet.reds"],
        "DHG-core": ["r6/scripts/DHG/core/events.reds"],
    })
    # neither entry carries a Nexus id, so no version order settles this: it falls back to
    # identifier coherence, which is a guess and therefore opt-in.
    with contextlib.redirect_stdout(io.StringIO()):
        D.action_fix_duplicates(tmp_path, dry_run=True, apply_heuristics=True)
    assert (tmp_path / "r6/scripts/DHG/core/events.reds").exists()
    assert "DHG-core" in D.load_manifest(tmp_path)["mods"]

    with contextlib.redirect_stdout(io.StringIO()):
        D.action_fix_duplicates(tmp_path, dry_run=False, apply_heuristics=True)
    # stray lowercase copy moved out, coherent uppercase kept
    assert not (tmp_path / "r6/scripts/DHG/core").exists()
    assert (tmp_path / "r6/scripts/DHG/Core/events.reds").exists()
    # reversible: the moved copy is recoverable under the backup dir
    backups = list((tmp_path / D.DUP_BACKUP_DIRNAME).rglob("events.reds"))
    assert backups and backups[0].read_text().find("TimeTickEvent") != -1
    # manifest no longer points at the vanished path; the now-empty mod is dropped
    mods = D.load_manifest(tmp_path)["mods"]
    assert "DHG-core" not in mods
    assert "r6/scripts/DHG/Core/events.reds" in mods["DHG-Core"]
    # collision is gone
    assert D.find_case_collisions(tmp_path, {}) == []


def test_merge_folds_disjoint_nonredscript_twin(tmp_path):
    """A stray file dropped into a wrong-cased twin (e.g. CET nativeinteractions/) is
    losslessly folded back into the real framework folder; the twin is archived."""
    base = "bin/x64/plugins/cyber_engine_tweaks/mods"
    _write(tmp_path, f"{base}/NI/init.lua", "framework")
    _write(tmp_path, f"{base}/NI/db.sqlite3", "db")
    _write(tmp_path, f"{base}/ni/projects/stray.json", "stray")   # disjoint 1-file twin
    c = D.find_case_collisions(tmp_path, {})[0]
    keep, _drops, mode, _reason = D.classify_collision(c)
    assert keep is not None and mode == "merge" and keep["name"] == "NI"
    with contextlib.redirect_stdout(io.StringIO()):
        D.action_fix_duplicates(tmp_path, dry_run=False)
    assert (tmp_path / base / "NI/projects/stray.json").read_text() == "stray"  # folded in
    assert not (tmp_path / base / "ni").exists()                                 # twin gone
    assert list((tmp_path / D.DUP_BACKUP_DIRNAME).rglob("stray.json"))           # reversible
    assert D.find_case_collisions(tmp_path, {}) == []


def test_normalize_preserves_mod_owned_casing():
    """ROOT CAUSE: normalize_rel_path lowercased 'scripts'/'data'/'core' at every depth, so
    it rewrote a mod's OWN folders — 'DisclosedHiddenGems/Core' -> 'core',
    'Codeware/Scripts' -> 'scripts'. Wherever anything else had written the true casing that
    manufactured the case-duplicate. Normalize the game's structure only; below it the
    archive's casing is authoritative."""
    n = lambda p: D.normalize_rel_path(Path(p)).as_posix()   # noqa: E731
    # the game's own structure is still normalized, at any casing
    assert n("R6/Scripts/Foo/Bar.reds") == "r6/scripts/Foo/Bar.reds"
    assert n("Archive/PC/Mod/x.archive") == "archive/pc/mod/x.archive"
    assert n("RED4ext/Plugins/ArchiveXL/ArchiveXL.dll") == "red4ext/plugins/ArchiveXL/ArchiveXL.dll"
    # below it, mod-owned casing survives verbatim
    assert n("r6/scripts/DisclosedHiddenGems/Core/MappinManager.reds") == \
        "r6/scripts/DisclosedHiddenGems/Core/MappinManager.reds"
    assert n("red4ext/plugins/Codeware/Scripts/Codeware.Global.reds") == \
        "red4ext/plugins/Codeware/Scripts/Codeware.Global.reds"
    assert n("red4ext/plugins/TweakXL/Data/ExtraFlats.dat") == \
        "red4ext/plugins/TweakXL/Data/ExtraFlats.dat"


def test_deploy_does_not_manufacture_case_duplicate(tmp_path):
    """End to end: deploying a mod that ships 'Core/' must not create a second 'core/'."""
    game = tmp_path / "game"; game.mkdir()
    src = tmp_path / "src" / "r6" / "scripts" / "DHG" / "Core"
    src.mkdir(parents=True)
    (src / "a.reds").write_text("module DHG.Core\nclass A {}\n", encoding="utf-8")
    dep = D.Deployer(game, dry_run=False, verbose=False)
    dep._deploy_tree(tmp_path / "src", game, "DHG-1-0")
    assert (game / "r6/scripts/DHG/Core/a.reds").exists()
    assert not (game / "r6/scripts/DHG/core").exists()
    assert D.find_case_collisions(game, {}) == []


def test_framework_bundle_keeps_newer_build_not_the_manifest_owner(tmp_path):
    """Regression: the Codeware 'JobQueue' break.

    A framework's bundled API case-splits into two BUILDS. The newer build (uppercase
    'Scripts', shipped by Codeware 1.20.3, defining JobQueue) had no manifest owner, because
    the deployer records only the casing it wrote; the older 1.19.0 build in lowercase
    'scripts' did. Keeping the "managed" copy on that basis archived the API redscript
    actually reads and broke every dependent mod with UNRESOLVED_TYPE. Build age decides,
    ownership does not — and ownership must follow the surviving casing."""
    _write(tmp_path, "red4ext/plugins/Codeware/Scripts/Codeware.Global.reds",
           "public native class JobQueue {}")                      # newer build, unmanaged
    _write(tmp_path, "red4ext/plugins/Codeware/scripts/Codeware.Global.reds",
           "// 1.19.0 — no JobQueue")                              # older build, managed
    _touch(tmp_path / "red4ext/plugins/Codeware/scripts/Codeware.Global.reds", 1769434058)
    _touch(tmp_path / "red4ext/plugins/Codeware/Scripts/Codeware.Global.reds", 1777845006)
    D.save_manifest(tmp_path, {
        "Codeware-7780-1-19-0-1769434058": ["red4ext/plugins/Codeware/scripts/Codeware.Global.reds"],
        "Codeware-7780-1-20-3-1777845006": ["red4ext/plugins/Codeware/scripts/Codeware.Global.reds"],
    })
    c = D.find_case_collisions(tmp_path, D.load_manifest(tmp_path)["mods"])[0]
    assert c["is_framework_bundle"] and c["has_reds"]
    keep, _drops, mode, _reason = D.classify_collision(c)
    assert keep is not None and mode == "framework" and keep["name"] == "Scripts"

    # mtime is a guess, so this one is only applied on explicit opt-in
    with contextlib.redirect_stdout(io.StringIO()):
        D.action_fix_duplicates(tmp_path, dry_run=False, apply_heuristics=True)
    survivor = tmp_path / "red4ext/plugins/Codeware/Scripts/Codeware.Global.reds"
    assert "JobQueue" in survivor.read_text()                       # the API the compile needs
    assert not (tmp_path / "red4ext/plugins/Codeware/scripts").exists()
    assert list((tmp_path / D.DUP_BACKUP_DIRNAME).rglob("Codeware.Global.reds"))  # reversible
    mods = D.load_manifest(tmp_path)["mods"]                        # ownership follows the keep
    assert all(f.startswith("red4ext/plugins/Codeware/Scripts/")
               for files in mods.values() for f in files)


def test_owner_version_beats_mtime_on_a_framework_split(tmp_path):
    """Once both builds are in the manifest, the Nexus upload epoch settles the split and
    mtime is not consulted at all. Here mtime points the WRONG way — the stale 1.19.0 files
    were copied most recently, which is exactly how mtime misleads — and the correct 1.20.3
    copy still wins because its manifest entry is the newer version."""
    _write(tmp_path, "red4ext/plugins/Codeware/Scripts/Codeware.Global.reds",
           "public native class JobQueue {}")                       # v1.20.3
    _write(tmp_path, "red4ext/plugins/Codeware/scripts/Codeware.Global.reds",
           "// 1.19.0 — no JobQueue")                               # v1.19.0
    _touch(tmp_path / "red4ext/plugins/Codeware/Scripts/Codeware.Global.reds", 1700000000)
    _touch(tmp_path / "red4ext/plugins/Codeware/scripts/Codeware.Global.reds", 1790000000)
    D.save_manifest(tmp_path, {
        "Codeware-7780-1-19-0-1769434058": ["red4ext/plugins/Codeware/scripts/Codeware.Global.reds"],
        "Codeware-7780-1-20-3-1777845006": ["red4ext/plugins/Codeware/Scripts/Codeware.Global.reds"],
    })
    c = D.find_case_collisions(tmp_path, D.load_manifest(tmp_path)["mods"])[0]
    keep, _drops, mode, _reason = D.classify_collision(c)
    assert mode == "owner-version" and keep["name"] == "Scripts"
    assert mode in D.DECISIVE_MODES                  # applied without --apply-heuristics
    with contextlib.redirect_stdout(io.StringIO()):
        D.action_fix_duplicates(tmp_path, dry_run=False)
    assert "JobQueue" in (tmp_path / "red4ext/plugins/Codeware/Scripts"
                          / "Codeware.Global.reds").read_text()
    assert not (tmp_path / "red4ext/plugins/Codeware/scripts").exists()
    # the retired version stops claiming the archived path and, left owning nothing, is
    # dropped — the surviving casing keeps exactly one owner, never two
    mods = D.load_manifest(tmp_path)["mods"]
    assert set(mods) == {"Codeware-7780-1-20-3-1777845006"}
    assert mods["Codeware-7780-1-20-3-1777845006"] == [
        "red4ext/plugins/Codeware/Scripts/Codeware.Global.reds"]


def test_owner_version_declines_when_a_variant_is_unmanaged(tmp_path):
    """The live Codeware shape: the correct newer copy had no manifest entry. Ownership is
    then absence of evidence, not evidence of absence — the rule must decline rather than
    hand the win to the merely-managed copy."""
    _write(tmp_path, "red4ext/plugins/Codeware/Scripts/Codeware.Global.reds", "class JobQueue {}")
    _write(tmp_path, "red4ext/plugins/Codeware/scripts/Codeware.Global.reds", "// old")
    D.save_manifest(tmp_path, {
        "Codeware-7780-1-19-0-1769434058": ["red4ext/plugins/Codeware/scripts/Codeware.Global.reds"],
    })
    c = D.find_case_collisions(tmp_path, D.load_manifest(tmp_path)["mods"])[0]
    keep, _drops, _reason = D.decide_by_owner_version(c)
    assert keep is None


def test_owner_version_declines_when_owners_are_different_mods(tmp_path):
    _write(tmp_path, "r6/scripts/Shared/Data/a.reds", "class A {}")
    _write(tmp_path, "r6/scripts/Shared/data/a.reds", "class B {}")
    D.save_manifest(tmp_path, {
        "Mod One-100-1-0-1700000000": ["r6/scripts/Shared/Data/a.reds"],
        "Mod Two-200-1-0-1780000000": ["r6/scripts/Shared/data/a.reds"],
    })
    c = D.find_case_collisions(tmp_path, D.load_manifest(tmp_path)["mods"])[0]
    keep, _drops, _reason = D.decide_by_owner_version(c)
    assert keep is None, "two different mods are not two versions of one mod"


def test_heuristic_cases_are_not_applied_by_default(tmp_path):
    """The guard that would have prevented the JobQueue break: a case-split with no version
    order behind it is reported, not acted on, unless the caller opts in."""
    _write(tmp_path, "red4ext/plugins/Codeware/Scripts/Codeware.Global.reds", "class JobQueue {}")
    _write(tmp_path, "red4ext/plugins/Codeware/scripts/Codeware.Global.reds", "// old")
    _touch(tmp_path / "red4ext/plugins/Codeware/Scripts/Codeware.Global.reds", 1700000000)
    _touch(tmp_path / "red4ext/plugins/Codeware/scripts/Codeware.Global.reds", 1790000000)
    D.save_manifest(tmp_path, {
        "Codeware-7780-1-19-0-1769434058": ["red4ext/plugins/Codeware/scripts/Codeware.Global.reds"],
    })
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.action_fix_duplicates(tmp_path, dry_run=False)
    assert "--apply-heuristics" in buf.getvalue()
    # nothing moved: both casings still on disk, JobQueue still in the compile path
    assert (tmp_path / "red4ext/plugins/Codeware/Scripts/Codeware.Global.reds").exists()
    assert (tmp_path / "red4ext/plugins/Codeware/scripts/Codeware.Global.reds").exists()
    assert not (tmp_path / D.DUP_BACKUP_DIRNAME).exists()


def test_reds_case_variants_are_never_dropped_by_ownership(tmp_path):
    """A mod's own scripts, case-split with differing bytes and one side unmanaged. Manifest
    ownership says nothing about which copy the compiler reads, so this must never be
    auto-resolved by ownership — either it is decided on identifier coherence or it is left
    alone. What must NOT happen is a .reds folder disappearing for being 'unmanaged'."""
    _write(tmp_path, "r6/scripts/MyMod/Sub/Scripts/a.reds", "class Alpha {}")
    _write(tmp_path, "r6/scripts/MyMod/Sub/scripts/a.reds", "class Beta {}")
    D.save_manifest(tmp_path, {"MyMod": ["r6/scripts/MyMod/Sub/scripts/a.reds"]})
    c = D.find_case_collisions(tmp_path, D.load_manifest(tmp_path)["mods"])[0]
    _keep, _drops, mode, _reason = D.classify_collision(c)
    assert mode != "keep-owned"                       # ownership must not decide compile input
    with contextlib.redirect_stdout(io.StringIO()):
        D.action_fix_duplicates(tmp_path, dry_run=False)
    survivors = list((tmp_path / "r6/scripts/MyMod/Sub").rglob("a.reds"))
    assert survivors, "a redscript source folder was archived on ownership grounds"


def test_conflict_both_managed_left_for_manual(tmp_path):
    """Two managed non-script copies with differing bytes must NOT be auto-resolved (can't
    tell which version is right) — reported for manual resolution, nothing moved."""
    _write(tmp_path, "red4ext/plugins/X/Sub/Data/a.dat", "V1")
    _write(tmp_path, "red4ext/plugins/X/Sub/data/a.dat", "V2")
    D.save_manifest(tmp_path, {
        "ModA": ["red4ext/plugins/X/Sub/Data/a.dat"],
        "ModB": ["red4ext/plugins/X/Sub/data/a.dat"],
    })
    c = D.find_case_collisions(tmp_path, D.load_manifest(tmp_path)["mods"])[0]
    keep, _drops, mode, _reason = D.classify_collision(c)
    assert mode == "skip" and keep is None
    with contextlib.redirect_stdout(io.StringIO()):
        D.action_fix_duplicates(tmp_path, dry_run=False)
    assert (tmp_path / "red4ext/plugins/X/Sub/Data/a.dat").exists()
    assert (tmp_path / "red4ext/plugins/X/Sub/data/a.dat").exists()      # untouched


def test_unmanaged_leftover_keeps_managed_when_no_scripts_involved(tmp_path):
    """Ownership may still decide a conflict when no compile input is at stake."""
    _write(tmp_path, "red4ext/plugins/X/Sub/Data/a.dat", "OLD")     # orphan leftover
    _write(tmp_path, "red4ext/plugins/X/Sub/data/a.dat", "NEW")     # managed
    D.save_manifest(tmp_path, {"X-1-0": ["red4ext/plugins/X/Sub/data/a.dat"]})
    c = D.find_case_collisions(tmp_path, D.load_manifest(tmp_path)["mods"])[0]
    keep, _drops, mode, _reason = D.classify_collision(c)
    assert keep is not None and mode == "keep-owned" and keep["name"] == "data"


def test_deploy_reuses_existing_casing_no_new_duplicate(tmp_path):
    """PREVENTION: with `Core/` already on disk, deploying a mod whose tree normalizes to
    `core/` must land in the existing `Core/`, not create a second case-variant."""
    game = tmp_path / "game"
    _write(game, "r6/scripts/Mod/Core/existing.reds", "module Mod.Core\nclass A {}\n")
    extract = tmp_path / "extract"
    _write(extract, "r6/scripts/Mod/Core/added.reds", "module Mod.Core\nclass B {}\n")
    dep = D.Deployer(game, dry_run=False, verbose=False)
    with contextlib.redirect_stdout(io.StringIO()):
        assert dep.deploy(extract, "Mod v2") is True
    # both files live under the single pre-existing casing; no lowercase twin created
    assert (game / "r6/scripts/Mod/Core/existing.reds").exists()
    assert (game / "r6/scripts/Mod/Core/added.reds").exists()
    assert not (game / "r6/scripts/Mod/core").exists()
    assert D.find_case_collisions(game, {}) == []


def test_report_health_emits_parseable_summary(tmp_path):
    _two_version_mod(tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_health(tmp_path, {})
    out = buf.getvalue()
    assert n == 1
    # unowned, so no version order settles it: counted as heuristic-only, not auto-fixable
    assert ("[health] 1 case-duplicate folder(s); 0 auto-fixable; 1 heuristic-only; "
            "1 in redscript") in out
    assert "SYM_REDEFINITION" in out


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
    print(f"{n} health tests passed")
