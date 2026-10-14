#!/usr/bin/env python3
"""Nexus Collection Manager — a PySide6 front-end for a Cyberpunk 2077 modding pipeline.

The tabs wrap backend modules (see the ``nexus_core`` package):

    1 · Downloads  nexus_core.download_manager — persistent direct/NXM Nexus queue
    2 · Trending   nexus_core.trending_mods — live Nexus discovery + one-click install
    3 · Collections nexus_core.downloader — collection batch downloads
    2 · Install    nexus_core.deployer    — extract + deploy archives (bsdtar)
    3 · Manage     nexus_core.modctl      — park / unpark deployed mods
    4 · Scanner    nexus_core.mod_scanner — TF-IDF/DBSCAN grouping + SHA-256 integrity

The GUI does NOT reimplement that logic: it launches each backend as a
``python -m nexus_core.<module>`` subprocess and streams its stdout into the
window, parsing the lines the backends already print for a live progress bar.
That keeps every hard-won behaviour (free-download throttle handling, resumable
aria2c, conflict-tracking deploy, framework-safe parking) untouched.

Direct downloads use the authenticated Nexus API for Premium accounts and manager-authorized NXM links for free accounts.

First run opens a three-step guided setup. Re-run it any time with:  python3 main.py --setup

Run:  python3 main.py
"""
import os
import sys

# Root-run Linux desktop sessions commonly have no usable AT-SPI bus and GTK
# otherwise prints a harmless dbind warning before Qt starts.  Do not disable
# accessibility for normal desktop users.
if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
    # A root-run GUI normally cannot access the desktop user's AT-SPI bus.
    # Force the bridge off even when an inherited environment contains
    # NO_AT_BRIDGE=0; setdefault() left that broken value untouched.
    os.environ["NO_AT_BRIDGE"] = "1"
    # Qt 6 can also instantiate its native AT-SPI accessibility bridge, which
    # is separate from GTK's NO_AT_BRIDGE switch.  A root-run GUI cannot use
    # the desktop user's bus, so disable both Qt accessibility toggles before
    # importing PySide6.
    os.environ["QT_ACCESSIBILITY"] = "0"
    os.environ["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "0"

try:
    from PySide6.QtWidgets import QApplication, QDialog
    from PySide6.QtCore import QTimer
except ImportError:
    sys.exit("PySide6 is required:  python3 -m pip install --user PySide6")

from gui.theme import install_theme_manager
from gui.main_window import MainWindow
from gui.onboarding import OnboardingWizard, needs_onboarding
from gui.i18n import install_language_manager
from PySide6.QtCore import QSettings


def main():
    force_setup = "--setup" in sys.argv[1:]
    qt_argv = [arg for arg in sys.argv if arg != "--setup"]
    app = QApplication(qt_argv)
    app.setApplicationName("Nexus Collection Manager")
    app.setOrganizationName("nexus-cp2077")
    cfg = QSettings("nexus-cp2077", "collection-manager")
    install_theme_manager(app, cfg)
    install_language_manager(app, cfg)
    if force_setup or needs_onboarding(cfg):
        wizard = OnboardingWizard(cfg)
        if wizard.exec() != QDialog.DialogCode.Accepted:
            return 0
    win = MainWindow()
    win.show()
    nxm = next((arg for arg in sys.argv[1:] if str(arg).lower().startswith("nxm://")), "")
    if nxm:
        def dispatch_nxm():
            win.tabs.setCurrentWidget(win.download_manager)
            win.download_manager.accept_nxm(nxm)
        QTimer.singleShot(0, dispatch_nxm)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
