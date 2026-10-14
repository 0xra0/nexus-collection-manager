import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import conflict_detection as C
from nexus_core import smart_deployment as S


def _src(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _archive(tmp_path: Path, name: str, hashes: list[int]) -> Path:
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    index_pos = 44
    records = b"".join(
        struct.pack("<QqIIIII20s", h, 0, 0, 0, 0, 0, 0, b"\0" * 20)
        for h in hashes
    )
    table = struct.pack("<IIQIII", 8, 20 + len(records), 0, len(hashes), 0, 0) + records
    total = 44 + len(table)
    header = struct.pack("<4sIQIQIQI", b"RDAR", 12, index_pos, len(table), 0, 0, total - 8, 0)
    p.write_bytes(header + table)
    return p


def test_read_archive_index_extracts_resource_hashes(tmp_path):
    arc = _archive(tmp_path, "a.archive", [1, 2, 0xDEADBEEF])
    idx = C.read_archive_index(arc)
    assert idx.warning == ""
    assert idx.version == 12
    assert idx.resource_hashes == frozenset({1, 2, 0xDEADBEEF})


def test_archive_resource_conflict_and_nexus_links(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    a = _archive(tmp_path, "Alpha-1234-1-0.archive", [10, 20, 30])
    b = _archive(tmp_path, "Beta-5678-1-0.archive", [20, 30, 40])
    with S.Store(game) as st:
        st.stage_mod("Alpha-1234-1-0", [(a, "archive/pc/mod/a.archive")], source_archive=a.name)
        st.stage_mod("Beta-5678-1-0", [(b, "archive/pc/mod/b.archive")], source_archive=b.name)
        result = C.scan(st)
        rows = [x for x in result["items"] if x["kind"] == "archive-resource"]
        assert len(rows) == 1
        row = rows[0]
        assert row["resource_count"] == 2
        assert row["severity"] == "high"
        urls = {m["name"]: m["nexus_url"] for m in row["mods"]}
        assert urls["Alpha-1234-1-0"].endswith("/mods/1234")
        assert urls["Beta-5678-1-0"].endswith("/mods/5678")


def test_patch_relation_downgrades_archive_severity(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    a = _archive(tmp_path, "Body-1234.archive", list(range(40)))
    b = _archive(tmp_path, "Body-Patch-5678.archive", list(range(20, 60)))
    with S.Store(game) as st:
        st.stage_mod("Body Overhaul", [(a, "archive/pc/mod/body.archive")], source_archive=a.name)
        st.stage_mod("Body Overhaul Compatibility Patch", [(b, "archive/pc/mod/body_patch.archive")], source_archive=b.name)
        result = C.scan(st)
        row = next(x for x in result["items"] if x["kind"] == "archive-resource")
        assert row["severity"] == "medium"  # ordinary high -> medium when dependency/patch context exists
        assert "compatibility/patch" in row["dependency_context"]


def test_file_overlap_conflict_and_prefer_action(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    a = _src(tmp_path, "a.reds", b"A")
    b = _src(tmp_path, "b.reds", b"B")
    rel = "r6/scripts/shared.reds"
    with S.Store(game) as st:
        st.stage_mod("A", [(a, rel)])
        st.stage_mod("B", [(b, rel)], resolutions={rel: "overwrite"})
        result = C.scan(st)
        row = next(x for x in result["items"] if x["kind"] == "file-overlap")
        assert row["severity"] == "critical"
        out = C.action_prefer(st, row["id"], "A")
        assert out["policy"]["type"] == "file-owner"
        desired, _ = st.desired()
        assert next(iter(desired.values())).mod_name == "A"


def test_archive_prefer_creates_ascii_aliases_and_first_winner(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    a = _archive(tmp_path, "A-1234.archive", [1])
    b = _archive(tmp_path, "B-5678.archive", [1])
    with S.Store(game) as st:
        st.stage_mod("A", [(a, "archive/pc/mod/zeta.archive")], source_archive=a.name)
        st.stage_mod("B", [(b, "archive/pc/mod/alpha.archive")], source_archive=b.name)
        row = next(x for x in C.scan(st)["items"] if x["kind"] == "archive-resource")
        out = C.action_prefer(st, row["id"], "A")
        assert out["policy"]["order"][0] == "A"
        desired, _ = st.desired()
        paths = sorted(desired)
        assert paths[0].split("/")[-1].startswith("!ncm-000000--")
        assert paths[1].split("/")[-1].startswith("!ncm-000001--")
        assert desired[paths[0]].mod_name == "A"


def test_redscript_replacement_and_addmethod_conflicts(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    a = _src(tmp_path, "a.reds", b"@replaceMethod(PlayerPuppet)\npublic func Foo() -> Void {}\n@addMethod(PlayerPuppet)\npublic func Bar() -> Void {}")
    b = _src(tmp_path, "b.reds", b"@replaceMethod(PlayerPuppet)\npublic func Foo() -> Void {}\n@addMethod(PlayerPuppet)\npublic func Bar() -> Void {}")
    with S.Store(game) as st:
        st.stage_mod("A", [(a, "r6/scripts/a.reds")])
        st.stage_mod("B", [(b, "r6/scripts/b.reds")])
        rows = [x for x in C.scan(st)["items"] if x["kind"] == "redscript"]
        titles = "\n".join(x["title"] for x in rows)
        assert "PlayerPuppet::Foo" in titles
        assert "PlayerPuppet::Bar" in titles
        assert all(x["severity"] == "critical" for x in rows)


def test_redscript_wrappers_do_not_conflict(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    src = b"@wrapMethod(PlayerPuppet)\npublic func Foo() -> Void { wrappedMethod(); }"
    with S.Store(game) as st:
        st.stage_mod("A", [(_src(tmp_path, "a.reds", src), "r6/scripts/a.reds")])
        st.stage_mod("B", [(_src(tmp_path, "b.reds", src), "r6/scripts/b.reds")])
        assert not [x for x in C.scan(st)["items"] if x["kind"] == "redscript"]


def test_tweakxl_direct_assignment_conflict(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    a = _src(tmp_path, "a.yaml", b"Items.Test:\n  price: 10\n  tags:\n    - !append-once Quality.IconicItem\n")
    b = _src(tmp_path, "b.yaml", b"Items.Test:\n  price: 20\n  tags:\n    - !append-once Quality.LegendaryItem\n")
    with S.Store(game) as st:
        st.stage_mod("A", [(a, "r6/tweaks/a.yaml")])
        st.stage_mod("B", [(b, "r6/tweaks/b.yaml")])
        rows = [x for x in C.scan(st)["items"] if x["kind"] == "tweakxl"]
        price = next(x for x in rows if x["tweak_key"] == "Items.Test.price")
        assert price["severity"] == "high"
        tags = next(x for x in rows if x["tweak_key"] == "Items.Test.tags")
        assert tags["severity"] == "medium"
        assert tags["operations"] == ["mutation"]


def test_disable_and_uninstall_actions_from_inbox(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    a = _src(tmp_path, "a.lua", b"A")
    b = _src(tmp_path, "b.lua", b"B")
    rel = "bin/x64/plugins/cyber_engine_tweaks/mods/shared/init.lua"
    with S.Store(game) as st:
        st.stage_mod("A", [(a, rel)])
        st.stage_mod("B", [(b, rel)], resolutions={rel: "overwrite"})
        row = next(x for x in C.scan(st)["items"] if x["kind"] == "file-overlap")
        C.action_disable(st, row["id"], "B")
        assert next(m for m in st.installed_mods() if m["name"] == "B")["enabled"] == 0
        # Re-enable B, rescan, then uninstall it through the inbox action.
        st.set_enabled("B", True)
        row = next(x for x in C.scan(st)["items"] if x["kind"] == "file-overlap")
        C.action_uninstall(st, row["id"], "B")
        assert st._mod_row("B") is None
        assert st._mod_row("A") is not None


def test_inbox_persists_resolution_status(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    a = _src(tmp_path, "a", b"A"); b = _src(tmp_path, "b", b"B")
    rel = "engine/config/platform/pc/shared.ini"
    with S.Store(game) as st:
        st.stage_mod("A", [(a, rel)])
        st.stage_mod("B", [(b, rel)], resolutions={rel: "overwrite"})
        row = next(x for x in C.scan(st)["items"] if x["kind"] == "file-overlap")
        C.action_prefer(st, row["id"], "A")
        saved = st.conn.execute("SELECT status,resolution FROM conflict_inbox WHERE id=?", (row["id"],)).fetchone()
        assert saved["status"] == "resolved"
        assert "preferred A" in saved["resolution"]
        # Rescan keeps the resolution attached to the same stable conflict ID.
        again = C.scan(st)
        item = next(x for x in again["items"] if x["id"] == row["id"])
        assert item["status"] == "resolved"
