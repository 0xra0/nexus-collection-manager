from pathlib import Path


def test_gui_header_configuration_uses_compat_helper():
    gui = Path(__file__).resolve().parents[1] / "gui"
    offenders = []
    for path in sorted(gui.glob("*.py")):
        if path.name == "widgets.py":
            continue
        text = path.read_text(encoding="utf-8")
        if ".horizontalHeader()" in text:
            offenders.append(path.name)
    assert not offenders, f"direct horizontalHeader() calls bypass compatibility helper: {offenders}"


def test_atspi_bridge_is_disabled_before_pyside_import():
    main = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    root_guard = main.index('os.geteuid() == 0')
    env_pos = main.index('os.environ["NO_AT_BRIDGE"] = "1"')
    qt_pos = main.index("from PySide6.QtWidgets")
    assert root_guard < env_pos < qt_pos
