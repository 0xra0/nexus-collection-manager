"""Regression tests for NexusMods update checking in nexus_core.deployer.

The precise check matches the installed file on Nexus (by upload timestamp, tolerant
of the collection format's minute truncation; version as fallback) and follows the
file_updates chain — so a file is compared only against its own successors, never an
unrelated 'main' file of the same mod (which caused false "update available"). Mocked,
offline.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402


def _files_payload():
    # An old main file (v1.0) that was updated twice, plus a standalone optional file.
    return {
        "files": [
            {"file_id": 10, "uploaded_timestamp": 1000, "version": "1.0",
             "category_name": "OLD_VERSION", "file_name": "Mod-1-0-1000.zip"},
            {"file_id": 11, "uploaded_timestamp": 2000, "version": "1.1",
             "category_name": "OLD_VERSION", "file_name": "Mod-1-1-2000.zip"},
            {"file_id": 12, "uploaded_timestamp": 3000, "version": "2.0",
             "category_name": "MAIN", "file_name": "Mod-2-0-3000.zip"},
            {"file_id": 50, "uploaded_timestamp": 4000, "version": "2.29",
             "category_name": "OPTIONAL", "file_name": "Extra-2-29-4000.zip"},
        ],
        "file_updates": [
            {"old_file_id": 10, "new_file_id": 11},
            {"old_file_id": 11, "new_file_id": 12},
        ],
    }


def _mock(monkeypatch):
    monkeypatch.setattr(D, "fetch_nexus_files_full", lambda mod_id, key: _files_payload())


def test_old_file_reports_update_to_chain_end(tmp_path, monkeypatch):
    _mock(monkeypatch)
    status, detail = D.check_file_update("1", 1000, "1.0", "KEY", {})
    assert status == "update" and isinstance(detail, dict)
    assert detail["installed_ver"] == "1.0" and detail["latest_ver"] == "2.0"
    assert detail["latest_file"] == "Mod-2-0-3000.zip"
    assert detail["file_id"] == 12                 # chain end, so a download can target it
    assert detail["epoch"] == 3000


def test_current_main_file_is_current(tmp_path, monkeypatch):
    _mock(monkeypatch)
    status, detail = D.check_file_update("1", 3000, "2.0", "KEY", {})
    assert status == "current"


def test_standalone_optional_not_flagged_by_main_version(tmp_path, monkeypatch):
    """The bug: an OPTIONAL file with its own version must not be reported outdated just
    because the mod's main file has a higher version."""
    _mock(monkeypatch)
    status, _detail = D.check_file_update("1", 4000, "2.29", "KEY", {})
    assert status == "current"


def test_minute_truncated_timestamp_matches_within_window(tmp_path, monkeypatch):
    """Collection-format names carry a minute-floored timestamp; a file uploaded at
    :35s must still match when the installed epoch is the minute floor."""
    _mock(monkeypatch)
    payload = _files_payload()
    payload["files"][2]["uploaded_timestamp"] = 3035   # :35 within the minute
    monkeypatch.setattr(D, "fetch_nexus_files_full", lambda m, k: payload)
    status, _detail = D.check_file_update("1", 3000, "2.0", "KEY", {})  # floor of 3035
    assert status == "current"


def test_version_fallback_when_no_timestamp_match(tmp_path, monkeypatch):
    _mock(monkeypatch)
    # epoch far from any file, but version 1.1 exists -> found, and it has a successor
    status, detail = D.check_file_update("1", 999999999, "1.1", "KEY", {})
    assert status == "update" and isinstance(detail, dict)
    assert detail["latest_ver"] == "2.0"


def test_unknown_when_file_absent(tmp_path, monkeypatch):
    _mock(monkeypatch)
    status, reason = D.check_file_update("1", 999999999, "9.9", "KEY", {})
    assert status == "unknown" and "not listed" in reason


def test_unknown_when_fetch_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "fetch_nexus_files_full", lambda m, k: None)
    status, reason = D.check_file_update("1", 1000, "1.0", "KEY", {})
    assert status == "unknown"


def test_chain_handles_cycle():
    # a malformed cyclic chain must terminate, not hang
    updates = [{"old_file_id": 1, "new_file_id": 2}, {"old_file_id": 2, "new_file_id": 1}]
    assert D._newest_file_in_chain(1, updates) in (1, 2)


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
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
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
            print("PASS", name); n += 1
        finally:
            mp.undo()
    print(f"{n} update tests passed")
