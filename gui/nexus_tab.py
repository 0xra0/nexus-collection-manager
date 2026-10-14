"""Installed Nexus mods with live endorse/track account state."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QGroupBox, QScrollArea, QFrame, QMenu, QDialog, QDialogButtonBox,
    QTextBrowser,
)

from .config import DEF_GAME_ROOT
from .runner import ProcRunner, backend, fmt_argv
from .widgets import BrowseEdit
from .mod_detail import ModDetailDialog as FullModDetailDialog
from .nexus_credentials import bind_api_key_edit

PREFIX = "[nexus-account] "
DETAIL_PREFIX = "[trending-json] "
DOWNLOAD_PREFIX = "[downloads-json] "


class NexusModCard(QFrame):
    action_requested = Signal(str, str)   # mod_id, endorse|unendorse|track|untrack
    detail_requested = Signal(str)

    def __init__(self, mod: dict, parent=None):
        super().__init__(parent)
        self.mod = dict(mod)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

        self.name = QLabel()
        self.name.setTextFormat(Qt.TextFormat.PlainText)
        f = self.name.font(); f.setBold(True); self.name.setFont(f)
        self.meta = QLabel()
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.meta.setProperty("muted", True)

        self.endorse_btn = QPushButton()
        self.track_btn = QPushButton()
        self.detail_btn = QPushButton("Details")
        self.endorse_btn.clicked.connect(self._endorse_clicked)
        self.track_btn.clicked.connect(self._track_clicked)
        self.detail_btn.clicked.connect(lambda: self.detail_requested.emit(str(self.mod["mod_id"])))

        buttons = QHBoxLayout()
        buttons.addWidget(self.endorse_btn)
        buttons.addWidget(self.track_btn)
        buttons.addWidget(self.detail_btn)
        buttons.addStretch(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(self.name)
        lay.addWidget(self.meta)
        lay.addLayout(buttons)
        self.refresh(mod)

    def refresh(self, mod: dict):
        self.mod.update(mod)
        self.name.setText(str(self.mod.get("name") or f"Mod {self.mod.get('mod_id', '?')}"))
        version = self.mod.get("installed_version") or "?"
        nfiles = int(self.mod.get("file_count") or 0)
        self.meta.setText(f"Nexus #{self.mod.get('mod_id')} · installed v{version} · {nfiles} managed file(s)")
        endorsed = self.mod.get("endorsed")
        tracked = self.mod.get("tracked")
        self.endorse_btn.setText("♥ Endorsed" if endorsed is True else "♡ Endorse" if endorsed is False else "♡ Endorse ?")
        self.track_btn.setText("★ Tracking" if tracked is True else "☆ Track" if tracked is False else "☆ Track ?")
        self.endorse_btn.setToolTip("Remove endorsement" if endorsed else "Endorse this mod on Nexus Mods")
        self.track_btn.setToolTip("Stop tracking" if tracked else "Track updates on Nexus Mods")

    def set_busy(self, busy: bool):
        for widget in (self.endorse_btn, self.track_btn, self.detail_btn):
            widget.setEnabled(not busy)

    def _endorse_clicked(self):
        action = "unendorse" if self.mod.get("endorsed") is True else "endorse"
        self.action_requested.emit(str(self.mod["mod_id"]), action)

    def _track_clicked(self):
        action = "untrack" if self.mod.get("tracked") is True else "track"
        self.action_requested.emit(str(self.mod["mod_id"]), action)

    def _menu(self, pos):
        menu = QMenu(self)
        endorsed = self.mod.get("endorsed") is True
        tracked = self.mod.get("tracked") is True
        endorse_action = menu.addAction("Remove endorsement" if endorsed else "Endorse on Nexus")
        track_action = menu.addAction("Stop tracking" if tracked else "Track on Nexus")
        menu.addSeparator()
        details = menu.addAction("Mod details…")
        open_page = menu.addAction("Open Nexus page")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == endorse_action:
            self.action_requested.emit(str(self.mod["mod_id"]), "unendorse" if endorsed else "endorse")
        elif chosen == track_action:
            self.action_requested.emit(str(self.mod["mod_id"]), "untrack" if tracked else "track")
        elif chosen == details:
            self.detail_requested.emit(str(self.mod["mod_id"]))
        elif chosen == open_page:
            QDesktopServices.openUrl(QUrl(f"https://www.nexusmods.com/cyberpunk2077/mods/{self.mod['mod_id']}"))


class NexusModDetailDialog(QDialog):
    action_requested = Signal(str, str)

    def __init__(self, mod: dict, metadata: dict, parent=None):
        super().__init__(parent)
        self.mod = dict(mod)
        self.metadata = dict(metadata or {})
        self.setWindowTitle(self.metadata.get("name") or self.mod.get("name") or "Nexus mod")
        self.resize(620, 460)

        self.title = QLabel()
        title_font = self.title.font(); title_font.setPointSize(title_font.pointSize() + 3); title_font.setBold(True)
        self.title.setFont(title_font)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.byline = QLabel(); self.byline.setTextFormat(Qt.TextFormat.PlainText)
        self.byline.setProperty("muted", True)
        self.summary = QTextBrowser(); self.summary.setOpenExternalLinks(True)
        self.summary.setReadOnly(True)
        self.endorse_btn = QPushButton()
        self.track_btn = QPushButton()
        self.open_btn = QPushButton("Open Nexus page")
        self.open_btn.clicked.connect(self._open_page)
        self.endorse_btn.clicked.connect(self._endorse)
        self.track_btn.clicked.connect(self._track)

        actions = QHBoxLayout()
        actions.addWidget(self.endorse_btn)
        actions.addWidget(self.track_btn)
        actions.addWidget(self.open_btn)
        actions.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.byline)
        lay.addLayout(actions)
        lay.addWidget(self.summary, 1)
        lay.addWidget(bb)
        self.refresh(mod, metadata)

    def refresh(self, mod: dict, metadata: dict | None = None):
        self.mod.update(mod)
        if metadata is not None:
            self.metadata = dict(metadata)
        name = self.metadata.get("name") or self.mod.get("name") or f"Mod {self.mod.get('mod_id')}"
        author = self.metadata.get("author") or "Unknown author"
        nexus_ver = self.metadata.get("version") or "?"
        installed = self.mod.get("installed_version") or "?"
        endorsements = self.metadata.get("endorsement_count")
        downloads = self.metadata.get("unique_downloads") or self.metadata.get("mod_downloads")
        bits = [f"Nexus #{self.mod.get('mod_id')}", f"by {author}", f"installed v{installed}", f"Nexus v{nexus_ver}"]
        if endorsements is not None:
            bits.append(f"{endorsements} endorsements")
        if downloads is not None:
            bits.append(f"{downloads} downloads")
        self.title.setText(str(name))
        self.byline.setText(" · ".join(bits))
        summary = self.metadata.get("summary") or "No Nexus summary available."
        description = self.metadata.get("description") or ""
        # QTextBrowser supports simple HTML; escape API-provided text before rendering.
        import html
        body = f"<p>{html.escape(str(summary))}</p>"
        if description:
            body += "<hr>" + str(description)
        self.summary.setHtml(body)
        self.endorse_btn.setText("♥ Endorsed — remove" if self.mod.get("endorsed") is True else "♡ Endorse")
        self.track_btn.setText("★ Tracking — stop" if self.mod.get("tracked") is True else "☆ Track")

    def set_busy(self, busy: bool):
        self.endorse_btn.setEnabled(not busy)
        self.track_btn.setEnabled(not busy)

    def _endorse(self):
        action = "unendorse" if self.mod.get("endorsed") is True else "endorse"
        self.action_requested.emit(str(self.mod["mod_id"]), action)

    def _track(self):
        action = "untrack" if self.mod.get("tracked") is True else "track"
        self.action_requested.emit(str(self.mod["mod_id"]), action)

    def _open_page(self):
        QDesktopServices.openUrl(QUrl(f"https://www.nexusmods.com/cyberpunk2077/mods/{self.mod['mod_id']}"))


class NexusTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self._mode = ""
        self._target_id = ""
        self._rollback = None
        self._result = None
        self.mods: dict[str, dict] = {}
        self.cards: dict[str, NexusModCard] = {}
        self.detail_dialog: FullModDetailDialog | None = None

        self.game_root = BrowseEdit(cfg.value("nx/game_root", cfg.value("dp/game_root", DEF_GAME_ROOT)), "dir")
        self.api_key = QLineEdit()
        bind_api_key_edit(self.api_key, cfg)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Nexus Mods API key (required for endorse/track)")
        self.api_key.setToolTip("Used only for authenticated Nexus API requests. It is passed to the backend via environment, not shown in the command log.")

        form = QFormLayout()
        form.addRow("Game root:", self.game_root)
        form.addRow("Nexus API key:", self.api_key)
        note = QLabel("Endorsements and tracking are Nexus account state. Buttons update optimistically, then verify the live API response; failures roll back immediately.")
        note.setWordWrap(True); note.setProperty("muted", True)
        form.addRow("", note)
        box = QGroupBox("Nexus account — installed mods")
        box.setLayout(form)

        self.refresh_btn = QPushButton("Sync with Nexus")
        self.refresh_btn.setProperty("accent", True)
        self.refresh_btn.clicked.connect(lambda: self.refresh(sync=True))
        self.status = QLabel("Loading installed Nexus mods…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        head = QHBoxLayout(); head.addWidget(self.refresh_btn); head.addWidget(self.status, 1)

        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.cards_layout = QVBoxLayout(self.container)
        self.cards_layout.setContentsMargins(4, 4, 4, 4)
        self.cards_layout.setSpacing(8)
        self.cards_layout.addStretch(1)
        self.scroll.setWidget(self.container)

        lay = QVBoxLayout(self)
        lay.addWidget(box)
        lay.addLayout(head)
        lay.addWidget(self.scroll, 1)

        self.refresh(sync=bool(self.api_key.text().strip()))

    def _env(self):
        return {"NEXUS_API_KEY": self.api_key.text().strip()} if self.api_key.text().strip() else {}

    def refresh(self, sync=True):
        if self.runner:
            return
        root = Path(self.game_root.text())
        if not root.exists():
            self.status.setText(f"Game root not found: {root}")
            return
        if sync and not self.api_key.text().strip():
            QMessageBox.information(self, "Nexus API key", "Add your Nexus Mods API key to sync endorsement/tracking state.")
            sync = False
        command = "sync" if sync else "inventory"
        argv = backend("nexus_account") + [command, "--game-root", str(root)]
        self._start(argv, command, target_id="")
        self.status.setText("Syncing live Nexus account state…" if sync else "Loading installed Nexus mods…")

    def _start(self, argv, mode, target_id="", rollback=None):
        self._mode = mode
        self._target_id = target_id
        self._rollback = rollback
        self._result = None
        self.refresh_btn.setEnabled(False)
        if target_id in self.cards:
            self.cards[target_id].set_busy(True)
        if self.detail_dialog and target_id and self.detail_dialog._mod_id() == target_id:
            self.detail_dialog.set_busy(True)
        self.runner = ProcRunner(argv, extra_env=self._env())
        self.runner.line.connect(self._on_line)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def _on_line(self, text):
        if text.startswith(DETAIL_PREFIX):
            try:
                self._result = json.loads(text[len(DETAIL_PREFIX):])
            except ValueError:
                self._result = {"type": "error", "message": "Invalid full-detail response from Nexus."}
            return
        if text.startswith(DOWNLOAD_PREFIX):
            try:
                data = json.loads(text[len(DOWNLOAD_PREFIX):])
            except ValueError:
                return
            if data.get("type") == "progress":
                done = int(data.get("bytes_done") or 0)
                total = int(data.get("total_bytes") or 0)
                self.status.setText(f"Downloading Nexus mod #{self._target_id}: {done / 1048576:.1f} / {total / 1048576:.1f} MiB")
            return
        if not text.startswith(PREFIX):
            return
        try:
            self._result = json.loads(text[len(PREFIX):])
        except ValueError:
            self._result = {"type": "error", "message": "Invalid response from Nexus account backend."}

    def _on_done(self, rc):
        mode, target, rollback, result = self._mode, self._target_id, self._rollback, self._result or {}
        self.runner = None
        self.refresh_btn.setEnabled(True)
        if target in self.cards:
            self.cards[target].set_busy(False)
        if self.detail_dialog and target and self.detail_dialog._mod_id() == target:
            self.detail_dialog.set_busy(False)

        if rc != 0 or result.get("type") == "error":
            if rollback and target in self.mods:
                field, old = rollback
                self.mods[target][field] = old
                self._refresh_mod(target)
            msg = result.get("message") or f"Nexus action failed (exit {rc})."
            self.status.setText(msg)
            if mode not in ("inventory",):
                QMessageBox.warning(self, "Nexus Mods", msg)
            return

        if result.get("type") in ("sync", "inventory"):
            self._apply_inventory(result.get("mods", []))
            if result.get("type") == "sync":
                user = (result.get("user") or {}).get("name") or "Nexus user"
                self.status.setText(f"Live sync complete for {user} — {len(self.mods)} installed Nexus mod(s).")
            else:
                self.status.setText(f"{len(self.mods)} installed Nexus mod(s); account state not synced.")
        elif result.get("type") == "state":
            mid = str(result.get("mod_id"))
            if "endorsed" in result:
                self.mods[mid]["endorsed"] = bool(result["endorsed"])
            if "tracked" in result:
                self.mods[mid]["tracked"] = bool(result["tracked"])
            self._refresh_mod(mid)
            self.status.setText(f"Nexus confirmed: {result.get('action', mode)} for mod #{mid}.")
        elif result.get("type") == "detail":
            mid = str(result.get("mod_id"))
            if mid in self.mods:
                if result.get("endorsed") is not None:
                    self.mods[mid]["endorsed"] = bool(result.get("endorsed"))
                if result.get("tracked") is not None:
                    self.mods[mid]["tracked"] = bool(result.get("tracked"))
                self._refresh_mod(mid)
                self._show_detail(mid, result)
        elif result.get("type") == "requires-nxm":
            url = str(result.get("nexus_url") or "")
            self.status.setText("Free account: Nexus requires Download with Manager authorization.")
            if QMessageBox.question(
                self, "Download with Manager required",
                "Nexus free accounts require a fresh Download with Manager authorization. Open this mod's Files page now?",
            ) == QMessageBox.StandardButton.Yes and url:
                QDesktopServices.openUrl(QUrl(url))
        elif result.get("type") == "installed":
            install = result.get("install") or {}
            self.status.setText(f"Installed {install.get('mod_name') or self.mods.get(target, {}).get('name') or 'mod'} successfully.")
            QMessageBox.information(self, "Installed", self.status.text())

    def _apply_inventory(self, rows):
        incoming = {}
        for raw in rows:
            mid = str(raw.get("mod_id") or "")
            if not mid:
                continue
            prior = self.mods.get(mid, {})
            row = dict(raw)
            if "endorsed" not in row and "endorsed" in prior:
                row["endorsed"] = prior["endorsed"]
            if "tracked" not in row and "tracked" in prior:
                row["tracked"] = prior["tracked"]
            incoming[mid] = row
        self.mods = incoming
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.cards = {}
        for mid, mod in self.mods.items():
            card = NexusModCard(mod)
            card.action_requested.connect(self.toggle)
            card.detail_requested.connect(self.open_detail)
            self.cards[mid] = card
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)

    def _refresh_mod(self, mid: str):
        if mid in self.cards and mid in self.mods:
            self.cards[mid].refresh(self.mods[mid])
        if self.detail_dialog and self.detail_dialog._mod_id() == mid:
            self.detail_dialog.refresh(self.mods[mid])

    def toggle(self, mod_id: str, action: str):
        if self.runner or mod_id not in self.mods:
            return
        if not self.api_key.text().strip():
            QMessageBox.information(self, "Nexus API key", "Add your Nexus Mods API key first.")
            return
        field = "endorsed" if action in ("endorse", "unendorse") else "tracked"
        desired = action in ("endorse", "track")
        old = self.mods[mod_id].get(field)
        self.mods[mod_id][field] = desired   # optimistic; backend verifies server truth
        self._refresh_mod(mod_id)
        argv = backend("nexus_account") + [action, "--mod-id", mod_id]
        if field == "endorsed":
            argv += ["--version", str(self.mods[mod_id].get("installed_version") or "")]
        self.status.setText(f"Updating Nexus: {action} mod #{mod_id}…")
        self._start(argv, action, target_id=mod_id, rollback=(field, old))

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
        argv = backend("trending") + ["install", "--game-root", str(root), "--mod-id", mod_id]
        if file_id:
            argv += ["--file-id", str(file_id)]
        self.status.setText(f"Preparing install for Nexus mod #{mod_id}…")
        self._start(argv, "install", target_id=mod_id)

    def open_detail(self, mod_id: str):
        if self.runner or mod_id not in self.mods:
            return
        if not self.api_key.text().strip():
            self._show_detail(mod_id, {"type": "detail", "mod_id": int(mod_id), "metadata": {}, "warnings": ["Nexus API key is required to load live files, changelogs and requirements."]})
            return
        argv = backend("trending") + ["detail", "--mod-id", mod_id]
        self.status.setText(f"Loading full Nexus details for mod #{mod_id}…")
        self._start(argv, "detail", target_id=mod_id)

    def _show_detail(self, mod_id: str, detail: dict):
        dlg = FullModDetailDialog(self.mods[mod_id], detail, self, allow_install=True)
        dlg.install_requested.connect(self.install)
        dlg.account_requested.connect(self.toggle)
        dlg.open_requested.connect(lambda mid: QDesktopServices.openUrl(QUrl(f"https://www.nexusmods.com/cyberpunk2077/mods/{mid}")))
        self.detail_dialog = dlg
        dlg.finished.connect(lambda _=0: setattr(self, "detail_dialog", None))
        dlg.show()

    def save(self):
        self.cfg.setValue("nx/game_root", self.game_root.text())
