"""Subprocess orchestration: run a nexus_core backend and stream its stdout.

The GUI never imports the backends into its own process — it launches each as
``python -m nexus_core.<module>`` in a new session so the backends' many
``sys.exit()`` paths and the deployer's stdout tee stay isolated from Qt. A
ProcRunner (a QThread) reads the child's merged stdout/stderr line by line and
re-emits each line as a Qt signal.
"""
import os
import signal
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal, QTimer

from .parsing import clean

# Directory that contains the nexus_core/ package, added to the child's PYTHONPATH
# so `python -m nexus_core.x` resolves no matter what the working directory is.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

_MODULE = {
    "downloader": "nexus_core.downloader",   # was test.py
    "deployer":   "nexus_core.deployer",     # was extract2.py
    "modctl":     "nexus_core.modctl",        # was modctl.py
    "scanner":    "nexus_core.mod_scanner",   # read-only TF-IDF/DBSCAN integrity scanner
    "nexus_account": "nexus_core.nexus_account", # live endorse/track account actions
    "correlation": "nexus_core.auto_correlation", # multi-tier Nexus identity inference/review
    "installer":  "nexus_core.mod_installation", # preview/conflict/FOMOD interactive installer
    "smart_deployment": "nexus_core.smart_deployment", # Linux staging/hardlink/profile engine
    "conflicts": "nexus_core.conflict_detection", # staged multi-layer conflict inbox/actions
    "load_order": "nexus_core.load_order", # archive order rules/modlist.txt generation
    "archives": "nexus_core.archive_management", # staged package archive browser/link/cleanup
    "download_manager": "nexus_core.download_manager", # persistent Premium/NXM download queue
    "trending": "nexus_core.trending_mods", # Nexus discovery/account feeds + safe install
    "profiles": "nexus_core.profile_manager", # portable smart-deployment profile management
    "updates": "nexus_core.update_checker", # staged Nexus file-lineage update checker
    "onboarding": "nexus_core.onboarding", # Linux game discovery/validation for guided setup
}

# Preview runs the downloader's own fetcher in a child process, isolating its
# top-level curl_cffi import from the GUI. Kept tiny: it imports the real module,
# loads cookies, calls fetch_collection(), and dumps the result as JSON.
PREVIEW_CODE = (
    "import sys, json; from pathlib import Path; "
    "from nexus_core import downloader as d; "
    "d._COOKIES = d.load_cookies(Path(sys.argv[1])); "
    "files, manual, total = d.fetch_collection(sys.argv[2], True); "
    "Path(sys.argv[3]).write_text("
    "json.dumps({'files': files, 'manual': manual, 'total': total}))"
)


def backend(name: str) -> list:
    """argv prefix that runs one of the nexus_core backends as a module. Everything
    appended after it is the backend's own argv (its main()/argparse handles it)."""
    return [sys.executable, "-m", _MODULE[name]]


def preview_argv(cookies: str, slug: str, out_path: str) -> list:
    return [sys.executable, "-c", PREVIEW_CODE, cookies, slug, out_path]


def fmt_argv(argv: list) -> str:
    """Readable one-liner for the log. Collapses the inline `-c <preview>` blob and
    shortens the interpreter path to just `python`."""
    out = []
    i = 0
    while i < len(argv):
        a = str(argv[i])
        if a == "-c":
            out.append("-c <preview>")
            i += 2
            continue
        if a in {"--nexus-api-key", "--api-key"} and i + 1 < len(argv):
            out.extend([a, "********"])
            i += 2
            continue
        if i == 0 and a == sys.executable:
            a = "python"
        out.append(a)
        i += 1
    return " ".join(out)


class ProcRunner(QThread):
    line        = Signal(str)     # one cleaned stdout line
    finished_rc = Signal(int)     # process return code (or -1 if never started)

    def __init__(self, argv, cwd=None, extra_env=None):
        super().__init__()
        self.argv = [str(a) for a in argv]
        self.cwd = str(cwd) if cwd else None
        self.extra_env = {str(k): str(v) for k, v in (extra_env or {}).items()}
        self._proc = None
        self._aborting = False

    @property
    def was_aborted(self) -> bool:
        """True when stop() was requested by the GUI for this run."""
        return self._aborting

    def run(self):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"      # force the child to flush per-line
        env.update(self.extra_env)
        # Make `-m nexus_core.x` importable regardless of cwd.
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (str(PROJECT_ROOT) + os.pathsep + existing
                             if existing else str(PROJECT_ROOT))
        try:
            self._proc = subprocess.Popen(
                self.argv, cwd=self.cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True)  # own group -> killable
        except OSError as e:
            self.line.emit(f"[launcher] failed to start: {e}")
            self.finished_rc.emit(-1)
            return
        try:
            stream = self._proc.stdout
            for raw in stream if stream is not None else []:
                if raw:
                    self.line.emit(clean(raw))
        except Exception as e:                       # noqa: BLE001
            self.line.emit(f"[launcher] read error: {e}")
        rc = self._proc.wait()
        self.finished_rc.emit(rc)

    def _signal_group(self, sig):
        p = self._proc
        if p and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    def stop(self):
        """Graceful stop: SIGINT lets the downloader checkpoint and aria2c leave
        resumable partials; the deployer unwinds its scratch dir. Escalate if it hangs."""
        self._aborting = True
        self._signal_group(signal.SIGINT)
        QTimer.singleShot(5000, lambda: self._signal_group(signal.SIGTERM))
        QTimer.singleShot(9000, lambda: self._signal_group(signal.SIGKILL))
