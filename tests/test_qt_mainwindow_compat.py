from pathlib import Path


def test_main_window_uses_compat_helpers_for_typed_accessors():
    root = Path(__file__).resolve().parents[1]
    text = (root / "gui" / "main_window.py").read_text(encoding="utf-8")
    assert ".menuBar()" not in text
    assert ".statusBar()" not in text
    assert "safe_menu_bar(self)" in text
    assert "safe_status_bar(self)" in text
    assert "safe_add_menu(" in text
    assert "safe_add_action(" in text


def test_qt_compat_helpers_construct_explicit_subtypes():
    text = (Path(__file__).resolve().parents[1] / "gui" / "widgets.py").read_text(encoding="utf-8")
    assert "replacement = QMenuBar(window)" in text
    assert "window.setMenuBar(replacement)" in text
    assert "window.menuBar()" not in text
    assert "menu = QMenu(title, parent)" in text
    assert "action = QAction(text, parent)" in text
    assert "replacement = QStatusBar(window)" in text
    assert "window.setStatusBar(replacement)" in text
    assert "window.statusBar()" not in text


def test_root_launch_disables_qt_accessibility_before_pyside_import():
    main = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    qt_import = main.index("from PySide6.QtWidgets")
    for statement in (
        'os.environ["NO_AT_BRIDGE"] = "1"',
        'os.environ["QT_ACCESSIBILITY"] = "0"',
        'os.environ["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "0"',
    ):
        assert main.index(statement) < qt_import
