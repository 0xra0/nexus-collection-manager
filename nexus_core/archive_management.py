#!/usr/bin/env python3
"""Managed archive library for downloaded Cyberpunk 2077 mod packages.

Canonical package archives live under ``<game>/downloaded_mods/archives``.  This
module indexes them in the smart-deployment SQLite database, lets the UI browse
archive contents without extracting, records manual Nexus identity links, and
performs conservative orphan cleanup.

An archive is considered an *orphan* only when it is neither associated with a
managed/staged mod nor linked to a Nexus Mods page.  Cleanup is dry-run by
default and never touches staged extracted payloads or deployed hardlinks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import smart_deployment as SD

ARCHIVE_DIRNAME = "archives"
SUPPORTED_SUFFIXES = {".zip", ".7z", ".rar"}
GAME_DOMAIN = "cyberpunk2077"
JSON_PREFIX = "[archives-json] "
_NEXUS_URL_RE = re.compile(r"(?:nexusmods\.com/[^/]+/mods/|^)(\d+)(?:\D|$)", re.I)


class ArchiveManagementError(RuntimeError):
    pass


def archive_root(game_root: Path) -> Path:
    return SD.staging_root(game_root) / ARCHIVE_DIRNAME


def _emit(payload: dict[str, Any]) -> None:
    print(JSON_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _ensure_schema(store: SD.Store) -> None:
    """Create archive tables for databases made by any earlier app version."""
    with store.conn:
        store.conn.executescript(
            """
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
            """
        )


def _safe_filename(name: str) -> str:
    clean = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", Path(name).name).strip(" .")
    return clean or "mod-archive.zip"


def _hashes(path: Path, chunk: int = 4 * 1024 * 1024) -> tuple[str, str]:
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            sha.update(block)
            md5.update(block)
    return sha.hexdigest(), md5.hexdigest()


def _copy_atomic_and_hash(src: Path, dest: Path) -> tuple[str, str, int, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".archive-import-", dir=str(dest.parent))
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    try:
        with src.open("rb") as r, os.fdopen(fd, "wb") as w:
            while True:
                block = r.read(4 * 1024 * 1024)
                if not block:
                    break
                w.write(block)
                sha.update(block)
                md5.update(block)
            w.flush(); os.fsync(w.fileno())
        try:
            shutil.copystat(src, tmp_name, follow_symlinks=False)
        except OSError:
            pass
        os.replace(tmp_name, dest)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
    st = dest.stat()
    return sha.hexdigest(), md5.hexdigest(), int(st.st_size), int(st.st_mtime_ns)


def _record_by_id(store: SD.Store, archive_id: int):
    _ensure_schema(store)
    row = store.conn.execute("SELECT * FROM archive_library WHERE id=?", (int(archive_id),)).fetchone()
    if row is None:
        raise KeyError(f"archive not found: {archive_id}")
    return row


def _path_for_row(store: SD.Store, row) -> Path:
    root = archive_root(store.game_root).resolve()
    path = (store.root / str(row["stage_rel"])).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ArchiveManagementError(f"archive index path escapes archive library: {path}") from exc
    return path


def _candidate_destination(store: SD.Store, source: Path, sha256: str) -> Path:
    root = archive_root(store.game_root)
    root.mkdir(parents=True, exist_ok=True)
    name = _safe_filename(source.name)
    dest = root / name
    if not dest.exists():
        return dest
    try:
        existing = store.conn.execute(
            "SELECT sha256 FROM archive_library WHERE stage_rel=?",
            (dest.relative_to(store.root).as_posix(),),
        ).fetchone()
        if existing and str(existing[0]) == sha256:
            return dest
    except (OSError, ValueError):
        pass
    p = Path(name)
    return root / f"{p.stem}--{sha256[:10]}{p.suffix.lower()}"


def _suggested_nexus_id(file_name: str) -> int | None:
    """Conservative filename suggestion; never persisted until the user links it."""
    # Nexus collection downloads conventionally include -<mod id>-<version...>.
    stem = Path(file_name).stem
    candidates = re.findall(r"(?:^|[-_ ])(\d{3,8})(?=[-_ .]|$)", stem)
    if not candidates:
        return None
    # The right-most large numeric token is usually the Nexus mod id; version
    # fragments are normally much shorter once split by separators.
    try:
        value = int(candidates[-1])
    except ValueError:
        return None
    return value if value > 0 else None


def import_archive(
    store: SD.Store,
    source: Path,
    *,
    managed_mod_id: int | None = None,
) -> dict[str, Any]:
    """Copy/index one package archive into the canonical archive library."""
    _ensure_schema(store)
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"unsupported mod archive type: {source.suffix or '(none)'}")

    root = archive_root(store.game_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        already_inside = source.relative_to(root) is not None
    except ValueError:
        already_inside = False

    if already_inside:
        dest = source
        sha256, md5 = _hashes(dest)
        st = dest.stat(); size = int(st.st_size); mtime_ns = int(st.st_mtime_ns)
    else:
        # Common case: stream-copy + hash once.  Only hash the source before
        # copying when the human filename already collides in the library.
        dest = root / _safe_filename(source.name)
        if not dest.exists():
            sha256, md5, size, mtime_ns = _copy_atomic_and_hash(source, dest)
        else:
            source_sha, source_md5 = _hashes(source)
            stage_rel_existing = dest.relative_to(store.root).as_posix()
            existing = store.conn.execute(
                "SELECT sha256,md5,size,mtime_ns FROM archive_library WHERE stage_rel=?",
                (stage_rel_existing,),
            ).fetchone()
            if existing is not None and str(existing["sha256"]) == source_sha:
                sha256, md5 = source_sha, source_md5
                st = dest.stat(); size = int(st.st_size); mtime_ns = int(st.st_mtime_ns)
            else:
                if existing is None:
                    dest_sha, _ = _hashes(dest)
                else:
                    dest_sha = str(existing["sha256"])
                if dest_sha == source_sha:
                    sha256, md5 = source_sha, source_md5
                    st = dest.stat(); size = int(st.st_size); mtime_ns = int(st.st_mtime_ns)
                else:
                    p = Path(dest.name)
                    dest = root / f"{p.stem}--{source_sha[:10]}{p.suffix.lower()}"
                    if dest.exists():
                        dest_sha2, dest_md52 = _hashes(dest)
                        if dest_sha2 == source_sha:
                            sha256, md5 = dest_sha2, dest_md52
                            st = dest.stat(); size = int(st.st_size); mtime_ns = int(st.st_mtime_ns)
                        else:
                            dest = root / f"{p.stem}--{source_sha[:10]}--{int(time.time() * 1000)}{p.suffix.lower()}"
                            sha256, md5, size, mtime_ns = _copy_atomic_and_hash(source, dest)
                    else:
                        sha256, md5, size, mtime_ns = _copy_atomic_and_hash(source, dest)
                    if sha256 != source_sha:
                        dest.unlink(missing_ok=True)
                        raise ArchiveManagementError("archive copy hash mismatch")

    stage_rel = dest.relative_to(store.root).as_posix()
    now = time.time()
    with store.conn:
        existing = store.conn.execute(
            "SELECT id,nexus_mod_id,nexus_file_id,nexus_game_domain,link_source FROM archive_library WHERE stage_rel=?",
            (stage_rel,),
        ).fetchone()
        if existing:
            archive_id = int(existing["id"])
            store.conn.execute(
                "UPDATE archive_library SET file_name=?,original_path=?,sha256=?,md5=?,size=?,mtime_ns=? WHERE id=?",
                (dest.name, str(source), sha256, md5, size, mtime_ns, archive_id),
            )
        else:
            cur = store.conn.execute(
                "INSERT INTO archive_library(file_name,stage_rel,original_path,sha256,md5,size,mtime_ns,imported_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (dest.name, stage_rel, str(source), sha256, md5, size, mtime_ns, now),
            )
            archive_id = int(cur.lastrowid)
        if managed_mod_id is not None:
            mod = store.conn.execute("SELECT id FROM mods WHERE id=?", (int(managed_mod_id),)).fetchone()
            if mod is None:
                raise KeyError(f"managed mod not found: {managed_mod_id}")
            store.conn.execute(
                "INSERT OR REPLACE INTO archive_installs(archive_id,mod_id,linked_at) VALUES(?,?,?)",
                (archive_id, int(managed_mod_id), now),
            )
    return archive_detail(store, archive_id)


def associate_source_archive(store: SD.Store, source_archive: str | Path, managed_mod_id: int) -> dict[str, Any] | None:
    """Associate an install's source package, importing it when it still exists."""
    _ensure_schema(store)
    raw = str(source_archive or "").strip()
    if not raw:
        return None
    source = Path(raw).expanduser()
    if source.is_file():
        return import_archive(store, source, managed_mod_id=managed_mod_id)

    # If the original download was moved/deleted, reuse an already indexed copy
    # by exact original path first, then basename only when that is unambiguous.
    rows = store.conn.execute(
        "SELECT id FROM archive_library WHERE original_path=?",
        (raw,),
    ).fetchall()
    if not rows:
        rows = store.conn.execute(
            "SELECT id FROM archive_library WHERE file_name=?",
            (Path(raw).name,),
        ).fetchall()
    if len(rows) != 1:
        return None
    archive_id = int(rows[0][0])
    with store.conn:
        store.conn.execute(
            "INSERT OR REPLACE INTO archive_installs(archive_id,mod_id,linked_at) VALUES(?,?,?)",
            (archive_id, int(managed_mod_id), time.time()),
        )
    return archive_detail(store, archive_id)


