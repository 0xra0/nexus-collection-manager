"""Three-step first-run setup wizard: Nexus account, game paths, initial scan."""
from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QVBoxLayout, QWizard, QWizardPage, QWidget,
)

from .config import DEF_DOWNLOADS, DEF_GAME_ROOT, DEF_SCAN_PATHS
from .runner import ProcRunner, backend
from .widgets import BrowseEdit, LogView
from .i18n import LANG_EN, LANG_UK, current_language, language_manager
from .nexus_credentials import credential_hub, get_nexus_api_key
from .theme import (
    THEME_CYBERPUNK, THEME_QT_DARK, THEME_QT_DEFAULT, THEME_QT_WHITE,
    theme_manager,
)

RE_ACCOUNT = re.compile(r"^\[nexus-account\]\s+(\{.*\})$")
RE_ONBOARDING = re.compile(r"^\[onboarding\]\s+(\{.*\})$")
RE_SCAN_PROGRESS = re.compile(r"^\[scanner-progress\]\s+hash\s+(\d+)/(\d+)\s*$")
RE_SCAN_SUMMARY = re.compile(
    r"^\[scanner\]\s+roots=(\d+)\s+files=(\d+)\s+groups=(\d+)\s+"
    r"added=(\d+)\s+changed=(\d+)\s+removed=(\d+)\s+unchanged=(\d+)\s+errors=(\d+)\s*$"
)

API_ACCESS_URL = "https://www.nexusmods.com/users/myaccount?tab=api+access"

GAME_KEYS = (
    "dm/game_root", "trend/game_root", "endorsed/game_root", "tracked/game_root",
    "updates/game_root", "in/game_root", "links/game_root", "profiles/game_root",
    "cf/game_root", "lo/game_root", "archives/game_root", "mg/game_root",
    "sc/game_root", "nx/game_root", "ac/game_root", "dp/game_root",
)
API_KEYS = (
    "nx/api_key", "dm/api_key", "trend/api_key", "endorsed/api_key", "tracked/api_key",
    "updates/api_key", "cf/api_key", "ac/api_key", "dp/api_key",
)
DOWNLOAD_KEYS = ("dl/downloads", "in/downloads", "dp/downloads", "ac/downloads")


def _json_from_line(rx: re.Pattern, text: str) -> dict | None:
    m = rx.match(text.strip())
    if not m:
        return None
    try:
        value = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


