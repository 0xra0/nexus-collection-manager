"""Regression tests for scale-dependent performance-mod settings.

EXRC (Runtime Controller, mod 28727) compares the live LuaJIT heap against a configurable
`limit` in MB, escalating at 0.9x and 1.25x. Shipped default 128 -> thresholds at 115 and
160 MB. Measured on this install the heap's LIVE floor after a forced full GC is ~577 MB,
peaking over 1000 MB, so EXRC sat in permanent EMERGENCY from 58 s after load, firing its
most aggressive GC steps forever at a number it can never reduce. None of its four built-in
profiles fit: even OVER (512 MB, "500+ MOD") is below this install's live floor.

The postscript is the most important thing in this file. Raising the limit demonstrably
changed EXRC's behaviour (first EMERGENCY 58 s -> 197 s) and did NOT improve the frame rate.
Several tests below exist purely to keep the tool from ever implying otherwise, so that the
experiment is not re-run. Pure stdlib — no PySide6, no network.
"""
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

REPO = str(Path(__file__).resolve().parent.parent)
EXRC = "Runtime Controller-28727-1-4-1700000000"
CET = "bin/x64/plugins/cyber_engine_tweaks/mods"


def _mk(game: Path, rel: str, body: bytes = b"x" * 8) -> Path:
    p = game / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


def _install(game: Path, cet_mods: int = 322, limit=None, folder="EXRC") -> dict:
    """An install of `cet_mods` CET mods, with EXRC among them."""
    mods = {EXRC: [f"{CET}/{folder}/init.lua"]}
    _mk(game, f"{CET}/{folder}/init.lua")
    if limit is not None:
        _mk(game, f"{CET}/{folder}/config.json",
            json.dumps({"limit": limit, "autoGC": True}).encode())
        mods[EXRC].append(f"{CET}/{folder}/config.json")
    for i in range(cet_mods - 1):
        _mk(game, f"{CET}/Filler{i}/init.lua")
        mods[f"Filler {i}-{40000 + i}-1-0-1700000000"] = [f"{CET}/Filler{i}/init.lua"]
    return mods


def _out(fn, *a, **kw) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


# ── the reported case ────────────────────────────────────────────────────────

def test_shipped_default_on_this_install_is_flagged(tmp_path):
    game = tmp_path / "game"
    mods = _install(game, 322, limit=128)
    rows = D.check_scale_settings(game, mods)
    assert len(rows) == 1
    assert rows[0]["mod_id"] == "28727"
    assert rows[0]["value"] == 128
    assert rows[0]["verdict"] == "below-measured"


def test_the_reported_escalation_thresholds_are_reproduced(tmp_path):
    """0.9x and 1.25x of 128 = the 115 MB CRIT_MEM and 160 MB EMERGENCY from the report."""
    game = tmp_path / "game"
    rows = D.check_scale_settings(game, _install(game, 322, limit=128))
    assert rows[0]["thresholds"] == (115, 160)


def test_even_the_top_profile_is_reported_as_too_small(tmp_path):
    """OVER = 512 MB, labelled "500+ MOD", is still below the 577 MB live floor. This is the
    second half of the fix direction: flag when the mod's own ceiling cannot reach."""
    game = tmp_path / "game"
    rows = D.check_scale_settings(game, _install(game, 322, limit=512))
    assert rows and rows[0]["verdict"] == "below-measured"
    text = _out(D.report_scale, game, _install(game, 322, limit=512))
    assert "None of its built-in profiles fit" in text
    assert "OVER" in text


def test_the_hand_set_fix_clears_the_finding(tmp_path):
    game = tmp_path / "game"
    assert D.check_scale_settings(game, _install(game, 322, limit=800)) == []


def test_a_value_just_over_the_measured_floor_is_accepted(tmp_path):
    game = tmp_path / "game"
    assert D.check_scale_settings(game, _install(game, 322, limit=578)) == []


