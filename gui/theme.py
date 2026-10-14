"""Runtime Qt theme support for Nexus Collection Manager.

The module deliberately keeps theme identifiers/normalization importable without
PySide6 so unit tests and backend-only installations can inspect the settings
contract.  Qt integration is enabled when PySide6 is available.
"""
from __future__ import annotations

THEME_QT_DEFAULT = "qt-default"
THEME_QT_DARK = "qt-dark"
THEME_QT_WHITE = "qt-white"
THEME_CYBERPUNK = "cyberpunk-2077"
DEFAULT_THEME = THEME_QT_DARK  # preserve the visual default used before v0.17

THEMES = (
    THEME_QT_DEFAULT,
    THEME_QT_DARK,
    THEME_QT_WHITE,
    THEME_CYBERPUNK,
)

THEME_LABELS = {
    THEME_QT_DEFAULT: "Qt Default",
    THEME_QT_DARK: "Qt Dark",
    THEME_QT_WHITE: "Qt White",
    THEME_CYBERPUNK: "Cyberpunk 2077",
}

_ALIASES = {
    "": DEFAULT_THEME,
    "default": THEME_QT_DEFAULT,
    "qt": THEME_QT_DEFAULT,
    "qt default": THEME_QT_DEFAULT,
    "qt-default": THEME_QT_DEFAULT,
    "dark": THEME_QT_DARK,
    "qt dark": THEME_QT_DARK,
    "qt-dark": THEME_QT_DARK,
    "white": THEME_QT_WHITE,
    "light": THEME_QT_WHITE,
    "qt white": THEME_QT_WHITE,
    "qt light": THEME_QT_WHITE,
    "qt-white": THEME_QT_WHITE,
    "cyberpunk": THEME_CYBERPUNK,
    "cyberpunk 2077": THEME_CYBERPUNK,
    "cyberpunk-2077": THEME_CYBERPUNK,
}


def normalize_theme(value: object) -> str:
    """Return a stable theme id, falling back to the historical dark default."""
    raw = str(value or "").strip().lower().replace("_", "-")
    return _ALIASES.get(raw, raw if raw in THEMES else DEFAULT_THEME)


def theme_label(theme: object) -> str:
    return THEME_LABELS[normalize_theme(theme)]


# ----------------------------- Qt integration -------------------------------
try:  # pragma: no cover - exercised on real GUI systems
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication
    QT_AVAILABLE = True
except Exception:  # pragma: no cover - headless/unit-test builders
    QT_AVAILABLE = False


