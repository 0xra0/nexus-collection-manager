#!/usr/bin/env python3
"""Nexus discovery/account feeds with safe one-click Premium installation.

Trending/recently-updated discovery intentionally reuses the existing Nexus v1
client so account state, request headers and error handling stay consistent with
Endorse/Track and Download Manager.  Installation reuses the persistent download
queue, Archive Management and the preview-first installer; it never writes an
independent copy path.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from . import deployer as D
from . import download_manager as DM
from . import mod_installation as MI
from . import smart_deployment as SD
from .nexus_account import (
    GAME_DOMAIN,
    NexusAPIError,
    NexusAccountClient,
    account_entries_from_payload,
    mod_ids_from_account_payload,
    resolve_api_key,
)
from .nexus_graphql import NexusGraphQLClient, NexusGraphQLError

APP_VERSION = "0.17.6"
JSON_PREFIX = "[trending-json] "


class TrendingModsError(RuntimeError):
    pass


def _emit(payload: dict[str, Any]) -> None:
    print(JSON_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _first(data: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = data.get(name)
        if value is not None:
            return value
    return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            return 0
    return 0


def normalize_mod(row: dict[str, Any], *, endorsed: set[str] | None = None,
                  tracked: set[str] | None = None) -> dict[str, Any]:
    """Normalize legacy Nexus feed rows to one stable GUI/API shape."""
    mod_id = _int(_first(row, "mod_id", "modId", "id"))
    mid = str(mod_id) if mod_id else ""
    updated = _timestamp(_first(row, "updated_timestamp", "updatedTimestamp", "updated_at", "updatedAt"))
    created = _timestamp(_first(row, "created_timestamp", "createdTimestamp", "created_at", "createdAt"))
    return {
        "mod_id": mod_id,
        "name": str(_first(row, "name", default=f"Nexus mod {mod_id}") or f"Nexus mod {mod_id}"),
        "summary": str(_first(row, "summary", default="") or ""),
        "version": str(_first(row, "version", default="") or ""),
        "author": str(_first(row, "author", "uploaded_by", "uploadedBy", default="") or ""),
        "picture_url": str(_first(row, "picture_url", "pictureUrl", default="") or ""),
        "endorsements": _int(_first(row, "endorsement_count", "endorsements")),
        "downloads": _int(_first(row, "unique_downloads", "uniqueDownloads", "mod_downloads", "downloads")),
        "category": str(_first(row, "category_name", "category", default="") or ""),
        "created_timestamp": created,
        "updated_timestamp": updated,
        "endorsed": (mid in endorsed) if endorsed is not None and mid else None,
        "tracked": (mid in tracked) if tracked is not None and mid else None,
        "nexus_url": f"https://www.nexusmods.com/{GAME_DOMAIN}/mods/{mod_id}" if mod_id else "",
    }


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("mods", "data", "results", "nodes"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def account_sets(client: NexusAccountClient) -> tuple[set[str], set[str]]:
    tracked = mod_ids_from_account_payload(client.tracked(), client.domain)
    endorsed = mod_ids_from_account_payload(client.endorsements(), client.domain, endorsements=True)
    return tracked, endorsed


def _embedded_account_mod(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    nested = entry.get("mod")
    if isinstance(nested, dict):
        merged = dict(entry)
        merged.update(nested)
        return merged
    return dict(entry)


def _account_feed(client: NexusAccountClient, kind: str) -> dict[str, Any]:
    """Hydrate the user's endorsed/tracked membership into discovery-card rows.

    Membership comes from the authenticated v1 account list.  Basic mod metadata is
    hydrated in batches through GraphQL when an API key is available, then falls back
    to embedded list metadata or REST details.  Per-mod metadata failures are fail-soft
    so one deleted/hidden mod cannot make the entire account list disappear.
    """
    is_endorsed = kind == "endorsed"
    primary_payload = client.endorsements() if is_endorsed else client.tracked()
    entries = account_entries_from_payload(primary_payload, client.domain, endorsements=is_endorsed)
    ids: list[str] = []
    embedded: dict[str, dict[str, Any]] = {}
    for entry in entries:
        mid_set = mod_ids_from_account_payload([entry], client.domain, endorsements=is_endorsed)
        if not mid_set:
            continue
        mid = next(iter(mid_set))
        ids.append(mid)
        embedded[mid] = _embedded_account_mod(entry)

    errors: list[str] = []
    if is_endorsed:
        endorsed: set[str] | None = set(ids)
        try:
            tracked: set[str] | None = mod_ids_from_account_payload(client.tracked(), client.domain)
        except NexusAPIError as exc:
            tracked = None
            errors.append(f"tracked state: {exc}")
    else:
        tracked = set(ids)
        try:
            endorsed = mod_ids_from_account_payload(client.endorsements(), client.domain, endorsements=True)
        except NexusAPIError as exc:
            endorsed = None
            errors.append(f"endorsement state: {exc}")

    metadata: dict[int, dict[str, Any]] = {}
    api_key = str(getattr(client, "api_key", "") or "").strip()
    if api_key and ids:
        try:
            metadata = NexusGraphQLClient(api_key).batch_mods_basic(client.domain, ids)
        except NexusGraphQLError as exc:
            errors.append(f"metadata batch: {exc}")

    items: list[dict[str, Any]] = []
    for mid in ids:
        row = metadata.get(int(mid)) or embedded.get(mid) or {}
        # Minimal account rows usually only contain the ID/domain.  Fetch one REST detail
        # as a fallback when the batch lookup was unavailable or returned no node.
        if not row.get("name"):
            try:
                detail = client.mod_details(mid)
                if detail:
                    row = detail
            except NexusAPIError as exc:
                errors.append(f"mod #{mid}: {exc}")
        row = dict(row)
        row.setdefault("mod_id", int(mid))
        item = normalize_mod(row, endorsed=endorsed, tracked=tracked)
        item["account_list"] = kind
        items.append(item)

    return {
        "type": "feed",
        "feed": kind,
        "items": items,
        "count": len(items),
        "fetched_at": int(time.time()),
        "warnings": errors,
    }


def get_feed(client: NexusAccountClient, kind: str = "trending") -> dict[str, Any]:
    kind = str(kind).strip().lower().replace("_", "-")
    if kind in {"endorsed", "tracked"}:
        return _account_feed(client, kind)
    if kind == "trending":
        path = f"/games/{client.domain}/mods/trending.json"
    elif kind in {"updated", "recent", "recently-updated"}:
        kind = "updated"
        path = f"/games/{client.domain}/mods/latest_updated.json"
    else:
        raise ValueError("feed must be 'trending', 'updated', 'endorsed', or 'tracked'")
    raw = client.request("GET", path)
    tracked, endorsed = account_sets(client)
    items = [normalize_mod(x, endorsed=endorsed, tracked=tracked) for x in _rows(raw)]
    items = [x for x in items if x["mod_id"] > 0]
    return {"type": "feed", "feed": kind, "items": items, "count": len(items), "fetched_at": int(time.time())}


def _file_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        files = payload.get("files")
        if isinstance(files, list):
            return [x for x in files if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def _file_id(row: dict[str, Any]) -> int:
    return _int(_first(row, "file_id", "fileId", "id"))


def _file_time(row: dict[str, Any]) -> int:
    return _timestamp(_first(row, "uploaded_timestamp", "uploadedTimestamp", "uploaded_at", "uploadedAt"))


def _is_primary(row: dict[str, Any]) -> bool:
    for key in ("is_primary", "isPrimary", "primary"):
        if key in row:
            value = row.get(key)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes"}
            return bool(value)
    return False


def _category(row: dict[str, Any]) -> str:
    return str(_first(row, "category_name", "categoryName", "category", default="") or "").strip().lower()


def _file_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": _file_id(row),
        "name": str(_first(row, "name", "file_name", "fileName", default="") or ""),
        "file_name": str(_first(row, "file_name", "fileName", "name", default="") or ""),
        "version": str(row.get("version") or ""),
        "category": str(_first(row, "category_name", "categoryName", "category", default="") or ""),
        "is_primary": _is_primary(row),
        "uploaded_timestamp": _file_time(row),
        "size": _int(_first(row, "size_in_bytes", "sizeInBytes", "size")),
    }


def list_mod_files(client: NexusAccountClient, mod_id: int) -> list[dict[str, Any]]:
    raw = client.request("GET", f"/games/{client.domain}/mods/{int(mod_id)}/files.json")
    return [_file_view(x) for x in _file_rows(raw) if _file_id(x) > 0]



def _normalize_changelogs(payload: Any) -> list[dict[str, Any]]:
    """Normalize Nexus v1 changelog shapes into ordered version entries."""
    out: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for version, entries in payload.items():
            if isinstance(entries, str):
                items = [entries]
            elif isinstance(entries, (list, tuple)):
                items = [str(x) for x in entries if x is not None]
            elif isinstance(entries, dict):
                items = [str(x) for x in entries.values() if x is not None]
            else:
                items = []
            out.append({"version": str(version), "entries": items})
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            version = str(_first(item, "version", "version_number", "versionNumber", default="") or "")
            raw = _first(item, "changes", "entries", "changelog", default=[])
            if isinstance(raw, str):
                entries = [raw]
            elif isinstance(raw, (list, tuple)):
                entries = [str(x) for x in raw if x is not None]
            else:
                entries = []
            out.append({"version": version, "entries": entries})
    return out


def _normalize_requirements(block: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(block, dict):
        return [], []
    modreq = block.get("modRequirements") if isinstance(block.get("modRequirements"), dict) else {}
    reqblock = modreq.get("nexusRequirements") if isinstance(modreq, dict) else {}
    nodes = reqblock.get("nodes") if isinstance(reqblock, dict) else []
    requirements: list[dict[str, Any]] = []
    for row in nodes or []:
        if not isinstance(row, dict):
            continue
        requirements.append({
            "mod_id": _int(_first(row, "modId", "mod_id")),
            "name": str(_first(row, "modName", "name", default="") or ""),
            "url": str(row.get("url") or ""),
            "notes": str(row.get("notes") or ""),
            "external": bool(row.get("externalRequirement")),
            "game_id": _int(row.get("gameId")),
        })
    dlcs: list[dict[str, Any]] = []
    for row in (modreq.get("dlcRequirements") if isinstance(modreq, dict) else []) or []:
        if not isinstance(row, dict):
            continue
        expansion = row.get("gameExpansion") if isinstance(row.get("gameExpansion"), dict) else {}
        dlcs.append({
            "id": str(expansion.get("id") or ""),
            "name": str(expansion.get("name") or "Unknown DLC"),
            "notes": str(row.get("notes") or ""),
        })
    return requirements, dlcs


def mod_detail(client: NexusAccountClient, mod_id: int) -> dict[str, Any]:
    """Aggregate full in-app mod details with fail-soft secondary sections."""
    mid = int(mod_id)
    metadata = client.mod_details(mid)
    warnings: list[str] = []

    try:
        files = list_mod_files(client, mid)
    except NexusAPIError as exc:
        files = []
        warnings.append(f"files: {exc}")

    try:
        raw_changelog = client.request("GET", f"/games/{client.domain}/mods/{mid}/changelogs.json")
        changelogs = _normalize_changelogs(raw_changelog)
    except NexusAPIError as exc:
        changelogs = []
        warnings.append(f"changelogs: {exc}")

    requirements: list[dict[str, Any]] = []
    dlc_requirements: list[dict[str, Any]] = []
    api_key = str(getattr(client, "api_key", "") or "").strip()
    if api_key:
        try:
            gql = NexusGraphQLClient(api_key).get_mod(client.domain, mid, requirements=True)
            requirements, dlc_requirements = _normalize_requirements(gql)
        except NexusGraphQLError as exc:
            warnings.append(f"requirements: {exc}")

    endorsed = tracked = None
    try:
        tracked_set, endorsed_set = account_sets(client)
        tracked = str(mid) in tracked_set
        endorsed = str(mid) in endorsed_set
    except NexusAPIError as exc:
        warnings.append(f"account state: {exc}")

    return {
        "type": "detail",
        "mod_id": mid,
        "metadata": metadata,
        "files": files,
        "changelogs": changelogs,
        "requirements": requirements,
        "dlc_requirements": dlc_requirements,
        "endorsed": endorsed,
        "tracked": tracked,
        "nexus_url": f"https://www.nexusmods.com/{client.domain}/mods/{mid}",
        "warnings": warnings,
    }

def choose_install_file(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Choose the safest one-click candidate: primary MAIN, then newest MAIN.

    We deliberately refuse to auto-pick only optional/misc/old-version files.
    Those packages require the user to inspect the Files tab on Nexus instead of
    guessing which variant should be installed.
    """
    files = [dict(x) for x in rows if _int(x.get("file_id")) > 0]
    if not files:
        raise TrendingModsError("This mod has no downloadable files.")
    main = [x for x in files if str(x.get("category") or "").strip().lower() == "main"]
    if not main:
        raise TrendingModsError(
            "No MAIN file is available for safe one-click install; open the Nexus Files tab and choose a file manually."
        )
    primaries = [x for x in main if bool(x.get("is_primary"))]
    pool = primaries or main
    return max(pool, key=lambda x: (_int(x.get("uploaded_timestamp")), _int(x.get("file_id"))))


