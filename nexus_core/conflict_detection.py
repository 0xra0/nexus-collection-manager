#!/usr/bin/env python3
"""Multi-layer conflict detection for the Linux smart-deployment backend.

The engine operates exclusively on canonical staged content under
``<game>/downloaded_mods``.  It does not need the active game projection to be
present, so conflicts can be reviewed while the game tree is fully undeployed.

Layers:
  * exact projected-path/file overlaps;
  * RED4 ``.archive`` internal resource-hash overlaps;
  * redscript annotation conflicts (replace/add methods and fields);
  * TweakXL flat/record-key overlaps, distinguishing direct writes from array
    mutation operations;
  * dependency/compatibility context for severity classification.

Conflict rows are synchronized into the smart-deployment SQLite database.  The
stable IDs let the GUI behave like an inbox while direct actions (disable,
uninstall, prefer/load order) are executed against the same database.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence
from urllib.parse import quote_plus

try:
    import yaml
    from yaml.nodes import MappingNode, ScalarNode, SequenceNode
except Exception:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None
    MappingNode = ScalarNode = SequenceNode = object  # type: ignore[assignment]

from .auto_correlation import default_state_path as correlation_state_path
from .auto_correlation import extract_mod_ids
from .nexus_graphql import NexusGraphQLClient, NexusGraphQLError
from .smart_deployment import Store, deployment_lock

GAME_DOMAIN = "cyberpunk2077"
JSON_PREFIX = "[conflict-json] "
SCHEMA_VERSION = 1

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_PATCH_WORDS = {"patch", "compat", "compatibility", "addon", "add-on", "fix", "bridge", "integration"}
_STOP_WORDS = {
    "the", "and", "for", "with", "mod", "mods", "cyberpunk", "2077", "update", "version",
    "archive", "redmod", "installer", "main", "optional", "file", "files", "v",
}

_REDS_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_REDS_ANNOT_RE = re.compile(r"@(replaceMethod|replaceGlobal|addMethod|wrapMethod)\s*\(([^)]*)\)", re.I)
_REDS_FUNC_RE = re.compile(r"\bfunc\s+([A-Za-z_]\w*)", re.I)
_REDS_FIELD_ANNOT_RE = re.compile(r"@addField\s*\(([^)]*)\)", re.I)
_REDS_FIELD_RE = re.compile(r"\b(?:let|var)\s+([A-Za-z_]\w*)\s*:", re.I)
_TWEAK_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_$][A-Za-z0-9_.$:\-/]*)\s*(\+=|-=|=)\s*", re.M)


@dataclass(frozen=True)
class ArchiveIndex:
    path: str
    version: int
    resource_hashes: frozenset[int]
    names: dict[int, str] = field(default_factory=dict)
    dependencies: frozenset[int] = frozenset()
    warning: str = ""


@dataclass
class ManagedMod:
    id: int
    name: str
    source_archive: str
    enabled: bool
    nexus_id: str = ""
    nexus_url: str = ""
    files: list[dict[str, Any]] = field(default_factory=list)
    archives: list[dict[str, Any]] = field(default_factory=list)


def _emit(payload: dict[str, Any]) -> None:
    print(JSON_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _stable_id(kind: str, *parts: str) -> str:
    raw = "\0".join([kind, *sorted(str(p) for p in parts)])
    return hashlib.sha1(raw.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:20]


def _fnv1a64(text: str) -> int:
    value = 0xCBF29CE484222325
    for b in text.replace("/", "\\").lower().encode("utf-8", "replace"):
        value ^= b
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def _looks_resource_path(value: str) -> bool:
    if not value or len(value) > 1024 or "\x00" in value:
        return False
    if "/" not in value and "\\" not in value:
        return False
    suffix = PurePosixPath(value.replace("\\", "/")).suffix
    return bool(suffix and 1 < len(suffix) <= 20)


def read_archive_index(path: Path) -> ArchiveIndex:
    """Read RED4 archive resource hashes without extracting the archive.

    The RED4 file table stores a 64-bit name hash for every contained resource.
    WolvenKit archives may additionally carry custom path strings; those are
    best-effort decoded for human-readable inbox evidence, but conflict proof is
    based on the hash table and therefore still works without strings.
    """
    path = path.expanduser().resolve()
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            header = fh.read(44)
            if len(header) != 44:
                raise ValueError("truncated RED4 archive header")
            magic, version, index_pos, index_size, _dbg_pos, _dbg_size, _file_size, custom_len = struct.unpack(
                "<4sIQIQIQI", header
            )
            if magic != b"RDAR":
                raise ValueError("not a RED4 archive (missing RDAR magic)")
            if version < 8 or version > 64:
                raise ValueError(f"unsupported RED4 archive version {version}")
            if index_pos < 44 or index_pos + 28 > size:
                raise ValueError("invalid RED4 archive index offset")

            names: dict[int, str] = {}
            if custom_len and 44 + custom_len <= min(size, index_pos):
                fh.seek(44)
                custom = fh.read(custom_len)
                if custom.startswith(b"LXRS") and len(custom) >= 20:
                    try:
                        _cmagic, _cver, _usize, _zsize, path_count = struct.unpack_from("<4sIiii", custom, 0)
                    except struct.error:
                        path_count = 0
                    if 0 < path_count <= 2_000_000:
                        # WolvenKit path blocks may be compressed.  When path strings are
                        # stored plainly this recovers them; otherwise hashes still suffice.
                        tail = custom[20:]
                        strings = []
                        for raw in tail.split(b"\0"):
                            if len(strings) >= path_count:
                                break
                            try:
                                text = raw.decode("utf-8")
                            except UnicodeDecodeError:
                                continue
                            if _looks_resource_path(text):
                                strings.append(text.replace("/", "\\"))
                        for text in strings:
                            names[_fnv1a64(text)] = text

            fh.seek(index_pos)
            head = fh.read(28)
            if len(head) != 28:
                raise ValueError("truncated RED4 file table")
            file_table_offset, _file_table_size, _crc, entry_count, segment_count, dep_count = struct.unpack(
                "<IIQIII", head
            )
            if entry_count > 2_000_000 or segment_count > 20_000_000 or dep_count > 50_000_000:
                raise ValueError("implausibly large RED4 table counts")
            # FileTableOffset is normally 8: it skips the offset/size preamble;
            # CRC+counts consume another 20 bytes before the 56-byte records.
            records_pos = index_pos + max(8, file_table_offset) + 20
            required = records_pos + entry_count * 56
            if required > size:
                raise ValueError("RED4 file records extend beyond archive")
            fh.seek(records_pos)
            resources: set[int] = set()
            deps: set[int] = set()
            dep_ranges: list[tuple[int, int]] = []
            for _ in range(entry_count):
                raw = fh.read(56)
                if len(raw) != 56:
                    raise ValueError("truncated RED4 file record")
                name_hash, _timestamp, _inline, _seg_start, _seg_end, dep_start, dep_end, _sha1 = struct.unpack(
                    "<QqIIIII20s", raw
                )
                resources.add(int(name_hash))
                if dep_end >= dep_start:
                    dep_ranges.append((int(dep_start), int(dep_end)))
            # Resource-dependency hashes live after file records + segment table.
            dep_pos = records_pos + entry_count * 56 + segment_count * 16
            if dep_count and dep_pos + dep_count * 8 <= size:
                fh.seek(dep_pos)
                raw = fh.read(dep_count * 8)
                deps.update(struct.unpack(f"<{dep_count}Q", raw))
            return ArchiveIndex(str(path), int(version), frozenset(resources), names, frozenset(deps))
    except Exception as exc:  # noqa: BLE001
        return ArchiveIndex(str(path), 0, frozenset(), {}, frozenset(), str(exc))


def _load_correlation_ids(game_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return (name->nexus id, projected path->nexus id) from reviewed correlation state."""
    path = correlation_state_path(game_root)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}, {}
    by_name: dict[str, str] = {}
    by_path: dict[str, str] = {}
    for row in state.get("results") or []:
        match = row.get("match") or {}
        mid = str(match.get("mod_id") or "")
        if not mid.isdigit():
            continue
        name = str(row.get("local_name") or "").strip().casefold()
        if name:
            by_name[name] = mid
        for rel in row.get("files") or []:
            by_path[str(rel).replace("\\", "/").casefold()] = mid
    return by_name, by_path