def refresh_library(store: SD.Store) -> dict[str, Any]:
    """Index package files physically present under downloaded_mods/archives."""
    _ensure_schema(store)
    root = archive_root(store.game_root)
    root.mkdir(parents=True, exist_ok=True)
    indexed = 0; refreshed = 0; skipped = 0
    for path in sorted(root.rglob("*"), key=lambda p: str(p).casefold()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        stage_rel = path.relative_to(store.root).as_posix()
        row = store.conn.execute("SELECT * FROM archive_library WHERE stage_rel=?", (stage_rel,)).fetchone()
        st = path.stat()
        if row and int(row["size"]) == st.st_size and int(row["mtime_ns"]) == st.st_mtime_ns:
            skipped += 1
            continue
        sha256, md5 = _hashes(path)
        with store.conn:
            if row:
                store.conn.execute(
                    "UPDATE archive_library SET file_name=?,sha256=?,md5=?,size=?,mtime_ns=? WHERE id=?",
                    (path.name, sha256, md5, int(st.st_size), int(st.st_mtime_ns), int(row["id"])),
                )
                refreshed += 1
            else:
                store.conn.execute(
                    "INSERT INTO archive_library(file_name,stage_rel,original_path,sha256,md5,size,mtime_ns,imported_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (path.name, stage_rel, str(path), sha256, md5, int(st.st_size), int(st.st_mtime_ns), time.time()),
                )
                indexed += 1
    return {"indexed": indexed, "refreshed": refreshed, "unchanged": skipped, "root": str(root)}


def _row_to_dict(store: SD.Store, row) -> dict[str, Any]:
    archive_id = int(row["id"])
    installs = store.conn.execute(
        "SELECT m.id,m.name FROM archive_installs ai JOIN mods m ON m.id=ai.mod_id "
        "WHERE ai.archive_id=? ORDER BY m.name COLLATE NOCASE",
        (archive_id,),
    ).fetchall()
    path = _path_for_row(store, row)
    nexus_id = int(row["nexus_mod_id"]) if row["nexus_mod_id"] is not None else None
    installed = [{"id": int(x["id"]), "name": str(x["name"])} for x in installs]
    orphan = not installed and nexus_id is None
    return {
        "id": archive_id,
        "file_name": str(row["file_name"]),
        "path": str(path),
        "stage_rel": str(row["stage_rel"]),
        "original_path": str(row["original_path"] or ""),
        "sha256": str(row["sha256"]),
        "md5": str(row["md5"]),
        "size": int(row["size"]),
        "mtime_ns": int(row["mtime_ns"]),
        "imported_at": float(row["imported_at"]),
        "exists": path.is_file(),
        "installed_mods": installed,
        "nexus_mod_id": nexus_id,
        "nexus_file_id": int(row["nexus_file_id"]) if row["nexus_file_id"] is not None else None,
        "nexus_game_domain": str(row["nexus_game_domain"] or GAME_DOMAIN),
        "nexus_url": f"https://www.nexusmods.com/{row['nexus_game_domain'] or GAME_DOMAIN}/mods/{nexus_id}" if nexus_id else "",
        "link_source": str(row["link_source"] or ""),
        "suggested_nexus_mod_id": _suggested_nexus_id(str(row["file_name"])),
        "orphan": orphan,
        "status": "missing" if not path.is_file() else ("orphan" if orphan else ("installed" if installed else "linked")),
    }


def archive_detail(store: SD.Store, archive_id: int) -> dict[str, Any]:
    return _row_to_dict(store, _record_by_id(store, archive_id))


def list_archives(store: SD.Store, *, refresh: bool = True) -> dict[str, Any]:
    _ensure_schema(store)
    refresh_result = refresh_library(store) if refresh else None
    rows = store.conn.execute("SELECT * FROM archive_library ORDER BY file_name COLLATE NOCASE,id").fetchall()
    items = [_row_to_dict(store, row) for row in rows]
    total_bytes = sum(int(x["size"]) for x in items if x["exists"])
    return {
        "root": str(archive_root(store.game_root)),
        "archives": len(items),
        "bytes": total_bytes,
        "orphans": sum(1 for x in items if x["orphan"]),
        "missing": sum(1 for x in items if not x["exists"]),
        "items": items,
        "refresh": refresh_result,
    }


def _unsafe_member(name: str) -> bool:
    raw = str(name).replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return True
    return any(part == ".." for part in PurePosixPath(raw).parts)


def browse_contents(store: SD.Store, archive_id: int, *, max_entries: int = 5000) -> dict[str, Any]:
    row = _record_by_id(store, archive_id)
    path = _path_for_row(store, row)
    if not path.is_file():
        raise FileNotFoundError(path)
    max_entries = max(1, min(int(max_entries), 20000))
    entries: list[dict[str, Any]] = []
    truncated = False
    fmt = path.suffix.lower().lstrip(".")

    if zipfile.is_zipfile(path):
        fmt = "zip"
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            truncated = len(infos) > max_entries
            for info in infos[:max_entries]:
                mode = (info.external_attr >> 16) & 0xFFFF
                kind = "directory" if info.is_dir() else ("symlink" if stat.S_ISLNK(mode) else "file")
                entries.append({
                    "path": info.filename,
                    "size": int(info.file_size),
                    "compressed": int(info.compress_size),
                    "kind": kind,
                    "unsafe": _unsafe_member(info.filename),
                })
    else:
        bsdtar = shutil.which("bsdtar")
        if not bsdtar:
            raise ArchiveManagementError(f"bsdtar is required to browse .{fmt} packages")
        proc = subprocess.run(
            [bsdtar, "-tf", str(path)], capture_output=True, text=True, errors="replace", timeout=60,
        )
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "archive listing failed").strip().splitlines()[-1]
            raise ArchiveManagementError(msg)
        names = [line.rstrip("\r") for line in proc.stdout.splitlines() if line.strip()]
        truncated = len(names) > max_entries
        for name in names[:max_entries]:
            entries.append({
                "path": name,
                "size": None,
                "compressed": None,
                "kind": "directory" if name.endswith("/") else "file",
                "unsafe": _unsafe_member(name),
            })
    return {
        "archive": _row_to_dict(store, row),
        "format": fmt,
        "entries": entries,
        "entry_count": len(entries),
        "truncated": truncated,
        "unsafe_entries": sum(1 for e in entries if e["unsafe"]),
    }


