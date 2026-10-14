"""Archive library browser and orphan cleanup for downloaded mod packages."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QLabel, QPushButton,
    QMessageBox, QInputDialog, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView,
)

from .config import DEF_GAME_ROOT
from .runner import ProcRunner, backend, fmt_argv
from .widgets import BrowseEdit, LogView, safe_horizontal_header

PREFIX = "[archives-json] "


def _human_bytes(value: int) -> str:
    n = float(value or 0)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if n < 1024 or unit == units[-1]:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


class ArchiveTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self.mode = ""
        self.result = None
        self.rows: list[dict] = []

        self.game_root = BrowseEdit(
            cfg.value("archives/game_root", cfg.value("links/game_root", cfg.value("in/game_root", DEF_GAME_ROOT))),
            "dir",
        )
        self.library = QLabel("<game>/downloaded_mods/archives")
        self.library.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form = QFormLayout(); form.addRow("Game root:", self.game_root); form.addRow("Archive library:", self.library)
        box = QGroupBox("Downloaded archive staging")
        box.setLayout(form)

        self.refresh_btn = QPushButton("Refresh")
        self.import_btn = QPushButton("Import archive…")
        self.contents_btn = QPushButton("Browse contents")
        self.link_btn = QPushButton("Link Nexus…")
        self.unlink_btn = QPushButton("Unlink Nexus")
        self.open_btn = QPushButton("Open Nexus")
        self.delete_btn = QPushButton("Delete archive…")
        self.cleanup_btn = QPushButton("Clean orphans…")
        self.cleanup_btn.setProperty("accent", True)
        self.refresh_btn.clicked.connect(self.refresh)
        self.import_btn.clicked.connect(self.import_archive)
        self.contents_btn.clicked.connect(self.browse_contents)
        self.link_btn.clicked.connect(self.link_nexus)
        self.unlink_btn.clicked.connect(self.unlink_nexus)
        self.open_btn.clicked.connect(self.open_nexus)
        self.delete_btn.clicked.connect(self.delete_archive)
        self.cleanup_btn.clicked.connect(self.preview_cleanup)
        controls = QHBoxLayout()
        for b in (self.refresh_btn, self.import_btn, self.contents_btn, self.link_btn,
                  self.unlink_btn, self.open_btn, self.delete_btn, self.cleanup_btn):
            controls.addWidget(b)
        controls.addStretch(1)

        self.status = QLabel("Archive library not loaded yet.")
        self.status.setWordWrap(True)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Archive", "Size", "Installed by", "Nexus", "Status", "SHA-256", "Original source"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.doubleClicked.connect(lambda _idx: self.browse_contents())
        h = safe_horizontal_header(self.table)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        archive_box = QGroupBox("Managed package archives")
        av = QVBoxLayout(archive_box); av.addWidget(self.status); av.addWidget(self.table)

        self.content_status = QLabel("Select an archive and choose Browse contents.")
        self.content_status.setWordWrap(True)
        self.contents = QTableWidget(0, 4)
        self.contents.setHorizontalHeaderLabels(["Entry", "Size", "Type", "Safety"])
        self.contents.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        ch = safe_horizontal_header(self.contents)
        ch.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        ch.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        ch.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        ch.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        content_box = QGroupBox("Archive contents — metadata only, no extraction")
        cv = QVBoxLayout(content_box); cv.addWidget(self.content_status); cv.addWidget(self.contents)

        self.log = LogView(); self.log.setMaximumHeight(120)
        lay = QVBoxLayout(self)
        lay.addWidget(box)
        lay.addLayout(controls)
        lay.addWidget(archive_box, 3)
        lay.addWidget(content_box, 2)
        lay.addWidget(self.log)

        self.game_root.edit.textChanged.connect(self._update_library)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self._update_library(); self._selection_changed()

    def _root(self) -> Path:
        return Path(self.game_root.text()).expanduser()

    def _update_library(self):
        self.library.setText(str(self._root() / "downloaded_mods" / "archives"))

    def _selected(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.rows):
            return None
        return self.rows[row]

    def _selection_changed(self):
        row = self._selected()
        has = row is not None
        self.contents_btn.setEnabled(has)
        self.link_btn.setEnabled(has)
        self.unlink_btn.setEnabled(has and bool(row.get("nexus_mod_id")))
        self.open_btn.setEnabled(has and bool(row.get("nexus_url")))
        self.delete_btn.setEnabled(has)

    def _argv(self, command: str, extra: list[str] | None = None) -> list[str]:
        return backend("archives") + [command, "--game-root", str(self._root())] + list(extra or [])

    def _start(self, command: str, extra: list[str] | None = None):
        if self.runner:
            return
        root = self._root()
        if not root.is_dir():
            QMessageBox.warning(self, "Archives", f"Game root not found:\n{root}")
            return
        argv = self._argv(command, extra)
        self.log.add("$ " + fmt_argv(argv))
        self.mode = command; self.result = None
        self._set_running(True)
        self.runner = ProcRunner(argv)
        self.runner.line.connect(self._on_line)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def _set_running(self, running: bool):
        for b in (self.refresh_btn, self.import_btn, self.contents_btn, self.link_btn,
                  self.unlink_btn, self.open_btn, self.delete_btn, self.cleanup_btn):
            b.setEnabled(not running)
        if not running:
            self._selection_changed()

    def _on_line(self, text: str):
        self.log.add(text)
        if text.startswith(PREFIX):
            try:
                self.result = json.loads(text[len(PREFIX):])
            except ValueError:
                self.result = {"type": "error", "message": "Invalid archive backend response"}

    def _on_done(self, rc: int):
        mode = self.mode; data = self.result or {}
        self.runner = None; self._set_running(False)
        if rc != 0 or data.get("type") == "error":
            QMessageBox.warning(self, "Archives", str(data.get("message") or f"Archive backend exited with code {rc}"))
            return
        if mode == "list":
            self._render_library(data)
        elif mode == "contents":
            self._render_contents(data)
        elif mode == "cleanup":
            if data.get("dry_run"):
                self._confirm_cleanup(data)
            else:
                failures = data.get("failures") or []
                msg = f"Deleted {data.get('deleted', 0)} orphan archive(s), freed {_human_bytes(int(data.get('freed_bytes') or 0))}."
                if failures:
                    msg += f"\n\n{len(failures)} item(s) could not be deleted; see log."
                QMessageBox.information(self, "Archive cleanup", msg)
                self.refresh()
        elif mode in {"import", "link", "unlink", "delete"}:
            self.refresh()

    def refresh(self):
        self._start("list")

    def _render_library(self, data: dict):
        self.rows = [dict(x) for x in data.get("items") or []]
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            installed = ", ".join(str(x.get("name") or "") for x in row.get("installed_mods") or [])
            nexus = f"#{row['nexus_mod_id']}" if row.get("nexus_mod_id") else (
                f"suggest #{row['suggested_nexus_mod_id']}" if row.get("suggested_nexus_mod_id") else "—"
            )
            vals = [
                str(row.get("file_name") or ""), _human_bytes(int(row.get("size") or 0)), installed or "—",
                nexus, str(row.get("status") or ""), str(row.get("sha256") or ""), str(row.get("original_path") or ""),
            ]
            for c, value in enumerate(vals):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, int(row.get("id") or 0))
                self.table.setItem(r, c, item)
        self.table.resizeRowsToContents()
        self.status.setText(
            f"{len(self.rows)} archive(s) · {_human_bytes(int(data.get('bytes') or 0))} · "
            f"{int(data.get('orphans') or 0)} orphan(s) · {int(data.get('missing') or 0)} missing file(s). "
            "Orphans are neither installed-source references nor Nexus-linked downloads."
        )
        self._selection_changed()

    def import_archive(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import mod archive", str(Path(self.cfg.value("dl/downloads", str(Path.home())))),
                                              "Mod archives (*.zip *.7z *.rar);;All files (*)")
        if path:
            self._start("import", [path])

    def browse_contents(self):
        row = self._selected()
        if row:
            self._start("contents", ["--archive-id", str(row["id"])])

    def _render_contents(self, data: dict):
        entries = data.get("entries") or []
        self.contents.setRowCount(len(entries))
        for r, entry in enumerate(entries):
            size = entry.get("size")
            vals = [
                str(entry.get("path") or ""), _human_bytes(int(size)) if size is not None else "—",
                str(entry.get("kind") or "file"), "unsafe path" if entry.get("unsafe") else "OK",
            ]
            for c, value in enumerate(vals):
                self.contents.setItem(r, c, QTableWidgetItem(value))
        self.contents.resizeRowsToContents()
        arc = data.get("archive") or {}
        self.content_status.setText(
            f"{arc.get('file_name', 'Archive')} · {data.get('format', '').upper()} · "
            f"showing {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
            + (" (truncated)" if data.get("truncated") else "")
            + (f" · WARNING: {data.get('unsafe_entries')} unsafe path(s)" if data.get("unsafe_entries") else "")
        )

    def link_nexus(self):
        row = self._selected()
        if not row:
            return
        initial = str(row.get("nexus_mod_id") or row.get("suggested_nexus_mod_id") or "")
        text, ok = QInputDialog.getText(
            self, "Link archive to Nexus", "Nexus mod ID or Cyberpunk 2077 Nexus URL:", text=initial,
        )
        if ok and text.strip():
            self._start("link", ["--archive-id", str(row["id"]), "--nexus-mod", text.strip()])

    def unlink_nexus(self):
        row = self._selected()
        if not row:
            return
        if QMessageBox.question(
            self, "Unlink Nexus mod",
            f"Remove the Nexus link from {row.get('file_name')}?\n\n"
            "If this archive is not associated with an installed mod, it will become eligible for orphan cleanup."
        ) == QMessageBox.StandardButton.Yes:
            self._start("unlink", ["--archive-id", str(row["id"])])

    def open_nexus(self):
        row = self._selected()
        if row and row.get("nexus_url"):
            QDesktopServices.openUrl(QUrl(str(row["nexus_url"])))

    def delete_archive(self):
        row = self._selected()
        if not row:
            return
        installed = row.get("installed_mods") or []
        msg = f"Delete raw package archive:\n\n{row.get('file_name')}\n\n"
        extra: list[str] = ["--archive-id", str(row["id"])]
        if installed:
            msg += (
                "It is recorded as the source for: " + ", ".join(str(x.get("name") or "") for x in installed) +
                "\n\nThe installed staged payload and deployed hardlinks will be preserved, but reinstalling from this package will require downloading it again."
            )
            extra.append("--force")
        else:
            msg += "This removes only the package archive; staged installed payloads are unaffected."
        if QMessageBox.question(self, "Delete archive", msg) == QMessageBox.StandardButton.Yes:
            self._start("delete", extra)

    def preview_cleanup(self):
        self._start("cleanup")

    def _confirm_cleanup(self, data: dict):
        items = data.get("items") or []
        if not items:
            QMessageBox.information(self, "Archive cleanup", "No orphaned archives were found.")
            return
        names = "\n".join(f"• {x.get('file_name')} ({_human_bytes(int(x.get('size') or 0))})" for x in items[:15])
        if len(items) > 15:
            names += f"\n… and {len(items) - 15} more"
        text = (
            f"Delete {len(items)} orphan archive(s) and free {_human_bytes(int(data.get('bytes') or 0))}?\n\n"
            f"{names}\n\nOnly archives with no installed-mod association and no Nexus link are included."
        )
        if QMessageBox.question(self, "Clean orphaned archives", text) == QMessageBox.StandardButton.Yes:
            self._start("cleanup", ["--apply"])

    def save(self):
        self.cfg.setValue("archives/game_root", self.game_root.text())
