"""Regression tests for uninstall dependency safety in nexus_core.deployer.

An uninstall is unsafe unless it checks BOTH dependency graphs. They are independent and
their blast radii differ by orders of magnitude:

  (a) Lua       A calls GetMod("B"). Remove B and the call returns nil forever; A's
                integration silently stops. Blast radius: one mod.  -> WARN.
  (b) redscript A does `import B.Something`. Remove B's r6/scripts and A fails to COMPILE,
                which fails the whole script blob — EVERY redscript mod stops loading.
                Blast radius: the entire install.  -> BLOCK.

Found live 2026-07-19: StealthRunner (7616) is imported by InformativeHealthbar
(`import StealthRunner.*`), StreetSense (`import StealthRunner.StealthRunnerConfig`) and
LHUD. Removing it on request would have broken the compile for the whole install.

StealthRunner is also the case for partial uninstall: 75 .reds / 279 wrap-replace versus 22
Lua / 116 Observe-Override. Only the reds half is load-bearing for others, so dropping the
Lua half alone is safe and is where the per-frame cost lives. Pure stdlib.
"""
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

CET = "bin/x64/plugins/cyber_engine_tweaks/mods"
SR = "StealthRunner-7616-2-1-1700000000"
IHB = "InformativeHealthbar-3000-1-0-1700000000"


def _w(game: Path, rel: str, text: str = "x"):
    p = game / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel


def _quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


def _stealthrunner(game: Path):
    """The reported shape: a mod with both halves, imported by three others."""
    mods = {SR: [_w(game, "r6/scripts/StealthRunner/core.reds",
                    "module StealthRunner\n\npublic class StealthRunnerConfig {}\n"),
                 _w(game, f"{CET}/StealthRunner/init.lua",
                    'registerForEvent("onUpdate", function() end)\n')],
            IHB: [_w(game, "r6/scripts/InformativeHealthbar/hb.reds",
                     "module InformativeHealthbar\nimport StealthRunner.*\n")],
            "StreetSense-3001-1-0-1700000000": [
                _w(game, "r6/scripts/StreetSense/ss.reds",
                   "module StreetSense\nimport StealthRunner.StealthRunnerConfig\n")],
            "LHUD-3002-1-0-1700000000": [
                _w(game, "r6/scripts/LHUD/lhud.reds",
                   "module LHUD\nimport StealthRunner.StealthrunnerObjectiveTextUpdatedEvt\n")]}
    D.save_manifest(game, dict(mods))
    return mods


# ── the redscript graph: a blocker ───────────────────────────────────────────

def test_the_reported_stealthrunner_removal_is_refused(tmp_path):
    game = tmp_path / "game"
    mods = _stealthrunner(game)
    impact = D.uninstall_impact(game, mods, [SR])
    assert len(impact["blockers"]) == 1
    b = impact["blockers"][0]
    assert b["module"] == "StealthRunner"
    assert set(b["importers"]) == {IHB, "StreetSense-3001-1-0-1700000000",
                                   "LHUD-3002-1-0-1700000000"}


def test_both_import_forms_are_detected(tmp_path):
    """`import X.*` (whole module) and `import X.Symbol` (one symbol from it) both create the
    dependency, and prefix matching covers them without knowing which is which."""
    assert D._import_satisfied_by("StealthRunner", "StealthRunner")
    assert D._import_satisfied_by("StealthRunner.StealthRunnerConfig", "StealthRunner")
    assert not D._import_satisfied_by("StealthRunnerExtra", "StealthRunner")
    mods_, imps = D.parse_reds_modules(
        "module A\nimport StealthRunner.*\nimport Other.Thing\n")
    assert mods_ == {"A"} and imps == {"StealthRunner", "Other.Thing"}


def test_disable_is_refused_and_records_nothing(tmp_path):
    """A refused disable must not leave a disabled entry behind for the next deploy to act
    on — the check runs before intent is recorded."""
    game = tmp_path / "game"
    _stealthrunner(game)
    _r, out = _quiet(D.action_disable, game, "7616", dry_run=False)
    assert "REFUSED" in out and "Nothing was changed" in out
    assert D.load_disabled(game) == {}
    assert (game / "r6/scripts/StealthRunner/core.reds").exists()


def test_the_refusal_explains_the_whole_install_blast_radius(tmp_path):
    game = tmp_path / "game"
    _stealthrunner(game)
    _r, out = _quiet(D.action_disable, game, "7616", dry_run=False)
    assert "EVERY redscript mod" in out
    assert "--disable-lua" in out            # offers the partial route


def test_removing_the_importers_too_is_allowed(tmp_path):
    """Nothing is orphaned if the mods that import it go as well."""
    game = tmp_path / "game"
    mods = _stealthrunner(game)
    impact = D.uninstall_impact(game, mods, list(mods))
    assert impact["blockers"] == []