_BASE_QSS = r"""
QWidget { font-size: 10pt; }
QMainWindow, QDialog, QWizard { background: @BG@; }
QToolTip { background:@SURFACE2@; color:@TEXT@; border:1px solid @BORDER@;
           padding:4px 6px; border-radius:@RADIUS_SMALL@; }

QMenuBar { background:@BG@; color:@TEXT@; }
QMenuBar::item { background:transparent; padding:5px 9px; }
QMenuBar::item:selected { background:@SURFACE2@; color:@TEXT@; }
QMenu { background:@SURFACE@; color:@TEXT@; border:1px solid @BORDER@; }
QMenu::item { padding:6px 28px 6px 10px; }
QMenu::item:selected { background:@SEL@; color:@SEL_TEXT@; }
QMenu::separator { height:1px; background:@BORDER@; margin:4px 8px; }

QTabWidget::pane { border:1px solid @BORDER@; border-radius:@RADIUS@; top:-1px; background:@BG@; }
QTabBar::tab { background:transparent; color:@MUTED@; padding:8px 18px; margin-right:2px;
               border:none; border-bottom:2px solid transparent; }
QTabBar::tab:hover { color:@TEXT@; background:@SURFACE2@; }
QTabBar::tab:selected { color:@TEXT@; border-bottom:2px solid @ACCENT@; }

QGroupBox { background:transparent; border:1px solid @BORDER@; border-radius:@RADIUS@;
            margin-top:14px; padding:14px 12px 12px 12px; }
QGroupBox::title { subcontrol-origin:margin; subcontrol-position:top left; left:12px;
                   padding:2px 6px; color:@GROUP_TITLE@; }
QLabel { background:transparent; color:@TEXT@; }
QLabel[muted="true"] { color:@MUTED@; }
QLabel[warning="true"] { color:@WARNING@; }
QLabel[danger="true"] { color:@DANGER@; }

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QAbstractSpinBox {
    background:@SURFACE@; color:@TEXT@; border:1px solid @BORDER@; border-radius:@RADIUS_SMALL@;
    padding:6px 8px; selection-background-color:@SEL@; selection-color:@SEL_TEXT@; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QAbstractSpinBox:focus { border:1px solid @FOCUS@; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QAbstractSpinBox:disabled { color:@MUTED@; background:@BG@; }
QComboBox::drop-down { border:none; width:22px; }
QComboBox QAbstractItemView { background:@SURFACE@; color:@TEXT@; border:1px solid @BORDER@;
                             selection-background-color:@SEL@; selection-color:@SEL_TEXT@; }

QPushButton, QToolButton {
    background:@SURFACE2@; color:@TEXT@; border:1px solid @BORDER2@; border-radius:@RADIUS_SMALL@;
    padding:7px 14px; }
QPushButton:hover, QToolButton:hover { background:@RAISED@; border:1px solid @FOCUS@; }
QPushButton:pressed, QToolButton:pressed { background:@BG@; }
QPushButton:disabled, QToolButton:disabled {
    color:@MUTED@; background:@SURFACE@; border:1px solid @BORDER@; }
QPushButton[accent="true"] {
    background:@PRIMARY@; color:@PRIMARY_TEXT@; border:1px solid @PRIMARY@; font-weight:600; }
QPushButton[accent="true"]:hover { background:@PRIMARY_HOVER@; border-color:@PRIMARY_HOVER@; }
QPushButton[accent="true"]:pressed { background:@PRIMARY_PRESS@; }
QPushButton[accent="true"]:disabled {
    background:@SURFACE@; color:@MUTED@; border:1px solid @BORDER@; }
QPushButton[danger="true"] {
    background:@DANGER@; color:#ffffff; border:1px solid @DANGER@; font-weight:600; }
QPushButton[danger="true"]:hover { background:@DANGER_HOVER@; border-color:@DANGER_HOVER@; }
QPushButton[danger="true"]:disabled {
    background:@SURFACE@; color:@MUTED@; border:1px solid @BORDER@; }

QCheckBox, QRadioButton { spacing:8px; background:transparent; color:@TEXT@; }
QCheckBox::indicator, QRadioButton::indicator {
    width:16px; height:16px; border:1px solid @BORDER2@; background:@SURFACE@; }
QCheckBox::indicator { border-radius:@CHECK_RADIUS@; }
QRadioButton::indicator { border-radius:8px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border:1px solid @FOCUS@; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background:@ACCENT@; border:1px solid @ACCENT@; }

QProgressBar { background:@SURFACE@; border:1px solid @BORDER@; border-radius:@RADIUS_SMALL@;
               min-height:18px; text-align:center; color:@TEXT@; }
QProgressBar::chunk { border-radius:@CHUNK_RADIUS@; margin:0px; background:@ACCENT@; }

QTableWidget, QTableView, QListWidget, QTreeWidget {
    background:@SURFACE@; alternate-background-color:@SURFACE2@; gridline-color:@BORDER@;
    border:1px solid @BORDER@; border-radius:@RADIUS_SMALL@; selection-background-color:@SEL@;
    selection-color:@SEL_TEXT@; outline:none; color:@TEXT@; }
QTableWidget::item, QTableView::item { padding:3px 4px; }
QHeaderView::section { background:@SURFACE2@; color:@HEADER_TEXT@; padding:6px 8px;
                       border:none; border-bottom:1px solid @BORDER@; }
QTableCornerButton::section { background:@SURFACE2@; border:none; }

QScrollBar:vertical { background:transparent; width:12px; margin:2px; }
QScrollBar::handle:vertical { background:@BORDER2@; border-radius:@SCROLL_RADIUS@; min-height:24px; }
QScrollBar::handle:vertical:hover { background:@SCROLL_HOVER@; }
QScrollBar:horizontal { background:transparent; height:12px; margin:2px; }
QScrollBar::handle:horizontal { background:@BORDER2@; border-radius:@SCROLL_RADIUS@; min-width:24px; }
QScrollBar::handle:horizontal:hover { background:@SCROLL_HOVER@; }
QScrollBar::add-line, QScrollBar::sub-line { width:0; height:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }

QStatusBar { color:@MUTED@; border-top:1px solid @BORDER@; background:@BG@; }
QStatusBar::item { border:none; }
QWizard QLabel { color:@TEXT@; }
"""

