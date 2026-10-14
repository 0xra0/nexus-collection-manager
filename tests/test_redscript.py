"""Regression tests for redscript compile health in nexus_core.deployer.

A standalone scc.exe run reported 7875 errors (2693 UNRESOLVED_TYPE, 2591 UNRESOLVED_METHOD,
997 UNRESOLVED_IMPORT, 943 TYPE_ERR, …) on an install that was fine. They are not real:
standalone scc compiles without the game's registered native types, so every framework symbol
is unresolvable. That same run also rewrote r6/cache/final.redscripts and .ts, corrupting the
state under test. A raw error count read as health is what makes tooling — or an agent —
conclude the install is destroyed and start "fixing" things.

So: the app never invokes scc (asserted below), the verdict comes from the game's own log,
and counts are only ever reported as a delta against a captured baseline.
Pure stdlib — no PySide6, no network, no game install touched.
"""
import contextlib
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

GOOD_LOG = """\
INFO [redscript] Loading scripts from r6/scripts
WARN [redscript] deprecated annotation in Foo.reds
INFO [redscript] Compilation complete
"""

BROKEN_LOG = """\
INFO [redscript] Loading scripts from r6/scripts
ERROR [redscript] [SYM_REDEFINITION] the name of this type conflicts with DHG.Core
ERROR [redscript] [SYM_REDEFINITION] the name of this type conflicts with DHG.Core
ERROR [redscript] [UNRESOLVED_TYPE] JobQueue
"""


def _log(root: Path, text: str):
    p = root / D.REDSCRIPT_LOG_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _artifact(root: Path):
    p = root / D.MODDED_CACHE_RELS[0]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("compiled", encoding="utf-8")
    return p


def _isolate_baseline(monkeypatch, tmp_path):
    """Keep baselines out of the real ~/.config during tests."""
    monkeypatch.setattr(D, "CONFIG_DIR", tmp_path / "cfg")


def test_the_app_never_invokes_scc():
    """The guardrail itself: a standalone compile is not a health check, and adding one back
    would reintroduce both the false errors and the live-cache corruption.

    Checks the invariant that matters — no process is spawned other than bsdtar — by walking
    the AST rather than grepping, since 'scc.exe' appears legitimately in comments and in the
    running-process names to watch for."""
    import ast
    tree = ast.parse(Path(D.__file__).read_text(encoding="utf-8"))
    spawned = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (f"{getattr(fn.value, 'id', '')}.{fn.attr}"
                if isinstance(fn, ast.Attribute) else getattr(fn, "id", ""))
        if name in ("subprocess.run", "subprocess.Popen", "subprocess.call",
                    "subprocess.check_output", "os.system", "os.execv"):
            arg = node.args[0] if node.args else None
            first = None
            if isinstance(arg, ast.List) and arg.elts and isinstance(arg.elts[0], ast.Constant):
                first = arg.elts[0].value
            elif isinstance(arg, ast.Constant):
                first = arg.value
            spawned.append(first)
    assert spawned, "expected to find the bsdtar calls — did the scan break?"
    assert all(s == "bsdtar" for s in spawned), f"unexpected process spawned: {spawned}"


def test_parses_completion_and_error_codes(tmp_path):
    p = _log(tmp_path, BROKEN_LOG)
    parsed = D.parse_redscript_log(p)
    assert parsed["complete"] is False
    assert parsed["by_code"] == {"SYM_REDEFINITION": 2, "UNRESOLVED_TYPE": 1}
    assert parsed["total"] == 3

    p = _log(tmp_path, GOOD_LOG)
    parsed = D.parse_redscript_log(p)
    assert parsed["complete"] is True and parsed["total"] == 0   # WARN is not an error


def test_ok_requires_both_completion_and_the_artifact(tmp_path):
    _log(tmp_path, GOOD_LOG)
    # completed but no modded cache written -> not healthy
    assert D.redscript_health(tmp_path)["state"] == "failed"
    _artifact(tmp_path)
    assert D.redscript_health(tmp_path)["state"] == "ok"


def test_no_log_is_not_reported_as_failure(tmp_path):
    h = D.redscript_health(tmp_path)
    assert h["state"] == "no-log"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert D.report_redscript_health(tmp_path) == 0     # absence of evidence, not failure
    assert "[redscript] no-log" in buf.getvalue()


def test_log_older_than_the_last_deploy_is_stale_not_a_verdict(tmp_path):
    """A log written before the last deploy describes a tree that no longer exists. Reporting
    it as health is the same class of false signal as a standalone scc run."""
    p = _log(tmp_path, BROKEN_LOG)
    _artifact(tmp_path)
    import os
    os.utime(p, (1700000000, 1700000000))
    manifest = {"updated": datetime.fromtimestamp(1780000000, timezone.utc)
                .isoformat(timespec="seconds")}
    h = D.redscript_health(tmp_path, manifest)
    assert h["state"] == "stale"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert D.report_redscript_health(tmp_path, manifest) == 0
    assert "[redscript] stale" in buf.getvalue()
    assert "FAILED" not in buf.getvalue()          # never a verdict off a stale log


