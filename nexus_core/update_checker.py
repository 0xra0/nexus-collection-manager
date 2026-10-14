#!/usr/bin/env python3
"""Profile-aware Nexus mod update checker for the smart staging library.

The checker treats a locally installed Nexus *file* as the unit of update
identity.  When Archive Management has a confirmed Nexus file id we follow
Nexus' file-update lineage rather than comparing against an unrelated newest
file on the same mod page.  Older/manual installs fall back to MD5 reverse
lookup and, last, a conservative version match.

Downloading an update is delegated to Download Manager, so Premium accounts
can fetch it directly and free accounts are sent through Nexus' required NXM
"Download with Manager" authorization flow.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from . import deployer as D
from . import download_manager as DM
from . import smart_deployment as SD
from .nexus_account import GAME_DOMAIN, NexusAPIError, NexusAccountClient, resolve_api_key

APP_VERSION = "0.17.6"
JSON_PREFIX = "[updates-json] "


class ModUpdateError(RuntimeError):
    pass


def _emit(payload: dict[str, Any]) -> None:
    print(JSON_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _norm_version(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.removeprefix("version").strip().lstrip("v")
    return re.sub(r"[^0-9a-z]+", ".", text).strip(".")


def _file_id(row: dict[str, Any]) -> int:
    return _int(row.get("file_id") or row.get("fileId") or row.get("id"))


def _file_version(row: dict[str, Any]) -> str:
    return str(row.get("version") or "").strip()


def _file_name(row: dict[str, Any]) -> str:
    return str(row.get("file_name") or row.get("fileName") or row.get("name") or "").strip()


def _file_time(row: dict[str, Any]) -> int:
    return _int(row.get("uploaded_timestamp") or row.get("uploadedTimestamp"))


def _file_size(row: dict[str, Any]) -> int:
    exact = _int(row.get("size_in_bytes") or row.get("sizeInBytes"))
    if exact > 0:
        return exact
    kib = _int(row.get("size_kb") or row.get("sizeKb") or row.get("size"))
    return kib * 1024 if kib > 0 else 0


def _file_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": _file_id(row),
        "name": str(row.get("name") or _file_name(row)),
        "file_name": _file_name(row),
        "version": _file_version(row),
        "category": str(row.get("category_name") or row.get("categoryName") or row.get("category") or ""),
        "uploaded_timestamp": _file_time(row),
        "size": _file_size(row),
    }


def _parse_filename_identity(file_name: str) -> tuple[int | None, str]:
    stem = Path(str(file_name or "")).stem
    parsed = D.nexus_key_from_stem(stem)
    if not parsed:
        return None, ""
    _name, mod_id, _epoch, version = parsed
    try:
        return int(mod_id), str(version or "")
    except (TypeError, ValueError):
        return None, str(version or "")


def _latest_installed_archive(store: SD.Store, managed_mod_id: int):
    """Return the newest package actually associated with this staged revision.

    ``archive_installs`` records packages used as install sources.  A package
    merely imported or Nexus-linked but never installed is intentionally not a
    source of local-version truth.
    """
    return store.conn.execute(
        "SELECT al.*,ai.linked_at FROM archive_installs ai "
        "JOIN archive_library al ON al.id=ai.archive_id "
        "WHERE ai.mod_id=? ORDER BY ai.linked_at DESC,al.id DESC LIMIT 1",
        (int(managed_mod_id),),
    ).fetchone()


def local_mods(store: SD.Store) -> list[dict[str, Any]]:
    """Return smart-staged mods with their best-known Nexus identity.

    This is intentionally read-only.  Correlation/link decisions are never
    fabricated by the update checker; unlinked mods remain visible as such.
    """
    profile = store.active_profile
    rows = store.conn.execute(
        "SELECT m.id,m.name,m.source_archive,m.installed_at,m.enabled_default,"
        "COALESCE(pm.enabled,m.enabled_default) enabled "
        "FROM mods m LEFT JOIN profile_mods pm ON pm.mod_id=m.id AND pm.profile=? "
        "ORDER BY m.name COLLATE NOCASE",
        (profile,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        archive = _latest_installed_archive(store, int(row["id"]))
        nexus_mod_id: int | None = None
        nexus_file_id: int | None = None
        version = ""
        identity = "unlinked"
        archive_id = None
        archive_name = ""
        archive_md5 = ""
        if archive is not None:
            archive_id = int(archive["id"])
            archive_name = str(archive["file_name"] or "")
            archive_md5 = str(archive["md5"] or "")
            if archive["nexus_mod_id"] is not None:
                nexus_mod_id = int(archive["nexus_mod_id"])
                identity = "archive-link"
            if archive["nexus_file_id"] is not None:
                nexus_file_id = int(archive["nexus_file_id"])
                identity = "archive-file-link"
            parsed_mod, parsed_ver = _parse_filename_identity(archive_name)
            if nexus_mod_id is None and parsed_mod is not None:
                nexus_mod_id = parsed_mod
                identity = "filename"
            if parsed_ver:
                version = parsed_ver
        if nexus_mod_id is None:
            parsed_mod, parsed_ver = _parse_filename_identity(str(row["name"]))
            if parsed_mod is not None:
                nexus_mod_id = parsed_mod
                identity = "staged-name"
            if not version and parsed_ver:
                version = parsed_ver
        out.append({
            "managed_mod_id": int(row["id"]),
            "name": str(row["name"]),
            "enabled": bool(row["enabled"]),
            "profile": profile,
            "installed_at": float(row["installed_at"]),
            "source_archive": str(row["source_archive"] or ""),
            "archive_id": archive_id,
            "archive_name": archive_name,
            "archive_md5": archive_md5,
            "nexus_mod_id": nexus_mod_id,
            "nexus_file_id": nexus_file_id,
            "installed_version": version,
            "identity_source": identity,
        })
    return out


def _files_payload(client: NexusAccountClient, mod_id: int) -> dict[str, Any]:
    data = client.request("GET", f"/games/{client.domain}/mods/{int(mod_id)}/files.json")
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"files": data, "file_updates": []}
    raise ModUpdateError(f"Nexus returned an unexpected file list for mod #{mod_id}")


def _md5_match(client: NexusAccountClient, md5: str, wanted_mod_id: int | None) -> dict[str, Any] | None:
    if not md5:
        return None
    try:
        data = client.request("GET", f"/games/{client.domain}/mods/md5_search/{md5}.json")
    except NexusAPIError as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(data, list):
        return None
    best = None
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mod = entry.get("mod") if isinstance(entry.get("mod"), dict) else {}
        details = entry.get("file_details") if isinstance(entry.get("file_details"), dict) else {}
        mid = _int(mod.get("mod_id") or mod.get("modId"))
        if wanted_mod_id and mid == int(wanted_mod_id):
            return details
        if best is None:
            best = details
    return best if not wanted_mod_id else None


def _by_version(files: Iterable[dict[str, Any]], version: str) -> dict[str, Any] | None:
    want = _norm_version(version)
    if not want:
        return None
    candidates = [x for x in files if _norm_version(_file_version(x)) == want]
    if not candidates:
        return None
    # Archived/OLD_VERSION entries can coexist with a current entry carrying the
    # same human version string.  Prefer a visible/current category, then newest.
    candidates.sort(
        key=lambda x: (
            0 if str(x.get("category_name") or "").upper() == "OLD_VERSION" else 1,
            _file_time(x),
            _file_id(x),
        ),
        reverse=True,
    )
    return candidates[0]


def _successor_id(installed_id: int, updates: Iterable[dict[str, Any]]) -> int:
    nxt: dict[int, int] = {}
    for row in updates:
        if not isinstance(row, dict):
            continue
        old = _int(row.get("old_file_id") or row.get("oldFileId"))
        new = _int(row.get("new_file_id") or row.get("newFileId"))
        if old > 0 and new > 0:
            nxt[old] = new
    cur = int(installed_id)
    seen: set[int] = set()
    while cur in nxt and cur not in seen:
        seen.add(cur)
        cur = nxt[cur]
    return cur


def _resolve_installed_file(
    client: NexusAccountClient,
    local: dict[str, Any],
    files: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    by_id = {_file_id(row): row for row in files if _file_id(row) > 0}
    explicit_id = _int(local.get("nexus_file_id"))
    if explicit_id:
        if explicit_id in by_id:
            return by_id[explicit_id], "nexus-file-id"
        try:
            exact = client.request(
                "GET",
                f"/games/{client.domain}/mods/{_int(local.get('nexus_mod_id'))}/files/{explicit_id}.json",
            )
            if isinstance(exact, dict) and _file_id(exact) == explicit_id:
                return exact, "nexus-file-id"
        except NexusAPIError as exc:
            if exc.status != 404:
                raise

    md5 = str(local.get("archive_md5") or "")
    if md5:
        details = _md5_match(client, md5, _int(local.get("nexus_mod_id")) or None)
        match_id = _file_id(details or {})
        if match_id and match_id in by_id:
            return by_id[match_id], "md5"

    version = str(local.get("installed_version") or "")
    match = _by_version(files, version)
    if match is not None:
        return match, "version"
    return None, ""


def check_one(client: NexusAccountClient, local: dict[str, Any], *, cache: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    row = dict(local)
    mod_id = _int(row.get("nexus_mod_id"))
    base = {
        **row,
        "status": "unlinked" if not mod_id else "unknown",
        "reason": "",
        "installed_file": None,
        "latest_file": None,
        "latest_version": "",
        "nexus_url": f"https://www.nexusmods.com/{client.domain}/mods/{mod_id}" if mod_id else "",
        "download_url": "",
        "match_source": "",
    }
    if not mod_id:
        base["reason"] = "No confirmed Nexus mod identity. Link its source archive or use Auto-Correlation first."
        return base

    cache = cache if cache is not None else {}
    if mod_id not in cache:
        cache[mod_id] = _files_payload(client, mod_id)
    payload = cache[mod_id]
    files = [x for x in payload.get("files", []) if isinstance(x, dict)]
    installed, source = _resolve_installed_file(client, row, files)
    if installed is None:
        base["reason"] = "Could not identify the installed Nexus file from file id, MD5, or version metadata."
        return base

    installed_id = _file_id(installed)
    by_id = {_file_id(x): x for x in files if _file_id(x) > 0}
    latest_id = _successor_id(installed_id, payload.get("file_updates", []) or [])
    latest = by_id.get(latest_id)
    if latest is None and latest_id != installed_id:
        try:
            fetched = client.request(
                "GET", f"/games/{client.domain}/mods/{mod_id}/files/{latest_id}.json"
            )
            latest = fetched if isinstance(fetched, dict) else None
        except NexusAPIError:
            latest = None
    latest = latest or (installed if latest_id == installed_id else {"file_id": latest_id})
    base["match_source"] = source
    base["installed_file"] = _file_view(installed)
    base["installed_version"] = _file_version(installed) or str(row.get("installed_version") or "")
    base["latest_file"] = _file_view(latest)
    base["latest_version"] = _file_version(latest)
    base["nexus_file_id"] = installed_id
    if latest_id != installed_id:
        base["status"] = "update"
        base["reason"] = "Nexus marks this file as having a newer successor in the same file lineage."
        base["download_url"] = (
            f"https://www.nexusmods.com/{client.domain}/mods/{mod_id}?tab=files&file_id={latest_id}"
        )
    else:
        base["status"] = "current"
        base["reason"] = "Installed Nexus file is the newest file in its update lineage."
    return base


def check_updates(store: SD.Store, client: NexusAccountClient, *, enabled_only: bool = False) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    cache: dict[int, dict[str, Any]] = {}
    for local in local_mods(store):
        if enabled_only and not local["enabled"]:
            continue
        try:
            items.append(check_one(client, local, cache=cache))
        except (NexusAPIError, ModUpdateError) as exc:
            failed = dict(local)
            mid = _int(local.get("nexus_mod_id"))
            failed.update({
                "status": "error",
                "reason": str(exc),
                "installed_file": None,
                "latest_file": None,
                "latest_version": "",
                "nexus_url": f"https://www.nexusmods.com/{client.domain}/mods/{mid}" if mid else "",
                "download_url": "",
                "match_source": "",
            })
            items.append(failed)
            warnings.append(f"{local['name']}: {exc}")
    rank = {"update": 0, "unknown": 1, "unlinked": 2, "error": 3, "current": 4}
    items.sort(key=lambda x: (rank.get(str(x.get("status")), 9), str(x.get("name", "")).casefold()))
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "type": "check",
        "profile": store.active_profile,
        "checked_at": int(time.time()),
        "items": items,
        "counts": counts,
        "updates": counts.get("update", 0),
        "total": len(items),
        "warnings": warnings,
    }


def _find_update(result: dict[str, Any], *, managed_mod_id: int | None = None,
                 mod_name: str = "", nexus_mod_id: int | None = None) -> dict[str, Any]:
    rows = [x for x in result.get("items", []) if x.get("status") == "update"]
    if managed_mod_id is not None:
        rows = [x for x in rows if _int(x.get("managed_mod_id")) == int(managed_mod_id)]
    if mod_name:
        rows = [x for x in rows if str(x.get("name")) == str(mod_name)]
    if nexus_mod_id is not None:
        rows = [x for x in rows if _int(x.get("nexus_mod_id")) == int(nexus_mod_id)]
    if len(rows) != 1:
        if not rows:
            raise ModUpdateError("No matching available update was found; run check again first.")
        raise ModUpdateError("Update selector is ambiguous; select the managed mod by id/name.")
    return rows[0]


def download_update(
    store: SD.Store,
    client: NexusAccountClient,
    *,
    api_key: str,
    managed_mod_id: int | None = None,
    mod_name: str = "",
    nexus_mod_id: int | None = None,
) -> dict[str, Any]:
    """Download one confirmed update through the existing persistent queue.

    Premium users can finish directly.  Free users receive a Nexus Files URL
    because Nexus requires a fresh NXM authorization token for the selected file.
    The incoming NXM URL is handled by the already-registered Download Manager.
    """
    checked = check_updates(store, client)
    update = _find_update(
        checked, managed_mod_id=managed_mod_id, mod_name=mod_name, nexus_mod_id=nexus_mod_id,
    )
    latest = update.get("latest_file") or {}
    file_id = _int(latest.get("file_id"))
    mod_id = _int(update.get("nexus_mod_id"))
    if not mod_id or not file_id:
        raise ModUpdateError("Update has no downloadable Nexus file identity.")

    user = client.validate()
    if not DM._is_premium(user):
        return {
            "type": "requires-nxm",
            "mod": update,
            "nexus_url": update.get("download_url") or update.get("nexus_url"),
            "message": "Free Nexus accounts require Download with Manager authorization for this update.",
        }

    queued = DM.enqueue(
        store,
        domain=client.domain,
        mod_id=mod_id,
        file_id=file_id,
        api_key=api_key,
        install_after=False,
    )
    job_id = _int((queued.get("job") or {}).get("id"))
    if not job_id:
        raise ModUpdateError("Download Manager did not create a queue job.")
    downloaded = DM.run_job(store, job_id, api_key=api_key)
    return {
        "type": "downloaded",
        "mod": update,
        "job": downloaded.get("job") or {},
        "archive": downloaded.get("archive") or {},
    }


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Check smart-staged Cyberpunk 2077 mods for Nexus updates")
    sub = ap.add_subparsers(dest="command", required=True)

    def root_cmd(name: str, help_text: str):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--game-root", type=Path, required=True)
        p.add_argument("--api-key", default="", help=argparse.SUPPRESS)
        return p

    p = root_cmd("check", "Compare local staged Nexus files with their Nexus update lineage")
    p.add_argument("--enabled-only", action="store_true")

    p = root_cmd("download", "Download one available update through Download Manager")
    selector = p.add_mutually_exclusive_group(required=True)
    selector.add_argument("--managed-mod-id", type=int)
    selector.add_argument("--mod-name", default="")
    selector.add_argument("--nexus-mod-id", type=int)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = args.game_root.expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"game root not found: {root}")
        key = resolve_api_key(getattr(args, "api_key", ""))
        if not key:
            raise ModUpdateError("A Nexus Mods API key is required.")
        client = NexusAccountClient(key)
        with SD.Store(root, create=False) as store:
            if args.command == "check":
                result = check_updates(store, client, enabled_only=args.enabled_only)
            elif args.command == "download":
                result = download_update(
                    store,
                    client,
                    api_key=key,
                    managed_mod_id=args.managed_mod_id,
                    mod_name=args.mod_name,
                    nexus_mod_id=args.nexus_mod_id,
                )
            else:
                raise AssertionError(args.command)
        _emit(result)
        return 0
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