def _parse_nexus_id(value: str | int) -> int:
    raw = str(value).strip()
    if raw.isdigit():
        mod_id = int(raw)
    else:
        m = _NEXUS_URL_RE.search(raw)
        if not m:
            raise ValueError("Nexus mod must be a positive numeric ID or Nexus Mods URL")
        mod_id = int(m.group(1))
    if mod_id <= 0:
        raise ValueError("Nexus mod id must be positive")
    return mod_id


def link_nexus(store: SD.Store, archive_id: int, nexus_mod: str | int, *, nexus_file_id: int | None = None) -> dict[str, Any]:
    row = _record_by_id(store, archive_id)
    mod_id = _parse_nexus_id(nexus_mod)
    if nexus_file_id is not None and int(nexus_file_id) <= 0:
        raise ValueError("Nexus file id must be positive")
    with store.conn:
        store.conn.execute(
            "UPDATE archive_library SET nexus_mod_id=?,nexus_file_id=?,nexus_game_domain=?,link_source='manual' WHERE id=?",
            (mod_id, int(nexus_file_id) if nexus_file_id is not None else None, GAME_DOMAIN, int(row["id"])),
        )
    return archive_detail(store, archive_id)


def unlink_nexus(store: SD.Store, archive_id: int) -> dict[str, Any]:
    _record_by_id(store, archive_id)
    with store.conn:
        store.conn.execute(
            "UPDATE archive_library SET nexus_mod_id=NULL,nexus_file_id=NULL,link_source='' WHERE id=?",
            (int(archive_id),),
        )
    return archive_detail(store, archive_id)


