"""Tab 1 — download every file in a Nexus collection (nexus_core.downloader + aria2c)."""
import json
import os
import re
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton,
    QSpinBox, QCheckBox, QProgressBar, QMessageBox, QGroupBox,
)

from .config import DEF_COLLECTION, DEF_DOWNLOADS, DEF_COOKIES
from .parsing import (slug_from, human_bytes, dir_size,
                      RE_QUEUED, RE_HEARTBEAT, RE_DONE, RE_FAIL)
from .runner import ProcRunner, backend, preview_argv, fmt_argv
from .widgets import BrowseEdit, LogView
from .dialogs import CollectionPreviewDialog


class DownloadTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self.preview_runner = None
        self._preview_buf = []
        self._preview_name = ""
        self._preview_out = None
        self.total_bytes = 0
        self.total_files = 0

        self.collection = QLineEdit(cfg.value("dl/collection", DEF_COLLECTION))
        self.downloads  = BrowseEdit(cfg.value("dl/downloads", DEF_DOWNLOADS), "dir")
        self.cookies    = BrowseEdit(cfg.value("dl/cookies", DEF_COOKIES), "file")

        self.jobs = QSpinBox(); self.jobs.setRange(0, 16); self.jobs.setValue(0)
        self.jobs.setSpecialValueText("auto")
        self.conns = QSpinBox(); self.conns.setRange(0, 32); self.conns.setValue(0)
        self.conns.setSpecialValueText("auto")
        self.gentle   = QCheckBox("Gentle (dodge free-download throttle)")
        self.verify   = QCheckBox("Verify archives first (prune corrupt)")
        self.optional = QCheckBox("Include optional files")

        form = QFormLayout()
        form.addRow("Collection (slug or URL):", self.collection)
        form.addRow("Download folder:", self.downloads)
        form.addRow("Cookies file:", self.cookies)
        conc = QHBoxLayout()
        conc.addWidget(QLabel("Jobs:")); conc.addWidget(self.jobs)
        conc.addSpacing(12)
        conc.addWidget(QLabel("Connections/file:")); conc.addWidget(self.conns)
        conc.addStretch(1)
        cw = QWidget(); cw.setLayout(conc)
        form.addRow("Concurrency (0 = script default):", cw)
        form.addRow("", self.gentle)
        form.addRow("", self.verify)
        form.addRow("", self.optional)

        self.preview_btn = QPushButton("Preview collection")
        self.start_btn = QPushButton("Start download")
        self.start_btn.setProperty("accent", True)      # green primary action
        self.stop_btn  = QPushButton("Stop"); self.stop_btn.setEnabled(False)
        self.preview_btn.clicked.connect(self.preview)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        btns = QHBoxLayout()
        btns.addWidget(self.preview_btn)
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)
        btns.addStretch(1)

        self.bar = QProgressBar(); self.bar.setRange(0, 1000)
        self.status = QLabel("Idle.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)

        self.log = LogView()

        lay = QVBoxLayout(self)
        box = QGroupBox("Collection download (nexus_core.downloader + aria2c)")
        box.setLayout(form)
        lay.addWidget(box)
        lay.addLayout(btns)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        lay.addWidget(self.log, 1)

        self.poll = QTimer(self)
        self.poll.setInterval(2000)
        self.poll.timeout.connect(self._poll_dir)

    # -- collection preview --
    def preview(self):
        if self.runner or self.preview_runner:
            return
        if not Path(self.cookies.text()).is_file():
            QMessageBox.warning(self, "Missing", f"Cookies file not found:\n{self.cookies.text()}")
            return
        slug = slug_from(self.collection.text()) or DEF_COLLECTION
        self._preview_out = Path(tempfile.gettempdir()) / f"nexus_preview_{os.getpid()}.json"
        try:
            self._preview_out.unlink()
        except OSError:
            pass
        argv = preview_argv(self.cookies.text(), slug, str(self._preview_out))
        self._preview_buf = []
        self._preview_name = ""
        self.preview_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.status.setText(f"Fetching collection '{slug}' …")
        self.log.clear()
        self.log.add(f"$ preview '{slug}'")
        self.preview_runner = ProcRunner(argv)
        self.preview_runner.line.connect(self._preview_line)
        self.preview_runner.finished_rc.connect(self._preview_done)
        self.preview_runner.start()

    def _preview_line(self, text):
        self.log.add(text)
        self._preview_buf.append(text)
        m = re.match(r"Collection: (.+?)\s+\(revision", text)
        if m:
            self._preview_name = m.group(1)

    def _preview_done(self, rc):
        self.preview_runner = None
        self.preview_btn.setEnabled(True)
        self.start_btn.setEnabled(True)
        data, out = None, self._preview_out
        if out and out.exists():
            try:
                data = json.loads(out.read_text())
            except (OSError, ValueError):
                data = None
            try:
                out.unlink()
            except OSError:
                pass
        if rc != 0 or not data:
            tail = "\n".join(self._preview_buf[-8:]) or f"exit code {rc}"
            QMessageBox.warning(self, "Preview failed",
                                "Could not fetch the collection:\n\n" + tail)
            self.status.setText("Preview failed — see log.")
            return
        files = data.get("files", [])
        manual = data.get("manual", [])
        total = int(data.get("total") or 0)
        self.status.setText(f"Preview: {len(files)} file(s) · {human_bytes(total)}.")
        dlg = CollectionPreviewDialog(self, self._preview_name, files, manual)
        dlg.exec()
        if dlg.download_requested:
            self.start()

    # -- lifecycle --
    def start(self):
        if self.runner or self.preview_runner:
            return
        cookies = Path(self.cookies.text())
        if not cookies.is_file():
            QMessageBox.warning(self, "Missing", f"Cookies file not found:\n{cookies}")
            return
        out = self.downloads.text()
        Path(out).mkdir(parents=True, exist_ok=True)

        argv = backend("downloader") + [self.collection.text().strip() or DEF_COLLECTION,
                "--out", out, "--cookies", self.cookies.text()]
        if self.jobs.value() > 0:
            argv += ["--jobs", str(self.jobs.value())]
        if self.conns.value() > 0:
            argv += ["--connections", str(self.conns.value())]
        if self.gentle.isChecked():
            argv.append("--gentle")
        if self.verify.isChecked():
            argv.append("--verify")
        if self.optional.isChecked():
            argv.append("--include-optional")

        self.total_bytes = 0
        self.total_files = 0
        self.bar.setValue(0)
        self.status.setText("Starting…")
        self.log.clear()
        self.log.add("$ " + fmt_argv(argv))
        self._set_running(True)
        self.poll.start()
        self.runner = ProcRunner(argv)
        self.runner.line.connect(self._on_line)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def stop(self):
        if self.runner:
            self.status.setText("Stopping (leaving resumable partials)…")
            self.runner.stop()
            self.stop_btn.setEnabled(False)

    def _set_running(self, running):
        self.start_btn.setEnabled(not running)
        self.preview_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        for w in (self.collection, self.downloads, self.cookies,
                  self.jobs, self.conns, self.gentle, self.verify, self.optional):
            w.setEnabled(not running)

    def _poll_dir(self):
        if self.total_bytes <= 0:
            return
        cur = dir_size(Path(self.downloads.text()))
        frac = min(cur / self.total_bytes, 1.0)
        self.bar.setValue(int(frac * 1000))

    # -- output parsing --
    def _on_line(self, text):
        self.log.add(text)
        m = RE_QUEUED.search(text)
        if m:
            self.total_files = int(m.group(1))
            self.total_bytes = int(float(m.group(2)) * 1e9)
            self.status.setText(f"Queued {self.total_files} files · {m.group(2)} GB")
            return
        m = RE_HEARTBEAT.search(text)
        if m:
            cur_disp, tot_disp, pct, mbps, eta, active, done, failed = m.groups()
            self.bar.setValue(int(float(pct) * 10))
            self.status.setText(
                f"{cur_disp} / {tot_disp}  ·  {pct}%  ·  {mbps} MB/s  ·  ETA {eta}  ·  "
                f"active {active}  ·  done {done}  ·  failed {failed}"
                + (f"  of {self.total_files}" if self.total_files else ""))
            return
        if RE_DONE.match(text) or RE_FAIL.match(text):
            self._poll_dir()

    def _on_done(self, rc):
        self.poll.stop()
        self._poll_dir()
        self._set_running(False)
        self.runner = None
        if rc == 0:
            self.bar.setValue(1000)
            self.status.setText("Download finished. Re-run to retry any failures.")
        elif rc in (130, -1) or self._just_stopped(rc):
            self.status.setText("Stopped. Completed files are kept; partials resume on re-run.")
        else:
            self.status.setText(f"Exited with code {rc}. See log above.")

    @staticmethod
    def _just_stopped(rc):
        return rc in (130, 143, -2, -15, -9)

    def save(self):
        c = self.cfg
        c.setValue("dl/collection", self.collection.text())
        c.setValue("dl/downloads", self.downloads.text())
        c.setValue("dl/cookies", self.cookies.text())
