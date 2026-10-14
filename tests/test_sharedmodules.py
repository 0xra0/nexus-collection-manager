"""Regression tests for redscript modules declared by more than one MOD.

A module may legally span files, so several mods can contribute to one module. Observed on
this install — 6 of them:

    BetterLeveling   <- Better Leveling (Revamp of Custom Level Cap), Better Leveling Addon
    Codeware.UI      <- CustomHackingSystem, StealthRunner
    EquipmentEx      <- EquipmentEx, NPC Outfit Manager, WEAVE
    MisterChedda     <- fishbones_projectile, jinx_grenade, relic_shower_sickness, ...
    NightCitySkies   <- NCS_SKYWATCH, NIGHT_CITY_SKIES
    StashSearch      <- backpackSearch.reds, stashSearch.reds

Being legal is what makes it dangerous. The module-level uninstall check only refuses when
EVERY provider of a module is going; with several contributors the module survives, the
`import` still resolves, and the compile fails anyway on the types that left. The check has
to be at SYMBOL granularity, and that is what these tests pin. The false-positive direction
matters as much: a wrongly refused uninstall is a tool that cannot be used. Pure stdlib.
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

REPO = str(Path(__file__).resolve().parent.parent)
STEALTH = "StealthRunner-7616-1-9-1700000000"
HACKING = "CustomHackingSystem-12345-1-2-1700000000"
OTHER = "Some Consumer-500-1-0-1700000000"


def _mk(game: Path, rel: str, text: str) -> Path:
    p = game / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _codeware_ui(game: Path) -> dict:
    """The reported Codeware.UI case: two mods, one module."""
    _mk(game, "r6/scripts/StealthRunner/ui.reds",
        "module Codeware.UI\n\npublic class StealthRunnerPanel {\n  let x: Int32;\n}\n")
    _mk(game, "r6/scripts/CustomHackingSystem/ui.reds",
        "module Codeware.UI\n\npublic class HackingPanel {\n  let y: Int32;\n}\n")
    return {STEALTH: ["r6/scripts/StealthRunner/ui.reds"],
            HACKING: ["r6/scripts/CustomHackingSystem/ui.reds"]}


def _out(fn, *a, **kw) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


# ── detection ────────────────────────────────────────────────────────────────

def test_two_mods_declaring_one_module_are_found(tmp_path):
    game = tmp_path / "game"
    rows = D.find_shared_modules(game, _codeware_ui(game))
    assert len(rows) == 1
    assert rows[0]["module"] == "Codeware.UI"
    assert rows[0]["owners"] == sorted([STEALTH, HACKING])


def test_a_module_from_one_mod_is_not_reported(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/scripts/Solo/a.reds", "module Solo\npublic class A {}\n")
    _mk(game, "r6/scripts/Solo/b.reds", "module Solo\npublic class B {}\n")
    mods = {"Solo-1-1-0-1700000000": ["r6/scripts/Solo/a.reds", "r6/scripts/Solo/b.reds"]}
    assert D.find_shared_modules(game, mods) == []


def test_more_than_two_contributors_are_all_listed(tmp_path):
    """EquipmentEx <- EquipmentEx, NPC Outfit Manager, WEAVE."""
    game = tmp_path / "game"
    mods = {}
    for i, who in enumerate(("EquipmentEx", "NPCOutfitManager", "WEAVE")):
        rel = f"r6/scripts/{who}/x.reds"
        _mk(game, rel, f"module EquipmentEx\npublic class {who}Part {{}}\n")
        mods[f"{who}-{600 + i}-1-0-1700000000"] = [rel]
    rows = D.find_shared_modules(game, mods)
    assert len(rows) == 1 and len(rows[0]["owners"]) == 3


def test_symbol_counts_per_contributor_are_reported(tmp_path):
    game = tmp_path / "game"
    rows = D.find_shared_modules(game, _codeware_ui(game))
    assert rows[0]["symbols"] == {STEALTH: 1, HACKING: 1}


def test_unmanaged_reds_cannot_create_a_shared_module(tmp_path):
    """An unattributable file must not invent a contributor — the same rule the module graph
    uses, for the same reason."""
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    mods.pop(STEALTH)
    assert D.find_shared_modules(game, mods) == []


def test_a_module_named_only_in_a_comment_is_not_a_declaration(tmp_path):
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/Third/x.reds", "// module Codeware.UI\nmodule Third\n")
    mods["Third-700-1-0-1700000000"] = ["r6/scripts/Third/x.reds"]
    rows = D.find_shared_modules(game, mods)
    assert rows[0]["owners"] == sorted([STEALTH, HACKING])


# ── the gap this closes: module survives, types do not ───────────────────────

def test_module_level_check_stays_silent_here(tmp_path):
    """The premise. Codeware.UI still has a provider after StealthRunner goes, so the module
    graph correctly reports no orphaned import — and misses the break entirely."""
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/Consumer/use.reds",
        "module Consumer\nimport Codeware.UI.*\n\npublic class C {\n"
        "  let p: ref<StealthRunnerPanel>;\n}\n")
    mods[OTHER] = ["r6/scripts/Consumer/use.reds"]
    imp = D.uninstall_impact(game, mods, [STEALTH])
    assert imp["blockers"] == []


def test_but_the_symbol_check_refuses_it(tmp_path):
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/Consumer/use.reds",
        "module Consumer\nimport Codeware.UI.*\n\npublic class C {\n"
        "  let p: ref<StealthRunnerPanel>;\n}\n")
    mods[OTHER] = ["r6/scripts/Consumer/use.reds"]
    imp = D.uninstall_impact(game, mods, [STEALTH])
    assert [s["symbol"] for s in imp["symbols"]] == ["StealthRunnerPanel"]
    assert imp["symbols"][0]["providers"] == [STEALTH]


def test_the_reference_is_reported_with_file_and_line(tmp_path):
    """A source-level check has to show its evidence — the user must be able to verify the
    refusal in seconds."""
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/Consumer/use.reds",
        "module Consumer\nimport Codeware.UI.*\n\npublic class C {\n"
        "  let p: ref<StealthRunnerPanel>;\n}\n")
    mods[OTHER] = ["r6/scripts/Consumer/use.reds"]
    ref = D.uninstall_impact(game, mods, [STEALTH])["symbols"][0]["refs"][0]
    assert ref["rel"] == "r6/scripts/Consumer/use.reds"
    assert ref["line"] == 5
    assert ref["owner"] == OTHER


def test_report_refuses_and_explains_the_module_survives(tmp_path):
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/Consumer/use.reds",
        "module Consumer\nimport Codeware.UI.*\npublic class C { let p: ref<StealthRunnerPanel>; }\n")
    mods[OTHER] = ["r6/scripts/Consumer/use.reds"]
    imp = D.uninstall_impact(game, mods, [STEALTH])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        blocked = D.report_uninstall_impact(imp)
    assert blocked is True
    text = buf.getvalue()
    assert "REFUSED" in text
    assert "the module itself survives" in text
    assert "--force-prune" in text


def test_an_unmanaged_file_referencing_the_type_still_blocks(tmp_path):
    """Unmanaged .reds count on both sides here, unlike the module graph: this check only
    asks whether the compile breaks, and an unmanaged file is compiled like any other."""
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/HandInstalled.reds",
        "module Hand\npublic class H { let p: ref<StealthRunnerPanel>; }\n")
    imp = D.uninstall_impact(game, mods, [STEALTH])
    assert [s["symbol"] for s in imp["symbols"]] == ["StealthRunnerPanel"]
    assert imp["symbols"][0]["refs"][0]["owner"] == ""


def test_a_type_redeclared_by_a_survivor_is_not_at_risk(tmp_path):
    """If something staying declares the same type, removing this one does not delete it."""
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/CustomHackingSystem/compat.reds",
        "module Codeware.UI\npublic class StealthRunnerPanel {}\n")
    mods[HACKING].append("r6/scripts/CustomHackingSystem/compat.reds")
    _mk(game, "r6/scripts/Consumer/use.reds",
        "module Consumer\npublic class C { let p: ref<StealthRunnerPanel>; }\n")
    mods[OTHER] = ["r6/scripts/Consumer/use.reds"]
    assert D.symbols_at_risk(game, mods, [STEALTH]) == []


# ── the false-positive direction ─────────────────────────────────────────────

def test_a_type_nobody_references_does_not_block(tmp_path):
    """Removing a mod whose types are used only by itself is the normal case and must stay
    frictionless."""
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    assert D.symbols_at_risk(game, mods, [STEALTH]) == []


def test_a_reference_only_in_a_comment_does_not_block(tmp_path):
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/Consumer/use.reds",
        "module Consumer\n// TODO: wire up StealthRunnerPanel one day\npublic class C {}\n")
    mods[OTHER] = ["r6/scripts/Consumer/use.reds"]
    assert D.symbols_at_risk(game, mods, [STEALTH]) == []


def test_a_reference_in_a_block_comment_does_not_block(tmp_path):
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/Consumer/use.reds",
        "module Consumer\n/* was: ref<StealthRunnerPanel>\n   removed */\npublic class C {}\n")
    mods[OTHER] = ["r6/scripts/Consumer/use.reds"]
    assert D.symbols_at_risk(game, mods, [STEALTH]) == []


def test_the_departing_mods_own_references_do_not_block(tmp_path):
    """StealthRunner's other files use its own types. They leave together."""
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/StealthRunner/more.reds",
        "module Codeware.UI\npublic class Two { let p: ref<StealthRunnerPanel>; }\n")
    mods[STEALTH].append("r6/scripts/StealthRunner/more.reds")
    assert D.symbols_at_risk(game, mods, [STEALTH]) == []