def _nexus_url(mod_id: str, name: str) -> str:
    if mod_id and mod_id.isdigit():
        return f"https://www.nexusmods.com/cyberpunk2077/mods/{mod_id}"
    return "https://www.nexusmods.com/cyberpunk2077/search/?gsearch=" + quote_plus(name) + "&gsearchtype=mods"


def _tokenize_name(name: str) -> set[str]:
    toks = set(re.findall(r"[a-z0-9]+", name.casefold()))
    return {t for t in toks if len(t) >= 3 and t not in _STOP_WORDS and not t.isdigit()}


def _patch_relation(a: ManagedMod, b: ManagedMod) -> str:
    al = a.name.casefold(); bl = b.name.casefold()
    aw = _tokenize_name(a.name); bw = _tokenize_name(b.name)
    if not aw or not bw:
        return ""
    if (_PATCH_WORDS & set(re.findall(r"[a-z-]+", al))) and len(aw & bw) >= 1:
        return f"{a.name} looks like a compatibility/patch package for {b.name}"
    if (_PATCH_WORDS & set(re.findall(r"[a-z-]+", bl))) and len(aw & bw) >= 1:
        return f"{b.name} looks like a compatibility/patch package for {a.name}"
    return ""


def _mods(store: Store) -> tuple[dict[int, ManagedMod], list[str]]:
    profile = store.active_profile
    by_name_corr, by_path_corr = _load_correlation_ids(store.game_root)
    rows = store.conn.execute(
        "SELECT m.id,m.name,m.source_archive,COALESCE(pm.enabled,m.enabled_default) enabled "
        "FROM mods m LEFT JOIN profile_mods pm ON pm.mod_id=m.id AND pm.profile=? ORDER BY m.id",
        (profile,),
    ).fetchall()
    out: dict[int, ManagedMod] = {}
    warnings: list[str] = []
    for row in rows:
        mod = ManagedMod(int(row["id"]), str(row["name"]), str(row["source_archive"] or ""), bool(row["enabled"]))
        texts = [mod.name, mod.source_archive]
        mids: set[str] = set()
        for text in texts:
            mids.update(extract_mod_ids(text))
        # Archive Management links are explicit user evidence and outrank filename guesses.
        linked_rows = store.conn.execute(
            "SELECT DISTINCT al.nexus_mod_id FROM archive_installs ai "
            "JOIN archive_library al ON al.id=ai.archive_id "
            "WHERE ai.mod_id=? AND al.nexus_mod_id IS NOT NULL",
            (mod.id,),
        ).fetchall()
        linked_ids = {str(int(x[0])) for x in linked_rows if x[0] is not None}
        if len(linked_ids) == 1:
            mids = set(linked_ids)
        elif linked_ids:
            mids.update(linked_ids)
        if not mids:
            corr = by_name_corr.get(mod.name.casefold())
            if corr:
                mids.add(corr)
        frows = store.conn.execute(
            "SELECT rel_path,stage_rel,projected_rel,sha256,size,redmod_mode FROM files WHERE mod_id=? ORDER BY rel_path",
            (mod.id,),
        ).fetchall()
        for fr in frows:
            d = dict(fr)
            mod.files.append(d)
            if not mids:
                corr = by_path_corr.get(str(fr["projected_rel"]).casefold()) or by_path_corr.get(str(fr["rel_path"]).casefold())
                if corr:
                    mids.add(corr)
            if str(fr["projected_rel"]).lower().endswith(".archive"):
                stage = store.root / str(fr["stage_rel"])
                mod.archives.append({
                    "name": PurePosixPath(str(fr["projected_rel"])).name,
                    "projected_rel": str(fr["projected_rel"]),
                    "stage_rel": str(fr["stage_rel"]),
                    "stage_path": str(stage),
                })
        if len(mids) == 1:
            mod.nexus_id = next(iter(mids))
        elif len(mids) > 1:
            warnings.append(f"{mod.name}: multiple Nexus IDs inferred ({', '.join(sorted(mids))}); using search links")
        mod.nexus_url = _nexus_url(mod.nexus_id, mod.name)
        for arc in mod.archives:
            arc["nexus_id"] = mod.nexus_id
            arc["nexus_url"] = mod.nexus_url
        out[mod.id] = mod
    return out, warnings


