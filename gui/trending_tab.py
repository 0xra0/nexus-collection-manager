"""Browse Nexus discovery/account mod feeds with direct install and account actions."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QGroupBox, QScrollArea, QFrame, QButtonGroup, QRadioButton,
    QMenu, QProgressBar,
)

from .config import DEF_GAME_ROOT
from .runner import ProcRunner, backend
from .widgets import BrowseEdit
from .mod_detail import ModDetailDialog
from .nexus_credentials import bind_api_key_edit

PREFIX = "[trending-json] "
DOWNLOAD_PREFIX = "[downloads-json] "
ACCOUNT_PREFIX = "[nexus-account] "


def _date(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d") if int(ts or 0) else ""
    except (ValueError, TypeError, OSError):
        return ""


class DiscoveryCard(QFrame):
    install_requested = Signal(str)
    account_requested = Signal(str, str)
    detail_requested = Signal(str)
    open_requested = Signal(str)

    def __init__(self, mod: dict, parent=None):
        super().__init__(parent)
        self.mod = dict(mod)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

        self.title = QLabel(); self.title.setTextFormat(Qt.TextFormat.PlainText)
        f = self.title.font(); f.setBold(True); f.setPointSize(f.pointSize() + 1); self.title.setFont(f)
        self.meta = QLabel(); self.meta.setTextFormat(Qt.TextFormat.PlainText); self.meta.setProperty("muted", True)
        self.summary = QLabel(); self.summary.setTextFormat(Qt.TextFormat.PlainText); self.summary.setWordWrap(True)
        self.summary.setMaximumHeight(56)

        self.install_btn = QPushButton("Install")
        self.install_btn.setProperty("accent", True)
        self.endorse_btn = QPushButton()
        self.track_btn = QPushButton()
        self.detail_btn = QPushButton("Details")
        self.open_btn = QPushButton("Nexus")
        self.install_btn.clicked.connect(lambda: self.install_requested.emit(str(self.mod["mod_id"])))
        self.endorse_btn.clicked.connect(self._endorse)
        self.track_btn.clicked.connect(self._track)
        self.detail_btn.clicked.connect(lambda: self.detail_requested.emit(str(self.mod["mod_id"])))
        self.open_btn.clicked.connect(lambda: self.open_requested.emit(str(self.mod["mod_id"])))

        actions = QHBoxLayout()
        for w in (self.install_btn, self.endorse_btn, self.track_btn, self.detail_btn, self.open_btn):
            actions.addWidget(w)
        actions.addStretch(1)
        lay = QVBoxLayout(self); lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(self.title); lay.addWidget(self.meta); lay.addWidget(self.summary); lay.addLayout(actions)
        self.refresh(mod)

    def refresh(self, mod: dict):
        self.mod.update(mod)
        mid = self.mod.get("mod_id")
        self.title.setText(str(self.mod.get("name") or f"Nexus mod #{mid}"))
        bits = [f"Nexus #{mid}"]
        if self.mod.get("author"): bits.append(f"by {self.mod['author']}")
        if self.mod.get("version"): bits.append(f"v{self.mod['version']}")
        if self.mod.get("endorsements") is not None: bits.append(f"{int(self.mod.get('endorsements') or 0):,} endorsements")
        if self.mod.get("downloads") is not None: bits.append(f"{int(self.mod.get('downloads') or 0):,} downloads")
        when = _date(self.mod.get("updated_timestamp"))
        if when: bits.append(f"updated {when}")
        self.meta.setText(" · ".join(bits))
        self.summary.setText(str(self.mod.get("summary") or "No summary available."))
        endorsed = self.mod.get("endorsed")
        tracked = self.mod.get("tracked")
        self.endorse_btn.setText("♥ Endorsed" if endorsed is True else "♡ Endorse")
        self.track_btn.setText("★ Tracking" if tracked is True else "☆ Track")
        self.install_btn.setToolTip(
            "Premium: download + install the primary/newest MAIN file immediately using FOMOD defaults and safe conflict skipping. "
            "Free accounts are sent to Nexus for the required Download-with-Manager authorization."
        )

    def set_busy(self, busy: bool):
        for w in (self.install_btn, self.endorse_btn, self.track_btn, self.detail_btn, self.open_btn):
            w.setEnabled(not busy)

    def _endorse(self):
        self.account_requested.emit(str(self.mod["mod_id"]), "unendorse" if self.mod.get("endorsed") is True else "endorse")

    def _track(self):
        self.account_requested.emit(str(self.mod["mod_id"]), "untrack" if self.mod.get("tracked") is True else "track")

    def _menu(self, pos):
        menu = QMenu(self)
        install = menu.addAction("Install")
        endorse = menu.addAction("Remove endorsement" if self.mod.get("endorsed") else "Endorse")
        track = menu.addAction("Stop tracking" if self.mod.get("tracked") else "Track")
        details = menu.addAction("Mod details…")
        menu.addSeparator(); open_page = menu.addAction("Open Nexus page")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == install: self.install_requested.emit(str(self.mod["mod_id"]))
        elif chosen == endorse: self._endorse()
        elif chosen == track: self._track()
        elif chosen == details: self.detail_requested.emit(str(self.mod["mod_id"]))
        elif chosen == open_page: self.open_requested.emit(str(self.mod["mod_id"]))


class TrendingTab(QWidget):
    def __init__(self, cfg, *, fixed_feed: str | None = None, settings_prefix: str = "trend"):
        super().__init__()
        self.cfg = cfg
        self.fixed_feed = fixed_feed
        self.settings_prefix = settings_prefix
        self.runner = None
        self.mods: dict[str, dict] = {}
        self.cards: dict[str, DiscoveryCard] = {}
        self._mode = ""
        self._target = ""
        self._rollback = None
        self._result = None
        self.detail_dialog: ModDetailDialog | None = None

        self.game_root = BrowseEdit(
            cfg.value(f"{settings_prefix}/game_root", cfg.value("trend/game_root", cfg.value("dm/game_root", DEF_GAME_ROOT))),
            "dir",
        )
        self.api_key = QLineEdit()
        bind_api_key_edit(self.api_key, cfg)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Nexus Mods API key")
        form = QFormLayout(); form.addRow("Game root:", self.game_root); form.addRow("Nexus API key:", self.api_key)
        note = QLabel(
            "Discovery is live from Nexus Mods. Install uses the existing Download Manager → Archives → Smart Installer pipeline. "
            "Unattended installs use authored FOMOD defaults and Skip for existing-file conflicts."
        )
        note.setWordWrap(True); note.setProperty("muted", True); form.addRow("", note)
        box = QGroupBox("Nexus discovery"); box.setLayout(form)

        self.trending = None
        self.updated = None
        head = QHBoxLayout()
        if fixed_feed is None:
            self.trending = QRadioButton("Trending"); self.updated = QRadioButton("Recently Updated")
            self.trending.setChecked(cfg.value(f"{settings_prefix}/feed", "trending") != "updated")
            self.updated.setChecked(not self.trending.isChecked())
            group = QButtonGroup(self); group.addButton(self.trending); group.addButton(self.updated)
            head.addWidget(self.trending); head.addWidget(self.updated)
        else:
            heading = QLabel(self._feed_label())
            font = heading.font(); font.setBold(True); heading.setFont(font)
            head.addWidget(heading)
        self.refresh_btn = QPushButton("Refresh from Nexus"); self.refresh_btn.setProperty("accent", True)
        self.refresh_btn.clicked.connect(self.refresh)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.stop)
        head.addWidget(self.refresh_btn); head.addWidget(self.stop_btn); head.addStretch(1)

        self.progress = QProgressBar(); self.progress.setRange(0, 1000); self.progress.hide()
        self.status = QLabel("Ready."); self.status.setTextFormat(Qt.TextFormat.PlainText)

        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.container = QWidget(); self.cards_layout = QVBoxLayout(self.container); self.cards_layout.addStretch(1)
        self.scroll.setWidget(self.container)

        lay = QVBoxLayout(self); lay.addWidget(box); lay.addLayout(head); lay.addWidget(self.progress); lay.addWidget(self.status); lay.addWidget(self.scroll, 1)
        if self.api_key.text().strip() and self.fixed_feed is None:
            self.refresh()
        elif not self.api_key.text().strip():
            self.status.setText(f"Add a Nexus API key, then refresh {self._feed_label().lower()}.")
        else:
            self.status.setText(f"Refresh to browse {self._feed_label().lower()} from your Nexus account.")

    def _env(self):
        key = self.api_key.text().strip()
        return {"NEXUS_API_KEY": key} if key else {}

    def _feed(self):
        if self.fixed_feed:
            return self.fixed_feed
        return "updated" if self.updated and self.updated.isChecked() else "trending"

    def _feed_label(self):
        return {
            "trending": "Trending Mods",
            "updated": "Recently Updated Mods",
            "endorsed": "Endorsed Mods",
            "tracked": "Tracked Mods",
        }.get(self._feed(), "Nexus Mods")

    def _start(self, argv, mode, target="", rollback=None):
        if self.runner:
            return
        self._mode, self._target, self._rollback, self._result = mode, target, rollback, None
        self.refresh_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        if target in self.cards: self.cards[target].set_busy(True)
        if self.detail_dialog and target and self.detail_dialog._mod_id() == target:
            self.detail_dialog.set_busy(True)
        self.runner = ProcRunner(argv, extra_env=self._env())
        self.runner.line.connect(self._on_line); self.runner.finished_rc.connect(self._done); self.runner.start()

    def refresh(self):
        if self.runner:
            return
        if not self.api_key.text().strip():
            QMessageBox.information(self, "Nexus API key", "Add your Nexus Mods API key first.")
            return
        self.status.setText(f"Loading {self._feed_label().lower()} from Nexus…")
        self._start(backend("trending") + ["feed", "--feed", self._feed()], "feed")

    def install(self, mod_id: str, file_id: str = ""):
        if self.runner or mod_id not in self.mods:
            return
        root = Path(self.game_root.text()).expanduser()
        if not root.is_dir():
            QMessageBox.warning(self, "Game root", f"Game root not found: {root}")
            return
        if not self.api_key.text().strip():
            QMessageBox.information(self, "Nexus API key", "Add your Nexus Mods API key first.")
            return
        self.progress.show(); self.progress.setValue(0)
        self.status.setText(f"Preparing one-click install for Nexus mod #{mod_id}…")
        argv = backend("trending") + ["install", "--game-root", str(root), "--mod-id", mod_id]
        if file_id:
            argv += ["--file-id", str(file_id)]
        self._start(argv, "install", target=mod_id)

    def toggle(self, mod_id: str, action: str):
        if self.runner or mod_id not in self.mods:
            return
        field = "endorsed" if action in {"endorse", "unendorse"} else "tracked"
        old = self.mods[mod_id].get(field); desired = action in {"endorse", "track"}
        self.mods[mod_id][field] = desired; self.cards[mod_id].refresh(self.mods[mod_id])
        argv = backend("nexus_account") + [action, "--mod-id", mod_id]
        if field == "endorsed": argv += ["--version", str(self.mods[mod_id].get("version") or "")]
        self.status.setText(f"Updating Nexus account: {action} #{mod_id}…")
        self._start(argv, "account", target=mod_id, rollback=(field, old))

    def open_detail(self, mod_id: str):
        if self.runner or mod_id not in self.mods:
            return
        if not self.api_key.text().strip():
            QMessageBox.information(self, "Nexus API key", "Add your Nexus Mods API key first.")
            return
        self.status.setText(f"Loading full Nexus details for mod #{mod_id}…")
        self._start(backend("trending") + ["detail", "--mod-id", mod_id], "detail", target=mod_id)

    def _show_detail(self, mod_id: str, detail: dict):
        if mod_id not in self.mods:
            return
        dlg = ModDetailDialog(self.mods[mod_id], detail, self, allow_install=True)
        dlg.install_requested.connect(self.install)
        dlg.account_requested.connect(self.toggle)
        dlg.open_requested.connect(self.open_nexus)
        self.detail_dialog = dlg
        dlg.finished.connect(lambda _=0: setattr(self, "detail_dialog", None))
        dlg.show()

    def open_nexus(self, mod_id: str):
        QDesktopServices.openUrl(QUrl(f"https://www.nexusmods.com/cyberpunk2077/mods/{mod_id}"))

    def _on_line(self, text: str):
        if text.startswith(PREFIX):
            try: self._result = json.loads(text[len(PREFIX):])
            except ValueError: self._result = {"type": "error", "message": "Invalid discovery response."}
            return
        if text.startswith(ACCOUNT_PREFIX):
            try: self._result = json.loads(text[len(ACCOUNT_PREFIX):])
            except ValueError: self._result = {"type": "error", "message": "Invalid Nexus account response."}
            return
        if text.startswith(DOWNLOAD_PREFIX):
            try: data = json.loads(text[len(DOWNLOAD_PREFIX):])
            except ValueError: return
            if data.get("type") == "progress":
                frac = float(data.get("progress") or 0.0)
                self.progress.setValue(max(0, min(1000, int(frac * 1000))))
                done = int(data.get("bytes_done") or 0); total = int(data.get("total_bytes") or 0)
                self.status.setText(f"Downloading Nexus mod #{self._target}: {done / 1048576:.1f} / {total / 1048576:.1f} MiB")

    def _done(self, rc: int):
        mode, target, rollback, result = self._mode, self._target, self._rollback, self._result or {}
        self.runner = None; self.refresh_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        if target in self.cards: self.cards[target].set_busy(False)
        if self.detail_dialog and target and self.detail_dialog._mod_id() == target:
            self.detail_dialog.set_busy(False)
        if mode == "install": self.progress.hide()
        if rc != 0 or result.get("type") == "error":
            if rollback and target in self.mods:
                field, old = rollback; self.mods[target][field] = old; self.cards[target].refresh(self.mods[target])
            msg = result.get("message") or f"Nexus action failed (exit {rc})."
            self.status.setText(msg); QMessageBox.warning(self, "Nexus Mods", msg); return

        if result.get("type") == "feed":
            self._apply_feed(result.get("items") or [])
            label = {
                "updated": "Recently Updated",
                "endorsed": "Endorsed",
                "tracked": "Tracked",
            }.get(result.get("feed"), "Trending")
            warnings = result.get("warnings") or []
            suffix = f" · {len(warnings)} metadata warning(s)" if warnings else ""
            self.status.setText(f"{label}: {len(self.mods)} Nexus mod(s){suffix}.")
        elif result.get("type") == "state":
            mid = str(result.get("mod_id") or target)
            action = str(result.get("action") or "account update")
            if mid in self.mods:
                if "endorsed" in result: self.mods[mid]["endorsed"] = bool(result["endorsed"])
                if "tracked" in result: self.mods[mid]["tracked"] = bool(result["tracked"])
                if (self.fixed_feed == "endorsed" and action == "unendorse") or (self.fixed_feed == "tracked" and action == "untrack"):
                    self._remove_mod(mid)
                elif mid in self.cards:
                    self.cards[mid].refresh(self.mods[mid])
            if self.detail_dialog and self.detail_dialog._mod_id() == mid:
                merged = dict(self.detail_dialog.detail)
                if "endorsed" in result: merged["endorsed"] = bool(result["endorsed"])
                if "tracked" in result: merged["tracked"] = bool(result["tracked"])
                self.detail_dialog.refresh(self.mods.get(mid, {}), merged)
            self.status.setText(f"Nexus confirmed {action} for mod #{mid}.")
        elif result.get("type") == "detail":
            mid = str(result.get("mod_id") or target)
            if mid in self.mods:
                if result.get("endorsed") is not None: self.mods[mid]["endorsed"] = bool(result.get("endorsed"))
                if result.get("tracked") is not None: self.mods[mid]["tracked"] = bool(result.get("tracked"))
                if mid in self.cards: self.cards[mid].refresh(self.mods[mid])
                self._show_detail(mid, result)
                self.status.setText(f"Loaded full details for Nexus mod #{mid}.")
        elif result.get("type") == "requires-nxm":
            url = str(result.get("nexus_url") or "")
            self.status.setText("Free account: Nexus requires Download with Manager authorization.")
            if QMessageBox.question(
                self, "Download with Manager required",
                "Nexus free accounts require a fresh Download with Manager authorization. Open this mod's Files page now?\n\n"
                "After clicking Download with Manager there, the registered NXM handler will send the archive to Downloads.",
            ) == QMessageBox.StandardButton.Yes and url:
                QDesktopServices.openUrl(QUrl(url))
        elif result.get("type") == "installed":
            install = result.get("install") or {}
            self.progress.setValue(1000)
            self.status.setText(f"Installed {install.get('mod_name') or self.mods.get(target, {}).get('name') or 'mod'} successfully.")
            QMessageBox.information(self, "Installed", self.status.text())

    def _apply_feed(self, rows):
        self.mods = {str(x.get("mod_id")): dict(x) for x in rows if x.get("mod_id")}
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0); w = item.widget()
            if w: w.deleteLater()
        self.cards = {}
        for mid, mod in self.mods.items():
            card = DiscoveryCard(mod)
            card.install_requested.connect(self.install); card.account_requested.connect(self.toggle)
            card.detail_requested.connect(self.open_detail); card.open_requested.connect(self.open_nexus)
            self.cards[mid] = card; self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)

    def _remove_mod(self, mod_id: str):
        card = self.cards.pop(mod_id, None)
        self.mods.pop(mod_id, None)
        if card is not None:
            self.cards_layout.removeWidget(card)
            card.deleteLater()

    def stop(self):
        if self.runner:
            self.runner.stop(); self.status.setText("Stopping…")

    def save(self):
        self.cfg.setValue(f"{self.settings_prefix}/game_root", self.game_root.text())
        if self.fixed_feed is None:
            self.cfg.setValue(f"{self.settings_prefix}/feed", self._feed())