def test_very_short_type_names_are_ignored(tmp_path):
    """A two- or three-letter identifier collides with ordinary tokens; below the floor it is
    not evidence of anything."""
    game = tmp_path / "game"
    _mk(game, "r6/scripts/A/a.reds", "module Shared\npublic class UI {}\n")
    _mk(game, "r6/scripts/B/b.reds", "module Shared\npublic class BPart {}\n")
    _mk(game, "r6/scripts/C/c.reds", "module Other\npublic class C { let x: UI; }\n")
    mods = {"A-1-1-0-1700000000": ["r6/scripts/A/a.reds"],
            "B-2-1-0-1700000000": ["r6/scripts/B/b.reds"],
            "C-3-1-0-1700000000": ["r6/scripts/C/c.reds"]}
    assert D.symbols_at_risk(game, mods, ["A-1-1-0-1700000000"]) == []


def test_a_lua_only_removal_is_not_assessed(tmp_path):
    """--disable-lua keeps r6/scripts, so it cannot delete a type. The scope asymmetry from
    the two-graph check has to hold here too."""
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/Consumer/use.reds",
        "module Consumer\npublic class C { let p: ref<StealthRunnerPanel>; }\n")
    mods[OTHER] = ["r6/scripts/Consumer/use.reds"]
    imp = D.uninstall_impact(game, mods, [STEALTH], {STEALTH: "lua"})
    assert imp["symbols"] == []


