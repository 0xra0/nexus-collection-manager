"""Auto-Correlation tab — infer Nexus identity for recursively scanned local mods."""
from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton,
    QSpinBox, QPlainTextEdit, QMessageBox, QGroupBox, QProgressBar,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QMenu, QInputDialog,
    QAbstractItemView,
)

from .config import DEF_GAME_ROOT, DEF_SCAN_PATHS
from .runner import ProcRunner, backend, fmt_argv
from .widgets import BrowseEdit, LogView, safe_horizontal_header
from .nexus_credentials import bind_api_key_edit

PREFIX = "[correlation-result] "
PROGRESS_RE = re.compile(r"^\[correlation-progress\]\s+(\S+)\s+(\d+)/(\d+)\s*(.*)$")
SUMMARY_RE = re.compile(
    r"^\[correlation\]\s+groups=(\d+)\s+matched=(\d+)\s+"
    r"unmatched=(\d+)\s+ambiguous=(\d+)\s+warnings=(\d+)\s*$"
)


def _split_values(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n]+", text) if x.strip() and not x.strip().startswith("#")]


def _parse_mod_reference(text: str) -> str | None:
    value = str(text).strip()
    if value.isdigit():
        return str(int(value))
    m = re.search(r"/mods/(\d+)", value)
    return str(int(m.group(1))) if m else None


class CorrelationTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self._mode = ""
        self._target_key = ""
        self._result = None
        self.rows: dict[str, dict] = {}

        self.game_root = BrowseEdit(cfg.value("ac/game_root", cfg.value("dp/game_root", DEF_GAME_ROOT)), "dir")
        self.downloads = BrowseEdit(cfg.value("ac/downloads", cfg.value("dp/downloads", "")), "dir")
        self.api_key = QLineEdit()
        bind_api_key_edit(self.api_key, cfg)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Nexus Mods API key (network matching tiers)")

        self.paths = QPlainTextEdit()
        self.paths.setMaximumHeight(105)
        self.paths.setPlainText(cfg.value("ac/paths", cfg.value("sc/paths", "\n".join(DEF_SCAN_PATHS))))
        self.paths.setPlaceholderText("One scan root per line; relative paths resolve under the game root.")

        self.collections = QLineEdit(cfg.value("ac/collections", cfg.value("dl/collection", "")))
        self.collections.setPlaceholderText("Collection slug or URL; comma-separate multiple collections")

        self.jobs = QSpinBox(); self.jobs.setRange(1, 32); self.jobs.setValue(int(cfg.value("ac/jobs", 4)))
        self.content_queries = QSpinBox(); self.content_queries.setRange(0, 200); self.content_queries.setValue(int(cfg.value("ac/content_queries", 30)))
        self.fuzzy_searches = QSpinBox(); self.fuzzy_searches.setRange(0, 200); self.fuzzy_searches.setValue(int(cfg.value("ac/fuzzy_searches", 30)))

        form = QFormLayout()
        form.addRow("Game root:", self.game_root)
        form.addRow("Downloads / archives:", self.downloads)
        form.addRow("Scan roots:", self.paths)
        form.addRow("Nexus API key:", self.api_key)
        form.addRow("Collection(s):", self.collections)
        limits = QHBoxLayout()
        limits.addWidget(QLabel("Hash jobs:")); limits.addWidget(self.jobs)
        limits.addSpacing(12); limits.addWidget(QLabel("Content lookups:")); limits.addWidget(self.content_queries)
        limits.addSpacing(12); limits.addWidget(QLabel("Fuzzy searches:")); limits.addWidget(self.fuzzy_searches)
        limits.addStretch(1)
        limits_widget = QWidget(); limits_widget.setLayout(limits)
        form.addRow("Limits:", limits_widget)
        note = QLabel(
            "Evidence order: filename IDs → MD5 → file-content reverse lookup → endorsed/tracked → "
            "collections → requirements → Jaccard + Jaro-Winkler. Suggestions do not change deployment ownership; "
            "Confirm, Reject and Reassign are stored as review decisions.")
        note.setWordWrap(True); note.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("", note)
        box = QGroupBox("Nexus Auto-Correlation"); box.setLayout(form)

        self.run_btn = QPushButton("Run Auto-Correlation"); self.run_btn.setProperty("accent", True)
        self.reload_btn = QPushButton("Reload saved review")
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setEnabled(False)
        self.run_btn.clicked.connect(self.run_correlation)
        self.reload_btn.clicked.connect(self.reload_saved)
        self.stop_btn.clicked.connect(self.stop)
        buttons = QHBoxLayout(); buttons.addWidget(self.run_btn); buttons.addWidget(self.reload_btn); buttons.addWidget(self.stop_btn); buttons.addStretch(1)

        self.bar = QProgressBar(); self.bar.setRange(0, 100); self.bar.setValue(0)
        self.status = QLabel("No correlation run loaded."); self.status.setTextFormat(Qt.TextFormat.PlainText)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Local group", "Nexus match", "Confidence", "Evidence", "Status", "Files", "Review"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        header = safe_horizontal_header(self.table)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in (2, 3, 4, 5, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

        self.log = LogView(); self.log.setMaximumHeight(150)

        lay = QVBoxLayout(self)
        lay.addWidget(box); lay.addLayout(buttons); lay.addWidget(self.bar); lay.addWidget(self.status)
        lay.addWidget(self.table, 1); lay.addWidget(self.log)

    def _env(self):
        key = self.api_key.text().strip()
        return {"NEXUS_API_KEY": key} if key else {}

    def _scan_paths(self):
        return _split_values(self.paths.toPlainText())

    def _collections(self):
        return _split_values(self.collections.text())

    def _set_running(self, running: bool):
        self.run_btn.setEnabled(not running)
        self.reload_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.table.setEnabled(not running)

    def _start(self, argv: list[str], mode: str, target_key: str = ""):
        if self.runner:
            return
        self._mode = mode; self._target_key = target_key; self._result = None
        self._set_running(True)
        self.log.add("$ " + fmt_argv(argv))
        self.runner = ProcRunner(argv, extra_env=self._env())
        self.runner.line.connect(self._on_line)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def run_correlation(self):
        if self.runner:
            return
        root = Path(self.game_root.text()).expanduser()
        if not root.is_dir():
            QMessageBox.warning(self, "Missing", f"Game root not found:\n{root}")
            return
        paths = self._scan_paths()
        if not paths:
            QMessageBox.warning(self, "No scan roots", "Add at least one scan root.")
            return
        self.log.clear(); self.bar.setValue(0); self.status.setText("Scanning local mods…")
        argv = backend("correlation") + [
            "run", "--game-root", str(root), "--jobs", str(self.jobs.value()),
            "--max-content-queries", str(self.content_queries.value()),
            "--max-fuzzy-searches", str(self.fuzzy_searches.value()),
        ]
        downloads = self.downloads.text().strip()
        if downloads:
            argv += ["--downloads", downloads]
        for path in paths:
            argv += ["--path", path]
        for slug in self._collections():
            argv += ["--collection", slug]
        self._start(argv, "run")

    def reload_saved(self, quiet: bool = False):
        if self.runner:
            return
        root = Path(self.game_root.text()).expanduser()
        if not root.is_dir():
            if not quiet:
                QMessageBox.warning(self, "Missing", f"Game root not found:\n{root}")
            return
        if not quiet:
            self.status.setText("Loading saved correlation review…")
        self._start(backend("correlation") + ["status", "--game-root", str(root)], "status")

    def stop(self):
        if self.runner:
            self.status.setText("Stopping…"); self.runner.stop(); self.stop_btn.setEnabled(False)

    def _on_line(self, text: str):
        self.log.add(text)
        if text.startswith(PREFIX):
            try:
                self._result = json.loads(text[len(PREFIX):])
            except ValueError:
                self._result = {"type": "error", "message": "Invalid Auto-Correlation backend response."}
            return
        m = PROGRESS_RE.match(text)
        if m:
            phase, current, total, detail = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
            # Per-tier totals vary; show determinate progress where useful without pretending tiers are equal weight.
            self.bar.setValue(int(current / total * 100) if total else 0)
            self.status.setText(f"{phase.replace('-', ' ').title()}: {detail or f'{current}/{total}'}")
            return
        m = SUMMARY_RE.match(text)
        if m:
            groups, matched, unmatched, ambiguous, warnings = map(int, m.groups())
            self.status.setText(
                f"Correlation complete — {matched}/{groups} matched, {unmatched} unmatched, "
                f"{ambiguous} ambiguous, {warnings} warning(s).")

    def _on_done(self, rc: int):
        mode, target, result = self._mode, self._target_key, self._result or {}
        runner = self.runner
        was_aborted = bool(runner and getattr(runner, "was_aborted", False))
        self.runner = None; self._set_running(False)

        kind = result.get("type")
        # SIGINT is the runner's normal graceful-stop mechanism.  Older
        # backends may still surface it as return code -2; newer ones emit the
        # structured ``cancelled`` result and return 130.  Neither is an error.
        if was_aborted or kind == "cancelled" or rc in {-2, 130} and mode == "run":
            msg = result.get("message") or "Auto-Correlation cancelled."
            self.status.setText(msg)
            self.bar.setValue(0)
            return

        if rc != 0 or kind == "error":
            msg = result.get("message") or f"Auto-Correlation failed (exit {rc})."
            self.status.setText(msg)
            if mode not in {"status"}:
                QMessageBox.warning(self, "Auto-Correlation", msg)
            return

        if kind == "run":
            # The run response stays compact; load the persisted result immediately afterward.
            self.bar.setValue(100)
            self.reload_saved(quiet=True)
            return
        if kind == "status":
            state = result.get("state") or {}
            self._apply_rows(state.get("results") or [])
            summary = state.get("summary") or {}
            warnings = state.get("warnings") or []
            if self.rows:
                self.status.setText(
                    f"Loaded {len(self.rows)} groups — {summary.get('matched', 0)} matched, "
                    f"{summary.get('unmatched', 0)} unmatched, {summary.get('ambiguous', 0)} ambiguous"
                    + (f"; {len(warnings)} warning(s)." if warnings else "."))
            else:
                self.status.setText("No saved correlation run yet.")
            return
        if kind == "review":
            row = result.get("row") or {}
            if row.get("group_key"):
                self.rows[str(row["group_key"])] = row
                self._render_table()
            self.status.setText(f"Review saved: {result.get('action', mode)}.")
            return
        if kind == "search":
            self._choose_search_result(target, result.get("mods") or [])

    def _apply_rows(self, rows):
        self.rows = {str(row.get("group_key")): dict(row) for row in rows if row.get("group_key")}
        self._render_table()

    def _render_table(self):
        ordered = sorted(self.rows.values(), key=lambda row: (row.get("status") == "unmatched", str(row.get("name", "")).casefold()))
        self.table.setRowCount(len(ordered))
        for r, row in enumerate(ordered):
            key = str(row.get("group_key"))
            match = row.get("match") or {}
            confidence = float(match.get("confidence") or 0.0)
            evidence = match.get("source") or "—"
            if match.get("evidence"):
                evidence = ", ".join(dict.fromkeys(str(x.get("source")) for x in match["evidence"] if x.get("source")))
            values = [
                str(row.get("name") or row.get("scanner_id") or key[:8]),
                (f"{match.get('name') or 'Nexus Mod'}  #{match.get('mod_id')}" if match else "—"),
                (f"{confidence * 100:.1f}%" if match else "—"),
                evidence,
                str(row.get("status") or "unmatched"),
                str(len(row.get("files") or [])),
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, key)
                if c == 1 and match:
                    tips = [str(e.get("detail") or "") for e in match.get("evidence") or [] if e.get("detail")]
                    item.setToolTip("\n".join(tips))
                self.table.setItem(r, c, item)
            actions = QWidget(); hl = QHBoxLayout(actions); hl.setContentsMargins(2, 1, 2, 1); hl.setSpacing(3)
            confirm = QPushButton("Confirm"); reject = QPushButton("Reject"); reassign = QPushButton("Reassign…")
            confirm.setEnabled(bool(match) and row.get("status") not in {"confirmed", "manual"})
            reject.setEnabled(bool(match))
            confirm.clicked.connect(lambda _=False, k=key: self.review(k, "confirm"))
            reject.clicked.connect(lambda _=False, k=key: self.review(k, "reject"))
            reassign.clicked.connect(lambda _=False, k=key: self.reassign(k))
            hl.addWidget(confirm); hl.addWidget(reject); hl.addWidget(reassign)
            self.table.setCellWidget(r, 6, actions)
        self.table.resizeRowsToContents()

    def _row_key_at(self, pos) -> str:
        item = self.table.itemAt(pos)
        if not item:
            return ""
        row = item.row()
        first = self.table.item(row, 0)
        return str(first.data(Qt.ItemDataRole.UserRole) or "") if first else ""

    def _context_menu(self, pos):
        key = self._row_key_at(pos)
        if not key or key not in self.rows or self.runner:
            return
        row = self.rows[key]; has_match = bool(row.get("match"))
        menu = QMenu(self)
        confirm = menu.addAction("Confirm current match"); confirm.setEnabled(has_match)
        reject = menu.addAction("Reject current match"); reject.setEnabled(has_match)
        reassign = menu.addAction("Reassign to Nexus mod…")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == confirm: self.review(key, "confirm")
        elif action == reject: self.review(key, "reject")
        elif action == reassign: self.reassign(key)

    def review(self, key: str, action: str, mod_id: str = ""):
        if self.runner or key not in self.rows:
            return
        root = self.game_root.text().strip()
        argv = backend("correlation") + [action, "--game-root", root, "--group-key", key]
        if mod_id:
            argv += ["--mod-id", mod_id]
        self.status.setText(f"Saving {action} decision…")
        self._start(argv, action, key)

    def reassign(self, key: str):
        if self.runner or key not in self.rows:
            return
        text, ok = QInputDialog.getText(
            self, "Reassign Nexus mod", "Nexus mod ID, /mods/<id> URL, or name to search:")
        if not ok or not text.strip():
            return
        parsed = _parse_mod_reference(text)
        if parsed:
            self.review(key, "reassign", parsed)
            return
        if not self.api_key.text().strip():
            QMessageBox.information(self, "Nexus API key", "A Nexus API key is required to search by mod name. You can still enter a numeric mod ID or Nexus URL.")
            return
        self.status.setText(f"Searching Nexus for “{text.strip()}”…")
        self._start(backend("correlation") + ["search", "--query", text.strip()], "search-reassign", key)

    def _choose_search_result(self, key: str, mods: list[dict]):
        if not mods:
            QMessageBox.information(self, "No Nexus results", "No matching Nexus mods were returned.")
            return
        labels = []
        ids = []
        for mod in mods:
            mid = str(mod.get("modId") or "")
            if not mid:
                continue
            labels.append(f"{mod.get('name') or 'Nexus Mod'} — #{mid} — {mod.get('author') or 'unknown author'}")
            ids.append(mid)
        if not labels:
            QMessageBox.information(self, "No Nexus results", "No usable Nexus mod records were returned.")
            return
        choice, ok = QInputDialog.getItem(self, "Choose Nexus mod", "Assign local group to:", labels, 0, False)
        if ok and choice in labels:
            self.review(key, "reassign", ids[labels.index(choice)])

    def save(self):
        self.cfg.setValue("ac/game_root", self.game_root.text())
        self.cfg.setValue("ac/downloads", self.downloads.text())
        self.cfg.setValue("ac/paths", self.paths.toPlainText())
        self.cfg.setValue("ac/collections", self.collections.text())
        self.cfg.setValue("ac/jobs", self.jobs.value())
        self.cfg.setValue("ac/content_queries", self.content_queries.value())
        self.cfg.setValue("ac/fuzzy_searches", self.fuzzy_searches.value())
