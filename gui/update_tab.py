"""Nexus update checker UI for smart-staged mods."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QLabel, QPushButton,
    QLineEdit, QCheckBox, QProgressBar, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QMenu,
)

from .config import DEF_GAME_ROOT
from .runner import ProcRunner, backend, fmt_argv
from .widgets import BrowseEdit, LogView, safe_horizontal_header
from .nexus_credentials import bind_api_key_edit

PREFIX = "[updates-json] "
DOWNLOAD_PREFIX = "[downloads-json] "


class UpdateTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self.mode = ""
        self.result = None
        self.rows: list[dict] = []

        self.game_root = BrowseEdit(
            cfg.value("updates/game_root", cfg.value("links/game_root", cfg.value("dm/game_root", DEF_GAME_ROOT))),
            "dir",
        )
        self.api_key = QLineEdit()
        bind_api_key_edit(self.api_key, cfg)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Nexus Mods API key")
        self.enabled_only = QCheckBox("Check enabled mods only")
        self.enabled_only.setChecked(str(cfg.value("updates/enabled_only", "false")).lower() in {"1", "true", "yes"})
        form = QFormLayout()
        form.addRow("Game root:", self.game_root)
        form.addRow("Nexus API key:", self.api_key)
        form.addRow("", self.enabled_only)
        note = QLabel(
            "Updates are matched against the exact installed Nexus file when possible (file ID → MD5 → version), "
            "then compared through that file's Nexus update lineage. Download uses the existing persistent Download Manager."
        )
        note.setWordWrap(True); note.setProperty("muted", True)
        form.addRow("", note)
        box = QGroupBox("Mod Update Checker")
        box.setLayout(form)

        self.check_btn = QPushButton("Check for updates")
        self.check_btn.setProperty("accent", True)
        self.download_btn = QPushButton("Download update")
        self.open_btn = QPushButton("Open Nexus")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.check_btn.clicked.connect(self.check_updates)
        self.download_btn.clicked.connect(self.download_selected)
        self.open_btn.clicked.connect(self.open_nexus)
        self.stop_btn.clicked.connect(self.stop)
        controls = QHBoxLayout()
        for button in (self.check_btn, self.download_btn, self.open_btn, self.stop_btn):
            controls.addWidget(button)
        controls.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.hide()
        self.status = QLabel("Updates have not been checked yet.")
        self.status.setWordWrap(True)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Mod", "State", "Installed", "Latest", "Status", "Matched by", "Nexus ID", "Source archive",
        ])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.doubleClicked.connect(lambda _idx: self.open_nexus())
        header = safe_horizontal_header(self.table)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 7):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)

        self.log = LogView(); self.log.setMaximumHeight(130)
        layout = QVBoxLayout(self)
        layout.addWidget(box)
        layout.addLayout(controls)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.log)

        self._selection_changed()
        QTimer.singleShot(350, self._startup)

    def _root(self) -> Path:
        return Path(self.game_root.text()).expanduser()

    def _env(self) -> dict[str, str]:
        key = self.api_key.text().strip()
        return {"NEXUS_API_KEY": key} if key else {}

    def _selected(self) -> dict | None:
        row = self.table.currentRow()
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def _selection_changed(self):
        row = self._selected()
        idle = self.runner is None
        self.download_btn.setEnabled(idle and bool(row) and row.get("status") == "update")
        self.open_btn.setEnabled(idle and bool(row) and bool(row.get("nexus_url")))

    def _startup(self):
        if self._root().is_dir() and self.api_key.text().strip():
            self.check_updates(quiet=True)

    def _start(self, argv: list[str], mode: str, *, quiet: bool = False):
        if self.runner:
            return
        root = self._root()
        if not root.is_dir():
            if not quiet:
                QMessageBox.warning(self, "Mod Update Checker", f"Game root not found:\n{root}")
            return
        if not self.api_key.text().strip():
            if not quiet:
                QMessageBox.information(self, "Nexus API key", "Add your Nexus Mods API key first.")
            return
        if not quiet:
            self.log.add("$ " + fmt_argv(argv))
        self.mode = mode; self.result = None
        self.runner = ProcRunner(argv, extra_env=self._env())
        self._set_running(True)
        self.runner.line.connect(self._on_line)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def _set_running(self, running: bool):
        self.check_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        if not running:
            self.progress.hide()
        self._selection_changed()

    def check_updates(self, quiet: bool = False):
        argv = backend("updates") + ["check", "--game-root", str(self._root())]
        if self.enabled_only.isChecked():
            argv.append("--enabled-only")
        self.status.setText("Comparing staged mods with Nexus file metadata…")
        self.progress.setRange(0, 0); self.progress.show()
        self._start(argv, "check", quiet=quiet)

    def download_selected(self):
        row = self._selected()
        if not row or row.get("status") != "update":
            return
        self.status.setText(f"Downloading update for {row.get('name')}…")
        self.progress.setRange(0, 1000); self.progress.setValue(0); self.progress.show()
        argv = backend("updates") + [
            "download", "--game-root", str(self._root()),
            "--managed-mod-id", str(int(row.get("managed_mod_id") or 0)),
        ]
        self._start(argv, "download")

    def open_nexus(self):
        row = self._selected()
        if row and row.get("nexus_url"):
            QDesktopServices.openUrl(QUrl(str(row["nexus_url"])))

    def _menu(self, pos):
        row = self._selected()
        if not row:
            return
        menu = QMenu(self)
        download = menu.addAction("Download update")
        download.setEnabled(row.get("status") == "update" and self.runner is None)
        nexus = menu.addAction("Open Nexus Mods")
        nexus.setEnabled(bool(row.get("nexus_url")))
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == download:
            self.download_selected()
        elif action == nexus:
            self.open_nexus()

    def _on_line(self, text: str):
        self.log.add(text)
        if text.startswith(PREFIX):
            try:
                self.result = json.loads(text[len(PREFIX):])
            except ValueError:
                self.result = {"type": "error", "message": "Invalid update-checker response"}
            return
        if text.startswith(DOWNLOAD_PREFIX):
            try:
                payload = json.loads(text[len(DOWNLOAD_PREFIX):])
            except ValueError:
                return
            if payload.get("type") == "progress":
                total = int(payload.get("total_bytes") or 0)
                done = int(payload.get("bytes_done") or 0)
                if total > 0:
                    self.progress.setRange(0, 1000)
                    self.progress.setValue(min(1000, int(done * 1000 / total)))
                    self.status.setText(f"Downloading update… {done / (1024*1024):.1f} / {total / (1024*1024):.1f} MiB")

    def _on_done(self, rc: int):
        mode = self.mode; data = self.result or {}
        self.runner = None; self._set_running(False)
        if rc != 0 or data.get("type") == "error":
            QMessageBox.warning(self, "Mod Update Checker", str(data.get("message") or f"Backend exited with code {rc}"))
            self.status.setText("Update operation failed; see log.")
            return
        if mode == "check":
            self._render(data)
        elif mode == "download":
            if data.get("type") == "requires-nxm":
                url = str(data.get("nexus_url") or "")
                if url:
                    QDesktopServices.openUrl(QUrl(url))
                QMessageBox.information(
                    self,
                    "Nexus authorization required",
                    "This is a free Nexus account. The selected update's Files page has been opened. "
                    "Choose Download with Manager there; the registered NXM handler will send the archive to Downloads.",
                )
                self.status.setText("Waiting for Nexus Download-with-Manager authorization.")
            elif data.get("type") == "downloaded":
                archive = data.get("archive") or {}
                QMessageBox.information(
                    self,
                    "Update downloaded",
                    f"The update archive was downloaded into managed staging.\n\n{archive.get('path') or archive.get('file_name') or ''}\n\n"
                    "It has not been installed automatically; review/install it from the Install or Archives workspace.",
                )
                self.status.setText("Update downloaded to Archive Management.")
                self.check_updates(quiet=True)

    def _render(self, data: dict):
        self.rows = [dict(x) for x in data.get("items") or []]
        self.table.setRowCount(len(self.rows))
        labels = {
            "update": "UPDATE AVAILABLE",
            "current": "Current",
            "unknown": "Needs review",
            "unlinked": "Not linked",
            "error": "API error",
        }
        for r, row in enumerate(self.rows):
            vals = [
                str(row.get("name") or ""),
                "Enabled" if row.get("enabled") else "Disabled",
                str(row.get("installed_version") or "?"),
                str(row.get("latest_version") or "?"),
                labels.get(str(row.get("status")), str(row.get("status") or "")),
                str(row.get("match_source") or row.get("identity_source") or "—"),
                f"#{row.get('nexus_mod_id')}" if row.get("nexus_mod_id") else "—",
                str(row.get("archive_name") or row.get("source_archive") or "—"),
            ]
            for c, value in enumerate(vals):
                item = QTableWidgetItem(value)
                item.setToolTip(str(row.get("reason") or ""))
                if row.get("status") == "update" and c == 4:
                    font = item.font(); font.setBold(True); item.setFont(font)
                self.table.setItem(r, c, item)
        self.table.resizeRowsToContents()
        counts = data.get("counts") or {}
        self.status.setText(
            f"Profile: {data.get('profile', '?')} · {int(data.get('updates') or 0)} update(s) available · "
            f"{int(counts.get('current') or 0)} current · {int(counts.get('unknown') or 0) + int(counts.get('unlinked') or 0)} need identity/review."
        )
        self._selection_changed()

    def stop(self):
        if self.runner:
            self.runner.stop()
            self.status.setText("Stopping…")

    def save(self):
        self.cfg.setValue("updates/game_root", self.game_root.text())
        self.cfg.setValue("updates/enabled_only", self.enabled_only.isChecked())