# ── the refusal reaches the code that deletes ────────────────────────────────

def test_sync_prune_refuses_on_an_orphaned_symbol(tmp_path):
    """The check has to live where deletion happens, not only in the reporting path."""
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/Consumer/use.reds",
        "module Consumer\npublic class C { let p: ref<StealthRunnerPanel>; }\n")
    mods[OTHER] = ["r6/scripts/Consumer/use.reds"]
    disabled = {D.disable_key(STEALTH): {"stem": STEALTH, "when": "now", "scope": "all"}}
    plan = D.sync_prune(game, mods, disabled, dry_run=False, purge=False, force=False)
    assert plan.get("refused") is True
    assert (game / "r6/scripts/StealthRunner/ui.reds").exists()


def test_force_prune_overrides_the_symbol_refusal(tmp_path):
    game = tmp_path / "game"
    mods = _codeware_ui(game)
    _mk(game, "r6/scripts/Consumer/use.reds",
        "module Consumer\npublic class C { let p: ref<StealthRunnerPanel>; }\n")
    mods[OTHER] = ["r6/scripts/Consumer/use.reds"]
    D.save_manifest(game, mods)
    disabled = {D.disable_key(STEALTH): {"stem": STEALTH, "when": "now", "scope": "all"}}
    plan = D.sync_prune(game, mods, disabled, dry_run=False, purge=False, force=True)
    assert not plan.get("refused")
    assert not (game / "r6/scripts/StealthRunner/ui.reds").exists()


# ── CLI + health ─────────────────────────────────────────────────────────────

def test_cli_check_shared_modules(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _codeware_ui(game))
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-shared-modules"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[shared-modules] 1 module(s) declared by more than one mod" in r.stdout
    assert "module Codeware.UI" in r.stdout


def test_check_health_includes_the_shared_modules_line(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _codeware_ui(game))
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[shared-modules] 1 module(s)" in r.stdout


def test_report_says_there_is_nothing_to_fix(tmp_path):
    """It is how these mods are built. Presenting it as a defect would send the user to
    uninstall something that works."""
    game = tmp_path / "game"
    text = _out(D.report_shared_modules, game, _codeware_ui(game))
    assert "Nothing to fix" in text


def test_check_writes_nothing(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _codeware_ui(game))
    before = {p: p.stat().st_mtime_ns for p in game.rglob("*") if p.is_file()}
    _out(D.action_check_shared_modules, game)
    assert {p: p.stat().st_mtime_ns for p in game.rglob("*") if p.is_file()} == before


def test_gui_parses_the_shared_modules_line():
    src = (Path(REPO) / "gui" / "deploy_tab.py").read_text()
    import re as _re
    rx = _re.search(r"^RE_SHARED_MODULES = re\.compile\(\s*\n?\s*(r\"[^\"]+\")",
                    src, _re.MULTILINE)
    assert rx, "gui/deploy_tab.py has no RE_SHARED_MODULES parser"
    m = _re.compile(eval(rx.group(1))).match(
        "[shared-modules] 6 module(s) declared by more than one mod")
    assert m and m.group(1) == "6"


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
    print(f"{n} shared-module tests passed")