def delete_archive(store: SD.Store, archive_id: int, *, force: bool = False) -> dict[str, Any]:
    row = _record_by_id(store, archive_id)
    detail = _row_to_dict(store, row)
    if detail["installed_mods"] and not force:
        names = ", ".join(x["name"] for x in detail["installed_mods"])
        raise ArchiveManagementError(
            f"archive is the recorded source for installed mod(s): {names}; use --force to delete only the raw package"
        )
    path = _path_for_row(store, row)
    deleted = False
    if path.exists():
        if not path.is_file():
            raise ArchiveManagementError(f"archive library entry is not a regular file: {path}")
        path.unlink(); deleted = True
        # Remove empty user-created subdirectories under archives/, but never the root.
        root = archive_root(store.game_root).resolve()
        cur = path.parent
        while cur != root:
            try:
                cur.rmdir()
            except OSError:
                break
            cur = cur.parent
    with store.conn:
        store.conn.execute("DELETE FROM archive_library WHERE id=?", (int(archive_id),))
    return {
        "deleted": deleted,
        "id": int(archive_id),
        "file_name": detail["file_name"],
        "installed_mods": detail["installed_mods"],
        "staged_payload_preserved": True,
    }


def orphan_report(store: SD.Store, *, refresh: bool = True) -> dict[str, Any]:
    data = list_archives(store, refresh=refresh)
    items = [x for x in data["items"] if x["orphan"]]
    return {
        "root": data["root"],
        "orphans": len(items),
        "bytes": sum(int(x["size"]) for x in items if x["exists"]),
        "items": items,
    }