def test_an_unconfigured_mod_is_judged_on_its_shipped_default(tmp_path):
    """A CET mod that has never run has no config yet. That is not "fine" — it is the
    shipped 128 MB, which is the reported bug."""
    game = tmp_path / "game"
    rows = D.check_scale_settings(game, _install(game, 322, limit=None))
    assert rows and rows[0]["value"] == 128 and rows[0]["configured"] is False
    assert "shipped default (not configured)" in _out(D.report_scale, game,
                                                      _install(game, 322, limit=None))


# ── the finding must not claim frames it does not deliver ────────────────────

def test_report_always_says_this_does_not_raise_the_frame_rate(tmp_path):
    """The whole reason the postscript was written down. Every message says it, every time."""
    game = tmp_path / "game"
    text = _out(D.report_scale, game, _install(game, 322, limit=128))
    assert "does NOT raise the frame rate" in text
    assert "58 s to 197 s" in text
    assert "--perf-profile" in text


def test_gui_ranks_scale_below_the_measured_per_frame_finding():
    """Ranking a finding that does not change fps above the one that halves it would send
    the user after the wrong thing."""
    text = (Path(REPO) / "gui" / "deploy_tab.py").read_text()
    assert text.index("elif self._perf[0] >= PERF_WARN:") < text.index("elif self._scale:")


# ── no invented scaling law ──────────────────────────────────────────────────

def test_a_small_install_is_not_judged_by_the_measurement(tmp_path):
    """There is exactly ONE measurement (577 MB at 322 CET mods). Fitting a curve to one
    point is the same mistake as reading a per-mod CPU ranking out of static counts, so a
    much smaller install gets no verdict from it."""
    game = tmp_path / "game"
    rows = D.check_scale_settings(game, _install(game, 40, limit=128))
    assert [r["verdict"] for r in rows] != ["below-measured"]


def test_a_vanilla_sized_install_on_the_default_is_left_alone(tmp_path):
    game = tmp_path / "game"
    assert D.check_scale_settings(game, _install(game, 40, limit=128)) == []


def test_a_larger_install_than_measured_is_still_comparable(tmp_path):
    game = tmp_path / "game"
    rows = D.check_scale_settings(game, _install(game, 500, limit=128))
    assert rows and rows[0]["verdict"] == "below-measured"


def test_past_the_ladder_top_is_flagged_without_the_measurement(tmp_path):
    """A mod count past the top rung is the mod's OWN ladder saying it has run out of
    profiles. That needs no measurement of ours, so it applies where the measurement does
    not."""
    game = tmp_path / "game"
    spec = {k: dict(v) for k, v in D.PERF_SCALE_MODS.items()}
    spec["28727"].pop("measured_live_floor")
    rows = D.check_scale_settings(game, _install(game, 600, limit=512), None, spec)
    assert rows and rows[0]["verdict"] == "below-profile"
    assert "top rung" in rows[0]["basis"]


def test_profile_ladder_picks_the_labelled_rung():
    profiles = D.PERF_SCALE_MODS["28727"]["profiles"]
    assert D.profile_for_count(profiles, 10)[0] == "VANILLA"
    assert D.profile_for_count(profiles, 150)[0] == "STABLE"
    assert D.profile_for_count(profiles, 322)[0] == "ULTRA"
    assert D.profile_for_count(profiles, 900)[0] == "OVER"


# ── finding the config without hardcoding its filename ───────────────────────

def test_config_is_found_by_searching_not_by_a_guessed_filename(tmp_path):
    """CET mods name their config whatever they like. A guessed path would report "not
    configured" on an install where it plainly is, and then recommend a change the user
    already made."""
    game = tmp_path / "game"
    mods = {EXRC: [f"{CET}/EXRC/init.lua"]}
    _mk(game, f"{CET}/EXRC/init.lua")
    _mk(game, f"{CET}/EXRC/data/user_settings.json",
        json.dumps({"limit": 800}).encode())
    value, source = D.find_scale_config(game, D.PERF_SCALE_MODS["28727"], EXRC, mods)
    assert value == 800
    assert source == f"{CET}/EXRC/data/user_settings.json"


