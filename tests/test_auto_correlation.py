import json
from pathlib import Path

import pytest

from nexus_core import auto_correlation as ac


def _snapshot(tmp_path: Path, rows):
    files = {}
    groups = []
    root = tmp_path / "game"
    root.mkdir(exist_ok=True)
    for i, (name, rel, md5) in enumerate(rows, 1):
        path = str((root / rel).resolve())
        files[path] = {
            "root": str(root),
            "relative_path": rel,
            "md5": md5,
            "sha256": (str(i) * 64)[:64],
            "group_id": f"cluster-{i:04d}",
            "group_name": name,
        }
        groups.append({"id": f"cluster-{i:04d}", "name": name, "files": [path], "file_count": 1, "size": 1})
    return root, {"generated_at": "now", "files": files, "groups": groups}


class FakeAccount:
    def tracked(self):
        return [{"mod_id": 3333, "domain_name": ac.GAME_DOMAIN}]

    def endorsements(self):
        return [{"mod_id": 3333, "domain_name": ac.GAME_DOMAIN, "status": "Endorsed"}]


class FakeGQL:
    def batch_mods_basic(self, domain, ids):
        names = {
            1234: "Known Mod",
            2002: "Hash Identity",
            2222: "Content Helper",
            3333: "Tracked Widget",
            4444: "Collection Extra",
            5555: "Fancy Dependency",
        }
        return {int(i): {"modId": int(i), "name": names[int(i)], "author": "A", "endorsements": 100}
                for i in ids if int(i) in names}

    def batch_file_hashes(self, md5s):
        rows = []
        if "c" * 32 in set(md5s):
            rows.append({
                "md5": "c" * 32,
                "gameId": 3333,
                "modFile": {"mod": {"modId": 2002, "name": "Hash Identity", "author": "Hash", "endorsements": 50}},
            })
        return rows

    def search_file_contents(self, domain, *, file_path_wildcard=None, file_extension=None, count=20):
        if "UniqueScript.reds" in (file_path_wildcard or ""):
            return [{"filePath": "r6/scripts/ContentHelper/UniqueScript.reds", "modId": 2222, "fileId": 1}]
        return []

    def collection_latest(self, slug, domain):
        return {
            "slug": slug,
            "name": "My Collection",
            "latestPublishedRevision": {
                "revisionNumber": 7,
                "modFiles": [{"file": {"mod": {"modId": 4444, "name": "Collection Extra", "author": "C", "endorsements": 25}}}],
            },
        }

    def batch_mods(self, domain, ids, *, requirements=True):
        out = {}
        for raw in ids:
            mid = int(raw)
            if mid == 1234:
                out[mid] = {
                    "modId": mid,
                    "name": "Known Mod",
                    "modRequirements": {
                        "nexusRequirements": {"nodes": [{
                            "modId": 5555, "modName": "Fancy Dependency", "gameId": 3333,
                            "externalRequirement": False,
                        }]}
                    },
                }
            else:
                out[mid] = {"modId": mid, "name": f"Mod {mid}", "modRequirements": {"nexusRequirements": {"nodes": []}}}
        return out

    def search_mods(self, domain, name, *, count=20):
        if "fuzzy dream" in name:
            return [{"modId": 6666, "name": "Fuzzy Dream Reborn", "author": "F", "endorsements": 500, "downloads": 1000}]
        return []

    def get_mod(self, domain, mod_id, *, requirements=True):
        return {"modId": int(mod_id), "name": f"Manual {mod_id}", "author": "User"}


def test_name_similarity_algorithms_are_stable():
    assert ac.jaccard_similarity("Night City Estate", "Night City Estate Mod") == 1.0
    assert ac.jaro_winkler_similarity("MARTHA", "MARHTA") == pytest.approx(0.961111, rel=1e-5)
    jac, jw, combined = ac.fuzzy_similarity("Vehicle Handling", "Vehicle Handling Overhaul")
    assert 0.6 < jac <= 1.0
    assert 0.8 < jw <= 1.0
    assert 0.7 < combined <= 1.0


def test_extract_mod_ids_avoids_plain_versions_and_reads_nexus_conventions():
    assert ac.extract_mod_ids("Cool Mod-12345-1-2-1700000000.zip") == {"12345"}
    assert ac.extract_mod_ids("https://www.nexusmods.com/cyberpunk2077/mods/9988") == {"9988"}
    assert ac.extract_mod_ids("release-v2.12.0-final.zip") == set()


def test_group_keys_survive_cluster_number_changes(tmp_path):
    root, snap = _snapshot(tmp_path, [("Foo", "r6/scripts/Foo/main.reds", "a" * 32)])
    g1 = dict(snap["groups"][0]); g2 = dict(g1); g2["id"] = "cluster-9999"
    assert ac.group_key(root, g1, snap["files"]) == ac.group_key(root, g2, snap["files"])


