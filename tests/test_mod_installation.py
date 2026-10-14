import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D
from nexus_core import mod_installation as I
from nexus_core import smart_deployment as S


def _write(root: Path, rel: str, data: bytes):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _session(tmp_path: Path, extract: Path, name="NewMod-123-1-0"):
    I.SESSION_ROOT = tmp_path / "sessions"
    sess = I.SESSION_ROOT / "install_test"
    sess.mkdir(parents=True)
    target = sess / "extract"
    if target.exists():
        D.force_rmtree(target)
    target.mkdir()
    # copy fixture tree into the actual session extraction directory
    for p in extract.rglob("*"):
        rel = p.relative_to(extract)
        out = target / rel
        if p.is_dir(): out.mkdir(parents=True, exist_ok=True)
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(p.read_bytes())
    marker = {
        "version": 1, "archive": str(tmp_path / f"{name}.zip"),
        "archive_name": f"{name}.zip", "mod_name": name,
        "size": 1, "mtime_ns": 1, "created": "2026-09-13T00:00:00",
    }
    (sess / I.SESSION_MARKER).write_text(json.dumps(marker))
    return sess


def test_fomod_explicit_choice_changes_file_plan(tmp_path):
    ext = tmp_path / "ext"
    _write(ext, "A/archive/pc/mod/a.archive", b"A")
    _write(ext, "B/archive/pc/mod/b.archive", b"B")
    (ext / "fomod").mkdir(parents=True)
    cfg = ext / "fomod/ModuleConfig.xml"
    cfg.write_text('''<config><moduleName>Choice Mod</moduleName><installSteps order="Explicit">
      <installStep name="Variant"><optionalFileGroups order="Explicit">
       <group name="Pick one" type="SelectExactlyOne"><plugins order="Explicit">
        <plugin name="A"><files><file source="A/archive/pc/mod/a.archive" destination="archive/pc/mod/a.archive"/></files><typeDescriptor><type name="Recommended"/></typeDescriptor></plugin>
        <plugin name="B"><files><file source="B/archive/pc/mod/b.archive" destination="archive/pc/mod/b.archive"/></files><typeDescriptor><type name="Optional"/></typeDescriptor></plugin>
       </plugins></group></optionalFileGroups></installStep></installSteps></config>''')
    pairs, model = I.build_fomod_plan(cfg)
    assert [d for _s, d in pairs] == ["archive/pc/mod/a.archive"]
    gid = model["groups"][0]["id"]
    pairs, model = I.build_fomod_plan(cfg, {gid: ["B"]})
    assert [d for _s, d in pairs] == ["archive/pc/mod/b.archive"]
    assert model["groups"][0]["selected"] == ["B"]


def test_fomod_rejects_source_and_destination_traversal(tmp_path):
    ext = tmp_path / "ext"
    outside = tmp_path / "secret.archive"
    outside.write_bytes(b"SECRET")
    _write(ext, "ok.archive", b"OK")
    (ext / "fomod").mkdir(parents=True)
    cfg = ext / "fomod/ModuleConfig.xml"
    cfg.write_text('''<config><requiredInstallFiles>
      <file source="../secret.archive" destination="archive/pc/mod/stolen.archive"/>
      <file source="ok.archive" destination="../escaped.archive"/>
      <file source="ok.archive" destination="archive/pc/mod/ok.archive"/>
    </requiredInstallFiles></config>''')
    pairs, model = I.build_fomod_plan(cfg)
    assert [(s.name, d) for s, d in pairs] == [("ok.archive", "archive/pc/mod/ok.archive")]
    assert model["unsafe_entries"] == 2
    legacy_pairs, _summary = D.plan_fomod(cfg)
    assert [(s.name, d) for s, d in legacy_pairs] == [("ok.archive", "archive/pc/mod/ok.archive")]


