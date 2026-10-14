#!/usr/bin/env python3
"""Linux-native clean-game staging and hardlink deployment.

The game tree is treated as a *projection* of managed content rather than the
canonical copy.  Installed mod bytes live under ``<game>/downloaded_mods`` and
only hardlinks are materialised into the game paths required by Cyberpunk 2077.

Design goals:

* installs survive an undeploy without re-extracting archives;
* enable/disable and profile switches are DB changes + a cheap link rebuild;
* deployment never deletes a file it cannot prove is one of our hardlinks;
* a game patch or third-party tool replacing/modifying a link is detectable;
* no Windows junctions and no proprietary ``redMod.exe`` are required.

REDmod note
-----------
Official REDmod deployment compiles REDmod scripts/tweaks.  This Linux-only
backend deliberately does not invoke that Windows executable.  Archive-only
REDmods are converted to the legacy archive path at deployment time.  REDmod
payloads that require the proprietary compilation step are reported as
``native-compile-required`` and are left staged but not projected as if they had
been compiled successfully.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import heapq
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Sequence
from urllib.parse import urlparse

STAGING_DIRNAME = "downloaded_mods"
DB_NAME = "deployment.sqlite3"
PAYLOAD_DIRNAME = "mods"
LEGACY_STATE_DIRNAME = "legacy_state"
DEFAULT_PROFILE = "Default"
SCHEMA_VERSION = 4
JSON_PREFIX = "[links-json] "

# Paths that are generated/owned by mod tooling rather than Steam vanilla data.
# Used only for migration diagnostics; actual deployment is driven by the DB.
MOD_ROOTS = (
    "archive/pc/mod",
    "bin/x64/plugins/cyber_engine_tweaks/mods",
    "bin/x64/plugins",
    "mods",
    "red4ext/plugins",
    "r6/scripts",
    "r6/tweaks",
    "soundbanks",
)

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class SmartDeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class DesiredFile:
    rel_path: str
    mod_id: int
    mod_name: str
    stage_rel: str
    sha256: str
    size: int
    projected_rel: str
    redmod_mode: str = "normal"




@contextmanager
def deployment_lock(game_root: Path, *, exclusive: bool = True):
    """Serialize DB + hardlink projection changes without writing lock metadata in the game tree."""
    root = staging_root(game_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".deployment.lock"
    fh = path.open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

def _json_line(payload: dict) -> None:
    print(JSON_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def staging_root(game_root: Path) -> Path:
    return game_root.expanduser().resolve() / STAGING_DIRNAME


def db_path(game_root: Path) -> Path:
    return staging_root(game_root) / DB_NAME


def _safe_rel(value: str | Path) -> str:
    raw = str(value).replace("\\", "/").strip()
    if not raw or raw.startswith("/") or _WINDOWS_DRIVE_RE.match(raw):
        raise ValueError(f"unsafe managed path: {value}")
    p = PurePosixPath(raw)
    if any(part in {"", ".", ".."} for part in p.parts):
        raise ValueError(f"unsafe managed path: {value}")
    rel = p.as_posix()
    if rel == STAGING_DIRNAME or rel.startswith(STAGING_DIRNAME + "/"):
        raise ValueError("a mod cannot deploy into the manager staging directory")
    return rel


def _inside(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as e:
        raise ValueError(f"path escapes managed root: {candidate}") from e
    return candidate


def _sha256(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _same_inode(a: Path, b: Path) -> bool:
    try:
        sa = a.stat()
        sb = b.stat()
    except OSError:
        return False
    return sa.st_dev == sb.st_dev and sa.st_ino == sb.st_ino


def _slug(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-") or "mod"
    digest = hashlib.sha1(name.encode("utf-8", "replace")).hexdigest()[:10]
    return f"{clean[:80]}--{digest}"


def _atomic_copy(src: Path, dest: Path) -> tuple[str, int]:
    """Copy to a fresh inode then atomically publish it.

    Never overwrite an already-deployed staging inode in place: game hardlinks
    share that inode and would otherwise see partially-written new bytes.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".incoming-", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with src.open("rb") as r, tmp.open("wb") as w:
            shutil.copyfileobj(r, w, 1024 * 1024)
        try:
            shutil.copystat(src, tmp, follow_symlinks=False)
        except OSError:
            pass
        digest = _sha256(tmp)
        size = tmp.stat().st_size
        os.replace(tmp, dest)
        return digest, size
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _remove_empty_parents(path: Path, stop: Path) -> None:
    stop = stop.resolve()
    cur = path.parent
    while cur != stop:
        try:
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent


def _redmod_parts(rel: str) -> tuple[str, str] | None:
    parts = PurePosixPath(rel).parts
    if len(parts) < 3 or parts[0].lower() != "mods":
        return None
    return parts[1], "/".join(parts[2:])


def _project_redmod(rel: str) -> tuple[str, str]:
    """Return (projected path, mode) for a staged destination.

    Archive-only REDmods can be loaded without REDmod's compiler by projecting
    the archive into the legacy archive directory.  Already-legacy-compatible
    ``.reds`` and TweakXL yaml/json payloads are also mapped to the open Linux
    runtime framework locations.  REDmod ``.script`` and customSounds need the
    official deploy/compiler semantics and are intentionally not faked.
    """
    info = _redmod_parts(rel)
    if not info:
        return rel, "normal"
    mod_name, inside = info
    low = inside.lower()
    source_name = PurePosixPath(inside).name
    safe_mod = re.sub(r"[^A-Za-z0-9._-]+", "_", mod_name).strip("._-") or "redmod"
    if low in {"info.json", "mod.json"}:
        return rel, "metadata-only"
    if low.startswith("archives/") and source_name.lower().endswith(".archive"):
        return f"archive/pc/mod/{safe_mod}__{source_name}", "legacy-archive"
    if low.startswith("scripts/") and source_name.lower().endswith(".reds"):
        rest = inside[len("scripts/"):]
        return f"r6/scripts/{safe_mod}/{rest}", "native-redscript"
    if low.startswith("tweaks/") and source_name.lower().endswith((".yaml", ".yml", ".json")):
        rest = inside[len("tweaks/"):]
        return f"r6/tweaks/{safe_mod}/{rest}", "native-tweakxl"
    return rel, "native-compile-required"


