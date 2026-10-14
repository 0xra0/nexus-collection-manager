"""Mod Scanner tab — recursive discovery, TF-IDF/DBSCAN grouping and hash tracking."""
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton,
    QSpinBox, QDoubleSpinBox, QPlainTextEdit, QMessageBox, QGroupBox,
    QProgressBar,
)

from .config import DEF_GAME_ROOT, DEF_SCAN_PATHS
from .runner import ProcRunner, backend, fmt_argv
from .widgets import BrowseEdit, LogView

RE_SCAN_PROGRESS = re.compile(r"^\[scanner-progress\]\s+hash\s+(\d+)/(\d+)\s*$")
RE_SCAN_SUMMARY = re.compile(
    r"^\[scanner\]\s+roots=(\d+)\s+files=(\d+)\s+groups=(\d+)\s+"
    r"added=(\d+)\s+changed=(\d+)\s+removed=(\d+)\s+unchanged=(\d+)\s+errors=(\d+)\s*$"
)
RE_SCAN_STATE = re.compile(r"^\[scanner-state\]\s+(.+?)\s*$")


class ScannerTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self._summary = None
        self._state_path = ""
        self._updating = True

        root_default = cfg.value("sc/game_root", cfg.value("dp/game_root", DEF_GAME_ROOT))
        self.game_root = BrowseEdit(root_default, "dir")

        self.paths = QPlainTextEdit()
        self.paths.setMaximumHeight(145)
        saved_paths = cfg.value("sc/paths", "\n".join(DEF_SCAN_PATHS))
        self.paths.setPlainText(saved_paths)
        self.paths.setPlaceholderText(
            "One root per line. Relative paths are resolved under the game root; absolute paths are allowed.")

        self.eps = QDoubleSpinBox()
        self.eps.setRange(0.05, 0.95)
        self.eps.setSingleStep(0.05)
        self.eps.setDecimals(2)
        self.eps.setValue(float(cfg.value("sc/eps", 0.65)))
        self.eps.setToolTip(
            "DBSCAN cosine-distance threshold. Lower = stricter grouping; 0.65 means cosine similarity >= 0.35.")

        self.min_samples = QSpinBox()
        self.min_samples.setRange(1, 20)
        self.min_samples.setValue(int(cfg.value("sc/min_samples", 1)))
        self.min_samples.setToolTip(
            "DBSCAN minimum neighborhood size. 1 keeps legitimate one-file mods as singleton groups.")

        self.jobs = QSpinBox()
        self.jobs.setRange(1, 32)
        self.jobs.setValue(int(cfg.value("sc/jobs", 4)))
        self.jobs.setToolTip("Parallel SHA-256 integrity + MD5 Nexus-identity hashing workers.")

        form = QFormLayout()
        form.addRow("Game root:", self.game_root)
        form.addRow("Scan roots:", self.paths)

        cluster_row = QHBoxLayout()
        cluster_row.addWidget(QLabel("ε:"))
        cluster_row.addWidget(self.eps)
        cluster_row.addSpacing(12)
        cluster_row.addWidget(QLabel("Min samples:"))
        cluster_row.addWidget(self.min_samples)
        cluster_row.addSpacing(12)
        cluster_row.addWidget(QLabel("Hash jobs:"))
        cluster_row.addWidget(self.jobs)
        cluster_row.addStretch(1)
        cluster_widget = QWidget()
        cluster_widget.setLayout(cluster_row)
        form.addRow("TF-IDF + DBSCAN:", cluster_widget)

        note = QLabel(
            "Every file is SHA-256 hashed for integrity and MD5 hashed for Nexus identity lookup. The snapshot is stored outside the game folder; "
            "Verify compares against it without replacing the baseline.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("", note)

        self.scan_btn = QPushButton("Scan + update snapshot")
        self.scan_btn.setProperty("accent", True)
        self.verify_btn = QPushButton("Verify against snapshot")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.scan_btn.clicked.connect(lambda: self.start(update_snapshot=True))
        self.verify_btn.clicked.connect(lambda: self.start(update_snapshot=False))
        self.stop_btn.clicked.connect(self.stop)

        btns = QHBoxLayout()
        btns.addWidget(self.scan_btn)
        btns.addWidget(self.verify_btn)
        btns.addWidget(self.stop_btn)
        btns.addStretch(1)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.status = QLabel("Idle.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.log = LogView()

        box = QGroupBox("Recursive Mod Scanner")
        box.setLayout(form)
        lay = QVBoxLayout(self)
        lay.addWidget(box)
        lay.addLayout(btns)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        lay.addWidget(self.log, 1)

    def _configured_paths(self):
        paths = []
        for line in self.paths.toPlainText().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                paths.append(line)
        return paths

    def start(self, update_snapshot=True):
        if self.runner:
            return
        root = Path(self.game_root.text())
        if not root.exists():
            QMessageBox.warning(self, "Missing", f"Game root not found:\n{root}")
            return
        paths = self._configured_paths()
        if not paths:
            QMessageBox.warning(self, "No scan roots", "Add at least one scan root.")
            return

        argv = backend("scanner") + [
            "--game-root", str(root),
            "--eps", f"{self.eps.value():.2f}",
            "--min-samples", str(self.min_samples.value()),
            "--jobs", str(self.jobs.value()),
        ]
        for path in paths:
            argv += ["--path", path]
        if not update_snapshot:
            argv.append("--no-state")

        self._updating = update_snapshot
        self._summary = None
        self._state_path = ""
        self.bar.setValue(0)
        self.log.clear()
        action = "Scanning and updating integrity snapshot…" if update_snapshot else "Verifying against saved snapshot…"
        self.status.setText(action)
        self.log.add("$ " + fmt_argv(argv))
        self._set_running(True)
        self.runner = ProcRunner(argv)
        self.runner.line.connect(self._on_line)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def _set_running(self, running):
        self.scan_btn.setEnabled(not running)
        self.verify_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def stop(self):
        if self.runner:
            self.status.setText("Stopping…")
            self.runner.stop()
            self.stop_btn.setEnabled(False)

    def _on_line(self, text):
        self.log.add(text)
        m = RE_SCAN_PROGRESS.match(text)
        if m:
            current, total = int(m.group(1)), int(m.group(2))
            self.bar.setValue(int(current / total * 100) if total else 0)
            self.status.setText(f"Hashing files… {current}/{total}")
            return
        m = RE_SCAN_SUMMARY.match(text)
        if m:
            self._summary = tuple(int(x) for x in m.groups())
            return
        m = RE_SCAN_STATE.match(text)
        if m:
            self._state_path = m.group(1)

    def _on_done(self, rc):
        self.runner = None
        self._set_running(False)
        if rc != 0:
            self.status.setText(f"Scanner failed (exit {rc}); see log.")
            return
        self.bar.setValue(100)
        if not self._summary:
            self.status.setText("Scan complete.")
            return

        roots, files, groups, added, changed, removed, unchanged, errors = self._summary
        mode = "Snapshot updated" if self._updating else "Verification complete"
        self.status.setText(
            f"{mode} — {files} files, {groups} groups; "
            f"+{added} / Δ{changed} / −{removed}; {errors} error(s).")

        if not self._updating and (added or changed or removed):
            QMessageBox.warning(
                self, "Mod integrity changed",
                f"Integrity verification found {added} added, {changed} changed, and "
                f"{removed} removed file(s).\n\n"
                "The saved baseline was NOT replaced. See the scanner log for paths."
                + (f"\n\nSnapshot: {self._state_path}" if self._state_path else ""))
        elif errors:
            QMessageBox.warning(
                self, "Scanner completed with errors",
                f"The scan completed, but {errors} file/directory read error(s) occurred. "
                "See the log before treating this snapshot as complete.")

    def save(self):
        self.cfg.setValue("sc/game_root", self.game_root.text())
        self.cfg.setValue("sc/paths", self.paths.toPlainText())
        self.cfg.setValue("sc/eps", self.eps.value())
        self.cfg.setValue("sc/min_samples", self.min_samples.value())
        self.cfg.setValue("sc/jobs", self.jobs.value())
