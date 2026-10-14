"""Regression tests for the per-frame CET script budget in nexus_core.deployer.

The largest finding of the 2026-07-19 session and the only one backed by a controlled A/B.
Of 322 installed CET mods, 143 register an `onUpdate` handler — Lua on EVERY frame, all on
the game's single main thread. Measured, gameplay frames only:

    322 mods  n=639  p50 32 fps  GPU 18%     frames under 30 fps: 31.8%
    207 mods  n=127  p50 68 fps  GPU 33%     frames under 30 fps:  0.0%

Frame rate doubled; the GPU had ~3x headroom it could not use because the main thread was
the wall. More cores do not help — CET Lua is single-threaded.

THE RULE MOST OF THESE TESTS DEFEND: static counts are an aggregate SCALE signal, never a
per-mod CPU ranking. A handler that early-outs is nearly free and is indistinguishable to a
parser from one that walks the entity list. Nothing may sort or present mods as "expensive".
Pure stdlib — no PySide6, no network.
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

CET = "bin/x64/plugins/cyber_engine_tweaks/mods"


def _lua(game: Path, mod: str, text: str, name: str = "init.lua"):
    p = game / CET / mod / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


def _per_frame(game: Path, mod: str, hooks: int = 0):
    body = 'registerForEvent("onUpdate", function(dt) end)\n'
    body += "".join(f'Observe("C{i}", "m{i}", function() end)\n' for i in range(hooks))
    _lua(game, mod, body)


# ── counting ─────────────────────────────────────────────────────────────────

def test_onupdate_registrations_are_counted(tmp_path):
    game = tmp_path / "game"
    for i in range(5):
        _per_frame(game, f"Frame{i}")
    _lua(game, "Quiet", 'registerForEvent("onInit", function() end)\n')
    scan = D.scan_cet_cost(game)
    assert scan["onupdate"] == 5
    assert len(scan["mods"]) == 6


def test_observe_and_override_hooks_are_counted(tmp_path):
    game = tmp_path / "game"
    _lua(game, "Hooky",
         'Observe("A", "b", function() end)\n'
         'ObserveAfter("A", "c", function() end)\n'
         'ObserveBefore("A", "d", function() end)\n'
         'Override("A", "e", function() end)\n')
    assert D.scan_cet_cost(game)["hooks"] == 4


def test_hooks_are_a_separate_axis_from_per_frame_cost(tmp_path):
    """They fire on the GAME's call frequency, not once per frame, so they must not be
    folded into the per-frame count."""
    game = tmp_path / "game"
    _lua(game, "HooksOnly", 'Observe("A", "b", function() end)\n' * 50)
    scan = D.scan_cet_cost(game)
    assert scan["hooks"] == 50 and scan["onupdate"] == 0


def test_commented_out_registrations_do_not_count(tmp_path):
    game = tmp_path / "game"
    _lua(game, "M",
         '-- registerForEvent("onUpdate", function() end)\n'
         '--[[ Observe("A", "b", function() end) ]]\n'
         'registerForEvent("onInit", function() end)\n')
    scan = D.scan_cet_cost(game)
    assert scan["onupdate"] == 0 and scan["hooks"] == 0


def test_ondraw_is_tracked_separately_from_onupdate(tmp_path):
    game = tmp_path / "game"
    _lua(game, "Drawer", 'registerForEvent("onDraw", function() end)\n')
    scan = D.scan_cet_cost(game)
    assert scan["ondraw"] == 1 and scan["onupdate"] == 0


def test_registrations_across_several_files_of_one_mod_are_one_mod(tmp_path):
    game = tmp_path / "game"
    _lua(game, "Big", 'registerForEvent("onUpdate", function() end)\n')
    _lua(game, "Big", 'Observe("A", "b", function() end)\n', name="modules/extra.lua")
    scan = D.scan_cet_cost(game)
    assert scan["onupdate"] == 1 and scan["hooks"] == 1 and len(scan["mods"]) == 1


# ── the rule: counts are scale, never a ranking ──────────────────────────────

def test_the_report_never_ranks_mods_by_count(tmp_path):
    """The fix direction called this out by name. A cheap early-out handler and one that
    walks the entity list are identical to a parser, so any 'worst offenders' list would be
    fabricated authority."""
    game = tmp_path / "game"
    _per_frame(game, "Aardvark", hooks=1)
    _per_frame(game, "Zebra", hooks=200)          # by far the most hooks
    mods = {"M-1-1-0-1700000000": [f"{CET}/Aardvark/init.lua", f"{CET}/Zebra/init.lua"]}
    _r, out = _quiet(D.action_perf_profile, game, dry_run=False)
    lines = [ln for ln in out.splitlines() if ln.strip().startswith("- ")]
    assert [ln.strip() for ln in lines][0].startswith("- Aardvark"), \
        "candidates must be alphabetical, not ordered by hook count"
    assert "200" not in out                        # per-mod counts are not even shown


def test_the_warning_states_that_counts_are_not_costs(tmp_path):
    game = tmp_path / "game"
    for i in range(D.PERF_ONUPDATE_WARN + 1):
        _per_frame(game, f"Frame{i}")
    _n, out = _quiet(D.report_perf, game, {})
    assert "counts, not costs" in out
    assert "early-outs" in out
    assert "scale signal only" in out


def test_the_measured_anchors_are_the_real_numbers():
    """Both thresholds that claim to be measured must match the A/B, so the report never
    quotes a figure that was invented."""
    assert D.PERF_ONUPDATE_MEASURED_OK == 28
    assert D.PERF_ONUPDATE_MEASURED_BAD == 143
    assert D.PERF_ONUPDATE_MEASURED_OK < D.PERF_ONUPDATE_WARN < D.PERF_ONUPDATE_MEASURED_BAD


def test_the_report_quotes_the_measurement(tmp_path):
    """A bare number means nothing; the user needs to know 143 was measured at 32 fps."""
    game = tmp_path / "game"
    for i in range(150):
        _per_frame(game, f"Frame{i}")
    _n, out = _quiet(D.report_perf, game, {})
    assert "32 fps" in out and "68 fps" in out and "31.8%" in out
    assert "18% -> 33%" in out
    assert "single-threaded" in out


# ── thresholds ───────────────────────────────────────────────────────────────

def test_a_small_collection_does_not_warn(tmp_path):
    game = tmp_path / "game"
    for i in range(D.PERF_ONUPDATE_MEASURED_OK):
        _per_frame(game, f"Frame{i}")
    n, out = _quiet(D.report_perf, game, {})
    assert n == 0
    assert out.strip() == f"[perf] {D.PERF_ONUPDATE_MEASURED_OK} per-frame mod(s) of " \
                          f"{D.PERF_ONUPDATE_MEASURED_OK}; 0 hook(s)"


def test_the_measured_bad_configuration_warns_loudly(tmp_path):
    game = tmp_path / "game"
    for i in range(D.PERF_ONUPDATE_MEASURED_BAD):
        _per_frame(game, f"Frame{i}")
    n, out = _quiet(D.report_perf, game, {})
    assert n == 1 and "far past" in out


def test_an_empty_install_is_silent(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    n, out = _quiet(D.report_perf, game, {})
    assert n == 0 and out.strip() == "[perf] 0 per-frame mod(s) of 0; 0 hook(s)"


# ── the profile: selection is a safety test, not a cost estimate ─────────────

def test_a_depended_on_mod_is_never_suggested(tmp_path):
    """Exactly what made the measured A/B safe: the 28 mods other mods reach via GetMod()
    were kept, so nothing broke."""
    game = tmp_path / "game"
    _per_frame(game, "Library")
    _per_frame(game, "Leaf")
    _lua(game, "Consumer", 'local lib = GetMod("Library")\n')
    prof = D.perf_profile_candidates(game, {})
    assert [r["folder"] for r in prof["safe"]] == ["Leaf"]
    assert [r["folder"] for r in prof["needed"]] == ["Library"]


def test_a_self_lookup_does_not_protect_a_mod(tmp_path):
    """Otherwise any mod calling GetMod on itself becomes permanently unsuggestable."""
    game = tmp_path / "game"
    _lua(game, "Selfish",
         'registerForEvent("onUpdate", function() end)\nlocal me = GetMod("Selfish")\n')
    prof = D.perf_profile_candidates(game, {})
    assert [r["folder"] for r in prof["safe"]] == ["Selfish"]


def test_mods_without_a_per_frame_handler_are_not_candidates(tmp_path):
    """The profile is about per-frame cost. A hook-only mod does not run every frame."""
    game = tmp_path / "game"
    _lua(game, "HooksOnly", 'Observe("A", "b", function() end)\n')
    _per_frame(game, "Framey")
    prof = D.perf_profile_candidates(game, {})
    assert [r["folder"] for r in prof["safe"]] == ["Framey"]


def test_candidates_are_mapped_back_to_the_manifest_entry_that_owns_them(tmp_path):
    """A CET folder name is not a manifest key, so a suggestion is unactionable without it."""
    game = tmp_path / "game"
    _per_frame(game, "SomeMod")
    mods = {"Some Mod-500-1-0-1700000000": [f"{CET}/SomeMod/init.lua"]}
    prof = D.perf_profile_candidates(game, mods)
    assert prof["safe"][0]["owner"] == "Some Mod-500-1-0-1700000000"


def test_a_hand_installed_candidate_is_flagged_as_unmanageable(tmp_path):
    game = tmp_path / "game"
    _per_frame(game, "HandMod")
    _r, out = _quiet(D.action_perf_profile, game, dry_run=False)
    assert "hand-installed" in out
    assert "must be moved out by hand" in out


# ── applying ─────────────────────────────────────────────────────────────────

def test_the_profile_changes_nothing_without_apply(tmp_path):
    game = tmp_path / "game"
    _per_frame(game, "Leaf")
    (game / CET / "Leaf" / "init.lua").exists()
    D.save_manifest(game, {"Leaf Mod-500-1-0-1700000000": [f"{CET}/Leaf/init.lua"]})
    _r, out = _quiet(D.action_perf_profile, game, dry_run=False, apply=False)
    assert "Nothing has been changed" in out
    assert D.load_disabled(game) == {}
    assert (game / CET / "Leaf" / "init.lua").exists()


def test_apply_disables_through_the_normal_reversible_path(tmp_path):
    game = tmp_path / "game"
    _per_frame(game, "Leaf")
    _per_frame(game, "Library")
    _lua(game, "Consumer", 'local l = GetMod("Library")\n')
    D.save_manifest(game, {
        "Leaf Mod-500-1-0-1700000000": [f"{CET}/Leaf/init.lua"],
        "Library Mod-600-1-0-1700000000": [f"{CET}/Library/init.lua"]})
    _quiet(D.action_perf_profile, game, dry_run=False, apply=True)
    disabled = D.load_disabled(game)
    assert D.is_disabled("Leaf Mod-500-1-0-1700000000", disabled)
    assert not D.is_disabled("Library Mod-600-1-0-1700000000", disabled)
    assert not (game / CET / "Leaf" / "init.lua").exists()      # pruned
    assert (game / CET / "Library" / "init.lua").exists()       # kept, it is depended on
    # and it is undoable exactly like any other disable
    _quiet(D.action_enable, game, "Leaf Mod", dry_run=False)
    assert D.load_disabled(game) == {}


def test_apply_records_why_it_disabled_them(tmp_path):
    game = tmp_path / "game"
    _per_frame(game, "Leaf")
    D.save_manifest(game, {"Leaf Mod-500-1-0-1700000000": [f"{CET}/Leaf/init.lua"]})
    _quiet(D.action_perf_profile, game, dry_run=False, apply=True)
    entry = next(iter(D.load_disabled(game).values()))
    assert "performance" in entry.get("reason", "")


def test_apply_dry_run_changes_nothing(tmp_path):
    game = tmp_path / "game"
    _per_frame(game, "Leaf")
    D.save_manifest(game, {"Leaf Mod-500-1-0-1700000000": [f"{CET}/Leaf/init.lua"]})
    _r, out = _quiet(D.action_perf_profile, game, dry_run=True, apply=True)
    assert "[DRY RUN]" in out
    assert D.load_disabled(game) == {}
    assert (game / CET / "Leaf" / "init.lua").exists()


# ── read-only guarantee, reporting, CLI ──────────────────────────────────────

def test_reporting_never_mutates_the_install(tmp_path):
    import hashlib
    game = tmp_path / "game"
    for i in range(3):
        _per_frame(game, f"Frame{i}", hooks=2)

    def fp():
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(game.rglob("*")) if p.is_file()}
    before = fp()
    _quiet(D.report_perf, game, {})
    _quiet(D.action_perf_profile, game, dry_run=False, apply=False)
    assert fp() == before


def test_gui_regex_is_the_one_shipped_in_deploy_tab():
    src = (Path(__file__).resolve().parent.parent / "gui" / "deploy_tab.py").read_text()
    assert "RE_PERF" in src and "self._perf" in src


def test_perf_profile_cli(tmp_path):
    game = tmp_path / "game"
    _per_frame(game, "Leaf")
    _per_frame(game, "Library")
    _lua(game, "Consumer", 'local l = GetMod("Library")\n')
    D.save_manifest(game, {"Leaf Mod-500-1-0-1700000000": [f"{CET}/Leaf/init.lua"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--perf-profile"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Leaf" in r.stdout and "Nothing has been changed" in r.stdout
    assert (game / CET / "Leaf" / "init.lua").exists()


def test_check_health_includes_the_perf_line(tmp_path):
    game = tmp_path / "game"
    _per_frame(game, "One")
    D.save_manifest(game, {"M-1-1-0-1700000000": [f"{CET}/One/init.lua"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[perf] 1 per-frame mod(s) of 1" in r.stdout


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
    print(f"{n} per-frame budget tests passed")
