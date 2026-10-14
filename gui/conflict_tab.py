"""Conflict inbox for staged smart-deployment mods."""
from __future__ import annotations

import html
import json
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QLabel, QPushButton,
    QLineEdit, QCheckBox, QComboBox, QMessageBox, QInputDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QTextBrowser, QMenu,
)

from .config import DEF_GAME_ROOT
from .runner import ProcRunner, backend, fmt_argv
from .widgets import BrowseEdit, LogView, safe_horizontal_header
from .nexus_credentials import bind_api_key_edit

PREFIX = "[conflict-json] "


class ConflictTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self.mode = ""
        self.result = None
        self.rows: dict[str, dict] = {}
        self.row_ids: list[str] = []
        self._after_action_scan = False

        self.game_root = BrowseEdit(
            cfg.value("cf/game_root", cfg.value("links/game_root", cfg.value("in/game_root", DEF_GAME_ROOT))),
            "dir",
        )
        self.api_key = QLineEdit()
        bind_api_key_edit(self.api_key, cfg)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Optional — enrich severity with live Nexus requirements")
        self.show_key = QCheckBox("show")
        self.show_key.toggled.connect(
            lambda yes: self.api_key.setEchoMode(QLineEdit.EchoMode.Normal if yes else QLineEdit.EchoMode.Password)
        )
        keyrow = QHBoxLayout(); keyrow.addWidget(self.api_key, 1); keyrow.addWidget(self.show_key)
        keyw = QWidget(); keyw.setLayout(keyrow)
        form = QFormLayout()
        form.addRow("Game root:", self.game_root)
        form.addRow("Nexus API key:", keyw)
        note = QLabel(
            "Scans canonical files in downloaded_mods: exact file overlaps, RED4 archive resource hashes, "
            "redscript annotations, and TweakXL keys. Nexus requirements are optional and only refine severity."
        )
        note.setWordWrap(True); note.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("", note)
        settings = QGroupBox("Multi-layer conflict engine")
        settings.setLayout(form)

        self.scan_btn = QPushButton("Scan conflicts")
        self.scan_btn.setProperty("accent", True)
        self.refresh_btn = QPushButton("Refresh inbox")
        self.disable_btn = QPushButton("Disable mod…")
        self.uninstall_btn = QPushButton("Uninstall mod…")
        self.prefer_btn = QPushButton("Set winner / load order…")
        self.scan_btn.clicked.connect(lambda: self.scan())
        self.refresh_btn.clicked.connect(self.refresh)
        self.disable_btn.clicked.connect(lambda: self._act_selected("disable"))
        self.uninstall_btn.clicked.connect(lambda: self._act_selected("uninstall"))
        self.prefer_btn.clicked.connect(lambda: self._act_selected("prefer"))
        controls = QHBoxLayout()
        for button in (self.scan_btn, self.refresh_btn, self.disable_btn, self.uninstall_btn, self.prefer_btn):
            controls.addWidget(button)
        controls.addStretch(1)

        self.filter = QComboBox()
        self.filter.addItems(["All severities", "Critical", "High", "Medium", "Low", "Info"])
        self.filter.currentIndexChanged.connect(self._render_table)
        self.open_only = QCheckBox("Open only")
        self.open_only.toggled.connect(self._render_table)
        filterrow = QHBoxLayout()
        filterrow.addWidget(QLabel("Inbox filter:")); filterrow.addWidget(self.filter)
        filterrow.addWidget(self.open_only); filterrow.addStretch(1)

        self.status = QLabel("No conflict scan loaded.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Severity", "Layer", "Conflict", "Mods", "Status", "Evidence"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.itemSelectionChanged.connect(self._render_detail)
        header = safe_horizontal_header(self.table)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)

        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        self.detail.setMinimumHeight(150)
        detail_box = QGroupBox("Conflict detail · every staged archive links to Nexus")
        detail_lay = QVBoxLayout(detail_box); detail_lay.addWidget(self.detail)

        self.log = LogView(); self.log.setMaximumHeight(120)

        lay = QVBoxLayout(self)
        lay.addWidget(settings)
        lay.addLayout(controls)
        lay.addLayout(filterrow)
        lay.addWidget(self.status)
        lay.addWidget(self.table, 2)
        lay.addWidget(detail_box, 1)
        lay.addWidget(self.log)

        self._set_action_buttons(False)

    def _root(self) -> Path:
        return Path(self.game_root.text()).expanduser()

    def _env(self) -> dict[str, str]:
        key = self.api_key.text().strip()
        return {"NEXUS_API_KEY": key} if key else {}

    def _set_running(self, running: bool):
        self.scan_btn.setEnabled(not running)
        self.refresh_btn.setEnabled(not running)
        self.table.setEnabled(not running)
        if running:
            self._set_action_buttons(False)
        else:
            self._render_detail()

    def _set_action_buttons(self, enabled: bool, kind: str = ""):
        self.disable_btn.setEnabled(enabled)
        self.uninstall_btn.setEnabled(enabled)
        self.prefer_btn.setEnabled(enabled and kind in {"file-overlap", "archive-resource"})

    def _start(self, argv: list[str], mode: str, *, quiet: bool = False):
        if self.runner:
            return
        root = self._root()
        if not root.is_dir():
            if not quiet:
                QMessageBox.warning(self, "Conflict detection", f"Game root not found:\n{root}")
            return
        self.mode = mode; self.result = None
        self._set_running(True)
        if not quiet:
            self.log.add("$ " + fmt_argv(argv))
        self.runner = ProcRunner(argv, extra_env=self._env())
        self.runner.line.connect(self._on_line)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def scan(self, quiet: bool = False):
        root = self._root()
        self.status.setText("Scanning staged files for conflicts…")
        self._start(backend("conflicts") + ["scan", "--game-root", str(root)], "scan", quiet=quiet)

    def refresh(self):
        root = self._root()
        self._start(backend("conflicts") + ["inbox", "--game-root", str(root)], "inbox")

    def _on_line(self, text: str):
        self.log.add(text)
        if text.startswith(PREFIX):
            try:
                self.result = json.loads(text[len(PREFIX):])
            except ValueError:
                self.result = {"type": "error", "message": "Invalid conflict backend response."}

    def _on_done(self, rc: int):
        mode = self.mode; data = self.result or {}
        self.runner = None; self._set_running(False)
        if rc != 0 or data.get("type") == "error":
            msg = str(data.get("message") or f"Conflict backend exited with code {rc}")
            self.status.setText(msg)
            QMessageBox.warning(self, "Conflict detection", msg)
            return
        if mode in {"scan", "inbox"}:
            self._apply_inbox(data)
            return
        if mode in {"disable", "uninstall", "prefer"}:
            self.status.setText(f"{mode.title()} applied; rescanning conflict inbox…")
            self.scan(quiet=True)

    def _apply_inbox(self, data: dict):
        items = data.get("items") or []
        self.rows = {str(x.get("id")): dict(x) for x in items if x.get("id")}
        self._render_table()
        sev = data.get("severity") or {}
        warning_count = len(data.get("warnings") or [])
        bits = [f"{len(self.rows)} active conflict(s)"]
        for key in ("critical", "high", "medium", "low"):
            if sev.get(key):
                bits.append(f"{sev[key]} {key}")
        if data.get("dependency_relations") is not None:
            bits.append(f"{data.get('dependency_relations', 0)} dependency relation(s)")
        if warning_count:
            bits.append(f"{warning_count} warning(s)")
        self.status.setText(" · ".join(bits))

    def _filtered_rows(self) -> list[dict]:
        wanted = self.filter.currentText().casefold()
        out = list(self.rows.values())
        if wanted != "all severities":
            out = [x for x in out if str(x.get("severity", "")).casefold() == wanted]
        if self.open_only.isChecked():
            out = [x for x in out if x.get("status", "open") == "open"]
        rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        return sorted(out, key=lambda x: (rank.get(str(x.get("severity")), 9), str(x.get("kind")), str(x.get("title", "")).casefold()))

    def _render_table(self):
        current = self._selected_id()
        rows = self._filtered_rows()
        self.row_ids = [str(x["id"]) for x in rows]
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            mods = ", ".join(str(m.get("name") or "") for m in row.get("mods") or [])
            evidence = row.get("dependency_context") or self._evidence_text(row)
            values = [
                str(row.get("severity", "")), str(row.get("kind", "")).replace("-", " "),
                str(row.get("title", "")), mods, str(row.get("status", "open")), evidence,
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, str(row["id"]))
                self.table.setItem(r, c, item)
            if str(row["id"]) == current:
                self.table.selectRow(r)
        self.table.resizeRowsToContents()
        self._render_detail()

    @staticmethod
    def _evidence_text(row: dict) -> str:
        kind = row.get("kind")
        if kind == "archive-resource":
            return f"{row.get('resource_count', 0)} shared resource(s)"
        if kind == "tweakxl":
            return str(row.get("tweak_key") or "")
        if kind == "redscript":
            return str((row.get("evidence") or {}).get("target") or "")
        return ", ".join(str(x) for x in row.get("paths") or [])

    def _selected_id(self) -> str:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.row_ids):
            return ""
        return self.row_ids[row]

    def _selected(self) -> dict | None:
        cid = self._selected_id()
        return self.rows.get(cid) if cid else None

    def _render_detail(self):
        row = self._selected()
        if not row or self.runner:
            self._set_action_buttons(False)
            if not row:
                self.detail.setHtml("<i>Select a conflict to inspect evidence and Nexus links.</i>")
            return
        kind = str(row.get("kind") or "")
        self._set_action_buttons(True, kind)
        parts = [
            f"<h3>{html.escape(str(row.get('title') or 'Conflict'))}</h3>",
            f"<p><b>{html.escape(str(row.get('severity') or '').upper())}</b> · {html.escape(kind)} · "
            f"status: {html.escape(str(row.get('status') or 'open'))}</p>",
            f"<p>{html.escape(str(row.get('detail') or ''))}</p>",
        ]
        if row.get("dependency_context"):
            parts.append(f"<p><b>Dependency context:</b> {html.escape(str(row['dependency_context']))}</p>")
        parts.append("<h4>Mods / archives</h4><ul>")
        for mod in row.get("mods") or []:
            state = "enabled" if mod.get("enabled") else "disabled"
            parts.append(f"<li><b>{html.escape(str(mod.get('name') or ''))}</b> ({state})<ul>")
            for arc in mod.get("archives") or []:
                name = str(arc.get("name") or mod.get("source_archive") or mod.get("name") or "archive")
                url = str(arc.get("nexus_url") or mod.get("nexus_url") or "")
                projected = str(arc.get("projected_rel") or "")
                link = f'<a href="{html.escape(url, quote=True)}">{html.escape(name)}</a>' if url else html.escape(name)
                parts.append(f"<li>{link}" + (f" — {html.escape(projected)}" if projected else "") + "</li>")
            parts.append("</ul></li>")
        parts.append("</ul>")
        if row.get("paths"):
            parts.append("<p><b>Paths:</b><br>" + "<br>".join(html.escape(str(x)) for x in row["paths"][:20]) + "</p>")
        if row.get("resources"):
            parts.append("<p><b>Shared archive resources (sample):</b><br>" + "<br>".join(html.escape(str(x)) for x in row["resources"][:24]) + "</p>")
        if row.get("tweak_key"):
            parts.append(f"<p><b>TweakXL key:</b> {html.escape(str(row['tweak_key']))} · operations: "
                         f"{html.escape(', '.join(row.get('operations') or []))}</p>")
        if row.get("annotations"):
            parts.append(f"<p><b>Annotations:</b> {html.escape(', '.join(row.get('annotations') or []))}</p>")
        if row.get("resolution"):
            parts.append(f"<p><b>Recorded resolution:</b> {html.escape(str(row['resolution']))}</p>")
        self.detail.setHtml("".join(parts))

    def _choose_mod(self, row: dict, title: str) -> str:
        names = [str(m.get("name")) for m in row.get("mods") or [] if m.get("name")]
        if not names:
            return ""
        name, ok = QInputDialog.getItem(self, title, "Mod:", names, 0, False)
        return str(name) if ok else ""

    def _act_selected(self, action: str):
        row = self._selected()
        if not row or self.runner:
            return
        if action == "prefer" and row.get("kind") not in {"file-overlap", "archive-resource"}:
            QMessageBox.information(
                self, "Conflict preference",
                "Load-order/owner preference is only a safe resolver for file overlaps and archive-resource conflicts. "
                "Redscript and TweakXL collisions require disabling/uninstalling a mod or editing the mods themselves."
            )
            return
        label = {"disable": "Disable conflicting mod", "uninstall": "Uninstall conflicting mod", "prefer": "Choose winner"}[action]
        name = self._choose_mod(row, label)
        if not name:
            return
        if action == "uninstall":
            answer = QMessageBox.question(
                self, "Uninstall staged mod",
                f"Uninstall {name!r} from managed staging?\n\nIts active hardlinks will be removed/rebuilt first, then its canonical staged payload is deleted."
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        elif action == "disable":
            answer = QMessageBox.question(self, "Disable mod", f"Disable {name!r} in the active profile and rebuild links?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        root = self._root()
        argv = backend("conflicts") + [
            action, "--game-root", str(root), "--conflict-id", str(row["id"]), "--mod", name,
        ]
        self._start(argv, action)

    def _context_menu(self, pos):
        row = self._selected()
        if not row or self.runner:
            return
        menu = QMenu(self)
        prefer = menu.addAction("Set winner / load order…")
        prefer.setEnabled(row.get("kind") in {"file-overlap", "archive-resource"})
        disable = menu.addAction("Disable mod…")
        uninstall = menu.addAction("Uninstall mod…")
        menu.addSeparator()
        nexus_actions = []
        for mod in row.get("mods") or []:
            for arc in mod.get("archives") or []:
                label = f"Open Nexus · {arc.get('name') or mod.get('name')}"
                act = menu.addAction(label)
                nexus_actions.append((act, str(arc.get("nexus_url") or mod.get("nexus_url") or "")))
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == prefer:
            self._act_selected("prefer")
        elif chosen == disable:
            self._act_selected("disable")
        elif chosen == uninstall:
            self._act_selected("uninstall")
        else:
            for act, url in nexus_actions:
                if chosen == act and url:
                    QDesktopServices.openUrl(QUrl(url)); break

    def save(self):
        self.cfg.setValue("cf/game_root", self.game_root.text())
