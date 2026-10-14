"""Regression tests for locale-sensitive crashes in nexus_core.deployer.

Observed 2026-07-16: the game hard-crashed a few seconds into loading a save with

    File: ...\\common\\redConfig\\src\\inGameConfigGroup.cpp   Line: 76
    Message: Could not find in-game config var 'Text' in group '/language'

DedraExoticDancers/modules/translation_multilang.lua probes the candidate list
{OnScreen, Text, Subtitle, VoiceOver}, returning on the first locale it can normalise. Players
on a supported locale return at OnScreen and never reach the landmine; this user's ua-ua is
absent from the mod's 20-locale table, so they fall through to `Text` and die. `Text` does not
exist and `Subtitle` is a typo for `Subtitles`.

The crash is a C++ engine assert, so the mod's own pcall cannot catch it — which is why
detecting the probe statically is the only defence. Pure stdlib — no PySide6, no network.
"""
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

CET = "bin/x64/plugins/cyber_engine_tweaks/mods"


def _lua(game: Path, mod: str, text: str, name: str = "init.lua") -> Path:
    p = game / CET / mod / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _options(game: Path, names, rel="r6/config/settings/options.json"):
    p = game / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"groups": [
        {"group_name": "/gameplay", "vars": [{"name": "Difficulty"}]},
        {"group_name": "/language", "vars": [{"name": n} for n in names]},
    ]}), encoding="utf-8")
    return p


def _bugs(game: Path, valid=None):
    return {(b["caller"], b["var"]): (b["verdict"], b["suggestion"])
            for b in D.find_language_var_bugs(game, valid)}


# ── the reported crash ───────────────────────────────────────────────────────

def test_the_exact_reported_candidate_list_is_caught(tmp_path):
    """The real file stores these as TABLE LITERALS and passes them to GetVar elsewhere. A
    scanner matching only GetVar("/language", ...) would miss the very bug it exists for."""
    game = tmp_path / "game"
    _lua(game, "DedraExoticDancers",
         'local candidates = {\n'
         '  {"/language", "OnScreen"},\n'
         '  {"/language", "Text"},\n'
         '  {"/language", "Subtitle"},\n'
         '  {"/language", "VoiceOver"},\n'
         '}\n'
         'for _, c in ipairs(candidates) do local v = GetVar(c[1], c[2]) end\n',
         name="modules/translation_multilang.lua")
    got = _bugs(game)
    assert set(got) == {("DedraExoticDancers", "Text"), ("DedraExoticDancers", "Subtitle")}
    assert got[("DedraExoticDancers", "Text")] == ("unknown", "")
    assert got[("DedraExoticDancers", "Subtitle")] == ("typo", "Subtitles")


def test_the_valid_four_are_never_flagged(tmp_path):
    game = tmp_path / "game"
    _lua(game, "GoodMod",
         'GetVar("/language", "OnScreen")\nGetVar("/language", "Subtitles")\n'
         'GetVar("/language", "VoiceOver")\nGetVar("/language", "Platform")\n')
    assert D.find_language_var_bugs(game) == []


def test_the_fallback_var_list_is_the_documented_four():
    assert set(D.FALLBACK_LANGUAGE_VARS) == {"VoiceOver", "Subtitles", "OnScreen", "Platform"}


# ── the valid list comes from the game, not from us ──────────────────────────

def test_valid_vars_are_read_from_the_games_own_options_json(tmp_path):
    """Same principle as reading the redscript log instead of re-running the compiler: the
    engine's own config is the authority on what the engine defines."""
    game = tmp_path / "game"
    _options(game, ["OnScreen", "Subtitles", "VoiceOver", "SomethingNew"])
    assert D.game_language_vars(game) == ("OnScreen", "SomethingNew", "Subtitles", "VoiceOver")
    _lua(game, "M", 'GetVar("/language", "SomethingNew")\n')
    assert D.find_language_var_bugs(game) == []          # real here, though not in our list


def test_the_platform_options_file_is_merged_in(tmp_path):
    game = tmp_path / "game"
    _options(game, ["OnScreen"])
    _options(game, ["Platform"], rel="r6/config/settings/platform/pc/options.json")
    assert D.game_language_vars(game) == ("OnScreen", "Platform")