def test_full_multi_tier_pipeline(tmp_path, monkeypatch):
    rows = [
        ("Known Mod", "archive/pc/mod/KnownMod-1234-1-0.archive", "a" * 32),
        ("Hash Identity", "archive/pc/mod/hash_identity.archive", "c" * 32),
        ("Content Helper", "r6/scripts/ContentHelper/UniqueScript.reds", "d" * 32),
        ("Tracked Widget", "red4ext/plugins/TrackedWidget/widget.dll", "e" * 32),
        ("Collection Extra", "mods/CollectionExtra/info.json", "f" * 32),
        ("Fancy Dependency", "r6/scripts/FancyDependency/main.reds", "1" * 32),
        ("Fuzzy Dream", "archive/pc/mod/FuzzyDream.archive", "2" * 32),
    ]
    root, snap = _snapshot(tmp_path, rows)
    # Avoid depending on a real deploy manifest in this pure inference test.
    monkeypatch.setattr(ac, "_manifest_group_links", lambda *a, **k: ({}, {}))
    result = ac.correlate(
        snap, root, gql=FakeGQL(), account=FakeAccount(), collections=["abc123"],
        decisions={}, max_content_queries=20, max_fuzzy_searches=20,
    )
    by_name = {row["local_name"]: row for row in result["results"]}

    assert by_name["Known Mod"]["match"]["mod_id"] == "1234"
    assert by_name["Known Mod"]["match"]["source"] == "filename-id"
    assert by_name["Hash Identity"]["match"]["mod_id"] == "2002"
    assert by_name["Hash Identity"]["match"]["source"] == "md5"
    assert by_name["Content Helper"]["match"]["mod_id"] == "2222"
    assert by_name["Content Helper"]["match"]["source"] == "content"
    assert by_name["Tracked Widget"]["match"]["mod_id"] == "3333"
    assert by_name["Tracked Widget"]["match"]["source"] == "account"
    assert by_name["Collection Extra"]["match"]["mod_id"] == "4444"
    assert by_name["Collection Extra"]["match"]["source"] == "collection"
    assert by_name["Fancy Dependency"]["match"]["mod_id"] == "5555"
    assert by_name["Fancy Dependency"]["match"]["source"] == "requirement"
    assert by_name["Fuzzy Dream"]["match"]["mod_id"] == "6666"
    assert by_name["Fuzzy Dream"]["match"]["source"] == "fuzzy"


def test_review_confirm_reject_and_reassign_persist(tmp_path):
    path = tmp_path / "correlation.json"
    state = {
        "version": 1,
        "decisions": {},
        "results": [{
            "group_key": "g1", "local_name": "Foo", "status": "suggested", "rejected_mod_ids": [],
            "match": {"mod_id": "10", "name": "Ten", "confidence": 0.8, "source": "fuzzy"},
            "alternatives": [
                {"mod_id": "10", "name": "Ten", "confidence": 0.8, "source": "fuzzy"},
                {"mod_id": "11", "name": "Eleven", "confidence": 0.7, "source": "account"},
            ],
        }],
    }
    ac._atomic_json(path, state)

    row = ac.apply_review(path, "g1", "confirm")
    assert row["status"] == "confirmed"
    assert ac.load_state(path)["decisions"]["g1"]["mod_id"] == "10"

    row = ac.apply_review(path, "g1", "reject")
    assert row["match"]["mod_id"] == "11"
    assert "10" in ac.load_state(path)["decisions"]["g1"]["rejected_mod_ids"]

    row = ac.apply_review(path, "g1", "reassign", "99", meta={"name": "Correct Mod", "author": "A"})
    assert row["status"] == "manual"
    assert row["match"]["mod_id"] == "99"
    assert row["match"]["source"] == "manual"


def test_rejected_candidate_is_not_selected_on_rerun(tmp_path):
    root, snap = _snapshot(tmp_path, [("Known Mod", "archive/pc/mod/KnownMod-1234-1-0.archive", "a" * 32)])
    key = ac.group_key(root, snap["groups"][0], snap["files"])
    result = ac.correlate(snap, root, gql=None, account=None,
                          decisions={key: {"decision": "rejected", "rejected_mod_ids": ["1234"]}})
    row = result["results"][0]
    assert row["match"] is None
    assert row["status"] == "unmatched"


def test_scanner_serializes_md5_alongside_sha256(tmp_path):
    game = tmp_path / "game"
    path = game / "archive/pc/mod/foo.archive"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"hello")
    result = ac.mod_scanner.scan_mods(game, ["archive/pc/mod"], jobs=1,
                                      state_path=tmp_path / "scan.json", save_snapshot=False)
    row = result["snapshot"]["files"][str(path.resolve())]
    import hashlib
    assert row["md5"] == hashlib.md5(b"hello", usedforsecurity=False).hexdigest()
    assert row["sha256"] == hashlib.sha256(b"hello").hexdigest()


def test_cli_sigint_is_structured_cancellation(tmp_path, monkeypatch, capsys):
    game = tmp_path / "game"
    game.mkdir()

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ac.mod_scanner, "scan_mods", interrupted)
    rc = ac.main(["run", "--game-root", str(game), "--path", "."])
    assert rc == 130
    lines = capsys.readouterr().out.splitlines()
    prefix = "[correlation-result] "
    payload_line = next(line for line in lines if line.startswith(prefix))
    payload = json.loads(payload_line[len(prefix):])
    assert payload["type"] == "cancelled"
    assert "cancel" in payload["message"].lower()


def test_correlation_gui_treats_sigint_as_cancellation_not_failure():
    source = (Path(__file__).parents[1] / "gui" / "correlation_tab.py").read_text(encoding="utf-8")
    assert 'kind == "cancelled"' in source
    assert 'getattr(runner, "was_aborted", False)' in source
    assert 'rc in {-2, 130}' in source
