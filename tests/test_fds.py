"""Regression tests for file-descriptor headroom estimation in nexus_core.deployer.

Measured on the real install: a working session peaked at ~3411 open fds against a hard
`ulimit -n` of 4096 (83%), with 1135 mod archives and 320 CET mods present. A census at 2147
handles showed ~1197 .archive, 260 CET db.sqlite3 and 260 CET .log.

The naive model (archives + CET x2) yields 1775 for that install — barely half the measured
3411 — so a check built on it would report 43% where the truth was 83%. Most of these tests
pin the calibration against the real numbers, because a capacity check that says "plenty of
room" right up to the wall is worse than no check at all. Pure stdlib.
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

CET = "bin/x64/plugins/cyber_engine_tweaks/mods"

# The one real measurement everything is calibrated against.
REAL_ARCHIVES, REAL_CET, REAL_PEAK, REAL_HARD = 1135, 320, 3411, 4096


def _install(game: Path, archives: int = 0, cet: int = 0, redmod_archives: int = 0):
    for i in range(archives):
        p = game / "archive/pc/mod" / f"m{i}.archive"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    for i in range(redmod_archives):
        p = game / "mods" / f"RedMod{i}" / "archive" / f"r{i}.archive"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    for i in range(cet):
        p = game / CET / f"CetMod{i}" / "init.lua"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("return {}")
    return game


# ── the calibration ──────────────────────────────────────────────────────────

def test_the_model_reproduces_the_measured_peak(tmp_path):
    """The whole point. Feed it the real install's shape and it must land on the real
    measured peak, not on the naive 1775."""
    game = _install(tmp_path / "game", archives=REAL_ARCHIVES, cet=REAL_CET)
    e = D.estimate_fd_usage(game, limit=REAL_HARD)
    assert e["archives"] == REAL_ARCHIVES and e["cet"] == REAL_CET
    assert e["estimate"] == REAL_PEAK
    assert round(e["fraction"] * 100) == 83


def test_the_naive_model_would_have_understated_it_by_half():
    """Guards the reason the overhead constant exists. If someone removes it believing
    archives+CET*2 is enough, this documents what that would have reported."""
    naive = REAL_ARCHIVES * D.FD_PER_ARCHIVE + REAL_CET * D.FD_PER_CET_MOD
    assert naive == 1775
    assert round(naive / REAL_HARD * 100) == 43        # what the naive check would claim
    assert round(REAL_PEAK / REAL_HARD * 100) == 83    # what was actually measured


def test_the_marginal_costs_match_the_census():
    """The census at 2147 handles found 260 db.sqlite3 and 260 .log for CET mods — one pair
    per mod — and ~1197 .archive. These marginals are the well-grounded part of the model."""
    assert D.FD_PER_ARCHIVE == 1
    assert D.FD_PER_CET_MOD == 2


def test_the_overhead_constant_is_the_measured_residual():
    assert D.FD_BASELINE_OVERHEAD == REAL_PEAK - (REAL_ARCHIVES * D.FD_PER_ARCHIVE
                                                  + REAL_CET * D.FD_PER_CET_MOD)


def test_2048_is_not_in_the_model():
    """Three runs once peaked at exactly 2146 and that looked like wine's msvcrt 2048 CRT
    stdio cap plus baseline. It was a coincidence — .archive files go through Win32
    CreateFile and never count against that cap, and a later run reached 3411."""
    for v in (D.FD_PER_ARCHIVE, D.FD_PER_CET_MOD, D.FD_BASELINE_OVERHEAD):
        assert v != 2048
    game = _install(Path(__import__("tempfile").mkdtemp()) / "game", archives=10, cet=5)
    assert D.estimate_fd_usage(game, limit=4096)["limit"] == 4096


# ── counting ─────────────────────────────────────────────────────────────────

def test_redmod_archives_are_counted_too(tmp_path):
    """A REDmod's archive is opened at startup exactly like a loose one."""
    game = _install(tmp_path / "game", archives=5, redmod_archives=3)
    assert D.estimate_fd_usage(game, limit=4096)["archives"] == 8


def test_archives_are_counted_from_disk_not_the_manifest(tmp_path):
    """A hand-installed archive consumes a descriptor just like a managed one."""
    game = _install(tmp_path / "game", archives=7)
    assert D.estimate_fd_usage(game, limit=4096)["archives"] == 7   # no manifest at all


def test_backup_dirs_do_not_inflate_the_count(tmp_path):
    """Copies parked by --fix-duplicates are not loaded by the game, so they cost nothing."""
    game = _install(tmp_path / "game", archives=4)
    p = game / "archive/pc/mod" / D.DUP_BACKUP_DIRNAME / "old" / "x.archive"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    assert D.estimate_fd_usage(game, limit=4096)["archives"] == 4


def test_non_archive_files_are_not_counted(tmp_path):
    game = _install(tmp_path / "game", archives=2)
    for name in ("readme.txt", "mod.xl", "thing.archive.bak"):
        (game / "archive/pc/mod" / name).write_bytes(b"x")
    assert D.estimate_fd_usage(game, limit=4096)["archives"] == 2