class Store:
    def __init__(self, game_root: Path, *, create: bool = True):
        self.game_root = game_root.expanduser().resolve()
        self.root = staging_root(self.game_root)
        self.path = self.root / DB_NAME
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mods(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    slug TEXT NOT NULL UNIQUE,
                    source_archive TEXT,
                    installed_at REAL NOT NULL,
                    enabled_default INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS files(
                    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
                    rel_path TEXT NOT NULL,
                    stage_rel TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    projected_rel TEXT NOT NULL,
                    redmod_mode TEXT NOT NULL DEFAULT 'normal',
                    PRIMARY KEY(mod_id, rel_path)
                );
                CREATE INDEX IF NOT EXISTS files_rel_idx ON files(rel_path);
                CREATE INDEX IF NOT EXISTS files_projected_idx ON files(projected_rel);
                CREATE TABLE IF NOT EXISTS profiles(
                    name TEXT PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_profile
                    ON profiles(active) WHERE active=1;
                CREATE TABLE IF NOT EXISTS profile_mods(
                    profile TEXT NOT NULL REFERENCES profiles(name) ON DELETE CASCADE,
                    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
                    enabled INTEGER NOT NULL,
                    PRIMARY KEY(profile, mod_id)
                );
                CREATE TABLE IF NOT EXISTS conflict_rules(
                    profile TEXT NOT NULL REFERENCES profiles(name) ON DELETE CASCADE,
                    rel_path TEXT NOT NULL,
                    preferred_mod_id INTEGER REFERENCES mods(id) ON DELETE CASCADE,
                    PRIMARY KEY(profile, rel_path)
                );
                CREATE TABLE IF NOT EXISTS load_order(
                    profile TEXT NOT NULL REFERENCES profiles(name) ON DELETE CASCADE,
                    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
                    priority INTEGER NOT NULL,
                    PRIMARY KEY(profile, mod_id)
                );
                CREATE INDEX IF NOT EXISTS load_order_profile_idx ON load_order(profile, priority);
                CREATE TABLE IF NOT EXISTS archive_order(
                    profile TEXT NOT NULL REFERENCES profiles(name) ON DELETE CASCADE,
                    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
                    rel_path TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    PRIMARY KEY(profile, mod_id, rel_path)
                );
                CREATE INDEX IF NOT EXISTS archive_order_profile_idx
                    ON archive_order(profile, priority);
                CREATE TABLE IF NOT EXISTS load_order_rules(
                    profile TEXT NOT NULL REFERENCES profiles(name) ON DELETE CASCADE,
                    before_mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
                    after_mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
                    note TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    PRIMARY KEY(profile, before_mod_id, after_mod_id),
                    CHECK(before_mod_id <> after_mod_id)
                );
                CREATE INDEX IF NOT EXISTS load_order_rules_profile_idx
                    ON load_order_rules(profile, before_mod_id, after_mod_id);
                CREATE TABLE IF NOT EXISTS generated_deployed(
                    rel_path TEXT PRIMARY KEY,
                    stage_rel TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    dev INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    deployed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conflict_inbox(
                    id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL REFERENCES profiles(name) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    resolved_at REAL
                );
                CREATE INDEX IF NOT EXISTS conflict_inbox_profile_idx ON conflict_inbox(profile, active, status, severity);
                CREATE TABLE IF NOT EXISTS archive_library(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_name TEXT NOT NULL,
                    stage_rel TEXT NOT NULL UNIQUE,
                    original_path TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL,
                    md5 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    imported_at REAL NOT NULL,
                    nexus_mod_id INTEGER,
                    nexus_file_id INTEGER,
                    nexus_game_domain TEXT NOT NULL DEFAULT 'cyberpunk2077',
                    link_source TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS archive_library_sha_idx ON archive_library(sha256);
                CREATE INDEX IF NOT EXISTS archive_library_nexus_idx ON archive_library(nexus_mod_id);
                CREATE TABLE IF NOT EXISTS archive_installs(
                    archive_id INTEGER NOT NULL REFERENCES archive_library(id) ON DELETE CASCADE,
                    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
                    linked_at REAL NOT NULL,
                    PRIMARY KEY(archive_id, mod_id)
                );
                CREATE INDEX IF NOT EXISTS archive_installs_mod_idx ON archive_installs(mod_id);
                CREATE TABLE IF NOT EXISTS deployed(
                    rel_path TEXT PRIMARY KEY,
                    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
                    stage_rel TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    dev INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    deployed_at REAL NOT NULL
                );
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO profiles(name, active, created_at) VALUES(?,?,?)",
                (DEFAULT_PROFILE, 1, time.time()),
            )
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @property
    def active_profile(self) -> str:
        row = self.conn.execute("SELECT name FROM profiles WHERE active=1").fetchone()
        if row:
            return str(row[0])
        with self.conn:
            self.conn.execute("UPDATE profiles SET active=0")
            self.conn.execute("INSERT OR IGNORE INTO profiles(name,active,created_at) VALUES(?,?,?)",
                              (DEFAULT_PROFILE, 1, time.time()))
            self.conn.execute("UPDATE profiles SET active=1 WHERE name=?", (DEFAULT_PROFILE,))
        return DEFAULT_PROFILE

    def profiles(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT p.name,p.active,p.created_at,COUNT(pm.mod_id) total,"
            "COALESCE(SUM(pm.enabled),0) enabled FROM profiles p "
            "LEFT JOIN profile_mods pm ON pm.profile=p.name "
            "GROUP BY p.name ORDER BY p.active DESC,p.name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def create_profile(self, name: str, from_profile: str | None = None) -> dict:
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("profile name must be 1-120 characters")
        base = from_profile or self.active_profile
        if not self.conn.execute("SELECT 1 FROM profiles WHERE name=?", (base,)).fetchone():
            raise KeyError(f"profile not found: {base}")
        with self.conn:
            self.conn.execute("INSERT INTO profiles(name,active,created_at) VALUES(?,?,?)",
                              (name, 0, time.time()))
            self.conn.execute(
                "INSERT INTO profile_mods(profile,mod_id,enabled) "
                "SELECT ?,mod_id,enabled FROM profile_mods WHERE profile=?",
                (name, base),
            )
            self.conn.execute(
                "INSERT INTO conflict_rules(profile,rel_path,preferred_mod_id) "
                "SELECT ?,rel_path,preferred_mod_id FROM conflict_rules WHERE profile=?",
                (name, base),
            )
            self.conn.execute(
                "INSERT INTO load_order(profile,mod_id,priority) "
                "SELECT ?,mod_id,priority FROM load_order WHERE profile=?",
                (name, base),
            )
            self.conn.execute(
                "INSERT INTO archive_order(profile,mod_id,rel_path,priority) "
                "SELECT ?,mod_id,rel_path,priority FROM archive_order WHERE profile=?",
                (name, base),
            )
            self.conn.execute(
                "INSERT INTO load_order_rules(profile,before_mod_id,after_mod_id,note,created_at) "
                "SELECT ?,before_mod_id,after_mod_id,note,created_at FROM load_order_rules WHERE profile=?",
                (name, base),
            )
        return {"name": name, "copied_from": base}

    def switch_profile_flag(self, name: str) -> None:
        if not self.conn.execute("SELECT 1 FROM profiles WHERE name=?", (name,)).fetchone():
            raise KeyError(f"profile not found: {name}")
        with self.conn:
            self.conn.execute("UPDATE profiles SET active=0 WHERE active=1")
            self.conn.execute("UPDATE profiles SET active=1 WHERE name=?", (name,))

    def _mod_row(self, name: str):
        return self.conn.execute("SELECT * FROM mods WHERE name=?", (name,)).fetchone()

    def _ensure_mod(self, name: str, source_archive: str | None = None) -> sqlite3.Row:
        row = self._mod_row(name)
        if row:
            with self.conn:
                self.conn.execute(
                    "UPDATE mods SET source_archive=COALESCE(?,source_archive),installed_at=? WHERE id=?",
                    (source_archive, time.time(), row["id"]),
                )
            return self._mod_row(name)
        slug = _slug(name)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO mods(name,slug,source_archive,installed_at,enabled_default) VALUES(?,?,?,?,1)",
                (name, slug, source_archive, time.time()),
            )
            mod_id = int(cur.lastrowid)
            active = self.active_profile
            profiles = [r[0] for r in self.conn.execute("SELECT name FROM profiles")]
            for profile in profiles:
                self.conn.execute(
                    "INSERT OR REPLACE INTO profile_mods(profile,mod_id,enabled) VALUES(?,?,?)",
                    (profile, mod_id, 1 if profile == active else 0),
                )
        return self.conn.execute("SELECT * FROM mods WHERE id=?", (mod_id,)).fetchone()

    def owners(self, rel_path: str, *, projected: bool = False) -> list[str]:
        col = "f.projected_rel" if projected else "f.rel_path"
        rows = self.conn.execute(
            f"SELECT m.name FROM files f JOIN mods m ON m.id=f.mod_id WHERE {col}=? ORDER BY m.id",
            (_safe_rel(rel_path),),
        ).fetchall()
        return [str(r[0]) for r in rows]

    def installed_mods(self) -> list[dict]:
        profile = self.active_profile
        rows = self.conn.execute(
            "SELECT m.id,m.name,m.installed_at,COALESCE(pm.enabled,m.enabled_default) enabled,"
            "COUNT(f.rel_path) files,SUM(CASE WHEN f.redmod_mode='native-compile-required' THEN 1 ELSE 0 END) redmod_blocked "
            "FROM mods m LEFT JOIN profile_mods pm ON pm.mod_id=m.id AND pm.profile=? "
            "LEFT JOIN files f ON f.mod_id=m.id GROUP BY m.id ORDER BY m.name COLLATE NOCASE",
            (profile,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_enabled(self, name: str, enabled: bool, profile: str | None = None) -> None:
        row = self._mod_row(name)
        if not row:
            raise KeyError(f"managed mod not found: {name}")
        profile = profile or self.active_profile
        if not self.conn.execute("SELECT 1 FROM profiles WHERE name=?", (profile,)).fetchone():
            raise KeyError(f"profile not found: {profile}")
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO profile_mods(profile,mod_id,enabled) VALUES(?,?,?)",
                (profile, row["id"], 1 if enabled else 0),
            )

    def stage_mod(
        self,
        name: str,
        pairs: Sequence[tuple[Path, str]],
        *,
        resolutions: dict[str, str] | None = None,
        source_archive: str | None = None,
    ) -> dict:
        """Store a full revision in staging and update conflict preferences.

        ``resolutions`` uses destination paths from the install preview.  "skip"
        means retain the currently preferred managed owner (or intentionally
        block projection when the conflict is unmanaged); "overwrite" means the
        newly-staged mod is preferred for that path.
        """
        resolutions = { _safe_rel(k): v for k, v in (resolutions or {}).items()
                        if v in {"skip", "overwrite"} }
        row = self._ensure_mod(name, source_archive)
        mod_id = int(row["id"])
        slug = str(row["slug"])
        payload_root = self.root / PAYLOAD_DIRNAME / slug / "payload"
        existing_self = {
            str(r["rel_path"]): r for r in self.conn.execute(
                "SELECT * FROM files WHERE mod_id=?", (mod_id,)
            ).fetchall()
        }
        prepared: list[tuple[str, str, str, int, str, str]] = []
        seen: set[str] = set()
        source_skipped: list[str] = []
        for src, raw_rel in pairs:
            rel = _safe_rel(raw_rel)
            if rel.casefold() == "archive/pc/mod/modlist.txt":
                source_skipped.append(rel)
                continue
            if rel in seen:
                # The install planner already resolves duplicates; keep last one
                # here too in case the library API is used directly.
                prepared = [x for x in prepared if x[0] != rel]
            seen.add(rel)
            old_self = existing_self.get(rel)
            if resolutions.get(rel) == "skip" and old_self is not None:
                # A reinstall-level Skip means keep the old canonical staged
                # bytes too, not merely the old game pathname. Otherwise replacing
                # the staging inode would make the next rebuild silently install
                # bytes the user explicitly skipped.
                prepared.append((
                    rel, str(old_self["stage_rel"]), str(old_self["sha256"]),
                    int(old_self["size"]), str(old_self["projected_rel"]),
                    str(old_self["redmod_mode"]),
                ))
                source_skipped.append(rel)
                continue
            src = Path(src).resolve()
            if not src.is_file():
                continue
            stage_path = _inside(self.root, payload_root / Path(rel))
            digest, size = _atomic_copy(src, stage_path)
            stage_rel = stage_path.relative_to(self.root).as_posix()
            projected, redmode = _project_redmod(rel)
            prepared.append((rel, stage_rel, digest, size, projected, redmode))

        if not prepared:
            raise SmartDeploymentError("mod contains no files that can be staged")

        profile = self.active_profile
        # Snapshot existing candidates before replacing this mod's file rows.
        previous_preferred: dict[str, int | None] = {}
        for rel, _stage, _sha, _size, projected, _mode in prepared:
            r = self.conn.execute(
                "SELECT preferred_mod_id FROM conflict_rules WHERE profile=? AND rel_path=?",
                (profile, projected),
            ).fetchone()
            if r is not None:
                previous_preferred[projected] = r[0]
            else:
                c = self.conn.execute(
                    "SELECT f.mod_id FROM files f JOIN profile_mods pm ON pm.mod_id=f.mod_id "
                    "AND pm.profile=? AND pm.enabled=1 WHERE f.projected_rel=? AND f.mod_id<>? "
                    "ORDER BY f.mod_id DESC LIMIT 1",
                    (profile, projected, mod_id),
                ).fetchone()
                previous_preferred[projected] = int(c[0]) if c else None

        with self.conn:
            self.conn.execute("DELETE FROM files WHERE mod_id=?", (mod_id,))
            for rel, stage_rel, digest, size, projected, redmode in prepared:
                self.conn.execute(
                    "INSERT INTO files(mod_id,rel_path,stage_rel,sha256,size,projected_rel,redmod_mode) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (mod_id, rel, stage_rel, digest, size, projected, redmode),
                )
                action = resolutions.get(rel)
                # Preview resolutions are keyed by authored destination; rules are
                # keyed by the final projected game path.
                if action == "overwrite":
                    self.conn.execute(
                        "INSERT OR REPLACE INTO conflict_rules(profile,rel_path,preferred_mod_id) VALUES(?,?,?)",
                        (profile, projected, mod_id),
                    )
                elif action == "skip":
                    self.conn.execute(
                        "INSERT OR REPLACE INTO conflict_rules(profile,rel_path,preferred_mod_id) VALUES(?,?,?)",
                        (profile, projected, previous_preferred.get(projected)),
                    )
                else:
                    # New paths/reinstalls default to the current mod; genuine
                    # collisions should have received an explicit preview action.
                    competing = self.conn.execute(
                        "SELECT 1 FROM files WHERE projected_rel=? AND mod_id<>? LIMIT 1",
                        (projected, mod_id),
                    ).fetchone()
                    if competing is None:
                        self.conn.execute(
                            "INSERT OR REPLACE INTO conflict_rules(profile,rel_path,preferred_mod_id) VALUES(?,?,?)",
                            (profile, projected, mod_id),
                        )
            self.conn.execute(
                "INSERT OR REPLACE INTO profile_mods(profile,mod_id,enabled) VALUES(?,?,1)",
                (profile, mod_id),
            )

        # Delete obsolete staged bytes for this mod.  Unlinked game hardlinks are
        # safe: they keep their inode until rebuild removes them.
        referenced = {x[1] for x in prepared}
        if payload_root.exists():
            for p in sorted(payload_root.rglob("*"), reverse=True):
                if p.is_file():
                    rel = p.relative_to(self.root).as_posix()
                    if rel not in referenced:
                        try:
                            p.unlink()
                        except OSError:
                            pass
                elif p.is_dir():
                    try:
                        p.rmdir()
                    except OSError:
                        pass

        blocked = sum(1 for x in prepared if x[5] == "native-compile-required")
        return {
            "mod_id": mod_id,
            "name": name,
            "staged_files": len(prepared),
            "source_skipped": source_skipped,
            "redmod_native_compile_required": blocked,
            "profile": profile,
        }

    def set_load_order(self, ordered_names: Sequence[str], profile: str | None = None) -> dict:
        """Persist an explicit first-wins order for managed .archive files.

        Cyberpunk legacy archives are resolved in ASCII filename order and the first
        archive containing a resource wins.  We therefore project ordered archives
        through deterministic manager-owned aliases.  Staged/canonical filenames are
        never renamed.
        """
        profile = profile or self.active_profile
        if not self.conn.execute("SELECT 1 FROM profiles WHERE name=?", (profile,)).fetchone():
            raise KeyError(f"profile not found: {profile}")
        ids: list[tuple[int, str]] = []
        seen: set[int] = set()
        for raw in ordered_names:
            row = self._mod_row(str(raw))
            if not row:
                raise KeyError(f"managed mod not found: {raw}")
            mid = int(row["id"])
            if mid in seen:
                continue
            seen.add(mid); ids.append((mid, str(row["name"])))
        with self.conn:
            self.conn.execute("DELETE FROM archive_order WHERE profile=?", (profile,))
            self.conn.execute("DELETE FROM load_order WHERE profile=?", (profile,))
            for rank, (mid, _name) in enumerate(ids):
                self.conn.execute(
                    "INSERT OR REPLACE INTO load_order(profile,mod_id,priority) VALUES(?,?,?)",
                    (profile, mid, rank),
                )
        return {"profile": profile, "order": [name for _mid, name in ids]}

    def clear_load_order(self, names: Sequence[str] | None = None, profile: str | None = None) -> dict:
        profile = profile or self.active_profile
        with self.conn:
            if names:
                for name in names:
                    row = self._mod_row(str(name))
                    if row:
                        self.conn.execute("DELETE FROM load_order WHERE profile=? AND mod_id=?", (profile, row["id"]))
            else:
                self.conn.execute("DELETE FROM load_order WHERE profile=?", (profile,))
                self.conn.execute("DELETE FROM archive_order WHERE profile=?", (profile,))
        return {"profile": profile, "cleared": list(names or [])}

    def load_order_rows(self, profile: str | None = None) -> list[dict]:
        profile = profile or self.active_profile
        rows = self.conn.execute(
            "SELECT m.name,lo.mod_id,lo.priority FROM load_order lo "
            "JOIN mods m ON m.id=lo.mod_id WHERE lo.profile=? ORDER BY lo.priority,m.name COLLATE NOCASE",
            (profile,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def _set_setting(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def modlist_managed(self) -> bool:
        return self._setting("manage_modlist", "0").strip().lower() in {"1", "true", "yes", "on"}

    def set_modlist_managed(self, enabled: bool) -> None:
        self._set_setting("manage_modlist", "1" if enabled else "0")

    @staticmethod
    def _is_archive_projection(rel: str) -> bool:
        p = PurePosixPath(rel)
        return (
            len(p.parts) >= 4
            and tuple(x.lower() for x in p.parts[:3]) == ("archive", "pc", "mod")
            and p.suffix.lower() == ".archive"
        )

    def load_order_rules(self, profile: str | None = None) -> list[dict]:
        profile = profile or self.active_profile
        rows = self.conn.execute(
            "SELECT bm.name before_mod,am.name after_mod,r.note,r.created_at "
            "FROM load_order_rules r JOIN mods bm ON bm.id=r.before_mod_id "
            "JOIN mods am ON am.id=r.after_mod_id WHERE r.profile=? "
            "ORDER BY bm.name COLLATE NOCASE,am.name COLLATE NOCASE",
            (profile,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _archive_items(self, profile: str | None = None) -> list[dict]:
        profile = profile or self.active_profile
        desired, _blocked = self.desired(profile)
        out: list[dict] = []
        for deployed_rel, item in desired.items():
            if not self._is_archive_projection(deployed_rel):
                continue
            original = PurePosixPath(item.rel_path).name
            deployed = PurePosixPath(deployed_rel).name
            stored = self.conn.execute(
                "SELECT projected_rel FROM files WHERE mod_id=? AND rel_path=?",
                (item.mod_id, item.rel_path),
            ).fetchone()
            natural_deployed = PurePosixPath(str(stored[0]) if stored else deployed_rel).name
            row = self.conn.execute(
                "SELECT priority FROM archive_order WHERE profile=? AND mod_id=? AND rel_path=?",
                (profile, item.mod_id, item.rel_path),
            ).fetchone()
            mod_priority = self.conn.execute(
                "SELECT priority FROM load_order WHERE profile=? AND mod_id=?",
                (profile, item.mod_id),
            ).fetchone()
            out.append({
                "key": f"{item.mod_id}:{item.rel_path}",
                "mod_id": item.mod_id,
                "mod": item.mod_name,
                "rel_path": item.rel_path,
                "projected_rel": item.projected_rel,
                "archive": original,
                "deployed_archive": deployed,
                "natural_deployed_archive": natural_deployed,
                "archive_priority": int(row[0]) if row is not None else None,
                "mod_priority": int(mod_priority[0]) if mod_priority is not None else None,
                "redmod_mode": item.redmod_mode,
            })
        return out

    @staticmethod
    def _toposort_mods(items: list[dict], rules: list[dict], priorities: dict[int, int]) -> tuple[list[int], list[str]]:
        nodes = {int(x["mod_id"]) for x in items}
        names = {int(x["mod_id"]): str(x["mod"]) for x in items}
        min_archive: dict[int, str] = {}
        for x in items:
            mid = int(x["mod_id"])
            name = str(x["deployed_archive"])
            if mid not in min_archive or name.encode("utf-8") < min_archive[mid].encode("utf-8"):
                min_archive[mid] = name
        edges: dict[int, set[int]] = {n: set() for n in nodes}
        indegree = {n: 0 for n in nodes}
        for rule in rules:
            before = int(rule["before_mod_id"]); after = int(rule["after_mod_id"])
            if before not in nodes or after not in nodes or before == after or after in edges[before]:
                continue
            edges[before].add(after); indegree[after] += 1

        def key(mid: int):
            return (
                priorities.get(mid, 10**9),
                min_archive.get(mid, "").encode("utf-8"),
                names.get(mid, "").casefold(),
                mid,
            )

        heap = [(key(mid), mid) for mid, degree in indegree.items() if degree == 0]
        heapq.heapify(heap)
        ordered: list[int] = []
        while heap:
            _k, mid = heapq.heappop(heap)
            ordered.append(mid)
            for nxt in sorted(edges[mid]):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    heapq.heappush(heap, (key(nxt), nxt))
        warnings: list[str] = []
        if len(ordered) != len(nodes):
            cycle = sorted((nodes - set(ordered)), key=key)
            warnings.append("load-order preference cycle detected; cyclic mods fell back to deterministic order")
            ordered.extend(cycle)
        return ordered, warnings

    def archive_load_order(self, profile: str | None = None) -> dict:
        """Return the effective archive order. First row has highest priority/wins."""
        profile = profile or self.active_profile
        items = self._archive_items(profile)
        if not items:
            return {"profile": profile, "items": [], "rules": self.load_order_rules(profile), "warnings": []}

        explicit = {str(r["key"]): int(r["archive_priority"]) for r in items if r["archive_priority"] is not None}
        db_rules = self.conn.execute(
            "SELECT before_mod_id,after_mod_id,note FROM load_order_rules WHERE profile=?",
            (profile,),
        ).fetchall()
        mod_priorities = {
            int(r["mod_id"]): int(r["priority"])
            for r in self.conn.execute("SELECT mod_id,priority FROM load_order WHERE profile=?", (profile,)).fetchall()
        }
        warnings: list[str] = []
        if explicit:
            ordered = sorted(
                items,
                key=lambda x: (
                    explicit.get(str(x["key"]), 10**9),
                    str(x["deployed_archive"]).encode("utf-8"),
                    int(x["mod_id"]),
                ),
            )
        elif db_rules or mod_priorities:
            mids, warnings = self._toposort_mods(items, [dict(r) for r in db_rules], mod_priorities)
            rank = {mid: i for i, mid in enumerate(mids)}
            ordered = sorted(
                items,
                key=lambda x: (
                    rank.get(int(x["mod_id"]), 10**9),
                    str(x["deployed_archive"]).encode("utf-8"),
                    str(x["rel_path"]),
                ),
            )
        else:
            ordered = sorted(items, key=lambda x: (str(x["deployed_archive"]).encode("utf-8"), int(x["mod_id"])))

        for pos, row in enumerate(ordered, 1):
            row["position"] = pos
            row["winner_priority"] = "highest" if pos == 1 else str(pos)
        return {"profile": profile, "items": ordered, "rules": self.load_order_rules(profile), "warnings": warnings}

    def set_archive_order(self, archive_keys: Sequence[str], profile: str | None = None) -> dict:
        profile = profile or self.active_profile
        known = {str(x["key"]): x for x in self._archive_items(profile)}
        supplied: list[str] = []
        seen: set[str] = set()
        for key in archive_keys:
            key = str(key)
            if key in seen:
                continue
            if key not in known:
                raise KeyError(f"archive not found in active profile: {key}")
            seen.add(key); supplied.append(key)
        # A partial order is useful from API consumers: append the rest in their
        # current effective order rather than silently dropping them.
        for row in self.archive_load_order(profile)["items"]:
            key = str(row["key"])
            if key not in seen:
                seen.add(key); supplied.append(key)
        with self.conn:
            self.conn.execute("DELETE FROM archive_order WHERE profile=?", (profile,))
            # A complete archive list supersedes the older mod-level alias order.
            # modlist.txt now carries the authoritative first-wins sequence.
            self.conn.execute("DELETE FROM load_order WHERE profile=?", (profile,))
            for priority, key in enumerate(supplied):
                row = known[key]
                self.conn.execute(
                    "INSERT INTO archive_order(profile,mod_id,rel_path,priority) VALUES(?,?,?,?)",
                    (profile, int(row["mod_id"]), str(row["rel_path"]), priority),
                )
        return self.archive_load_order(profile)

    def clear_archive_order(self, profile: str | None = None, *, clear_rules: bool = False) -> dict:
        profile = profile or self.active_profile
        with self.conn:
            self.conn.execute("DELETE FROM archive_order WHERE profile=?", (profile,))
            self.conn.execute("DELETE FROM load_order WHERE profile=?", (profile,))
            if clear_rules:
                self.conn.execute("DELETE FROM load_order_rules WHERE profile=?", (profile,))
        return self.archive_load_order(profile)

    def set_load_rule(self, before_mod: str, after_mod: str, *, note: str = "", profile: str | None = None) -> dict:
        profile = profile or self.active_profile
        before = self._mod_row(before_mod); after = self._mod_row(after_mod)
        if not before:
            raise KeyError(f"managed mod not found: {before_mod}")
        if not after:
            raise KeyError(f"managed mod not found: {after_mod}")
        if int(before["id"]) == int(after["id"]):
            raise ValueError("a mod cannot be ordered before itself")
        # Validate the prospective graph before destroying a complete manual order.
        prospective = [dict(r) for r in self.conn.execute(
            "SELECT before_mod_id,after_mod_id,note FROM load_order_rules WHERE profile=?",
            (profile,),
        ).fetchall()]
        edge = {"before_mod_id": int(before["id"]), "after_mod_id": int(after["id"]), "note": note.strip()}
        prospective = [r for r in prospective if not (
            int(r["before_mod_id"]) == edge["before_mod_id"] and int(r["after_mod_id"]) == edge["after_mod_id"]
        )]
        prospective.append(edge)
        items = self._archive_items(profile)
        priorities = {
            int(r["mod_id"]): int(r["priority"])
            for r in self.conn.execute("SELECT mod_id,priority FROM load_order WHERE profile=?", (profile,)).fetchall()
        }
        _order, warnings = self._toposort_mods(items, prospective, priorities)
        if warnings:
            raise ValueError("load-order rule would create a cycle")
        # Pairwise rules and a complete manually-sorted archive list are two
        # alternative preference modes. A valid new rule intentionally returns
        # to rule mode and removes the older mod-level alias priorities.
        with self.conn:
            self.conn.execute("DELETE FROM archive_order WHERE profile=?", (profile,))
            self.conn.execute("DELETE FROM load_order WHERE profile=?", (profile,))
            self.conn.execute(
                "INSERT OR REPLACE INTO load_order_rules(profile,before_mod_id,after_mod_id,note,created_at) "
                "VALUES(?,?,?,?,?)",
                (profile, int(before["id"]), int(after["id"]), note.strip(), time.time()),
            )
        return self.archive_load_order(profile)

    def remove_load_rule(self, before_mod: str, after_mod: str, profile: str | None = None) -> dict:
        profile = profile or self.active_profile
        before = self._mod_row(before_mod); after = self._mod_row(after_mod)
        if before and after:
            with self.conn:
                self.conn.execute(
                    "DELETE FROM load_order_rules WHERE profile=? AND before_mod_id=? AND after_mod_id=?",
                    (profile, int(before["id"]), int(after["id"])),
                )
        return self.archive_load_order(profile)

    def modlist_content(self, profile: str | None = None) -> str:
        rows = self.archive_load_order(profile)["items"]
        if not rows:
            return ""
        return "".join(str(r["deployed_archive"]) + "\n" for r in rows)

    def modlist_preview(self, profile: str | None = None) -> dict:
        profile = profile or self.active_profile
        plan = self.archive_load_order(profile)
        desired = [str(x["deployed_archive"]) for x in plan["items"]]
        target = self.game_root / "archive" / "pc" / "mod" / "modlist.txt"
        current: list[str] = []
        if target.is_file():
            try:
                current = [line.strip() for line in target.read_text(encoding="utf-8-sig", errors="replace").splitlines() if line.strip()]
            except OSError:
                current = []
        current_pos = {name: i for i, name in enumerate(current)}
        desired_pos = {name: i for i, name in enumerate(desired)}
        moved = [
            {"archive": name, "from": current_pos[name] + 1, "to": desired_pos[name] + 1}
            for name in desired if name in current_pos and current_pos[name] != desired_pos[name]
        ]
        return {
            "profile": profile,
            "managed": self.modlist_managed(),
            "path": str(target),
            "current": current,
            "desired": desired,
            "added": [x for x in desired if x not in current_pos],
            "removed": [x for x in current if x not in desired_pos],
            "moved": moved,
            "would_change": current != desired,
            "content": self.modlist_content(profile),
            "items": plan["items"],
            "rules": plan["rules"],
            "warnings": plan["warnings"],
        }

    def _generated_row(self, rel: str):
        return self.conn.execute("SELECT * FROM generated_deployed WHERE rel_path=?", (rel,)).fetchone()

    def sync_modlist(self, *, force_external: bool = False, enable_management: bool = False) -> dict:
        """Generate and hardlink archive/pc/mod/modlist.txt from the active profile."""
        profile = self.active_profile
        if enable_management:
            self.set_modlist_managed(True)
        rel = "archive/pc/mod/modlist.txt"
        target = self.game_root / Path(rel)
        old = self._generated_row(rel)
        content = self.modlist_content(profile)
        if not content:
            removed = False
            if target.exists() and old is not None:
                try:
                    st = target.stat()
                    if (st.st_dev, st.st_ino) == (int(old["dev"]), int(old["inode"])):
                        target.unlink(); removed = True; _remove_empty_parents(target, self.game_root)
                except OSError:
                    pass
            with self.conn:
                self.conn.execute("DELETE FROM generated_deployed WHERE rel_path=?", (rel,))
            return {"ok": True, "profile": profile, "path": rel, "removed": removed, "archives": 0, "content": ""}

        gen_dir = self.root / "generated" / "load_order" / hashlib.sha1(profile.encode("utf-8", "replace")).hexdigest()[:12]
        canonical = gen_dir / "modlist.txt"
        gen_dir.mkdir(parents=True, exist_ok=True)
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        # Atomic publish of the canonical generated bytes; an already-linked game
        # file remains on its old inode until the atomic relink below.
        fd, tmp_name = tempfile.mkstemp(prefix=".modlist-", dir=str(gen_dir))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(encoded)
                fh.flush(); os.fsync(fh.fileno())
            os.replace(tmp_name, canonical)
        finally:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        cstat = canonical.stat()
        managed_current = False
        if target.exists() and old is not None:
            try:
                tst = target.stat()
                managed_current = (tst.st_dev, tst.st_ino) == (int(old["dev"]), int(old["inode"]))
            except OSError:
                managed_current = False
        if target.exists() and not managed_current and not force_external:
            return {
                "ok": False, "profile": profile, "path": rel, "archives": len(self.archive_load_order(profile)["items"]),
                "conflict": "existing modlist.txt is not a manager-owned hardlink", "content": content,
            }
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not managed_current and force_external:
            backup_dir = self.root / "external_backups" / "archive" / "pc" / "mod"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"modlist.txt.{int(time.time())}.bak"
            shutil.copy2(target, backup)
        tmp_target = target.parent / f".ncm-link-{uuid.uuid4().hex}"
        try:
            os.link(canonical, tmp_target)
            os.replace(tmp_target, target)
        except OSError as e:
            raise SmartDeploymentError(f"hardlink failed for {rel}: {e}") from e
        finally:
            try:
                tmp_target.unlink(missing_ok=True)
            except OSError:
                pass
        cstat = canonical.stat()
        stage_rel = canonical.relative_to(self.root).as_posix()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO generated_deployed(rel_path,stage_rel,sha256,dev,inode,deployed_at) VALUES(?,?,?,?,?,?)",
                (rel, stage_rel, digest, int(cstat.st_dev), int(cstat.st_ino), time.time()),
            )
        return {
            "ok": True, "profile": profile, "path": rel, "archives": len(self.archive_load_order(profile)["items"]),
            "stage_rel": stage_rel, "sha256": digest, "content": content,
        }

    def _ordered_archive_alias(self, profile: str, item: DesiredFile) -> str:
        rel = item.projected_rel
        p = PurePosixPath(rel)
        if len(p.parts) < 4 or tuple(x.lower() for x in p.parts[:3]) != ("archive", "pc", "mod"):
            return rel
        if p.suffix.lower() != ".archive":
            return rel
        row = self.conn.execute(
            "SELECT priority FROM load_order WHERE profile=? AND mod_id=?",
            (profile, item.mod_id),
        ).fetchone()
        if row is None:
            return rel
        # '!' sorts before alphanumeric archive names. Every mod participating in an
        # explicit order receives this prefix, preserving the requested order among them.
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", p.name)
        alias = f"!ncm-{int(row[0]):06d}--{item.mod_id}--{safe}"
        return PurePosixPath(*p.parts[:-1], alias).as_posix()

    def uninstall_mod(self, name: str) -> dict:
        """Remove a staged mod without ever deleting an unproven external game file."""
        row = self._mod_row(name)
        if not row:
            raise KeyError(f"managed mod not found: {name}")
        mod_id = int(row["id"]); slug = str(row["slug"])
        # First make the active projection stop selecting this mod while all deployment
        # ownership rows still exist, so deploy() can safely unlink/swap its hardlinks.
        profiles = [str(r[0]) for r in self.conn.execute("SELECT name FROM profiles")]
        with self.conn:
            for profile in profiles:
                self.conn.execute(
                    "INSERT OR REPLACE INTO profile_mods(profile,mod_id,enabled) VALUES(?,?,0)",
                    (profile, mod_id),
                )
        before = self.deploy()
        with self.conn:
            self.conn.execute("DELETE FROM mods WHERE id=?", (mod_id,))
        # Cascaded load-order/conflict preferences may expose another winner.
        after = self.deploy()
        payload = self.root / PAYLOAD_DIRNAME / slug
        shutil.rmtree(payload, ignore_errors=True)
        return {"name": name, "mod_id": mod_id, "before": before, "after": after}

    def desired(self, profile: str | None = None) -> tuple[dict[str, DesiredFile], list[dict]]:
        profile = profile or self.active_profile
        rows = self.conn.execute(
            "SELECT f.*,m.name FROM files f JOIN mods m ON m.id=f.mod_id "
            "JOIN profile_mods pm ON pm.mod_id=m.id AND pm.profile=? AND pm.enabled=1 "
            "ORDER BY f.projected_rel,m.id",
            (profile,),
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        blocked: list[dict] = []
        for r in rows:
            if r["redmod_mode"] == "metadata-only":
                continue
            if r["redmod_mode"] == "native-compile-required":
                blocked.append({
                    "mod": r["name"], "path": r["rel_path"],
                    "reason": "REDmod payload requires the official REDmod compilation step",
                })
                continue
            grouped.setdefault(str(r["projected_rel"]), []).append(r)

        desired: dict[str, DesiredFile] = {}
        for projected, candidates in grouped.items():
            rule = self.conn.execute(
                "SELECT preferred_mod_id FROM conflict_rules WHERE profile=? AND rel_path=?",
                (profile, projected),
            ).fetchone()
            if rule is not None and rule[0] is None:
                continue  # explicit keep-unmanaged/skip decision
            chosen = None
            if rule is not None:
                pid = int(rule[0])
                chosen = next((r for r in candidates if int(r["mod_id"]) == pid), None)
            if chosen is None:
                chosen = candidates[-1]  # deterministic last-installed winner
            item = DesiredFile(
                rel_path=str(chosen["rel_path"]),
                mod_id=int(chosen["mod_id"]),
                mod_name=str(chosen["name"]),
                stage_rel=str(chosen["stage_rel"]),
                sha256=str(chosen["sha256"]),
                size=int(chosen["size"]),
                projected_rel=projected,
                redmod_mode=str(chosen["redmod_mode"]),
            )
            final_rel = self._ordered_archive_alias(profile, item)
            desired[final_rel] = DesiredFile(
                rel_path=item.rel_path, mod_id=item.mod_id, mod_name=item.mod_name,
                stage_rel=item.stage_rel, sha256=item.sha256, size=item.size,
                projected_rel=final_rel, redmod_mode=item.redmod_mode,
            )
        return desired, blocked

    def deployment_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM deployed ORDER BY rel_path").fetchall()

    def drift(self) -> dict:
        items: list[dict] = []
        for row in self.deployment_rows():
            rel = str(row["rel_path"])
            target = self.game_root / Path(rel)
            stage = self.root / Path(str(row["stage_rel"]))
            status = "ok"
            detail = ""
            if not stage.is_file():
                status, detail = "staging-missing", "canonical staged file is missing"
            elif not target.exists():
                status, detail = "missing", "deployed game link was removed"
            else:
                try:
                    st = target.stat()
                    ss = stage.stat()
                except OSError as e:
                    status, detail = "unreadable", str(e)
                else:
                    recorded = (int(row["dev"]), int(row["inode"]))
                    if (st.st_dev, st.st_ino) != recorded:
                        status, detail = "replaced", "game path no longer points to the deployed inode"
                    elif (ss.st_dev, ss.st_ino) != recorded:
                        status, detail = "stale-link", "staging has a newer revision; link rebuild required"
                    else:
                        digest = _sha256(stage)
                        if digest != str(row["sha256"]):
                            status, detail = "modified-in-place", "hardlinked inode differs from install hash"
            if status != "ok":
                items.append({"path": rel, "status": status, "detail": detail})
        for row in self.conn.execute("SELECT * FROM generated_deployed ORDER BY rel_path").fetchall():
            rel = str(row["rel_path"])
            target = self.game_root / Path(rel)
            stage = self.root / Path(str(row["stage_rel"]))
            status = "ok"; detail = ""
            if not stage.is_file():
                status, detail = "staging-missing", "canonical generated file is missing"
            elif not target.exists():
                status, detail = "missing", "generated game link was removed"
            else:
                try:
                    st = target.stat(); ss = stage.stat()
                except OSError as e:
                    status, detail = "unreadable", str(e)
                else:
                    recorded = (int(row["dev"]), int(row["inode"]))
                    if (st.st_dev, st.st_ino) != recorded:
                        status, detail = "replaced", "generated game path no longer points to the managed inode"
                    elif (ss.st_dev, ss.st_ino) != recorded:
                        status, detail = "stale-link", "generated staging has a newer revision; rebuild required"
                    elif _sha256(stage) != str(row["sha256"]):
                        status, detail = "modified-in-place", "generated hardlinked inode differs from recorded hash"
            if status != "ok":
                items.append({"path": rel, "status": status, "detail": detail, "generated": True})

        counts: dict[str, int] = {}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {"ok": not items, "drift_count": len(items), "counts": counts, "items": items}

    def _managed_current(self, rel: str, target: Path) -> tuple[bool, sqlite3.Row | None]:
        row = self.conn.execute("SELECT * FROM deployed WHERE rel_path=?", (rel,)).fetchone()
        if row is None or not target.exists():
            return False, row
        try:
            st = target.stat()
        except OSError:
            return False, row
        return (st.st_dev, st.st_ino) == (int(row["dev"]), int(row["inode"])), row

    def deploy(self, *, force_external: bool = False, force_external_paths: set[str] | None = None,
               sync_generated: bool = True) -> dict:
        desired, blocked = self.desired()
        force_external_paths = { _safe_rel(p) for p in (force_external_paths or set()) }
        existing = {str(r["rel_path"]): r for r in self.deployment_rows()}
        removed: list[str] = []
        linked: list[str] = []
        unchanged: list[str] = []
        external_conflicts: list[dict] = []
        preserved_drift: list[dict] = []

        # Remove links that are no longer desired. Never delete an externally
        # replaced file just because our DB used to own that pathname.
        for rel, row in existing.items():
            if rel in desired:
                continue
            target = self.game_root / Path(rel)
            stage = self.root / Path(str(row["stage_rel"]))
            is_managed, _ = self._managed_current(rel, target)
            if target.exists() and is_managed:
                try:
                    target.unlink()
                    removed.append(rel)
                    _remove_empty_parents(target, self.game_root)
                except OSError as e:
                    preserved_drift.append({"path": rel, "reason": str(e)})
                    continue
            elif target.exists():
                preserved_drift.append({"path": rel, "reason": "externally replaced; preserved"})
            with self.conn:
                self.conn.execute("DELETE FROM deployed WHERE rel_path=?", (rel,))

        for rel, item in desired.items():
            rel = _safe_rel(rel)
            target = _inside(self.game_root, self.game_root / Path(rel))
            stage = _inside(self.root, self.root / Path(item.stage_rel))
            if not stage.is_file():
                external_conflicts.append({"path": rel, "reason": "staging file missing", "mod": item.mod_name})
                continue
            stage_stat = stage.stat()
            if target.exists() and _same_inode(target, stage):
                # Hardlink already correct. Refresh the baseline only if the
                # canonical payload still matches its immutable install hash.
                if _sha256(stage) != item.sha256:
                    external_conflicts.append({
                        "path": rel, "reason": "staged/deployed inode was modified in place", "mod": item.mod_name,
                    })
                    continue
                unchanged.append(rel)
            else:
                can_replace = False
                old = existing.get(rel)
                if target.exists() and old is not None:
                    try:
                        tst = target.stat()
                        can_replace = (tst.st_dev, tst.st_ino) == (int(old["dev"]), int(old["inode"]))
                    except OSError:
                        can_replace = False
                allow_external = force_external or rel in force_external_paths
                if target.exists() and not can_replace and not allow_external:
                    external_conflicts.append({
                        "path": rel,
                        "reason": "existing game file is not a managed hardlink; migrate it or resolve manually",
                        "mod": item.mod_name,
                    })
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                # Build the hardlink *before* touching the current pathname, then
                # atomically swap it in. A cross-filesystem/permission failure
                # therefore cannot leave the game with a missing file.
                tmp_target = target.parent / f".ncm-link-{uuid.uuid4().hex}"
                try:
                    os.link(stage, tmp_target)
                except OSError as e:
                    raise SmartDeploymentError(
                        f"hardlink failed for {rel}: {e}. Staging and game paths must be on the same Linux filesystem."
                    ) from e
                try:
                    if target.exists() and not can_replace and allow_external:
                        backup = self.root / "external_backups" / Path(rel)
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        if not backup.exists():
                            shutil.copy2(target, backup)
                    os.replace(tmp_target, target)
                finally:
                    try:
                        tmp_target.unlink(missing_ok=True)
                    except OSError:
                        pass
                linked.append(rel)
                stage_stat = stage.stat()

            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO deployed(rel_path,mod_id,stage_rel,sha256,dev,inode,deployed_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (rel, item.mod_id, item.stage_rel, item.sha256,
                     int(stage_stat.st_dev), int(stage_stat.st_ino), time.time()),
                )

        modlist = None
        if sync_generated and self.modlist_managed():
            if external_conflicts:
                modlist = {"ok": False, "skipped": "deployment conflicts must be resolved before regenerating modlist.txt"}
            else:
                modlist = self.sync_modlist(force_external=False)
                if not modlist.get("ok", False):
                    external_conflicts.append({
                        "path": "archive/pc/mod/modlist.txt",
                        "reason": str(modlist.get("conflict") or "modlist generation failed"),
                        "mod": "Nexus Collection Manager",
                    })
        return {
            "ok": not external_conflicts,
            "profile": self.active_profile,
            "desired": len(desired),
            "linked": linked,
            "removed": removed,
            "unchanged": unchanged,
            "conflicts": external_conflicts,
            "preserved_drift": preserved_drift,
            "redmod_blocked": blocked,
            "modlist": modlist,
        }

    def undeploy(self) -> dict:
        removed: list[str] = []
        preserved: list[dict] = []
        for row in self.deployment_rows():
            rel = str(row["rel_path"])
            target = self.game_root / Path(rel)
            stage = self.root / Path(str(row["stage_rel"]))
            if not target.exists():
                with self.conn:
                    self.conn.execute("DELETE FROM deployed WHERE rel_path=?", (rel,))
                continue
            is_managed, _ = self._managed_current(rel, target)
            if not is_managed:
                preserved.append({"path": rel, "reason": "externally replaced; not deleted"})
                with self.conn:
                    self.conn.execute("DELETE FROM deployed WHERE rel_path=?", (rel,))
                continue
            try:
                target.unlink()
                removed.append(rel)
                _remove_empty_parents(target, self.game_root)
            except OSError as e:
                preserved.append({"path": rel, "reason": str(e)})
                continue
            with self.conn:
                self.conn.execute("DELETE FROM deployed WHERE rel_path=?", (rel,))
        for row in self.conn.execute("SELECT * FROM generated_deployed ORDER BY rel_path").fetchall():
            rel = str(row["rel_path"]); target = self.game_root / Path(rel)
            if target.exists():
                try:
                    st = target.stat()
                    if (st.st_dev, st.st_ino) == (int(row["dev"]), int(row["inode"])):
                        target.unlink(); removed.append(rel); _remove_empty_parents(target, self.game_root)
                    else:
                        preserved.append({"path": rel, "reason": "externally replaced generated file; not deleted"})
                except OSError as e:
                    preserved.append({"path": rel, "reason": str(e)})
            with self.conn:
                self.conn.execute("DELETE FROM generated_deployed WHERE rel_path=?", (rel,))
        return {"ok": not preserved, "removed": removed, "preserved": preserved, "profile": self.active_profile}

    def status(self) -> dict:
        desired, blocked = self.desired()
        mods = self.installed_mods()
        drift = self.drift()
        return {
            "game_root": str(self.game_root),
            "staging_root": str(self.root),
            "active_profile": self.active_profile,
            "profiles": self.profiles(),
            "mods": mods,
            "mod_count": len(mods),
            "desired_links": len(desired),
            "deployed_links": self.conn.execute("SELECT COUNT(*) FROM deployed").fetchone()[0],
            "drift": drift,
            "redmod_blocked": blocked,
            "load_order": self.load_order_rows(),
            "archive_load_order": self.archive_load_order(),
            "modlist_managed": self.modlist_managed(),
            "modlist_preview": self.modlist_preview(),
        }


def _legacy_manifest(game_root: Path) -> dict[str, list[str]]:
    path = game_root / ".mod_manifest.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    mods = raw.get("mods", raw) if isinstance(raw, dict) else {}
    if not isinstance(mods, dict):
        return {}
    return {str(k): [str(x) for x in (v or [])] for k, v in mods.items() if isinstance(v, list)}


def _legacy_disabled(game_root: Path) -> set[str]:
    path = game_root / ".mod_disabled.json"
    if not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(raw, dict):
        return set()
    result = set()
    for _key, value in raw.items():
        if isinstance(value, dict) and value.get("stem"):
            result.add(str(value["stem"]))
    return result


def _migrate_legacy_unlocked(game_root: Path, *, remove_legacy_state: bool = True) -> dict:
    """One-time import of this manager's historical in-place manifest.

    The old manifest already contains the authoritative mod->path ownership
    needed to separate pre-existing installs. Files are copied to independent
    staging inodes first; only after all staging writes succeed are the original
    in-place files removed and recreated as hardlinks.
    """
    game_root = game_root.expanduser().resolve()
    manifest = _legacy_manifest(game_root)
    if not manifest:
        raise SmartDeploymentError("no legacy .mod_manifest.json found; nothing authoritative to migrate")
    disabled = _legacy_disabled(game_root)
    missing: list[dict] = []
    imported: list[dict] = []
    originals: list[Path] = []
    with Store(game_root) as store:
        for name, rels in manifest.items():
            pairs: list[tuple[Path, str]] = []
            for raw in rels:
                try:
                    rel = _safe_rel(raw)
                except ValueError:
                    missing.append({"mod": name, "path": raw, "reason": "unsafe legacy path"})
                    continue
                src = game_root / Path(rel)
                if not src.is_file():
                    missing.append({"mod": name, "path": rel, "reason": "missing"})
                    continue
                pairs.append((src, rel))
                originals.append(src)
            if pairs:
                info = store.stage_mod(name, pairs, resolutions={rel: "overwrite" for _src, rel in pairs})
                imported.append(info)
            else:
                # Preserve an explicitly disabled historical entry even if its
                # files were already removed by the old disable implementation.
                row = store._ensure_mod(name)
                with store.conn:
                    for profile in [r[0] for r in store.conn.execute("SELECT name FROM profiles")]:
                        store.conn.execute(
                            "INSERT OR REPLACE INTO profile_mods(profile,mod_id,enabled) VALUES(?,?,?)",
                            (profile, row["id"], 0 if name in disabled else 1),
                        )
            if name in disabled:
                store.set_enabled(name, False)

        # Now that staging is durable, remove only the legacy files that still
        # match paths claimed by the legacy manifest.  No recursive root delete.
        for p in originals:
            try:
                p.unlink()
                _remove_empty_parents(p, game_root)
            except FileNotFoundError:
                pass
        deploy_result = store.deploy()

        moved_state: list[str] = []
        if remove_legacy_state:
            dst_root = store.root / LEGACY_STATE_DIRNAME
            dst_root.mkdir(parents=True, exist_ok=True)
            for name in (".mod_manifest.json", ".mod_disabled.json", ".mod_pins.json"):
                src = game_root / name
                if src.exists():
                    dst = dst_root / name
                    if dst.exists():
                        dst = dst_root / f"{name}.{int(time.time())}"
                    shutil.move(str(src), str(dst))
                    moved_state.append(name)

        return {
            "ok": deploy_result.get("ok", False),
            "imported_mods": len(imported),
            "imported": imported,
            "missing": missing,
            "legacy_state_moved": moved_state,
            "deploy": deploy_result,
        }


def migrate_legacy(game_root: Path, *, remove_legacy_state: bool = True) -> dict:
    with deployment_lock(game_root, exclusive=True):
        return _migrate_legacy_unlocked(game_root, remove_legacy_state=remove_legacy_state)


def stage_and_deploy(
    game_root: Path,
    name: str,
    pairs: Sequence[tuple[Path, str]],
    *,
    resolutions: dict[str, str] | None = None,
    source_archive: str | None = None,
    auto_deploy: bool = True,
) -> dict:
    with deployment_lock(game_root, exclusive=True):
        with Store(game_root) as store:
            staged = store.stage_mod(name, pairs, resolutions=resolutions, source_archive=source_archive)
            archive_record = None
            archive_warning = ""
            if source_archive:
                try:
                    # Lazy import avoids making the core link engine depend on package-format tooling.
                    from . import archive_management as AM
                    archive_record = AM.associate_source_archive(store, source_archive, int(staged["mod_id"]))
                except Exception as exc:  # archive retention must not invalidate an otherwise safe install
                    archive_warning = str(exc)
            force_paths = {
                _project_redmod(_safe_rel(rel))[0]
                for rel, action in (resolutions or {}).items() if action == "overwrite"
            }
            deployed = store.deploy(force_external_paths=force_paths) if auto_deploy else None
            return {
                "staged": staged, "deploy": deployed,
                "archive": archive_record, "archive_warning": archive_warning,
            }


def set_enabled(game_root: Path, name: str, enabled: bool, *, rebuild: bool = True) -> dict:
    with deployment_lock(game_root, exclusive=True):
        with Store(game_root) as store:
            store.set_enabled(name, enabled)
            result = store.deploy() if rebuild else None
            return {"name": name, "enabled": enabled, "profile": store.active_profile, "deploy": result}


def switch_profile(game_root: Path, name: str) -> dict:
    with deployment_lock(game_root, exclusive=True):
        with Store(game_root) as store:
            old = store.active_profile
            store.switch_profile_flag(name)
            result = store.deploy()
            return {"old_profile": old, "active_profile": name, "deploy": result}


class _APIHandler(BaseHTTPRequestHandler):
    server_version = "NexusCollectionManagerLinks/0.8"

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        token = getattr(self.server, "api_token", None)
        if not token:
            return True
        return self.headers.get("Authorization") == f"Bearer {token}"

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        route = urlparse(self.path).path
        try:
            root = getattr(self.server, "game_root")
            with deployment_lock(root, exclusive=False):
                with Store(root) as store:
                    if route == "/status":
                        self._send(200, store.status())
                        return
                    if route == "/drift":
                        self._send(200, store.drift())
                        return
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        route = urlparse(self.path).path
        try:
            root = getattr(self.server, "game_root")
            with deployment_lock(root, exclusive=True):
                with Store(root) as store:
                    if route == "/deploy":
                        self._send(200, store.deploy())
                        return
                    if route == "/undeploy":
                        self._send(200, store.undeploy())
                        return
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})
            return
        self._send(404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:
        print("[links-api] " + (fmt % args))

def serve(game_root: Path, host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise SmartDeploymentError("non-local API binding requires --token")
    httpd = ThreadingHTTPServer((host, port), _APIHandler)
    httpd.game_root = game_root.expanduser().resolve()  # type: ignore[attr-defined]
    httpd.api_token = token  # type: ignore[attr-defined]
    print(f"[links-api] listening on http://{host}:{httpd.server_port} for {httpd.game_root}")
    httpd.serve_forever()


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Clean-game hardlink deployment manager (Linux only)")
    sub = ap.add_subparsers(dest="command", required=True)

    def root_parser(name: str, help_text: str):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--game-root", type=Path, required=True)
        return p

    root_parser("status", "Show staged/deployed/profile state")
    p = root_parser("deploy", "Rebuild game hardlinks for the active profile")
    p.add_argument("--force-external", action="store_true",
                   help="Back up and replace non-managed files that collide with desired links")
    root_parser("undeploy", "Remove only hardlinks owned by this deployment DB")
    root_parser("drift", "Detect replaced/missing/modified deployed links")
    root_parser("migrate", "Migrate the historical .mod_manifest.json in-place install")

    p = root_parser("enable", "Enable a managed mod in the active profile and rebuild links")
    p.add_argument("name")
    p = root_parser("disable", "Disable a managed mod in the active profile and rebuild links")
    p.add_argument("name")

    p = root_parser("profile-create", "Create a profile cloned from the active profile")
    p.add_argument("name")
    p.add_argument("--from-profile")
    p = root_parser("profile-switch", "Atomically switch active profile and rebuild links")
    p.add_argument("name")
    root_parser("profiles", "List profiles")

    p = root_parser("serve", "Serve localhost GET /status,/drift and POST /deploy,/undeploy")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token", default=os.environ.get("NCM_DEPLOY_TOKEN"))
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.game_root.expanduser().resolve()
    try:
        if not root.is_dir():
            raise FileNotFoundError(f"game root not found: {root}")
        if args.command == "serve":
            serve(root, args.host, args.port, args.token)
            return 0
        if args.command == "migrate":
            result = migrate_legacy(root)
        else:
            readonly = args.command in {"status", "drift", "profiles"}
            with deployment_lock(root, exclusive=not readonly):
                with Store(root) as store:
                    if args.command == "status": result = store.status()
                    elif args.command == "deploy": result = store.deploy(force_external=args.force_external)
                    elif args.command == "undeploy": result = store.undeploy()
                    elif args.command == "drift": result = store.drift()
                    elif args.command == "enable":
                        store.set_enabled(args.name, True); result = store.deploy(); result["enabled"] = args.name
                    elif args.command == "disable":
                        store.set_enabled(args.name, False); result = store.deploy(); result["disabled"] = args.name
                    elif args.command == "profile-create": result = store.create_profile(args.name, args.from_profile)
                    elif args.command == "profile-switch":
                        old = store.active_profile; store.switch_profile_flag(args.name); result = store.deploy()
                        result.update({"old_profile": old, "active_profile": args.name})
                    elif args.command == "profiles": result = {"active_profile": store.active_profile, "profiles": store.profiles()}
                    else: raise AssertionError(args.command)
        _json_line(result)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"[!] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
