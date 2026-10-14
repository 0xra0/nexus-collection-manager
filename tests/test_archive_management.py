import json
import zipfile
from pathlib import Path

import pytest

from nexus_core import archive_management as AM
from nexus_core import smart_deployment as SD


def _zip(path: Path, entries: dict[str, bytes] | None = None) -> Path:
    entries = entries or {"archive/pc/mod/example.archive": b"payload", "README.txt": b"hello"}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_import_browse_link_and_unlink(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    package = _zip(tmp_path / "CoolMod-1234-1-0.zip")
    with SD.Store(game) as store:
        item = AM.import_archive(store, package)
        assert item["file_name"] == package.name
        assert item["orphan"] is True
        assert item["suggested_nexus_mod_id"] == 1234
        assert Path(item["path"]).is_file()
        assert len(item["sha256"]) == 64 and len(item["md5"]) == 32

        content = AM.browse_contents(store, item["id"])
        assert {x["path"] for x in content["entries"]} >= {"archive/pc/mod/example.archive", "README.txt"}
        assert content["unsafe_entries"] == 0

        linked = AM.link_nexus(store, item["id"], "https://www.nexusmods.com/cyberpunk2077/mods/1234")
        assert linked["nexus_mod_id"] == 1234
        assert linked["orphan"] is False
        assert linked["nexus_url"].endswith("/mods/1234")

        unlinked = AM.unlink_nexus(store, item["id"])
        assert unlinked["nexus_mod_id"] is None
        assert unlinked["orphan"] is True


def test_browse_marks_unsafe_members_without_extracting(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    package = _zip(tmp_path / "unsafe.zip", {"../escape.txt": b"x", "/abs.txt": b"y", "ok/file.txt": b"z"})
    with SD.Store(game) as store:
        item = AM.import_archive(store, package)
        result = AM.browse_contents(store, item["id"])
        unsafe = {x["path"] for x in result["entries"] if x["unsafe"]}
        assert "../escape.txt" in unsafe
        assert "/abs.txt" in unsafe
        assert "ok/file.txt" not in unsafe
        assert not (tmp_path / "escape.txt").exists()


def test_installer_source_archive_association_survives_raw_archive_delete(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    package = _zip(tmp_path / "Installed-5555-1-0.zip")
    payload = tmp_path / "payload.archive"; payload.write_bytes(b"game-payload")

    result = SD.stage_and_deploy(
        game, "Installed-5555-1-0", [(payload, "archive/pc/mod/installed.archive")],
        source_archive=str(package), auto_deploy=True,
    )
    assert result["archive"] and result["archive"]["installed_mods"][0]["name"] == "Installed-5555-1-0"
    archive_id = result["archive"]["id"]
    game_file = game / "archive/pc/mod/installed.archive"
    assert game_file.is_file()

    with SD.Store(game, create=False) as store:
        with pytest.raises(AM.ArchiveManagementError):
            AM.delete_archive(store, archive_id)
        deleted = AM.delete_archive(store, archive_id, force=True)
        assert deleted["staged_payload_preserved"] is True
        assert game_file.is_file()
        assert store.installed_mods()[0]["name"] == "Installed-5555-1-0"


def test_cleanup_deletes_only_uninstalled_and_unlinked_orphans(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    installed_pkg = _zip(tmp_path / "installed.zip")
    linked_pkg = _zip(tmp_path / "linked.zip", {"a.txt": b"linked"})
    orphan_pkg = _zip(tmp_path / "orphan.zip", {"b.txt": b"orphan"})
    payload = tmp_path / "p.archive"; payload.write_bytes(b"payload")

    installed = SD.stage_and_deploy(
        game, "Installed", [(payload, "archive/pc/mod/x.archive")],
        source_archive=str(installed_pkg), auto_deploy=False,
    )["archive"]
    with SD.Store(game, create=False) as store:
        linked = AM.import_archive(store, linked_pkg)
        AM.link_nexus(store, linked["id"], 9999)
        orphan = AM.import_archive(store, orphan_pkg)

        preview = AM.cleanup_orphans(store, apply=False)
        assert preview["dry_run"] is True
        assert [x["id"] for x in preview["items"]] == [orphan["id"]]
        assert Path(orphan["path"]).exists()

        applied = AM.cleanup_orphans(store, apply=True)
        assert applied["deleted"] == 1
        assert not Path(orphan["path"]).exists()
        rows = AM.list_archives(store, refresh=False)["items"]
        ids = {x["id"] for x in rows}
        assert installed["id"] in ids
        assert linked["id"] in ids


def test_refresh_indexes_existing_archives_and_cli_json(tmp_path, capsys):
    game = tmp_path / "game"; game.mkdir()
    root = AM.archive_root(game); root.mkdir(parents=True)
    package = _zip(root / "manual.zip")
    with SD.Store(game) as store:
        result = AM.list_archives(store, refresh=True)
        assert result["archives"] == 1
        assert result["items"][0]["file_name"] == "manual.zip"

    rc = AM.main(["list", "--game-root", str(game), "--no-refresh"])
    assert rc == 0
    line = [x for x in capsys.readouterr().out.splitlines() if x.startswith(AM.JSON_PREFIX)][-1]
    payload = json.loads(line[len(AM.JSON_PREFIX):])
    assert payload["archives"] == 1
    assert package.exists()


def test_explicit_archive_nexus_link_is_consumed_by_conflict_engine(tmp_path):
    from nexus_core import conflict_detection as C

    game = tmp_path / "game"; game.mkdir()
    package = _zip(tmp_path / "source-without-id.zip")
    a = tmp_path / "a.reds"; a.write_text("@replaceMethod(PlayerPuppet)\npublic func Foo() -> Void {}")
    b = tmp_path / "b.reds"; b.write_text("@replaceMethod(PlayerPuppet)\npublic func Foo() -> Void {}")
    installed = SD.stage_and_deploy(
        game, "Mystery Mod", [(a, "r6/scripts/a.reds")], source_archive=str(package), auto_deploy=False,
    )["archive"]
    with SD.Store(game, create=False) as store:
        AM.link_nexus(store, installed["id"], 4242)
        store.stage_mod("Other Mod", [(b, "r6/scripts/b.reds")])
        row = next(x for x in C.scan(store)["items"] if x["kind"] == "redscript")
        mystery = next(x for x in row["mods"] if x["name"] == "Mystery Mod")
        assert mystery["nexus_id"] == "4242"
        assert mystery["nexus_url"].endswith("/mods/4242")