def _mod_payload(mod: ManagedMod) -> dict[str, Any]:
    archives = mod.archives or [{
        "name": Path(mod.source_archive).name if mod.source_archive else mod.name,
        "projected_rel": "",
        "nexus_id": mod.nexus_id,
        "nexus_url": mod.nexus_url,
    }]
    return {
        "id": mod.id,
        "name": mod.name,
        "enabled": mod.enabled,
        "source_archive": mod.source_archive,
        "nexus_id": mod.nexus_id,
        "nexus_url": mod.nexus_url,
        "archives": [{k: a.get(k, "") for k in ("name", "projected_rel", "nexus_id", "nexus_url")} for a in archives],
    }


def _dependency_relations(mods: dict[int, ManagedMod], api_key: str, warnings: list[str]) -> dict[frozenset[int], str]:
    rel: dict[frozenset[int], str] = {}
    enabled = [m for m in mods.values() if m.enabled]
    for a, b in itertools.combinations(enabled, 2):
        reason = _patch_relation(a, b)
        if reason:
            rel[frozenset((a.id, b.id))] = reason
    if not api_key:
        return rel
    by_nexus = {m.nexus_id: m for m in enabled if m.nexus_id}
    if not by_nexus:
        return rel
    try:
        rows = NexusGraphQLClient(api_key).batch_mods(GAME_DOMAIN, by_nexus, requirements=True)
    except NexusGraphQLError as exc:
        warnings.append(f"Nexus dependency lookup: {exc}")
        return rel
    for mid, meta in rows.items():
        dependent = by_nexus.get(str(mid))
        if not dependent:
            continue
        reqs = ((meta.get("modRequirements") or {}).get("nexusRequirements") or {}).get("nodes") or []
        for req in reqs:
            try:
                rid = str(int(req.get("modId")))
            except (TypeError, ValueError):
                continue
            required = by_nexus.get(rid)
            if required and required.id != dependent.id:
                rel[frozenset((dependent.id, required.id))] = (
                    f"Nexus requirement: {dependent.name} requires {required.name}"
                )
    return rel


