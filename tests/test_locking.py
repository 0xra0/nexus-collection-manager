"""Regression tests for the concurrency guard in nexus_core.deployer.

Nothing stopped two processes from mutating one install at once. When a second session ran
the manager while the game was up, .mod_manifest.json, r6/cache/final.redscripts (+ .bk/.ts)
and r6/logs/redscript_rCURRENT.log were all rewritten mid-diagnosis: A/B comparisons were
made against states that no longer existed, and one launch failed in an unrelated way purely
from the interference.

Two writers, two guards: an flock against other copies of this tool, and a process check
against the game and scc.exe, which write the same files and know nothing about our lock.
Pure stdlib — no PySide6, no network, and no game install is touched.
"""
import contextlib
import hashlib
import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

REPO = str(Path(__file__).resolve().parent.parent)


def _write(root, rel, data="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8")


def _tree_fingerprint(root: Path) -> dict:
    """Path -> (size, mtime_ns, sha256) for every file, so 'nothing changed' is a claim
    about content AND timestamps rather than a glance at the directory listing."""
    out = {}
    for f in sorted(root.rglob("*")):
        if f.is_file():
            st = f.stat()
            out[str(f.relative_to(root))] = (st.st_size, st.st_mtime_ns,
                                             hashlib.sha256(f.read_bytes()).hexdigest())
    return out


def test_exclusive_lock_blocks_a_second_holder(tmp_path):
    with D.game_lock(tmp_path, "deploy") as got:
        assert got is True
        # a second process must be refused; sys.exit() inside game_lock -> SystemExit
        code = textwrap.dedent(f"""
            import sys; sys.path.insert(0, {REPO!r})
            from pathlib import Path
            from nexus_core import deployer as D
            try:
                with D.game_lock(Path({str(tmp_path)!r}), "--fix-versions"):
                    print("ACQUIRED")
            except SystemExit as e:
                print("REFUSED"); print(e)
        """)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "REFUSED" in r.stdout and "ACQUIRED" not in r.stdout
    assert "locked by pid" in r.stdout          # names the holder, not just "busy"
    assert "--fix-versions" in r.stdout         # and names what was refused


def test_lock_is_released_when_the_holder_dies(tmp_path):
    """flock is dropped by the kernel on process death, so a crashed run cannot strand the
    install behind a lock nobody owns — which is why there is no --force-unlock."""
    code = textwrap.dedent(f"""
        import sys, os; sys.path.insert(0, {REPO!r})
        from pathlib import Path
        from nexus_core import deployer as D
        cm = D.game_lock(Path({str(tmp_path)!r}), "deploy")
        cm.__enter__()
        os._exit(1)                      # die hard, holding the lock
    """)
    subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    with D.game_lock(tmp_path, "deploy") as got:
        assert got is True               # freely acquirable again


def test_shared_locks_do_not_block_each_other(tmp_path):
    """Two read-only reports may run at once; only a writer excludes."""
    with D.game_lock(tmp_path, "--check-health", exclusive=False):
        code = textwrap.dedent(f"""
            import sys; sys.path.insert(0, {REPO!r})
            from pathlib import Path
            from nexus_core import deployer as D
            with D.game_lock(Path({str(tmp_path)!r}), "--check-health", exclusive=False):
                print("ACQUIRED")
        """)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "ACQUIRED" in r.stdout


def test_reader_is_refused_while_a_writer_holds_the_lock(tmp_path):
    """A health report generated halfway through a deploy describes a tree that never
    existed — exactly the invalidated A/B comparison this guard exists to prevent."""
    with D.game_lock(tmp_path, "deploy"):
        code = textwrap.dedent(f"""
            import sys; sys.path.insert(0, {REPO!r})
            from pathlib import Path
            from nexus_core import deployer as D
            try:
                with D.game_lock(Path({str(tmp_path)!r}), "--check-health", exclusive=False):
                    print("ACQUIRED")
            except SystemExit:
                print("REFUSED")
        """)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "REFUSED" in r.stdout


def test_lock_file_records_and_then_clears_the_holder(tmp_path):
    lock = tmp_path / D.LOCK_NAME
    with D.game_lock(tmp_path, "--fix-duplicates"):
        assert str(os.getpid()) in lock.read_text()
        assert "--fix-duplicates" in lock.read_text()
    assert lock.read_text() == ""          # cleared on release, so no stale holder is shown