class NexusPage(QWizardPage):
    """Step 1: validate a Nexus personal API key or continue offline."""
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self._validated_key = ""
        self._payload = None
        self.setTitle("1 · Connect Nexus Mods")
        self.setSubTitle("Validate your Nexus account so downloads, updates, endorsements, tracking and metadata work immediately.")

        self.language = QComboBox()
        self.language.addItem("English", LANG_EN)
        self.language.addItem("Українська", LANG_UK)
        wanted_language = current_language()
        idx = self.language.findData(wanted_language)
        if idx >= 0:
            self.language.setCurrentIndex(idx)
        self.language.currentIndexChanged.connect(self._language_changed)

        self.theme = QComboBox()
        self.theme.addItem("Qt Default", THEME_QT_DEFAULT)
        self.theme.addItem("Qt Dark", THEME_QT_DARK)
        self.theme.addItem("Qt White", THEME_QT_WHITE)
        self.theme.addItem("Cyberpunk 2077", THEME_CYBERPUNK)
        themes = theme_manager()
        wanted_theme = themes.theme if themes else THEME_QT_DARK
        theme_idx = self.theme.findData(wanted_theme)
        if theme_idx >= 0:
            self.theme.setCurrentIndex(theme_idx)
        self.theme.currentIndexChanged.connect(self._theme_changed)

        self.key = QLineEdit(get_nexus_api_key(cfg))
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Nexus Mods Personal API Key")
        self.key.textChanged.connect(self._key_changed)

        self.show = QCheckBox("Show key")
        self.show.toggled.connect(lambda yes: self.key.setEchoMode(
            QLineEdit.EchoMode.Normal if yes else QLineEdit.EchoMode.Password))
        self.offline = QCheckBox("Continue without Nexus login")
        self.offline.setToolTip("Local install/deploy/profile features still work. You can add a key later.")
        self.offline.toggled.connect(self._offline_changed)

        self.validate_btn = QPushButton("Validate account")
        self.validate_btn.setProperty("accent", True)
        self.validate_btn.clicked.connect(self.validate_account)
        self.open_btn = QPushButton("Open Nexus API Access")
        self.open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(API_ACCESS_URL)))
        self.status = QLabel("Enter your Nexus Mods Personal API Key, then validate it.")
        self.status.setWordWrap(True)

        row = QHBoxLayout(); row.addWidget(self.key, 1); row.addWidget(self.show)
        buttons = QHBoxLayout(); buttons.addWidget(self.validate_btn); buttons.addWidget(self.open_btn); buttons.addStretch(1)
        form = QFormLayout(); form.addRow("Language:", self.language); form.addRow("Theme:", self.theme); form.addRow("API key:", _wrap(row)); form.addRow("", self.offline)
        lay = QVBoxLayout(self); lay.addLayout(form); lay.addLayout(buttons); lay.addWidget(self.status); lay.addStretch(1)

    def _language_changed(self):
        manager = language_manager()
        if manager:
            manager.set_language(self.language.currentData())

    def _theme_changed(self):
        manager = theme_manager()
        if manager:
            manager.set_theme(self.theme.currentData())

    def _key_changed(self):
        if self.key.text().strip() != self._validated_key:
            self._validated_key = ""
            self._payload = None
            if not self.offline.isChecked():
                self.status.setText("Key changed — validate the account again.")
        self.completeChanged.emit()

    def _offline_changed(self, yes: bool):
        self.key.setEnabled(not yes)
        self.validate_btn.setEnabled(not yes and self.runner is None)
        self.open_btn.setEnabled(not yes)
        if yes:
            self.status.setText("Offline mode selected. Nexus-backed features can be enabled later by adding an API key.")
        elif not self._validated_key:
            self.status.setText("Enter your Nexus Mods Personal API Key, then validate it.")
        self.completeChanged.emit()

    def isComplete(self):  # noqa: N802 (Qt API)
        return bool(self.offline.isChecked() or (self._validated_key and self._validated_key == self.key.text().strip()))

    def validate_account(self):
        key = self.key.text().strip()
        if not key:
            QMessageBox.warning(self, "Nexus login", "Paste your Nexus Mods Personal API Key first.")
            return
        if self.runner:
            return
        self.status.setText("Validating Nexus account…")
        self.validate_btn.setEnabled(False)
        self.runner = ProcRunner(backend("nexus_account") + ["validate"], extra_env={"NEXUS_API_KEY": key})
        self.runner.line.connect(self._line)
        self.runner.finished_rc.connect(self._done)
        self.runner.start()

    def _line(self, text: str):
        payload = _json_from_line(RE_ACCOUNT, text)
        if payload:
            self._payload = payload

    def _done(self, rc: int):
        self.runner = None
        self.validate_btn.setEnabled(not self.offline.isChecked())
        payload = self._payload or {}
        if rc == 0 and payload.get("type") == "validate":
            user = payload.get("user") or {}
            name = str(user.get("name") or "Nexus user")
            tier = "Premium" if user.get("is_premium") else "Free"
            self._validated_key = self.key.text().strip()
            self.status.setText(f"✓ Connected as {name} · {tier} account")
        else:
            self._validated_key = ""
            self.status.setText("Could not validate this Nexus API key. Check the key and network connection.")
        self.completeChanged.emit()

    def shutdown(self):
        if self.runner:
            self.runner.stop()


