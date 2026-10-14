#!/usr/bin/env python3
"""Persistent Nexus Mods download queue with Linux NXM deep-link support.

Premium accounts can resolve downloads directly through the Nexus v1 API. Free
accounts can download manager-authorized ``nxm://`` links carrying temporary
``key``/``expires`` credentials. Completed packages are indexed by Archive
Management automatically, so the download queue and archive library share one
canonical staging area.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import archive_management as AM
from . import smart_deployment as SD
from .nexus_account import NexusAPIError, NexusAccountClient, resolve_api_key

API_BASE = "https://api.nexusmods.com/v1"
GAME_DOMAIN = "cyberpunk2077"
APP_NAME = "Nexus-Collection-Manager"
APP_VERSION = "0.17.6"
JSON_PREFIX = "[downloads-json] "
HANDLER_CONFIG = Path.home() / ".config" / "nexus-collection-manager" / "nxm-handler.json"
DESKTOP_FILE = Path.home() / ".local" / "share" / "applications" / "nexus-collection-manager-nxm.desktop"
_SUPPORTED_ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar"}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._()\[\] +'-]+")


class DownloadManagerError(RuntimeError):
    pass


@dataclass(frozen=True)
class NxmLink:
    original: str
    game_domain: str
    mod_id: int
    file_id: int
    key: str = ""
    expires: int = 0
    user_id: int | None = None

    @property
    def expired(self) -> bool:
        return bool(self.expires and self.expires <= int(time.time()))


def parse_nxm(url: str) -> NxmLink:
    raw = str(url or "").strip()
    if len(raw) > 4096:
        raise ValueError("NXM URL is too long")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() != "nxm":
        raise ValueError("expected an nxm:// URL")
    domain = (parsed.netloc or "").strip().lower()
    parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
    if len(parts) != 4 or parts[0].lower() != "mods" or parts[2].lower() != "files":
        raise ValueError("only nxm://<game>/mods/<mod_id>/files/<file_id> links are supported")
    try:
        mod_id, file_id = int(parts[1]), int(parts[3])
    except ValueError as exc:
        raise ValueError("NXM mod/file IDs must be numeric") from exc
    if not domain or mod_id <= 0 or file_id <= 0:
        raise ValueError("invalid NXM game/mod/file identifier")
    q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    key = (q.get("key") or [""])[0]
    try:
        expires = int((q.get("expires") or ["0"])[0] or 0)
    except ValueError:
        expires = 0
    user_raw = (q.get("user_id") or [""])[0]
    try:
        user_id = int(user_raw) if user_raw else None
    except ValueError:
        user_id = None
    return NxmLink(raw, domain, mod_id, file_id, key, expires, user_id)


def _emit(payload: dict[str, Any]) -> None:
    print(JSON_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _is_premium(user: dict[str, Any]) -> bool:
    for key in ("is_premium", "is_premium?", "isPremium", "premium"):
        if key in user:
            value = user.get(key)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "premium"}
            return bool(value)
    return False


def _user_id(user: dict[str, Any]) -> int | None:
    for key in ("user_id", "userId", "id"):
        value = user.get(key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    return None


def _safe_filename(name: str, mod_id: int, file_id: int) -> str:
    base = Path(str(name or "")).name.strip()
    base = _SAFE_NAME_RE.sub("_", base).strip(" .")
    if not base:
        base = f"Nexus-{mod_id}-{file_id}.zip"
    suffix = Path(base).suffix.lower()
    if suffix not in _SUPPORTED_ARCHIVE_SUFFIXES:
        # Nexus mod managers should receive archive files. Keep a useful name if
        # metadata is odd, while making the staged package obviously an archive.
        base = f"{base}.zip"
    return base


def _ensure_schema(store: SD.Store) -> None:
    with store.conn:
        store.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS download_jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_domain TEXT NOT NULL,
                mod_id INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                file_name TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                bytes_done INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                part_rel TEXT NOT NULL DEFAULT '',
                final_rel TEXT NOT NULL DEFAULT '',
                nxm_key TEXT NOT NULL DEFAULT '',
                nxm_expires INTEGER NOT NULL DEFAULT 0,
                nxm_user_id INTEGER,
                archive_id INTEGER,
                error TEXT NOT NULL DEFAULT '',
                added_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                completed_at REAL,
                worker_pid INTEGER,
                install_after INTEGER NOT NULL DEFAULT 0,
                install_status TEXT NOT NULL DEFAULT '',
                install_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS download_install_intents(
                game_domain TEXT NOT NULL,
                mod_id INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(game_domain,mod_id,file_id)
            );
            CREATE INDEX IF NOT EXISTS download_jobs_status_idx ON download_jobs(status);
            CREATE INDEX IF NOT EXISTS download_jobs_file_idx ON download_jobs(game_domain,mod_id,file_id);
            """
        )
        columns = {str(r[1]) for r in store.conn.execute("PRAGMA table_info(download_jobs)").fetchall()}
        if "worker_pid" not in columns:
            store.conn.execute("ALTER TABLE download_jobs ADD COLUMN worker_pid INTEGER")
        if "install_after" not in columns:
            store.conn.execute("ALTER TABLE download_jobs ADD COLUMN install_after INTEGER NOT NULL DEFAULT 0")
        if "install_status" not in columns:
            store.conn.execute("ALTER TABLE download_jobs ADD COLUMN install_status TEXT NOT NULL DEFAULT ''")
        if "install_error" not in columns:
            store.conn.execute("ALTER TABLE download_jobs ADD COLUMN install_error TEXT NOT NULL DEFAULT ''")


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _recover_stale_jobs(store: SD.Store) -> None:
    rows = store.conn.execute("SELECT id,worker_pid FROM download_jobs WHERE status='downloading'").fetchall()
    stale = [int(r["id"]) for r in rows if not _pid_alive(r["worker_pid"])]
    if stale:
        now = time.time()
        with store.conn:
            store.conn.executemany(
                "UPDATE download_jobs SET status='paused',worker_pid=NULL,updated_at=? WHERE id=?",
                [(now, jid) for jid in stale],
            )


