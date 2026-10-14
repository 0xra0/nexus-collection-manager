"""Regression tests for the FOMOD installer in nexus_core.deployer.

These guard the behaviours that previously caused "FOMOD mods not installing":
case-mismatched source paths on a case-sensitive filesystem, ignored condition
flags / conditionalFileInstalls, priority collisions, and the authoritative-vs-
fallback deploy decision. Pure stdlib — no PySide6, no network, no external tools.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402


def _write(root: Path, rel: str, data: bytes = b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _full_fixture(root: Path):
    """A mod whose disk casing deliberately differs from the XML's casing."""
    _write(root, "00 Core/archive/pc/mod/core.archive", b"CORE")
    _write(root, "01 Textures/HD/archive/pc/mod/tex.archive", b"HD-TEX")
    _write(root, "scripts/r6/scripts/mod.reds", b"REDS")
    _write(root, "Extras/hd_patch.archive", b"HD-PATCH")
    _write(root, "Base/archive/pc/mod/shared.archive", b"LOWPRI")
    _write(root, "Override/archive/pc/mod/shared.archive", b"HIGHPRI")
    (root / "fomod").mkdir(parents=True, exist_ok=True)
    (root / "fomod" / "ModuleConfig.xml").write_text(r"""<?xml version="1.0" encoding="utf-8"?>
<config>
  <moduleName>Test Mod</moduleName>
  <requiredInstallFiles>
    <folder source="00 CORE\Archive\PC\Mod" destination="archive\pc\mod" priority="0"/>
    <file   source="Base\archive\pc\mod\shared.archive"     destination="archive\pc\mod\shared.archive" priority="0"/>
    <file   source="OVERRIDE\archive\pc\mod\shared.archive" destination="archive\pc\mod\shared.archive" priority="5"/>
    <folder source="Scripts" destination=""/>
  </requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Textures">
      <optionalFileGroups order="Explicit">
        <group name="Texture Quality" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="HD">
              <files><folder source="01 TEXTURES\HD\Archive\PC\Mod" destination="archive\pc\mod"/></files>
              <conditionFlags><flag name="hd">on</flag></conditionFlags>
              <typeDescriptor><type name="Recommended"/></typeDescriptor>
            </plugin>
            <plugin name="None">
              <files/>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls>
    <patterns>
      <pattern>
        <dependencies operator="And"><flagDependency flag="hd" value="on"/></dependencies>
        <files><file source="EXTRAS\hd_patch.archive" destination="archive\pc\mod\hd_patch.archive"/></files>
      </pattern>
      <pattern>
        <dependencies operator="And"><flagDependency flag="hd" value="off"/></dependencies>
        <files><file source="EXTRAS\nope.archive" destination="archive\pc\mod\nope.archive"/></files>
      </pattern>
    </patterns>
  </conditionalFileInstalls>
</config>
""", encoding="utf-8")


def test_plan_resolves_casing_flags_and_priority(tmp_path):
    extract = tmp_path / "extract"
    _full_fixture(extract)
    cfg = D.find_fomod_config(extract)
    assert cfg is not None
    pairs, _summary = D.plan_fomod(cfg)
    dest_map = {dest: src for src, dest in pairs}

    # case-mismatched required folder resolved
    assert "archive/pc/mod/core.archive" in dest_map
    # recommended plugin auto-selected, and its flag gated the conditional install
    assert "archive/pc/mod/tex.archive" in dest_map
    assert "archive/pc/mod/hd_patch.archive" in dest_map
    # the hd=off pattern must NOT fire (flag is on)
    assert "archive/pc/mod/nope.archive" not in dest_map
    # empty destination -> mirrored to game root
    assert "r6/scripts/mod.reds" in dest_map
    # priority collision -> higher priority source wins
    assert dest_map["archive/pc/mod/shared.archive"].read_bytes() == b"HIGHPRI"


def test_deploy_copies_only_selected_files(tmp_path):
    extract = tmp_path / "extract"
    game = tmp_path / "game"
    game.mkdir()
    _full_fixture(extract)
    dep = D.Deployer(game, dry_run=False, verbose=False)
    assert dep.deploy(extract, "Test Mod") is True
    assert (game / "archive/pc/mod/core.archive").read_bytes() == b"CORE"
    assert (game / "archive/pc/mod/shared.archive").read_bytes() == b"HIGHPRI"
    assert (game / "r6/scripts/mod.reds").read_bytes() == b"REDS"
    assert not (game / "archive/pc/mod/nope.archive").exists()


