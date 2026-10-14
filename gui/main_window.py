"""The main window: download, install, deploy, conflicts/load-order, account and diagnostics."""
from shutil import which

from PySide6.QtCore import QSettings
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import QMainWindow, QTabWidget, QMessageBox

from .download_tab import DownloadTab
from .download_manager_tab import DownloadManagerTab
from .trending_tab import TrendingTab
from .deploy_tab import DeployTab
from .installation_tab import InstallationTab
from .deployment_tab import DeploymentTab
from .manage_tab import ManageTab
from .scanner_tab import ScannerTab
from .nexus_tab import NexusTab
from .correlation_tab import CorrelationTab
from .conflict_tab import ConflictTab
from .load_order_tab import LoadOrderTab
from .archive_tab import ArchiveTab
from .profile_tab import ProfileTab
from .update_tab import UpdateTab
from .i18n import LANG_EN, LANG_UK, language_manager, tr
from .widgets import safe_add_action, safe_add_menu, safe_menu_bar, safe_status_bar
from .theme import (
    THEME_CYBERPUNK, THEME_QT_DARK, THEME_QT_DEFAULT, THEME_QT_WHITE,
    theme_manager,
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nexus Collection Manager — Cyberpunk 2077")
        self.resize(920, 720)
        self.cfg = QSettings("nexus-cp2077", "collection-manager")

        self.tabs = QTabWidget()
        self.download_manager = DownloadManagerTab(self.cfg)
        self.trending_mods = TrendingTab(self.cfg)
        self.endorsed_mods = TrendingTab(self.cfg, fixed_feed="endorsed", settings_prefix="endorsed")
        self.tracked_mods = TrendingTab(self.cfg, fixed_feed="tracked", settings_prefix="tracked")
        self.updates = UpdateTab(self.cfg)
        self.download = DownloadTab(self.cfg)
        self.install = InstallationTab(self.cfg)
        self.deploy   = DeployTab(self.cfg)
        self.links    = DeploymentTab(self.cfg)
        self.conflicts = ConflictTab(self.cfg)
        self.load_order = LoadOrderTab(self.cfg)
        self.archives = ArchiveTab(self.cfg)
        self.profiles = ProfileTab(self.cfg)
        self.manage   = ManageTab(self.cfg)
        self.scanner  = ScannerTab(self.cfg)
        self.nexus     = NexusTab(self.cfg)
        self.correlation = CorrelationTab(self.cfg)
        self.tabs.addTab(self.download_manager, "1 · Downloads")
        self.tabs.addTab(self.trending_mods, "2 · Trending")
        self.tabs.addTab(self.endorsed_mods, "3 · Endorsed")
        self.tabs.addTab(self.tracked_mods, "4 · Tracked")
        self.tabs.addTab(self.updates, "5 · Updates")
        self.tabs.addTab(self.download, "6 · Collections")
        self.tabs.addTab(self.install, "7 · Install")
        self.tabs.addTab(self.links, "8 · Deploy")
        self.tabs.addTab(self.profiles, "9 · Profiles")
        self.tabs.addTab(self.conflicts, "10 · Conflicts")
        self.tabs.addTab(self.load_order, "11 · Load Order")
        self.tabs.addTab(self.archives, "12 · Archives")
        self.tabs.addTab(self.manage, "13 · Manage")
        self.tabs.addTab(self.scanner, "14 · Scanner")
        self.tabs.addTab(self.nexus, "15 · Nexus")
        self.tabs.addTab(self.correlation, "16 · Correlate")
        self.tabs.addTab(self.deploy, "17 · Advanced")
        self.setCentralWidget(self.tabs)

        self._menu_bar = safe_menu_bar(self)
        self._status_bar = safe_status_bar(self)
        setup_menu = safe_add_menu(self._menu_bar, "Setup")
        setup_action = safe_add_action(setup_menu, "Guided Setup…")
        setup_action.triggered.connect(self._guided_setup)
        language_menu = safe_add_menu(setup_menu, "Language")
        self._language_group = QActionGroup(self)
        self._language_group.setExclusive(True)
        self._lang_en = safe_add_action(language_menu, "English")
        self._lang_uk = safe_add_action(language_menu, "Українська")
        for action, code in ((self._lang_en, LANG_EN), (self._lang_uk, LANG_UK)):
            action.setCheckable(True)
            action.setData(code)
            self._language_group.addAction(action)
            action.triggered.connect(lambda _checked=False, c=code: self._set_language(c))
        manager = language_manager()
        if manager:
            manager.languageChanged.connect(self._sync_language_actions)
            manager.languageChanged.connect(lambda _code: self._status_bar.showMessage(self._preflight()))
            self._sync_language_actions(manager.language)

        theme_menu = safe_add_menu(setup_menu, "Theme")
        self._theme_group = QActionGroup(self)
        self._theme_group.setExclusive(True)
        self._theme_default = safe_add_action(theme_menu, "Qt Default")
        self._theme_dark = safe_add_action(theme_menu, "Qt Dark")
        self._theme_white = safe_add_action(theme_menu, "Qt White")
        self._theme_cyberpunk = safe_add_action(theme_menu, "Cyberpunk 2077")
        for action, code in (
            (self._theme_default, THEME_QT_DEFAULT),
            (self._theme_dark, THEME_QT_DARK),
            (self._theme_white, THEME_QT_WHITE),
            (self._theme_cyberpunk, THEME_CYBERPUNK),
        ):
            action.setCheckable(True)
            action.setData(code)
            self._theme_group.addAction(action)
            action.triggered.connect(lambda _checked=False, c=code: self._set_theme(c))
        themes = theme_manager()
        if themes:
            themes.themeChanged.connect(self._sync_theme_actions)
            self._sync_theme_actions(themes.theme)
        self._status_bar.showMessage(self._preflight())


    def _set_language(self, code):
        manager = language_manager()
        if manager:
            manager.set_language(code)
            self._sync_language_actions(manager.language)

    def _sync_language_actions(self, code):
        self._lang_en.setChecked(code == LANG_EN)
        self._lang_uk.setChecked(code == LANG_UK)

    def _set_theme(self, code):
        manager = theme_manager()
        if manager:
            manager.set_theme(code)
            self._sync_theme_actions(manager.theme)

    def _sync_theme_actions(self, code):
        self._theme_default.setChecked(code == THEME_QT_DEFAULT)
        self._theme_dark.setChecked(code == THEME_QT_DARK)
        self._theme_white.setChecked(code == THEME_QT_WHITE)
        self._theme_cyberpunk.setChecked(code == THEME_CYBERPUNK)

    def _guided_setup(self):
        from .onboarding import OnboardingWizard
        wizard = OnboardingWizard(self.cfg, self)
        if wizard.exec():
            QMessageBox.information(
                self, "Setup saved",
                "Guided setup is complete. Restart Nexus Collection Manager to reload every open tab with the new paths/account settings.")

    def _preflight(self):
        bits = []
        bits.append(tr("aria2c ✓") if which("aria2c") else tr("aria2c ✗ (download needs it)"))
        bits.append(tr("bsdtar ✓") if which("bsdtar") else tr("bsdtar ✗ (install needs it)"))
        return "   ".join(bits)

    def closeEvent(self, event):
        for tab in (self.download_manager, self.trending_mods, self.endorsed_mods, self.tracked_mods, self.updates, self.download, self.install, self.links, self.profiles, self.conflicts, self.load_order, self.archives, self.manage, self.scanner, self.nexus, self.correlation, self.deploy):
            tab.save()
            r = getattr(tab, "runner", None)
            if r:
                r.stop()
            api_r = getattr(tab, "api_runner", None)
            if api_r:
                api_r.stop()
        self.cfg.sync()
        super().closeEvent(event)
