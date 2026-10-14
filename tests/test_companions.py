"""Regression tests for companion/patch files and dangling cross-mod references.

Mods reference each other by path and by string rather than by declared dependency, so even
a CORRECT uninstall leaves references behind. Observed:

  r6/scripts/TSU_mods/NCestate.reds — a tooltip patch keyed on the string "TSU_NCestate".
      After removing NCestate it is dead code that STILL wraps WorldMapTooltipController on
      every world-map tooltip. It costs frame time, not just tidiness.
  AppearanceMenuMod/Collabs/Custom Props/boe6_gambling_system_roulette.lua — a spawnable
      prop pointing at assets from "Gambling System - Roulette". After removing that mod the
      prop remains listed in AMM and simply fails to spawn.

Both share one shape: a file in a companion root whose FILENAME names the mod it patches.
The hard requirement is the false-positive direction — calling a live patch dangling tells
the user to delete something that works — so most of what follows guards that. Pure stdlib.
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

REPO = str(Path(__file__).resolve().parent.parent)
TSU = "r6/scripts/TSU_mods"
AMM = "bin/x64/plugins/cyber_engine_tweaks/mods/AppearanceMenuMod/Collabs/Custom Props"

NCESTATE = "Night City Estate-12857-1-2-1700000000"
ROULETTE = "Gambling System - Roulette-30100-1-0-1700000000"
TSU_MOD = "Tooltip Stats UI-27000-2-0-1700000000"
AMM_MOD = "AppearanceMenuMod-790-2-6-1700000000"


def _mk(game: Path, rel: str, body: bytes = b"x" * 8) -> Path:
    p = game / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


def _install(game: Path, with_ncestate=True, with_roulette=True) -> dict:
    """The reported layout: TSU and AMM each ship a patch for another mod."""
    mods = {}
    _mk(game, f"{TSU}/NCestate.reds", b'@wrapMethod(WorldMapTooltipController)\n')
    _mk(game, "bin/x64/plugins/cyber_engine_tweaks/mods/TSU/init.lua")
    mods[TSU_MOD] = [f"{TSU}/NCestate.reds",
                     "bin/x64/plugins/cyber_engine_tweaks/mods/TSU/init.lua"]
    _mk(game, f"{AMM}/boe6_gambling_system_roulette.lua")
    _mk(game, "bin/x64/plugins/cyber_engine_tweaks/mods/AppearanceMenuMod/init.lua")
    mods[AMM_MOD] = [f"{AMM}/boe6_gambling_system_roulette.lua",
                     "bin/x64/plugins/cyber_engine_tweaks/mods/AppearanceMenuMod/init.lua"]
    if with_ncestate:
        _mk(game, "bin/x64/plugins/cyber_engine_tweaks/mods/NCestate/init.lua")
        mods[NCESTATE] = ["bin/x64/plugins/cyber_engine_tweaks/mods/NCestate/init.lua"]
    if with_roulette:
        _mk(game, "archive/pc/mod/Gambling Props.archive")
        mods[ROULETTE] = ["archive/pc/mod/Gambling Props.archive"]
    return mods


def _out(fn, *a, **kw) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


# ── nothing is dangling while both targets are installed ─────────────────────

def test_nothing_dangles_when_every_target_is_installed(tmp_path):
    """The false-positive floor. If this ever fires, the tool is telling users to delete
    working patches."""
    game = tmp_path / "game"
    mods = _install(game)
    assert D.find_dangling_companions(game, mods) == []


def test_abbreviated_target_matches_its_cet_folder(tmp_path):
    """NCestate.reds names a CET folder, not a Nexus mod name."""
    game = tmp_path / "game"
    mods = _install(game)
    ids = D.installed_identifiers(game, mods)
    assert D._identifies("NCestate", ids)


def test_author_prefixed_target_matches_the_spaced_mod_name(tmp_path):
    """"boe6_gambling_system_roulette" vs "Gambling System - Roulette": an author prefix on
    one side and separators on the other. Both compact to a containment match."""
    game = tmp_path / "game"
    mods = _install(game)
    ids = D.installed_identifiers(game, mods)
    assert D._identifies("boe6_gambling_system_roulette", ids)


def test_compact_folds_separators_and_case():
    assert D._compact("Gambling System - Roulette") == D._compact("gambling_system_roulette")
    assert D._compact("NCestate") == "ncestate"


# ── the reported cases, after the target is removed ──────────────────────────

def test_tsu_patch_dangles_once_ncestate_is_gone(tmp_path):
    game = tmp_path / "game"
    mods = _install(game, with_ncestate=False)
    found = D.find_dangling_companions(game, mods)
    assert [c["rel"] for c in found] == [f"{TSU}/NCestate.reds"]
    assert found[0]["target"] == "NCestate"


def test_amm_prop_dangles_once_roulette_is_gone(tmp_path):
    game = tmp_path / "game"
    mods = _install(game, with_roulette=False)
    found = D.find_dangling_companions(game, mods)
    assert [c["rel"] for c in found] == [f"{AMM}/boe6_gambling_system_roulette.lua"]


def test_the_dangling_file_is_owned_by_the_mod_that_stays(tmp_path):
    """The whole difficulty: NCestate.reds came out of TSU's archive. TSU is still installed,
    so the file keeps loading and nothing about the removal touches it."""
    game = tmp_path / "game"
    mods = _install(game, with_ncestate=False)
    found = D.find_dangling_companions(game, mods)
    assert found[0]["owner"] == TSU_MOD


def test_ownership_is_not_reassigned_to_the_patched_mod(tmp_path):
    """The fix direction asked for companions to be modelled as belonging to the mod they
    patch. Rewriting ownership would make --check-attribution correctly report the file as
    not tracing to its owner's archive, and a re-deploy of TSU would rewrite it anyway. The
    link is separate data; the manifest is left alone."""
    game = tmp_path / "game"
    mods = _install(game)
    before = {k: list(v) for k, v in mods.items()}
    D.find_companions(game, mods)
    D.companions_orphaned_by(game, mods, [NCESTATE])
    assert mods == before
    assert f"{TSU}/NCestate.reds" in mods[TSU_MOD]
    assert f"{TSU}/NCestate.reds" not in mods[NCESTATE]


# ── fires BEFORE the removal, which is the point ─────────────────────────────

def test_orphaned_by_predicts_the_break_before_removing(tmp_path):
    game = tmp_path / "game"
    mods = _install(game)
    assert D.find_dangling_companions(game, mods) == []      # nothing wrong yet
    doomed = D.companions_orphaned_by(game, mods, [NCESTATE])
    assert [c["rel"] for c in doomed] == [f"{TSU}/NCestate.reds"]


def test_a_companion_leaving_with_its_own_mod_is_not_reported(tmp_path):
    """If the patch is shipped BY the mod being removed, it goes too. Warning about it would
    be noise about a file that is already handled."""
    game = tmp_path / "game"
    mods = _install(game)
    mods[NCESTATE].append(f"{TSU}/NCestate.reds")
    mods[TSU_MOD].remove(f"{TSU}/NCestate.reds")
    assert D.companions_orphaned_by(game, mods, [NCESTATE]) == []


def test_already_dangling_files_are_not_blamed_on_this_removal(tmp_path):
    game = tmp_path / "game"
    mods = _install(game, with_roulette=False)
    doomed = D.companions_orphaned_by(game, mods, [NCESTATE])
    assert [c["rel"] for c in doomed] == [f"{TSU}/NCestate.reds"]


def test_uninstall_impact_carries_companions_and_they_never_block(tmp_path):
    game = tmp_path / "game"
    mods = _install(game)
    imp = D.uninstall_impact(game, mods, [NCESTATE])
    assert [c["rel"] for c in imp["companions"]] == [f"{TSU}/NCestate.reds"]
    assert imp["blockers"] == []


def test_impact_report_prints_companions_without_refusing(tmp_path):
    game = tmp_path / "game"
    mods = _install(game)
    imp = D.uninstall_impact(game, mods, [NCESTATE])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        blocked = D.report_uninstall_impact(imp)
    assert blocked is False
    assert "NCestate.reds" in buf.getvalue()
    assert "Not a blocker" in buf.getvalue()


# ── the heuristic must not over-reach ────────────────────────────────────────

def test_a_short_target_name_is_never_accused(tmp_path):
    """Below the length floor a name matches by coincidence. Under-reporting costs a stale
    patch; a false accusation costs the user a working one."""
    game = tmp_path / "game"
    mods = _install(game)
    _mk(game, f"{TSU}/HUD.reds")
    mods[TSU_MOD].append(f"{TSU}/HUD.reds")
    assert f"{TSU}/HUD.reds" not in {c["rel"] for c in D.find_dangling_companions(game, mods)}


def test_a_hand_installed_target_still_counts_as_installed(tmp_path):
    """The mod is on disk but in no manifest entry. Its patch is live, not dangling."""
    game = tmp_path / "game"
    mods = _install(game, with_ncestate=False)
    _mk(game, "bin/x64/plugins/cyber_engine_tweaks/mods/NCestate/init.lua")
    assert D.find_dangling_companions(game, mods) == []


def test_files_outside_a_companion_root_are_never_considered(tmp_path):
    """A .reds in r6/scripts is a mod, not a patch. Treating every filename as a mod
    reference is the general string-grep this deliberately does not do."""
    game = tmp_path / "game"
    mods = _install(game)
    _mk(game, "r6/scripts/SomeDeletedMod/main.reds")
    rels = {c["rel"] for c in D.find_companions(game, mods)}
    assert "r6/scripts/SomeDeletedMod/main.reds" not in rels


def test_non_source_files_in_a_companion_root_are_ignored(tmp_path):
    game = tmp_path / "game"
    mods = _install(game)
    _mk(game, f"{TSU}/readme.txt")
    _mk(game, f"{TSU}/icon.png")
    rels = {c["rel"] for c in D.find_companions(game, mods)}
    assert not any(r.endswith((".txt", ".png")) for r in rels)


def test_matching_uses_the_mods_real_nexus_name_when_known(tmp_path):
    """With --fetch-names run, the archive stem may say nothing while the real name matches."""
    game = tmp_path / "game"
    mods = _install(game, with_roulette=False)
    mods["v2_All in one Vortex Installer-30100-1-0-1700000000"] = [
        "archive/pc/mod/props.archive"]
    names = {"30100": "Gambling System - Roulette"}
    assert D.find_dangling_companions(game, mods, names) == []


def test_a_user_added_companion_root_is_honoured(tmp_path):
    """Every UI framework grows this pattern; a table only the tool ships is stale in a week."""
    game = tmp_path / "game"
    mods = _install(game)
    _mk(game, "r6/scripts/OtherUI_patches/DeletedMod.reds")
    roots = dict(D.COMPANION_ROOTS)
    roots["r6/scripts/OtherUI_patches"] = "OtherUI per-mod patch"
    found = [c for c in D.find_companions(game, mods, None, roots) if c["dangling"]]
    assert [c["rel"] for c in found] == ["r6/scripts/OtherUI_patches/DeletedMod.reds"]


# ── removal ──────────────────────────────────────────────────────────────────

def test_prune_dangling_removes_only_the_dangling_file(tmp_path):
    game = tmp_path / "game"
    mods = _install(game, with_ncestate=False)
    D.save_manifest(game, mods)
    _out(D.action_prune_dangling, game, dry_run=False)
    assert not (game / TSU / "NCestate.reds").exists()
    assert (game / AMM / "boe6_gambling_system_roulette.lua").exists()
    assert (game / "bin/x64/plugins/cyber_engine_tweaks/mods/TSU/init.lua").exists()


def test_prune_dangling_keeps_the_owners_other_files_in_the_manifest(tmp_path):
    """A partial removal of a mod that stays installed: TSU keeps its entry minus that path.
    Dropping the entry would make its surviving files unmanaged and prunable by mistake."""
    game = tmp_path / "game"
    mods = _install(game, with_ncestate=False)
    D.save_manifest(game, mods)
    _out(D.action_prune_dangling, game, dry_run=False)
    after = D.load_manifest(game)["mods"]
    assert TSU_MOD in after
    assert f"{TSU}/NCestate.reds" not in after[TSU_MOD]
    assert "bin/x64/plugins/cyber_engine_tweaks/mods/TSU/init.lua" in after[TSU_MOD]


def test_partial_removal_does_not_claim_a_whole_mod_was_removed(tmp_path):
    """TSU keeps 1 of its 2 files, so "Removed 1 mod(s)" would be a false statement about a
    mod that is still installed. The suffix (dir counts, terminating period) must survive
    the reworded branch too — a ternary binds looser than + and silently ate it once."""
    game = tmp_path / "game"
    mods = _install(game, with_ncestate=False)
    D.save_manifest(game, mods)
    text = _out(D.action_prune_dangling, game, dry_run=False)
    assert "Removed part of 1 mod(s)" in text
    assert "empty dir(s) pruned." in text


def test_prune_dangling_is_recoverable(tmp_path):
    game = tmp_path / "game"
    mods = _install(game, with_ncestate=False)
    D.save_manifest(game, mods)
    _out(D.action_prune_dangling, game, dry_run=False)
    assert list((game / ".mod_trash").rglob("NCestate.reds"))


def test_prune_dangling_dry_run_changes_nothing(tmp_path):
    game = tmp_path / "game"
    mods = _install(game, with_ncestate=False)
    D.save_manifest(game, mods)
    text = _out(D.action_prune_dangling, game, dry_run=True)
    assert "[dry-run]" in text
    assert (game / TSU / "NCestate.reds").exists()


def test_prune_dangling_says_so_when_there_is_nothing_to_do(tmp_path):
    game = tmp_path / "game"
    mods = _install(game)
    D.save_manifest(game, mods)
    assert "Nothing to do" in _out(D.action_prune_dangling, game, dry_run=False)


def test_report_explains_that_a_redeploy_brings_them_back(tmp_path):
    """The file really is in TSU's archive, so this is correct behaviour and must not read
    like the removal failed."""
    game = tmp_path / "game"
    mods = _install(game, with_ncestate=False)
    assert "re-deploy" in _out(D.report_dangling, game, mods)


def test_check_dangling_writes_nothing(tmp_path):
    game = tmp_path / "game"
    mods = _install(game, with_ncestate=False)
    D.save_manifest(game, mods)
    before = {p: p.stat().st_mtime_ns for p in game.rglob("*") if p.is_file()}
    _out(D.action_check_dangling, game)
    assert {p: p.stat().st_mtime_ns for p in game.rglob("*") if p.is_file()} == before


# ── CLI + health ─────────────────────────────────────────────────────────────

def test_cli_check_dangling(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, with_ncestate=False))
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-dangling"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[dangling] 1 companion file(s) targeting a mod that is not installed" in r.stdout
    assert "NCestate.reds" in r.stdout


def test_check_health_includes_the_dangling_line(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, with_ncestate=False))
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[dangling] 1 companion file(s)" in r.stdout


def test_cli_disable_warns_about_companions_before_removing(tmp_path):
    """The warning has to arrive while the user is still deciding."""
    game = tmp_path / "game"
    D.save_manifest(game, _install(game))
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--disable", "12857", "--dry-run"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NCestate.reds" in r.stdout
    assert "--prune-dangling" in r.stdout


# ── the GUI must stay in sync with the machine line ──────────────────────────

def test_gui_parses_the_dangling_line():
    src = (Path(REPO) / "gui" / "deploy_tab.py").read_text()
    import re as _re
    rx = _re.search(r"^RE_DANGLING = re\.compile\(\s*\n?\s*(r\"[^\"]+\")", src, _re.MULTILINE)
    assert rx, "gui/deploy_tab.py has no RE_DANGLING parser"
    pattern = _re.compile(eval(rx.group(1)))
    m = pattern.match("[dangling] 4 companion file(s) targeting a mod that is not installed")
    assert m and m.group(1) == "4"


def test_gui_ranks_dangling_above_pure_bookkeeping():
    """A stale tooltip patch still wraps every tooltip, so it costs frame time — it is not
    in the same class as an untracked file sitting on disk."""
    text = (Path(REPO) / "gui" / "deploy_tab.py").read_text()
    order = [text.index(k) for k in
             ("elif self._perf[0] >= PERF_WARN:", "elif self._pins[1]:",
              "elif self._disabled[1]:", "elif domains or meths:", "elif self._dangling:",
              "elif missing_mods:", "elif units:", "elif self._fds")]
    assert order == sorted(order), "GUI _on_done chain is out of severity order"


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
    print(f"{n} companion/dangling tests passed")
