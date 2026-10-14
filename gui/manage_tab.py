"""Tab 3 — enable / disable deployed mods without re-extracting (nexus_core.modctl)."""
import json
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton,
    QCheckBox, QMessageBox, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QHeaderView, QGroupBox,
)

from .config import DEF_GAME_ROOT, DEF_PARK_DIR, MANIFEST_NAME, PARK_STATE_NAME
from .parsing import is_framework
from .runner import ProcRunner, backend, fmt_argv
from .widgets import BrowseEdit, LogView, safe_horizontal_header
from .dialogs import DeleteModsDialog


class ManageTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None

        self.game_root = BrowseEdit(cfg.value("mg/game_root", DEF_GAME_ROOT), "dir")
        self.park_dir  = BrowseEdit(cfg.value("mg/park_dir", DEF_PARK_DIR), "dir")
        note = QLabel("Note: nexus_core.modctl has its game-root/park-dir baked in; these "
                      "fields only drive the table below, so keep them matching.")
        note.setProperty("muted", True)
        note.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Game root:", self.game_root)
        form.addRow("Park folder:", self.park_dir)
        form.addRow("", note)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter by name…")
        self.filter.textChanged.connect(self._apply_filter)
        self.force = QCheckBox("Allow parking frameworks (--force)")

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Status", "Files", "Framework", "Mod"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = safe_horizontal_header(self.table)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for col in (0, 1, 2):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

        self.refresh_btn = QPushButton("Refresh")
        self.park_btn    = QPushButton("Park selected")
        self.unpark_btn  = QPushButton("Unpark selected")
        self.unparkall_btn = QPushButton("Unpark all")
        self.status_btn  = QPushButton("Status")
        self.delete_btn  = QPushButton("Delete selected")
        self.refresh_btn.clicked.connect(self.refresh)
        self.park_btn.clicked.connect(self.park_selected)
        self.unpark_btn.clicked.connect(self.unpark_selected)
        self.unparkall_btn.clicked.connect(self.unpark_all)
        self.status_btn.clicked.connect(self.show_status)
        self.delete_btn.clicked.connect(self.delete_selected)
        btns = QHBoxLayout()
        for b in (self.refresh_btn, self.park_btn, self.unpark_btn,
                  self.unparkall_btn, self.status_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self.delete_btn)     # destructive action, set apart on the right

        self.count_lbl = QLabel("—")
        self.log = LogView()
        self.log.setMaximumHeight(160)

        lay = QVBoxLayout(self)
        box = QGroupBox("Enable / disable deployed mods (nexus_core.modctl park/unpark)")
        box.setLayout(form)
        lay.addWidget(box)
        frow = QHBoxLayout()
        frow.addWidget(self.filter, 1)
        frow.addWidget(self.force)
        lay.addLayout(frow)
        lay.addWidget(self.table, 1)
        lay.addLayout(btns)
        lay.addWidget(self.count_lbl)
        lay.addWidget(self.log)

        self._rows = []   # (name, filecount, parked_bool, framework_bool)
        self.refresh()

    # -- data --
    def _load(self):
        manifest = Path(self.game_root.text()) / MANIFEST_NAME
        parked_path = Path(self.park_dir.text()) / PARK_STATE_NAME
        mods, parked = {}, {}
        try:
            mods = json.loads(manifest.read_text()).get("mods", {})
        except (OSError, ValueError):
            pass
        try:
            parked = json.loads(parked_path.read_text())
        except (OSError, ValueError):
            pass
        rows = []
        for name in sorted(set(mods) | set(parked), key=str.lower):
            files = mods.get(name) or parked.get(name) or []
            rows.append((name, len(files), name in parked, is_framework(name)))
        return rows

    def refresh(self):
        self._rows = self._load()
        self._render()

    def _render(self):
        needle = self.filter.text().lower().strip()
        shown = [r for r in self._rows if needle in r[0].lower()]
        self.table.setRowCount(len(shown))
        for i, (name, nfiles, is_parked, fw) in enumerate(shown):
            parked_flag = "Parked" if is_parked else "Active"
            status = QTableWidgetItem(parked_flag)
            self.table.setItem(i, 0, status)
            self.table.setItem(i, 1, QTableWidgetItem(str(nfiles)))
            self.table.setItem(i, 2, QTableWidgetItem("FRAMEWORK" if fw else ""))
            self.table.setItem(i, 3, QTableWidgetItem(name))
        tot_a = sum(1 for r in self._rows if not r[2])
        tot_p = sum(1 for r in self._rows if r[2])
        self.count_lbl.setText(
            f"{len(shown)} shown · {tot_a} active · {tot_p} parked "
            f"(of {len(self._rows)} total)")

    def _apply_filter(self):
        self._render()

    def _selected_names(self, want_parked):
        names = []
        for item in self.table.selectedItems():
            if item.column() != 3:
                continue
            name = item.text()
            row = next((r for r in self._rows if r[0] == name), None)
            if row and row[2] == want_parked:
                names.append(name)
        return names

    # -- actions --
    def park_selected(self):
        names = self._selected_names(want_parked=False)
        if not names:
            QMessageBox.information(self, "Park", "Select one or more Active mods first.")
            return
        # Feed exact names via a temp file so a substring can't catch the wrong mod.
        tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "nexus_park_names.txt"
        try:
            tmp.write_text("\n".join(names))
        except OSError as e:
            QMessageBox.warning(self, "Park", f"Could not write name list: {e}")
            return
        argv = backend("modctl") + ["park", "--from-file", str(tmp)]
        if self.force.isChecked():
            argv.append("--force")
        self._run(argv, f"Parking {len(names)} mod(s)…")

    def unpark_selected(self):
        names = self._selected_names(want_parked=True)
        if not names:
            QMessageBox.information(self, "Unpark", "Select one or more Parked mods first.")
            return
        self._run(backend("modctl") + ["unpark", *names],
                  f"Unparking {len(names)} mod(s)…")

    def unpark_all(self):
        if QMessageBox.question(self, "Unpark all", "Restore every parked mod?") \
                != QMessageBox.StandardButton.Yes:
            return
        self._run(backend("modctl") + ["unpark", "--all"], "Unparking all…")

    def show_status(self):
        self._run(backend("modctl") + ["status"], "modctl status…")

    def _selected_rows(self):
        """Every _rows entry whose Mod cell is selected (regardless of park state)."""
        names = {item.text() for item in self.table.selectedItems() if item.column() == 3}
        return [r for r in self._rows if r[0] in names]

    def delete_selected(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "Delete", "Select one or more mods to delete.")
            return
        # (name, filecount, framework?) for active mods; parked mods can't be deleted
        # here — their files live in the park folder, not the game folder.
        active = [(name, n, fw) for name, n, is_parked, fw in rows if not is_parked]
        parked = [name for name, _n, is_parked, _fw in rows if is_parked]
        if not active:
            QMessageBox.information(
                self, "Delete",
                "Only parked mods are selected. Unpark them first — their files are in "
                "the park folder, so deleting from the game folder would do nothing.")
            return
        dlg = DeleteModsDialog(self, active, parked, self.game_root.text())
        dlg.exec()
        if not dlg.confirmed:
            return
        # Exact names via a temp file so a substring can never catch the wrong mod.
        tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "nexus_delete_names.txt"
        try:
            tmp.write_text("\n".join(dlg.names))
        except OSError as e:
            QMessageBox.warning(self, "Delete", f"Could not write name list: {e}")
            return
        argv = backend("deployer") + ["--game-root", self.game_root.text(),
                                      "--remove-from-file", str(tmp)]
        if dlg.purge:
            argv.append("--purge")
        if dlg.dry_run:
            argv.append("--dry-run")
        verb = "Previewing deletion of" if dlg.dry_run else "Deleting"
        self._run(argv, f"{verb} {len(dlg.names)} mod(s)…")

    def _run(self, argv, status):
        if self.runner:
            return
        self.log.clear()
        self.log.add("$ " + fmt_argv(argv))
        self.count_lbl.setText(status)
        self._set_busy(True)
        self.runner = ProcRunner(argv)
        self.runner.line.connect(self.log.add)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def _set_busy(self, busy):
        for b in (self.refresh_btn, self.park_btn, self.unpark_btn,
                  self.unparkall_btn, self.status_btn, self.delete_btn):
            b.setEnabled(not busy)

    def _on_done(self):
        self.runner = None
        self._set_busy(False)
        self.refresh()

    def save(self):
        c = self.cfg
        c.setValue("mg/game_root", self.game_root.text())
        c.setValue("mg/park_dir", self.park_dir.text())