def install_target(client: NexusAccountClient, mod_id: int) -> dict[str, Any]:
    files = list_mod_files(client, int(mod_id))
    chosen = choose_install_file(files)
    return {
        "type": "install-target",
        "mod_id": int(mod_id),
        "file": chosen,
        "files": files,
        "nexus_url": f"https://www.nexusmods.com/{client.domain}/mods/{int(mod_id)}?tab=files&file_id={chosen['file_id']}",
    }


def one_click_install(game_root: Path, mod_id: int, *, api_key: str = "",
                      file_id: int | None = None) -> dict[str, Any]:
    """Download + install a Nexus MAIN file with conservative defaults.

    Premium can complete the operation directly.  Free accounts receive a
    ``requires_nxm`` response because Nexus requires a fresh Download-with-Manager
    authorization token; bypassing that restriction is intentionally unsupported.
    FOMOD authored defaults are used and pre-existing file conflicts default to
    Skip, matching the installer's safest unattended policy.
    """
    root = game_root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"game root not found: {root}")
    key = resolve_api_key(api_key)
    if not key:
        raise TrendingModsError("A Nexus Mods API key is required.")
    client = NexusAccountClient(key)
    user = client.validate()
    if file_id is None:
        target = install_target(client, int(mod_id))
        selected = target["file"]
        file_id = int(selected["file_id"])
    else:
        selected = next((x for x in list_mod_files(client, int(mod_id)) if int(x["file_id"]) == int(file_id)), None)
        if selected is None:
            raise TrendingModsError(f"Nexus file #{file_id} is not part of mod #{mod_id}.")
        target = {"nexus_url": f"https://www.nexusmods.com/{client.domain}/mods/{int(mod_id)}?tab=files&file_id={int(file_id)}"}

    if not DM._is_premium(user):
        with SD.Store(root) as store:
            DM.arm_install_intent(store, int(mod_id), int(file_id), domain=client.domain)
        return {
            "type": "requires-nxm",
            "mod_id": int(mod_id),
            "file": selected,
            "nexus_url": target["nexus_url"],
            "auto_install_armed": True,
            "message": "Free Nexus accounts require Download with Manager authorization for this file; the matching NXM download is armed to install automatically afterward.",
        }

    # Avoid starting installation while the game/compiler is active.  The
    # downloader itself is safe, but checking up front avoids a large download
    # ending in a surprising late refusal.
    D.require_game_idle("one-click Nexus install", False)
    with SD.Store(root) as store:
        queued = DM.enqueue(
            store, domain=client.domain, mod_id=int(mod_id), file_id=int(file_id), api_key=key
        )
        job_id = int(queued["job"]["id"])
        download = DM.run_job(store, job_id, api_key=key)
        archive = Path(str((download.get("archive") or {}).get("path") or ""))
    if not archive.is_file():
        raise TrendingModsError("Download completed but the staged archive could not be located.")

    D.require_bsdtar()
    session = MI.create_session(archive)
    try:
        preview = MI.preview_session(session, root, {})
        install = MI.apply_session(
            session, root, selections={}, resolutions={}, default_conflict="skip", dry_run=False
        )
    finally:
        MI.cleanup_session(session)
    return {
        "type": "installed",
        "mod_id": int(mod_id),
        "file_id": int(file_id),
        "file": selected,
        "download": download.get("job") or {},
        "install": install,
        "preview_counts": preview.get("counts") or {},
        "nexus_url": f"https://www.nexusmods.com/{client.domain}/mods/{int(mod_id)}",
    }


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Browse Nexus discovery and account mod lists for Cyberpunk 2077")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("feed", help="Fetch trending, updated, endorsed, or tracked mods with account state")
    p.add_argument("--feed", choices=("trending", "updated", "endorsed", "tracked"), default="trending")
    p.add_argument("--api-key", default="", help=argparse.SUPPRESS)

    p = sub.add_parser("files", help="List downloadable files and the recommended MAIN file")
    p.add_argument("--mod-id", type=int, required=True)
    p.add_argument("--api-key", default="", help=argparse.SUPPRESS)

    p = sub.add_parser("detail", help="Fetch full mod details, files, changelogs and requirements")
    p.add_argument("--mod-id", type=int, required=True)
    p.add_argument("--api-key", default="", help=argparse.SUPPRESS)

    p = sub.add_parser("install", help="Download and install the selected/latest MAIN file")
    p.add_argument("--game-root", type=Path, required=True)
    p.add_argument("--mod-id", type=int, required=True)
    p.add_argument("--file-id", type=int)
    p.add_argument("--api-key", default="", help=argparse.SUPPRESS)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        key = resolve_api_key(getattr(args, "api_key", ""))
        if not key:
            raise TrendingModsError("No Nexus API key configured.")
        if args.command == "feed":
            result = get_feed(NexusAccountClient(key), args.feed)
        elif args.command == "files":
            result = install_target(NexusAccountClient(key), args.mod_id)
        elif args.command == "detail":
            result = mod_detail(NexusAccountClient(key), args.mod_id)
        elif args.command == "install":
            result = one_click_install(args.game_root, args.mod_id, api_key=key, file_id=args.file_id)
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
