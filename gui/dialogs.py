"""Modal dialogs: the pre-download collection preview and the safe-delete confirmation."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QCheckBox, QPushButton,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView, QDialogButtonBox,
    QPlainTextEdit,
)

from .parsing import human_bytes
from .widgets import SizeItem, safe_horizontal_header


class CollectionPreviewDialog(QDialog):
    """Read-only listing of a collection's files, shown before committing to a
    download. 'Download this collection' hands control back to the tab."""
    def __init__(self, parent, name, files, manual):
        super().__init__(parent)
        self.setWindowTitle(f"Collection preview — {name}" if name else "Collection preview")
        self.resize(760, 580)
        self.download_requested = False

        req = [f for f in files if not f.get("optional")]
        opt = [f for f in files if f.get("optional")]
        req_bytes = sum(int(f.get("size") or 0) for f in req)
        opt_bytes = sum(int(f.get("size") or 0) for f in opt)

        summary = QLabel(
            f"<b>{len(files)}</b> file(s) &nbsp;·&nbsp; {len(req)} required "
            f"(<b>{human_bytes(req_bytes)}</b>)"
            + (f" &nbsp;·&nbsp; {len(opt)} optional ({human_bytes(opt_bytes)})" if opt else "")
            + (f" &nbsp;·&nbsp; {len(manual)} external/manual" if manual else ""))
        summary.setTextFormat(Qt.TextFormat.RichText)
        summary.setWordWrap(True)

        note = QLabel("Optional files download only when “Include optional” is ticked. "
                      "External/manual files can’t be fetched by the tool — grab them "
                      "from their mod pages.")
        note.setProperty("muted", True)
        note.setWordWrap(True)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter by name…")
        self.filter.textChanged.connect(self._render)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Mod file", "Size", "Type"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = safe_horizontal_header(self.table)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSortingEnabled(True)

        self._rows = [(f.get("fileName", "?"), int(f.get("size") or 0),
                       "optional" if f.get("optional") else "required") for f in files]
        self._rows += [(m.get("fileName", "?"), int(m.get("size") or 0), "external")
                       for m in manual]
        self._render()

        bb = QDialogButtonBox()
        dl = bb.addButton("Download this collection", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        dl.setDefault(True)
        bb.accepted.connect(self._accept_download)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(summary)
        lay.addWidget(self.filter)
        lay.addWidget(self.table, 1)
        lay.addWidget(note)
        lay.addWidget(bb)

    def _render(self):
        needle = self.filter.text().lower().strip()
        rows = [r for r in self._rows if needle in r[0].lower()]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, (name, size, typ) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(name))
            self.table.setItem(i, 1, SizeItem(size))
            self.table.setItem(i, 2, QTableWidgetItem(typ))
        self.table.setSortingEnabled(True)

    def _accept_download(self):
        self.download_requested = True
        self.accept()


class DeleteModsDialog(QDialog):
    """Confirmation for deleting deployed mods. Shows exactly what will go, flags
    frameworks (which other mods depend on), and lets the user choose recoverable
    trash vs permanent deletion, and a dry-run preview. Parked mods can't be deleted
    here (their files live outside the game folder) and are listed as skipped."""
    def __init__(self, parent, active_rows, parked_names, game_root):
        super().__init__(parent)
        self.setWindowTitle("Delete mods")
        self.resize(560, 460)
        # results the caller reads after exec()
        self.confirmed = False
        self.purge = False
        self.dry_run = False
        self.names = [name for name, _n, _fw in active_rows]

        self._frameworks = [name for name, _n, fw in active_rows if fw]
        total_files = sum(n for _name, n, _fw in active_rows)

        lay = QVBoxLayout(self)

        head = QLabel(f"<b>Delete {len(active_rows)} mod(s)</b> "
                      f"— {total_files} tracked file(s) from:<br>{game_root}")
        head.setTextFormat(Qt.TextFormat.RichText)
        head.setWordWrap(True)
        lay.addWidget(head)

        # the exact mods that will be removed
        listing = QPlainTextEdit()
        listing.setReadOnly(True)
        listing.setMaximumHeight(150)
        listing.setPlainText("\n".join(
            f"{'⚠ FRAMEWORK  ' if fw else ''}{name}   ({n} file{'s' if n != 1 else ''})"
            for name, n, fw in active_rows))
        lay.addWidget(listing)

        note = QLabel("Files also owned by a mod you keep are left in place. "
                      "Empty folders are pruned afterwards.")
        note.setProperty("muted", True)
        note.setWordWrap(True)
        lay.addWidget(note)

        if parked_names:
            skipped = QLabel(f"⏭ {len(parked_names)} selected mod(s) are <b>parked</b> and were "
                             f"skipped — unpark them first to delete: "
                             f"{', '.join(parked_names)}")
            skipped.setTextFormat(Qt.TextFormat.RichText)
            skipped.setProperty("warning", True)
            skipped.setWordWrap(True)
            lay.addWidget(skipped)

        self.permanent = QCheckBox("Permanently delete (skip the recoverable .mod_trash copy)")
        self.dry = QCheckBox("Dry run — preview only, delete nothing")
        lay.addWidget(self.permanent)
        lay.addWidget(self.dry)

        self.ack = None
        if self._frameworks:
            warn = QLabel("⚠ <b>Framework mod(s) selected.</b> Other mods depend on these; "
                          "deleting them can break your load order.")
            warn.setTextFormat(Qt.TextFormat.RichText)
            warn.setProperty("danger", True)
            warn.setWordWrap(True)
            lay.addWidget(warn)
            self.ack = QCheckBox("I understand — delete the framework(s) anyway")
            lay.addWidget(self.ack)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.del_btn = QPushButton("Delete")
        self.del_btn.setProperty("danger", True)
        self.del_btn.clicked.connect(self._accept)
        row.addWidget(cancel)
        row.addWidget(self.del_btn)
        lay.addLayout(row)

        if self.ack is not None:
            self.ack.toggled.connect(self._sync)
        self.dry.toggled.connect(self._sync)
        self._sync()

    def _sync(self):
        # A framework delete needs the ack, unless it's only a dry run.
        blocked = bool(self._frameworks) and self.ack is not None \
            and not self.ack.isChecked() and not self.dry.isChecked()
        self.del_btn.setEnabled(not blocked)
        self.del_btn.setText("Preview" if self.dry.isChecked() else "Delete")

    def _accept(self):
        self.confirmed = True
        self.purge = self.permanent.isChecked()
        self.dry_run = self.dry.isChecked()
        self.accept()
