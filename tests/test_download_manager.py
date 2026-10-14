from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import pytest

from nexus_core import download_manager as DM
from nexus_core import smart_deployment as SD
from nexus_core import archive_management as AM


def game(tmp_path: Path) -> Path:
    root = tmp_path / "game"
    root.mkdir()
    return root


def test_parse_nxm_mod_link():
    link = DM.parse_nxm("nxm://cyberpunk2077/mods/1234/files/5678?key=abc%2B123&expires=1999999999&user_id=42")
    assert link.game_domain == "cyberpunk2077"
    assert link.mod_id == 1234
    assert link.file_id == 5678
    assert link.key == "abc+123"
    assert link.expires == 1999999999
    assert link.user_id == 42


def test_parse_nxm_rejects_collection_and_bad_scheme():
    with pytest.raises(ValueError):
        DM.parse_nxm("https://nexusmods.com/foo")
    with pytest.raises(ValueError):
        DM.parse_nxm("nxm://cyberpunk2077/collections/foo/revisions/1")


def test_enqueue_nxm_hides_authorization_from_queue(tmp_path):
    root = game(tmp_path)
    nxm = f"nxm://cyberpunk2077/mods/1001/files/2002?key=secret-token&expires={int(time.time())+3600}&user_id=7"
    with SD.Store(root) as store:
        result = DM.enqueue_nxm(store, nxm)
        assert result["job"]["mod_id"] == 1001
        queue = DM.list_jobs(store)["items"]
        assert len(queue) == 1
        assert "nxm_key" not in queue[0]
        assert "secret-token" not in json.dumps(queue)


class DummyClient:
    def __init__(self):
        self.calls = []
    def request(self, method, path):
        self.calls.append((method, path))
        return [{"URI": "https://cdn.example/file.zip", "name": "test"}]


def test_download_links_premium_needs_no_nxm_token():
    client = DummyClient()
    row = {"game_domain": "cyberpunk2077", "mod_id": 1, "file_id": 2,
           "nxm_key": "", "nxm_expires": 0, "nxm_user_id": None}
    urls = DM._download_links(client, row, user={"user_id": 5, "is_premium": True})
    assert urls == ["https://cdn.example/file.zip"]
    assert "?key=" not in client.calls[0][1]


def test_download_links_free_uses_nxm_and_checks_user():
    client = DummyClient()
    row = {"game_domain": "cyberpunk2077", "mod_id": 1, "file_id": 2,
           "nxm_key": "abc", "nxm_expires": int(time.time()) + 300, "nxm_user_id": 9}
    urls = DM._download_links(client, row, user={"user_id": 9, "is_premium": False})
    assert urls
    assert "key=abc" in client.calls[0][1]
    with pytest.raises(DM.DownloadManagerError, match="different Nexus account"):
        DM._download_links(client, row, user={"user_id": 10, "is_premium": False})


def test_free_direct_id_download_requires_nxm():
    client = DummyClient()
    row = {"game_domain": "cyberpunk2077", "mod_id": 1, "file_id": 2,
           "nxm_key": "", "nxm_expires": 0, "nxm_user_id": None}
    with pytest.raises(DM.DownloadManagerError, match="Download with Manager"):
        DM._download_links(client, row, user={"user_id": 9, "is_premium": False})


def test_run_job_imports_completed_archive_and_links_nexus(tmp_path, monkeypatch):
    root = game(tmp_path)
    with SD.Store(root) as store:
        queued = DM.enqueue(store, domain="cyberpunk2077", mod_id=2468, file_id=1357)
        jid = queued["job"]["id"]

        class Client:
            def __init__(self, *args, **kwargs): pass
            def validate(self): return {"user_id": 1, "name": "Tester", "is_premium": True}

        monkeypatch.setattr(DM, "NexusAccountClient", Client)
        monkeypatch.setattr(DM, "_refresh_job_metadata", lambda store, job_id, client: store.conn.execute(
            "SELECT * FROM download_jobs WHERE id=?", (job_id,)).fetchone())
        monkeypatch.setattr(DM, "_download_links", lambda *args, **kwargs: ["https://cdn.example/mod.zip"])

        def fake_transfer(urls, part, *, expected, progress, stop_event):
            with zipfile.ZipFile(part, "w") as zf:
                zf.writestr("archive/pc/mod/Test.archive", b"payload")
            size = part.stat().st_size
            progress(size, size, 1000.0)
            return size
        monkeypatch.setattr(DM, "_transfer", fake_transfer)

        result = DM.run_job(store, jid, api_key="x")
        assert result["job"]["status"] == "complete"
        archive_id = result["job"]["archive_id"]
        detail = AM.archive_detail(store, archive_id)
        assert detail["nexus_mod_id"] == 2468
        assert detail["nexus_file_id"] == 1357
        assert Path(detail["path"]).is_file()