_DARK = {
    "WARNING": "#d29922", "DANGER": "#f85149", "DANGER_HOVER": "#ff7b72",
    "BG": "#0d1117", "SURFACE": "#161b22", "SURFACE2": "#1c2128", "RAISED": "#262c36",
    "BORDER": "#30363d", "BORDER2": "#3d444d", "TEXT": "#e6edf3", "MUTED": "#8b949e",
    "ACCENT": "#1f6feb", "FOCUS": "#388bfd", "SEL": "#1f6feb", "SEL_TEXT": "#ffffff",
    "PRIMARY": "#238636", "PRIMARY_HOVER": "#2ea043", "PRIMARY_PRESS": "#1a612b",
    "PRIMARY_TEXT": "#ffffff", "GROUP_TITLE": "#8b949e", "HEADER_TEXT": "#8b949e",
    "SCROLL_HOVER": "#4a525c", "RADIUS": "10px", "RADIUS_SMALL": "8px",
    "CHECK_RADIUS": "4px", "CHUNK_RADIUS": "7px", "SCROLL_RADIUS": "5px",
}

_WHITE = {
    "WARNING": "#9a6700", "DANGER": "#cf222e", "DANGER_HOVER": "#a40e26",
    "BG": "#f6f8fa", "SURFACE": "#ffffff", "SURFACE2": "#eef1f4", "RAISED": "#e2e7ed",
    "BORDER": "#d0d7de", "BORDER2": "#afb8c1", "TEXT": "#1f2328", "MUTED": "#656d76",
    "ACCENT": "#0969da", "FOCUS": "#0969da", "SEL": "#0969da", "SEL_TEXT": "#ffffff",
    "PRIMARY": "#1f883d", "PRIMARY_HOVER": "#1a7f37", "PRIMARY_PRESS": "#116329",
    "PRIMARY_TEXT": "#ffffff", "GROUP_TITLE": "#57606a", "HEADER_TEXT": "#57606a",
    "SCROLL_HOVER": "#8c959f", "RADIUS": "10px", "RADIUS_SMALL": "8px",
    "CHECK_RADIUS": "4px", "CHUNK_RADIUS": "7px", "SCROLL_RADIUS": "5px",
}

_CYBERPUNK = {
    "WARNING": "#fcee0a", "DANGER": "#ff003c", "DANGER_HOVER": "#ff4167",
    "BG": "#07090b", "SURFACE": "#101419", "SURFACE2": "#171d22", "RAISED": "#232b32",
    "BORDER": "#3d464d", "BORDER2": "#65727c", "TEXT": "#f3f5f4", "MUTED": "#9aa5aa",
    "ACCENT": "#fcee0a", "FOCUS": "#00f0ff", "SEL": "#00f0ff", "SEL_TEXT": "#050708",
    "PRIMARY": "#fcee0a", "PRIMARY_HOVER": "#fff65a", "PRIMARY_PRESS": "#d8cb00",
    "PRIMARY_TEXT": "#0b0d0e", "GROUP_TITLE": "#fcee0a", "HEADER_TEXT": "#00f0ff",
    "SCROLL_HOVER": "#00f0ff", "RADIUS": "0px", "RADIUS_SMALL": "0px",
    "CHECK_RADIUS": "0px", "CHUNK_RADIUS": "0px", "SCROLL_RADIUS": "0px",
}