def test_fomod_with_missing_sources_falls_back_to_structure_scan(tmp_path):
    extract = tmp_path / "extract"
    game = tmp_path / "game"
    game.mkdir()
    (extract / "fomod").mkdir(parents=True)
    (extract / "fomod" / "ModuleConfig.xml").write_text(
        '<config><requiredInstallFiles>'
        '<file source="missing/nope.archive" destination="archive/pc/mod/nope.archive"/>'
        '</requiredInstallFiles></config>')
    _write(extract, "archive/pc/mod/real.archive", b"REAL")
    dep = D.Deployer(game, dry_run=False, verbose=False)
    assert dep.deploy(extract, "Broken") is True
    assert (game / "archive/pc/mod/real.archive").read_bytes() == b"REAL"
    assert not (game / "archive/pc/mod/nope.archive").exists()


def test_utf16_bom_moduleconfig_parses(tmp_path):
    extract = tmp_path / "extract"
    (extract / "fomod").mkdir(parents=True)
    _write(extract, "data/x.archive", b"C")
    xml = ('<config><requiredInstallFiles>'
           '<file source="data/x.archive" destination="archive/pc/mod/x.archive"/>'
           '</requiredInstallFiles></config>')
    (extract / "fomod" / "ModuleConfig.xml").write_bytes(("﻿" + xml).encode("utf-16"))
    cfg = D.find_fomod_config(extract)
    pairs, _ = D.plan_fomod(cfg)
    assert any(dest == "archive/pc/mod/x.archive" for _src, dest in pairs)


def test_select_any_all_optional_installs_everything(tmp_path):
    """A SelectAny group of all-Optional plugins (e.g. Load Begone) must auto-select
    every usable option, not nothing — otherwise a real mod installs empty."""
    extract = tmp_path / "extract"
    for i, feat in enumerate(("Main", "Video", "Music")):
        _write(extract, f"{feat}/archive/pc/mod/f{i}.archive", feat.encode())
    (extract / "fomod").mkdir(parents=True)
    plugins = "".join(
        f'<plugin name="{feat}"><files>'
        f'<file source="{feat}\\archive\\pc\\mod\\f{i}.archive" '
        f'destination="archive\\pc\\mod\\f{i}.archive"/></files>'
        f'<typeDescriptor><type name="Optional"/></typeDescriptor></plugin>'
        for i, feat in enumerate(("Main", "Video", "Music")))
    (extract / "fomod" / "ModuleConfig.xml").write_text(
        f'<config><installSteps><installStep name="s"><optionalFileGroups>'
        f'<group name="Features" type="SelectAny"><plugins>{plugins}</plugins></group>'
        f'</optionalFileGroups></installStep></installSteps></config>')
    pairs, _ = D.plan_fomod(D.find_fomod_config(extract))
    dests = {dest for _src, dest in pairs}
    assert dests == {
        "archive/pc/mod/f0.archive",
        "archive/pc/mod/f1.archive",
        "archive/pc/mod/f2.archive",
    }, dests


def test_select_atmostone_prefers_recommended_else_none():
    import xml.etree.ElementTree as ET
    grp = ET.fromstring(
        '<group type="SelectAtMostOne"><plugins>'
        '<plugin name="A"><typeDescriptor><type name="Optional"/></typeDescriptor></plugin>'
        '<plugin name="B"><typeDescriptor><type name="Recommended"/></typeDescriptor></plugin>'
        '</plugins></group>')
    chosen = D._select_plugins(grp, {})
    assert [p.get("name") for p in chosen] == ["B"]

    grp2 = ET.fromstring(
        '<group type="SelectAtMostOne"><plugins>'
        '<plugin name="A"><typeDescriptor><type name="Optional"/></typeDescriptor></plugin>'
        '</plugins></group>')
    assert D._select_plugins(grp2, {}) == []  # nothing recommended -> select none


if __name__ == "__main__":
    import inspect, tempfile
    n = 0
    for _name, _fn in sorted(globals().items()):
        if not (_name.startswith("test_") and callable(_fn)):
            continue
        _kw = {"tmp_path": Path(tempfile.mkdtemp())} \
            if "tmp_path" in inspect.signature(_fn).parameters else {}
        _fn(**_kw)
        print("PASS", _name); n += 1
    print(f"{n} fomod tests passed")