class GamePage(QWizardPage):
    """Step 2: pick and validate the Linux Cyberpunk install and downloads path."""
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self._validated_root = ""
        self._payload = None
        self.setTitle("2 · Configure Cyberpunk 2077")
        self.setSubTitle("Choose the actual game root. Smart Deployment will keep canonical mod content in downloaded_mods/ under this install.")

        self.game_root = BrowseEdit(str(cfg.value("links/game_root", cfg.value("in/game_root", DEF_GAME_ROOT))), "dir")
        self.downloads = BrowseEdit(str(cfg.value("in/downloads", cfg.value("dp/downloads", DEF_DOWNLOADS))), "dir")
        self.game_root.edit.textChanged.connect(self._root_changed)

        self.detect_btn = QPushButton("Auto-detect Linux install")
        self.detect_btn.clicked.connect(self.detect)
        self.validate_btn = QPushButton("Validate game")
        self.validate_btn.setProperty("accent", True)
        self.validate_btn.clicked.connect(self.validate_game)
        self.candidates = QComboBox()
        self.candidates.setVisible(False)
        self.candidates.currentIndexChanged.connect(self._candidate_selected)
        self.status = QLabel("Select the Cyberpunk 2077 folder containing bin/, archive/, r6/, and engine/.")
        self.status.setWordWrap(True)
        self.staging = QLabel("Staging: —")
        self.staging.setWordWrap(True)

        form = QFormLayout(); form.addRow("Game root:", self.game_root); form.addRow("Downloads:", self.downloads); form.addRow("Detected:", self.candidates); form.addRow("", self.staging)
        buttons = QHBoxLayout(); buttons.addWidget(self.detect_btn); buttons.addWidget(self.validate_btn); buttons.addStretch(1)
        lay = QVBoxLayout(self); lay.addLayout(form); lay.addLayout(buttons); lay.addWidget(self.status); lay.addStretch(1)

    def _root_changed(self):
        if str(Path(self.game_root.text()).expanduser().resolve(strict=False)) != self._validated_root:
            self._validated_root = ""
            self.status.setText("Game path changed — validate it again.")
        root = self.game_root.text().strip()
        self.staging.setText(f"Managed staging: {Path(root).expanduser() / 'downloaded_mods'}" if root else "Staging: —")
        self.completeChanged.emit()

    def isComplete(self):  # noqa: N802
        current = str(Path(self.game_root.text()).expanduser().resolve(strict=False)) if self.game_root.text() else ""
        return bool(self._validated_root and self._validated_root == current)

    def _run(self, argv):
        if self.runner:
            return
        self._payload = None
        self.detect_btn.setEnabled(False); self.validate_btn.setEnabled(False)
        self.runner = ProcRunner(argv)
        self.runner.line.connect(self._line)
        self.runner.finished_rc.connect(self._done)
        self.runner.start()

    def detect(self):
        self.status.setText("Looking in Steam libraries…")
        argv = backend("onboarding") + ["detect-game"]
        if self.game_root.text().strip():
            argv += ["--candidate", self.game_root.text().strip()]
        self._run(argv)

    def validate_game(self):
        root = self.game_root.text().strip()
        if not root:
            QMessageBox.warning(self, "Game configuration", "Choose the Cyberpunk 2077 game root first.")
            return
        self.status.setText("Validating game layout…")
        self._run(backend("onboarding") + ["probe-game", "--game-root", root])

    def _line(self, text: str):
        payload = _json_from_line(RE_ONBOARDING, text)
        if payload:
            self._payload = payload

    def _done(self, rc: int):
        self.runner = None
        self.detect_btn.setEnabled(True); self.validate_btn.setEnabled(True)
        payload = self._payload or {}
        if payload.get("type") == "games":
            games = payload.get("games") or []
            self.candidates.blockSignals(True)
            self.candidates.clear()
            for row in games:
                self.candidates.addItem(str(row.get("path") or ""), row)
            self.candidates.blockSignals(False)
            self.candidates.setVisible(bool(games))
            if games:
                self.candidates.setCurrentIndex(0)
                self._candidate_selected(0)
                self.status.setText(f"Found {len(games)} candidate install(s). Validate the selected path to continue.")
            else:
                self.status.setText("No Steam install was detected automatically. Browse to the Cyberpunk 2077 folder manually.")
        elif payload.get("type") == "game" and payload.get("valid") and rc == 0:
            self._validated_root = str(payload.get("path") or "")
            markers = ", ".join(payload.get("markers") or [])
            self.game_root.setText(self._validated_root)
            self.staging.setText(f"Managed staging: {payload.get('staging_dir')}")
            self.status.setText(f"✓ Cyberpunk install validated. Found: {markers}")
        elif payload.get("type") == "game":
            markers = ", ".join(payload.get("markers") or []) or "none"
            self._validated_root = ""
            self.status.setText(f"This folder does not look like a complete Cyberpunk 2077 root. Recognized markers: {markers}")
        else:
            self.status.setText("Game validation failed. Check the selected path.")
        self.completeChanged.emit()

    def _candidate_selected(self, index: int):
        row = self.candidates.itemData(index)
        if isinstance(row, dict) and row.get("path"):
            self.game_root.setText(str(row["path"]))

    def shutdown(self):
        if self.runner:
            self.runner.stop()


