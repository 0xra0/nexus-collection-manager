"""Regression tests for pinned files and runtime-state preservation in nexus_core.deployer.

Deploy rewrote every managed file from its mod source with no notion of a file the user had
deliberately changed. Two live losses, 2026-07-17:

  * red4ext/plugins/audioware/audioware.dll, owned by Audioware-12001 — a locally rebuilt DLL
    (fixing the 100%-CPU busy-spin and the All Foods crash) reverted to stock 1.9.2 on the
    next deploy.
  * red4ext/plugins/mod_settings/user.ini, owned by ONE mod (Perfect City Config-24423) but
    actually a SHARED store: 177 sections holding every ModSettings mod's config plus the
    user's own in-game choices. A deploy overwrote the whole file.

Two mechanisms, because these are different problems: a PIN is explicit intent about one path
(nothing about a hand-built DLL looks different from any other DLL), while PRESERVE is a
policy about a class of file and is write-ONCE, so a mod's defaults still land on a fresh
install. Pure stdlib — no PySide6, no network.
"""
import contextlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexus_core import deployer as D  # noqa: E402

AUDIOWARE = "red4ext/plugins/audioware/audioware.dll"
USER_INI = "red4ext/plugins/mod_settings/user.ini"


def _mk(root: Path, rel: str, content: bytes = b"x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


def _disp(game: Path, rel: str, src_content: bytes, dst_content: bytes | None, pins=None):
    src = _mk(game / "_src", "f", src_content)
    if dst_content is not None:
        _mk(game, rel, dst_content)
    return D.deploy_disposition(rel, src, game / rel, pins or {"pinned": {}, "preserve": []})


# ── pins: the rebuilt audioware.dll ──────────────────────────────────────────

def test_a_pinned_file_is_never_overwritten(tmp_path):
    game = tmp_path / "game"
    _mk(game, AUDIOWARE, b"MY LOCALLY REBUILT DLL")
    _quiet(D.action_pin, game, AUDIOWARE, dry_run=False)
    pins = D.load_pins(game)
    assert _disp(game, AUDIOWARE, b"stock 1.9.2", b"MY LOCALLY REBUILT DLL", pins) == "pinned"


def test_pinning_protects_a_binary_nothing_else_could(tmp_path):
    """A rebuilt DLL is byte-different from stock but indistinguishable in every other way —
    same name, same path, same extension. Explicit intent is the only signal available, which
    is why pins exist separately from the runtime-state policy."""
    game = tmp_path / "game"
    _mk(game, AUDIOWARE, b"rebuilt")
    empty = {"pinned": {}, "preserve": []}
    assert D.deploy_disposition(AUDIOWARE, _mk(game / "_s", "f", b"stock"),
                                game / AUDIOWARE, empty) == "write"   # unprotected: clobbered
    _quiet(D.action_pin, game, AUDIOWARE, dry_run=False)
    assert D.deploy_disposition(AUDIOWARE, _mk(game / "_s", "f", b"stock"),
                                game / AUDIOWARE, D.load_pins(game)) == "pinned"


def test_pin_survives_a_full_deploy_end_to_end(tmp_path):
    game, dl = tmp_path / "game", tmp_path / "dl"
    game.mkdir()
    dl.mkdir()
    with zipfile.ZipFile(dl / "Audioware-12001-1-9-2-1700000000.zip", "w") as z:
        z.writestr(AUDIOWARE, "STOCK 1.9.2")
    repo = str(Path(__file__).resolve().parent.parent)
    base = [sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
            "--downloads", str(dl), "--tmp", str(tmp_path / "tmp"), "--skip-hash"]

    subprocess.run(base, cwd=repo, capture_output=True, text=True)
    assert (game / AUDIOWARE).read_text() == "STOCK 1.9.2"

    (game / AUDIOWARE).write_text("MY REBUILT DLL")
    subprocess.run([*base[:5], "--pin", AUDIOWARE], cwd=repo, capture_output=True, text=True)

    r = subprocess.run(base + ["--force"], cwd=repo, capture_output=True, text=True)
    assert (game / AUDIOWARE).read_text() == "MY REBUILT DLL", \
        "the pinned rebuild was reverted — this is the bug"
    assert "pinned file(s) left as you have them" in r.stdout


def test_unpin_lets_deploy_write_again(tmp_path):
    game = tmp_path / "game"
    _mk(game, AUDIOWARE, b"rebuilt")
    _quiet(D.action_pin, game, AUDIOWARE, dry_run=False)
    _quiet(D.action_unpin, game, "audioware", dry_run=False)
    assert D.load_pins(game)["pinned"] == {}
    assert _disp(game, AUDIOWARE, b"stock", b"rebuilt", D.load_pins(game)) != "pinned"


def test_pin_dry_run_records_nothing(tmp_path):
    game = tmp_path / "game"
    _mk(game, AUDIOWARE, b"rebuilt")
    _quiet(D.action_pin, game, AUDIOWARE, dry_run=True)
    assert D.load_pins(game)["pinned"] == {}


def test_pinning_a_nonexistent_path_is_refused(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    _r, out = _quiet(D.action_pin, game, "red4ext/plugins/nope/nope.dll", dry_run=False)
    assert "Not a file" in out
    assert D.load_pins(game)["pinned"] == {}


def test_health_notices_a_pinned_file_that_changed(tmp_path):
    """The only way to learn that something got past the protection."""
    game = tmp_path / "game"
    _mk(game, AUDIOWARE, b"rebuilt")
    _quiet(D.action_pin, game, AUDIOWARE, dry_run=False)
    n, out = _quiet(D.report_pins, game)
    assert n == 0 and "[pins] 1 pinned file(s); 0 changed or missing" in out

    (game / AUDIOWARE).write_bytes(b"something else")
    n, out = _quiet(D.report_pins, game)
    assert n == 1 and "contents changed" in out

    (game / AUDIOWARE).unlink()
    n, out = _quiet(D.report_pins, game)
    assert n == 1 and "gone from disk" in out


# ── preserve: the shared user.ini ────────────────────────────────────────────

def test_a_shared_runtime_store_is_not_overwritten(tmp_path):
    game = tmp_path / "game"
    assert _disp(game, USER_INI, b"[mod defaults]", b"[177 sections of MY settings]") \
        == "preserved"


def test_preserve_is_write_once_not_never_write(tmp_path):
    """A mod legitimately ships defaults that must land on a FRESH install. Refusing to
    create the file would break the mod; refusing to overwrite it protects the user."""
    game = tmp_path / "game"
    game.mkdir()
    assert _disp(game, USER_INI, b"[defaults]", None) == "write"


def test_redscript_config_storages_are_preserved(tmp_path):
    """RedscriptConfigFramework rewrites its JSON in r6/storages/ when the game exits."""
    game = tmp_path / "game"
    rel = "r6/storages/RedscriptConfigFramework/settings.json"
    assert _disp(game, rel, b"{}", b'{"mine": true}') == "preserved"


def test_an_ordinary_config_is_not_preserved_by_accident(tmp_path):
    """The preserve list is deliberately short. A wrong entry silently stops a mod updating
    its own config — the same class of quiet failure this exists to prevent."""
    game = tmp_path / "game"
    assert _disp(game, "r6/config/somemod.json", b"new", b"old") in ("write", "backup")


def test_a_user_added_preserve_entry_works(tmp_path):
    game = tmp_path / "game"
    pins = {"pinned": {}, "preserve": ["r6/tweaks/mine.yaml", "r6/custom/"]}
    assert _disp(game, "r6/tweaks/mine.yaml", b"new", b"old", pins) == "preserved"
    assert _disp(game, "r6/custom/deep/thing.json", b"new", b"old", pins) == "preserved"
    assert _disp(game, "r6/other/thing.json", b"new", b"old", pins) != "preserved"


def test_user_ini_survives_a_full_deploy_end_to_end(tmp_path):
    game, dl = tmp_path / "game", tmp_path / "dl"
    game.mkdir()
    dl.mkdir()
    with zipfile.ZipFile(dl / "Perfect City Config-24423-1-0-1700000000.zip", "w") as z:
        z.writestr(USER_INI, "[defaults from the mod]")
    repo = str(Path(__file__).resolve().parent.parent)
    base = [sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
            "--downloads", str(dl), "--tmp", str(tmp_path / "tmp"), "--skip-hash"]

    subprocess.run(base, cwd=repo, capture_output=True, text=True)
    assert (game / USER_INI).read_text() == "[defaults from the mod]"   # first install lands

    (game / USER_INI).write_text("[audioware]\nbufferSize=Option1024\n")
    r = subprocess.run(base + ["--force"], cwd=repo, capture_output=True, text=True)
    assert "bufferSize=Option1024" in (game / USER_INI).read_text(), \
        "the shared settings store was overwritten — this is the bug"
    assert "runtime-state file(s) preserved" in r.stdout


# ── backup before overwrite ──────────────────────────────────────────────────

def test_a_changed_config_is_backed_up_before_being_overwritten(tmp_path):
    game = tmp_path / "game"
    assert _disp(game, "r6/config/thing.json", b'{"new": 1}', b'{"my edit": 1}') == "backup"


def test_an_unchanged_file_is_not_backed_up(tmp_path):
    """Rewriting identical bytes is not a loss and needs no copy."""
    game = tmp_path / "game"
    assert _disp(game, "r6/config/thing.json", b"same", b"same") == "write"


def test_binaries_are_not_backed_up(tmp_path):
    """A changed .archive or .dll is a new mod version 99.9% of the time; copying 84 MB
    aside on every update would be pure churn. Binaries you care about get pinned."""
    game = tmp_path / "game"
    assert _disp(game, "archive/pc/mod/big.archive", b"v2", b"v1") == "write"
    assert _disp(game, "red4ext/plugins/x/x.dll", b"v2", b"v1") == "write"


def test_a_huge_text_file_is_not_backed_up(tmp_path):
    game = tmp_path / "game"
    big = b"x" * (D.BACKUP_MAX_BYTES + 1)
    assert _disp(game, "r6/config/huge.json", b"new", big) == "write"


def test_the_backup_actually_lands_on_disk(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    dep = D.Deployer(game, dry_run=False, verbose=False)
    _mk(game, "r6/config/thing.json", b'{"my edit": 1}')
    src = _mk(tmp_path / "src", "thing.json", b'{"from the mod": 1}')
    _quiet(dep._place, src, game / "r6/config/thing.json", "Mod-1-1-0-1700000000")
    assert (game / "r6/config/thing.json").read_bytes() == b'{"from the mod": 1}'
    saved = list((game / D.OVERWRITE_BACKUP_DIRNAME).rglob("thing.json"))
    assert len(saved) == 1 and saved[0].read_bytes() == b'{"my edit": 1}'
    assert dep.backed_up == ["r6/config/thing.json"]


def test_backups_are_a_copy_so_a_failed_backup_cannot_lose_the_file(tmp_path):
    """The deploy must still succeed if the stash fails; a backup that MOVED the file would
    turn a full disk into data loss."""
    src = (Path(__file__).resolve().parent.parent / "nexus_core" / "deployer.py").read_text()
    body = src.split("def _backup_before_overwrite")[1].split("\n    def ")[0]
    assert "shutil.copy2" in body and "shutil.move" not in body


# ── ownership and interaction with the rest ──────────────────────────────────

def test_a_protected_file_is_still_owned_by_its_mod(tmp_path):
    """Ownership must not depend on whether the bytes were written. Dropping it would make
    the file unmanaged — reported as an orphan, and eligible for pruning."""
    game = tmp_path / "game"
    _mk(game, AUDIOWARE, b"rebuilt")
    _quiet(D.action_pin, game, AUDIOWARE, dry_run=False)
    dep = D.Deployer(game, dry_run=False, verbose=False)
    src = _mk(tmp_path / "src", "audioware.dll", b"stock")
    _quiet(dep._place, src, game / AUDIOWARE, "Audioware-12001-1-9-2-1700000000")
    assert dep.ownership() == {"Audioware-12001-1-9-2-1700000000": [AUDIOWARE]}
    assert (game / AUDIOWARE).read_bytes() == b"rebuilt"


def test_the_overwrite_backup_dir_is_skipped_by_the_health_walks(tmp_path):
    """Otherwise every protected file reappears as a case-collision or an unmanaged file."""
    assert D.OVERWRITE_BACKUP_DIRNAME in D.HEALTH_SKIP_DIRS
    game = tmp_path / "game"
    _mk(game, f"{D.OVERWRITE_BACKUP_DIRNAME}/20260101_000000/r6/scripts/x.reds", b"x")
    assert D.find_unmanaged_files(game, {"M-1-1-0-1700000000": ["a"]})["units"] == []


def test_dry_run_writes_nothing_but_still_plans(tmp_path):
    game = tmp_path / "game"
    _mk(game, "r6/config/thing.json", b"mine")
    dep = D.Deployer(game, dry_run=True, verbose=False)
    src = _mk(tmp_path / "src", "thing.json", b"theirs")
    _quiet(dep._place, src, game / "r6/config/thing.json", "Mod-1-1-0-1700000000")
    assert (game / "r6/config/thing.json").read_bytes() == b"mine"
    assert not (game / D.OVERWRITE_BACKUP_DIRNAME).exists()
    assert dep.backed_up == ["r6/config/thing.json"]        # reported, not performed


# ── state file ───────────────────────────────────────────────────────────────

def test_the_pins_file_lives_beside_the_manifest(tmp_path):
    game = tmp_path / "game"
    _mk(game, AUDIOWARE, b"x")
    _quiet(D.action_pin, game, AUDIOWARE, dry_run=False)
    assert json.loads((game / D.PINS_NAME).read_text())["pinned"]


def test_a_corrupt_pins_file_protects_nothing_rather_than_failing(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    (game / D.PINS_NAME).write_text("{ not json")
    assert D.load_pins(game) == {"pinned": {}, "preserve": []}
    (game / D.PINS_NAME).write_text('{"pinned": "not a dict", "preserve": 7}')
    assert D.load_pins(game) == {"pinned": {}, "preserve": []}


def test_gui_regex_is_the_one_shipped_in_deploy_tab():
    src = (Path(__file__).resolve().parent.parent / "gui" / "deploy_tab.py").read_text()
    assert "RE_PINS" in src and "self._pins" in src


def test_check_health_includes_the_pins_line(tmp_path):
    game = tmp_path / "game"
    _mk(game, "archive/pc/mod/m.archive")
    D.save_manifest(game, {"M-1-1-0-1700000000": ["archive/pc/mod/m.archive"]})
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--check-health"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[pins] 0 pinned file(s)" in r.stdout


def test_list_pins_cli(tmp_path):
    game = tmp_path / "game"
    _mk(game, AUDIOWARE, b"rebuilt")
    _quiet(D.action_pin, game, AUDIOWARE, dry_run=False)
    repo = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
                        "--list-pins"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert AUDIOWARE in r.stdout
    assert USER_INI in r.stdout            # the policy list is shown too


if __name__ == "__main__":
    import inspect
    import tempfile
    n = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        kw = {"tmp_path": Path(tempfile.mkdtemp())} \
            if "tmp_path" in inspect.signature(fn).parameters else {}
        fn(**kw)
        print("PASS", name); n += 1
    print(f"{n} pin/preserve tests passed")
