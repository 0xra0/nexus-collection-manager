"""Reusable Nexus mod detail dialog used by discovery and installed-mod views."""
from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
    QHeaderView,
)

from .widgets import safe_horizontal_header


def _date(ts) -> str:
    try:
        value = int(ts or 0)
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d") if value else ""
    except (TypeError, ValueError, OSError):
        return ""


def _size(value) -> str:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    units = ("B", "KiB", "MiB", "GiB")
    f = float(n)
    for unit in units:
        if f < 1024.0 or unit == units[-1]:
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024.0
    return str(n)


def _safe_description_html(text: str) -> str:
    """Keep Nexus formatting while stripping active/embed-only elements Qt does not need."""
    import re
    value = str(text or "")
    value = re.sub(r"(?is)<(script|style|iframe|object|embed)\b.*?>.*?</\1\s*>", "", value)
    value = re.sub(r"(?is)<(script|style|iframe|object|embed)\b[^>]*/?>", "", value)
    value = re.sub(r"(?i)\s+on[a-z]+\s*=\s*([\"']).*?\1", "", value)
    return value


class ModDetailDialog(QDialog):
    install_requested = Signal(str, str)  # mod_id, optional file_id
    account_requested = Signal(str, str)  # mod_id, action
    open_requested = Signal(str)

    def __init__(self, mod: dict, detail: dict, parent=None, *, allow_install: bool = True):
        super().__init__(parent)
        self.mod = dict(mod or {})
        self.detail = dict(detail or {})
        self.allow_install = bool(allow_install)
        self._file_ids: list[str] = []
        self.resize(860, 650)

        self.title = QLabel()
        f = self.title.font(); f.setPointSize(f.pointSize() + 4); f.setBold(True); self.title.setFont(f)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.meta = QLabel(); self.meta.setTextFormat(Qt.TextFormat.PlainText); self.meta.setWordWrap(True)
        self.meta.setProperty("muted", True)
        self.warning = QLabel(); self.warning.setTextFormat(Qt.TextFormat.PlainText); self.warning.setWordWrap(True)
        self.warning.setProperty("warning", True)

        self.install_btn = QPushButton("Install")
        self.install_btn.setProperty("accent", True)
        self.endorse_btn = QPushButton()
        self.track_btn = QPushButton()
        self.open_btn = QPushButton("Open Nexus")
        self.install_btn.clicked.connect(lambda: self.install_requested.emit(self._mod_id(), ""))
        self.endorse_btn.clicked.connect(self._endorse)
        self.track_btn.clicked.connect(self._track)
        self.open_btn.clicked.connect(lambda: self.open_requested.emit(self._mod_id()))
        self.install_btn.setVisible(self.allow_install)

        actions = QHBoxLayout()
        for w in (self.install_btn, self.endorse_btn, self.track_btn, self.open_btn):
            actions.addWidget(w)
        actions.addStretch(1)

        self.tabs = QTabWidget()
        self.description = QTextBrowser(); self.description.setOpenExternalLinks(True); self.description.setReadOnly(True)
        self.tabs.addTab(self.description, "Description")

        files_page = QWidget(); files_lay = QVBoxLayout(files_page); files_lay.setContentsMargins(0, 0, 0, 0)
        self.files = QTableWidget(0, 6)
        self.files.setHorizontalHeaderLabels(["Category", "File", "Version", "Size", "Uploaded", "ID"])
        self.files.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.files.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.files.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.files.verticalHeader().setVisible(False)
        safe_horizontal_header(self.files).setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.install_file_btn = QPushButton("Install selected file")
        self.install_file_btn.setVisible(self.allow_install)
        self.install_file_btn.clicked.connect(self._install_selected)
        self.files.itemSelectionChanged.connect(self._update_file_action)
        files_lay.addWidget(self.files, 1); files_lay.addWidget(self.install_file_btn)
        self.tabs.addTab(files_page, "Files")

        self.changelog = QTextBrowser(); self.changelog.setOpenExternalLinks(True); self.changelog.setReadOnly(True)
        self.tabs.addTab(self.changelog, "Changelog")
        self.requirements = QTextBrowser(); self.requirements.setOpenExternalLinks(True); self.requirements.setReadOnly(True)
        self.tabs.addTab(self.requirements, "Requirements")
        self.dlc = QTextBrowser(); self.dlc.setReadOnly(True)
        self.tabs.addTab(self.dlc, "DLC")

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close); bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.title); lay.addWidget(self.meta); lay.addLayout(actions); lay.addWidget(self.warning)
        lay.addWidget(self.tabs, 1); lay.addWidget(bb)
        self.refresh(mod, detail)

    def _mod_id(self) -> str:
        return str(self.detail.get("mod_id") or self.mod.get("mod_id") or "")

    def refresh(self, mod: dict | None = None, detail: dict | None = None):
        if mod:
            self.mod.update(mod)
        if detail is not None:
            self.detail = dict(detail)
        metadata = self.detail.get("metadata") if isinstance(self.detail.get("metadata"), dict) else {}
        mid = self._mod_id()
        name = metadata.get("name") or self.mod.get("name") or f"Nexus mod #{mid}"
        self.setWindowTitle(str(name))
        self.title.setText(str(name))
        bits = [f"Nexus #{mid}"]
        author = metadata.get("author") or self.mod.get("author")
        version = metadata.get("version") or self.mod.get("version")
        if author: bits.append(f"by {author}")
        if version: bits.append(f"v{version}")
        endorsements = metadata.get("endorsement_count", metadata.get("endorsements", self.mod.get("endorsements")))
        downloads = metadata.get("unique_downloads", metadata.get("mod_downloads", metadata.get("downloads", self.mod.get("downloads"))))
        if endorsements is not None: bits.append(f"{int(endorsements or 0):,} endorsements")
        if downloads is not None: bits.append(f"{int(downloads or 0):,} downloads")
        updated = _date(metadata.get("updated_timestamp") or self.mod.get("updated_timestamp"))
        if updated: bits.append(f"updated {updated}")
        self.meta.setText(" · ".join(bits))

        summary = metadata.get("summary") or self.mod.get("summary") or "No summary available."
        desc = metadata.get("description") or ""
        body = f"<h3>Summary</h3><p>{html.escape(str(summary))}</p>"
        if desc:
            body += "<hr>" + _safe_description_html(str(desc))
        self.description.setHtml(body)

        self._populate_files(self.detail.get("files") or [])
        self._populate_changelog(self.detail.get("changelogs") or [])
        self._populate_requirements(self.detail.get("requirements") or [])
        self._populate_dlc(self.detail.get("dlc_requirements") or [])

        endorsed = self.detail.get("endorsed", self.mod.get("endorsed"))
        tracked = self.detail.get("tracked", self.mod.get("tracked"))
        self.endorse_btn.setText("♥ Endorsed — remove" if endorsed is True else "♡ Endorse")
        self.track_btn.setText("★ Tracking — stop" if tracked is True else "☆ Track")
        warnings = [str(x) for x in (self.detail.get("warnings") or []) if str(x)]
        self.warning.setText("Some sections could not be loaded: " + " · ".join(warnings) if warnings else "")
        self.warning.setVisible(bool(warnings))

    def _populate_files(self, rows):
        self.files.setRowCount(0); self._file_ids = []
        for row in rows:
            if not isinstance(row, dict): continue
            ridx = self.files.rowCount(); self.files.insertRow(ridx)
            fid = str(row.get("file_id") or "")
            self._file_ids.append(fid)
            vals = [
                str(row.get("category") or ""),
                str(row.get("name") or row.get("file_name") or ""),
                str(row.get("version") or ""),
                _size(row.get("size")),
                _date(row.get("uploaded_timestamp")),
                fid,
            ]
            for col, value in enumerate(vals):
                self.files.setItem(ridx, col, QTableWidgetItem(value))
        if self.files.rowCount():
            self.files.selectRow(0)
        self._update_file_action()

    def _populate_changelog(self, rows):
        if not rows:
            self.changelog.setHtml("<p>No changelog entries were returned by Nexus.</p>"); return
        chunks = []
        for row in rows:
            version = html.escape(str(row.get("version") or "Unknown version"))
            entries = row.get("entries") or []
            items = "".join(f"<li>{html.escape(str(x))}</li>" for x in entries)
            chunks.append(f"<h3>{version}</h3><ul>{items}</ul>" if items else f"<h3>{version}</h3><p>No notes.</p>")
        self.changelog.setHtml("".join(chunks))

    def _populate_requirements(self, rows):
        if not rows:
            self.requirements.setHtml("<p>No Nexus mod requirements were returned.</p>"); return
        chunks = ["<ul>"]
        for row in rows:
            name = html.escape(str(row.get("name") or (f"Mod #{row.get('mod_id')}" if row.get("mod_id") else "External requirement")))
            notes = html.escape(str(row.get("notes") or ""))
            url = html.escape(str(row.get("url") or ""), quote=True)
            label = f'<a href="{url}">{name}</a>' if url else name
            if row.get("external"): label += " <i>(external)</i>"
            chunks.append(f"<li>{label}{' — ' + notes if notes else ''}</li>")
        chunks.append("</ul>")
        self.requirements.setHtml("".join(chunks))

    def _populate_dlc(self, rows):
        if not rows:
            self.dlc.setHtml("<p>No DLC requirements were returned.</p>"); return
        chunks = ["<ul>"]
        for row in rows:
            name = html.escape(str(row.get("name") or "Unknown DLC"))
            notes = html.escape(str(row.get("notes") or ""))
            chunks.append(f"<li><b>{name}</b>{' — ' + notes if notes else ''}</li>")
        chunks.append("</ul>")
        self.dlc.setHtml("".join(chunks))

    def _update_file_action(self):
        row = self.files.currentRow()
        valid = 0 <= row < len(self._file_ids) and bool(self._file_ids[row])
        self.install_file_btn.setEnabled(self.allow_install and valid)

    def _install_selected(self):
        row = self.files.currentRow()
        if 0 <= row < len(self._file_ids) and self._file_ids[row]:
            self.install_requested.emit(self._mod_id(), self._file_ids[row])

    def _endorse(self):
        endorsed = self.detail.get("endorsed", self.mod.get("endorsed")) is True
        self.account_requested.emit(self._mod_id(), "unendorse" if endorsed else "endorse")

    def _track(self):
        tracked = self.detail.get("tracked", self.mod.get("tracked")) is True
        self.account_requested.emit(self._mod_id(), "untrack" if tracked else "track")

    def set_busy(self, busy: bool):
        for widget in (self.install_btn, self.install_file_btn, self.endorse_btn, self.track_btn, self.open_btn):
            widget.setEnabled(not busy)
        if not busy:
            self._update_file_action()
