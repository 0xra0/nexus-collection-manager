from nexus_core.nexus_graphql import NexusGraphQLClient


def test_md5_lookup_batches_at_500(monkeypatch):
    client = NexusGraphQLClient("test-key")
    calls = []

    def fake_execute(query, variables=None):
        calls.append((query, variables or {}))
        return {"fileHashes": [{"md5": x} for x in (variables or {}).get("md5s", [])]}

    monkeypatch.setattr(client, "execute", fake_execute)
    hashes = [f"{i:032x}" for i in range(501)]
    rows = client.batch_file_hashes(hashes)
    assert len(rows) == 501
    assert [len(c[1]["md5s"]) for c in calls] == [500, 1]


def test_search_mods_uses_variable_sort(monkeypatch):
    client = NexusGraphQLClient("test-key")
    captured = {}

    def fake_execute(query, variables=None):
        captured["query"] = query
        captured["variables"] = variables or {}
        return {"mods": {"nodes": [{"modId": 123, "name": "Example"}]}}

    monkeypatch.setattr(client, "execute", fake_execute)
    rows = client.search_mods("cyberpunk2077", "Example")
    assert rows[0]["modId"] == 123
    assert "$sort: [ModsSort!]" in captured["query"]
    assert "sort: $sort" in captured["query"]
    assert captured["variables"]["sort"] == [{"field": "endorsements", "direction": "DESC"}]
    assert captured["variables"]["filter"]["gameId"][0]["value"] == 3333


def test_file_content_reverse_lookup_filter(monkeypatch):
    client = NexusGraphQLClient("test-key")
    captured = {}

    def fake_execute(query, variables=None):
        captured.update(variables or {})
        return {"modFileContents": {"nodes": [{"modId": 77, "filePath": "r6/scripts/rare.reds"}]}}

    monkeypatch.setattr(client, "execute", fake_execute)
    rows = client.search_file_contents("cyberpunk2077", file_path_wildcard="*scripts/rare.reds*")
    assert rows[0]["modId"] == 77
    filt = captured["filter"]
    assert filt["gameId"][0]["value"] == 3333
    assert filt["filePathWildcard"][0]["value"] == "*scripts/rare.reds*"
