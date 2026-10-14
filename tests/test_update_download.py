"""Regression tests for the mod update checker and one-click download.

The precise version comparison (check_file_update) already existed; these tests cover the
layer added on top of it: a structured scan (scan_updates), machine-readable [update]/
[updates] lines, and a download action that fetches the newer file INTO Downloads named so
the next deploy retires the old version.

The load-bearing correctness point is canonical_update_filename: a file downloaded by id
from a mod page carries the uploader's own display name, which shares NO identity with the
installed collection stem, so a naive fetch would leave BOTH versions installed. The
synthesized <name>-<modid>-<ver>-<epoch> name round-trips through mod_identity() to exactly
the installed mod, with a higher epoch, so drop_superseded retires the old one.

Deliberately NOT folded into --check-health (network + key + rate limit vs. every other
health probe being a local instant read); one test locks that decision in. The downloader
is fully mocked — no network, no cookies, no game folder. Pure stdlib, no PySide6.
"""
import ast
import contextlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# An installed mod (Night City Estate, id 12857) at v1.0 that Nexus has updated to v1.2,
# plus a second mod (JoyRide, id 40000) that is current. Collection-format stems.
STEM_UPD = "Night City Estate-12857-1-0-1700000000"
STEM_CUR = "GTA JoyRide-40000-2-0-1700000500"

PAYLOADS = {
    "12857": {
        "files": [
            {"file_id": 900, "uploaded_timestamp": 1700000000, "version": "1.0",
             "category_name": "OLD_VERSION", "file_name": "NCestate-1-0.zip",
             "size_in_bytes": 5_000_000},
            {"file_id": 950, "uploaded_timestamp": 1700009999, "version": "1.2",
             "category_name": "MAIN", "file_name": "Night City Estate 1.2.7z",
             "size_in_bytes": 6_500_000},
        ],
        "file_updates": [{"old_file_id": 900, "new_file_id": 950}],
    },
    "40000": {
        "files": [
            {"file_id": 700, "uploaded_timestamp": 1700000500, "version": "2.0",
             "category_name": "MAIN", "file_name": "JoyRide-2-0.zip",
             "size_in_bytes": 9_000_000},
        ],
        "file_updates": [],
    },
}


def _game(tmp_path: Path) -> Path:
    game = tmp_path / "game"
    game.mkdir(parents=True, exist_ok=True)
    manifest = {"game_root": str(game), "updated": "now",
                "mods": {STEM_UPD: ["archive/pc/mod/nce.archive"],
                         STEM_CUR: ["archive/pc/mod/joyride.archive"]}}
    (game / ".mod_manifest.json").write_text(json.dumps(manifest))
    return game


def _mock(monkeypatch):
    monkeypatch.setattr(D, "fetch_nexus_files_full",
                        lambda mod_id, key: PAYLOADS.get(str(mod_id)))


