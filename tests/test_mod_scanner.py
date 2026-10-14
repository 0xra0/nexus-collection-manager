"""Regression tests for the standalone TF-IDF/DBSCAN integrity scanner."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from nexus_core import mod_scanner as S


def _write(path: Path, data: bytes | str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def test_resolve_configured_paths_relative_absolute_and_missing(tmp_path):
    game = tmp_path / "game"
    rel = game / "archive" / "pc" / "mod"
    absolute = tmp_path / "external_mods"
    rel.mkdir(parents=True)
    absolute.mkdir()

    roots, missing = S.resolve_scan_paths(
        game, ["archive/pc/mod", str(absolute), "archive/pc/mod", "r6/scripts"])

    assert roots == [rel.resolve(), absolute.resolve()]
    assert missing == [(game / "r6/scripts").resolve()]


def test_tfidf_dbscan_groups_name_similar_documents():
    corpus = [
        ["nightcityestate", "nightcityestate", "nightcityestate"],
        ["nightcityestate", "nightcityestate", "ui"],
        ["vehiclehandling", "vehiclehandling", "vehiclehandling"],
    ]
    vectors = S.compute_tfidf(corpus)
    labels = S.dbscan(vectors, eps=0.65, min_samples=1)

    assert labels[0] == labels[1]
    assert labels[2] != labels[0]


def test_scan_hashes_recursively_groups_scattered_mod_and_tracks_changes(tmp_path):
    game = tmp_path / "game"
    archive = _write(game / "archive/pc/mod/NightCityEstate.archive", b"archive-v1")
    script = _write(game / "r6/scripts/NightCityEstate/init.reds", "class Estate {}")
    other = _write(game / "archive/pc/mod/VehicleHandling.archive", b"vehicle")
    state = tmp_path / "scanner-state.json"

    first = S.scan_mods(
        game,
        ["archive/pc/mod", "r6/scripts"],
        state_path=state,
        jobs=2,
        save_snapshot=True,
    )
    snap = first["snapshot"]

    assert snap["summary"]["files"] == 3
    assert snap["summary"]["added"] == 3
    assert snap["summary"]["groups"] == 2
    assert snap["files"][str(archive.resolve())]["sha256"] == hashlib.sha256(b"archive-v1").hexdigest()
    assert snap["files"][str(script.resolve())]["sha256"] == hashlib.sha256(b"class Estate {}").hexdigest()
    assert (snap["files"][str(archive.resolve())]["group_id"] ==
            snap["files"][str(script.resolve())]["group_id"])
    assert (snap["files"][str(other.resolve())]["group_id"] !=
            snap["files"][str(archive.resolve())]["group_id"])
    assert state.exists()

    # Second scan is an integrity verification against the saved baseline.
    second = S.scan_mods(
        game,
        ["archive/pc/mod", "r6/scripts"],
        state_path=state,
        jobs=1,
        save_snapshot=False,
    )
    assert second["snapshot"]["summary"]["unchanged"] == 3
    assert second["changes"]["changed"] == []

    archive.write_bytes(b"archive-v2")
    script.unlink()
    added = _write(game / "r6/scripts/NewHUD/main.reds", "class NewHUD {}")
    third = S.scan_mods(
        game,
        ["archive/pc/mod", "r6/scripts"],
        state_path=state,
        jobs=1,
        save_snapshot=False,
    )
    assert str(archive.resolve()) in third["changes"]["changed"]
    assert str(script.resolve()) in third["changes"]["removed"]
    assert str(added.resolve()) in third["changes"]["added"]

    # Verify mode must not replace the baseline.
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert persisted["files"][str(archive.resolve())]["sha256"] == hashlib.sha256(b"archive-v1").hexdigest()
    assert str(script.resolve()) in persisted["files"]


def test_overlapping_roots_do_not_double_count_files(tmp_path):
    game = tmp_path / "game"
    f = _write(game / "mods/Foo/info.json", '{"name":"Foo"}')
    records, errors = S.discover_files([game, game / "mods"])
    assert errors == []
    assert len(records) == 1
    assert records[0]["path"].resolve() == f.resolve()
    assert records[0]["root"] == (game / "mods").resolve()


def test_deployer_scan_works_without_bsdtar(tmp_path):
    """The scanner is independent of archive extraction and must not inherit its preflight."""
    repo = Path(__file__).resolve().parents[1]
    game = tmp_path / "game"
    _write(game / "archive/pc/mod/Foo.archive", b"foo")
    state = tmp_path / "state.json"
    env = dict(os.environ)
    env["PATH"] = ""  # shutil.which('bsdtar') is guaranteed to fail in the child
    r = subprocess.run(
        [sys.executable, "-m", "nexus_core.deployer", "--game-root", str(game),
         "--scan", "--scan-state", str(state), "--scan-jobs", "1"],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[scanner] roots=1 files=1 groups=1" in r.stdout
    assert state.exists()


def test_dbscan_marks_isolated_point_as_noise_when_density_requires_two():
    corpus = [["foo", "foo"], ["foo", "foo", "extra"], ["bar", "bar"]]
    labels = S.dbscan(S.compute_tfidf(corpus), eps=0.65, min_samples=2)
    assert labels[0] == labels[1] >= 0
    assert labels[2] == -1