def test_raw_count_is_never_presented_as_a_verdict(tmp_path, monkeypatch):
    """Healthy installs log non-fatal errors. With no baseline the count must be shown with
    an explicit disclaimer, never as a score."""
    _isolate_baseline(monkeypatch, tmp_path)
    _log(tmp_path, "INFO Compilation complete\nERROR [TYPE_ERR] x\nERROR [TYPE_ERR] y\n")
    _artifact(tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = D.report_redscript_health(tmp_path)
    out = buf.getvalue()
    assert rc == 0                                  # 2 errors, still healthy
    assert "compile completed" in out.lower()
    assert "NOT a health score" in out
    assert "delta=n/a" in out


def test_delta_against_baseline_is_the_reported_signal(tmp_path, monkeypatch):
    _isolate_baseline(monkeypatch, tmp_path)
    _log(tmp_path, "INFO Compilation complete\nERROR [TYPE_ERR] a\nERROR [TYPE_ERR] b\n")
    _artifact(tmp_path)
    with contextlib.redirect_stdout(io.StringIO()):
        D.action_redscript_baseline(tmp_path)
    assert D.load_redscript_baseline(tmp_path)["total"] == 2

    # same profile later -> no regression
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.report_redscript_health(tmp_path)
    assert "delta=0" in buf.getvalue() and "No regression" in buf.getvalue()

    # a new error appears -> that is the signal worth acting on
    _log(tmp_path, "INFO Compilation complete\nERROR [TYPE_ERR] a\nERROR [TYPE_ERR] b\n"
                   "ERROR [UNRESOLVED_TYPE] JobQueue\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.report_redscript_health(tmp_path)
    assert "delta=1" in buf.getvalue() and "New errors since the baseline" in buf.getvalue()


def test_baseline_refuses_to_record_a_broken_compile(tmp_path, monkeypatch):
    _isolate_baseline(monkeypatch, tmp_path)
    _log(tmp_path, BROKEN_LOG)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            D.action_redscript_baseline(tmp_path)
    except SystemExit as e:
        assert "did not complete" in str(e)
    else:
        raise AssertionError("baselined a broken compile as known-good")


def test_baseline_is_stored_outside_the_game_folder(tmp_path, monkeypatch):
    """The health path must stay non-mutating with respect to the install."""
    game = tmp_path / "game"; game.mkdir()
    _isolate_baseline(monkeypatch, tmp_path)
    _log(game, GOOD_LOG)
    _artifact(game)
    before = {str(p.relative_to(game)) for p in game.rglob("*") if p.is_file()}
    with contextlib.redirect_stdout(io.StringIO()):
        D.action_redscript_baseline(game)
    after = {str(p.relative_to(game)) for p in game.rglob("*") if p.is_file()}
    assert before == after
    assert (tmp_path / "cfg" / D.BASELINE_NAME).exists()


def test_baselines_are_keyed_per_install(tmp_path, monkeypatch):
    _isolate_baseline(monkeypatch, tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    for g, n in ((a, 1), (b, 3)):
        g.mkdir()
        _log(g, "INFO Compilation complete\n" + "ERROR [TYPE_ERR] x\n" * n)
        _artifact(g)
        with contextlib.redirect_stdout(io.StringIO()):
            D.action_redscript_baseline(g)
    assert D.load_redscript_baseline(a)["total"] == 1
    assert D.load_redscript_baseline(b)["total"] == 3
    store = json.loads((tmp_path / "cfg" / D.BASELINE_NAME).read_text())
    assert set(store) == {str(a), str(b)}


def test_broken_compile_reports_top_codes_and_nonzero(tmp_path, monkeypatch):
    _isolate_baseline(monkeypatch, tmp_path)
    _log(tmp_path, BROKEN_LOG)
    _artifact(tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = D.report_redscript_health(tmp_path)
    out = buf.getvalue()
    assert rc == 1
    assert "FAILED" in out and "2x SYM_REDEFINITION" in out
    assert "[redscript] failed" in out


def test_health_check_stays_non_mutating_with_redscript_reporting(tmp_path, monkeypatch):
    """The guarantee from the concurrency fix must survive this addition."""
    import hashlib
    _isolate_baseline(monkeypatch, tmp_path)
    game = tmp_path / "game"; game.mkdir()
    _log(game, GOOD_LOG)
    _artifact(game)
    (game / "r6/cache/final.redscripts").write_text("live cache")
    D.save_manifest(game, {})

    def fp():
        return {str(f.relative_to(game)): (f.stat().st_size, f.stat().st_mtime_ns,
                                           hashlib.sha256(f.read_bytes()).hexdigest())
                for f in sorted(game.rglob("*")) if f.is_file()}
    before = fp()
    time.sleep(0.01)
    with contextlib.redirect_stdout(io.StringIO()):
        D.report_health(game, {}, D.load_manifest(game))
    assert fp() == before


if __name__ == "__main__":
    import inspect
    import tempfile

    class _MP:
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def undo(self):
            for obj, name, val in reversed(self._undo): setattr(obj, name, val)

    n = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        params = inspect.signature(fn).parameters
        kw = {}
        if "tmp_path" in params: kw["tmp_path"] = Path(tempfile.mkdtemp())
        mp = _MP()
        if "monkeypatch" in params: kw["monkeypatch"] = mp
        try: fn(**kw)
        finally: mp.undo()
        print("PASS", name); n += 1
    print(f"{n} redscript tests passed")