def _row_dict(store: SD.Store, row) -> dict[str, Any]:
    data = dict(row)
    total = int(data.get("total_bytes") or data.get("size") or 0)
    done = int(data.get("bytes_done") or 0)
    data["progress"] = (done / total) if total > 0 else (1.0 if data.get("status") == "complete" else 0.0)
    data["nexus_url"] = f"https://www.nexusmods.com/{data['game_domain']}/mods/{data['mod_id']}?tab=files&file_id={data['file_id']}"
    # Authorization tokens must never be exposed through queue JSON/UI.
    data.pop("nxm_key", None)
    data.pop("nxm_expires", None)
    data.pop("nxm_user_id", None)
    return data


def list_jobs(store: SD.Store, *, limit: int = 500) -> dict[str, Any]:
    _ensure_schema(store)
    _recover_stale_jobs(store)
    rows = store.conn.execute(
        "SELECT * FROM download_jobs ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 5000)),)
    ).fetchall()
    return {"type": "queue", "items": [_row_dict(store, r) for r in rows]}


def _api_file(client: NexusAccountClient, domain: str, mod_id: int, file_id: int) -> dict[str, Any]:
    data = client.request("GET", f"/games/{domain}/mods/{mod_id}/files/{file_id}.json")
    return data if isinstance(data, dict) else {}


