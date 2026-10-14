"""Archive load-order editor and modlist.txt dry-run/apply UI."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QLabel, QPushButton,
    QComboBox, QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QMessageBox, QTextBrowser,
)

from .config import DEF_GAME_ROOT
from .runner import ProcRunner, backend, fmt_argv
from .widgets import BrowseEdit, LogView, safe_horizontal_header

PREFIX = "[load-order-json] "


class LoadOrderTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self.mode = ""
        self.result = None
        self.rows: list[dict] = []
        self.rules: list[dict] = []

        self.game_root = BrowseEdit(
            cfg.value("lo/game_root", cfg.value("links/game_root", cfg.value("in/game_root", DEF_GAME_ROOT))),
            "dir",
        )
        self.refresh_btn = QPushButton("Refresh")
        self.preview_btn = QPushButton("Dry-run preview")
        self.apply_btn = QPushButton("Apply + generate modlist.txt")
        self.apply_btn.setProperty("accent", True)
        self.reset_btn = QPushButton("Reset to ASCII")
        self.disable_btn = QPushButton("Stop managing modlist.txt")
        self.refresh_btn.clicked.connect(self.refresh)
        self.preview_btn.clicked.connect(self.preview)
        self.apply_btn.clicked.connect(self.apply)
        self.reset_btn.clicked.connect(self.reset_ascii)
        self.disable_btn.clicked.connect(self.disable_generation)

        form = QFormLayout()
        form.addRow("Game root:", self.game_root)
        path_box = QGroupBox("Archive load order — first row has highest priority / wins conflicts")
        path_box.setLayout(form)

        controls = QHBoxLayout()
        for b in (self.refresh_btn, self.preview_btn, self.apply_btn, self.reset_btn, self.disable_btn):
            controls.addWidget(b)
        controls.addStretch(1)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "Archive", "Mod", "Deployed filename", "Source path"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        h = safe_horizontal_header(self.table)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)

        self.top_btn = QPushButton("Top")
        self.up_btn = QPushButton("↑ Up")
        self.down_btn = QPushButton("↓ Down")
        self.bottom_btn = QPushButton("Bottom")
        self.top_btn.clicked.connect(lambda: self._move("top"))
        self.up_btn.clicked.connect(lambda: self._move("up"))
        self.down_btn.clicked.connect(lambda: self._move("down"))
        self.bottom_btn.clicked.connect(lambda: self._move("bottom"))
        move = QHBoxLayout()
        move.addWidget(self.top_btn); move.addWidget(self.up_btn); move.addWidget(self.down_btn); move.addWidget(self.bottom_btn)
        move.addStretch(1)
        archive_box = QGroupBox("Working order")
        av = QVBoxLayout(archive_box)
        av.addWidget(self.table)
        av.addLayout(move)

        self.before = QComboBox(); self.after = QComboBox()
        self.note = QLineEdit(); self.note.setPlaceholderText("Optional reason, e.g. compatibility patch wins")
        self.add_rule_btn = QPushButton("Add before → after rule")
        self.remove_rule_btn = QPushButton("Remove selected rule")
        self.add_rule_btn.clicked.connect(self.add_rule)
        self.remove_rule_btn.clicked.connect(self.remove_rule)
        rule_controls = QHBoxLayout()
        rule_controls.addWidget(QLabel("Before:")); rule_controls.addWidget(self.before, 1)
        rule_controls.addWidget(QLabel("After:")); rule_controls.addWidget(self.after, 1)
        rule_controls.addWidget(self.note, 2); rule_controls.addWidget(self.add_rule_btn)

        self.rule_table = QTableWidget(0, 3)
        self.rule_table.setHorizontalHeaderLabels(["Before (wins first)", "After", "Reason"])
        self.rule_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rule_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.rule_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        rh = safe_horizontal_header(self.rule_table)
        rh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        rh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        rh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        rule_bottom = QHBoxLayout(); rule_bottom.addWidget(self.remove_rule_btn); rule_bottom.addStretch(1)
        rules_box = QGroupBox("Preference rules — durable per profile")
        rv = QVBoxLayout(rules_box); rv.addLayout(rule_controls); rv.addWidget(self.rule_table); rv.addLayout(rule_bottom)

        self.status = QLabel("Load order not loaded yet.")
        self.status.setWordWrap(True)
        self.preview_text = QTextBrowser()
        self.preview_text.setMinimumHeight(120)
        preview_box = QGroupBox("Dry-run / modlist.txt")
        pv = QVBoxLayout(preview_box); pv.addWidget(self.status); pv.addWidget(self.preview_text)

        self.log = LogView(); self.log.setMaximumHeight(120)
        lay = QVBoxLayout(self)
        lay.addWidget(path_box)
        lay.addLayout(controls)
        lay.addWidget(archive_box, 3)
        lay.addWidget(rules_box, 2)
        lay.addWidget(preview_box, 2)
        lay.addWidget(self.log)

        self._set_action_state(False)

    def _root(self) -> Path:
        return Path(self.game_root.text()).expanduser()

    def _keys(self) -> list[str]:
        return [str(row.get("key")) for row in self.rows if row.get("key")]

    def _argv(self, command: str, extra: list[str] | None = None) -> list[str]:
        return backend("load_order") + [command, "--game-root", str(self._root())] + list(extra or [])

    def _start(self, command: str, extra: list[str] | None = None):
        if self.runner:
            return
        root = self._root()
        if not root.is_dir():
            QMessageBox.warning(self, "Load Order", f"Game root not found:\n{root}")
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
        for b in (self.refresh_btn, self.preview_btn, self.apply_btn, self.reset_btn, self.disable_btn,
                  self.top_btn, self.up_btn, self.down_btn, self.bottom_btn, self.add_rule_btn,
                  self.remove_rule_btn):
            b.setEnabled(not running)

    def _set_action_state(self, loaded: bool):
        for b in (self.preview_btn, self.apply_btn, self.reset_btn, self.disable_btn,
                  self.top_btn, self.up_btn, self.down_btn, self.bottom_btn,
                  self.add_rule_btn, self.remove_rule_btn):
            b.setEnabled(loaded)

    def _on_line(self, text: str):
        self.log.add(text)
        if text.startswith(PREFIX):
            try:
                self.result = json.loads(text[len(PREFIX):])
            except ValueError:
                self.result = {"type": "error", "message": "Invalid load-order backend response"}

    def _on_done(self, rc: int):
        mode = self.mode; data = self.result or {}
        self.runner = None; self._set_running(False)
        if rc != 0 or data.get("type") == "error":
            msg = str(data.get("message") or f"Load-order backend exited with code {rc}")
            QMessageBox.warning(self, "Load Order", msg)
            self._set_action_state(bool(self.rows))
            return
        if mode == "show":
            order = (data.get("order") or {}).get("items") or data.get("items") or []
            self.rows = [dict(x) for x in order]
            self.rules = list((data.get("order") or {}).get("rules") or data.get("rules") or [])
            self._render_rows(); self._render_rules(); self._render_preview(data)
        elif mode == "preview":
            self._render_preview(data)
        elif mode == "apply":
            modlist = data.get("modlist") or {}
            if not modlist.get("ok", True) and modlist.get("conflict"):
                answer = QMessageBox.question(
                    self, "Existing modlist.txt",
                    "The existing modlist.txt is not managed by this app.\n\n"
                    "Back it up under downloaded_mods/external_backups and replace it?"
                )
                if answer == QMessageBox.StandardButton.Yes:
                    extra = self._order_args() + ["--force-external"]
                    self._start("apply", extra)
                    return
            self._render_preview(data.get("preview") or data)
            self.refresh()
            return
        elif mode == "reset":
            modlist = data.get("modlist") or {}
            if not modlist.get("ok", True) and modlist.get("conflict"):
                answer = QMessageBox.question(
                    self, "Existing modlist.txt",
                    "ASCII reset changed the desired archive filenames, but the existing modlist.txt is not managed by this app.\n\n"
                    "Back it up and replace it with the reset order?"
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self._start("reset", ["--apply", "--force-external"])
                    return
            self.refresh(); return
        elif mode in {"rule-add", "rule-remove", "disable"}:
            self.refresh(); return
        self._set_action_state(True)

    def refresh(self):
        self._start("show")

    def _render_rows(self):
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            values = [
                str(r + 1), str(row.get("archive") or ""), str(row.get("mod") or ""),
                str(row.get("deployed_archive") or ""), str(row.get("rel_path") or ""),
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, str(row.get("key") or ""))
                self.table.setItem(r, c, item)
        self.table.resizeRowsToContents()
        mods = []
        seen = set()
        for row in self.rows:
            name = str(row.get("mod") or "")
            if name and name not in seen:
                seen.add(name); mods.append(name)
        for combo in (self.before, self.after):
            current = combo.currentText()
            combo.clear(); combo.addItems(mods)
            i = combo.findText(current)
            if i >= 0: combo.setCurrentIndex(i)
        self._set_action_state(True)

    def _render_rules(self):
        self.rule_table.setRowCount(len(self.rules))
        for r, rule in enumerate(self.rules):
            for c, key in enumerate(("before_mod", "after_mod", "note")):
                self.rule_table.setItem(r, c, QTableWidgetItem(str(rule.get(key) or "")))
        self.rule_table.resizeRowsToContents()

    def _render_preview(self, data: dict):
        profile = str(data.get("profile") or "Default")
        managed = bool(data.get("managed"))
        desired = data.get("desired") or []
        current = data.get("current") or []
        moved = data.get("moved") or []
        added = data.get("added") or []
        removed = data.get("removed") or []
        warnings = data.get("warnings") or []
        link_changes = data.get("link_changes") or []
        changed = bool(data.get("would_change"))
        self.status.setText(
            f"Profile {profile} · {len(desired)} archive(s) · modlist management {'ON' if managed else 'OFF'} · "
            f"{'changes pending' if changed else 'game modlist already matches'}"
        )
        html = ["<b>Dry-run result</b><br>"]
        if moved: html.append(f"Moved: {len(moved)}<br>")
        if added: html.append("Add: " + ", ".join(map(str, added)) + "<br>")
        if removed: html.append("Remove: " + ", ".join(map(str, removed)) + "<br>")
        if link_changes:
            html.append("Link filename changes: " + ", ".join(
                f"{x.get('from')} → {x.get('to')}" for x in link_changes
            ) + "<br>")
        if warnings: html.append("<b>Warnings:</b> " + " · ".join(map(str, warnings)) + "<br>")
        html.append("<br><b>Generated modlist.txt</b><pre>" + "\n".join(map(str, desired)) + "</pre>")
        if current:
            html.append("<b>Current game modlist.txt</b><pre>" + "\n".join(map(str, current)) + "</pre>")
        self.preview_text.setHtml("".join(html))

    def _move(self, where: str):
        r = self.table.currentRow()
        if r < 0 or not self.rows:
            return
        if where == "top": n = 0
        elif where == "bottom": n = len(self.rows) - 1
        elif where == "up": n = max(0, r - 1)
        else: n = min(len(self.rows) - 1, r + 1)
        if n == r: return
        row = self.rows.pop(r); self.rows.insert(n, row)
        self._render_rows(); self.table.selectRow(n)
        self.status.setText("Working order changed locally — use Dry-run preview, then Apply.")

    def _order_args(self) -> list[str]:
        out: list[str] = []
        for key in self._keys():
            out += ["--archive-key", key]
        return out

    def preview(self):
        self._start("preview", self._order_args())

    def apply(self):
        self._start("apply", self._order_args())

    def reset_ascii(self):
        if QMessageBox.question(
            self, "Reset archive order",
            "Clear manual archive order, legacy mod priorities, and preference rules, then return to ASCII order?\n\n"
            "This does not uninstall or disable any mod."
        ) == QMessageBox.StandardButton.Yes:
            self._start("reset", ["--apply"])

    def disable_generation(self):
        if QMessageBox.question(
            self, "Stop managing modlist.txt",
            "Disable automatic modlist.txt generation and remove the manager-owned hardlink?\n\n"
            "An externally replaced modlist.txt will never be deleted."
        ) == QMessageBox.StandardButton.Yes:
            self._start("disable")

    def add_rule(self):
        before = self.before.currentText().strip(); after = self.after.currentText().strip()
        if not before or not after:
            return
        if before == after:
            QMessageBox.information(self, "Load Order", "Choose two different mods.")
            return
        self._start("rule-add", ["--before", before, "--after", after, "--note", self.note.text().strip()])

    def remove_rule(self):
        row = self.rule_table.currentRow()
        if row < 0 or row >= len(self.rules):
            return
        rule = self.rules[row]
        self._start("rule-remove", ["--before", str(rule.get("before_mod")), "--after", str(rule.get("after_mod"))])

    def save(self):
        self.cfg.setValue("lo/game_root", self.game_root.text())