def _relation(mod_ids: Iterable[int], relations: dict[frozenset[int], str]) -> str:
    reasons = []
    for a, b in itertools.combinations(sorted(set(mod_ids)), 2):
        r = relations.get(frozenset((a, b)))
        if r:
            reasons.append(r)
    return "; ".join(dict.fromkeys(reasons))


def _severity(base: str, dependency_context: str) -> str:
    if not dependency_context:
        return base
    # Expected patch/dependency overlaps still deserve review, but should not be
    # presented with the same urgency as two unrelated mods silently fighting.
    return {"critical": "high", "high": "medium", "medium": "low"}.get(base, base)


def _file_overlap_conflicts(store: Store, mods: dict[int, ManagedMod], relations: dict[frozenset[int], str]) -> list[dict]:
    profile = store.active_profile
    rows = store.conn.execute(
        "SELECT f.projected_rel,f.rel_path,f.mod_id,m.name,f.sha256 FROM files f "
        "JOIN mods m ON m.id=f.mod_id JOIN profile_mods pm ON pm.mod_id=m.id "
        "AND pm.profile=? AND pm.enabled=1 ORDER BY f.projected_rel,m.id",
        (profile,),
    ).fetchall()
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row["projected_rel"]), []).append(row)
    out = []
    for rel, owners in grouped.items():
        mids = list(dict.fromkeys(int(r["mod_id"]) for r in owners))
        if len(mids) < 2:
            continue
        context = _relation(mids, relations)
        ext = PurePosixPath(rel).suffix.casefold()
        base = "critical" if ext in {".dll", ".asi", ".reds", ".lua"} else "high"
        sev = _severity(base, context)
        rule = store.conn.execute(
            "SELECT preferred_mod_id FROM conflict_rules WHERE profile=? AND rel_path=?", (profile, rel)
        ).fetchone()
        preferred = int(rule[0]) if rule is not None and rule[0] is not None else mids[-1]
        payload_mods = [_mod_payload(mods[mid]) for mid in mids]
        out.append({
            "id": _stable_id("file-overlap", rel, *(str(x) for x in mids)),
            "kind": "file-overlap",
            "severity": sev,
            "title": f"{len(mids)} mods deploy the same file",
            "detail": f"Only one staged owner can be projected at {rel}.",
            "mods": payload_mods,
            "paths": [rel],
            "preferred_mod_id": preferred,
            "dependency_context": context,
            "evidence": {"projected_path": rel, "owners": len(mids)},
        })
    return out


