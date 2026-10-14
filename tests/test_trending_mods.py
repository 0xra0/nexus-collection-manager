from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from nexus_core import trending_mods as TM
from nexus_core import smart_deployment as SD
from nexus_core import download_manager as DM


class FeedClient:
    domain = "cyberpunk2077"
    def request(self, method, path):
        assert method == "GET"
        if path.endswith("trending.json"):
            return [{"mod_id": 12, "name": "Hot Mod", "endorsement_count": 44, "updated_timestamp": 200}]
        if path.endswith("latest_updated.json"):
            return [{"mod_id": 13, "name": "Updated Mod", "endorsement_count": 2, "updated_timestamp": 300}]
        raise AssertionError(path)
    def tracked(self):
        return [{"mod_id": 12, "domain_name": "cyberpunk2077"}]
    def endorsements(self):
        return [{"mod_id": 12, "domain_name": "cyberpunk2077", "status": "Endorsed"}]


def test_feed_normalizes_and_syncs_account_state():
    feed = TM.get_feed(FeedClient(), "trending")
    assert feed["feed"] == "trending"
    assert feed["items"][0]["mod_id"] == 12
    assert feed["items"][0]["endorsed"] is True
    assert feed["items"][0]["tracked"] is True
    updated = TM.get_feed(FeedClient(), "updated")
    assert updated["items"][0]["name"] == "Updated Mod"


def test_choose_install_file_prefers_primary_main_then_latest_main():
    rows = [
        {"file_id": 1, "category": "MAIN", "uploaded_timestamp": 100, "is_primary": False},
        {"file_id": 2, "category": "MAIN", "uploaded_timestamp": 90, "is_primary": True},
        {"file_id": 3, "category": "OPTIONAL", "uploaded_timestamp": 999, "is_primary": True},
    ]
    assert TM.choose_install_file(rows)["file_id"] == 2
    rows[1]["is_primary"] = False
    assert TM.choose_install_file(rows)["file_id"] == 1


def test_choose_install_file_refuses_optional_only():
    with pytest.raises(TM.TrendingModsError, match="No MAIN file"):
        TM.choose_install_file([{"file_id": 7, "category": "OPTIONAL", "uploaded_timestamp": 5}])


class FilesClient:
    domain = "cyberpunk2077"
    def request(self, method, path):
        return {"files": [
            {"file_id": 9, "name": "Optional", "category_name": "OPTIONAL", "uploaded_timestamp": 99},
            {"file_id": 8, "name": "Main", "file_name": "Main-1-0.zip", "category_name": "MAIN", "uploaded_timestamp": 88},
        ]}


def test_install_target_selects_main_file():
    result = TM.install_target(FilesClient(), 123)
    assert result["file"]["file_id"] == 8
    assert "file_id=8" in result["nexus_url"]


def test_free_one_click_returns_nxm_requirement(tmp_path, monkeypatch):
    root = tmp_path / "game"; root.mkdir()
    class Client(FilesClient):
        def __init__(self, *a, **kw): pass
        def validate(self): return {"user_id": 1, "is_premium": False}
    monkeypatch.setattr(TM, "NexusAccountClient", Client)
    result = TM.one_click_install(root, 123, api_key="x")
    assert result["type"] == "requires-nxm"
    assert result["file"]["file_id"] == 8
    assert "Download with Manager" in result["message"]


def test_premium_one_click_reuses_download_and_installer(tmp_path, monkeypatch):
    root = tmp_path / "game"; root.mkdir()
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("archive/pc/mod/Hot.archive", b"payload")

    class Client(FilesClient):
        def __init__(self, *a, **kw): pass
        def validate(self): return {"user_id": 1, "is_premium": True}
    monkeypatch.setattr(TM, "NexusAccountClient", Client)
    monkeypatch.setattr(TM.D, "require_game_idle", lambda *a, **kw: None)
    monkeypatch.setattr(TM.D, "require_bsdtar", lambda: None)

    def fake_enqueue(store, **kwargs):
        return {"job": {"id": 77}}
    def fake_run(store, job_id, **kwargs):
        return {"job": {"id": job_id, "status": "complete"}, "archive": {"path": str(archive)}}
    monkeypatch.setattr(TM.DM, "enqueue", fake_enqueue)
    monkeypatch.setattr(TM.DM, "run_job", fake_run)
    monkeypatch.setattr(TM.MI, "create_session", lambda p: tmp_path / "session")
    monkeypatch.setattr(TM.MI, "preview_session", lambda *a, **kw: {"counts": {"new": 1}})
    monkeypatch.setattr(TM.MI, "apply_session", lambda *a, **kw: {"ok": True, "mod_name": "Hot"})
    monkeypatch.setattr(TM.MI, "cleanup_session", lambda *a, **kw: None)

    result = TM.one_click_install(root, 123, api_key="x")
    assert result["type"] == "installed"
    assert result["file_id"] == 8
    assert result["install"]["ok"] is True


class AccountListClient:
    domain = "cyberpunk2077"
    api_key = ""

    def tracked(self):
        return [
            {"mod_id": 12, "domain_name": self.domain},
            {"mod_id": 15, "domain_name": self.domain},
        ]

    def endorsements(self):
        return [
            {"mod_id": 12, "domain_name": self.domain, "status": "Endorsed"},
            {"mod_id": 13, "domain_name": self.domain, "status": "Abstained"},
            {"mod_id": 99, "domain_name": "skyrimspecialedition", "status": "Endorsed"},
        ]

    def mod_details(self, mod_id):
        mid = int(mod_id)
        return {
            "mod_id": mid,
            "name": f"Account Mod {mid}",
            "summary": f"Summary {mid}",
            "author": "Tester",
            "version": "1.0",
            "updated_timestamp": 1000 + mid,
        }


