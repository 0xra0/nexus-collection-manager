"""Regression tests for NexusMods MD5 verification in nexus_core.deployer.

The bug these guard: verification read a per-file `md5` from the files.json list,
which that endpoint never returns, so every archive reported MISMATCH ("bad
checksum") even for byte-perfect official downloads. The fix uses the md5_search
endpoint. These tests mock the network so they run offline.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402


def _archive(tmp_path, name="Cool Mod-1234-1-0-1700000000.zip", data=b"payload"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_verified_on_md5_search_hit(tmp_path, monkeypatch):
    arc = _archive(tmp_path)
    md5 = D.compute_md5(arc)
    hit = [{"mod": {"mod_id": 1234, "name": "Cool Mod"},
            "file_details": {"file_id": 99, "file_name": arc.name,
                             "version": "1.0", "md5": md5}}]
    monkeypatch.setattr(D, "_nexus_request_ex", lambda url, key: (hit, 200))
    status, name, msg = D.verify_nexus_hash(arc, "KEY")
    assert status == "verified", msg
    assert name == arc.name
    assert "Cool Mod" in msg


def test_404_is_unknown_not_mismatch(tmp_path, monkeypatch):
    """A hash Nexus hasn't indexed must never be reported as a mismatch/corruption."""
    arc = _archive(tmp_path)
    monkeypatch.setattr(D, "_nexus_request_ex", lambda url, key: (None, 404))
    status, _name, msg = D.verify_nexus_hash(arc, "KEY")
    assert status == "unknown", msg
    assert "not indexed" in msg


def test_no_api_key_short_circuits(tmp_path):
    arc = _archive(tmp_path)
    status, _name, _msg = D.verify_nexus_hash(arc, "")
    assert status == "no_key"


def test_network_error_is_error_not_mismatch(tmp_path, monkeypatch):
    arc = _archive(tmp_path)
    monkeypatch.setattr(D, "_nexus_request_ex", lambda url, key: (None, None))
    status, _name, _msg = D.verify_nexus_hash(arc, "KEY")
    assert status == "error"


def test_prefers_match_for_filename_mod_id(tmp_path, monkeypatch):
    """When md5_search returns several mods sharing a hash, pick the one whose mod_id
    matches the filename (…-1234-…)."""
    arc = _archive(tmp_path, name="Shared-1234-1-0-1700000000.zip")
    md5 = D.compute_md5(arc)
    matches = [
        {"mod": {"mod_id": 9999, "name": "Other Mod"},
         "file_details": {"file_id": 1, "file_name": "other.zip", "version": "1", "md5": md5}},
        {"mod": {"mod_id": 1234, "name": "Right Mod"},
         "file_details": {"file_id": 2, "file_name": "right.zip", "version": "2", "md5": md5}},
    ]
    monkeypatch.setattr(D, "_nexus_request_ex", lambda url, key: (matches, 200))
    status, name, msg = D.verify_nexus_hash(arc, "KEY")
    assert status == "verified"
    assert "Right Mod" in msg and name == "right.zip"


def test_uses_md5_search_endpoint_not_files_list(tmp_path, monkeypatch):
    """Guard the actual bug: verification must query md5_search, never files.json."""
    arc = _archive(tmp_path)
    seen = {}

    def spy(url, key):
        seen["url"] = url
        return ([], 200)  # empty list -> unknown, but we only care about the URL

    monkeypatch.setattr(D, "_nexus_request_ex", spy)
    D.verify_nexus_hash(arc, "KEY")
    assert "md5_search" in seen["url"]
    assert seen["url"].endswith(f"{D.compute_md5(arc)}.json")
    assert "/files.json" not in seen["url"]


def _run_standalone():
    import inspect, tempfile

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
        kwargs = {}
        if "tmp_path" in params:
            kwargs["tmp_path"] = Path(tempfile.mkdtemp())
        mp = _MP()
        if "monkeypatch" in params:
            kwargs["monkeypatch"] = mp
        try:
            fn(**kwargs)
            print("PASS", name); n += 1
        finally:
            mp.undo()
    print(f"{n} hash tests passed")


if __name__ == "__main__":
    _run_standalone()