def test_preview_marks_managed_conflict_and_defaults_to_skip(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    ext = tmp_path / "ext"
    _write(game, "archive/pc/mod/shared.archive", b"OLD")
    _write(ext, "archive/pc/mod/shared.archive", b"NEW")
    D.save_manifest(game, {"OldMod-1-1-0": ["archive/pc/mod/shared.archive"]})
    p = I.preview_extracted(ext, game, "NewMod-2-1-0")
    row = p["rows"][0]
    assert row["status"] == "conflict"
    assert row["owners"] == ["OldMod-1-1-0"]
    assert row["default_action"] == "skip"
    assert p["conflicts"] == 1


def test_apply_requires_migration_when_legacy_manifest_exists(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    ext = tmp_path / "fixture"
    _write(game, "archive/pc/mod/old.archive", b"OLD")
    _write(ext, "archive/pc/mod/new.archive", b"NEW")
    D.save_manifest(game, {"Old": ["archive/pc/mod/old.archive"]})
    sess = _session(tmp_path, ext, "New")
    preview = I.preview_session(sess, game)
    assert preview["migration_required"]
    try:
        I.apply_session(sess, game)
    except RuntimeError as e:
        assert "migration" in str(e).lower()
    else:
        raise AssertionError("hybrid legacy + smart install should be refused")


def test_apply_skip_preserves_owner_then_overwrite_transfers_projection(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    ext = tmp_path / "fixture"
    old_src = _write(tmp_path, "old/shared.archive", b"OLD")
    _write(ext, "archive/pc/mod/shared.archive", b"NEW")
    rel = "archive/pc/mod/shared.archive"
    with S.Store(game) as store:
        store.stage_mod("OldMod-1-1-0", [(old_src, rel)])
        store.deploy()
    sess = _session(tmp_path, ext, "NewMod-2-1-0")

    out = I.apply_session(sess, game, default_conflict="skip")
    assert out["ok"]
    assert (game / rel).read_bytes() == b"OLD"
    with S.Store(game, create=False) as store:
        desired, _ = store.desired()
        assert desired[rel].mod_name == "OldMod-1-1-0"
        assert set(store.owners(rel, projected=True)) == {"OldMod-1-1-0", "NewMod-2-1-0"}

    out = I.apply_session(sess, game, resolutions={rel: "overwrite"})
    assert out["ok"]
    assert (game / rel).read_bytes() == b"NEW"
    with S.Store(game, create=False) as store:
        desired, _ = store.desired()
        assert desired[rel].mod_name == "NewMod-2-1-0"


def test_installing_disabled_smart_mod_reenables_active_profile(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    ext = tmp_path / "fixture"
    src = _write(ext, "archive/pc/mod/x.archive", b"X")
    name = "Blocked-55-1-0"
    rel = "archive/pc/mod/x.archive"
    with S.Store(game) as store:
        store.stage_mod(name, [(src, rel)])
        store.set_enabled(name, False)
        store.deploy()
    sess = _session(tmp_path, ext, name)
    out = I.apply_session(sess, game)
    assert out["ok"]
    with S.Store(game, create=False) as store:
        row = next(m for m in store.installed_mods() if m["name"] == name)
        assert row["enabled"]
    assert (game / rel).exists()


def test_deployer_place_rejects_destination_outside_game(tmp_path):
    game = tmp_path / "game"; game.mkdir()
    src = tmp_path / "src"; src.write_bytes(b"X")
    dep = D.Deployer(game, dry_run=False, verbose=False)
    try:
        dep._place(src, tmp_path / "escaped.bin", "Bad")
    except ValueError as e:
        assert "escapes game root" in str(e)
    else:
        raise AssertionError("outside destination must be rejected")

def test_fomod_flag_choice_reveals_later_step(tmp_path):
    ext = tmp_path / "ext"
    _write(ext, "a.archive", b"A")
    _write(ext, "b.archive", b"B")
    _write(ext, "extra.archive", b"E")
    (ext / "fomod").mkdir(parents=True)
    cfg = ext / "fomod/ModuleConfig.xml"
    cfg.write_text('''<config><installSteps order="Explicit">
      <installStep name="Mode"><optionalFileGroups><group name="Mode" type="SelectExactlyOne"><plugins>
        <plugin name="A"><files><file source="a.archive" destination="archive/pc/mod/a.archive"/></files><conditionFlags><flag name="mode">a</flag></conditionFlags><typeDescriptor><type name="Recommended"/></typeDescriptor></plugin>
        <plugin name="B"><files><file source="b.archive" destination="archive/pc/mod/b.archive"/></files><conditionFlags><flag name="mode">b</flag></conditionFlags><typeDescriptor><type name="Optional"/></typeDescriptor></plugin>
      </plugins></group></optionalFileGroups></installStep>
      <installStep name="B extras"><visible><flagDependency flag="mode" value="b"/></visible><optionalFileGroups>
        <group name="Extras" type="SelectAny"><plugins><plugin name="Extra"><files><file source="extra.archive" destination="archive/pc/mod/extra.archive"/></files><typeDescriptor><type name="Optional"/></typeDescriptor></plugin></plugins></group>
      </optionalFileGroups></installStep>
    </installSteps></config>''')
    _pairs, default_model = I.build_fomod_plan(cfg)
    assert [g["step"] for g in default_model["groups"]] == ["Mode"]
    gid = default_model["groups"][0]["id"]
    pairs, selected_model = I.build_fomod_plan(cfg, {gid: ["B"]})
    assert [g["step"] for g in selected_model["groups"]] == ["Mode", "B extras"]
    assert {d for _s, d in pairs} == {"archive/pc/mod/b.archive", "archive/pc/mod/extra.archive"}


def test_installer_disable_enable_state(tmp_path, capsys):
    game = tmp_path / "game"; game.mkdir()
    rel = "archive/pc/mod/toggle.archive"
    _write(game, rel, b"T")
    stem = "ToggleMod-77-1-0"
    D.save_manifest(game, {stem: [rel]})
    rc = I.main(["disable", stem, "--game-root", str(game), "--downloads", str(tmp_path), "--ignore-running"])
    assert rc == 0
    state = I.installed_state(game)
    row = next(m for m in state["mods"] if m["name"] == stem)
    assert row["status"] == "disabled"
    assert not (game / rel).exists()
    rc = I.main(["enable", stem, "--game-root", str(game), "--downloads", str(tmp_path), "--ignore-running"])
    assert rc == 0
    assert D.load_disabled(game) == {}
    capsys.readouterr()
