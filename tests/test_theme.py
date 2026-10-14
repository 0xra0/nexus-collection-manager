from pathlib import Path
from gui.theme import (
    DEFAULT_THEME,
    THEMES,
    THEME_CYBERPUNK,
    THEME_QT_DARK,
    THEME_QT_DEFAULT,
    THEME_QT_WHITE,
    normalize_theme,
    theme_label,
)


def test_theme_catalogue_has_requested_themes():
    assert THEMES == (
        THEME_QT_DEFAULT,
        THEME_QT_DARK,
        THEME_QT_WHITE,
        THEME_CYBERPUNK,
    )
    assert theme_label(THEME_QT_DEFAULT) == "Qt Default"
    assert theme_label(THEME_QT_DARK) == "Qt Dark"
    assert theme_label(THEME_QT_WHITE) == "Qt White"
    assert theme_label(THEME_CYBERPUNK) == "Cyberpunk 2077"


def test_theme_alias_normalization():
    assert normalize_theme("qt") == THEME_QT_DEFAULT
    assert normalize_theme("dark") == THEME_QT_DARK
    assert normalize_theme("light") == THEME_QT_WHITE
    assert normalize_theme("cyberpunk") == THEME_CYBERPUNK


def test_unknown_theme_falls_back_to_stable_default():
    assert DEFAULT_THEME == THEME_QT_DARK
    assert normalize_theme("not-a-theme") == DEFAULT_THEME


def test_all_custom_theme_qss_tokens_are_resolved():
    from gui.theme import _CYBERPUNK, _DARK, _WHITE, _qss

    for tokens in (_DARK, _WHITE, _CYBERPUNK):
        rendered = _qss(tokens)
        assert "@BG@" not in rendered
        assert "@DANGER@" not in rendered
        assert "@RADIUS@" not in rendered


def test_cyberpunk_palette_uses_signature_high_contrast_accents():
    from gui.theme import _CYBERPUNK

    assert _CYBERPUNK["ACCENT"].lower() == "#fcee0a"
    assert _CYBERPUNK["FOCUS"].lower() == "#00f0ff"
    assert _CYBERPUNK["DANGER"].lower() == "#ff003c"
    assert _CYBERPUNK["RADIUS"] == "0px"


def test_root_linux_launch_forces_atspi_bridge_off():
    main = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert 'os.environ["NO_AT_BRIDGE"] = "1"' in main
    assert 'os.environ.setdefault("NO_AT_BRIDGE", "1")' not in main