def test_unreadable_options_fall_back_and_never_return_empty(tmp_path):
    """An empty valid-set would mark every probe in every mod as a crash — the worst possible
    failure for a check whose whole job is to be trusted."""
    game = tmp_path / "game"
    game.mkdir()
    assert D.game_language_vars(game) == D.FALLBACK_LANGUAGE_VARS
    (game / "r6/config/settings").mkdir(parents=True)
    (game / "r6/config/settings/options.json").write_text("{ this is not json")
    assert D.game_language_vars(game) == D.FALLBACK_LANGUAGE_VARS
    _options(game, [])                                    # present but no /language vars
    assert D.game_language_vars(game) == D.FALLBACK_LANGUAGE_VARS


# ── parsing forms and false positives ────────────────────────────────────────

def test_all_three_reference_forms_parse():
    assert D.parse_language_vars('GetVar("/language", "A")') == {"A"}
    assert D.parse_language_vars('{"/language", "B"}') == {"B"}
    assert D.parse_language_vars("GameOptions.Get('/language/C')") == {"C"}


def test_commented_out_probes_are_ignored(tmp_path):
    game = tmp_path / "game"
    _lua(game, "M",
         '-- GetVar("/language", "Text")\n'
         '--[[ {"/language", "Subtitle"} ]]\n'
         'GetVar("/language", "OnScreen")\n')
    assert D.find_language_var_bugs(game) == []


def test_an_unrelated_config_group_is_not_touched(tmp_path):
    game = tmp_path / "game"
    _lua(game, "M", 'GetVar("/gameplay", "Text")\nGetVar("/audio", "Volume")\n')
    assert D.find_language_var_bugs(game) == []


def test_case_wrong_var_is_reported_as_a_typo(tmp_path):
    """The engine matches these exactly; 'subtitles' asserts just as hard as 'Text'."""
    game = tmp_path / "game"
    _lua(game, "M", 'GetVar("/language", "subtitles")\n')
    assert _bugs(game)[("M", "subtitles")] == ("typo", "Subtitles")