def _gui_regex(name: str):
    """Compile the regex assigned to `name` in gui/deploy_tab.py, read straight from source
    (adjacent string literals are one Constant after parse) — no PySide6 import needed."""
    tree = ast.parse((REPO / "gui" / "deploy_tab.py").read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and node.targets
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            call = node.value
            assert isinstance(call, ast.Call), f"{name} is not a call"
            pat = call.args[0]
            assert isinstance(pat, ast.Constant) and isinstance(pat.value, str), \
                f"{name} pattern is not a string literal"
            return re.compile(pat.value)
    raise AssertionError(f"gui/deploy_tab.py has no {name}")


# ── scan_updates ──────────────────────────────────────────────────────────────

def test_scan_finds_update_and_skips_current(tmp_path, monkeypatch):
    _mock(monkeypatch)
    scan = D.scan_updates(_game(tmp_path), "KEY")
    assert scan["checked"] == 2
    assert scan["current"] == 1
    assert scan["unknown"] == []
    assert len(scan["updates"]) == 1
    r = scan["updates"][0]
    assert r["stem"] == STEM_UPD
    assert r["mod_id"] == "12857"
    assert r["installed_ver"] == "1.0" and r["latest_ver"] == "1.2"
    assert r["file_id"] == 950            # chain end, so a download can target it
    assert r["size"] == 6_500_000
    assert r["epoch"] == 1700009999


def test_scan_only_filter_by_modid_and_stem(tmp_path, monkeypatch):
    _mock(monkeypatch)
    game = _game(tmp_path)
    by_id = D.scan_updates(game, "KEY", only="12857")
    assert by_id["checked"] == 1 and len(by_id["updates"]) == 1
    by_stem = D.scan_updates(game, "KEY", only="night city")
    assert by_stem["checked"] == 1 and len(by_stem["updates"]) == 1
    # A mod that is current -> checked but no update.
    cur = D.scan_updates(game, "KEY", only="joyride")
    assert cur["checked"] == 1 and len(cur["updates"]) == 0


def test_scan_unknown_when_not_on_nexus(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "fetch_nexus_files_full", lambda m, k: None)
    scan = D.scan_updates(_game(tmp_path), "KEY")
    assert len(scan["unknown"]) == 2 and scan["updates"] == []


def test_scan_ignores_unparseable_stems(tmp_path, monkeypatch):
    _mock(monkeypatch)
    game = tmp_path / "g"
    game.mkdir()
    (game / ".mod_manifest.json").write_text(json.dumps(
        {"game_root": str(game), "updated": "now",
         "mods": {"some hand-installed folder": ["r6/scripts/x.reds"]}}))
    scan = D.scan_updates(game, "KEY")
    assert scan["checked"] == 0
    assert len(scan["unknown"]) == 1 and "cannot parse" in scan["unknown"][0][1]


# ── canonical_update_filename (the load-bearing bit) ─────────────────────────

def test_canonical_name_roundtrips_to_same_identity_higher_epoch():
    dl = D.canonical_update_filename(STEM_UPD, "12857", "1.2", 1700009999, ".7z")
    assert dl == "Night City Estate-12857-1.2-1700009999.7z"
    key_new = D.nexus_key_from_stem(Path(dl).stem)
    key_old = D.nexus_key_from_stem(STEM_UPD)
    assert key_new is not None and key_old is not None, \
        "synthesized name must parse via NEXUS_NAME_RE"
    assert D.mod_identity(Path(dl).stem) == D.mod_identity(STEM_UPD), \
        "same mod_identity -> a later deploy supersedes the installed version"
    assert key_new[2] > key_old[2], "newer epoch"


def test_canonical_name_preserves_extension():
    assert D.canonical_update_filename(STEM_UPD, "12857", "1.2", 1700009999, ".rar") \
        .endswith(".rar")
    assert D.canonical_update_filename(STEM_UPD, "12857", "1.2", 1700009999, "") \
        .endswith(".zip")                 # sensible default when Nexus gives no suffix


def test_canonical_name_falls_back_when_epoch_unparseable():
    # A non-unix-looking timestamp: keep a readable name, but it just won't auto-supersede.
    dl = D.canonical_update_filename(STEM_UPD, "12857", "1.2", 3000, ".zip")
    assert dl == "Night City Estate-12857-1.2.zip"
    assert D.nexus_key_from_stem(Path(dl).stem) is None


def test_canonical_name_sanitizes_version_token():
    dl = D.canonical_update_filename(STEM_UPD, "12857", "v1.2 hotfix", 1700009999, ".zip")
    assert " " not in Path(dl).stem.split("-12857-")[1]   # version made a single token


# ── report_updates: machine lines ────────────────────────────────────────────

def _report_lines(scan):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        n = D.report_updates(scan)
    return n, buf.getvalue()


def test_report_emits_update_rows_and_summary(tmp_path, monkeypatch):
    _mock(monkeypatch)
    n, out = _report_lines(D.scan_updates(_game(tmp_path), "KEY"))
    assert n == 1
    assert "[updates] 1 installed mod(s) have a newer version on Nexus" in out
    row = next(l for l in out.splitlines() if l.startswith("[update] "))
    assert "mod_id=12857" in row and "file_id=950" in row and "size=6500000" in row
    assert "v1.0 -> v1.2" in row
    assert "dl=Night City Estate-12857-1.2-1700009999.7z" in row


def test_report_zero_updates_still_emits_summary(tmp_path, monkeypatch):
    _mock(monkeypatch)
    scan = D.scan_updates(_game(tmp_path), "KEY", only="joyride")
    n, out = _report_lines(scan)
    assert n == 0
    assert "[updates] 0 installed mod(s) have a newer version on Nexus" in out


# ── download action (downloader fully mocked) ────────────────────────────────

def test_download_dry_run_plans_without_downloader(tmp_path, monkeypatch):
    _mock(monkeypatch)
    game = _game(tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.action_download_updates(game, tmp_path / "Downloads", "KEY", dry_run=True)
    out = buf.getvalue()
    assert "[download-plan] 1 update(s) would be fetched" in out
    assert not (tmp_path / "Downloads" / "Night City Estate-12857-1.2-1700009999.7z").exists()


def test_download_real_path_calls_downloader_and_lands_file(tmp_path, monkeypatch):
    _mock(monkeypatch)
    import nexus_core.downloader as DL
    calls = []
    monkeypatch.setattr(DL, "is_complete", lambda dest, size: False)

    def fake_dl(mod, dest, conns):
        calls.append((mod["fileName"], mod["modId"], mod["fileId"], mod["size"], conns))
        Path(dest).write_bytes(b"x")
        return True, ""

    monkeypatch.setattr(DL, "download_with_retries", fake_dl)
    game = _game(tmp_path)
    dldir = tmp_path / "Downloads"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.action_download_updates(game, dldir, "KEY")
    out = buf.getvalue()
    assert "[downloaded] 1 of 1 update(s) fetched" in out
    assert calls == [("Night City Estate-12857-1.2-1700009999.7z",
                      12857, 950, 6_500_000, D.UPDATE_DL_CONNS)]
    assert (dldir / "Night City Estate-12857-1.2-1700009999.7z").exists()


def test_download_skips_already_present(tmp_path, monkeypatch):
    _mock(monkeypatch)
    import nexus_core.downloader as DL
    monkeypatch.setattr(DL, "is_complete", lambda dest, size: True)   # pretend on disk

    def boom(mod, dest, conns):
        raise AssertionError("must not download when already complete")

    monkeypatch.setattr(DL, "download_with_retries", boom)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.action_download_updates(_game(tmp_path), tmp_path / "Downloads", "KEY")
    out = buf.getvalue()
    assert "[skip] already present" in out and "[downloaded] 1 of 1" in out


def test_download_reports_partial_failure(tmp_path, monkeypatch):
    _mock(monkeypatch)
    import nexus_core.downloader as DL
    monkeypatch.setattr(DL, "is_complete", lambda dest, size: False)
    monkeypatch.setattr(DL, "download_with_retries",
                        lambda mod, dest, conns: (False, "CDN 403"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.action_download_updates(_game(tmp_path), tmp_path / "Downloads", "KEY")
    out = buf.getvalue()
    assert "[fail]" in out and "[downloaded] 0 of 1 update(s) fetched" in out


def test_download_no_key_guard(tmp_path):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.action_download_updates(_game(tmp_path), tmp_path / "Downloads", "")
    assert "No NexusMods API key" in buf.getvalue()


def test_download_no_match_guard(tmp_path, monkeypatch):
    _mock(monkeypatch)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.action_download_updates(_game(tmp_path), tmp_path / "Downloads", "KEY",
                                  only="nonexistent")
    out = buf.getvalue()
    assert "No updates to download matching 'nonexistent'" in out
    assert "[downloaded] 0 of 0" in out


def test_update_download_mod_shape():
    row = {"mod_id": "12857", "file_id": "950", "size": 6_500_000,
           "download_name": "X-12857-1.2-1700009999.7z"}
    mod = D.update_download_mod(row)
    assert mod == {"modId": 12857, "fileId": 950, "size": 6_500_000,
                   "fileName": "X-12857-1.2-1700009999.7z"}


# ── GUI stays in sync with the backend's machine lines ───────────────────────

def test_gui_regex_parses_update_row(tmp_path, monkeypatch):
    _mock(monkeypatch)
    _n, out = _report_lines(D.scan_updates(_game(tmp_path), "KEY"))
    row = next(l for l in out.splitlines() if l.startswith("[update] "))
    m = _gui_regex("RE_UPDATE_ROW").match(row)
    assert m, f"GUI RE_UPDATE_ROW does not match backend line:\n{row}"
    assert m.group("stem") == STEM_UPD
    assert m.group("mod") == "12857" and m.group("file") == "950"
    assert m.group("size") == "6500000"
    assert m.group("iv") == "1.0" and m.group("lv") == "1.2"
    assert m.group("dl") == "Night City Estate-12857-1.2-1700009999.7z"


def test_gui_regex_parses_updates_summary(tmp_path, monkeypatch):
    _mock(monkeypatch)
    _n, out = _report_lines(D.scan_updates(_game(tmp_path), "KEY"))
    summary = next(l for l in out.splitlines() if l.startswith("[updates] "))
    m = _gui_regex("RE_UPDATES").match(summary)
    assert m and m.group(1) == "1"


def test_gui_regex_parses_downloaded_line(tmp_path, monkeypatch):
    _mock(monkeypatch)
    import nexus_core.downloader as DL
    monkeypatch.setattr(DL, "is_complete", lambda dest, size: False)
    monkeypatch.setattr(DL, "download_with_retries",
                        lambda mod, dest, conns: (Path(dest).write_bytes(b"x"), (True, ""))[1])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.action_download_updates(_game(tmp_path), tmp_path / "Downloads", "KEY")
    line = next(l for l in buf.getvalue().splitlines() if l.startswith("[downloaded] "))
    m = _gui_regex("RE_DOWNLOADED").match(line)
    assert m and m.group(1) == "1" and m.group(2) == "1"


# ── the deliberate design decision: update-check is NOT part of health ───────

def test_check_health_does_not_run_updates(tmp_path):
    """Update checking is a network + key + rate-limited operation; every other health probe
    is a local instant read. This asserts --check-health never emits an [updates] line, so
    the default health sweep stays offline and fast."""
    game = _game(tmp_path)
    r = subprocess.run(
        [sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
         "--check-health"],
        cwd=str(REPO), capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[updates]" not in r.stdout
    assert "[update]" not in r.stdout


if __name__ == "__main__":
    import inspect
    import tempfile

    class _MP:
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def undo(self):
            for obj, name, val in reversed(self._undo):
                setattr(obj, name, val)

    n = 0
    for _name, fn in sorted(globals().items()):
        if not (_name.startswith("test_") and callable(fn)):
            continue
        params = inspect.signature(fn).parameters
        kw = {}
        if "tmp_path" in params:
            kw["tmp_path"] = Path(tempfile.mkdtemp())
        mp = _MP()
        if "monkeypatch" in params:
            kw["monkeypatch"] = mp
        try:
            fn(**kw)
            print("PASS", _name); n += 1
        finally:
            mp.undo()
    print(f"{n} update-download tests passed")