def _file_size(meta: dict[str, Any]) -> int:
    for key in ("size_in_bytes", "sizeInBytes", "size"):
        try:
            value = int(meta.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        # v1 historically exposes 'size' in KB in some responses. Prefer the
        # explicit byte field; only use generic size as-is when no better field.
        if value > 0:
            return value
    return 0


def enqueue(
    store: SD.Store,
    *,
    domain: str,
    mod_id: int,
    file_id: int,
    api_key: str = "",
    nxm: NxmLink | None = None,
    install_after: bool = False,
) -> dict[str, Any]:
    _ensure_schema(store)
    domain = str(domain).strip().lower()
    if domain != GAME_DOMAIN:
        raise DownloadManagerError(f"this manager is configured for {GAME_DOMAIN}, not {domain}")
    if mod_id <= 0 or file_id <= 0:
        raise ValueError("mod-id and file-id must be positive")
    if nxm is not None and (nxm.game_domain != domain or nxm.mod_id != mod_id or nxm.file_id != file_id):
        raise ValueError("NXM link identity does not match requested mod/file")
    key = resolve_api_key(api_key)
    meta: dict[str, Any] = {}
    if key:
        try:
            meta = _api_file(NexusAccountClient(key, domain=domain), domain, mod_id, file_id)
        except Exception:
            # Queue creation is useful even while offline; metadata can be refreshed
            # when the worker starts.
            meta = {}
    file_name = _safe_filename(
        str(meta.get("file_name") or meta.get("fileName") or meta.get("name") or f"Nexus-{mod_id}-{file_id}.zip"),
        mod_id, file_id,
    )
    display_name = str(meta.get("name") or file_name)
    version = str(meta.get("version") or "")
    size = _file_size(meta)
    now = time.time()
    with store.conn:
        cur = store.conn.execute(
            """INSERT INTO download_jobs(
                game_domain,mod_id,file_id,file_name,display_name,version,size,status,
                bytes_done,total_bytes,nxm_key,nxm_expires,nxm_user_id,added_at,updated_at,install_after
            ) VALUES(?,?,?,?,?,?,?,'queued',0,?,?,?,?,?,?,?)""",
            (
                domain, int(mod_id), int(file_id), file_name, display_name, version, int(size), int(size),
                nxm.key if nxm else "", int(nxm.expires if nxm else 0), nxm.user_id if nxm else None,
                now, now, 1 if install_after else 0,
            ),
        )
        job_id = int(cur.lastrowid)
    row = store.conn.execute("SELECT * FROM download_jobs WHERE id=?", (job_id,)).fetchone()
    return {"type": "queued", "job": _row_dict(store, row)}


def arm_install_intent(store: SD.Store, mod_id: int, file_id: int, *, domain: str = GAME_DOMAIN) -> dict[str, Any]:
    """Remember that the next matching NXM download should auto-install after completion."""
    _ensure_schema(store)
    now = time.time()
    with store.conn:
        store.conn.execute(
            "INSERT OR REPLACE INTO download_install_intents(game_domain,mod_id,file_id,created_at) VALUES(?,?,?,?)",
            (str(domain).lower(), int(mod_id), int(file_id), now),
        )
        # Expire abandoned intents after a day.
        store.conn.execute("DELETE FROM download_install_intents WHERE created_at<?", (now - 86400,))
    return {"mod_id": int(mod_id), "file_id": int(file_id), "armed": True}


def _consume_install_intent(store: SD.Store, domain: str, mod_id: int, file_id: int) -> bool:
    _ensure_schema(store)
    row = store.conn.execute(
        "SELECT created_at FROM download_install_intents WHERE game_domain=? AND mod_id=? AND file_id=?",
        (str(domain).lower(), int(mod_id), int(file_id)),
    ).fetchone()
    if row is None:
        return False
    with store.conn:
        store.conn.execute(
            "DELETE FROM download_install_intents WHERE game_domain=? AND mod_id=? AND file_id=?",
            (str(domain).lower(), int(mod_id), int(file_id)),
        )
    return True


def enqueue_nxm(store: SD.Store, url: str, *, api_key: str = "") -> dict[str, Any]:
    nxm = parse_nxm(url)
    if nxm.expired:
        raise DownloadManagerError("NXM authorization has expired; click Download with Manager again on Nexus Mods")
    auto_install = _consume_install_intent(store, nxm.game_domain, nxm.mod_id, nxm.file_id)
    return enqueue(
        store, domain=nxm.game_domain, mod_id=nxm.mod_id, file_id=nxm.file_id,
        api_key=api_key, nxm=nxm, install_after=auto_install,
    )


def account_status(api_key: str) -> dict[str, Any]:
    key = resolve_api_key(api_key)
    if not key:
        return {"type": "account", "configured": False, "premium": False}
    user = NexusAccountClient(key).validate()
    return {
        "type": "account",
        "configured": True,
        "premium": _is_premium(user),
        "user_id": _user_id(user),
        "name": str(user.get("name") or ""),
    }


def _download_links(
    client: NexusAccountClient,
    job,
    *,
    user: dict[str, Any],
) -> list[str]:
    domain, mod_id, file_id = str(job["game_domain"]), int(job["mod_id"]), int(job["file_id"])
    path = f"/games/{domain}/mods/{mod_id}/files/{file_id}/download_link.json"
    premium = _is_premium(user)
    if premium:
        data = client.request("GET", path)
    else:
        key = str(job["nxm_key"] or "")
        expires = int(job["nxm_expires"] or 0)
        link_user = job["nxm_user_id"]
        if not key or not expires:
            raise DownloadManagerError(
                "Free Nexus accounts require a fresh 'Download with Manager' (nxm://) link for this file"
            )
        if expires <= int(time.time()):
            raise DownloadManagerError("NXM authorization expired; request a fresh manager download link")
        current_user = _user_id(user)
        if link_user is not None and current_user is not None and int(link_user) != current_user:
            raise DownloadManagerError("NXM link belongs to a different Nexus account")
        query = urllib.parse.urlencode({"key": key, "expires": expires})
        data = client.request("GET", path + "?" + query)
    if not isinstance(data, list):
        raise DownloadManagerError("Nexus download-link API returned an unexpected response")
    urls = [str(item.get("URI")) for item in data if isinstance(item, dict) and item.get("URI")]
    if not urls:
        raise DownloadManagerError("Nexus returned no CDN download URLs")
    return urls


def _refresh_job_metadata(store: SD.Store, job_id: int, client: NexusAccountClient) -> Any:
    row = store.conn.execute("SELECT * FROM download_jobs WHERE id=?", (int(job_id),)).fetchone()
    if row is None:
        raise KeyError(f"download job not found: {job_id}")
    try:
        meta = _api_file(client, str(row["game_domain"]), int(row["mod_id"]), int(row["file_id"]))
    except Exception:
        return row
    name = _safe_filename(
        str(meta.get("file_name") or meta.get("fileName") or row["file_name"]), int(row["mod_id"]), int(row["file_id"])
    )
    display = str(meta.get("name") or row["display_name"] or name)
    version = str(meta.get("version") or row["version"] or "")
    size = _file_size(meta) or int(row["size"] or 0)
    with store.conn:
        store.conn.execute(
            "UPDATE download_jobs SET file_name=?,display_name=?,version=?,size=?,total_bytes=?,updated_at=? WHERE id=?",
            (name, display, version, size, size, time.time(), int(job_id)),
        )
    return store.conn.execute("SELECT * FROM download_jobs WHERE id=?", (int(job_id),)).fetchone()


def _unique_final_path(store: SD.Store, job) -> Path:
    root = AM.archive_root(store.game_root)
    root.mkdir(parents=True, exist_ok=True)
    base = _safe_filename(str(job["file_name"]), int(job["mod_id"]), int(job["file_id"]))
    candidate = root / base
    # If another queued/completed file already claims this name, suffix Nexus file
    # id. This also protects against Nexus's increasingly human-readable filenames.
    other = store.conn.execute(
        "SELECT id FROM download_jobs WHERE id<>? AND final_rel=? LIMIT 1",
        (int(job["id"]), candidate.relative_to(store.root).as_posix()),
    ).fetchone()
    if candidate.exists() or other:
        p = Path(base)
        candidate = root / f"{p.stem}--nexus-{int(job['file_id'])}{p.suffix}"
    return candidate


def _open_stream(url: str, offset: int, timeout: int = 45):
    headers = {
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Accept": "*/*",
    }
    if offset > 0:
        headers["Range"] = f"bytes={offset}-"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _transfer(
    urls: list[str],
    part: Path,
    *,
    expected: int,
    progress: Callable[[int, int, float], None],
    stop_event: threading.Event,
) -> int:
    last_error: Exception | None = None
    for mirror in urls:
        if stop_event.is_set():
            raise KeyboardInterrupt
        offset = part.stat().st_size if part.exists() else 0
        try:
            with _open_stream(mirror, offset) as resp:
                status = getattr(resp, "status", resp.getcode())
                append = offset > 0 and int(status) == 206
                if offset > 0 and not append:
                    offset = 0
                total = expected
                if total <= 0:
                    length = resp.headers.get("Content-Length")
                    try:
                        total = int(length or 0) + (offset if append else 0)
                    except ValueError:
                        total = 0
                mode = "ab" if append else "wb"
                done = offset
                started = time.monotonic()
                last_emit = 0.0
                with part.open(mode) as out:
                    while True:
                        if stop_event.is_set():
                            raise KeyboardInterrupt
                        block = resp.read(1024 * 1024)
                        if not block:
                            break
                        out.write(block)
                        done += len(block)
                        now = time.monotonic()
                        if now - last_emit >= 0.25:
                            elapsed = max(now - started, 1e-6)
                            progress(done, total, max(0.0, (done - offset) / elapsed))
                            last_emit = now
                    out.flush(); os.fsync(out.fileno())
                progress(done, total, 0.0)
                if expected > 0 and done != expected:
                    raise DownloadManagerError(f"download size mismatch: got {done}, expected {expected}")
                return done
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    raise DownloadManagerError(f"all Nexus CDN mirrors failed: {last_error}")



def _auto_install_archive(game_root: Path, archive: Path) -> dict[str, Any]:
    """Install one completed download with authored defaults and conflict=skip."""
    from . import deployer as D
    from . import mod_installation as MI
    D.require_game_idle("automatic Nexus install", False)
    D.require_bsdtar()
    session = MI.create_session(archive)
    try:
        preview = MI.preview_session(session, game_root, {})
        result = MI.apply_session(
            session, game_root, selections={}, resolutions={}, default_conflict="skip", dry_run=False
        )
    finally:
        MI.cleanup_session(session)
    return {"preview_counts": preview.get("counts") or {}, "install": result}

def run_job(store: SD.Store, job_id: int, *, api_key: str = "", stop_event: threading.Event | None = None) -> dict[str, Any]:
    _ensure_schema(store)
    key = resolve_api_key(api_key)
    if not key:
        raise DownloadManagerError("A Nexus Mods API key is required to resolve downloads")
    client = NexusAccountClient(key)
    user = client.validate()
    row = _refresh_job_metadata(store, int(job_id), client)
    link_user = row["nxm_user_id"]
    if link_user is not None and _user_id(user) is not None and int(link_user) != _user_id(user):
        raise DownloadManagerError("NXM link belongs to a different Nexus account")
    final = _unique_final_path(store, row)
    part = final.with_name(final.name + ".part")
    final_rel = final.relative_to(store.root).as_posix()
    part_rel = part.relative_to(store.root).as_posix()
    now = time.time()
    with store.conn:
        store.conn.execute(
            "UPDATE download_jobs SET status='downloading',error='',started_at=COALESCE(started_at,?),updated_at=?,part_rel=?,final_rel=?,worker_pid=? WHERE id=?",
            (now, now, part_rel, final_rel, os.getpid(), int(job_id)),
        )
    stopper = stop_event or threading.Event()
    last_db = [0.0]

    def progress(done: int, total: int, speed: float) -> None:
        now_m = time.monotonic()
        if now_m - last_db[0] >= 0.5 or (total > 0 and done >= total):
            with store.conn:
                store.conn.execute(
                    "UPDATE download_jobs SET bytes_done=?,total_bytes=?,updated_at=? WHERE id=?",
                    (int(done), int(total), time.time(), int(job_id)),
                )
            last_db[0] = now_m
        _emit({
            "type": "progress", "job_id": int(job_id), "bytes_done": int(done), "total_bytes": int(total),
            "speed_bps": int(speed), "progress": (done / total if total > 0 else 0.0),
        })

    try:
        urls = _download_links(client, row, user=user)
        done = _transfer(urls, part, expected=int(row["size"] or 0), progress=progress, stop_event=stopper)
        os.replace(part, final)
        # Archive-library mutation is brief; hold the deployment lock only for
        # this publish/index step so GUI queue readers can observe progress while
        # the network transfer is running.
        with SD.deployment_lock(store.game_root, exclusive=True):
            detail = AM.import_archive(store, final)
            detail = AM.link_nexus(store, int(detail["id"]), int(row["mod_id"]), nexus_file_id=int(row["file_id"]))
        with store.conn:
            store.conn.execute(
                """UPDATE download_jobs SET status='complete',bytes_done=?,total_bytes=?,archive_id=?,
                   nxm_key='',nxm_expires=0,nxm_user_id=NULL,error='',completed_at=?,updated_at=?,worker_pid=NULL WHERE id=?""",
                (done, done, int(detail["id"]), time.time(), time.time(), int(job_id)),
            )
        install_result = None
        if bool(row["install_after"]):
            try:
                with store.conn:
                    store.conn.execute(
                        "UPDATE download_jobs SET install_status='installing',install_error='',updated_at=? WHERE id=?",
                        (time.time(), int(job_id)),
                    )
                install_result = _auto_install_archive(store.game_root, final)
                with store.conn:
                    store.conn.execute(
                        "UPDATE download_jobs SET install_status='installed',install_error='',updated_at=? WHERE id=?",
                        (time.time(), int(job_id)),
                    )
            except Exception as exc:  # download remains valid even when install needs attention
                with store.conn:
                    store.conn.execute(
                        "UPDATE download_jobs SET install_status='failed',install_error=?,updated_at=? WHERE id=?",
                        (str(exc), time.time(), int(job_id)),
                    )
                _emit({"type": "install-error", "job_id": int(job_id), "message": str(exc)})
        current = store.conn.execute("SELECT * FROM download_jobs WHERE id=?", (int(job_id),)).fetchone()
        result = {"type": "complete", "job": _row_dict(store, current), "archive": detail}
        if install_result is not None:
            result["auto_install"] = install_result
        _emit(result)
        return result
    except KeyboardInterrupt:
        current = part.stat().st_size if part.exists() else 0
        with store.conn:
            store.conn.execute(
                "UPDATE download_jobs SET status='paused',bytes_done=?,updated_at=?,worker_pid=NULL WHERE id=?",
                (current, time.time(), int(job_id)),
            )
        raise
    except Exception as exc:
        current = part.stat().st_size if part.exists() else 0
        with store.conn:
            store.conn.execute(
                "UPDATE download_jobs SET status='failed',bytes_done=?,error=?,updated_at=?,worker_pid=NULL WHERE id=?",
                (current, str(exc), time.time(), int(job_id)),
            )
        raise


def run_queue(store: SD.Store, *, api_key: str = "", job_id: int | None = None) -> dict[str, Any]:
    _ensure_schema(store)
    _recover_stale_jobs(store)
    if job_id is not None:
        ids = [int(job_id)]
    else:
        ids = [int(r[0]) for r in store.conn.execute(
            "SELECT id FROM download_jobs WHERE status IN ('queued','paused','failed') ORDER BY id"
        ).fetchall()]
    stop = threading.Event()
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)

    def _stop(_sig, _frame):
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    completed = 0; failed = 0
    try:
        for jid in ids:
            if stop.is_set():
                break
            try:
                run_job(store, jid, api_key=api_key, stop_event=stop)
                completed += 1
            except KeyboardInterrupt:
                break
            except Exception as exc:  # noqa: BLE001
                failed += 1
                _emit({"type": "error", "job_id": jid, "message": str(exc)})
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    result = {"type": "run-finished", "completed": completed, "failed": failed, "stopped": stop.is_set()}
    _emit(result)
    return result


def retry_job(store: SD.Store, job_id: int) -> dict[str, Any]:
    row = store.conn.execute("SELECT * FROM download_jobs WHERE id=?", (int(job_id),)).fetchone()
    if row is None:
        raise KeyError(f"download job not found: {job_id}")
    if str(row["status"]) == "complete":
        return {"type": "job", "job": _row_dict(store, row)}
    with store.conn:
        store.conn.execute("UPDATE download_jobs SET status='queued',error='',updated_at=? WHERE id=?", (time.time(), int(job_id)))
    row = store.conn.execute("SELECT * FROM download_jobs WHERE id=?", (int(job_id),)).fetchone()
    return {"type": "job", "job": _row_dict(store, row)}


def remove_job(store: SD.Store, job_id: int, *, delete_partial: bool = False) -> dict[str, Any]:
    row = store.conn.execute("SELECT * FROM download_jobs WHERE id=?", (int(job_id),)).fetchone()
    if row is None:
        raise KeyError(f"download job not found: {job_id}")
    if str(row["status"]) == "downloading":
        raise DownloadManagerError("stop the active download before removing it from the queue")
    if delete_partial and row["part_rel"]:
        part = (store.root / str(row["part_rel"])).resolve(strict=False)
        try:
            part.relative_to(AM.archive_root(store.game_root).resolve())
        except ValueError as exc:
            raise DownloadManagerError("partial download path escapes archive staging") from exc
        part.unlink(missing_ok=True)
    with store.conn:
        store.conn.execute("DELETE FROM download_jobs WHERE id=?", (int(job_id),))
    return {"type": "removed", "job_id": int(job_id), "partial_deleted": bool(delete_partial)}


def register_nxm_handler(game_root: Path) -> dict[str, Any]:
    root = game_root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    HANDLER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    HANDLER_CONFIG.write_text(json.dumps({"game_root": str(root)}, indent=2), encoding="utf-8")
    DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Prefer opening the Qt app so the browser deep-link lands in the visible
    # queue and can use the API key already stored in QSettings.  The backend
    # handle-nxm command remains available for headless use.
    executable = shutil.which("nexus-collection-manager")
    if executable:
        command = f'"{executable}" %u'
    else:
        command = f'"{sys.executable}" -m main %u'
    desktop = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Nexus Collection Manager NXM Handler\n"
        f"Exec={command}\n"
        "NoDisplay=true\n"
        "Terminal=false\n"
        "MimeType=x-scheme-handler/nxm;\n"
    )
    DESKTOP_FILE.write_text(desktop, encoding="utf-8")
    commands: list[list[str]] = []
    if shutil.which("update-desktop-database"):
        commands.append(["update-desktop-database", str(DESKTOP_FILE.parent)])
    if shutil.which("xdg-mime"):
        commands.append(["xdg-mime", "default", DESKTOP_FILE.name, "x-scheme-handler/nxm"])
    for cmd in commands:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return {"type": "nxm-handler", "registered": True, "desktop_file": str(DESKTOP_FILE), "game_root": str(root)}


def _handler_root() -> Path:
    try:
        data = json.loads(HANDLER_CONFIG.read_text(encoding="utf-8"))
        root = Path(str(data.get("game_root") or "")).expanduser().resolve()
    except (OSError, ValueError, TypeError):
        raise DownloadManagerError("NXM handler is not configured; register it from the Download Manager first")
    if not root.is_dir():
        raise DownloadManagerError(f"configured NXM game root no longer exists: {root}")
    return root


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Persistent Nexus download manager with Premium + NXM support")
    sub = ap.add_subparsers(dest="command", required=True)

    def root_cmd(name: str, help_text: str):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--game-root", type=Path, required=True)
        p.add_argument("--api-key", default="", help=argparse.SUPPRESS)
        return p

    root_cmd("account", "Validate configured Nexus account / Premium status")
    p = root_cmd("add", "Queue a Nexus mod file by IDs")
    p.add_argument("--mod-id", type=int, required=True)
    p.add_argument("--file-id", type=int, required=True)
    p.add_argument("--nxm", default="", help="optional manager-authorized nxm:// URL")
    p = root_cmd("nxm", "Queue a Download-with-Manager nxm:// link")
    p.add_argument("url")
    p = root_cmd("queue", "List persistent downloads")
    p.add_argument("--limit", type=int, default=500)
    p = root_cmd("run", "Run queued/paused/failed downloads")
    p.add_argument("--job-id", type=int)
    p = root_cmd("retry", "Put one failed/paused job back in the queue")
    p.add_argument("--job-id", type=int, required=True)
    p = root_cmd("remove", "Remove one non-running queue item")
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--delete-partial", action="store_true")
    p = root_cmd("register-nxm", "Register this Linux app as x-scheme-handler/nxm")
    p = sub.add_parser("handle-nxm", help="Desktop-handler entry point for an nxm:// URL")
    p.add_argument("url")
    p.add_argument("--game-root", type=Path)
    p.add_argument("--api-key", default="", help=argparse.SUPPRESS)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "handle-nxm":
            root = args.game_root.expanduser().resolve() if args.game_root else _handler_root()
        else:
            root = args.game_root.expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"game root not found: {root}")
        api_key = resolve_api_key(getattr(args, "api_key", ""))
        # Download transfers deliberately do not hold the global deployment flock:
        # queue readers need to observe progress and SQLite WAL safely serializes
        # these small state updates.  run_job takes the flock only while publishing
        # the completed package into Archive Management.
        with SD.Store(root) as store:
            _ensure_schema(store)
            if args.command == "account":
                result = account_status(api_key)
                _emit(result)
            elif args.command == "add":
                nxm = parse_nxm(args.nxm) if args.nxm else None
                result = enqueue(store, domain=GAME_DOMAIN, mod_id=args.mod_id, file_id=args.file_id, api_key=api_key, nxm=nxm)
                _emit(result)
            elif args.command == "nxm":
                result = enqueue_nxm(store, args.url, api_key=api_key); _emit(result)
            elif args.command == "queue":
                result = list_jobs(store, limit=args.limit); _emit(result)
            elif args.command == "run":
                run_queue(store, api_key=api_key, job_id=args.job_id)
            elif args.command == "retry":
                result = retry_job(store, args.job_id); _emit(result)
            elif args.command == "remove":
                result = remove_job(store, args.job_id, delete_partial=args.delete_partial); _emit(result)
            elif args.command == "register-nxm":
                result = register_nxm_handler(root); _emit(result)
            elif args.command == "handle-nxm":
                queued = enqueue_nxm(store, args.url, api_key=api_key); _emit(queued)
                if api_key:
                    run_queue(store, api_key=api_key, job_id=int(queued["job"]["id"]))
                else:
                    _emit({"type": "waiting-auth", "job_id": int(queued["job"]["id"]),
                           "message": "Queued NXM download; configure a Nexus API key in the app to start it."})
            else:
                raise AssertionError(args.command)
        return 0
    except KeyboardInterrupt:
        return 130
    except NexusAPIError as exc:
        _emit({"type": "error", "message": str(exc), "status": exc.status})
        print(f"[!] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        _emit({"type": "error", "message": str(exc)})
        print(f"[!] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