def _archive_resource_conflicts(store: Store, mods: dict[int, ManagedMod], relations: dict[frozenset[int], str], warnings: list[str]) -> list[dict]:
    archives: list[tuple[ManagedMod, dict[str, Any], ArchiveIndex]] = []
    for mod in mods.values():
        if not mod.enabled:
            continue
        for arc in mod.archives:
            idx = read_archive_index(Path(str(arc["stage_path"])))
            if idx.warning:
                warnings.append(f"{mod.name}/{arc['name']}: archive index unavailable: {idx.warning}")
                continue
            archives.append((mod, arc, idx))
    out = []
    for (ma, aa, ia), (mb, ab, ib) in itertools.combinations(archives, 2):
        if ma.id == mb.id:
            continue
        shared = ia.resource_hashes & ib.resource_hashes
        if not shared:
            continue
        context = _relation((ma.id, mb.id), relations)
        count = len(shared)
        base = "critical" if count >= 25 else "high"
        sev = _severity(base, context)
        named = []
        for h in sorted(shared)[:24]:
            name = ia.names.get(h) or ib.names.get(h)
            named.append(name or f"hash:{h:016x}")
        out.append({
            "id": _stable_id("archive-resource", f"{ma.id}:{aa['projected_rel']}", f"{mb.id}:{ab['projected_rel']}"),
            "kind": "archive-resource",
            "severity": sev,
            "title": f"Archive resources overlap ({count})",
            "detail": (
                f"{aa['name']} and {ab['name']} contain {count} identical REDengine resource path hash(es). "
                "The archive loaded first wins each contested resource."
            ),
            "mods": [_mod_payload(ma), _mod_payload(mb)],
            "paths": [str(aa["projected_rel"]), str(ab["projected_rel"])],
            "dependency_context": context,
            "resources": named,
            "resource_count": count,
            "evidence": {
                "left_archive": aa["name"], "right_archive": ab["name"],
                "shared_resource_hashes": count, "sample": named,
            },
        })
    return out


def parse_redscript_annotations(text: str) -> list[tuple[str, str, str]]:
    """Return ``(annotation, target, member)`` triples from live redscript code."""
    body = _REDS_COMMENT_RE.sub(" ", text)
    out: list[tuple[str, str, str]] = []
    for m in _REDS_ANNOT_RE.finditer(body):
        annotation = m.group(1).lower()
        target = (m.group(2) or "").strip() or "<global>"
        nxt = _REDS_FUNC_RE.search(body, m.end(), m.end() + 500)
        if nxt:
            out.append((annotation, target, nxt.group(1)))
    for m in _REDS_FIELD_ANNOT_RE.finditer(body):
        target = (m.group(1) or "").strip() or "<global>"
        nxt = _REDS_FIELD_RE.search(body, m.end(), m.end() + 350)
        if nxt:
            out.append(("addfield", target, nxt.group(1)))
    return out


def _redscript_conflicts(store: Store, mods: dict[int, ManagedMod], relations: dict[frozenset[int], str]) -> list[dict]:
    targets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for mod in mods.values():
        if not mod.enabled:
            continue
        for f in mod.files:
            rel = str(f["rel_path"])
            if not rel.casefold().endswith(".reds"):
                continue
            path = store.root / str(f["stage_rel"])
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for ann, cls, member in parse_redscript_annotations(text):
                targets.setdefault((cls, member), []).append({
                    "annotation": ann, "mod_id": mod.id, "mod": mod, "path": rel,
                })
    out = []
    for (cls, member), entries in targets.items():
        mids = sorted({int(e["mod_id"]) for e in entries})
        if len(mids) < 2:
            continue
        anns = {str(e["annotation"]) for e in entries}
        # Multiple wrappers intentionally chain. A wrapper coexisting with a replacement
        # is also normal; only competing outright definitions are an inbox conflict.
        dangerous = [e for e in entries if e["annotation"] in {"replacemethod", "replaceglobal", "addmethod", "addfield"}]
        dmids = sorted({int(e["mod_id"]) for e in dangerous})
        if len(dmids) < 2:
            continue
        context = _relation(dmids, relations)
        sev = _severity("critical", context)
        label = f"{cls}::{member}" if cls != "<global>" else f"{member}()"
        out.append({
            "id": _stable_id("redscript", cls, member, *(str(x) for x in dmids)),
            "kind": "redscript",
            "severity": sev,
            "title": f"Redscript annotation collision: {label}",
            "detail": "Multiple enabled mods define an outright replacement/addition for the same redscript symbol.",
            "mods": [_mod_payload(mods[mid]) for mid in dmids],
            "paths": sorted({str(e["path"]) for e in dangerous}),
            "dependency_context": context,
            "annotations": sorted(anns),
            "evidence": {"target": label, "annotations": sorted(anns)},
        })
    return out