def test_remove_job_can_delete_partial(tmp_path):
    root = game(tmp_path)
    with SD.Store(root) as store:
        queued = DM.enqueue(store, domain="cyberpunk2077", mod_id=1, file_id=2)
        jid = queued["job"]["id"]
        part = AM.archive_root(root) / "x.zip.part"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"partial")
        with store.conn:
            store.conn.execute("UPDATE download_jobs SET part_rel=?,status='paused' WHERE id=?",
                               (part.relative_to(store.root).as_posix(), jid))
        DM.remove_job(store, jid, delete_partial=True)
        assert not part.exists()
        assert DM.list_jobs(store)["items"] == []


def test_register_nxm_handler_writes_linux_desktop_file(tmp_path, monkeypatch):
    root = game(tmp_path)
    cfg = tmp_path / "config" / "nxm-handler.json"
    desktop = tmp_path / "apps" / "nexus-collection-manager-nxm.desktop"
    monkeypatch.setattr(DM, "HANDLER_CONFIG", cfg)
    monkeypatch.setattr(DM, "DESKTOP_FILE", desktop)
    monkeypatch.setattr(DM.shutil, "which", lambda name: None)
    result = DM.register_nxm_handler(root)
    assert result["registered"] is True
    text = desktop.read_text()
    assert "MimeType=x-scheme-handler/nxm;" in text
    assert "-m main %u" in text
    assert json.loads(cfg.read_text())["game_root"] == str(root.resolve())


def test_stale_downloading_job_recovers_to_paused(tmp_path):
    root = game(tmp_path)
    with SD.Store(root) as store:
        queued = DM.enqueue(store, domain="cyberpunk2077", mod_id=3, file_id=4)
        jid = queued["job"]["id"]
        with store.conn:
            store.conn.execute(
                "UPDATE download_jobs SET status='downloading',worker_pid=? WHERE id=?",
                (999_999_999, jid),
            )
        item = DM.list_jobs(store)["items"][0]
        assert item["status"] == "paused"


def test_nxm_consumes_pending_install_intent(tmp_path):
    root = game(tmp_path)
    with SD.Store(root) as store:
        DM.arm_install_intent(store, 1001, 2002)
        nxm = f"nxm://cyberpunk2077/mods/1001/files/2002?key=abc&expires={int(time.time())+3600}&user_id=7"
        queued = DM.enqueue_nxm(store, nxm)
        assert queued["job"]["install_after"] == 1
        assert store.conn.execute("SELECT COUNT(*) FROM download_install_intents").fetchone()[0] == 0


def test_run_job_auto_installs_when_armed(tmp_path, monkeypatch):
    root = game(tmp_path)
    with SD.Store(root) as store:
        queued = DM.enqueue(store, domain="cyberpunk2077", mod_id=2468, file_id=1357, install_after=True)
        jid = queued["job"]["id"]

        class Client:
            def __init__(self, *args, **kwargs): pass
            def validate(self): return {"user_id": 1, "name": "Tester", "is_premium": True}

        monkeypatch.setattr(DM, "NexusAccountClient", Client)
        monkeypatch.setattr(DM, "_refresh_job_metadata", lambda store, job_id, client: store.conn.execute(
            "SELECT * FROM download_jobs WHERE id=?", (job_id,)).fetchone())
        monkeypatch.setattr(DM, "_download_links", lambda *args, **kwargs: ["https://cdn.example/mod.zip"])

        def fake_transfer(urls, part, *, expected, progress, stop_event):
            with zipfile.ZipFile(part, "w") as zf:
                zf.writestr("archive/pc/mod/Test.archive", b"payload")
            size = part.stat().st_size
            progress(size, size, 1000.0)
            return size
        monkeypatch.setattr(DM, "_transfer", fake_transfer)
        monkeypatch.setattr(DM, "_auto_install_archive", lambda game_root, archive: {"install": {"ok": True}})

        result = DM.run_job(store, jid, api_key="x")
        assert result["job"]["install_status"] == "installed"
        assert result["auto_install"]["install"]["ok"] is True
