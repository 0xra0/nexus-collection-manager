"""Small reusable widgets shared across tabs."""
import html
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLineEdit, QToolButton, QPlainTextEdit, QFileDialog,
    QTableWidgetItem, QHeaderView, QMainWindow, QMenuBar, QMenu, QStatusBar,
)

from .parsing import human_bytes


def safe_horizontal_header(table):
    """Return a usable QHeaderView for a table across PySide6 wrapper quirks.

    Some mismatched/buggy PySide6 + Shiboken installations have been observed to
    expose QTableView.horizontalHeader() only as its QAbstractScrollArea base
    wrapper.  That wrapper lacks QHeaderView-specific methods such as
    setSectionResizeMode().  Replace it with an explicitly constructed
    QHeaderView in that case instead of crashing the whole application.
    """
    header = table.horizontalHeader()
    if callable(getattr(header, "setSectionResizeMode", None)):
        return header
    replacement = QHeaderView(Qt.Orientation.Horizontal, table)
    table.setHorizontalHeader(replacement)
    return replacement


def safe_menu_bar(window: QMainWindow) -> QMenuBar:
    """Return a real QMenuBar even when a PySide accessor loses its subtype.

    PySide6 6.11.x can, in some environments, expose QMainWindow.menuBar() as
    its QWidget base wrapper.  Construct and install an explicit QMenuBar when
    the returned wrapper does not provide QMenuBar-specific API.
    """
    # Do not call the QMainWindow menu-bar accessor here: on the affected PySide6 6.11.x
    # binding that accessor is the operation that returns the bad QWidget
    # wrapper.  Install an explicitly constructed subtype instead.
    replacement = QMenuBar(window)
    window.setMenuBar(replacement)
    return replacement


def safe_add_menu(parent, title: str) -> QMenu:
    """Create a QMenu explicitly instead of trusting addMenu()'s return cast."""
    menu = QMenu(title, parent)
    parent.addMenu(menu)
    return menu


def safe_add_action(parent, text: str) -> QAction:
    """Create a QAction explicitly instead of trusting addAction()'s return cast."""
    action = QAction(text, parent)
    parent.addAction(action)
    return action


def safe_status_bar(window: QMainWindow) -> QStatusBar:
    """Return a real QStatusBar across the same PySide subtype-wrapper quirk."""
    # Same rule as the menu-bar accessor: bypass the typed accessor entirely.
    replacement = QStatusBar(window)
    window.setStatusBar(replacement)
    return replacement


class BrowseEdit(QWidget):
    """A line edit paired with a Browse… button for a file or directory path."""
    def __init__(self, default="", kind="dir"):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(default)
        self.kind = kind
        btn = QToolButton()
        btn.setText("Browse…")
        btn.clicked.connect(self._browse)
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)

    def _browse(self):
        start = self.edit.text().strip() or str(Path.home())
        if self.kind == "dir":
            p = QFileDialog.getExistingDirectory(self, "Select folder", start)
        else:
            p, _ = QFileDialog.getOpenFileName(self, "Select file", start)
        if p:
            self.edit.setText(p)

    def text(self):
        return self.edit.text().strip()

    def setText(self, t):
        self.edit.setText(t)


class LogView(QPlainTextEdit):
    """Read-only, monospaced, auto-scrolling console with light colour coding."""
    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(8000)
        f = QFont("monospace")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self.setFont(f)

    def add(self, text: str):
        if not text.strip():
            return
        esc = html.escape(text)
        color = None
        if text.startswith("✓") or "[✓]" in text or "VERIFIED" in text:
            color = "#3fb950"
        elif text.startswith("✗") or "[!]" in text or "MISMATCH" in text or "[corrupt]" in text:
            color = "#f85149"
        elif text.startswith("~~") or text.startswith("      "):
            color = "#8b949e"
        elif text.startswith("[") or text.startswith("==="):
            color = "#58a6ff"
        line = f'<span style="color:{color}">{esc}</span>' if color else esc
        self.appendHtml(line)
        self.moveCursor(QTextCursor.MoveOperation.End)


class SizeItem(QTableWidgetItem):
    """Table cell that displays a human size but sorts by the raw byte count."""
    def __init__(self, nbytes: int):
        super().__init__(human_bytes(nbytes) if nbytes else "?")
        self._n = nbytes

    def __lt__(self, other):
        if isinstance(other, SizeItem):
            return self._n < other._n
        return super().__lt__(other)