def _yaml_node_keys(node: Any, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if yaml is None:
        return out
    if isinstance(node, MappingNode):
        for key_node, value_node in node.value:
            key = str(getattr(key_node, "value", "")).strip()
            if not key:
                continue
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value_node, MappingNode):
                nested = _yaml_node_keys(value_node, path)
                out.extend(nested or [(path, "direct")])
            elif isinstance(value_node, SequenceNode):
                tags = [str(getattr(x, "tag", "")).casefold() for x in value_node.value]
                mutation = bool(tags) and all(any(op in tag for op in ("append", "prepend", "remove")) for tag in tags)
                out.append((path, "mutation" if mutation else "direct"))
                for child in value_node.value:
                    if isinstance(child, MappingNode):
                        out.extend(_yaml_node_keys(child, path))
            else:
                out.append((path, "direct"))
    return out


def parse_tweak_keys(text: str, suffix: str) -> list[tuple[str, str]]:
    suffix = suffix.casefold()
    if suffix == ".tweak":
        return [(m.group(1), "mutation" if m.group(2) in {"+=", "-="} else "direct") for m in _TWEAK_ASSIGN_RE.finditer(text)]
    if suffix == ".json":
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return []
        out: list[tuple[str, str]] = []
        def walk(value: Any, prefix: str = "") -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    p = f"{prefix}.{k}" if prefix else str(k)
                    if isinstance(v, dict):
                        walk(v, p)
                    else:
                        out.append((p, "direct"))
        walk(data)
        return out
    if yaml is not None:
        try:
            docs = list(yaml.compose_all(text))
            out: list[tuple[str, str]] = []
            for doc in docs:
                if doc is not None:
                    out.extend(_yaml_node_keys(doc))
            return out
        except Exception:  # noqa: BLE001
            pass
    # Conservative fallback: top-level/nested YAML mapping keys by indentation.
    stack: list[tuple[int, str]] = []
    out = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or ":" not in raw:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key = raw.strip().split(":", 1)[0].strip().strip("'\"")
        if not key or key.startswith("-"):
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        prefix = ".".join(x[1] for x in stack)
        path = f"{prefix}.{key}" if prefix else key
        out.append((path, "direct"))
        stack.append((indent, key))
    return out


def _tweakxl_conflicts(store: Store, mods: dict[int, ManagedMod], relations: dict[frozenset[int], str], warnings: list[str]) -> list[dict]:
    keys: dict[str, list[dict[str, Any]]] = {}
    for mod in mods.values():
        if not mod.enabled:
            continue
        for f in mod.files:
            rel = str(f["rel_path"])
            low = rel.casefold()
            suffix = PurePosixPath(low).suffix
            if suffix not in {".yaml", ".yml", ".json", ".tweak"}:
                continue
            # TweakXL consumes r6/tweaks and REDmod tweak payloads. Avoid treating random
            # JSON/YAML configuration files elsewhere as TweakDB writes.
            if not (low.startswith("r6/tweaks/") or (low.startswith("mods/") and "/tweaks/" in low)):
                continue
            path = store.root / str(f["stage_rel"])
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                warnings.append(f"{mod.name}/{rel}: cannot read TweakXL source: {exc}")
                continue
            for key, operation in parse_tweak_keys(text, suffix):
                if key.startswith("$"):
                    continue
                keys.setdefault(key, []).append({"mod_id": mod.id, "mod": mod, "path": rel, "operation": operation})
    out = []
    for key, entries in keys.items():
        mids = sorted({int(e["mod_id"]) for e in entries})
        if len(mids) < 2:
            continue
        ops = {str(e["operation"]) for e in entries}
        context = _relation(mids, relations)
        base = "high" if "direct" in ops else "medium"
        sev = _severity(base, context)
        out.append({
            "id": _stable_id("tweakxl", key, *(str(x) for x in mids)),
            "kind": "tweakxl",
            "severity": sev,
            "title": f"TweakXL key modified by {len(mids)} mods",
            "detail": f"Multiple enabled tweak files touch {key}. Direct assignments can override each other; array mutations are usually more composable.",
            "mods": [_mod_payload(mods[mid]) for mid in mids],
            "paths": sorted({str(e["path"]) for e in entries}),
            "dependency_context": context,
            "tweak_key": key,
            "operations": sorted(ops),
            "evidence": {"key": key, "operations": sorted(ops)},
        })
    return out


