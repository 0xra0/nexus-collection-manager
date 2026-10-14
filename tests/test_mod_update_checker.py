from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from nexus_core import archive_management as AM
from nexus_core import smart_deployment as SD
from nexus_core import update_checker as U
from nexus_core.nexus_account import NexusAPIError


def _package(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("archive/pc/mod/test.archive", b"archive")
    return path


def _install(tmp_path: Path, package_name: str, *, mod_name: str = "Managed Mod"):
    game = tmp_path / "game"; game.mkdir()
    package = _package(tmp_path / package_name)
    payload = tmp_path / "payload.archive"; payload.write_bytes(b"payload")
    result = SD.stage_and_deploy(
        game, mod_name, [(payload, "archive/pc/mod/test.archive")],
        source_archive=str(package), auto_deploy=False,
    )
    return game, result["archive"]


class FilesClient:
    domain = "cyberpunk2077"
    def __init__(self, *, file_updates=None, md5_details=None, premium=True):
        self.file_updates = list(file_updates or [])
        self.md5_details = md5_details
        self.premium = premium
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        if path.endswith("/files.json"):
            return {
                "files": [
                    {"file_id": 10, "file_name": "v1.zip", "version": "1.0", "category_name": "MAIN", "uploaded_timestamp": 100},
                    {"file_id": 11, "file_name": "v2.zip", "version": "2.0", "category_name": "MAIN", "uploaded_timestamp": 200},
                    {"file_id": 99, "file_name": "optional.zip", "version": "99", "category_name": "OPTIONAL", "uploaded_timestamp": 999},
                ],
                "file_updates": self.file_updates,
            }
        if "/md5_search/" in path:
            if self.md5_details is None:
                raise NexusAPIError("not found", status=404)
            return [{"mod": {"mod_id": 100}, "file_details": dict(self.md5_details)}]
        raise AssertionError(path)

    def validate(self):
        return {"user_id": 1, "name": "Tester", "is_premium": self.premium}


def test_exact_file_id_follows_only_its_update_lineage(tmp_path):
    game, archive = _install(tmp_path, "anything.zip")
    with SD.Store(game, create=False) as store:
        AM.link_nexus(store, archive["id"], 100, nexus_file_id=10)
        result = U.check_updates(store, FilesClient(file_updates=[{"old_file_id": 10, "new_file_id": 11}]))
    row = result["items"][0]
    assert row["status"] == "update"
    assert row["installed_version"] == "1.0"
    assert row["latest_version"] == "2.0"
    assert row["latest_file"]["file_id"] == 11
    assert row["match_source"] == "nexus-file-id"


def test_unrelated_newer_sibling_file_does_not_create_false_update(tmp_path):
    game, archive = _install(tmp_path, "anything.zip")
    with SD.Store(game, create=False) as store:
        AM.link_nexus(store, archive["id"], 100, nexus_file_id=10)
        result = U.check_updates(store, FilesClient(file_updates=[]))
    row = result["items"][0]
    assert row["status"] == "current"
    assert row["latest_file"]["file_id"] == 10


def test_md5_reverse_lookup_recovers_missing_file_id(tmp_path):
    game, archive = _install(tmp_path, "manual-package.zip")
    with SD.Store(game, create=False) as store:
        AM.link_nexus(store, archive["id"], 100)
        client = FilesClient(
            file_updates=[{"old_file_id": 10, "new_file_id": 11}],
            md5_details={"file_id": 10, "version": "1.0", "file_name": "v1.zip"},
        )
        result = U.check_updates(store, client)
    row = result["items"][0]
    assert row["status"] == "update"
    assert row["match_source"] == "md5"
    assert any("md5_search" in path for _, path in client.calls)


def test_filename_version_fallback_is_conservative(tmp_path):
    game, archive = _install(tmp_path, "Cool-Mod-100-1.0-1700000000.zip")
    with SD.Store(game, create=False) as store:
        # No explicit archive link. The Nexus-style filename identifies mod + version.
        result = U.check_updates(store, FilesClient(file_updates=[{"old_file_id": 10, "new_file_id": 11}]))
    row = result["items"][0]
    assert row["nexus_mod_id"] == 100
    assert row["installed_version"] == "1.0"
    assert row["match_source"] == "version"
    assert row["status"] == "update"


def test_unlinked_mod_stays_visible_without_network_guessing(tmp_path):
    game, _archive = _install(tmp_path, "manual-package.zip")
    class NoCalls(FilesClient):
        def request(self, method, path):
            raise AssertionError("unlinked mod must not make a Nexus lookup")
    with SD.Store(game, create=False) as store:
        result = U.check_updates(store, NoCalls())
    row = result["items"][0]
    assert row["status"] == "unlinked"
    assert "Auto-Correlation" in row["reason"]


def test_download_update_uses_existing_download_manager_for_premium(tmp_path, monkeypatch):
    game = tmp_path / "game"; game.mkdir()
    with SD.Store(game) as store:
        update = {
            "status": "update", "managed_mod_id": 7, "name": "M", "nexus_mod_id": 100,
            "latest_file": {"file_id": 11, "version": "2.0"}, "download_url": "https://example/files/11",
        }
        monkeypatch.setattr(U, "check_updates", lambda *_a, **_k: {"items": [update]})
        calls = {}
        monkeypatch.setattr(U.DM, "enqueue", lambda _store, **kw: calls.setdefault("enqueue", kw) or {"job": {"id": 5}})
        # The lambda above returns kw; use a real stub so result shape stays exact.
        def enqueue(_store, **kw):
            calls["enqueue"] = kw
            return {"job": {"id": 5}}
        monkeypatch.setattr(U.DM, "enqueue", enqueue)
        monkeypatch.setattr(U.DM, "run_job", lambda _store, jid, api_key="": {"job": {"id": jid, "status": "complete"}, "archive": {"id": 9}})
        result = U.download_update(store, FilesClient(premium=True), api_key="secret", managed_mod_id=7)
    assert result["type"] == "downloaded"
    assert calls["enqueue"]["mod_id"] == 100
    assert calls["enqueue"]["file_id"] == 11
    assert calls["enqueue"]["install_after"] is False


def test_free_account_update_requires_nxm_and_does_not_queue(tmp_path, monkeypatch):
    game = tmp_path / "game"; game.mkdir()
    with SD.Store(game) as store:
        update = {
            "status": "update", "managed_mod_id": 7, "name": "M", "nexus_mod_id": 100,
            "latest_file": {"file_id": 11, "version": "2.0"}, "download_url": "https://example/files/11",
        }
        monkeypatch.setattr(U, "check_updates", lambda *_a, **_k: {"items": [update]})
        monkeypatch.setattr(U.DM, "enqueue", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not queue without NXM")))
        result = U.download_update(store, FilesClient(premium=False), api_key="secret", managed_mod_id=7)
    assert result["type"] == "requires-nxm"
    assert result["nexus_url"].endswith("/11")