def cleanup_orphans(store: SD.Store, *, apply: bool = False) -> dict[str, Any]:
    report = orphan_report(store, refresh=True)
    if not apply:
        return {**report, "dry_run": True, "deleted": 0, "freed_bytes": 0}
    deleted = 0; freed = 0; failures: list[dict[str, str]] = []
    for item in list(report["items"]):
        try:
            size = int(item["size"]) if item["exists"] else 0
            delete_archive(store, int(item["id"]), force=False)
            deleted += 1; freed += size
        except Exception as exc:  # noqa: BLE001 - per-item cleanup should continue
            failures.append({"file_name": str(item["file_name"]), "error": str(exc)})
    return {
        **report,
        "dry_run": False,
        "deleted": deleted,
        "freed_bytes": freed,
        "failures": failures,
    }


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Browse and maintain downloaded mod archives in smart staging")
    sub = ap.add_subparsers(dest="command", required=True)

    def root_cmd(name: str, help_text: str):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--game-root", type=Path, required=True)
        return p

    p = root_cmd("list", "List/index archives in downloaded_mods/archives")
    p.add_argument("--no-refresh", action="store_true")

    p = root_cmd("import", "Copy a downloaded package into the managed archive library")
    p.add_argument("archive", type=Path)

    p = root_cmd("contents", "Browse an archive without extracting it")
    p.add_argument("--archive-id", type=int, required=True)
    p.add_argument("--max-entries", type=int, default=5000)

    p = root_cmd("link", "Link an archive to a Nexus Mods page")
    p.add_argument("--archive-id", type=int, required=True)
    p.add_argument("--nexus-mod", required=True, help="numeric mod id or Nexus Mods URL")
    p.add_argument("--nexus-file-id", type=int)

    p = root_cmd("unlink", "Remove the archive's Nexus Mods identity link")
    p.add_argument("--archive-id", type=int, required=True)

    p = root_cmd("delete", "Delete a raw staged package (never the extracted mod payload)")
    p.add_argument("--archive-id", type=int, required=True)
    p.add_argument("--force", action="store_true", help="allow deletion when associated with an installed mod")

    root_cmd("orphans", "Preview unreferenced + unlinked archives eligible for cleanup")
    p = root_cmd("cleanup", "Clean orphaned archives; dry-run unless --apply is supplied")
    p.add_argument("--apply", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.game_root.expanduser().resolve()
    try:
        if not root.is_dir():
            raise FileNotFoundError(f"game root not found: {root}")
        readonly = args.command == "contents" or (args.command == "list" and args.no_refresh)
        with SD.deployment_lock(root, exclusive=not readonly):
            with SD.Store(root) as store:
                _ensure_schema(store)
                if args.command == "list":
                    result = list_archives(store, refresh=not args.no_refresh)
                elif args.command == "import":
                    result = {"archive": import_archive(store, args.archive), "library": list_archives(store, refresh=False)}
                elif args.command == "contents":
                    result = browse_contents(store, args.archive_id, max_entries=args.max_entries)
                elif args.command == "link":
                    result = {"archive": link_nexus(store, args.archive_id, args.nexus_mod, nexus_file_id=args.nexus_file_id)}
                elif args.command == "unlink":
                    result = {"archive": unlink_nexus(store, args.archive_id)}
                elif args.command == "delete":
                    result = delete_archive(store, args.archive_id, force=args.force)
                elif args.command == "orphans":
                    result = orphan_report(store, refresh=True)
                elif args.command == "cleanup":
                    result = cleanup_orphans(store, apply=args.apply)
                else:
                    raise AssertionError(args.command)
        _emit(result)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        _emit({"type": "error", "message": str(exc)})
        print(f"[!] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
