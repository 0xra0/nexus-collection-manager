"""Clean-game hardlink deployment, drift, migration and profile controls."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QLabel, QPushButton,
    QCheckBox, QComboBox, QSpinBox, QMessageBox, QInputDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView,
)

from .config import DEF_GAME_ROOT
from .runner import ProcRunner, backend, fmt_argv
from .widgets import BrowseEdit, LogView, safe_horizontal_header

RESULT_PREFIX = "[links-json] "


class DeploymentTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self.api_runner = None
        self.mode = ""
        self.result = None

        self.game_root = BrowseEdit(
            cfg.value("links/game_root", cfg.value("in/game_root", cfg.value("dp/game_root", DEF_GAME_ROOT))),
            "dir",
        )
        self.staging = QLabel("<game>/downloaded_mods/")
        self.staging.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form = QFormLayout()
        form.addRow("Game root:", self.game_root)
        form.addRow("Managed staging:", self.staging)
        paths = QGroupBox("Clean game folder")
        paths.setLayout(form)

        self.deploy_btn = QPushButton("Deploy links")
        self.deploy_btn.setProperty("accent", True)
        self.undeploy_btn = QPushButton("Undeploy")
        self.drift_btn = QPushButton("Check drift")
        self.migrate_btn = QPushButton("Migrate legacy install…")
        self.refresh_btn = QPushButton("Refresh status")
        self.auto_deploy = QCheckBox("Auto-deploy active profile when manager launches")
        self.auto_deploy.setChecked(str(cfg.value("links/auto_deploy", "true")).lower() not in {"0", "false", "no"})
        self.deploy_btn.clicked.connect(lambda: self._command("deploy"))
        self.undeploy_btn.clicked.connect(self._undeploy)
        self.drift_btn.clicked.connect(lambda: self._command("drift"))
        self.migrate_btn.clicked.connect(self._migrate)
        self.refresh_btn.clicked.connect(lambda: self._command("status", quiet=True))
        controls = QHBoxLayout()
        for b in (self.deploy_btn, self.undeploy_btn, self.drift_btn, self.migrate_btn, self.refresh_btn):
            controls.addWidget(b)
        controls.addStretch(1)

        self.profile = QComboBox()
        self.profile.setMinimumWidth(220)
        self.create_profile_btn = QPushButton("New profile…")
        self.switch_profile_btn = QPushButton("Switch + rebuild")
        self.create_profile_btn.clicked.connect(self._create_profile)
        self.switch_profile_btn.clicked.connect(self._switch_profile)
        pr = QHBoxLayout()
        pr.addWidget(QLabel("Active / target profile:"))
        pr.addWidget(self.profile)
        pr.addWidget(self.create_profile_btn)
        pr.addWidget(self.switch_profile_btn)
        pr.addStretch(1)
        profile_box = QGroupBox("Profiles — DB flag + hardlink rebuild")
        profile_box.setLayout(pr)

        self.status = QLabel("No deployment status loaded yet.")
        self.status.setWordWrap(True)
        self.mods = QTableWidget(0, 5)
        self.mods.setHorizontalHeaderLabels(["Managed mod", "Enabled", "Files", "REDmod blocked", "Profile"])
        self.mods.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mods.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        mh = safe_horizontal_header(self.mods)
        mh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, 5):
            mh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        status_box = QGroupBox("Active projection")
        sv = QVBoxLayout(status_box)
        sv.addWidget(self.status)
        sv.addWidget(self.mods)

        self.drift = QTableWidget(0, 3)
        self.drift.setHorizontalHeaderLabels(["Game path", "Drift", "Detail"])
        self.drift.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.drift.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        dh = safe_horizontal_header(self.drift)
        dh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        dh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        dh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        drift_box = QGroupBox("Drift detection")
        dv = QVBoxLayout(drift_box)
        dv.addWidget(self.drift)

        self.api_port = QSpinBox()
        self.api_port.setRange(1024, 65535)
        self.api_port.setValue(int(cfg.value("links/api_port", 8765)))
        self.api_btn = QPushButton("Start local API")
        self.api_stop_btn = QPushButton("Stop API")
        self.api_stop_btn.setEnabled(False)
        self.api_state = QLabel("POST /deploy endpoint is stopped")
        self.api_btn.clicked.connect(self._start_api)
        self.api_stop_btn.clicked.connect(self._stop_api)
        ar = QHBoxLayout()
        ar.addWidget(QLabel("Port:")); ar.addWidget(self.api_port)
        ar.addWidget(self.api_btn); ar.addWidget(self.api_stop_btn)
        ar.addWidget(self.api_state, 1)
        api_box = QGroupBox("Local deployment endpoint (127.0.0.1 only)")
        api_box.setLayout(ar)

        self.log = LogView()
        self.log.setMaximumHeight(150)
        lay = QVBoxLayout(self)
        lay.addWidget(paths)
        lay.addLayout(controls)
        lay.addWidget(self.auto_deploy)
        lay.addWidget(profile_box)
        lay.addWidget(status_box, 2)
        lay.addWidget(drift_box, 1)
        lay.addWidget(api_box)
        lay.addWidget(self.log)

        self.game_root.edit.textChanged.connect(self._update_staging_label)
        self._update_staging_label()
        QTimer.singleShot(250, self._startup)

    def _root(self) -> Path:
        return Path(self.game_root.text()).expanduser()

    def _update_staging_label(self):
        self.staging.setText(str(self._root() / "downloaded_mods"))

    def _startup(self):
        root = self._root()
        if not root.is_dir():
            return
        if self.auto_deploy.isChecked():
            self._command("deploy", quiet=True)
        else:
            self._command("status", quiet=True)

    def _set_running(self, running: bool):
        for b in (self.deploy_btn, self.undeploy_btn, self.drift_btn, self.migrate_btn,
                  self.refresh_btn, self.create_profile_btn, self.switch_profile_btn):
            b.setEnabled(not running)

    def _command(self, command: str, extra: list[str] | None = None, quiet: bool = False):
        if self.runner:
            return
        root = self._root()
        if not root.is_dir():
            if not quiet:
                QMessageBox.warning(self, "Deployment", f"Game root not found:\n{root}")
            return
        argv = backend("smart_deployment") + [command, "--game-root", str(root)] + list(extra or [])
        if not quiet:
            self.log.add("$ " + fmt_argv(argv))
        self.mode = command
        self.result = None
        self._set_running(True)
        self.runner = ProcRunner(argv)
        self.runner.line.connect(self._on_line)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def _on_line(self, text: str):
        self.log.add(text)
        if text.startswith(RESULT_PREFIX):
            try:
                self.result = json.loads(text[len(RESULT_PREFIX):])
            except ValueError:
                self.result = {"error": "Invalid deployment response"}

    def _on_done(self, rc: int):
        mode = self.mode
        data = self.result or {}
        self.runner = None
        self._set_running(False)
        if rc != 0 or data.get("error"):
            QMessageBox.warning(self, "Smart deployment", data.get("error") or f"Backend exited with code {rc}")
            return
        if mode == "drift":
            self._render_drift(data)
            self.status.setText(f"Drift check: {data.get('drift_count', 0)} changed deployed path(s).")
            return
        if mode in {"deploy", "undeploy", "profile-switch", "migrate"}:
            if mode == "deploy":
                self.status.setText(
                    f"Deploy: {len(data.get('linked') or [])} linked, {len(data.get('removed') or [])} removed, "
                    f"{len(data.get('conflicts') or [])} external conflict(s), "
                    f"{len(data.get('redmod_blocked') or [])} REDmod compile-required file(s)."
                )
            elif mode == "undeploy":
                self.status.setText(
                    f"Undeployed {len(data.get('removed') or [])} managed link(s); "
                    f"preserved {len(data.get('preserved') or [])} externally changed path(s)."
                )
            elif mode == "migrate":
                self.status.setText(
                    f"Migration imported {data.get('imported_mods', 0)} mod(s); "
                    f"{len(data.get('missing') or [])} missing/unsafe legacy path(s)."
                )
            QTimer.singleShot(0, lambda: self._command("status", quiet=True))
            return
        if mode in {"status", "profiles", "profile-create"}:
            self._render_status(data)

    def _render_status(self, data: dict):
        active = str(data.get("active_profile") or "Default")
        profiles = data.get("profiles") or []
        self.profile.blockSignals(True)
        self.profile.clear()
        for p in profiles:
            name = str(p.get("name") or "")
            self.profile.addItem(name)
            if name == active:
                self.profile.setCurrentIndex(self.profile.count() - 1)
        self.profile.blockSignals(False)
        mods = data.get("mods") or []
        self.mods.setRowCount(len(mods))
        for r, mod in enumerate(mods):
            values = [
                mod.get("name", ""), "yes" if mod.get("enabled") else "no",
                str(mod.get("files", 0)), str(mod.get("redmod_blocked", 0)), active,
            ]
            for c, value in enumerate(values):
                self.mods.setItem(r, c, QTableWidgetItem(str(value)))
        drift = data.get("drift") or {}
        self._render_drift(drift)
        self.status.setText(
            f"Profile {active} · {data.get('mod_count', len(mods))} staged mod(s) · "
            f"{data.get('deployed_links', 0)}/{data.get('desired_links', 0)} desired link(s) deployed · "
            f"drift {drift.get('drift_count', 0)}"
        )

    def _render_drift(self, data: dict):
        rows = data.get("items") or []
        self.drift.setRowCount(len(rows))
        for r, item in enumerate(rows):
            for c, key in enumerate(("path", "status", "detail")):
                self.drift.setItem(r, c, QTableWidgetItem(str(item.get(key, ""))))
        self.drift.resizeRowsToContents()

    def _undeploy(self):
        if QMessageBox.question(
            self, "Undeploy active profile",
            "Remove all game-tree hardlinks managed by this app?\n\nStaged mod content is kept in downloaded_mods and can be redeployed instantly."
        ) == QMessageBox.StandardButton.Yes:
            self._command("undeploy")

    def _migrate(self):
        if QMessageBox.question(
            self, "Migrate legacy install",
            "Convert the existing .mod_manifest.json in-place installation to managed staging?\n\n"
            "Files are copied into downloaded_mods first, then the old files are replaced with hardlinks. "
            "Legacy manager JSON is moved into downloaded_mods/legacy_state/."
        ) == QMessageBox.StandardButton.Yes:
            self._command("migrate")

    def _create_profile(self):
        name, ok = QInputDialog.getText(self, "New profile", "Profile name:")
        if not ok or not name.strip():
            return
        self._command("profile-create", [name.strip()])

    def _switch_profile(self):
        name = self.profile.currentText().strip()
        if not name:
            return
        self._command("profile-switch", [name])

    def _start_api(self):
        if self.api_runner:
            return
        root = self._root()
        if not root.is_dir():
            QMessageBox.warning(self, "Local API", f"Game root not found:\n{root}")
            return
        port = self.api_port.value()
        argv = backend("smart_deployment") + [
            "serve", "--game-root", str(root), "--host", "127.0.0.1", "--port", str(port),
        ]
        self.log.add("$ " + fmt_argv(argv))
        self.api_runner = ProcRunner(argv)
        self.api_runner.line.connect(self.log.add)
        self.api_runner.finished_rc.connect(self._api_done)
        self.api_runner.start()
        self.api_btn.setEnabled(False)
        self.api_stop_btn.setEnabled(True)
        self.api_state.setText(f"POST http://127.0.0.1:{port}/deploy")

    def _api_done(self, _rc: int):
        self.api_runner = None
        self.api_btn.setEnabled(True)
        self.api_stop_btn.setEnabled(False)
        self.api_state.setText("POST /deploy endpoint is stopped")

    def _stop_api(self):
        if self.api_runner:
            self.api_runner.stop()
            self.api_stop_btn.setEnabled(False)

    def save(self):
        self.cfg.setValue("links/game_root", self.game_root.text())
        self.cfg.setValue("links/auto_deploy", self.auto_deploy.isChecked())
        self.cfg.setValue("links/api_port", self.api_port.value())
