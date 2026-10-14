"""Persistent Nexus download queue: direct Premium downloads + NXM links."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton,
    QSpinBox, QProgressBar, QMessageBox, QGroupBox, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QHeaderView, QCheckBox,
)

from .config import DEF_GAME_ROOT
from .runner import ProcRunner, backend
from .widgets import BrowseEdit, safe_horizontal_header
from .nexus_credentials import bind_api_key_edit

PREFIX = "[downloads-json] "


def _human_bytes(value: int | float) -> str:
    n = float(value or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if abs(n) < 1000 or unit == units[-1]:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


class DownloadManagerTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self.query_runner = None
        self._query_result = None
        self._action = ""
        self.jobs: dict[int, dict] = {}

        self.game_root = BrowseEdit(cfg.value("dm/game_root", cfg.value("dp/game_root", DEF_GAME_ROOT)), "dir")
        self.api_key = QLineEdit()
        bind_api_key_edit(self.api_key, cfg)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Nexus Mods API key")
        self.api_key.setToolTip("Premium accounts can download directly. Free accounts use authorization from an NXM manager link.")
        self.account = QLabel("Account not checked.")
        self.account.setTextFormat(Qt.TextFormat.PlainText)
        self.check_account = QPushButton("Check account")
        self.check_account.clicked.connect(self.account_status)

        form = QFormLayout()
        form.addRow("Game root:", self.game_root)
        form.addRow("Nexus API key:", self.api_key)
        accrow = QHBoxLayout(); accrow.addWidget(self.check_account); accrow.addWidget(self.account, 1)
        accw = QWidget(); accw.setLayout(accrow); form.addRow("Account:", accw)
        note = QLabel(
            "Premium: enter Mod ID + File ID for direct API downloads. Free: click “Download with Manager” on Nexus, "
            "or paste its nxm:// link here. Completed downloads are imported into the Archives staging library automatically."
        )
        note.setWordWrap(True); note.setProperty("muted", True)
        form.addRow("", note)
        settings = QGroupBox("Nexus Download Manager")
        settings.setLayout(form)

        self.mod_id = QSpinBox(); self.mod_id.setRange(1, 99_999_999)
        self.file_id = QSpinBox(); self.file_id.setRange(1, 99_999_999)
        self.add_btn = QPushButton("Queue IDs")
        self.add_run_btn = QPushButton("Queue + Download")
        self.add_run_btn.setProperty("accent", True)
        self.add_btn.clicked.connect(lambda: self.add_ids(False))
        self.add_run_btn.clicked.connect(lambda: self.add_ids(True))
        ids = QHBoxLayout()
        ids.addWidget(QLabel("Mod ID:")); ids.addWidget(self.mod_id)
        ids.addWidget(QLabel("File ID:")); ids.addWidget(self.file_id)
        ids.addWidget(self.add_btn); ids.addWidget(self.add_run_btn); ids.addStretch(1)

        self.nxm = QLineEdit()
        self.nxm.setPlaceholderText("nxm://cyberpunk2077/mods/123/files/456?key=…&expires=…&user_id=…")
        self.nxm.setEchoMode(QLineEdit.EchoMode.Normal)
        self.nxm_btn = QPushButton("Queue NXM")
        self.nxm_run_btn = QPushButton("Queue + Download NXM")
        self.nxm_btn.clicked.connect(lambda: self.add_nxm(False))
        self.nxm_run_btn.clicked.connect(lambda: self.add_nxm(True))
        nxmrow = QHBoxLayout(); nxmrow.addWidget(self.nxm, 1); nxmrow.addWidget(self.nxm_btn); nxmrow.addWidget(self.nxm_run_btn)

        self.register_btn = QPushButton("Register Linux NXM handler")
        self.register_btn.setToolTip("Make browser “Download with Manager” nxm:// links open this manager backend on Linux.")
        self.register_btn.clicked.connect(self.register_nxm)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        top_actions = QHBoxLayout(); top_actions.addWidget(self.register_btn); top_actions.addWidget(self.refresh_btn); top_actions.addStretch(1)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["ID", "Nexus", "File", "Status", "Progress", "Size", "Error"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        h = safe_horizontal_header(self.table)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self.open_nexus)

        self.run_btn = QPushButton("Download / Resume")
        self.run_btn.setProperty("accent", True)
        self.run_all_btn = QPushButton("Run queue")
        self.stop_btn = QPushButton("Pause/Stop"); self.stop_btn.setEnabled(False)
        self.retry_btn = QPushButton("Retry")
        self.remove_btn = QPushButton("Remove from queue")
        self.delete_partial = QCheckBox("delete partial on remove")
        self.open_btn = QPushButton("Open Nexus")
        self.run_btn.clicked.connect(self.run_selected)
        self.run_all_btn.clicked.connect(self.run_all)
        self.stop_btn.clicked.connect(self.stop)
        self.retry_btn.clicked.connect(self.retry_selected)
        self.remove_btn.clicked.connect(self.remove_selected)
        self.open_btn.clicked.connect(self.open_nexus)
        buttons = QHBoxLayout()
        for w in (self.run_btn, self.run_all_btn, self.stop_btn, self.retry_btn, self.remove_btn, self.delete_partial, self.open_btn):
            buttons.addWidget(w)
        buttons.addStretch(1)

        self.overall = QProgressBar(); self.overall.setRange(0, 1000)
        self.status = QLabel("Ready."); self.status.setTextFormat(Qt.TextFormat.PlainText)

        lay = QVBoxLayout(self)
        lay.addWidget(settings)
        lay.addLayout(ids)
        lay.addLayout(nxmrow)
        lay.addLayout(top_actions)
        lay.addWidget(self.table, 1)
        lay.addLayout(buttons)
        lay.addWidget(self.overall)
        lay.addWidget(self.status)

        self.poll = QTimer(self); self.poll.setInterval(900); self.poll.timeout.connect(self.refresh)
        QTimer.singleShot(100, self.refresh)
        if self.api_key.text().strip():
            QTimer.singleShot(250, self.account_status)

    def _env(self):
        key = self.api_key.text().strip()
        return {"NEXUS_API_KEY": key} if key else {}

    def _root_ok(self) -> Path | None:
        root = Path(self.game_root.text()).expanduser()
        if not root.is_dir():
            QMessageBox.warning(self, "Game root", f"Game root not found:\n{root}")
            return None
        return root

    def _parse_line(self, text):
        if not text.startswith(PREFIX):
            return None
        try:
            return json.loads(text[len(PREFIX):])
        except ValueError:
            return None

    def _selected_job_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        try:
            return int(item.text()) if item else None
        except ValueError:
            return None

    def _start_action(self, argv, action, *, after_job: int | None = None):
        if self.runner:
            return
        self._action = action
        self._after_job = after_job
        self.runner = ProcRunner(argv, extra_env=self._env())
        self.runner.line.connect(self._action_line)
        self.runner.finished_rc.connect(self._action_done)
        self.runner.start()

    def _action_line(self, text):
        data = self._parse_line(text)
        if not data:
            return
        typ = data.get("type")
        if typ == "progress":
            jid = int(data.get("job_id") or 0)
            frac = float(data.get("progress") or 0.0)
            speed = _human_bytes(int(data.get("speed_bps") or 0)) + "/s"
            self.overall.setValue(max(0, min(1000, int(frac * 1000))))
            self.status.setText(
                f"Downloading job #{jid}: {_human_bytes(data.get('bytes_done', 0))} / "
                f"{_human_bytes(data.get('total_bytes', 0))} · {speed}"
            )
        elif typ == "complete":
            job = data.get("job") or {}
            self.status.setText(f"Downloaded {job.get('file_name', 'archive')} and imported it into Archives.")
            self.overall.setValue(1000)
        elif typ == "error":
            self.status.setText(str(data.get("message") or "Download failed"))
        elif typ in {"queued", "job", "removed", "nxm-handler"}:
            self._query_result = data

    def _action_done(self, rc):
        action, after = self._action, getattr(self, "_after_job", None)
        self.runner = None
        self.stop_btn.setEnabled(False)
        self.run_btn.setEnabled(True); self.run_all_btn.setEnabled(True)
        self.poll.stop()
        if rc == 0 and after and action in {"add", "nxm"}:
            self.run_job(after)
            return
        if rc == 0 and action == "register":
            QMessageBox.information(self, "NXM handler", "Linux nxm:// handler registered for this game root.")
        elif rc != 0 and action not in {"run", "run-all"}:
            QMessageBox.warning(self, "Download Manager", self.status.text() or f"Action failed (exit {rc}).")
        self.refresh()

    def _simple_action(self, args, action):
        root = self._root_ok()
        if not root or self.runner:
            return
        self._query_result = None
        argv = backend("download_manager") + args + ["--game-root", str(root)]
        self._start_action(argv, action)

    def add_ids(self, run_after: bool):
        root = self._root_ok()
        if not root or self.runner:
            return
        self._query_result = None
        argv = backend("download_manager") + [
            "add", "--game-root", str(root), "--mod-id", str(self.mod_id.value()), "--file-id", str(self.file_id.value())
        ]
        # Use a tiny local callback convention: capture queued ID from stdout and
        # trigger run in _action_done.
        self._after_job = None
        self._run_after_add = run_after
        self._action = "add"
        self.runner = ProcRunner(argv, extra_env=self._env())
        def on_line(text):
            data = self._parse_line(text)
            if data and data.get("type") == "queued":
                job = data.get("job") or {}
                self._after_job = int(job.get("id") or 0) if run_after else None
                self.status.setText(f"Queued Nexus #{job.get('mod_id')} file #{job.get('file_id')}.")
            self._action_line(text)
        self.runner.line.connect(on_line); self.runner.finished_rc.connect(self._action_done); self.runner.start()

    def add_nxm(self, run_after: bool):
        root = self._root_ok()
        url = self.nxm.text().strip()
        if not root or self.runner:
            return
        if not url.lower().startswith("nxm://"):
            QMessageBox.warning(self, "NXM link", "Paste a complete nxm:// Download with Manager link.")
            return
        argv = backend("download_manager") + ["nxm", "--game-root", str(root), url]
        self._after_job = None; self._action = "nxm"
        self.runner = ProcRunner(argv, extra_env=self._env())
        def on_line(text):
            data = self._parse_line(text)
            if data and data.get("type") == "queued":
                job = data.get("job") or {}
                self._after_job = int(job.get("id") or 0) if run_after else None
                self.status.setText(f"Queued manager download for Nexus #{job.get('mod_id')} file #{job.get('file_id')}.")
                self.nxm.clear()
            self._action_line(text)
        self.runner.line.connect(on_line); self.runner.finished_rc.connect(self._action_done); self.runner.start()

    def accept_nxm(self, url: str):
        """Receive an nxm:// URL from the Linux desktop protocol handler."""
        self.nxm.setText(str(url))
        self.add_nxm(True)

    def register_nxm(self):
        root = self._root_ok()
        if not root or self.runner:
            return
        argv = backend("download_manager") + ["register-nxm", "--game-root", str(root)]
        self._start_action(argv, "register")

    def account_status(self):
        root = self._root_ok()
        if not root or self.query_runner:
            return
        argv = backend("download_manager") + ["account", "--game-root", str(root)]
        self.query_runner = ProcRunner(argv, extra_env=self._env())
        result = {}
        def line(text):
            nonlocal result
            data = self._parse_line(text)
            if data:
                result = data
        def done(rc):
            self.query_runner = None
            if rc != 0 or result.get("type") == "error":
                self.account.setText(result.get("message") or "Account check failed")
            elif not result.get("configured"):
                self.account.setText("No API key configured")
            else:
                tier = "Premium" if result.get("premium") else "Free"
                self.account.setText(f"{result.get('name') or 'Nexus user'} · {tier}")
        self.query_runner.line.connect(line); self.query_runner.finished_rc.connect(done); self.query_runner.start()

    def refresh(self):
        root = Path(self.game_root.text()).expanduser()
        if self.query_runner or not root.is_dir():
            return
        argv = backend("download_manager") + ["queue", "--game-root", str(root)]
        self.query_runner = ProcRunner(argv, extra_env=self._env())
        result = {}
        def line(text):
            nonlocal result
            data = self._parse_line(text)
            if data and data.get("type") == "queue":
                result = data
        def done(_rc):
            self.query_runner = None
            if result:
                self._apply_queue(result.get("items") or [])
        self.query_runner.line.connect(line); self.query_runner.finished_rc.connect(done); self.query_runner.start()

    def _apply_queue(self, items):
        selected = self._selected_job_id()
        self.jobs = {int(j["id"]): dict(j) for j in items}
        self.table.setRowCount(len(items))
        for r, job in enumerate(items):
            jid = int(job["id"])
            pct = float(job.get("progress") or 0.0) * 100
            vals = [
                str(jid), f"#{job.get('mod_id')} / #{job.get('file_id')}", str(job.get("display_name") or job.get("file_name") or ""),
                str(job.get("status") or ""), f"{pct:.1f}%", _human_bytes(job.get("total_bytes") or job.get("size") or 0),
                str(job.get("error") or ""),
            ]
            for c, value in enumerate(vals):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, jid)
                self.table.setItem(r, c, item)
            if selected == jid:
                self.table.selectRow(r)
        if not items:
            self.status.setText("Download queue is empty.")

    def run_job(self, job_id: int):
        root = self._root_ok()
        if not root or self.runner:
            return
        if not self.api_key.text().strip():
            QMessageBox.warning(self, "Nexus API key", "Add your Nexus Mods API key before downloading.")
            return
        argv = backend("download_manager") + ["run", "--game-root", str(root), "--job-id", str(job_id)]
        self.run_btn.setEnabled(False); self.run_all_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.poll.start(); self._start_action(argv, "run")

    def run_selected(self):
        jid = self._selected_job_id()
        if jid is None:
            QMessageBox.information(self, "Download", "Select a queued download first.")
            return
        self.run_job(jid)

    def run_all(self):
        root = self._root_ok()
        if not root or self.runner:
            return
        if not self.api_key.text().strip():
            QMessageBox.warning(self, "Nexus API key", "Add your Nexus Mods API key before downloading.")
            return
        argv = backend("download_manager") + ["run", "--game-root", str(root)]
        self.run_btn.setEnabled(False); self.run_all_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.poll.start(); self._start_action(argv, "run-all")

    def stop(self):
        if self.runner:
            self.status.setText("Stopping after the current network read; partial bytes remain resumable…")
            self.runner.stop(); self.stop_btn.setEnabled(False)

    def retry_selected(self):
        jid = self._selected_job_id()
        root = self._root_ok()
        if jid is None or not root or self.runner:
            return
        argv = backend("download_manager") + ["retry", "--game-root", str(root), "--job-id", str(jid)]
        self._start_action(argv, "retry")

    def remove_selected(self):
        jid = self._selected_job_id()
        root = self._root_ok()
        if jid is None or not root or self.runner:
            return
        job = self.jobs.get(jid, {})
        if job.get("status") == "downloading":
            QMessageBox.warning(self, "Download", "Stop the active download before removing it.")
            return
        if QMessageBox.question(self, "Remove download", f"Remove job #{jid} from the queue?") != QMessageBox.StandardButton.Yes:
            return
        argv = backend("download_manager") + ["remove", "--game-root", str(root), "--job-id", str(jid)]
        if self.delete_partial.isChecked():
            argv.append("--delete-partial")
        self._start_action(argv, "remove")

    def open_nexus(self, *_args):
        jid = self._selected_job_id()
        if jid is None:
            return
        url = str(self.jobs.get(jid, {}).get("nexus_url") or "")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def save(self):
        self.cfg.setValue("dm/game_root", self.game_root.text())