def test_a_module_still_provided_by_another_mod_is_not_a_blocker(tmp_path):
    game = tmp_path / "game"
    mods = {"A-1-1-0-1700000000": [_w(game, "r6/scripts/A/a.reds", "module Shared\n")],
            "B-2-1-0-1700000000": [_w(game, "r6/scripts/B/b.reds", "module Shared\n")],
            "C-3-1-0-1700000000": [_w(game, "r6/scripts/C/c.reds",
                                      "module C\nimport Shared.*\n")]}
    assert D.uninstall_impact(game, mods, ["A-1-1-0-1700000000"])["blockers"] == []


def test_an_unimported_module_is_free_to_remove(tmp_path):
    game = tmp_path / "game"
    mods = {"A-1-1-0-1700000000": [_w(game, "r6/scripts/A/a.reds", "module Lonely\n")]}
    assert D.uninstall_impact(game, mods, ["A-1-1-0-1700000000"])["blockers"] == []


def test_commented_out_imports_do_not_create_a_dependency(tmp_path):
    game = tmp_path / "game"
    mods = {"A-1-1-0-1700000000": [_w(game, "r6/scripts/A/a.reds", "module Prov\n")],
            "B-2-1-0-1700000000": [_w(game, "r6/scripts/B/b.reds",
                                      "module B\n// import Prov.*\n/* import Prov.X */\n")]}
    assert D.uninstall_impact(game, mods, ["A-1-1-0-1700000000"])["blockers"] == []


def test_module_identity_comes_from_the_declaration_not_the_folder(tmp_path):
    """The same rule that makes a case-duplicate folder a SYM_REDEFINITION."""
    game = tmp_path / "game"
    mods = {"A-1-1-0-1700000000": [_w(game, "r6/scripts/SomeFolder/a.reds",
                                      "module ActualName\n")],
            "B-2-1-0-1700000000": [_w(game, "r6/scripts/B/b.reds",
                                      "module B\nimport ActualName.*\n")]}
    impact = D.uninstall_impact(game, mods, ["A-1-1-0-1700000000"])
    assert impact["blockers"][0]["module"] == "ActualName"


def test_framework_bundled_scripts_are_part_of_the_graph(tmp_path):
    """The compiler reads red4ext/plugins/<Name>/Scripts too, so a dependency can live there."""
    game = tmp_path / "game"
    mods = {"Fw-7780-1-0-1700000000": [_w(game, "red4ext/plugins/Fw/Scripts/api.reds",
                                          "module FwApi\n")],
            "User-2-1-0-1700000000": [_w(game, "r6/scripts/User/u.reds",
                                         "module User\nimport FwApi.*\n")]}
    assert len(D.uninstall_impact(game, mods, ["Fw-7780-1-0-1700000000"])["blockers"]) == 1


# ── the Lua graph: a warning, never a blocker ────────────────────────────────

def test_a_lua_consumer_is_only_a_warning(tmp_path):
    """Blast radius is one mod, so it must not block: the user may well accept it."""
    game = tmp_path / "game"
    mods = {"Lib-1-1-0-1700000000": [_w(game, f"{CET}/Library/init.lua", "return {}\n")],
            "Use-2-1-0-1700000000": [_w(game, f"{CET}/Consumer/init.lua",
                                        'local l = GetMod("Library")\n')]}
    D.save_manifest(game, dict(mods))
    impact = D.uninstall_impact(game, mods, ["Lib-1-1-0-1700000000"])
    assert impact["blockers"] == []
    assert [w["folder"] for w in impact["warnings"]] == ["Library"]
    assert impact["warnings"][0]["consumers"] == ["Consumer"]


def test_a_lua_warning_does_not_stop_the_removal(tmp_path):
    game = tmp_path / "game"
    _w(game, f"{CET}/Library/init.lua", "return {}\n")
    _w(game, f"{CET}/Consumer/init.lua", 'local l = GetMod("Library")\n')
    mods = {"Lib-1-1-0-1700000000": [f"{CET}/Library/init.lua"],
            "Use-2-1-0-1700000000": [f"{CET}/Consumer/init.lua"]}
    D.save_manifest(game, dict(mods))
    _quiet(D.action_disable, game, "Lib-1", dry_run=False)
    assert not (game / CET / "Library/init.lua").exists()
    assert D.is_disabled("Lib-1-1-0-1700000000", D.load_disabled(game))


def test_a_consumer_that_is_also_being_removed_is_not_warned_about(tmp_path):
    game = tmp_path / "game"
    _w(game, f"{CET}/Library/init.lua", "return {}\n")
    _w(game, f"{CET}/Consumer/init.lua", 'local l = GetMod("Library")\n')
    mods = {"Lib-1-1-0-1700000000": [f"{CET}/Library/init.lua"],
            "Use-2-1-0-1700000000": [f"{CET}/Consumer/init.lua"]}
    assert D.uninstall_impact(game, mods, list(mods))["warnings"] == []


# ── partial uninstall: the asymmetry that makes it worth having ──────────────

