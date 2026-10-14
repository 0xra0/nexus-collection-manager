"""Portable smart-deployment profile manager UI."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QLabel, QPushButton,
    QComboBox, QFileDialog, QInputDialog, QMessageBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView,
)

from .config import DEF_GAME_ROOT
from .runner import ProcRunner, backend, fmt_argv
from .widgets import BrowseEdit, LogView, safe_horizontal_header

PREFIX = "[profile-json] "


class ProfileTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self.mode = ""
        self.result = None
        self.rows: list[dict] = []
        self.pending_import: dict | None = None

        self.game_root = BrowseEdit(
            cfg.value("profiles/game_root", cfg.value("links/game_root", cfg.value("in/game_root", DEF_GAME_ROOT))),
            "dir",
        )
        form = QFormLayout(); form.addRow("Game root:", self.game_root)
        paths = QGroupBox("Profile library — DB state + hardlink rebuild")
        paths.setLayout(form)

        self.refresh_btn = QPushButton("Refresh")
        self.save_btn = QPushButton("Save current as…")
        self.load_btn = QPushButton("Load + rebuild")
        self.load_btn.setProperty("accent", True)
        self.duplicate_btn = QPushButton("Duplicate…")
        self.export_btn = QPushButton("Export…")
        self.import_btn = QPushButton("Import…")
        self.refresh_btn.clicked.connect(self.refresh)
        self.save_btn.clicked.connect(self.save_current)
        self.load_btn.clicked.connect(self.load_selected)
        self.duplicate_btn.clicked.connect(self.duplicate_selected)
        self.export_btn.clicked.connect(self.export_selected)
        self.import_btn.clicked.connect(self.import_profile)
        controls = QHBoxLayout()
        for b in (self.refresh_btn, self.save_btn, self.load_btn, self.duplicate_btn, self.export_btn, self.import_btn):
            controls.addWidget(b)
        controls.addStretch(1)

        self.status = QLabel("Profiles not loaded yet.")
        self.status.setWordWrap(True)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Profile", "Active", "Enabled", "Total mods", "Created"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _idx: self.load_selected())
        h = safe_horizontal_header(self.table)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, 5):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        profiles_box = QGroupBox("Saved profiles")
        pv = QVBoxLayout(profiles_box); pv.addWidget(self.status); pv.addWidget(self.table)

        self.left = QComboBox(); self.right = QComboBox()
        self.compare_btn = QPushButton("Compare")
        self.compare_btn.clicked.connect(self.compare)
        compare_controls = QHBoxLayout()
        compare_controls.addWidget(QLabel("Left:")); compare_controls.addWidget(self.left, 1)
        compare_controls.addWidget(QLabel("Right:")); compare_controls.addWidget(self.right, 1)
        compare_controls.addWidget(self.compare_btn)
        self.compare_status = QLabel("Choose two profiles to compare enabled mods, conflicts, and load-order preferences.")
        self.compare_status.setWordWrap(True)
        self.diff = QTableWidget(0, 4)
        self.diff.setHorizontalHeaderLabels(["Category", "Item", "Left", "Right"])
        self.diff.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.diff.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        dh = safe_horizontal_header(self.diff)
        dh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        dh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        dh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        dh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        compare_box = QGroupBox("Compare profiles")
        cv = QVBoxLayout(compare_box); cv.addLayout(compare_controls); cv.addWidget(self.compare_status); cv.addWidget(self.diff)

        self.log = LogView(); self.log.setMaximumHeight(120)
        lay = QVBoxLayout(self)
        lay.addWidget(paths)
        lay.addLayout(controls)
        lay.addWidget(profiles_box, 2)
        lay.addWidget(compare_box, 2)
        lay.addWidget(self.log)

        self._selection_changed()
        QTimer.singleShot(300, self._startup)

    def _root(self) -> Path:
        return Path(self.game_root.text()).expanduser()

    def _selected(self) -> dict | None:
        r = self.table.currentRow()
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def _selection_changed(self):
        selected = self._selected() is not None
        for b in (self.load_btn, self.duplicate_btn, self.export_btn):
            b.setEnabled(selected and self.runner is None)

    def _startup(self):
        if self._root().is_dir():
            self.refresh()

    def _argv(self, command: str, extra: list[str] | None = None) -> list[str]:
        return backend("profiles") + [command, "--game-root", str(self._root())] + list(extra or [])

    def _start(self, command: str, extra: list[str] | None = None, *, quiet: bool = False, mode: str | None = None):
        if self.runner:
            return
        root = self._root()
        if not root.is_dir():
            if not quiet:
                QMessageBox.warning(self, "Profile Manager", f"Game root not found:\n{root}")
            return
        argv = self._argv(command, extra)
        if not quiet:
            self.log.add("$ " + fmt_argv(argv))
        self.mode = mode or command; self.result = None
        self._set_running(True)
        self.runner = ProcRunner(argv)
        self.runner.line.connect(self._on_line)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def _set_running(self, running: bool):
        for b in (self.refresh_btn, self.save_btn, self.load_btn, self.duplicate_btn,
                  self.export_btn, self.import_btn, self.compare_btn):
            b.setEnabled(not running)
        if not running:
            self._selection_changed()

    def _on_line(self, text: str):
        self.log.add(text)
        if text.startswith(PREFIX):
            try:
                self.result = json.loads(text[len(PREFIX):])
            except ValueError:
                self.result = {"type": "error", "message": "Invalid profile backend response"}

    def _on_done(self, rc: int):
        mode = self.mode; data = self.result or {}
        self.runner = None; self._set_running(False)
        if rc != 0 or data.get("type") == "error":
            QMessageBox.warning(self, "Profile Manager", str(data.get("message") or f"Backend exited with code {rc}"))
            return
        if mode == "list":
            self._render_profiles(data)
        elif mode == "compare":
            self._render_compare(data)
        elif mode == "export":
            QMessageBox.information(self, "Profile exported", f"Exported {data.get('profile')} to:\n{data.get('output')}")
        elif mode == "import-preview":
            pending = self.pending_import or {}
            msg = (
                f"Import as '{data.get('profile')}'?\n\n"
                f"Matched staged mods: {data.get('matched_count', 0)}\n"
                f"Missing referenced mods: {data.get('missing_count', 0)}\n"
                f"Skipped preferences: {data.get('skipped_count', 0)}\n\n"
                "Local mods not present in the exported profile will be disabled in the imported profile."
            )
            overwrite = bool(pending.get("overwrite"))
            if data.get("already_exists") and not overwrite:
                if QMessageBox.question(
                    self, "Overwrite imported profile",
                    msg + f"\n\nProfile '{data.get('profile')}' already exists. Replace it?"
                ) != QMessageBox.StandardButton.Yes:
                    self.pending_import = None
                    return
                overwrite = True
            elif QMessageBox.question(self, "Import profile preview", msg) != QMessageBox.StandardButton.Yes:
                self.pending_import = None
                return
            extra = [str(pending.get("path") or "")]
            if pending.get("name"):
                extra += ["--name", str(pending["name"])]
            if overwrite:
                extra.append("--overwrite")
            self._start("import", extra)
        elif mode == "import":
            self.pending_import = None
            msg = (
                f"Imported profile {data.get('profile')}.\n"
                f"Matched {data.get('matched_count', 0)} staged mod(s); "
                f"{data.get('missing_count', 0)} referenced mod(s) are not installed."
            )
            if data.get("skipped_count"):
                msg += f"\nSkipped {data.get('skipped_count')} preference(s) that could not be resolved."
            QMessageBox.information(self, "Profile imported", msg)
            self.refresh()
        elif mode in {"save", "duplicate", "load"}:
            if mode == "load":
                dep = data.get("deploy") or {}
                self.status.setText(
                    f"Loaded {data.get('active_profile')} · {len(dep.get('linked') or [])} linked · "
                    f"{len(dep.get('removed') or [])} removed."
                )
            self.refresh()

    def refresh(self):
        self._start("list", quiet=True)

    @staticmethod
    def _fmt_time(value) -> str:
        try:
            return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return "—"

    def _render_profiles(self, data: dict):
        self.rows = [dict(x) for x in data.get("profiles") or []]
        active = str(data.get("active_profile") or "")
        self.table.setRowCount(len(self.rows))
        self.left.blockSignals(True); self.right.blockSignals(True)
        old_left = self.left.currentText(); old_right = self.right.currentText()
        self.left.clear(); self.right.clear()
        for r, row in enumerate(self.rows):
            name = str(row.get("name") or "")
            vals = [
                name,
                "● active" if row.get("active") else "",
                str(int(row.get("enabled") or 0)),
                str(int(row.get("total") or 0)),
                self._fmt_time(row.get("created_at")),
            ]
            for c, value in enumerate(vals):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, name)
                self.table.setItem(r, c, item)
            self.left.addItem(name); self.right.addItem(name)
            if name == active:
                self.table.selectRow(r)
        if old_left and self.left.findText(old_left) >= 0:
            self.left.setCurrentText(old_left)
        elif active:
            self.left.setCurrentText(active)
        if old_right and self.right.findText(old_right) >= 0:
            self.right.setCurrentText(old_right)
        elif self.right.count() > 1:
            self.right.setCurrentIndex(1 if self.right.itemText(0) == active else 0)
        self.left.blockSignals(False); self.right.blockSignals(False)
        self.status.setText(
            f"{len(self.rows)} saved profile(s). Active: {active or '—'}. "
            "Profile edits are persisted immediately; Save current as… creates a named snapshot."
        )
        self._selection_changed()

    def save_current(self):
        name, ok = QInputDialog.getText(self, "Save current profile", "New profile name:")
        if not ok or not name.strip():
            return
        extra = [name.strip()]
        if any(str(x.get("name")) == name.strip() for x in self.rows):
            if QMessageBox.question(
                self, "Overwrite profile", f"Profile '{name.strip()}' already exists. Replace its saved configuration?"
            ) != QMessageBox.StandardButton.Yes:
                return
            extra.append("--overwrite")
        self._start("save", extra)

    def load_selected(self):
        row = self._selected()
        if not row:
            return
        name = str(row.get("name") or "")
        if row.get("active"):
            QMessageBox.information(self, "Profile Manager", f"'{name}' is already active.")
            return
        if QMessageBox.question(
            self, "Load profile",
            f"Switch to '{name}' and rebuild the game hardlinks now?\n\nStaged files are not copied or deleted."
        ) == QMessageBox.StandardButton.Yes:
            self._start("load", [name])

    def duplicate_selected(self):
        row = self._selected()
        if not row:
            return
        source = str(row.get("name") or "")
        name, ok = QInputDialog.getText(self, "Duplicate profile", f"Duplicate '{source}' as:")
        if ok and name.strip():
            self._start("duplicate", [source, name.strip()])

    def export_selected(self):
        row = self._selected()
        if not row:
            return
        name = str(row.get("name") or "")
        default = str(Path.home() / f"{name}.ncm-profile.json")
        path, _ = QFileDialog.getSaveFileName(self, "Export profile", default, "NCM Profile (*.json);;JSON (*.json)")
        if path:
            self._start("export", [name, "--output", path])

    def import_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import profile", str(Path.home()), "NCM Profile (*.json);;JSON (*.json)")
        if not path:
            return
        name, ok = QInputDialog.getText(
            self, "Import profile", "Optional local profile name (leave blank to keep exported name):"
        )
        if not ok:
            return
        clean_name = name.strip()
        self.pending_import = {"path": path, "name": clean_name, "overwrite": False}
        extra = [path, "--dry-run"]
        if clean_name:
            extra += ["--name", clean_name]
        self._start("import", extra, mode="import-preview")

    def compare(self):
        left = self.left.currentText().strip(); right = self.right.currentText().strip()
        if not left or not right:
            return
        self._start("compare", [left, right])

    def _render_compare(self, data: dict):
        rows = list(data.get("differences") or [])
        self.diff.setRowCount(len(rows))
        for r, row in enumerate(rows):
            vals = [row.get("category", ""), row.get("item", ""), row.get("left", ""), row.get("right", "")]
            for c, value in enumerate(vals):
                self.diff.setItem(r, c, QTableWidgetItem(str(value)))
        self.diff.resizeRowsToContents()
        if data.get("identical"):
            self.compare_status.setText(f"{data.get('left')} and {data.get('right')} are identical in managed profile state.")
        else:
            self.compare_status.setText(
                f"{data.get('difference_count', len(rows))} difference(s) between {data.get('left')} and {data.get('right')}."
            )

    def save(self):
        self.cfg.setValue("profiles/game_root", self.game_root.text())