class ScanPage(QWizardPage):
    """Step 3: build the initial read-only scanner snapshot."""
    def __init__(self, cfg, game_page: GamePage):
        super().__init__()
        self.cfg = cfg
        self.game_page = game_page
        self.runner = None
        self._scanned_root = ""
        self._summary = None
        self.setTitle("3 · Initial mod scan")
        self.setSubTitle("Discover existing mod files, group them with TF-IDF + DBSCAN, and record an integrity baseline before management begins.")

        self.paths = QPlainTextEdit(str(cfg.value("sc/paths", "\n".join(DEF_SCAN_PATHS))))
        self.paths.setMaximumHeight(150)
        self.jobs = QSpinBox(); self.jobs.setRange(1, 32); self.jobs.setValue(int(cfg.value("sc/jobs", 4)))
        self.start_btn = QPushButton("Run initial scan"); self.start_btn.setProperty("accent", True); self.start_btn.clicked.connect(self.start_scan)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.stop)
        self.bar = QProgressBar(); self.bar.setRange(0, 100)
        self.status = QLabel("Run the initial scan to finish setup."); self.status.setWordWrap(True)
        self.log = LogView(); self.log.setMaximumHeight(220)

        form = QFormLayout(); form.addRow("Scan roots:", self.paths); form.addRow("Hash workers:", self.jobs)
        buttons = QHBoxLayout(); buttons.addWidget(self.start_btn); buttons.addWidget(self.stop_btn); buttons.addStretch(1)
        lay = QVBoxLayout(self); lay.addLayout(form); lay.addLayout(buttons); lay.addWidget(self.bar); lay.addWidget(self.status); lay.addWidget(self.log, 1)

    def isComplete(self):  # noqa: N802
        root = str(Path(self.game_page.game_root.text()).expanduser().resolve(strict=False)) if self.game_page.game_root.text() else ""
        return bool(self._scanned_root and self._scanned_root == root)

    def initializePage(self):  # noqa: N802
        root = str(Path(self.game_page.game_root.text()).expanduser().resolve(strict=False)) if self.game_page.game_root.text() else ""
        if self._scanned_root and self._scanned_root != root:
            self._scanned_root = ""; self.bar.setValue(0); self.status.setText("Game root changed. Run a new initial scan.")
            self.completeChanged.emit()

    def configured_paths(self):
        return [line.strip() for line in self.paths.toPlainText().splitlines() if line.strip() and not line.lstrip().startswith("#")]

    def start_scan(self):
        if self.runner:
            return
        root = Path(self.game_page.game_root.text()).expanduser()
        paths = self.configured_paths()
        if not paths:
            QMessageBox.warning(self, "Initial scan", "Configure at least one scan root.")
            return
        argv = backend("scanner") + ["--game-root", str(root), "--jobs", str(self.jobs.value())]
        for path in paths:
            argv += ["--path", path]
        self.log.clear(); self.bar.setValue(0); self._summary = None
        self.status.setText("Scanning existing mod files…")
        self.start_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.runner = ProcRunner(argv)
        self.runner.line.connect(self._line)
        self.runner.finished_rc.connect(self._done)
        self.runner.start()

    def _line(self, text: str):
        self.log.add(text)
        m = RE_SCAN_PROGRESS.match(text)
        if m:
            current, total = int(m.group(1)), int(m.group(2))
            self.bar.setValue(int(100 * current / total) if total else 0)
            self.status.setText(f"Hashing existing mod files… {current}/{total}")
            return
        m = RE_SCAN_SUMMARY.match(text)
        if m:
            self._summary = tuple(int(v) for v in m.groups())

    def _done(self, rc: int):
        self.runner = None; self.start_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        if rc != 0:
            self.status.setText(f"Initial scan failed (exit {rc}). Review the log and try again.")
            self.completeChanged.emit(); return
        self.bar.setValue(100)
        self._scanned_root = str(Path(self.game_page.game_root.text()).expanduser().resolve(strict=False))
        if self._summary:
            roots, files, groups, added, changed, removed, unchanged, errors = self._summary
            self.status.setText(f"✓ Initial scan complete — {files} file(s) in {groups} inferred group(s); {errors} read/hash error(s).")
        else:
            self.status.setText("✓ Initial scan complete.")
        self.completeChanged.emit()

    def stop(self):
        if self.runner:
            self.runner.stop(); self.stop_btn.setEnabled(False); self.status.setText("Stopping scan…")

    def shutdown(self):
        self.stop()


