#!/usr/bin/env python3
"""Portable profile management for the smart-deployment backend.

Profiles are database state, not copied game directories.  A profile captures
which staged mods are enabled plus the conflict and archive-order preferences
that determine the projected hardlink tree.  Export files deliberately use mod
names/Nexus metadata instead of installation-local SQLite ids so they can be
moved between installations.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from .smart_deployment import Store, _safe_rel, deployment_lock

JSON_PREFIX = "[profile-json] "
PROFILE_FORMAT = "nexus-collection-manager-profile"
PROFILE_SCHEMA_VERSION = 1
MAX_IMPORT_BYTES = 16 * 1024 * 1024


class ProfileError(RuntimeError):
    pass


def _emit(payload: dict) -> None:
    print(JSON_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _profile_exists(store: Store, name: str) -> bool:
    return store.conn.execute("SELECT 1 FROM profiles WHERE name=?", (name,)).fetchone() is not None


def _validate_name(name: str) -> str:
    name = str(name).strip()
    if not name or len(name) > 120:
        raise ValueError("profile name must be 1-120 characters")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("profile name cannot contain control characters")
    return name


def _clear_profile_state(store: Store, profile: str) -> None:
    # conflict_inbox is derived scan state; replacing a profile invalidates it.
    for table in (
        "conflict_inbox",
        "load_order_rules",
        "archive_order",
        "load_order",
        "conflict_rules",
        "profile_mods",
    ):
        store.conn.execute(f"DELETE FROM {table} WHERE profile=?", (profile,))


def _copy_profile_state(store: Store, source: str, target: str, *, overwrite: bool = False) -> dict:
    source = _validate_name(source)
    target = _validate_name(target)
    if not _profile_exists(store, source):
        raise KeyError(f"profile not found: {source}")
    if source == target:
        return {"name": target, "copied_from": source, "unchanged": True}
    exists = _profile_exists(store, target)
    if exists and not overwrite:
        raise ValueError(f"profile already exists: {target}")

    with store.conn:
        if not exists:
            store.conn.execute(
                "INSERT INTO profiles(name,active,created_at) VALUES(?,?,?)",
                (target, 0, time.time()),
            )
        else:
            _clear_profile_state(store, target)
        store.conn.execute(
            "INSERT INTO profile_mods(profile,mod_id,enabled) "
            "SELECT ?,mod_id,enabled FROM profile_mods WHERE profile=?",
            (target, source),
        )
        store.conn.execute(
            "INSERT INTO conflict_rules(profile,rel_path,preferred_mod_id) "
            "SELECT ?,rel_path,preferred_mod_id FROM conflict_rules WHERE profile=?",
            (target, source),
        )
        store.conn.execute(
            "INSERT INTO load_order(profile,mod_id,priority) "
            "SELECT ?,mod_id,priority FROM load_order WHERE profile=?",
            (target, source),
        )
        store.conn.execute(
            "INSERT INTO archive_order(profile,mod_id,rel_path,priority) "
            "SELECT ?,mod_id,rel_path,priority FROM archive_order WHERE profile=?",
            (target, source),
        )
        store.conn.execute(
            "INSERT INTO load_order_rules(profile,before_mod_id,after_mod_id,note,created_at) "
            "SELECT ?,before_mod_id,after_mod_id,note,created_at FROM load_order_rules WHERE profile=?",
            (target, source),
        )
    return {"name": target, "copied_from": source, "overwritten": exists}


def save_profile(store: Store, name: str, *, source: str | None = None, overwrite: bool = False) -> dict:
    """Capture a profile under another name.

    Smart-deployment settings are persisted immediately, so "save" is a
    snapshot/save-as operation rather than flushing transient state.
    """
    source = source or store.active_profile
    result = _copy_profile_state(store, source, name, overwrite=overwrite)
    result["type"] = "save"
    return result


def duplicate_profile(store: Store, source: str, name: str) -> dict:
    result = _copy_profile_state(store, source, name, overwrite=False)
    result["type"] = "duplicate"
    return result


def load_profile(store: Store, name: str) -> dict:
    name = _validate_name(name)
    if not _profile_exists(store, name):
        raise KeyError(f"profile not found: {name}")
    old = store.active_profile
    store.switch_profile_flag(name)
    deploy = store.deploy()
    return {"type": "load", "old_profile": old, "active_profile": name, "deploy": deploy}


def _nexus_identity(store: Store, mod_id: int) -> dict[str, Any]:
    row = store.conn.execute(
        "SELECT al.nexus_mod_id,al.nexus_file_id,al.nexus_game_domain "
        "FROM archive_installs ai JOIN archive_library al ON al.id=ai.archive_id "
        "WHERE ai.mod_id=? AND al.nexus_mod_id IS NOT NULL "
        "ORDER BY ai.linked_at DESC,al.id DESC LIMIT 1",
        (mod_id,),
    ).fetchone()
    if not row:
        return {}
    return {
        "nexus_mod_id": int(row["nexus_mod_id"]) if row["nexus_mod_id"] is not None else None,
        "nexus_file_id": int(row["nexus_file_id"]) if row["nexus_file_id"] is not None else None,
        "nexus_game_domain": str(row["nexus_game_domain"] or "cyberpunk2077"),
    }


def snapshot(store: Store, profile: str | None = None) -> dict:
    profile = _validate_name(profile or store.active_profile)
    p = store.conn.execute("SELECT name,active,created_at FROM profiles WHERE name=?", (profile,)).fetchone()
    if not p:
        raise KeyError(f"profile not found: {profile}")

    mod_rows = store.conn.execute(
        "SELECT m.id,m.name,m.source_archive,COALESCE(pm.enabled,m.enabled_default) enabled "
        "FROM mods m LEFT JOIN profile_mods pm ON pm.mod_id=m.id AND pm.profile=? "
        "ORDER BY m.name COLLATE NOCASE",
        (profile,),
    ).fetchall()
    mods: list[dict] = []
    for row in mod_rows:
        item = {
            "name": str(row["name"]),
            "enabled": bool(row["enabled"]),
            "source_archive": str(row["source_archive"] or ""),
        }
        item.update(_nexus_identity(store, int(row["id"])))
        mods.append(item)

    conflicts = [dict(r) for r in store.conn.execute(
        "SELECT cr.rel_path,pm.name preferred_mod FROM conflict_rules cr "
        "LEFT JOIN mods pm ON pm.id=cr.preferred_mod_id WHERE cr.profile=? ORDER BY cr.rel_path",
        (profile,),
    ).fetchall()]
    legacy_order = [dict(r) for r in store.conn.execute(
        "SELECT m.name mod,lo.priority FROM load_order lo JOIN mods m ON m.id=lo.mod_id "
        "WHERE lo.profile=? ORDER BY lo.priority,m.name COLLATE NOCASE",
        (profile,),
    ).fetchall()]
    archive_order = [dict(r) for r in store.conn.execute(
        "SELECT m.name mod,ao.rel_path,ao.priority FROM archive_order ao "
        "JOIN mods m ON m.id=ao.mod_id WHERE ao.profile=? "
        "ORDER BY ao.priority,m.name COLLATE NOCASE,ao.rel_path",
        (profile,),
    ).fetchall()]
    rules = [dict(r) for r in store.conn.execute(
        "SELECT bm.name before_mod,am.name after_mod,r.note FROM load_order_rules r "
        "JOIN mods bm ON bm.id=r.before_mod_id JOIN mods am ON am.id=r.after_mod_id "
        "WHERE r.profile=? ORDER BY bm.name COLLATE NOCASE,am.name COLLATE NOCASE",
        (profile,),
    ).fetchall()]

    return {
        "format": PROFILE_FORMAT,
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile": profile,
        "created_at": float(p["created_at"]),
        "exported_at": time.time(),
        "mods": mods,
        "conflict_preferences": conflicts,
        "legacy_mod_order": legacy_order,
        "archive_order": archive_order,
        "load_order_rules": rules,
    }


def _atomic_json_write(path: Path, payload: dict) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def export_profile(store: Store, profile: str, output: Path) -> dict:
    data = snapshot(store, profile)
    _atomic_json_write(output, data)
    return {
        "type": "export",
        "profile": data["profile"],
        "output": str(output.expanduser().resolve()),
        "mods": len(data["mods"]),
        "enabled": sum(1 for m in data["mods"] if m["enabled"]),
    }


def _expect_list(data: dict, key: str) -> list:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"profile field {key!r} must be a list")
    return value


def validate_document(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("profile document must be a JSON object")
    if data.get("format") != PROFILE_FORMAT:
        raise ValueError("not a Nexus Collection Manager profile export")
    if int(data.get("schema_version", -1)) != PROFILE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported profile schema {data.get('schema_version')!r}; expected {PROFILE_SCHEMA_VERSION}"
        )
    _validate_name(str(data.get("profile") or ""))
    mods = _expect_list(data, "mods")
    seen: set[str] = set()
    for i, mod in enumerate(mods):
        if not isinstance(mod, dict):
            raise ValueError(f"mods[{i}] must be an object")
        name = str(mod.get("name") or "").strip()
        if not name or len(name) > 300:
            raise ValueError(f"mods[{i}] has an invalid name")
        if name in seen:
            raise ValueError(f"duplicate mod in profile: {name}")
        seen.add(name)
        if "enabled" not in mod or not isinstance(mod["enabled"], bool):
            raise ValueError(f"mods[{i}].enabled must be true or false")
        if mod.get("nexus_mod_id") is not None and int(mod["nexus_mod_id"]) <= 0:
            raise ValueError(f"mods[{i}].nexus_mod_id must be positive")
    for i, pref in enumerate(_expect_list(data, "conflict_preferences")):
        if not isinstance(pref, dict):
            raise ValueError(f"conflict_preferences[{i}] must be an object")
        _safe_rel(str(pref.get("rel_path") or ""))
    for i, row in enumerate(_expect_list(data, "archive_order")):
        if not isinstance(row, dict):
            raise ValueError(f"archive_order[{i}] must be an object")
        _safe_rel(str(row.get("rel_path") or ""))
        int(row.get("priority", i))
    for key in ("legacy_mod_order", "load_order_rules"):
        for i, row in enumerate(_expect_list(data, key)):
            if not isinstance(row, dict):
                raise ValueError(f"{key}[{i}] must be an object")
    return data


def read_document(path: Path) -> dict:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError("profile export is unexpectedly large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read profile JSON: {exc}") from exc
    return validate_document(data)


def _local_identity_maps(store: Store) -> tuple[dict[str, int], dict[int, set[int]], dict[int, str]]:
    by_name: dict[str, int] = {}
    names: dict[int, str] = {}
    for row in store.conn.execute("SELECT id,name FROM mods"):
        mid = int(row["id"]); name = str(row["name"])
        by_name[name] = mid; names[mid] = name
    by_nexus: dict[int, set[int]] = {}
    for row in store.conn.execute(
        "SELECT DISTINCT ai.mod_id,al.nexus_mod_id FROM archive_installs ai "
        "JOIN archive_library al ON al.id=ai.archive_id WHERE al.nexus_mod_id IS NOT NULL"
    ):
        by_nexus.setdefault(int(row["nexus_mod_id"]), set()).add(int(row["mod_id"]))
    return by_name, by_nexus, names


def _resolve_import_mods(store: Store, data: dict) -> tuple[dict[str, int], list[dict], list[dict]]:
    by_name, by_nexus, local_names = _local_identity_maps(store)
    resolved: dict[str, int] = {}
    matched: list[dict] = []
    missing: list[dict] = []
    for mod in data["mods"]:
        exported_name = str(mod["name"])
        mid = by_name.get(exported_name)
        method = "name" if mid is not None else ""
        nexus = mod.get("nexus_mod_id")
        if mid is None and nexus is not None:
            candidates = by_nexus.get(int(nexus), set())
            if len(candidates) == 1:
                mid = next(iter(candidates)); method = "nexus_mod_id"
        if mid is None:
            missing.append({
                "name": exported_name,
                "enabled": bool(mod["enabled"]),
                "nexus_mod_id": mod.get("nexus_mod_id"),
                "source_archive": str(mod.get("source_archive") or ""),
            })
            continue
        resolved[exported_name] = int(mid)
        matched.append({
            "exported_name": exported_name,
            "local_name": local_names[int(mid)],
            "enabled": bool(mod["enabled"]),
            "matched_by": method,
        })
    return resolved, matched, missing


def _validate_rule_cycles(rules: Iterable[tuple[int, int]]) -> None:
    edges: dict[int, set[int]] = {}
    indegree: dict[int, int] = {}
    for before, after in rules:
        if before == after:
            raise ValueError("load-order rule cannot order a mod before itself")
        edges.setdefault(before, set()).add(after)
        edges.setdefault(after, set())
        indegree.setdefault(before, 0); indegree.setdefault(after, 0)
    # Recalculate after de-duplicating edges.
    indegree = {node: 0 for node in edges}
    for tos in edges.values():
        for node in tos:
            indegree[node] += 1
    queue = [n for n, d in indegree.items() if d == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for nxt in edges[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen != len(indegree):
        raise ValueError("imported load-order rules contain a cycle")


def import_preview(store: Store, data: dict, *, name: str | None = None) -> dict:
    target = _validate_name(name or str(data["profile"]))
    resolved, matched, missing = _resolve_import_mods(store, data)
    skipped: list[dict] = []

    for pref in data.get("conflict_preferences", []):
        owner = pref.get("preferred_mod")
        if owner and str(owner) not in resolved:
            skipped.append({"kind": "conflict_preference", "path": pref.get("rel_path"), "missing_mod": owner})
    for row in data.get("legacy_mod_order", []):
        if str(row.get("mod") or "") not in resolved:
            skipped.append({"kind": "legacy_mod_order", "missing_mod": row.get("mod")})
    for row in data.get("archive_order", []):
        mod = str(row.get("mod") or "")
        mid = resolved.get(mod)
        rel = _safe_rel(str(row.get("rel_path") or ""))
        if mid is None:
            skipped.append({"kind": "archive_order", "missing_mod": mod, "path": rel})
        elif not store.conn.execute("SELECT 1 FROM files WHERE mod_id=? AND rel_path=?", (mid, rel)).fetchone():
            skipped.append({"kind": "archive_order", "mod": mod, "missing_archive": rel})
    valid_rule_ids: list[tuple[int, int]] = []
    for row in data.get("load_order_rules", []):
        before = str(row.get("before_mod") or ""); after = str(row.get("after_mod") or "")
        if before not in resolved or after not in resolved:
            skipped.append({"kind": "load_order_rule", "before": before, "after": after, "reason": "missing mod"})
        else:
            valid_rule_ids.append((resolved[before], resolved[after]))
    _validate_rule_cycles(valid_rule_ids)
    return {
        "type": "import-preview",
        "profile": target,
        "already_exists": _profile_exists(store, target),
        "matched": matched,
        "missing": missing,
        "skipped_preferences": skipped,
        "matched_count": len(matched),
        "missing_count": len(missing),
        "skipped_count": len(skipped),
    }


def import_profile(
    store: Store,
    data: dict,
    *,
    name: str | None = None,
    overwrite: bool = False,
    activate: bool = False,
) -> dict:
    preview = import_preview(store, data, name=name)
    target = preview["profile"]
    exists = _profile_exists(store, target)
    if exists and not overwrite:
        raise ValueError(f"profile already exists: {target}")
    resolved = {x["exported_name"]: None for x in preview["matched"]}
    # Re-resolve to ids after validation. The local DB cannot change while the
    # caller holds the deployment lock.
    resolved_ids, matched, missing = _resolve_import_mods(store, data)
    resolved.update(resolved_ids)

    # Profile members not named by the portable document are disabled. This is
    # safer than inheriting arbitrary local-only mods into an imported setup.
    exported_enabled = {str(m["name"]): bool(m["enabled"]) for m in data["mods"]}
    local_enabled: dict[int, bool] = {
        int(mid): exported_enabled[name]
        for name, mid in resolved_ids.items()
    }

    rule_rows: list[tuple[int, int, str]] = []
    for row in data.get("load_order_rules", []):
        before = resolved_ids.get(str(row.get("before_mod") or ""))
        after = resolved_ids.get(str(row.get("after_mod") or ""))
        if before is not None and after is not None:
            rule_rows.append((before, after, str(row.get("note") or "").strip()))
    _validate_rule_cycles((b, a) for b, a, _ in rule_rows)

    with store.conn:
        if not exists:
            store.conn.execute(
                "INSERT INTO profiles(name,active,created_at) VALUES(?,?,?)",
                (target, 0, time.time()),
            )
        else:
            _clear_profile_state(store, target)

        for row in store.conn.execute("SELECT id FROM mods"):
            mid = int(row["id"])
            store.conn.execute(
                "INSERT INTO profile_mods(profile,mod_id,enabled) VALUES(?,?,?)",
                (target, mid, 1 if local_enabled.get(mid, False) else 0),
            )

        for pref in data.get("conflict_preferences", []):
            rel = _safe_rel(str(pref.get("rel_path") or ""))
            owner_name = pref.get("preferred_mod")
            owner_id = resolved_ids.get(str(owner_name)) if owner_name else None
            if owner_name and owner_id is None:
                continue
            store.conn.execute(
                "INSERT INTO conflict_rules(profile,rel_path,preferred_mod_id) VALUES(?,?,?)",
                (target, rel, owner_id),
            )

        for row in sorted(data.get("legacy_mod_order", []), key=lambda x: int(x.get("priority", 0))):
            mid = resolved_ids.get(str(row.get("mod") or ""))
            if mid is not None:
                store.conn.execute(
                    "INSERT OR REPLACE INTO load_order(profile,mod_id,priority) VALUES(?,?,?)",
                    (target, mid, int(row.get("priority", 0))),
                )

        for row in sorted(data.get("archive_order", []), key=lambda x: int(x.get("priority", 0))):
            mod_name = str(row.get("mod") or "")
            mid = resolved_ids.get(mod_name)
            rel = _safe_rel(str(row.get("rel_path") or ""))
            if mid is None:
                continue
            if not store.conn.execute("SELECT 1 FROM files WHERE mod_id=? AND rel_path=?", (mid, rel)).fetchone():
                continue
            store.conn.execute(
                "INSERT OR REPLACE INTO archive_order(profile,mod_id,rel_path,priority) VALUES(?,?,?,?)",
                (target, mid, rel, int(row.get("priority", 0))),
            )

        for before, after, note in rule_rows:
            store.conn.execute(
                "INSERT OR REPLACE INTO load_order_rules"
                "(profile,before_mod_id,after_mod_id,note,created_at) VALUES(?,?,?,?,?)",
                (target, before, after, note, time.time()),
            )

        if activate:
            store.conn.execute("UPDATE profiles SET active=0 WHERE active=1")
            store.conn.execute("UPDATE profiles SET active=1 WHERE name=?", (target,))

    deploy = store.deploy() if activate else None
    return {
        **preview,
        "type": "import",
        "profile": target,
        "overwritten": exists,
        "activated": activate,
        "deploy": deploy,
    }


def _value_map(rows: list[dict], key_fn, value_fn) -> dict:
    return {key_fn(row): value_fn(row) for row in rows}


def compare_profiles(store: Store, left: str, right: str) -> dict:
    a = snapshot(store, left); b = snapshot(store, right)
    changes: list[dict] = []

    a_mod = _value_map(a["mods"], lambda x: x["name"], lambda x: "enabled" if x["enabled"] else "disabled")
    b_mod = _value_map(b["mods"], lambda x: x["name"], lambda x: "enabled" if x["enabled"] else "disabled")
    for key in sorted(set(a_mod) | set(b_mod), key=str.casefold):
        av = a_mod.get(key, "not present"); bv = b_mod.get(key, "not present")
        if av != bv:
            changes.append({"category": "mod", "item": key, "left": av, "right": bv})

    def pref_value(row: dict) -> str:
        return str(row.get("preferred_mod") or "<external/unmanaged>")
    a_pref = _value_map(a["conflict_preferences"], lambda x: x["rel_path"], pref_value)
    b_pref = _value_map(b["conflict_preferences"], lambda x: x["rel_path"], pref_value)
    for key in sorted(set(a_pref) | set(b_pref)):
        av = a_pref.get(key, "<none>"); bv = b_pref.get(key, "<none>")
        if av != bv:
            changes.append({"category": "conflict", "item": key, "left": av, "right": bv})

    def order_map(doc: dict, field: str) -> dict:
        out = {}
        for row in doc[field]:
            if field == "archive_order":
                key = f"{row['mod']} :: {row['rel_path']}"
            else:
                key = str(row["mod"])
            out[key] = int(row.get("priority", 0)) + 1
        return out
    for field, category in (("legacy_mod_order", "mod order"), ("archive_order", "archive order")):
        am = order_map(a, field); bm = order_map(b, field)
        for key in sorted(set(am) | set(bm), key=str.casefold):
            av = am.get(key, "default"); bv = bm.get(key, "default")
            if av != bv:
                changes.append({"category": category, "item": key, "left": av, "right": bv})

    def rules(doc: dict) -> dict[str, str]:
        return {
            f"{r['before_mod']} → {r['after_mod']}": str(r.get("note") or "")
            for r in doc["load_order_rules"]
        }
    ar = rules(a); br = rules(b)
    for key in sorted(set(ar) | set(br), key=str.casefold):
        av = ar.get(key, "<absent>"); bv = br.get(key, "<absent>")
        if av != bv:
            changes.append({"category": "load rule", "item": key, "left": av, "right": bv})

    return {
        "type": "compare",
        "left": left,
        "right": right,
        "identical": not changes,
        "difference_count": len(changes),
        "differences": changes,
    }


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Save, load, export, import, duplicate and compare mod profiles")
    sub = ap.add_subparsers(dest="command", required=True)

    def root(name: str, help_text: str):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--game-root", type=Path, required=True)
        return p

    root("list", "List profiles")

    p = root("show", "Show a portable snapshot of one profile")
    p.add_argument("profile", nargs="?")

    p = root("save", "Save/capture a profile under another name")
    p.add_argument("name")
    p.add_argument("--from-profile", dest="from_profile")
    p.add_argument("--overwrite", action="store_true")

    p = root("load", "Activate a profile and rebuild game hardlinks")
    p.add_argument("profile")

    p = root("duplicate", "Duplicate a profile")
    p.add_argument("source")
    p.add_argument("name")

    p = root("export", "Export a portable JSON profile")
    p.add_argument("profile")
    p.add_argument("--output", type=Path, required=True)

    p = root("import", "Import a portable JSON profile")
    p.add_argument("file", type=Path)
    p.add_argument("--name")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--activate", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = root("compare", "Compare two stored profiles")
    p.add_argument("left")
    p.add_argument("right")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    game = args.game_root.expanduser().resolve()
    try:
        if not game.is_dir():
            raise FileNotFoundError(f"game root not found: {game}")
        readonly = args.command in {"list", "show", "compare", "export"} or (
            args.command == "import" and args.dry_run
        )
        with deployment_lock(game, exclusive=not readonly):
            with Store(game) as store:
                if args.command == "list":
                    result = {"type": "list", "active_profile": store.active_profile, "profiles": store.profiles()}
                elif args.command == "show":
                    result = snapshot(store, args.profile)
                    result["type"] = "show"
                elif args.command == "save":
                    result = save_profile(store, args.name, source=args.from_profile, overwrite=args.overwrite)
                elif args.command == "load":
                    result = load_profile(store, args.profile)
                elif args.command == "duplicate":
                    result = duplicate_profile(store, args.source, args.name)
                elif args.command == "export":
                    result = export_profile(store, args.profile, args.output)
                elif args.command == "import":
                    doc = read_document(args.file)
                    if args.dry_run:
                        result = import_preview(store, doc, name=args.name)
                    else:
                        result = import_profile(
                            store, doc, name=args.name, overwrite=args.overwrite, activate=args.activate
                        )
                elif args.command == "compare":
                    result = compare_profiles(store, args.left, args.right)
                else:
                    raise AssertionError(args.command)
        _emit(result)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        _emit({"type": "error", "message": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
