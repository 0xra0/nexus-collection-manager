"""Regression tests for safe mod deletion in nexus_core.deployer.

Guards: exact-name matching (no substring hits), files shared with a kept mod are
left in place, recoverable .mod_trash vs --purge, and that a dry run mutates neither
the manifest nor the disk (the manifest pop/save previously ran even for dry runs).
Pure stdlib — no network, no external tools.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402


def _setup(game: Path, manifest: dict):
    for rels in manifest.values():
        for rel in rels:
            p = game / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_bytes(b"data")
    D.save_manifest(game, manifest)


def _mods(game: Path):
    return json.load(open(game / ".mod_manifest.json"))["mods"]


def test_dry_run_mutates_nothing(tmp_path):
    game = tmp_path
    _setup(game, {"Mod A": ["archive/pc/mod/a.archive"]})
    D.action_remove_names(game, ["Mod A"], dry_run=True, purge=False)
    assert "Mod A" in _mods(game), "dry run must not drop the manifest entry"
    assert (game / "archive/pc/mod/a.archive").exists(), "dry run must not delete files"


def test_safe_delete_moves_to_trash(tmp_path):
    game = tmp_path
    _setup(game, {"Mod A": ["archive/pc/mod/a.archive"]})
    D.action_remove_names(game, ["Mod A"], dry_run=False, purge=False)
    assert not (game / "archive/pc/mod/a.archive").exists()
    assert list((game / ".mod_trash").rglob("a.archive")), "file should be in .mod_trash"
    assert "Mod A" not in _mods(game)


def test_purge_deletes_permanently(tmp_path):
    game = tmp_path
    _setup(game, {"Mod A": ["archive/pc/mod/a.archive"]})
    D.action_remove_names(game, ["Mod A"], dry_run=False, purge=True)
    assert not (game / "archive/pc/mod/a.archive").exists()
    assert not (game / ".mod_trash").exists(), "purge must not create a trash copy"


def test_shared_file_kept_for_other_mod(tmp_path):
    game = tmp_path
    _setup(game, {
        "Mod A": ["archive/pc/mod/a.archive", "archive/pc/mod/shared.archive"],
        "Mod B": ["archive/pc/mod/b.archive", "archive/pc/mod/shared.archive"],
    })
    D.action_remove_names(game, ["Mod A"], dry_run=False, purge=True)
    assert not (game / "archive/pc/mod/a.archive").exists()
    assert (game / "archive/pc/mod/shared.archive").exists(), "shared file wrongly deleted"
    assert (game / "archive/pc/mod/b.archive").exists()


def test_exact_name_does_not_match_substring(tmp_path):
    game = tmp_path
    _setup(game, {
        "Mod B": ["archive/pc/mod/b.archive"],
        "Mod B Patch": ["archive/pc/mod/patch.archive"],
    })
    D.action_remove_names(game, ["Mod B"], dry_run=False, purge=True)
    mods = _mods(game)
    assert "Mod B" not in mods and "Mod B Patch" in mods
    assert (game / "archive/pc/mod/patch.archive").exists()


def test_missing_name_is_skipped_not_guessed(tmp_path):
    game = tmp_path
    _setup(game, {"Real Mod": ["archive/pc/mod/r.archive"]})
    D.action_remove_names(game, ["Nonexistent"], dry_run=False, purge=True)
    assert "Real Mod" in _mods(game), "an unknown name must not remove anything"
    assert (game / "archive/pc/mod/r.archive").exists()


def test_empty_dirs_pruned(tmp_path):
    game = tmp_path
    _setup(game, {"Mod A": ["archive/pc/mod/only.archive"]})
    D.action_remove_names(game, ["Mod A"], dry_run=False, purge=True)
    # archive/pc/mod (and up) held only this mod's file -> pruned
    assert not (game / "archive").exists(), "empty dir chain should be pruned"


if __name__ == "__main__":
    import inspect, tempfile
    n = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        kw = {"tmp_path": Path(tempfile.mkdtemp())} \
            if "tmp_path" in inspect.signature(fn).parameters else {}
        fn(**kw)
        print("PASS", name); n += 1
    print(f"{n} delete tests passed")