def test_no_cet_folder_is_not_an_error(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    assert D.find_language_var_bugs(game) == []
    assert D.find_locale_gaps(game, "ua-ua") == []


# ── locale coverage ──────────────────────────────────────────────────────────

def test_a_mod_without_the_users_locale_is_reported(tmp_path):
    game = tmp_path / "game"
    _lua(game, "Translated",
         'local T = { ["en-us"] = {}, ["pl-pl"] = {}, ["de-de"] = {}, ["fr-fr"] = {} }\n')
    gaps = D.find_locale_gaps(game, "ua-ua")
    assert [g["caller"] for g in gaps] == ["Translated"]
    assert "en-us" in gaps[0]["locales"]


def test_a_mod_listing_the_users_locale_is_not_reported(tmp_path):
    game = tmp_path / "game"
    _lua(game, "Translated",
         'local T = { ["en-us"] = {}, ["pl-pl"] = {}, ["ua-ua"] = {} }\n')
    assert D.find_locale_gaps(game, "ua-ua") == []


def test_fewer_than_three_locale_codes_is_not_a_locale_table(tmp_path):
    """One or two xx-yy strings happen by accident (a default, a comment, an example). Three
    distinct ones is a translation map."""
    game = tmp_path / "game"
    _lua(game, "M", 'local fallback = "en-us"\nlocal other = "pl-pl"\n')
    assert D.find_locale_gaps(game, "ua-ua") == []


def test_no_configured_locale_checks_nothing(tmp_path):
    """Better to check nothing than to guess the player's language wrongly and report every
    translated mod in the collection."""
    game = tmp_path / "game"
    _lua(game, "M", 'local T = { ["en-us"]={}, ["pl-pl"]={}, ["de-de"]={} }\n')
    assert D.find_locale_gaps(game, "") == []


def test_the_dangerous_combination_is_called_out(tmp_path):
    """A bad probe plus no strings for the user's locale is the reported crash exactly: the
    landmine sits on the branch that an unlisted locale takes."""
    game = tmp_path / "game"
    _lua(game, "DedraExoticDancers",
         'local T = { ["en-us"]={}, ["pl-pl"]={}, ["ru-ru"]={} }\n'
         'local v = GetVar("/language", "Text")\n')
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_locale(game, "ua-ua")
    out = buf.getvalue()
    assert n == 1
    assert "no strings for your locale" in out
    assert "crashes for you and for nobody else" in out


# ── report and read-only guarantee ───────────────────────────────────────────

def test_report_emits_a_parseable_line_the_gui_matches(tmp_path):
    game = tmp_path / "game"
    _lua(game, "Bad", 'GetVar("/language", "Text")\n')
    _lua(game, "Trans", 'local T={["en-us"]={},["pl-pl"]={},["de-de"]={}}\n')
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_locale(game, "ua-ua")
    out = buf.getvalue()
    assert n == 1
    assert "[locale] 1 invalid /language probe(s); 1 mod(s) missing 'ua-ua'" in out
    import re
    pat = re.compile(r"^\[locale\]\s+(\d+)\s+invalid /language probe\(s\);\s+"
                     r"(\d+)\s+mod\(s\) missing")
    line = next(ln for ln in out.splitlines() if ln.startswith("[locale]"))
    m = pat.match(line)
    assert m is not None and m.groups() == ("1", "1")


def test_a_clean_install_says_nothing_alarming(tmp_path):
    game = tmp_path / "game"
    _lua(game, "M", 'GetVar("/language", "OnScreen")\n')
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_locale(game, "ua-ua")
    assert n == 0
    assert buf.getvalue().strip() == "[locale] 0 invalid /language probe(s); 0 mod(s) missing 'ua-ua'"


def test_gui_regex_is_the_one_shipped_in_deploy_tab():
    src = (Path(__file__).resolve().parent.parent / "gui" / "deploy_tab.py").read_text()
    assert "RE_LOCALE" in src and "invalid /language probe" in src
    assert "self._locale" in src


def _fingerprint(root: Path) -> dict:
    import hashlib
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns,
                           hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def test_the_check_never_mutates_the_install(tmp_path):
    game = tmp_path / "game"
    _options(game, ["OnScreen", "Subtitles", "VoiceOver"])
    _lua(game, "Bad", 'GetVar("/language", "Text")\n')
    before = _fingerprint(game)
    with contextlib.redirect_stdout(io.StringIO()):
        D.report_locale(game, "ua-ua")
    assert _fingerprint(game) == before


def test_the_recorded_locale_goes_to_config_dir_not_the_game(tmp_path):
    game = tmp_path / "game"
    _lua(game, "M", 'GetVar("/language", "OnScreen")\n')
    old_dir, old_file = D.CONFIG_DIR, D.CONFIG_FILE
    try:
        D.CONFIG_DIR = tmp_path / "cfg"
        D.CONFIG_FILE = D.CONFIG_DIR / "config.json"
        before = _fingerprint(game)
        with contextlib.redirect_stdout(io.StringIO()):
            D.action_check_locale(game, "ua-ua")
        assert _fingerprint(game) == before                     # game untouched
        assert json.loads(D.CONFIG_FILE.read_text())["locale"] == "ua-ua"
        assert D.configured_locale() == "ua-ua"
    finally:
        D.CONFIG_DIR, D.CONFIG_FILE = old_dir, old_file


def test_check_locale_cli(tmp_path):
    game = tmp_path / "game"
    _lua(game, "DedraExoticDancers",
         'local c = { {"/language", "Text"}, {"/language", "Subtitle"} }\n',
         name="modules/translation_multilang.lua")
    D.save_manifest(game, {"Dedra-1-1-0-1700000000":
                           [f"{CET}/DedraExoticDancers/modules/translation_multilang.lua"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-locale"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[locale] 2 invalid /language probe(s)" in r.stdout
    assert 'did it mean "Subtitles"?' in r.stdout


def test_check_health_includes_the_locale_line(tmp_path):
    game = tmp_path / "game"
    _lua(game, "M", 'GetVar("/language", "OnScreen")\n')
    D.save_manifest(game, {"M-1-1-0-1700000000": [f"{CET}/M/init.lua"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[locale] 0 invalid /language probe(s)" in r.stdout


if __name__ == "__main__":
    import inspect
    import tempfile
    n = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        kw = {"tmp_path": Path(tempfile.mkdtemp())} \
            if "tmp_path" in inspect.signature(fn).parameters else {}
        fn(**kw)
        print("PASS", name); n += 1
    print(f"{n} locale-sensitivity tests passed")
