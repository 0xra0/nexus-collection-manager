from __future__ import annotations

import ast
from pathlib import Path

from gui.i18n import LANG_EN, LANG_UK, UK, normalize_language, set_current_language, tr


def test_language_normalization():
    assert normalize_language("uk-UA") == LANG_UK
    assert normalize_language("uk_UA") == LANG_UK
    assert normalize_language("Українська") == LANG_UK
    assert normalize_language("en-US") == LANG_EN
    assert normalize_language("") == LANG_EN


def test_english_is_canonical_fallback_and_ukrainian_translates():
    source = "Check for updates"
    assert tr(source, LANG_EN) == source
    assert tr(source, LANG_UK) == "Перевірити оновлення"
    # Nexus/mod-authored text not in the catalogue is intentionally untouched.
    authored = "My Totally Custom Cyberpunk Mod"
    assert tr(authored, LANG_UK) == authored


def test_runtime_language_selection():
    set_current_language(LANG_UK)
    assert tr("Install") == "Встановити"
    set_current_language(LANG_EN)
    assert tr("Install") == "Install"


def test_generated_game_root_message_is_localized_without_translating_path():
    source = "Game root not found:\n/mnt/games/Cyberpunk 2077"
    assert tr(source, LANG_UK) == "Кореневу папку гри не знайдено:\n/mnt/games/Cyberpunk 2077"


def _static_ui_strings() -> set[str]:
    calls = {
        "QLabel", "QPushButton", "QGroupBox", "QCheckBox", "QRadioButton",
        "QAction", "QLineEdit", "QInputDialog",
    }
    methods = {
        "setWindowTitle", "setTitle", "setSubTitle", "setPlaceholderText",
        "setToolTip", "addAction", "addTab", "setText", "setStatusTip",
        "setWhatsThis", "setHorizontalHeaderLabels", "showMessage",
        "information", "warning", "critical", "question", "addRow", "addItem",
    }
    out: set[str] = set()
    root = Path(__file__).resolve().parents[1] / "gui"
    for path in root.glob("*.py"):
        if path.name == "i18n.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                continue
            if name not in calls and name not in methods:
                continue
            for arg in node.args[:3]:
                values = []
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    values = [arg.value]
                elif isinstance(arg, (ast.List, ast.Tuple)):
                    values = [
                        elt.value for elt in arg.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    ]
                for value in values:
                    value = value.strip()
                    if value and any(ch.isalpha() for ch in value) and not value.startswith(("#", "http", "Q")):
                        out.add(value)
    return out


def test_all_static_gui_strings_have_a_ukrainian_catalogue_entry():
    # Technical tokens that remain visually identical (FOMOD, SHA-256, paths, etc.)
    # are still explicit catalogue entries. This makes untranslated new GUI chrome
    # visible as a test failure instead of silently reducing language coverage.
    missing = sorted(text for text in _static_ui_strings() if text not in UK)
    assert missing == []


def test_qt_widget_types_used_by_translator_are_imported():
    """Catch NameError regressions in the optional PySide6 integration block."""
    path = Path(__file__).resolve().parents[1] / "gui" / "i18n.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("PySide6."):
            imported.update(alias.asname or alias.name for alias in node.names)

    referenced_types: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "isinstance"):
            continue
        if len(node.args) < 2:
            continue
        type_arg = node.args[1]
        candidates = type_arg.elts if isinstance(type_arg, ast.Tuple) else [type_arg]
        for candidate in candidates:
            if isinstance(candidate, ast.Name) and candidate.id.startswith("Q"):
                referenced_types.add(candidate.id)

    missing = sorted(referenced_types - imported)
    assert missing == [], f"Qt types referenced by i18n.py but not imported: {missing}"


def test_event_filter_contains_translation_exceptions():
    path = Path(__file__).resolve().parents[1] / "gui" / "i18n.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    manager = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "LanguageManager"
    )
    event_filter = next(
        node for node in manager.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "eventFilter"
    )
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(event_filter)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    assert "_safe_translate_object" in calls
    assert any(isinstance(node, ast.Try) for node in ast.walk(event_filter))


def test_i18n_has_no_periodic_full_tree_retranslation_timer():
    """A periodic recursive walk made the 17-tab GUI unresponsive on real Qt."""
    path = Path(__file__).resolve().parents[1] / "gui" / "i18n.py"
    source = path.read_text(encoding="utf-8")
    assert "timeout.connect(self.retranslate_all)" not in source
    assert "setInterval(650)" not in source


def test_event_filter_uses_shallow_translation_for_runtime_events():
    path = Path(__file__).resolve().parents[1] / "gui" / "i18n.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    manager = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "LanguageManager"
    )
    event_filter = next(
        node for node in manager.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "eventFilter"
    )
    recursive_values = []
    for node in ast.walk(event_filter):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "recursive" and isinstance(kw.value, ast.Constant):
                    recursive_values.append(kw.value.value)
    assert recursive_values and all(value is False for value in recursive_values)
