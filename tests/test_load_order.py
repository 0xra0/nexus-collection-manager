import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import load_order as L
from nexus_core import smart_deployment as S


def _src(tmp_path: Path, name: str, data: bytes | None = None) -> Path:
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if data is not None else name.encode())
    return p


def _stage(st: S.Store, tmp_path: Path, mod: str, archive: str):
    st.stage_mod(mod, [(_src(tmp_path, f"{mod}-{archive}"), f"archive/pc/mod/{archive}")])


def _inode(path: Path):
    s = path.stat()
    return s.st_dev, s.st_ino


def test_default_archive_order_is_ascii_and_first_is_highest_priority(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "C", "zeta.archive")
        _stage(st, tmp_path, "A", "Alpha.archive")
        _stage(st, tmp_path, "B", "beta.archive")
        plan = st.archive_load_order()
        assert [x["deployed_archive"] for x in plan["items"]] == ["Alpha.archive", "beta.archive", "zeta.archive"]
        assert plan["items"][0]["winner_priority"] == "highest"


def test_explicit_archive_order_and_dry_run_do_not_touch_game(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "a.archive")
        _stage(st, tmp_path, "B", "b.archive")
        items = st.archive_load_order()["items"]
        keys = [items[1]["key"], items[0]["key"]]
        dry = L.preview(st, keys)
        assert dry["desired"] == ["b.archive", "a.archive"]
        assert dry["would_change"]
        assert not (game / "archive/pc/mod/modlist.txt").exists()
        # Preview must not persist the working order either.
        assert [x["deployed_archive"] for x in st.archive_load_order()["items"]] == ["a.archive", "b.archive"]
        st.set_archive_order(keys)
        assert [x["deployed_archive"] for x in st.archive_load_order()["items"]] == ["b.archive", "a.archive"]


def test_apply_generates_staged_hardlinked_modlist(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "a.archive")
        _stage(st, tmp_path, "B", "b.archive")
        items = st.archive_load_order()["items"]
        out = L.apply_order(st, [items[1]["key"], items[0]["key"]])
        assert out["modlist"]["ok"]
        target = game / "archive/pc/mod/modlist.txt"
        assert target.read_text() == "b.archive\na.archive\n"
        row = st.conn.execute("SELECT stage_rel FROM generated_deployed WHERE rel_path=?", ("archive/pc/mod/modlist.txt",)).fetchone()
        canonical = st.root / row[0]
        assert _inode(target) == _inode(canonical)
        assert st.modlist_managed()


def test_pairwise_preference_rules_are_applied_and_cycles_rejected(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "Base", "a.archive")
        _stage(st, tmp_path, "Patch", "z.archive")
        st.set_load_rule("Patch", "Base", note="patch must win")
        assert [x["mod"] for x in st.archive_load_order()["items"]] == ["Patch", "Base"]
        assert st.load_order_rules()[0]["note"] == "patch must win"
        try:
            st.set_load_rule("Base", "Patch")
        except ValueError as e:
            assert "cycle" in str(e)
        else:
            raise AssertionError("cycle should be rejected")
        assert len(st.load_order_rules()) == 1


def test_profile_clone_copies_archive_order_and_rules(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "a.archive")
        _stage(st, tmp_path, "B", "b.archive")
        st.set_load_rule("B", "A")
        st.create_profile("Other")
        assert st.load_order_rules("Other")[0]["before_mod"] == "B"
        assert [x["mod"] for x in st.archive_load_order("Other")["items"]] == ["B", "A"]


def test_managed_modlist_regenerates_on_normal_deploy(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "a.archive")
        L.apply_order(st)
        target = game / "archive/pc/mod/modlist.txt"
        assert target.read_text() == "a.archive\n"
        _stage(st, tmp_path, "B", "b.archive")
        result = st.deploy()
        assert result["modlist"]["ok"]
        assert target.read_text() == "a.archive\nb.archive\n"


def test_external_modlist_requires_explicit_force_and_is_backed_up(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    target = game / "archive/pc/mod/modlist.txt"
    target.parent.mkdir(parents=True)
    target.write_text("external.archive\n")
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "a.archive")
        blocked = st.sync_modlist(enable_management=True)
        assert not blocked["ok"]
        assert target.read_text() == "external.archive\n"
        applied = st.sync_modlist(force_external=True)
        assert applied["ok"]
        assert target.read_text() == "a.archive\n"
        backups = list((st.root / "external_backups/archive/pc/mod").glob("modlist.txt.*.bak"))
        assert backups and backups[0].read_text() == "external.archive\n"


def test_modlist_drift_and_undeploy_are_safe(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "a.archive")
        st.deploy(); L.apply_order(st)
        target = game / "archive/pc/mod/modlist.txt"
        target.unlink(); target.write_text("external.archive\n")
        drift = st.drift()
        row = next(x for x in drift["items"] if x["path"].endswith("modlist.txt"))
        assert row["status"] == "replaced"
        out = st.undeploy()
        assert target.read_text() == "external.archive\n"
        assert any(x["path"].endswith("modlist.txt") for x in out["preserved"])


def test_mod_package_cannot_install_its_own_modlist(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    real = _src(tmp_path, "real.archive")
    fake_list = _src(tmp_path, "modlist.txt", b"evil.archive\n")
    with S.Store(game) as st:
        out = st.stage_mod("A", [
            (real, "archive/pc/mod/a.archive"),
            (fake_list, "archive/pc/mod/modlist.txt"),
        ])
        assert "archive/pc/mod/modlist.txt" in out["source_skipped"]
        assert not st.conn.execute("SELECT 1 FROM files WHERE rel_path LIKE '%modlist.txt'").fetchone()


def test_cli_preview_and_apply(tmp_path, capsys):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "a.archive")
        key = st.archive_load_order()["items"][0]["key"]
    assert L.main(["preview", "--game-root", str(game), "--archive-key", key]) == 0
    payload = json.loads(capsys.readouterr().out.split(L.JSON_PREFIX, 1)[1])
    assert payload["type"] == "preview"
    assert not (game / "archive/pc/mod/modlist.txt").exists()
    assert L.main(["apply", "--game-root", str(game), "--archive-key", key]) == 0
    assert (game / "archive/pc/mod/modlist.txt").read_text() == "a.archive\n"

def test_manual_dry_run_predicts_removal_of_legacy_aliases(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    with S.Store(game) as st:
        _stage(st, tmp_path, "A", "zeta.archive")
        _stage(st, tmp_path, "B", "alpha.archive")
        st.set_load_order(["A", "B"])
        st.deploy()
        current = st.archive_load_order()["items"]
        assert any(str(x["deployed_archive"]).startswith("!ncm-") for x in current)
        dry = L.preview(st, [x["key"] for x in current])
        assert dry["link_changes"]
        assert dry["desired"] == ["zeta.archive", "alpha.archive"]
        # Dry-run is non-mutating: aliases still exist until Apply.
        assert any(str(x["deployed_archive"]).startswith("!ncm-") for x in st.archive_load_order()["items"])