def _wrap(layout) -> QWidget:
    widget = QWidget(); widget.setLayout(layout); return widget


class OnboardingWizard(QWizard):
    """Three-page setup wizard.  Settings are committed only on Finish."""
    setup_completed = Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Nexus Collection Manager · Guided Setup")
        self.resize(760, 610)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)

        self.nexus_page = NexusPage(cfg)
        self.game_page = GamePage(cfg)
        self.scan_page = ScanPage(cfg, self.game_page)
        self.addPage(self.nexus_page); self.addPage(self.game_page); self.addPage(self.scan_page)

    def accept(self):
        root = str(Path(self.game_page.game_root.text()).expanduser().resolve(strict=False))
        downloads = str(Path(self.game_page.downloads.text()).expanduser().resolve(strict=False))
        api_key = "" if self.nexus_page.offline.isChecked() else self.nexus_page.key.text().strip()
        scan_paths = "\n".join(self.scan_page.configured_paths())

        for key in GAME_KEYS:
            self.cfg.setValue(key, root)
        for key in DOWNLOAD_KEYS:
            self.cfg.setValue(key, downloads)
        credential_hub(self.cfg).set(api_key)
        self.cfg.setValue("sc/paths", scan_paths)
        self.cfg.setValue("sc/jobs", self.scan_page.jobs.value())
        self.cfg.setValue("onboarding/completed", True)
        self.cfg.setValue("onboarding/version", "0.17.5")
        self.cfg.sync()

        # Keep the legacy CLI/shared defaults aligned with the GUI.  Never erase
        # unrelated keys that may have been written by the deployer.
        try:
            config_path = Path.home() / ".config/cp2077_modder/config.json"
            data = {}
            if config_path.is_file():
                try:
                    loaded = json.loads(config_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data.update(loaded)
                except (OSError, ValueError, TypeError):
                    pass
            data.update({"game_root": root, "downloads": downloads, "nexus_api_key": api_key})
            config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(config_path)
        except OSError:
            # QSettings is the authoritative GUI configuration; a read-only home
            # directory should not prevent first-run setup from completing.
            pass
        self.setup_completed.emit()
        super().accept()

    def reject(self):
        self._shutdown()
        super().reject()

    def closeEvent(self, event):
        self._shutdown()
        super().closeEvent(event)

    def _shutdown(self):
        self.nexus_page.shutdown(); self.game_page.shutdown(); self.scan_page.shutdown()


def needs_onboarding(cfg) -> bool:
    """Return True only for a genuinely new configuration.

    Existing pre-v0.15 users are not interrupted by an upgrade: any previously
    configured game-root setting counts as an established installation.
    """
    if bool(cfg.value("onboarding/completed", False, type=bool)):
        return False
    return not any(cfg.contains(key) for key in GAME_KEYS)