def test_an_empty_install_is_just_the_overhead(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    e = D.estimate_fd_usage(game, limit=4096)
    assert e["archives"] == 0 and e["cet"] == 0
    assert e["estimate"] == D.FD_BASELINE_OVERHEAD


# ── headroom arithmetic ──────────────────────────────────────────────────────

def test_headroom_is_reported_in_mods_not_descriptors(tmp_path):
    """'685 descriptors left' means nothing to a user; 'about 685 more archive mods' does."""
    game = _install(tmp_path / "game", archives=REAL_ARCHIVES, cet=REAL_CET)
    e = D.estimate_fd_usage(game, limit=REAL_HARD)
    free = REAL_HARD - REAL_PEAK
    assert e["room_archives"] == free               # 1 fd each
    assert e["room_cet"] == free // 2               # 2 fds each


def test_headroom_never_goes_negative(tmp_path):
    game = _install(tmp_path / "game", archives=6000, cet=100)
    e = D.estimate_fd_usage(game, limit=4096)
    assert e["room_archives"] == 0 and e["room_cet"] == 0
    assert e["fraction"] > 1.0                      # the fraction still tells the truth


# ── thresholds and reporting ─────────────────────────────────────────────────

def _report(game, limit):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_fd_headroom(game, limit)
    return n, buf.getvalue()


def test_a_roomy_install_warns_about_nothing(tmp_path):
    game = _install(tmp_path / "game", archives=100, cet=10)
    n, out = _report(game, 65536)
    assert n == 0
    assert out.strip().startswith("[fds] ~")
    assert "tight" not in out and "LIMIT" not in out


def test_the_real_install_trips_the_warning(tmp_path):
    game = _install(tmp_path / "game", archives=REAL_ARCHIVES, cet=REAL_CET)
    n, out = _report(game, REAL_HARD)
    assert n == 1
    assert "getting tight" in out
    assert "83%" in out


def test_past_ninety_percent_is_escalated(tmp_path):
    game = _install(tmp_path / "game", archives=2200, cet=320)
    n, out = _report(game, REAL_HARD)
    assert n == 1
    assert "AT THE LIMIT" in out


def test_the_warning_states_the_calibration_is_weak(tmp_path):
    """The absolute number rests on one observation; the report must not pretend otherwise."""
    game = _install(tmp_path / "game", archives=REAL_ARCHIVES, cet=REAL_CET)
    _n, out = _report(game, REAL_HARD)
    assert "single measured session" in out
    assert "2048" in out and "not what binds" in out.replace("NOT", "not")


def test_an_unknown_limit_does_not_invent_one(tmp_path):
    game = _install(tmp_path / "game", archives=10, cet=2)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_fd_headroom(game, 0) if not any(D.fd_limits()) else 0
    if not any(D.fd_limits()):
        assert n == 0 and "limit unknown" in buf.getvalue()


def test_fd_limits_returns_a_pair():
    soft, hard = D.fd_limits()
    assert isinstance(soft, int) and isinstance(hard, int)


def test_report_emits_a_parseable_line_the_gui_matches(tmp_path):
    game = _install(tmp_path / "game", archives=REAL_ARCHIVES, cet=REAL_CET)
    _n, out = _report(game, REAL_HARD)
    assert f"[fds] ~{REAL_PEAK} of {REAL_HARD} (83%); {REAL_ARCHIVES} archive(s); "\
           f"{REAL_CET} CET mod(s)" in out
    import re
    pat = re.compile(r"^\[fds\]\s+~(\d+)\s+of\s+(\d+)\s+\((\d+)%\)")
    line = next(ln for ln in out.splitlines() if ln.startswith("[fds]"))
    m = pat.match(line)
    assert m is not None and m.groups() == (str(REAL_PEAK), str(REAL_HARD), "83")


def test_gui_regex_is_the_one_shipped_in_deploy_tab():
    src = (Path(__file__).resolve().parent.parent / "gui" / "deploy_tab.py").read_text()
    assert "RE_FDS" in src and "self._fds" in src


# ── read-only guarantee and CLI ──────────────────────────────────────────────

def _fingerprint(root: Path) -> dict:
    import hashlib
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns,
                           hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def test_the_check_never_mutates_the_install(tmp_path):
    game = _install(tmp_path / "game", archives=20, cet=5)
    before = _fingerprint(game)
    with contextlib.redirect_stdout(io.StringIO()):
        D.report_fd_headroom(game, 4096)
    assert _fingerprint(game) == before


def test_check_fds_cli(tmp_path):
    game = _install(tmp_path / "game", archives=REAL_ARCHIVES, cet=REAL_CET)
    D.save_manifest(game, {})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-fds", str(REAL_HARD)], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"[fds] ~{REAL_PEAK} of {REAL_HARD} (83%)" in r.stdout
    assert "getting tight" in r.stdout


def test_check_health_includes_the_fds_line(tmp_path):
    game = _install(tmp_path / "game", archives=3, cet=1)
    D.save_manifest(game, {"M-1-1-0-1700000000": ["archive/pc/mod/m0.archive"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[fds] ~" in r.stdout


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
    print(f"{n} fd-headroom tests passed")
