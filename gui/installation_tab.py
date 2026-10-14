"""Preview-first Mod Installation UI with conflict resolution and FOMOD choices."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton, QGroupBox,
    QMessageBox, QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QComboBox, QSplitter, QDialog, QDialogButtonBox,
    QRadioButton, QCheckBox, QButtonGroup, QScrollArea,
)

from .config import DEF_DOWNLOADS, DEF_GAME_ROOT
from .runner import ProcRunner, PROJECT_ROOT, backend, fmt_argv
from .widgets import BrowseEdit, LogView, SizeItem, safe_horizontal_header

RESULT_PREFIX = "[install-json] "
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar"}


class FomodWizardDialog(QDialog):
    """FOMOD choice editor.

    The backend re-evaluates visibility/type conditions whenever a choice changes and the
    user clicks another group.  This preserves flag-driven scripted installers without
    embedding a second XML/dependency interpreter in the Qt process.
    """
    def __init__(self, session: str, model: dict, selections: dict, parent=None):
        super().__init__(parent)
        self.session = session
        self.model = model or {}
        self.selections = {str(k): list(v) for k, v in (selections or {}).items()}
        self._current_id = ""
        self._buttons = []

        self.setWindowTitle(f"FOMOD Installer Wizard — {self.model.get('module_name', 'Mod')}")
        self.resize(760, 520)

        self.groups = QListWidget()
        self.groups.currentRowChanged.connect(self._show_group)
        self.title = QLabel(); self.title.setStyleSheet("font-weight:600; font-size:14px")
        self.help = QLabel(); self.help.setWordWrap(True)
        self.options_box = QWidget(); self.options_layout = QVBoxLayout(self.options_box)
        self.options_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(self.options_box)

        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Installer steps")); ll.addWidget(self.groups, 1)
        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.title); rl.addWidget(self.help); rl.addWidget(scroll, 1)
        split = QSplitter(); split.addWidget(left); split.addWidget(right); split.setStretchFactor(1, 1)

        self.refresh_btn = QPushButton("Re-evaluate dependent steps")
        self.refresh_btn.clicked.connect(self._refresh_model)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        foot = QHBoxLayout(); foot.addWidget(self.refresh_btn); foot.addStretch(1); foot.addWidget(buttons)

        lay = QVBoxLayout(self); lay.addWidget(split, 1); lay.addLayout(foot)
        self._populate_groups()

    def _populate_groups(self, keep_id: str = ""):
        self.groups.blockSignals(True)
        self.groups.clear()
        target = 0
        for i, group in enumerate(self.model.get("groups") or []):
            item = QListWidgetItem(f"{group.get('step', 'Step')}  ›  {group.get('name', 'Options')}")
            item.setData(Qt.ItemDataRole.UserRole, group.get("id"))
            self.groups.addItem(item)
            if group.get("id") == keep_id:
                target = i
        self.groups.blockSignals(False)
        if self.groups.count():
            self.groups.setCurrentRow(target)
        else:
            self.title.setText("No optional choices")
            self.help.setText("This FOMOD only contains required or conditional files.")
            self._clear_options()

    def _clear_options(self):
        while self.options_layout.count():
            item = self.options_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._buttons = []

    def _group_at(self, row: int):
        groups = self.model.get("groups") or []
        return groups[row] if 0 <= row < len(groups) else None

    def _show_group(self, row: int):
        group = self._group_at(row)
        if not group:
            return
        self._current_id = str(group.get("id") or "")
        self.title.setText(str(group.get("name") or "Options"))
        gtype = str(group.get("type") or "SelectExactlyOne")
        card = {
            "SelectExactlyOne": "Choose exactly one option.",
            "SelectAtMostOne": "Choose zero or one option.",
            "SelectAtLeastOne": "Choose one or more options.",
            "SelectAll": "All usable options are required.",
            "SelectAny": "Choose any number of options.",
        }.get(gtype, gtype)
        self.help.setText(f"{group.get('step', 'Installer step')} · {card}")
        self._clear_options()
        selected = set(self.selections.get(self._current_id, group.get("selected") or []))
        exclusive = gtype in {"SelectExactlyOne", "SelectAtMostOne"}
        bg = QButtonGroup(self); bg.setExclusive(exclusive)
        self._buttons.append(bg)
        if gtype == "SelectAtMostOne":
            none_btn = QRadioButton("None")
            none_btn.setProperty("option_name", "")
            none_btn.setChecked(not selected)
            none_btn.toggled.connect(self._capture_current)
            bg.addButton(none_btn); self.options_layout.addWidget(none_btn); self._buttons.append(none_btn)
        for option in group.get("options") or []:
            name = str(option.get("name") or "Unnamed option")
            text = name
            typ = str(option.get("type") or "Optional")
            if typ != "Optional":
                text += f"  [{typ}]"
            btn = QRadioButton(text) if exclusive else QCheckBox(text)
            btn.setEnabled(bool(option.get("usable", True)) and gtype != "SelectAll" and typ != "Required")
            btn.setChecked(name in selected or typ == "Required" or (gtype == "SelectAll" and option.get("usable", True)))
            btn.setProperty("option_name", name)
            tip = str(option.get("description") or "")
            if tip:
                btn.setToolTip(tip)
            btn.toggled.connect(self._capture_current)
            if exclusive:
                bg.addButton(btn)
            self.options_layout.addWidget(btn)
            self._buttons.append(btn)

    def _capture_current(self):
        if not self._current_id:
            return
        names = []
        for obj in self._buttons:
            if isinstance(obj, (QRadioButton, QCheckBox)) and obj.isChecked():
                name = str(obj.property("option_name") or "")
                if name:
                    names.append(name)
        self.selections[self._current_id] = names

    def _query_model(self):
        argv = backend("installer") + [
            "fomod", "--session", self.session,
            "--selections", json.dumps(self.selections, separators=(",", ":")),
        ]
        env = dict(os.environ)
        old_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + old_pp if old_pp else "")
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15, env=env)
        if proc.returncode != 0:
            raise RuntimeError((proc.stdout + "\n" + proc.stderr).strip() or "FOMOD evaluation failed")
        for line in proc.stdout.splitlines():
            if line.startswith(RESULT_PREFIX):
                return json.loads(line[len(RESULT_PREFIX):]).get("fomod") or {}
        raise RuntimeError("FOMOD backend returned no model")

    def _refresh_model(self):
        self._capture_current()
        keep = self._current_id
        try:
            self.model = self._query_model()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "FOMOD", str(e))
            return
        self._populate_groups(keep)

    def _accept(self):
        self._capture_current()
        # One final evaluation validates group cardinality and captures groups made visible
        # by the last selection. If a newly visible group appears, let the user review it.
        old_ids = {g.get("id") for g in self.model.get("groups") or []}
        try:
            new_model = self._query_model()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "FOMOD", str(e)); return
        new_ids = {g.get("id") for g in new_model.get("groups") or []}
        if new_ids - old_ids:
            self.model = new_model
            self._populate_groups(next(iter(new_ids - old_ids)))
            QMessageBox.information(
                self, "FOMOD dependencies changed",
                "Your choices revealed another installer step. Review the newly visible options, then press OK again.")
            return
        self.model = new_model
        self.accept()


class InstallationTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self.mode = ""
        self.result = None
        self.session = ""
        self.preview = None
        self.selections: dict[str, list[str]] = {}
        self.resolution_boxes: dict[str, tuple[QComboBox, str]] = {}
        self._temp_args: list[str] = []

        self.downloads = BrowseEdit(cfg.value("in/downloads", cfg.value("dp/downloads", DEF_DOWNLOADS)), "dir")
        self.game_root = BrowseEdit(cfg.value("in/game_root", cfg.value("dp/game_root", DEF_GAME_ROOT)), "dir")
        form = QFormLayout(); form.addRow("Downloads folder:", self.downloads); form.addRow("Game root:", self.game_root)
        paths_box = QGroupBox("Installation paths"); paths_box.setLayout(form)

        self.refresh_btn = QPushButton("Refresh archives")
        self.preview_btn = QPushButton("Preview selected")
        self.preview_btn.setProperty("accent", True)
        self.fomod_btn = QPushButton("FOMOD wizard…"); self.fomod_btn.setEnabled(False)
        self.safe_btn = QPushButton("Skip all conflicts"); self.safe_btn.setEnabled(False)
        self.overwrite_btn = QPushButton("Overwrite conflicts"); self.overwrite_btn.setEnabled(False)
        self.install_btn = QPushButton("Install resolved plan"); self.install_btn.setProperty("accent", True); self.install_btn.setEnabled(False)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setEnabled(False)
        self.refresh_btn.clicked.connect(self.refresh_archives)
        self.preview_btn.clicked.connect(self.preview_selected)
        self.fomod_btn.clicked.connect(self.open_fomod)
        self.safe_btn.clicked.connect(lambda: self._set_conflicts("skip"))
        self.overwrite_btn.clicked.connect(lambda: self._set_conflicts("overwrite"))
        self.install_btn.clicked.connect(self.install_plan)
        self.stop_btn.clicked.connect(self.stop)
        buttons = QHBoxLayout()
        for b in (self.refresh_btn, self.preview_btn, self.fomod_btn, self.safe_btn,
                  self.overwrite_btn, self.install_btn, self.stop_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)

        self.archives = QListWidget(); self.archives.setMinimumWidth(280)
        self.archives.itemDoubleClicked.connect(lambda _item: self.preview_selected())
        arch_box = QGroupBox("Downloaded archives")
        av = QVBoxLayout(arch_box); av.addWidget(self.archives)

        self.summary = QLabel("Select an archive and preview it before installation.")
        self.summary.setWordWrap(True)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Destination", "Status", "Current owner", "Size", "Resolution"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        h = safe_horizontal_header(self.table); h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3, 4): h.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        plan_box = QGroupBox("Pre-install preview / conflicts")
        pv = QVBoxLayout(plan_box); pv.addWidget(self.summary); pv.addWidget(self.table, 1)

        split = QSplitter(); split.addWidget(arch_box); split.addWidget(plan_box); split.setStretchFactor(1, 1)

        self.installed = QTableWidget(0, 4)
        self.installed.setHorizontalHeaderLabels(["Managed mod", "State", "Files", "Missing"])
        self.installed.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.installed.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        ih = safe_horizontal_header(self.installed); ih.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3): ih.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.refresh_installed_btn = QPushButton("Refresh installed")
        self.toggle_btn = QPushButton("Enable / Disable selected")
        self.refresh_installed_btn.clicked.connect(self.refresh_installed)
        self.toggle_btn.clicked.connect(self.toggle_selected)
        mr = QHBoxLayout(); mr.addWidget(self.refresh_installed_btn); mr.addWidget(self.toggle_btn); mr.addStretch(1)
        managed_box = QGroupBox("Enable / disable managed mods")
        mv = QVBoxLayout(managed_box); mv.addLayout(mr); mv.addWidget(self.installed)

        self.log = LogView(); self.log.setMaximumHeight(130)
        lay = QVBoxLayout(self); lay.addWidget(paths_box); lay.addLayout(buttons); lay.addWidget(split, 3); lay.addWidget(managed_box, 2); lay.addWidget(self.log)
        self.refresh_archives(); self.refresh_installed(quiet=True)

    def _set_running(self, running: bool):
        for b in (self.refresh_btn, self.preview_btn, self.fomod_btn, self.safe_btn,
                  self.overwrite_btn, self.install_btn, self.refresh_installed_btn, self.toggle_btn):
            b.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        if not running:
            has_plan = bool(self.preview)
            self.fomod_btn.setEnabled(has_plan and bool((self.preview or {}).get("fomod")))
            self.safe_btn.setEnabled(has_plan)
            self.overwrite_btn.setEnabled(has_plan)
            self.install_btn.setEnabled(has_plan)

    def refresh_archives(self):
        self.archives.clear()
        root = Path(self.downloads.text()).expanduser()
        if not root.is_dir():
            return
        files = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in ARCHIVE_SUFFIXES]
        files.sort(key=lambda p: (-p.stat().st_mtime_ns, p.name.casefold()))
        for p in files:
            item = QListWidgetItem(p.name); item.setData(Qt.ItemDataRole.UserRole, str(p)); self.archives.addItem(item)

    def _selected_archive(self):
        item = self.archives.currentItem()
        return Path(str(item.data(Qt.ItemDataRole.UserRole))) if item else None

    def _start(self, argv: list[str], mode: str):
        if self.runner:
            return
        self.mode = mode; self.result = None
        self.log.add("$ " + fmt_argv(argv))
        self._set_running(True)
        self.runner = ProcRunner(argv)
        self.runner.line.connect(self._on_line)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def preview_selected(self):
        archive = self._selected_archive()
        root = Path(self.game_root.text()).expanduser()
        if not archive:
            QMessageBox.information(self, "Install", "Select a downloaded archive first."); return
        if not root.is_dir():
            QMessageBox.warning(self, "Missing", f"Game root not found:\n{root}"); return
        self.selections = {}; self.preview = None; self.session = ""; self.table.setRowCount(0)
        self.summary.setText(f"Preparing preview for {archive.name}…")
        self._start(backend("installer") + ["preview", str(archive), "--game-root", str(root)], "preview")

    def _on_line(self, text: str):
        self.log.add(text)
        if text.startswith(RESULT_PREFIX):
            try: self.result = json.loads(text[len(RESULT_PREFIX):])
            except ValueError: self.result = {"error": "Invalid installer response"}

    def _on_done(self, rc: int):
        mode, result = self.mode, self.result or {}
        self.runner = None; self._set_running(False)
        for path in self._temp_args:
            try: Path(path).unlink(missing_ok=True)
            except OSError: pass
        self._temp_args.clear()
        if rc != 0 or result.get("error"):
            msg = result.get("error") or f"Installer backend exited with code {rc}."
            self.summary.setText(msg)
            QMessageBox.warning(self, "Mod Installation", msg)
            return
        if mode in {"preview", "refresh-preview"}:
            self.session = str(result.get("session") or self.session)
            self.preview = result
            self._render_preview()
            if mode == "preview" and result.get("fomod"):
                self.open_fomod(auto=True)
        elif mode == "fomod-only":
            if self.preview is not None:
                self.preview["fomod"] = result.get("fomod")
            self._refresh_preview_with_choices()
            return
        elif mode == "install":
            deploy = result.get("deploy") or {}
            archive = result.get("archive") or {}
            archive_note = (f" Package retained as {archive.get('file_name')}." if archive else "")
            if result.get("archive_warning"):
                archive_note += f" Archive retention warning: {result.get('archive_warning')}"
            self.summary.setText(
                f"Staged {result.get('mod_name', 'mod')} — {result.get('owned_files', 0)} managed file(s); "
                f"{len(deploy.get('linked') or [])} hardlink(s) rebuilt, "
                f"{len(deploy.get('conflicts') or [])} external conflict(s), "
                f"{result.get('redmod_native_compile_required', 0)} REDmod compile-required file(s)." + archive_note)
            self.preview = None; self.session = ""; self.table.setRowCount(0)
            self.refresh_installed(quiet=True)
        elif mode in {"list", "toggle"}:
            self._render_installed(result.get("mods") or [])

    def _render_preview(self):
        data = self.preview or {}; rows = data.get("rows") or []
        self.table.setRowCount(len(rows)); self.resolution_boxes = {}
        counts = data.get("counts") or {}
        fomod = data.get("fomod")
        info = [f"{data.get('archive_name', data.get('mod_name', 'Archive'))}: {len(rows)} installable file(s)"]
        if data.get("conflicts"):
            info.append(f"{data['conflicts']} conflict/unmanaged existing file(s)")
        if counts.get("identical"): info.append(f"{counts['identical']} identical")
        if counts.get("pinned") or counts.get("runtime-state"):
            info.append(f"{counts.get('pinned', 0) + counts.get('runtime-state', 0)} protected")
        if fomod:
            info.append(f"FOMOD: {len(fomod.get('groups') or [])} visible option group(s)")
        if data.get("disabled"):
            info.append("disabled in active profile — installing will re-enable it")
        if data.get("migration_required"):
            info.append("LEGACY IN-PLACE INSTALL: migrate it in the Deploy tab before installing")
        self.summary.setText(" · ".join(info))
        for r, row in enumerate(rows):
            owner = ", ".join(row.get("owners") or []) or "—"
            authored = str(row.get("path", "")); projected = str(row.get("projected_path") or authored)
            shown_path = authored if projected == authored else f"{authored}  →  {projected}"
            vals = [shown_path, row.get("status", ""), owner]
            for c, value in enumerate(vals):
                item = QTableWidgetItem(str(value)); item.setToolTip(str(row.get("reason") or "")); self.table.setItem(r, c, item)
            self.table.setItem(r, 3, SizeItem(int(row.get("size") or 0)))
            allowed = row.get("allowed_actions") or []
            if set(allowed).issubset({"skip", "overwrite"}) and len(allowed) > 1:
                combo = QComboBox(); combo.addItems(["skip", "overwrite"])
                combo.setCurrentText(str(row.get("default_action") or "skip"))
                self.table.setCellWidget(r, 4, combo); self.resolution_boxes[str(row.get("path"))] = (combo, str(row.get("status") or ""))
            else:
                self.table.setItem(r, 4, QTableWidgetItem(str(row.get("default_action") or "install")))
        self.table.resizeRowsToContents()
        self.fomod_btn.setEnabled(bool(fomod)); self.safe_btn.setEnabled(True); self.overwrite_btn.setEnabled(True)
        self.install_btn.setEnabled(not bool(data.get("migration_required")))

    def _set_conflicts(self, action: str):
        for combo, status in self.resolution_boxes.values():
            if status in {"conflict", "unmanaged"}:
                combo.setCurrentText(action)

    def open_fomod(self, auto: bool = False):
        if not self.preview or not self.preview.get("fomod") or not self.session:
            return
        dlg = FomodWizardDialog(self.session, self.preview.get("fomod") or {}, self.selections, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            if auto: self.summary.setText(self.summary.text() + " · FOMOD defaults retained")
            return
        self.selections = dlg.selections
        self.summary.setText("Rebuilding preview with FOMOD selections…")
        self._start(backend("installer") + [
            "fomod", "--session", self.session,
            "--selections", json.dumps(self.selections, separators=(",", ":")),
        ], "fomod-only")
        # fomod-only gives the model, not conflicts. _on_done cannot use it as preview;
        # start a conflict refresh immediately in the completion path below via a marker.

    def _refresh_preview_with_choices(self):
        # Re-run preview cheaply would normally re-extract. Instead the backend's apply path
        # validates again; for the UI conflict table, invoke a new preview-session command.
        self._start(backend("installer") + [
            "session-preview", "--session", self.session, "--game-root", self.game_root.text(),
            "--selections", json.dumps(self.selections, separators=(",", ":")),
        ], "refresh-preview")

    def _json_file_arg(self, payload: dict, prefix: str) -> str:
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            try: os.close(fd)
            except OSError: pass
            try: Path(path).unlink(missing_ok=True)
            except OSError: pass
            raise
        self._temp_args.append(path)
        return path

    def install_plan(self):
        if not self.preview or not self.session:
            return
        resolutions = {path: combo.currentText() for path, (combo, _status) in self.resolution_boxes.items()}
        overwrite = sum(1 for v in resolutions.values() if v == "overwrite")
        skip = sum(1 for v in resolutions.values() if v == "skip")
        if QMessageBox.question(
            self, "Install mod",
            f"Install {self.preview.get('mod_name', 'this mod')} now?\n\n"
            f"Explicit existing-file decisions: {overwrite} overwrite, {skip} skip.\n"
            "Pinned and runtime-state files remain protected regardless of this choice.",
        ) != QMessageBox.StandardButton.Yes:
            return
        selections_arg = self._json_file_arg(self.selections, "ncm_fomod_")
        resolutions_arg = self._json_file_arg(resolutions, "ncm_conflicts_")
        argv = backend("installer") + [
            "apply", "--session", self.session, "--game-root", self.game_root.text(),
            "--selections", selections_arg,
            "--resolutions", resolutions_arg,
            "--default-conflict", "skip",
        ]
        self._start(argv, "install")

    def refresh_installed(self, quiet: bool = False):
        root = Path(self.game_root.text()).expanduser()
        if not root.is_dir() or self.runner:
            return
        if not quiet: self.log.add("Refreshing managed mod state…")
        self._start(backend("installer") + ["list", "--game-root", str(root)], "list")

    def _render_installed(self, mods: list[dict]):
        self.installed.setRowCount(len(mods))
        for r, mod in enumerate(mods):
            values = [mod.get("name", ""), mod.get("status", ""), str(mod.get("files", 0)), str(mod.get("missing", 0))]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setData(Qt.ItemDataRole.UserRole, mod.get("name", "")); self.installed.setItem(r, c, item)
        self.installed.resizeRowsToContents()

    def toggle_selected(self):
        row = self.installed.currentRow()
        if row < 0: QMessageBox.information(self, "Manage", "Select a managed mod first."); return
        item = self.installed.item(row, 0); state_item = self.installed.item(row, 1)
        if not item or not state_item: return
        name = str(item.data(Qt.ItemDataRole.UserRole) or item.text())
        state = state_item.text()
        command = "enable" if "disabled" in state else "disable"
        if command == "disable" and QMessageBox.question(
            self, "Disable mod",
            f"Disable '{name}' in the active profile?\n\nOnly its managed game hardlinks are removed. "
            "The canonical files stay in downloaded_mods for instant re-enable/profile switching."
        ) != QMessageBox.StandardButton.Yes:
            return
        argv = backend("installer") + [command, name, "--game-root", self.game_root.text(), "--downloads", self.downloads.text()]
        self._start(argv, "toggle")

    def stop(self):
        if self.runner: self.runner.stop(); self.stop_btn.setEnabled(False)

    def save(self):
        self.cfg.setValue("in/downloads", self.downloads.text())
        self.cfg.setValue("in/game_root", self.game_root.text())
