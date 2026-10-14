import json
import os
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import smart_deployment as S


def _src(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / "sources" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _inode(p: Path):
    st = p.stat()
    return st.st_dev, st.st_ino


def test_stage_deploy_and_undeploy_use_hardlinks(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    src = _src(tmp_path, "a.archive", b"AAA")
    with S.Store(game) as st:
        staged = st.stage_mod("A", [(src, "archive/pc/mod/a.archive")])
        out = st.deploy()
        assert out["ok"]
        target = game / "archive/pc/mod/a.archive"
        row = st.conn.execute("SELECT stage_rel FROM files").fetchone()
        canonical = st.root / row[0]
        assert target.read_bytes() == b"AAA"
        assert _inode(target) == _inode(canonical)
        assert canonical.stat().st_nlink >= 2
        out = st.undeploy()
        assert "archive/pc/mod/a.archive" in out["removed"]
        assert not target.exists()
        assert canonical.read_bytes() == b"AAA"


def test_reinstall_publishes_fresh_inode_then_rebuilds_link(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    one = _src(tmp_path, "one.archive", b"ONE")
    two = _src(tmp_path, "two.archive", b"TWO")
    rel = "archive/pc/mod/x.archive"
    with S.Store(game) as st:
        st.stage_mod("A", [(one, rel)])
        st.deploy()
        target = game / rel
        old_inode = _inode(target)
        st.stage_mod("A", [(two, rel)])
        # Game projection is intentionally still the old immutable inode until rebuild.
        assert target.read_bytes() == b"ONE"
        assert _inode(target) == old_inode
        drift = st.drift()
        assert drift["items"][0]["status"] == "stale-link"
        out = st.deploy()
        assert out["ok"]
        assert target.read_bytes() == b"TWO"
        assert _inode(target) != old_inode
        row = st.conn.execute("SELECT stage_rel FROM files WHERE rel_path=?", (rel,)).fetchone()
        assert _inode(target) == _inode(st.root / row[0])


def test_drift_detects_external_replace_and_undeploy_preserves_it(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    src = _src(tmp_path, "x", b"X")
    rel = "r6/scripts/x.reds"
    with S.Store(game) as st:
        st.stage_mod("A", [(src, rel)])
        st.deploy()
        target = game / rel
        target.unlink()
        target.write_bytes(b"EXTERNAL")
        drift = st.drift()
        assert drift["drift_count"] == 1
        assert drift["items"][0]["status"] == "replaced"
        out = st.undeploy()
        assert out["preserved"]
        assert target.read_bytes() == b"EXTERNAL"


def test_drift_detects_in_place_hardlink_mutation(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    src = _src(tmp_path, "x", b"ORIGINAL")
    rel = "archive/pc/mod/x.archive"
    with S.Store(game) as st:
        st.stage_mod("A", [(src, rel)])
        st.deploy()
        (game / rel).write_bytes(b"MUTATED")
        drift = st.drift()
        assert drift["items"][0]["status"] == "modified-in-place"


def test_conflict_skip_and_overwrite_are_persistent_preferences(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    a = _src(tmp_path, "a", b"A")
    b = _src(tmp_path, "b", b"B")
    rel = "archive/pc/mod/shared.archive"
    with S.Store(game) as st:
        st.stage_mod("A", [(a, rel)])
        st.deploy()
        st.stage_mod("B", [(b, rel)], resolutions={rel: "skip"})
        st.deploy()
        assert (game / rel).read_bytes() == b"A"
        st.stage_mod("B", [(b, rel)], resolutions={rel: "overwrite"})
        st.deploy()
        assert (game / rel).read_bytes() == b"B"
        # Rebuild does not forget the explicit winner.
        st.undeploy(); st.deploy()
        assert (game / rel).read_bytes() == b"B"


def test_external_overwrite_is_path_scoped_and_backed_up(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    rel = "archive/pc/mod/shared.archive"
    target = game / rel; target.parent.mkdir(parents=True); target.write_bytes(b"EXTERNAL")
    src = _src(tmp_path, "new", b"NEW")
    with S.Store(game) as st:
        st.stage_mod("A", [(src, rel)], resolutions={rel: "overwrite"})
        blocked = st.deploy()
        assert not blocked["ok"]
        assert target.read_bytes() == b"EXTERNAL"
        allowed = st.deploy(force_external_paths={rel})
        assert allowed["ok"]
        assert target.read_bytes() == b"NEW"
        assert (st.root / "external_backups" / rel).read_bytes() == b"EXTERNAL"


def test_profile_switch_is_db_flag_plus_link_rebuild(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    a = _src(tmp_path, "a", b"A")
    b = _src(tmp_path, "b", b"B")
    rel = "archive/pc/mod/shared.archive"
    with S.Store(game) as st:
        st.stage_mod("A", [(a, rel)])
        st.stage_mod("B", [(b, rel)], resolutions={rel: "overwrite"})
        st.deploy()
        assert (game / rel).read_bytes() == b"B"
        st.create_profile("A only")
        st.set_enabled("B", False, "A only")
        st.switch_profile_flag("A only")
        st.deploy()
        assert st.active_profile == "A only"
        assert (game / rel).read_bytes() == b"A"
        assert sum(1 for p in st.profiles() if p["active"]) == 1


def test_redmod_archive_is_projected_without_redmod_exe(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    arc = _src(tmp_path, "cool.archive", b"ARC")
    script = _src(tmp_path, "logic.script", b"SCRIPT")
    with S.Store(game) as st:
        st.stage_mod("Cool", [
            (arc, "mods/Cool/archives/cool.archive"),
            (script, "mods/Cool/scripts/logic.script"),
        ])
        out = st.deploy()
        assert (game / "archive/pc/mod/Cool__cool.archive").read_bytes() == b"ARC"
        assert not (game / "mods/Cool/scripts/logic.script").exists()
        assert out["redmod_blocked"][0]["path"] == "mods/Cool/scripts/logic.script"
        assert "redMod.exe" not in json.dumps(out)


def test_migrate_legacy_manifest_copies_then_relinks_and_moves_state(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    rel = "archive/pc/mod/old.archive"
    old = game / rel; old.parent.mkdir(parents=True); old.write_bytes(b"OLD")
    (game / ".mod_manifest.json").write_text(json.dumps({"mods": {"OldMod": [rel]}}))
    out = S.migrate_legacy(game)
    assert out["imported_mods"] == 1
    assert not (game / ".mod_manifest.json").exists()
    with S.Store(game, create=False) as st:
        row = st.conn.execute("SELECT stage_rel FROM files").fetchone()
        canonical = st.root / row[0]
        assert _inode(game / rel) == _inode(canonical)
        assert (st.root / "legacy_state/.mod_manifest.json").is_file()


def test_migration_respects_legacy_disabled_state(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    rel = "archive/pc/mod/off.archive"
    p = game / rel; p.parent.mkdir(parents=True); p.write_bytes(b"OFF")
    (game / ".mod_manifest.json").write_text(json.dumps({"mods": {"OffMod": [rel]}}))
    (game / ".mod_disabled.json").write_text(json.dumps({"offmod": {"stem": "OffMod", "scope": "all"}}))
    S.migrate_legacy(game)
    with S.Store(game, create=False) as st:
        mod = next(m for m in st.installed_mods() if m["name"] == "OffMod")
        assert not mod["enabled"]
        assert not (game / rel).exists()


def test_path_traversal_and_staging_self_projection_are_rejected(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    src = _src(tmp_path, "x", b"X")
    with S.Store(game) as st:
        for rel in ("../escape", "/absolute", "C:/drive", "downloaded_mods/evil"):
            try:
                st.stage_mod("Bad", [(src, rel)])
            except ValueError:
                pass
            else:
                raise AssertionError(rel)


def test_local_deploy_endpoint(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    src = _src(tmp_path, "x", b"X")
    rel = "archive/pc/mod/api.archive"
    with S.Store(game) as st:
        st.stage_mod("API", [(src, rel)])
    server = ThreadingHTTPServer(("127.0.0.1", 0), S._APIHandler)
    server.game_root = game
    server.api_token = None
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/deploy", method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode())
        assert payload["ok"]
        assert (game / rel).read_bytes() == b"X"
    finally:
        server.shutdown(); server.server_close(); t.join(timeout=3)


def test_atomic_relink_failure_preserves_previous_game_path(tmp_path, monkeypatch):
    game = tmp_path / "game"; game.mkdir()
    one = _src(tmp_path, "one2", b"ONE")
    two = _src(tmp_path, "two2", b"TWO")
    rel = "archive/pc/mod/atomic.archive"
    with S.Store(game) as st:
        st.stage_mod("Atomic", [(one, rel)])
        st.deploy()
        target = game / rel
        st.stage_mod("Atomic", [(two, rel)])
        real_link = S.os.link
        def fail_link(src, dst, *a, **kw):
            if Path(dst).name.startswith(".ncm-link-"):
                raise OSError(18, "Invalid cross-device link")
            return real_link(src, dst, *a, **kw)
        monkeypatch.setattr(S.os, "link", fail_link)
        try:
            st.deploy()
        except S.SmartDeploymentError:
            pass
        else:
            raise AssertionError("hardlink failure should surface")
        assert target.read_bytes() == b"ONE"


def test_smart_state_does_not_create_legacy_manager_metadata_in_game_root(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    src = _src(tmp_path, "state", b"X")
    with S.Store(game) as st:
        st.stage_mod("State", [(src, "archive/pc/mod/state.archive")])
        st.deploy()
    assert not (game / ".mod_manifest.json").exists()
    assert not (game / ".mod_disabled.json").exists()
    assert not (game / ".mod_manager.lock").exists()
    assert (game / "downloaded_mods/deployment.sqlite3").is_file()


def test_reinstall_skip_keeps_old_canonical_bytes_across_rebuild(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    old = _src(tmp_path, "skip-old", b"OLD")
    new = _src(tmp_path, "skip-new", b"NEW")
    rel = "archive/pc/mod/skip.archive"
    with S.Store(game) as st:
        st.stage_mod("SameMod", [(old, rel)])
        st.deploy()
        out = st.stage_mod("SameMod", [(new, rel)], resolutions={rel: "skip"})
        assert out["source_skipped"] == [rel]
        st.deploy()
        assert (game / rel).read_bytes() == b"OLD"
        st.undeploy(); st.deploy()
        assert (game / rel).read_bytes() == b"OLD"