def _sync_inbox(store: Store, conflicts: list[dict[str, Any]]) -> None:
    profile = store.active_profile
    now = time.time()
    ids = {str(c["id"]) for c in conflicts}
    with store.conn:
        store.conn.execute("UPDATE conflict_inbox SET active=0 WHERE profile=?", (profile,))
        for conflict in conflicts:
            cid = str(conflict["id"])
            payload = json.dumps(conflict, ensure_ascii=False, sort_keys=True)
            old = store.conn.execute("SELECT status,resolution,first_seen FROM conflict_inbox WHERE id=?", (cid,)).fetchone()
            status = str(old["status"]) if old else "open"
            resolution = str(old["resolution"]) if old else ""
            first_seen = float(old["first_seen"]) if old else now
            store.conn.execute(
                "INSERT INTO conflict_inbox(id,profile,kind,severity,title,payload_json,status,resolution,active,first_seen,last_seen,resolved_at) "
                "VALUES(?,?,?,?,?,?,?,?,1,?,?,NULL) "
                "ON CONFLICT(id) DO UPDATE SET profile=excluded.profile,kind=excluded.kind,severity=excluded.severity," 
                "title=excluded.title,payload_json=excluded.payload_json,active=1,last_seen=excluded.last_seen",
                (cid, profile, conflict["kind"], conflict["severity"], conflict["title"], payload,
                 status, resolution, first_seen, now),
            )
        if ids:
            marks = ",".join("?" for _ in ids)
            # Conflicts that vanished after a direct action are retained for audit but no longer active.
            store.conn.execute(
                f"UPDATE conflict_inbox SET active=0 WHERE profile=? AND id NOT IN ({marks})",
                (profile, *ids),
            )


def inbox(store: Store, *, include_inactive: bool = False, include_resolved: bool = True) -> dict[str, Any]:
    where = ["profile=?"]
    args: list[Any] = [store.active_profile]
    if not include_inactive:
        where.append("active=1")
    if not include_resolved:
        where.append("status='open'")
    rows = store.conn.execute(
        "SELECT * FROM conflict_inbox WHERE " + " AND ".join(where), args
    ).fetchall()
    items = []
    for row in rows:
        try:
            item = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            item = {"id": row["id"], "kind": row["kind"], "severity": row["severity"], "title": row["title"]}
        item["status"] = row["status"]
        item["resolution"] = row["resolution"]
        item["active"] = bool(row["active"])
        item["first_seen"] = row["first_seen"]
        item["last_seen"] = row["last_seen"]
        items.append(item)
    items.sort(key=lambda x: (SEVERITY_RANK.get(str(x.get("severity")), 9), str(x.get("kind")), str(x.get("title")).casefold()))
    counts: dict[str, int] = {}
    for item in items:
        sev = str(item.get("severity") or "info")
        counts[sev] = counts.get(sev, 0) + 1
    return {"profile": store.active_profile, "count": len(items), "severity": counts, "items": items}


def scan(store: Store, *, api_key: str = "") -> dict[str, Any]:
    mods, warnings = _mods(store)
    relations = _dependency_relations(mods, api_key, warnings)
    conflicts = []
    conflicts.extend(_file_overlap_conflicts(store, mods, relations))
    conflicts.extend(_archive_resource_conflicts(store, mods, relations, warnings))
    conflicts.extend(_redscript_conflicts(store, mods, relations))
    conflicts.extend(_tweakxl_conflicts(store, mods, relations, warnings))
    conflicts.sort(key=lambda x: (SEVERITY_RANK.get(str(x.get("severity")), 9), str(x.get("kind")), str(x.get("title")).casefold()))
    _sync_inbox(store, conflicts)
    result = inbox(store)
    result.update({
        "type": "scan",
        "mods": len(mods),
        "enabled_mods": sum(1 for m in mods.values() if m.enabled),
        "warnings": warnings,
        "dependency_relations": len(relations),
    })
    return result


def _conflict_row(store: Store, conflict_id: str) -> tuple[Any, dict[str, Any]]:
    row = store.conn.execute("SELECT * FROM conflict_inbox WHERE id=? AND profile=?", (conflict_id, store.active_profile)).fetchone()
    if not row:
        raise KeyError(f"conflict not found in active-profile inbox: {conflict_id}")
    payload = json.loads(row["payload_json"])
    return row, payload