def test_config_is_found_under_a_renamed_folder_via_the_manifest(tmp_path):
    """The folder name is not fixed either — see the CET rename problem. The manifest knows
    where this mod's files actually went."""
    game = tmp_path / "game"
    mods = _install(game, 322, limit=800, folder="ExRC_Renamed")
    value, _src = D.find_scale_config(game, D.PERF_SCALE_MODS["28727"], EXRC, mods)
    assert value == 800


def test_a_json_without_the_key_is_not_mistaken_for_config(tmp_path):
    game = tmp_path / "game"
    mods = {EXRC: [f"{CET}/EXRC/init.lua"]}
    _mk(game, f"{CET}/EXRC/init.lua")
    _mk(game, f"{CET}/EXRC/strings.json", json.dumps({"title": "EXRC"}).encode())
    value, source = D.find_scale_config(game, D.PERF_SCALE_MODS["28727"], EXRC, mods)
    assert value is None and source is None


def test_malformed_json_does_not_crash_the_scan(tmp_path):
    game = tmp_path / "game"
    mods = {EXRC: [f"{CET}/EXRC/init.lua"]}
    _mk(game, f"{CET}/EXRC/init.lua")
    _mk(game, f"{CET}/EXRC/broken.json", b"{ not json")
    _mk(game, f"{CET}/EXRC/config.json", json.dumps({"limit": 128}).encode())
    assert D.find_scale_config(game, D.PERF_SCALE_MODS["28727"], EXRC, mods)[0] == 128


def test_a_non_numeric_value_is_ignored(tmp_path):
    game = tmp_path / "game"
    mods = {EXRC: [f"{CET}/EXRC/init.lua"]}
    _mk(game, f"{CET}/EXRC/init.lua")
    _mk(game, f"{CET}/EXRC/config.json", json.dumps({"limit": "auto"}).encode())
    assert D.find_scale_config(game, D.PERF_SCALE_MODS["28727"], EXRC, mods)[0] is None


# ── the mod not being installed ──────────────────────────────────────────────

def test_nothing_is_reported_when_the_mod_is_not_installed(tmp_path):
    game = tmp_path / "game"
    mods = {"Some Other Mod-100-1-0-1700000000": ["archive/pc/mod/x.archive"]}
    _mk(game, "archive/pc/mod/x.archive")
    assert D.check_scale_settings(game, mods) == []


def test_mod_id_installed_matches_by_identity(tmp_path):
    mods = {EXRC: [], "Other-999-1-0-1700000000": []}
    assert D.mod_id_installed("28727", mods) == EXRC
    assert D.mod_id_installed("11111", mods) == ""


# ── the table is user-extendable ─────────────────────────────────────────────

def test_a_user_supplied_setting_is_honoured(tmp_path):
    """This table describes other people's mods and goes stale on their release schedule."""
    game = tmp_path / "game"
    mods = _install(game, 322, limit=800)
    other = "Pool Mod-31500-1-0-1700000000"
    mods[other] = [f"{CET}/PoolMod/init.lua"]
    _mk(game, f"{CET}/PoolMod/init.lua")
    _mk(game, f"{CET}/PoolMod/cfg.json", json.dumps({"poolSize": 64}).encode())
    table = {"31500": {"name": "Pool Mod", "folders": ("PoolMod",), "key": "poolSize",
                       "unit": "entries", "what": "entity pool", "default": 64,
                       "profiles": [("SMALL", 64, 0), ("BIG", 128, 200)],
                       "measured_at_cet_mods": 0}}
    rows = D.check_scale_settings(game, mods, None, table)
    assert len(rows) == 1 and rows[0]["verdict"] == "below-profile"


# ── writing the fix ──────────────────────────────────────────────────────────