def _qss(tokens: dict[str, str]) -> str:
    value = _BASE_QSS
    for key, replacement in tokens.items():
        value = value.replace(f"@{key}@", replacement)
    return value


if QT_AVAILABLE:  # pragma: no cover - no PySide6 in the CI container
    def _palette(tokens: dict[str, str], *, light: bool = False) -> QPalette:
        c = lambda key: QColor(tokens[key])
        R = QPalette.ColorRole
        pal = QPalette()
        pal.setColor(R.Window, c("BG"))
        pal.setColor(R.WindowText, c("TEXT"))
        pal.setColor(R.Base, c("SURFACE"))
        pal.setColor(R.AlternateBase, c("SURFACE2"))
        pal.setColor(R.ToolTipBase, c("SURFACE2"))
        pal.setColor(R.ToolTipText, c("TEXT"))
        pal.setColor(R.Text, c("TEXT"))
        pal.setColor(R.Button, c("SURFACE2"))
        pal.setColor(R.ButtonText, c("TEXT"))
        pal.setColor(R.BrightText, QColor("#cf222e" if light else "#ff003c"))
        pal.setColor(R.Link, c("FOCUS"))
        pal.setColor(R.LinkVisited, c("FOCUS"))
        pal.setColor(R.Highlight, c("SEL"))
        pal.setColor(R.HighlightedText, c("SEL_TEXT"))
        pal.setColor(R.PlaceholderText, c("MUTED"))
        disabled = QPalette.ColorGroup.Disabled
        for role in (R.WindowText, R.Text, R.ButtonText):
            pal.setColor(disabled, role, c("MUTED"))
        return pal


    class ThemeManager(QObject):
        themeChanged = Signal(str)

        def __init__(self, app: QApplication, settings):
            super().__init__(app)
            self.app = app
            self.settings = settings
            # Capture the platform's untouched Qt state before any NCM theme is applied.
            self._default_style = str(app.style().objectName() or "")
            self._default_palette = QPalette(app.palette())
            self._default_stylesheet = str(app.styleSheet() or "")
            saved = settings.value("ui/theme", "")
            self.theme = normalize_theme(saved or DEFAULT_THEME)
            if not saved:
                settings.setValue("ui/theme", self.theme)
            self._apply(self.theme)

        def set_theme(self, theme: object, *, persist: bool = True):
            code = normalize_theme(theme)
            if persist:
                self.settings.setValue("ui/theme", code)
                self.settings.sync()
            self._apply(code)
            changed = code != self.theme
            self.theme = code
            if changed:
                self.themeChanged.emit(code)

        def _apply(self, code: str):
            if code == THEME_QT_DEFAULT:
                if self._default_style:
                    self.app.setStyle(self._default_style)
                self.app.setPalette(QPalette(self._default_palette))
                self.app.setStyleSheet(self._default_stylesheet)
                return

            self.app.setStyle("Fusion")
            if code == THEME_QT_WHITE:
                tokens = _WHITE
                self.app.setPalette(_palette(tokens, light=True))
            elif code == THEME_CYBERPUNK:
                tokens = _CYBERPUNK
                self.app.setPalette(_palette(tokens))
            else:
                tokens = _DARK
                self.app.setPalette(_palette(tokens))
            self.app.setStyleSheet(_qss(tokens))


_MANAGER = None


def install_theme_manager(app, settings):
    """Install the global runtime theme manager and apply the saved theme."""
    global _MANAGER
    if not QT_AVAILABLE:  # pragma: no cover
        return None
    if _MANAGER is None or _MANAGER.app is not app:
        _MANAGER = ThemeManager(app, settings)
    return _MANAGER


def theme_manager():
    return _MANAGER


def set_theme(theme: object, *, persist: bool = True) -> str:
    code = normalize_theme(theme)
    if _MANAGER is not None:
        _MANAGER.set_theme(code, persist=persist)
    return code


def apply_dark_theme(app):
    """Backward-compatible one-shot helper retained for external imports."""
    if not QT_AVAILABLE:  # pragma: no cover
        return
    app.setStyle("Fusion")
    app.setPalette(_palette(_DARK))
    app.setStyleSheet(_qss(_DARK))