def test_endorsed_feed_hydrates_live_account_membership():
    result = TM.get_feed(AccountListClient(), "endorsed")
    assert result["feed"] == "endorsed"
    assert [row["mod_id"] for row in result["items"]] == [12]
    row = result["items"][0]
    assert row["name"] == "Account Mod 12"
    assert row["endorsed"] is True
    assert row["tracked"] is True
    assert row["account_list"] == "endorsed"


def test_tracked_feed_hydrates_and_marks_cross_account_state():
    result = TM.get_feed(AccountListClient(), "tracked")
    assert [row["mod_id"] for row in result["items"]] == [12, 15]
    by_id = {row["mod_id"]: row for row in result["items"]}
    assert by_id[12]["endorsed"] is True
    assert by_id[15]["endorsed"] is False
    assert all(row["tracked"] is True for row in result["items"])


def test_account_feed_uses_batched_graphql_metadata_when_available(monkeypatch):
    client = AccountListClient()
    client.api_key = "secret"

    class FakeGraph:
        def __init__(self, key):
            assert key == "secret"

        def batch_mods_basic(self, domain, ids):
            assert domain == "cyberpunk2077"
            assert list(ids) == ["12", "15"]
            return {
                12: {"modId": 12, "name": "Batch Twelve", "updatedAt": "2026-09-01T00:00:00Z"},
                15: {"modId": 15, "name": "Batch Fifteen", "updatedAt": "2026-09-02T00:00:00Z"},
            }

    monkeypatch.setattr(TM, "NexusGraphQLClient", FakeGraph)
    monkeypatch.setattr(client, "mod_details", lambda _mid: (_ for _ in ()).throw(AssertionError("REST fallback not expected")))
    result = TM.get_feed(client, "tracked")
    assert [row["name"] for row in result["items"]] == ["Batch Twelve", "Batch Fifteen"]


class DetailClient:
    domain = "cyberpunk2077"
    api_key = "secret"

    def mod_details(self, mod_id):
        return {
            "mod_id": int(mod_id),
            "name": "Detailed Mod",
            "summary": "Short summary",
            "description": "<p>Long <b>description</b>.</p>",
            "version": "2.0",
            "author": "Author",
        }

    def request(self, method, path):
        assert method == "GET"
        if path.endswith("/files.json"):
            return {"files": [{
                "file_id": 77,
                "name": "Main package",
                "file_name": "main.zip",
                "version": "2.0",
                "category_name": "MAIN",
                "uploaded_timestamp": 1234,
                "size_in_bytes": 4096,
            }]}
        if path.endswith("/changelogs.json"):
            return {"2.0": ["Added feature", "Fixed bug"], "1.0": ["Initial release"]}
        raise AssertionError(path)

    def tracked(self):
        return [{"mod_id": 321, "domain_name": self.domain}]

    def endorsements(self):
        return [{"mod_id": 321, "domain_name": self.domain, "status": "Endorsed"}]


def test_mod_detail_aggregates_description_files_changelog_requirements_and_dlc(monkeypatch):
    class Graph:
        def __init__(self, key):
            assert key == "secret"
        def get_mod(self, domain, mod_id, requirements=True):
            assert (domain, mod_id, requirements) == ("cyberpunk2077", 321, True)
            return {
                "modRequirements": {
                    "nexusRequirements": {"nodes": [{
                        "modId": 55,
                        "modName": "Required Framework",
                        "url": "https://www.nexusmods.com/cyberpunk2077/mods/55",
                        "notes": "Required",
                        "externalRequirement": False,
                        "gameId": 3333,
                    }]},
                    "dlcRequirements": [{
                        "gameExpansion": {"id": "pl", "name": "Phantom Liberty"},
                        "notes": "Needed for the PL variant",
                    }],
                }
            }
    monkeypatch.setattr(TM, "NexusGraphQLClient", Graph)
    result = TM.mod_detail(DetailClient(), 321)
    assert result["type"] == "detail"
    assert result["metadata"]["description"].startswith("<p>")
    assert result["files"][0]["file_id"] == 77
    assert result["changelogs"][0]["version"] == "2.0"
    assert result["requirements"][0]["mod_id"] == 55
    assert result["dlc_requirements"][0]["name"] == "Phantom Liberty"
    assert result["endorsed"] is True
    assert result["tracked"] is True
    assert result["warnings"] == []


def test_mod_detail_secondary_sections_fail_soft(monkeypatch):
    client = DetailClient()
    def bad_request(method, path):
        raise TM.NexusAPIError("secondary unavailable")
    client.request = bad_request
    client.tracked = lambda: (_ for _ in ()).throw(TM.NexusAPIError("account unavailable"))

    class Graph:
        def __init__(self, key): pass
        def get_mod(self, *a, **kw):
            raise TM.NexusGraphQLError("requirements unavailable")
    monkeypatch.setattr(TM, "NexusGraphQLClient", Graph)

    result = TM.mod_detail(client, 321)
    assert result["metadata"]["name"] == "Detailed Mod"
    assert result["files"] == []
    assert result["changelogs"] == []
    assert result["requirements"] == []
    assert result["dlc_requirements"] == []
    assert result["endorsed"] is None
    assert result["tracked"] is None
    assert len(result["warnings"]) == 4


def test_changelog_normalizer_accepts_legacy_dict_shape():
    rows = TM._normalize_changelogs({"1.2": ["A", "B"], "1.1": "C"})
    assert rows == [
        {"version": "1.2", "entries": ["A", "B"]},
        {"version": "1.1", "entries": ["C"]},
    ]
