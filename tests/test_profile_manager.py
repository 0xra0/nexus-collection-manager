import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import profile_manager as P
from nexus_core import smart_deployment as S


def _src(tmp_path: Path, name: str, data: bytes | None = None) -> Path:
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if data is not None else name.encode())
    return p


def _stage(st: S.Store, tmp_path: Path, mod: str, archive: str, *, resolutions=None):
    return st.stage_mod(
        mod,
        [(_src(tmp_path, f"{mod}-{archive}"), f"archive/pc/mod/{archive}")],
        resolutions=resolutions,
    )


def test_duplicate_save_and_load_profile_rebuilds_links(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "a.archive")
        _stage(st, tmp_path, "B", "b.archive")
        st.deploy()
        st.create_profile("A only")
        st.set_enabled("B", False, "A only")

        dup = P.duplicate_profile(st, "A only", "A copy")
        assert dup["copied_from"] == "A only"
        assert P.compare_profiles(st, "A only", "A copy")["identical"]

        loaded = P.load_profile(st, "A only")
        assert loaded["active_profile"] == "A only"
        assert (game / "archive/pc/mod/a.archive").is_file()
        assert not (game / "archive/pc/mod/b.archive").exists()

        saved = P.save_profile(st, "Snapshot")
        assert saved["copied_from"] == "A only"
        assert P.compare_profiles(st, "A only", "Snapshot")["identical"]


def test_export_import_roundtrip_preserves_mod_conflict_and_load_rule_state(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    out = tmp_path / "portable.json"
    with S.Store(game) as st:
        shared = "archive/pc/mod/shared.archive"
        st.stage_mod("Base", [(_src(tmp_path, "base", b"BASE"), shared)])
        st.stage_mod("Patch", [(_src(tmp_path, "patch", b"PATCH"), shared)], resolutions={shared: "overwrite"})
        # Give both mods distinct archives too so the load-order rule has visible archive nodes.
        _stage(st, tmp_path, "Base", "base-extra.archive")
        _stage(st, tmp_path, "Patch", "patch-extra.archive")
        st.set_load_rule("Patch", "Base", note="compatibility patch first")
        st.create_profile("Portable")
        st.set_enabled("Base", False, "Portable")
        P.export_profile(st, "Portable", out)

        doc = P.read_document(out)
        preview = P.import_preview(st, doc, name="Imported")
        assert preview["matched_count"] == 2
        assert preview["missing_count"] == 0
        result = P.import_profile(st, doc, name="Imported")
        assert result["skipped_count"] == 0
        compared = P.compare_profiles(st, "Portable", "Imported")
        assert compared["identical"], compared["differences"]

    raw = json.loads(out.read_text())
    assert raw["format"] == P.PROFILE_FORMAT
    assert raw["schema_version"] == 1
    assert all("mod_id" not in row for row in raw["mods"])


def test_import_reports_missing_mods_and_disables_unspecified_local_mods(tmp_path):
    source_game = tmp_path / "source-game"; source_game.mkdir()
    export = tmp_path / "profile.json"
    with S.Store(source_game) as st:
        _stage(st, tmp_path, "Wanted", "wanted.archive")
        _stage(st, tmp_path, "Missing", "missing.archive")
        P.export_profile(st, "Default", export)

    target_game = tmp_path / "target-game"; target_game.mkdir()
    with S.Store(target_game) as st:
        _stage(st, tmp_path, "Wanted", "wanted2.archive")
        _stage(st, tmp_path, "LocalOnly", "local.archive")
        doc = P.read_document(export)
        result = P.import_profile(st, doc, name="From other machine")
        assert result["missing_count"] == 1
        assert result["missing"][0]["name"] == "Missing"
        snap = P.snapshot(st, "From other machine")
        states = {m["name"]: m["enabled"] for m in snap["mods"]}
        assert states["Wanted"] is True
        assert states["LocalOnly"] is False
        assert "Missing" not in states


def test_compare_reports_mod_and_rule_differences(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "a.archive")
        _stage(st, tmp_path, "B", "b.archive")
        st.create_profile("Other")
        st.set_enabled("B", False, "Other")
        st.set_load_rule("B", "A", note="B first", profile="Other")
        diff = P.compare_profiles(st, "Default", "Other")
        assert not diff["identical"]
        assert any(x["category"] == "mod" and x["item"] == "B" for x in diff["differences"])
        assert any(x["category"] == "load rule" for x in diff["differences"])


def test_import_rejects_unsafe_paths_and_rule_cycles(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "a.archive")
        _stage(st, tmp_path, "B", "b.archive")
        doc = P.snapshot(st)
        doc["conflict_preferences"] = [{"rel_path": "../escape", "preferred_mod": "A"}]
        try:
            P.validate_document(doc)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe imported path should fail validation")

        doc = P.snapshot(st)
        doc["load_order_rules"] = [
            {"before_mod": "A", "after_mod": "B", "note": ""},
            {"before_mod": "B", "after_mod": "A", "note": ""},
        ]
        try:
            P.import_preview(st, doc, name="Cyclic")
        except ValueError as exc:
            assert "cycle" in str(exc)
        else:
            raise AssertionError("cyclic imported rules should be rejected")


def test_cli_list_export_import_compare(tmp_path, capsys):
    game = tmp_path / "game"; game.mkdir()
    out = tmp_path / "cli.json"
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "a.archive")
        st.create_profile("Other")

    assert P.main(["list", "--game-root", str(game)]) == 0
    payload = json.loads(capsys.readouterr().out.split(P.JSON_PREFIX, 1)[1])
    assert {x["name"] for x in payload["profiles"]} == {"Default", "Other"}

    assert P.main(["export", "--game-root", str(game), "Default", "--output", str(out)]) == 0
    capsys.readouterr()
    assert out.is_file()
    assert P.main(["import", "--game-root", str(game), str(out), "--name", "Imported"]) == 0
    capsys.readouterr()
    assert P.main(["compare", "--game-root", str(game), "Default", "Imported"]) == 0
    compared = json.loads(capsys.readouterr().out.split(P.JSON_PREFIX, 1)[1])
    assert compared["identical"]
