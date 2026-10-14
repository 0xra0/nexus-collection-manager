#!/usr/bin/env python3
"""Nexus Mods account actions for installed mods.

Provides live endorsement/tracking state and mutations without coupling Qt to HTTP.
The GUI invokes this module as a subprocess, just like the other project backends.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GAME_DOMAIN = "cyberpunk2077"
API_BASE = "https://api.nexusmods.com/v1"
APP_NAME = "Nexus-Collection-Manager"
APP_VERSION = "0.17.6"
CONFIG_FILE = Path.home() / ".config" / "cp2077_modder" / "config.json"


class NexusAPIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _config_api_key() -> str:
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    return str(raw.get("nexus_api_key") or "").strip() if isinstance(raw, dict) else ""


def resolve_api_key(cli_key: str = "") -> str:
    return (cli_key or os.environ.get("NEXUS_API_KEY", "") or _config_api_key()).strip()


def _decode_body(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error", "detail", "reason"):
            value = payload.get(key)
            if value:
                if isinstance(value, (dict, list)):
                    return json.dumps(value, ensure_ascii=False)
                return str(value)
    if isinstance(payload, list) and payload:
        return _error_message(payload[0], fallback)
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return fallback


class NexusAccountClient:
    """Small v1 client for user-scoped endorsement/tracking operations."""

    def __init__(self, api_key: str, domain: str = GAME_DOMAIN, timeout: int = 20):
        if not api_key:
            raise NexusAPIError("A Nexus Mods API key is required.")
        self.api_key = api_key
        self.domain = domain
        self.timeout = timeout

    def request(self, method: str, path: str, *, form: dict | None = None,
                json_body: dict | None = None) -> Any:
        url = path if path.startswith("http") else API_BASE + path
        data = None
        headers = {
            "apikey": self.api_key,
            "Application-Name": APP_NAME,
            "Application-Version": APP_VERSION,
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            "Accept": "application/json",
        }
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return _decode_body(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = _decode_body(exc.read())
            except Exception:  # noqa: BLE001
                payload = None
            fallback = f"Nexus API HTTP {exc.code}: {exc.reason}"
            raise NexusAPIError(_error_message(payload, fallback), exc.code, payload) from exc
        except urllib.error.URLError as exc:
            raise NexusAPIError(f"Nexus API connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise NexusAPIError("Nexus API request timed out.") from exc

    def validate(self) -> dict:
        data = self.request("GET", "/users/validate.json")
        return data if isinstance(data, dict) else {}

    def tracked(self) -> Any:
        return self.request("GET", "/user/tracked_mods")

    def endorsements(self) -> Any:
        return self.request("GET", "/user/endorsements")

    def mod_details(self, mod_id: str | int) -> dict:
        data = self.request("GET", f"/games/{self.domain}/mods/{int(mod_id)}.json")
        return data if isinstance(data, dict) else {}

    def set_endorsed(self, mod_id: str | int, endorsed: bool, version: str = "") -> None:
        action = "endorse" if endorsed else "abstain"
        form = {"version": version} if version else {}
        self.request("POST", f"/games/{self.domain}/mods/{int(mod_id)}/{action}.json", form=form)

    def set_tracked(self, mod_id: str | int, tracked: bool) -> None:
        method = "POST" if tracked else "DELETE"
        path = f"/user/tracked_mods?{urllib.parse.urlencode({'domain_name': self.domain})}"
        self.request(method, path, json_body={"mod_id": int(mod_id)})


# Account list responses have varied slightly across v1 clients/eras. Keep extraction
# deliberately permissive while still filtering other games when the response carries a domain.
def _entry_mod_id(entry: Any) -> str | None:
    if isinstance(entry, (int, str)) and str(entry).isdigit():
        return str(int(entry))
    if not isinstance(entry, dict):
        return None
    for key in ("mod_id", "modId", "modid", "id"):
        value = entry.get(key)
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            return str(int(value))
    mod = entry.get("mod")
    if isinstance(mod, dict):
        return _entry_mod_id(mod)
    return None


def _entry_domain(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    for key in ("domain_name", "domainName", "game_domain_name", "gameDomainName"):
        if entry.get(key):
            return str(entry[key]).lower()
    game = entry.get("game")
    if isinstance(game, dict):
        return _entry_domain(game)
    return ""


def _entry_is_active_endorsement(entry: Any) -> bool:
    """Treat explicit abstained/non-endorsed records as inactive when present."""
    if not isinstance(entry, dict):
        return True
    for key in ("endorsed", "is_endorsed", "isEndorsed"):
        if key in entry:
            return bool(entry[key])
    for key in ("status", "endorsement_status", "endorsementStatus"):
        if key not in entry or entry[key] is None:
            continue
        value = str(entry[key]).strip().lower().replace("-", "_").replace(" ", "_")
        if value in {"abstain", "abstained", "not_endorsed", "unendorsed", "none", "false", "0"}:
            return False
        if value in {"endorse", "endorsed", "true", "1"}:
            return True
    return True


def account_entries_from_payload(payload: Any, domain: str = GAME_DOMAIN, *, endorsements: bool = False) -> list[Any]:
    """Return active account-list entries for one game, preserving Nexus response order.

    Nexus v1 has used several wrappers/shapes for tracked and endorsed lists.  Keeping
    the original entries (rather than only IDs) lets discovery views reuse embedded
    metadata when it is present while still filtering other games and abstained rows.
    Duplicate mod IDs are collapsed to the first active entry.
    """
    if isinstance(payload, dict):
        entries = None
        for key in ("mods", "tracked_mods", "endorsements", "data", "results"):
            if key in payload and isinstance(payload[key], (list, tuple)):
                entries = list(payload[key])
                break
        if entries is None:
            entries = [payload]
    elif isinstance(payload, (list, tuple, set)):
        entries = list(payload)
    else:
        entries = []

    out: list[Any] = []
    seen: set[str] = set()
    for entry in entries:
        if endorsements and not _entry_is_active_endorsement(entry):
            continue
        item_domain = _entry_domain(entry)
        if item_domain and item_domain != domain.lower():
            continue
        mid = _entry_mod_id(entry)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(entry)
    return out


def mod_ids_from_account_payload(payload: Any, domain: str = GAME_DOMAIN, *, endorsements: bool = False) -> set[str]:
    """Extract Nexus mod IDs from account-list payloads, tolerating legacy response shapes."""
    return {
        mid
        for entry in account_entries_from_payload(payload, domain, endorsements=endorsements)
        if (mid := _entry_mod_id(entry))
    }


@dataclass(frozen=True)
class InstalledMod:
    mod_id: str
    name: str
    installed_version: str
    stems: tuple[str, ...]
    file_count: int

    def as_dict(self) -> dict:
        return {
            "mod_id": self.mod_id,
            "name": self.name,
            "installed_version": self.installed_version,
            "stems": list(self.stems),
            "file_count": self.file_count,
        }


def installed_nexus_mods(game_root: Path) -> list[InstalledMod]:
    """Collapse manifest entries into one row per Nexus mod page/account entity."""
    # Reuse the deployer's canonical filename parser so update/deploy/account identity agrees.
    from .deployer import load_manifest, load_mod_names, nexus_key_from_stem, stem_display_name

    manifest = load_manifest(game_root)
    names = load_mod_names()
    grouped: dict[str, dict] = {}
    for stem, files in (manifest.get("mods", {}) or {}).items():
        key = nexus_key_from_stem(stem)
        if not key:
            continue
        _file_name, mod_id, epoch, version = key
        row = grouped.setdefault(mod_id, {
            "mod_id": mod_id,
            "name": names.get(mod_id) or stem_display_name(stem),
            "installed_version": str(version),
            "epoch": int(epoch),
            "stems": [],
            "file_count": 0,
        })
        row["stems"].append(stem)
        row["file_count"] += len(files) if isinstance(files, list) else 0
        if int(epoch) >= row["epoch"]:
            row["epoch"] = int(epoch)
            row["installed_version"] = str(version)
            if not names.get(mod_id):
                row["name"] = stem_display_name(stem)
    return [
        InstalledMod(
            mod_id=mid,
            name=row["name"],
            installed_version=row["installed_version"],
            stems=tuple(sorted(row["stems"], key=str.lower)),
            file_count=row["file_count"],
        )
        for mid, row in sorted(grouped.items(), key=lambda kv: (kv[1]["name"].lower(), int(kv[0])))
    ]


def live_state(client: NexusAccountClient) -> tuple[set[str], set[str]]:
    tracked = mod_ids_from_account_payload(client.tracked(), client.domain)
    endorsed = mod_ids_from_account_payload(client.endorsements(), client.domain, endorsements=True)
    return tracked, endorsed


def sync_installed(game_root: Path, client: NexusAccountClient) -> dict:
    user = client.validate()
    tracked, endorsed = live_state(client)
    mods = []
    for mod in installed_nexus_mods(game_root):
        row = mod.as_dict()
        row["endorsed"] = mod.mod_id in endorsed
        row["tracked"] = mod.mod_id in tracked
        mods.append(row)
    return {
        "type": "sync",
        "user": {
            "name": user.get("name", ""),
            "user_id": user.get("user_id"),
            "is_premium": bool(user.get("is_premium", False)),
            "is_supporter": bool(user.get("is_supporter", False)),
        },
        "mods": mods,
    }


def detail_state(client: NexusAccountClient, mod_id: str) -> dict:
    tracked, endorsed = live_state(client)
    meta = client.mod_details(mod_id)
    return {
        "type": "detail",
        "mod_id": str(mod_id),
        "endorsed": str(mod_id) in endorsed,
        "tracked": str(mod_id) in tracked,
        "metadata": meta,
    }


def mutate_and_verify(client: NexusAccountClient, action: str, mod_id: str, version: str = "") -> dict:
    if action == "endorse":
        client.set_endorsed(mod_id, True, version)
        ids = mod_ids_from_account_payload(client.endorsements(), client.domain, endorsements=True)
        state = mod_id in ids
        expected, field = True, "endorsed"
    elif action == "unendorse":
        client.set_endorsed(mod_id, False, version)
        ids = mod_ids_from_account_payload(client.endorsements(), client.domain, endorsements=True)
        state = mod_id in ids
        expected, field = False, "endorsed"
    elif action == "track":
        client.set_tracked(mod_id, True)
        ids = mod_ids_from_account_payload(client.tracked(), client.domain)
        state = mod_id in ids
        expected, field = True, "tracked"
    elif action == "untrack":
        client.set_tracked(mod_id, False)
        ids = mod_ids_from_account_payload(client.tracked(), client.domain)
        state = mod_id in ids
        expected, field = False, "tracked"
    else:
        raise ValueError(f"unknown action: {action}")
    if state != expected:
        raise NexusAPIError(f"Nexus accepted {action}, but the live account state did not confirm it.")
    return {"type": "state", "mod_id": mod_id, field: state, "action": action}


def _emit(payload: dict) -> None:
    print("[nexus-account] " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Nexus Mods endorsement/tracking account integration")
    ap.add_argument("command", choices=("validate", "inventory", "sync", "detail", "endorse", "unendorse", "track", "untrack"))
    ap.add_argument("--game-root", type=Path, default=None)
    ap.add_argument("--mod-id", default="")
    ap.add_argument("--version", default="")
    ap.add_argument("--api-key", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.command == "inventory":
        if args.game_root is None:
            _emit({"type": "error", "message": "--game-root is required for inventory."})
            return 2
        _emit({"type": "inventory", "mods": [m.as_dict() for m in installed_nexus_mods(args.game_root)]})
        return 0

    key = resolve_api_key(args.api_key)
    if not key:
        _emit({"type": "error", "message": "No Nexus API key configured."})
        return 2
    try:
        client = NexusAccountClient(key)
        if args.command == "validate":
            user = client.validate()
            _emit({
                "type": "validate",
                "user": {
                    "name": user.get("name", ""),
                    "user_id": user.get("user_id"),
                    "is_premium": bool(user.get("is_premium", False)),
                    "is_supporter": bool(user.get("is_supporter", False)),
                },
            })
        elif args.command == "sync":
            if args.game_root is None:
                raise NexusAPIError("--game-root is required for sync.")
            _emit(sync_installed(args.game_root, client))
        elif args.command == "detail":
            if not args.mod_id:
                raise NexusAPIError("--mod-id is required for detail.")
            _emit(detail_state(client, args.mod_id))
        else:
            if not args.mod_id:
                raise NexusAPIError(f"--mod-id is required for {args.command}.")
            _emit(mutate_and_verify(client, args.command, args.mod_id, args.version))
        return 0
    except NexusAPIError as exc:
        _emit({"type": "error", "message": str(exc), "status": exc.status})
        return 1
    except Exception as exc:  # noqa: BLE001
        _emit({"type": "error", "message": f"Unexpected Nexus account error: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