def test_disable_lua_keeps_the_reds_and_is_not_refused(tmp_path):
    """StealthRunner's Lua half is where the per-frame cost is; its reds half is what three
    other mods import. Dropping only the Lua half is both safe and useful."""
    game = tmp_path / "game"
    _stealthrunner(game)
    _r, out = _quiet(D.action_disable, game, "7616", dry_run=False, scope="lua")
    assert "REFUSED" not in out
    assert not (game / CET / "StealthRunner/init.lua").exists()   # Lua half gone
    assert (game / "r6/scripts/StealthRunner/core.reds").exists()  # reds half kept


def test_a_partially_disabled_mod_keeps_its_manifest_entry(tmp_path):
    """Dropping the entry would make the surviving r6/scripts unmanaged — reported as an
    orphan, and prunable by mistake."""
    game = tmp_path / "game"
    _stealthrunner(game)
    _quiet(D.action_disable, game, "7616", dry_run=False, scope="lua")
    mods = D.load_manifest(game)["mods"]
    assert mods[SR] == ["r6/scripts/StealthRunner/core.reds"]


def test_a_lua_scoped_mod_still_deploys(tmp_path):
    """Scope 'lua' is not a full disable: the archive must still be processed, or the reds
    half would never be installed in the first place."""
    disabled = {D.disable_key(SR): {"stem": SR, "scope": "lua"}}
    assert D.disabled_scope(SR, disabled) == "lua"
    assert not D.is_disabled(SR, disabled)
    full = {D.disable_key(SR): {"stem": SR, "scope": "all"}}
    assert D.is_disabled(SR, full)


def test_an_entry_with_no_scope_recorded_is_treated_as_a_full_disable(tmp_path):
    """Backwards compatibility with entries written before scopes existed."""
    assert D.disabled_scope(SR, {D.disable_key(SR): {"stem": SR}}) == "all"


def test_desired_paths_keeps_the_reds_of_a_lua_disabled_mod(tmp_path):
    mods = {SR: ["r6/scripts/StealthRunner/core.reds", f"{CET}/StealthRunner/init.lua"]}
    disabled = {D.disable_key(SR): {"stem": SR, "scope": "lua"}}
    keep = D.desired_paths(mods, disabled)
    assert "r6/scripts/stealthrunner/core.reds" in keep
    assert f"{CET}/stealthrunner/init.lua".lower() not in keep


# ── the check cannot be routed around ────────────────────────────────────────

def test_sync_prune_itself_refuses_not_just_the_disable_action(tmp_path):
    """Checked at the one place that actually deletes, so no caller can bypass it — e.g. a
    disabled entry recorded before this check existed."""
    game = tmp_path / "game"
    mods = _stealthrunner(game)
    disabled = {D.disable_key(SR): {"stem": SR, "scope": "all"}}
    plan, _ = _quiet(D.sync_prune, game, dict(mods), disabled, False)
    assert plan["refused"] is True and plan["refused_why"] == "imports"
    assert (game / "r6/scripts/StealthRunner/core.reds").exists()


def test_force_prune_overrides_the_import_blocker(tmp_path):
    game = tmp_path / "game"
    mods = _stealthrunner(game)
    disabled = {D.disable_key(SR): {"stem": SR, "scope": "all"}}
    plan, _ = _quiet(D.sync_prune, game, dict(mods), disabled, False, force=True)
    assert plan["refused"] is False
    assert not (game / "r6/scripts/StealthRunner/core.reds").exists()


def test_the_two_refusal_reasons_are_distinguishable(tmp_path):
    game = tmp_path / "game"
    mods = _stealthrunner(game)
    disabled = {D.disable_key(SR): {"stem": SR, "scope": "all"}}
    plan, _ = _quiet(D.sync_prune, game, dict(mods), disabled, False)
    _q, msg = _quiet(D.report_prune_plan, plan, False)
    assert "redscript module(s) are imported" in msg
    assert "more than half the install" not in msg


def test_the_graph_ignores_unmanaged_scripts(tmp_path):
    """A hand-installed .reds belongs to no manifest entry, so it cannot be attributed and
    must not create a phantom dependency on some mod."""
    game = tmp_path / "game"
    _w(game, "r6/scripts/Hand/loose.reds", "module Hand\nimport Whatever.*\n")
    mods = {"A-1-1-0-1700000000": [_w(game, "r6/scripts/A/a.reds", "module Whatever\n")]}
    assert D.uninstall_impact(game, mods, ["A-1-1-0-1700000000"])["blockers"] == []


def test_perf_profile_apply_cannot_orphan_an_import(tmp_path):
    """The performance profile disables in bulk; it routes through sync_prune, so the same
    refusal protects it."""
    game = tmp_path / "game"
    mods = _stealthrunner(game)
    D.save_manifest(game, dict(mods))
    _quiet(D.action_perf_profile, game, dry_run=False, apply=True)
    assert (game / "r6/scripts/StealthRunner/core.reds").exists()


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
    print(f"{n} dependency-graph tests passed")