def _resolve(store: Store, conflict_id: str, resolution: str) -> None:
    with store.conn:
        store.conn.execute(
            "UPDATE conflict_inbox SET status='resolved',resolution=?,resolved_at=? WHERE id=? AND profile=?",
            (resolution, time.time(), conflict_id, store.active_profile),
        )


def action_disable(store: Store, conflict_id: str, mod_name: str) -> dict[str, Any]:
    _row, payload = _conflict_row(store, conflict_id)
    names = {str(m.get("name")) for m in payload.get("mods") or []}
    if mod_name not in names:
        raise ValueError(f"{mod_name!r} is not part of conflict {conflict_id}")
    store.set_enabled(mod_name, False)
    deploy = store.deploy()
    _resolve(store, conflict_id, f"disabled {mod_name}")
    return {"action": "disable", "conflict_id": conflict_id, "mod": mod_name, "deploy": deploy}


def action_uninstall(store: Store, conflict_id: str, mod_name: str) -> dict[str, Any]:
    _row, payload = _conflict_row(store, conflict_id)
    names = {str(m.get("name")) for m in payload.get("mods") or []}
    if mod_name not in names:
        raise ValueError(f"{mod_name!r} is not part of conflict {conflict_id}")
    result = store.uninstall_mod(mod_name)
    _resolve(store, conflict_id, f"uninstalled {mod_name}")
    return {"action": "uninstall", "conflict_id": conflict_id, "mod": mod_name, "result": result}


def action_prefer(store: Store, conflict_id: str, mod_name: str) -> dict[str, Any]:
    _row, payload = _conflict_row(store, conflict_id)
    mods = [str(m.get("name")) for m in payload.get("mods") or [] if m.get("name")]
    if mod_name not in mods:
        raise ValueError(f"{mod_name!r} is not part of conflict {conflict_id}")
    kind = str(payload.get("kind") or "")
    if kind == "file-overlap":
        winner = store._mod_row(mod_name)
        if not winner:
            raise KeyError(f"managed mod not found: {mod_name}")
        with store.conn:
            for rel in payload.get("paths") or []:
                store.conn.execute(
                    "INSERT OR REPLACE INTO conflict_rules(profile,rel_path,preferred_mod_id) VALUES(?,?,?)",
                    (store.active_profile, str(rel), int(winner["id"])),
                )
        policy = {"type": "file-owner", "winner": mod_name, "paths": payload.get("paths") or []}
    elif kind == "archive-resource":
        order = [mod_name] + [name for name in mods if name != mod_name]
        store.set_load_order(order)
        policy = {"type": "archive-load-order", "order": order}
    else:
        raise ValueError(f"load-order preference is not a safe resolver for {kind} conflicts")
    deploy = store.deploy()
    _resolve(store, conflict_id, f"preferred {mod_name}")
    return {"action": "prefer", "conflict_id": conflict_id, "mod": mod_name, "policy": policy, "deploy": deploy}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Smart-deployment multi-layer conflict engine")
    sub = ap.add_subparsers(dest="command", required=True)

    def base(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--game-root", type=Path, required=True)
        return p

    p = base("scan", "Scan staged enabled mods and synchronize the conflict inbox")
    p.add_argument("--api-key", default=os.environ.get("NEXUS_API_KEY", ""), help=argparse.SUPPRESS)
    p = base("inbox", "Read the synchronized conflict inbox")
    p.add_argument("--all", action="store_true", help="Include inactive historical conflicts")
    p.add_argument("--open-only", action="store_true", help="Hide rows already resolved by an action")
    for cmd in ("disable", "uninstall", "prefer"):
        p = base(cmd, f"{cmd.title()} a mod directly from an inbox conflict")
        p.add_argument("--conflict-id", required=True)
        p.add_argument("--mod", required=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.game_root.expanduser().resolve()
    try:
        if not root.is_dir():
            raise FileNotFoundError(f"game root not found: {root}")
        readonly = args.command == "inbox"
        with deployment_lock(root, exclusive=not readonly):
            with Store(root) as store:
                if args.command == "scan":
                    result = scan(store, api_key=args.api_key.strip())
                elif args.command == "inbox":
                    result = inbox(store, include_inactive=args.all, include_resolved=not args.open_only)
                    result["type"] = "inbox"
                elif args.command == "disable":
                    result = action_disable(store, args.conflict_id, args.mod)
                elif args.command == "uninstall":
                    result = action_uninstall(store, args.conflict_id, args.mod)
                elif args.command == "prefer":
                    result = action_prefer(store, args.conflict_id, args.mod)
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
