import json
import urllib.parse
from pathlib import Path

import pytest

from nexus_core import nexus_account as na


def test_account_payload_ids_are_domain_filtered():
    payload = [
        {"mod_id": 12, "domain_name": "cyberpunk2077"},
        {"mod_id": "13", "game": {"domain_name": "cyberpunk2077"}},
        {"mod_id": 99, "domain_name": "skyrimspecialedition"},
    ]
    assert na.mod_ids_from_account_payload(payload) == {"12", "13"}
    assert na.mod_ids_from_account_payload({"tracked_mods": payload}) == {"12", "13"}


def test_endorsement_payload_ignores_explicit_abstained_records():
    payload = [
        {"mod_id": 12, "domain_name": "cyberpunk2077", "status": "Endorsed"},
        {"mod_id": 13, "domain_name": "cyberpunk2077", "status": "Abstained"},
        {"mod_id": 14, "domain_name": "cyberpunk2077", "endorsed": False},
    ]
    assert na.mod_ids_from_account_payload(payload, endorsements=True) == {"12"}


def test_installed_nexus_mods_groups_same_mod_id(tmp_path, monkeypatch):
    # Names differ because one Nexus page can contain several separately installable files.
    manifest = {
        "mods": {
            "Main File-1234-1.0-1700000000": ["a", "b"],
            "Optional Patch-1234-1.1-1700000100": ["c"],
            "Other Mod-5678-2.0-1700000200": ["d"],
            "Manual Mod": ["e"],
        }
    }
    (tmp_path / ".mod_manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(na.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    mods = na.installed_nexus_mods(tmp_path)
    by_id = {m.mod_id: m for m in mods}
    assert set(by_id) == {"1234", "5678"}
    assert by_id["1234"].file_count == 3
    assert by_id["1234"].installed_version == "1.1"
    assert len(by_id["1234"].stems) == 2


class FakeClient:
    domain = na.GAME_DOMAIN

    def __init__(self):
        self.endorsed = set()
        self.tracked_ids = set()
        self.calls = []

    def set_endorsed(self, mod_id, value, version=""):
        self.calls.append(("endorse", str(mod_id), value, version))
        (self.endorsed.add if value else self.endorsed.discard)(str(mod_id))

    def endorsements(self):
        return [{"mod_id": int(x), "domain_name": self.domain} for x in self.endorsed]

    def set_tracked(self, mod_id, value):
        self.calls.append(("track", str(mod_id), value))
        (self.tracked_ids.add if value else self.tracked_ids.discard)(str(mod_id))

    def tracked(self):
        return [{"mod_id": int(x), "domain_name": self.domain} for x in self.tracked_ids]


def test_mutations_are_verified_against_live_state():
    c = FakeClient()
    out = na.mutate_and_verify(c, "endorse", "42", "1.2.3")
    assert out["endorsed"] is True
    assert c.calls[-1] == ("endorse", "42", True, "1.2.3")
    out = na.mutate_and_verify(c, "unendorse", "42", "1.2.3")
    assert out["endorsed"] is False
    out = na.mutate_and_verify(c, "track", "42")
    assert out["tracked"] is True
    out = na.mutate_and_verify(c, "untrack", "42")
    assert out["tracked"] is False


def test_endorse_and_track_request_shapes(monkeypatch):
    seen = []

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b"{}"

    def fake_urlopen(req, timeout=0):
        seen.append(req)
        return Resp()

    monkeypatch.setattr(na.urllib.request, "urlopen", fake_urlopen)
    c = na.NexusAccountClient("secret")
    c.set_endorsed(77, True, "2.5")
    c.set_tracked(77, True)
    c.set_tracked(77, False)

    endorse, track, untrack = seen
    assert endorse.get_method() == "POST"
    assert endorse.full_url.endswith("/games/cyberpunk2077/mods/77/endorse.json")
    assert urllib.parse.parse_qs(endorse.data.decode()) == {"version": ["2.5"]}
    assert endorse.headers.get("Apikey") == "secret"

    assert track.get_method() == "POST"
    assert "/user/tracked_mods?domain_name=cyberpunk2077" in track.full_url
    assert json.loads(track.data) == {"mod_id": 77}
    assert untrack.get_method() == "DELETE"
    assert json.loads(untrack.data) == {"mod_id": 77}


def test_failed_live_verification_is_not_reported_as_success():
    class Bad(FakeClient):
        def endorsements(self):
            return []

    with pytest.raises(na.NexusAPIError, match="did not confirm"):
        na.mutate_and_verify(Bad(), "endorse", "12", "1.0")


def test_cli_inventory_does_not_need_api_key(tmp_path, capsys, monkeypatch):
    (tmp_path / ".mod_manifest.json").write_text(json.dumps({
        "mods": {"Example-123-1.0-1700000000": ["archive/pc/mod/example.archive"]}
    }))
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    monkeypatch.setattr(na, "_config_api_key", lambda: "")
    assert na.main(["inventory", "--game-root", str(tmp_path)]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("[nexus-account] ")
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["mods"][0]["mod_id"] == "123"


def test_account_entries_preserve_order_filter_domain_and_deduplicate():
    payload = {
        "endorsements": [
            {"mod_id": 5, "domain_name": "cyberpunk2077", "status": "Endorsed"},
            {"mod_id": 5, "domain_name": "cyberpunk2077", "status": "Endorsed"},
            {"mod_id": 6, "domain_name": "cyberpunk2077", "status": "Abstained"},
            {"mod_id": 7, "domain_name": "skyrimspecialedition", "status": "Endorsed"},
            {"mod_id": 8, "domain_name": "cyberpunk2077", "status": "Endorsed"},
        ]
    }
    rows = na.account_entries_from_payload(payload, endorsements=True)
    assert [na._entry_mod_id(row) for row in rows] == ["5", "8"]