def test_check_health_is_provably_non_mutating(tmp_path):
    """Fingerprint every file (content, size, mtime) before and after a full health run —
    including the paths the interference actually corrupted."""
    game = tmp_path
    _write(game, "r6/scripts/DHG/Core/a.reds", "module DHG.Core\nclass A {}")
    _write(game, "r6/scripts/DHG/core/a.reds", "module DHG.Core\nclass A {}")
    _write(game, "r6/cache/final.redscripts", "cache")
    _write(game, "r6/cache/final.redscripts.bk", "backup")
    _write(game, "r6/cache/final.redscripts.ts", "stamp")
    _write(game, "r6/logs/redscript_rCURRENT.log", "log")
    D.save_manifest(game, {"DHG-100-1-0-1700000000": ["r6/scripts/DHG/Core/a.reds"]})

    before = _tree_fingerprint(game)
    with contextlib.redirect_stdout(io.StringIO()):
        D.report_health(game, D.load_manifest(game)["mods"])
    after = _tree_fingerprint(game)
    assert before == after, f"health check mutated: {set(before) ^ set(after)}"


def test_health_check_via_lock_leaves_only_the_lock_file(tmp_path):
    """The shared lock itself is the one file the read-only path may create."""
    game = tmp_path
    _write(game, "r6/scripts/M/a.reds", "class A {}")
    D.save_manifest(game, {})
    before = _tree_fingerprint(game)
    with contextlib.redirect_stdout(io.StringIO()):
        with D.game_lock(game, "--check-health", exclusive=False):
            D.report_health(game, {})
    after = _tree_fingerprint(game)
    assert set(after) - set(before) == {D.LOCK_NAME}
    assert {k: v for k, v in after.items() if k != D.LOCK_NAME} == before


def test_finds_the_running_game_despite_truncated_proc_comm(monkeypatch, tmp_path):
    """/proc/<pid>/comm is capped at 15 chars, so the game reads as 'cyberpunk2077.e'.
    Matching on equality alone never detects it."""
    monkeypatch.setattr(D.os, "listdir", lambda p: ["4242"])
    monkeypatch.setattr(D.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(D, "_proc_cmdline", lambda pid: [])
    real_open = open

    def fake_open(path, *a, **k):
        if str(path) == "/proc/4242/comm":
            return io.StringIO("Cyberpunk2077.e\n")      # truncated, as the kernel gives it
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", fake_open)
    assert D.find_running_game_processes() == [(4242, "cyberpunk2077.exe")]


def test_finds_scc_via_cmdline(monkeypatch):
    monkeypatch.setattr(D.os, "listdir", lambda p: ["77"])
    monkeypatch.setattr(D.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(D, "_proc_cmdline",
                        lambda pid: ["wine", "C:\\game\\engine\\tools\\scc.exe", "-compile"])
    real_open = open

    def fake_open(path, *a, **k):
        if str(path).startswith("/proc/"):
            raise OSError("no comm")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", fake_open)
    assert D.find_running_game_processes() == [(77, "scc.exe")]      # Windows path separators


def test_require_game_idle_refuses_and_can_be_overridden(monkeypatch):
    monkeypatch.setattr(D, "find_running_game_processes", lambda: [(9, "cyberpunk2077.exe")])
    try:
        D.require_game_idle("deploy")
    except SystemExit as e:
        assert "cyberpunk2077.exe (pid 9)" in str(e) and "Refusing" in str(e)
    else:
        raise AssertionError("deploy was allowed while the game was running")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.require_game_idle("deploy", ignore_running=True)     # explicit override
    assert "continuing anyway" in buf.getvalue()


def test_require_game_idle_passes_when_nothing_runs(monkeypatch):
    monkeypatch.setattr(D, "find_running_game_processes", lambda: [])
    D.require_game_idle("deploy")          # must not raise


if __name__ == "__main__":
    import inspect
    import tempfile

    class _MP:
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val=None):
            if isinstance(obj, str):                       # "builtins.open" form
                mod, _, attr = obj.rpartition(".")
                import importlib
                obj, name, val = importlib.import_module(mod), attr, name
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
    print(f"{n} locking tests passed")