def test_set_scale_writes_the_value(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, 322, limit=128))
    _out(D.action_set_scale, game, "28727", 800, dry_run=False)
    cfg = json.loads((game / CET / "EXRC/config.json").read_text())
    assert cfg["limit"] == 800


def test_set_scale_preserves_the_rest_of_the_config(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, 322, limit=128))
    _out(D.action_set_scale, game, "28727", 800, dry_run=False)
    assert json.loads((game / CET / "EXRC/config.json").read_text())["autoGC"] is True


def test_set_scale_pins_the_file(tmp_path):
    """Without the pin the next deploy of EXRC puts 128 straight back — the config lives in
    the mod's own folder, so it comes out of the mod's own archive."""
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, 322, limit=128))
    _out(D.action_set_scale, game, "28727", 800, dry_run=False)
    pins = D.load_pins(game)
    assert D.is_pinned(f"{CET}/EXRC/config.json", pins)


def test_the_pin_survives_a_deploy_disposition_check(tmp_path):
    """End of the loop: the written value is what deploy_disposition refuses to overwrite."""
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, 322, limit=128))
    _out(D.action_set_scale, game, "28727", 800, dry_run=False)
    rel = f"{CET}/EXRC/config.json"
    src = tmp_path / "fresh_config.json"
    src.write_text(json.dumps({"limit": 128}))
    what = D.deploy_disposition(rel, src, game / rel, D.load_pins(game))
    assert what == "pinned"


def test_set_scale_dry_run_writes_nothing(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, 322, limit=128))
    text = _out(D.action_set_scale, game, "28727", 800, dry_run=True)
    assert "[dry-run]" in text
    assert json.loads((game / CET / "EXRC/config.json").read_text())["limit"] == 128
    assert not D.load_pins(game)["pinned"]


def test_set_scale_explains_when_there_is_no_config_yet(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, 322, limit=None))
    text = _out(D.action_set_scale, game, "28727", 800, dry_run=False)
    assert "no config file" in text
    assert "first run" in text


def test_set_scale_on_an_uninstalled_mod_says_so(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_manifest(game, {})
    assert "not installed" in _out(D.action_set_scale, game, "28727", 800, dry_run=False)


def test_set_scale_lists_what_it_knows_for_an_unknown_needle(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    D.save_manifest(game, {})
    text = _out(D.action_set_scale, game, "nosuchmod", 800, dry_run=False)
    assert "Known:" in text and "28727" in text


def test_check_scale_writes_nothing(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, 322, limit=128))
    before = {p: p.stat().st_mtime_ns for p in game.rglob("*") if p.is_file()}
    _out(D.action_check_scale, game)
    assert {p: p.stat().st_mtime_ns for p in game.rglob("*") if p.is_file()} == before


# ── CLI + health ─────────────────────────────────────────────────────────────

def test_cli_check_scale(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, 322, limit=128))
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-scale"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[scale] 1 performance setting(s) below what this install needs" in r.stdout
    assert "does NOT raise the frame rate" in r.stdout


def test_cli_set_scale_rejects_a_non_numeric_value(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, 322, limit=128))
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--set-scale", "28727", "big", "--ignore-running"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode != 0
    assert "must be a number" in (r.stdout + r.stderr)


def test_check_health_includes_the_scale_line(tmp_path):
    game = tmp_path / "game"
    D.save_manifest(game, _install(game, 322, limit=128))
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[scale] 1 performance setting(s)" in r.stdout


def test_gui_parses_the_scale_line():
    src = (Path(REPO) / "gui" / "deploy_tab.py").read_text()
    import re as _re
    rx = _re.search(r"^RE_SCALE = re\.compile\(\s*\n?\s*(r\"[^\"]+\")", src, _re.MULTILINE)
    assert rx, "gui/deploy_tab.py has no RE_SCALE parser"
    m = _re.compile(eval(rx.group(1))).match(
        "[scale] 2 performance setting(s) below what this install needs")
    assert m and m.group(1) == "2"


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
    print(f"{n} scale-setting tests passed")
