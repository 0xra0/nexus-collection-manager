#!/usr/bin/env python3
"""Multi-tier local-mod -> Nexus Mods correlation engine.

The engine treats scanner groups as hypotheses, never ownership facts. Automatic matches are
ranked with explicit evidence and confidence. User decisions (confirm/reject/reassign) are
stored outside the game directory and survive later scans through stable group fingerprints.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import mod_scanner
from .nexus_account import (
    GAME_DOMAIN,
    NexusAccountClient,
    NexusAPIError,
    mod_ids_from_account_payload,
    resolve_api_key,
)
from .nexus_graphql import NexusGraphQLClient, NexusGraphQLError

SCHEMA_VERSION = 1
GAME_ID = 3333
CONFIG_DIR = Path.home() / ".config" / "cp2077_modder"
STATE_DIR = CONFIG_DIR / "correlation"
ARCHIVE_EXTS = (".zip", ".7z", ".rar")

SOURCE_BASE = {
    "manual": 1.00,
    "filename-id": 0.99,
    "md5": 1.00,
    "content": 0.91,
    "account": 0.78,
    "collection": 0.84,
    "requirement": 0.82,
    "fuzzy": 0.52,
}
SOURCE_ORDER = {name: i for i, name in enumerate(
    ("manual", "md5", "filename-id", "content", "collection", "requirement", "account", "fuzzy")
)}
GENERIC = {
    "mod", "mods", "main", "file", "files", "archive", "patch", "optional", "version", "release",
    "cyberpunk", "cyberpunk2077", "cp2077", "update", "updated", "installer", "final", "latest",
    "redmod", "cet", "redscript", "tweakxl", "archivexl", "red4ext", "r6", "script", "scripts",
    "bin", "x64", "plugin", "plugins", "pc", "download", "downloads",
}
ProgressFn = Callable[[str, int, int, str], None]


@dataclass
class Evidence:
    source: str
    confidence: float
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "confidence": round(self.confidence, 4), "detail": self.detail}


@dataclass
class Candidate:
    mod_id: str
    meta: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)

    def add(self, source: str, confidence: float, detail: str = "") -> None:
        confidence = max(0.0, min(1.0, float(confidence)))
        # Keep one strongest item per source so repeated content paths do not inflate scores without bound.
        for idx, item in enumerate(self.evidence):
            if item.source == source:
                if confidence > item.confidence:
                    self.evidence[idx] = Evidence(source, confidence, detail)
                return
        self.evidence.append(Evidence(source, confidence, detail))

    @property
    def confidence(self) -> float:
        if not self.evidence:
            return 0.0
        strongest = max(x.confidence for x in self.evidence)
        # Independent corroboration gives a modest boost, while exact evidence remains capped at 1.
        corroboration = 0.025 * max(0, len({x.source for x in self.evidence}) - 1)
        return min(1.0, strongest + corroboration)

    @property
    def primary_source(self) -> str:
        if not self.evidence:
            return ""
        return min(
            self.evidence,
            key=lambda x: (-x.confidence, SOURCE_ORDER.get(x.source, 99)),
        ).source

    def as_dict(self) -> dict[str, Any]:
        meta = self.meta or {}
        return {
            "mod_id": self.mod_id,
            "name": str(meta.get("name") or f"Nexus Mod #{self.mod_id}"),
            "author": str(meta.get("author") or ""),
            "version": str(meta.get("version") or ""),
            "summary": str(meta.get("summary") or ""),
            "picture_url": str(meta.get("pictureUrl") or meta.get("picture_url") or ""),
            "endorsements": _int(meta.get("endorsements")),
            "downloads": _int(meta.get("downloads")),
            "confidence": round(self.confidence, 4),
            "source": self.primary_source,
            "evidence": [e.as_dict() for e in sorted(self.evidence, key=lambda e: -e.confidence)],
        }


@dataclass
class LocalGroup:
    key: str
    scanner_id: str
    name: str
    files: list[str]
    relpaths: list[str]
    md5s: list[str]
    candidates: dict[str, Candidate] = field(default_factory=dict)

    def add_candidate(self, mod_id: int | str, source: str, confidence: float, detail: str = "",
                      meta: dict[str, Any] | None = None) -> Candidate | None:
        try:
            mid = str(int(mod_id))
        except (TypeError, ValueError):
            return None
        if int(mid) <= 0:
            return None
        cand = self.candidates.setdefault(mid, Candidate(mid))
        if meta:
            cand.meta.update(meta)
        cand.add(source, confidence, detail)
        return cand


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def default_state_path(game_root: Path) -> Path:
    root = str(game_root.expanduser().resolve(strict=False))
    digest = hashlib.sha1(root.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", game_root.name or "game")[:48] or "game"
    return STATE_DIR / f"{safe}-{digest}.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("decisions", {})
            data.setdefault("results", [])
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"version": SCHEMA_VERSION, "decisions": {}, "results": []}


def _canonical_rel(game_root: Path, path: str, info: dict[str, Any]) -> str:
    p = Path(path)
    try:
        return p.resolve(strict=False).relative_to(game_root).as_posix().lower()
    except ValueError:
        root = str(info.get("root") or "")
        rel = str(info.get("relative_path") or p.name)
        return (root + "::" + rel).replace("\\", "/").lower()


def group_key(game_root: Path, group: dict[str, Any], files: dict[str, dict]) -> str:
    members = []
    for path in group.get("files") or []:
        members.append(_canonical_rel(game_root, path, files.get(path) or {}))
    payload = "\n".join(sorted(set(members), key=str.casefold))
    return hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()[:20]


def groups_from_snapshot(snapshot: dict[str, Any], game_root: Path) -> list[LocalGroup]:
    files = snapshot.get("files") or {}
    result: list[LocalGroup] = []
    for raw in snapshot.get("groups") or []:
        paths = [str(x) for x in raw.get("files") or [] if str(x) in files]
        relpaths = [str((files[p] or {}).get("relative_path") or Path(p).name).replace("\\", "/") for p in paths]
        md5s = [str((files[p] or {}).get("md5") or "").lower() for p in paths]
        md5s = [x for x in md5s if re.fullmatch(r"[0-9a-f]{32}", x)]
        result.append(LocalGroup(
            key=group_key(game_root, raw, files),
            scanner_id=str(raw.get("id") or ""),
            name=str(raw.get("name") or "Unnamed Mod"),
            files=paths,
            relpaths=relpaths,
            md5s=md5s,
        ))
    return result


def normalize_name(text: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text))
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b(?:v?\d+(?:[._-]\d+){1,4}|\d{9,})\b", " ", text, flags=re.I)
    words = re.findall(r"[a-z0-9]+", text.lower())
    words = [w for w in words if len(w) > 1 and w not in GENERIC and not w.isdigit()]
    return " ".join(words)


def name_tokens(text: str) -> set[str]:
    return set(normalize_name(text).split())


def jaccard_similarity(a: str, b: str) -> float:
    aa, bb = name_tokens(a), name_tokens(b)
    if not aa and not bb:
        return 1.0
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def jaro_winkler_similarity(a: str, b: str) -> float:
    s1, s2 = normalize_name(a), normalize_name(b)
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    match_distance = max(len(s1), len(s2)) // 2 - 1
    match_distance = max(0, match_distance)
    m1 = [False] * len(s1); m2 = [False] * len(s2)
    matches = 0
    for i, ch in enumerate(s1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len(s2))
        for j in range(start, end):
            if not m2[j] and ch == s2[j]:
                m1[i] = True; m2[j] = True; matches += 1; break
    if not matches:
        return 0.0
    seq1 = [s1[i] for i in range(len(s1)) if m1[i]]
    seq2 = [s2[j] for j in range(len(s2)) if m2[j]]
    transpositions = sum(x != y for x, y in zip(seq1, seq2)) / 2.0
    jaro = (matches / len(s1) + matches / len(s2) + (matches - transpositions) / matches) / 3.0
    prefix = 0
    for x, y in zip(s1[:4], s2[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1.0 - jaro) if jaro > 0.7 else jaro


def fuzzy_similarity(local: str, remote: str) -> tuple[float, float, float]:
    jac = jaccard_similarity(local, remote)
    jw = jaro_winkler_similarity(local, remote)
    return jac, jw, 0.55 * jac + 0.45 * jw


# Strong patterns first. The generic archive convention requires surrounding separators and
# at least 3 digits, preventing version numbers such as 2.12 from becoming mod IDs.
_ID_PATTERNS = (
    re.compile(r"nexusmods\.com/(?:[^/]+/)?mods/(\d+)", re.I),
    re.compile(r"\bmod[_ -]?id[_ -]?(\d{2,9})\b", re.I),
    re.compile(r"(?:^|[-_. ])([1-9]\d{2,8})(?=[-_. ](?:v?\d|\d{9,})|[-_. ]|$)", re.I),
)


def extract_mod_ids(text: str) -> set[str]:
    out: set[str] = set()
    for pattern in _ID_PATTERNS:
        for value in pattern.findall(str(text)):
            try:
                n = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 999_999_999:
                out.add(str(n))
    return out


def _candidate_mod(hit: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    mf = hit.get("modFile") if isinstance(hit, dict) else None
    mod = mf.get("mod") if isinstance(mf, dict) else None
    if not isinstance(mod, dict):
        return None, {}
    mid = mod.get("modId")
    try:
        return str(int(mid)), mod
    except (TypeError, ValueError):
        return None, {}


def _best_archive_group(archive_name: str, mod_name: str, groups: list[LocalGroup]) -> tuple[LocalGroup | None, float]:
    base = Path(archive_name).stem
    best = (None, 0.0)
    for group in groups:
        _, _, score1 = fuzzy_similarity(group.name, base)
        _, _, score2 = fuzzy_similarity(group.name, mod_name)
        score = max(score1, score2)
        if score > best[1]:
            best = (group, score)
    return best


def _hash_md5(path: Path) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _archive_rows(downloads: Path | None) -> list[dict[str, Any]]:
    if not downloads or not downloads.is_dir():
        return []
    rows = []
    for path in sorted(downloads.iterdir(), key=lambda p: p.name.casefold()):
        if not path.is_file() or not path.name.lower().endswith(ARCHIVE_EXTS):
            continue
        try:
            rows.append({"path": path, "name": path.name, "md5": _hash_md5(path)})
        except OSError:
            continue
    return rows


def _manifest_group_links(game_root: Path, groups: list[LocalGroup]) -> tuple[dict[str, LocalGroup], dict[str, list[str]]]:
    """Map manifest archive stems to scanner groups through deployed relative paths."""
    try:
        from .deployer import load_manifest
        mods = (load_manifest(game_root).get("mods") or {})
    except Exception:  # noqa: BLE001
        mods = {}
    path_to_group: dict[str, LocalGroup] = {}
    root = game_root.resolve(strict=False)
    for group in groups:
        for p in group.files:
            try:
                rel = Path(p).resolve(strict=False).relative_to(root).as_posix().lower()
                path_to_group[rel] = group
            except ValueError:
                pass
    stem_groups: dict[str, LocalGroup] = {}
    group_stems: dict[str, list[str]] = defaultdict(list)
    for stem, paths in mods.items():
        if not isinstance(paths, list):
            continue
        owners = [path_to_group.get(str(rel).replace("\\", "/").lower()) for rel in paths]
        owners = [x for x in owners if x]
        if not owners:
            continue
        owner = max(set(x.key for x in owners), key=lambda k: sum(1 for x in owners if x.key == k))
        group = next(x for x in groups if x.key == owner)
        stem_groups[str(stem)] = group
        group_stems[group.key].append(str(stem))
    return stem_groups, group_stems


def _archive_group(archive: Path, stem_groups: dict[str, LocalGroup], groups: list[LocalGroup], mod_name: str = "") -> tuple[LocalGroup | None, float]:
    astem = archive.stem.casefold()
    # Exact/near manifest archive name is the strongest local association.
    matches = [(stem, grp) for stem, grp in stem_groups.items()
               if astem == stem.casefold() or astem.startswith(stem.casefold()) or stem.casefold().startswith(astem)]
    if matches:
        matches.sort(key=lambda x: len(x[0]), reverse=True)
        return matches[0][1], 1.0
    return _best_archive_group(archive.name, mod_name, groups)


def _distinctive_paths(group: LocalGroup, limit: int = 2) -> list[str]:
    generic_names = {"init.lua", "info.json", "config.json", "settings.json", "main.reds", "module.reds"}
    scored = []
    for rel in group.relpaths:
        clean = rel.replace("\\", "/")
        base = Path(clean).name.lower()
        depth = clean.count("/")
        score = depth * 2 + min(len(base), 30) / 10 + (0 if base in generic_names else 3)
        scored.append((score, clean))
    return [p for _, p in sorted(scored, reverse=True)[:limit]]


def _content_score(local_path: str, remote_path: str) -> float:
    a = local_path.replace("\\", "/").lower().strip("/")
    b = str(remote_path or "").replace("\\", "/").lower().strip("/")
    if not a or not b:
        return 0.0
    if a == b or a.endswith(b) or b.endswith(a):
        return 1.0
    at, bt = set(re.findall(r"[a-z0-9]+", a)), set(re.findall(r"[a-z0-9]+", b))
    return len(at & bt) / len(at | bt) if at and bt else 0.0


def _metadata_index(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            mid = str(int(row.get("modId")))
        except (TypeError, ValueError):
            continue
        out[mid] = row
    return out


def _enrich_candidates(groups: list[LocalGroup], metadata: dict[str, dict[str, Any]]) -> None:
    for group in groups:
        for mid, cand in group.candidates.items():
            if mid in metadata:
                cand.meta.update(metadata[mid])


def _unmatched(groups: list[LocalGroup], decisions: dict[str, Any], threshold: float = 0.94) -> list[LocalGroup]:
    out = []
    for group in groups:
        dec = decisions.get(group.key) or {}
        if dec.get("decision") in {"confirmed", "manual"} and dec.get("mod_id"):
            continue
        viable = [c for mid, c in group.candidates.items() if mid not in set(map(str, dec.get("rejected_mod_ids") or []))]
        if not viable or max(c.confidence for c in viable) < threshold:
            out.append(group)
    return out


def _apply_account_tier(groups: list[LocalGroup], tracked: set[str], endorsed: set[str], metadata: dict[str, dict]) -> None:
    pool = tracked | endorsed
    for group in groups:
        for mid in pool:
            meta = metadata.get(mid)
            if not meta:
                continue
            jac, jw, score = fuzzy_similarity(group.name, str(meta.get("name") or ""))
            if score < 0.58:
                continue
            both = mid in tracked and mid in endorsed
            confidence = min(0.92, 0.68 + 0.20 * score + (0.04 if both else 0.0))
            flags = "endorsed+tracked" if both else "endorsed" if mid in endorsed else "tracked"
            group.add_candidate(mid, "account", confidence,
                                f"{flags}; name similarity={score:.2f} (Jaccard={jac:.2f}, JW={jw:.2f})", meta)


def _apply_collection_tier(groups: list[LocalGroup], collections: list[dict[str, Any]]) -> dict[str, dict]:
    metadata: dict[str, dict] = {}
    for col in collections:
        rev = col.get("latestPublishedRevision") or {}
        cname = str(col.get("name") or col.get("slug") or "collection")
        for mf in rev.get("modFiles") or []:
            f = mf.get("file") or {}
            mod = f.get("mod") or {}
            try:
                mid = str(int(mod.get("modId")))
            except (TypeError, ValueError):
                continue
            metadata[mid] = mod
            for group in groups:
                jac, jw, score = fuzzy_similarity(group.name, str(mod.get("name") or f.get("name") or ""))
                if score >= 0.60:
                    conf = min(0.94, 0.74 + 0.20 * score)
                    group.add_candidate(mid, "collection", conf,
                                        f"{cname}; name similarity={score:.2f} (Jaccard={jac:.2f}, JW={jw:.2f})", mod)
    return metadata


def _top_candidate(group: LocalGroup, rejected: set[str] | None = None) -> Candidate | None:
    rejected = rejected or set()
    viable = [c for mid, c in group.candidates.items() if mid not in rejected]
    if not viable:
        return None
    return sorted(viable, key=lambda c: (-c.confidence, -_int(c.meta.get("endorsements")), int(c.mod_id)))[0]


def _apply_requirements(groups: list[LocalGroup], client: NexusGraphQLClient, decisions: dict[str, Any],
                        warnings: list[str], progress: ProgressFn | None = None) -> dict[str, dict]:
    metadata: dict[str, dict] = {}
    for round_no in range(2):
        anchors = []
        for group in groups:
            rejected = set(map(str, (decisions.get(group.key) or {}).get("rejected_mod_ids") or []))
            top = _top_candidate(group, rejected)
            if top and top.confidence >= 0.78:
                anchors.append(top.mod_id)
        ids = list(dict.fromkeys(anchors))
        if not ids:
            break
        try:
            details = client.batch_mods(GAME_DOMAIN, ids, requirements=True)
        except NexusGraphQLError as exc:
            warnings.append(f"requirement propagation skipped: {exc}")
            break
        for mid, row in details.items():
            metadata[str(mid)] = row
        unmatched = _unmatched(groups, decisions, threshold=0.90)
        added = 0
        for anchor_id, row in details.items():
            reqs = (((row.get("modRequirements") or {}).get("nexusRequirements") or {}).get("nodes") or [])
            for req in reqs:
                if not isinstance(req, dict) or req.get("externalRequirement"):
                    continue
                if req.get("gameId") not in (None, GAME_ID, str(GAME_ID)):
                    continue
                try:
                    rid = str(int(req.get("modId")))
                except (TypeError, ValueError):
                    continue
                rname = str(req.get("modName") or "")
                for group in unmatched:
                    _, _, score = fuzzy_similarity(group.name, rname)
                    if score < 0.58:
                        continue
                    conf = min(0.94, 0.76 + 0.20 * score)
                    if group.add_candidate(rid, "requirement", conf,
                                           f"required by Nexus mod #{anchor_id}; name similarity={score:.2f}"):
                        added += 1
        if progress:
            progress("requirements", round_no + 1, 2, f"round {round_no + 1}: {added} candidate links")
        if not added:
            break
    return metadata


def _decision_overlay(group: LocalGroup, decision: dict[str, Any]) -> tuple[Candidate | None, str]:
    rejected = set(map(str, decision.get("rejected_mod_ids") or []))
    chosen_id = str(decision.get("mod_id") or "")
    kind = str(decision.get("decision") or "")
    if chosen_id and kind in {"confirmed", "manual"}:
        cand = group.candidates.get(chosen_id) or Candidate(chosen_id)
        if kind == "manual":
            cand.add("manual", 1.0, "manually reassigned by user")
        group.candidates[chosen_id] = cand
        return cand, kind
    top = _top_candidate(group, rejected)
    if top:
        return top, "suggested"
    return None, "unmatched"


def _row_for(group: LocalGroup, decision: dict[str, Any]) -> dict[str, Any]:
    rejected = set(map(str, decision.get("rejected_mod_ids") or []))
    match, status = _decision_overlay(group, decision)
    alternatives = [c.as_dict() for c in sorted(
        (c for mid, c in group.candidates.items() if mid not in rejected),
        key=lambda c: (-c.confidence, -_int(c.meta.get("endorsements")), int(c.mod_id)),
    )[:8]]
    return {
        "group_key": group.key,
        "scanner_id": group.scanner_id,
        "local_name": group.name,
        "file_count": len(group.files),
        "files": group.relpaths,
        "status": status,
        "rejected_mod_ids": sorted(rejected, key=lambda x: int(x) if x.isdigit() else 10**12),
        "match": match.as_dict() if match else None,
        "alternatives": alternatives,
    }


def correlate(
    snapshot: dict[str, Any],
    game_root: Path,
    *,
    gql: NexusGraphQLClient | None,
    account: NexusAccountClient | None,
    downloads: Path | None = None,
    collections: Iterable[str] = (),
    decisions: dict[str, Any] | None = None,
    max_content_queries: int = 30,
    max_fuzzy_searches: int = 30,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Run the matching pipeline. Network tiers fail independently and append warnings."""
    game_root = game_root.resolve(strict=False)
    decisions = decisions or {}
    groups = groups_from_snapshot(snapshot, game_root)
    warnings: list[str] = []
    metadata: dict[str, dict] = {}
    stem_groups, group_stems = _manifest_group_links(game_root, groups)

    # Tier 1: filename/archive-stem ID extraction. Manifest stems are archive filenames retained
    # by this manager, so they provide useful identity even when deployed filenames are generic.
    for group in groups:
        texts = [group.name, *group.relpaths, *group_stems.get(group.key, [])]
        for text in texts:
            for mid in extract_mod_ids(text):
                group.add_candidate(mid, "filename-id", 0.99, f"ID #{mid} extracted from {text!r}")
    archives = _archive_rows(downloads)
    for arc in archives:
        grp, assoc = _archive_group(arc["path"], stem_groups, groups)
        if grp:
            for mid in extract_mod_ids(arc["name"]):
                grp.add_candidate(mid, "filename-id", min(0.99, 0.88 + 0.11 * assoc),
                                  f"ID #{mid} extracted from archive {arc['name']}")
    if progress:
        progress("filename", len(groups), len(groups), "filename ID extraction complete")

    if gql is not None:
        # Fetch metadata for exact ID candidates early.
        ids = {mid for g in groups for mid in g.candidates}
        if ids:
            try:
                rows = gql.batch_mods_basic(GAME_DOMAIN, ids)
                metadata.update({str(k): v for k, v in rows.items()})
                _enrich_candidates(groups, metadata)
            except NexusGraphQLError as exc:
                warnings.append(f"filename metadata lookup: {exc}")

        # Tier 2: MD5 batch lookup for both deployed files and retained archives.
        md5_to_groups: dict[str, list[tuple[LocalGroup, float, str]]] = defaultdict(list)
        for group in groups:
            for digest in group.md5s:
                md5_to_groups[digest].append((group, 1.0, "deployed file"))
        for arc in archives:
            grp, assoc = _archive_group(arc["path"], stem_groups, groups)
            if grp:
                md5_to_groups[arc["md5"]].append((grp, assoc, arc["name"]))
        try:
            hits = gql.batch_file_hashes(md5_to_groups)
            for hit in hits:
                digest = str(hit.get("md5") or "").lower()
                mid, mod = _candidate_mod(hit)
                if not mid or digest not in md5_to_groups:
                    continue
                if hit.get("gameId") not in (None, GAME_ID, str(GAME_ID)):
                    continue
                metadata[mid] = mod
                for group, assoc, origin in md5_to_groups[digest]:
                    conf = 1.0 if assoc >= 0.999 else min(0.98, 0.86 + 0.12 * assoc)
                    group.add_candidate(mid, "md5", conf, f"MD5 {digest[:10]}… matched {origin}", mod)
        except NexusGraphQLError as exc:
            warnings.append(f"MD5 lookup: {exc}")
        if progress:
            progress("md5", len(md5_to_groups), len(md5_to_groups), "MD5 batch lookup complete")

        # Tier 3: archive-content reverse lookup for still uncertain groups.
        targets = _unmatched(groups, decisions, threshold=0.94)
        queries = 0
        content_hits: dict[str, list[tuple[LocalGroup, float, str]]] = defaultdict(list)
        for group in targets:
            if queries >= max_content_queries:
                break
            for local_path in _distinctive_paths(group, 2):
                if queries >= max_content_queries:
                    break
                # Searching the final two path components is distinctive but tolerant of archive root folders.
                tail = "/".join(local_path.replace("\\", "/").split("/")[-2:])
                try:
                    rows = gql.search_file_contents(GAME_DOMAIN, file_path_wildcard=f"*{tail}*", count=20)
                except NexusGraphQLError as exc:
                    warnings.append(f"content lookup for {group.name}: {exc}")
                    queries = max_content_queries
                    break
                queries += 1
                for row in rows:
                    try:
                        mid = str(int(row.get("modId")))
                    except (TypeError, ValueError):
                        continue
                    score = _content_score(local_path, row.get("filePath") or row.get("fileName") or "")
                    if score >= 0.55:
                        content_hits[mid].append((group, score, local_path))
        if content_hits:
            try:
                rows = gql.batch_mods_basic(GAME_DOMAIN, content_hits)
                metadata.update({str(k): v for k, v in rows.items()})
            except NexusGraphQLError as exc:
                warnings.append(f"content metadata lookup: {exc}")
            by_pair: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
            for mid, hits2 in content_hits.items():
                for group, score, path in hits2:
                    by_pair[(group.key, mid)].append((score, path))
            gmap = {g.key: g for g in groups}
            for (gkey, mid), vals in by_pair.items():
                best = max(x[0] for x in vals); count = len(vals)
                conf = min(0.97, 0.82 + 0.10 * best + 0.035 * min(count, 2))
                detail = f"{count} archive-content path hit(s); best path similarity={best:.2f}"
                gmap[gkey].add_candidate(mid, "content", conf, detail, metadata.get(mid))
        if progress:
            progress("content", queries, max_content_queries, f"{queries} reverse lookup queries")

        # Tier 4: endorsed/tracked account state, compared against local group names.
        tracked: set[str] = set(); endorsed: set[str] = set()
        if account is not None:
            try:
                tracked = mod_ids_from_account_payload(account.tracked(), GAME_DOMAIN)
                endorsed = mod_ids_from_account_payload(account.endorsements(), GAME_DOMAIN, endorsements=True)
                account_ids = list(tracked | endorsed)
                # Bound account enrichment to avoid burning a user's hourly quota on enormous lists.
                if len(account_ids) > 500:
                    warnings.append(f"account candidate enrichment capped at 500 of {len(account_ids)} mods")
                    account_ids = account_ids[:500]
                rows = gql.batch_mods_basic(GAME_DOMAIN, account_ids)
                account_meta = {str(k): v for k, v in rows.items()}
                metadata.update(account_meta)
                _apply_account_tier(_unmatched(groups, decisions, 0.98), tracked, endorsed, account_meta)
            except (NexusAPIError, NexusGraphQLError) as exc:
                warnings.append(f"endorsed/tracked sync: {exc}")
        if progress:
            progress("account", len(tracked | endorsed), len(tracked | endorsed), "account correlation complete")

        # Tier 5: one or more known collection revisions.
        col_rows: list[dict[str, Any]] = []
        for slug in [x.strip() for x in collections if str(x).strip()]:
            try:
                col = gql.collection_latest(slug, GAME_DOMAIN)
                if col:
                    col.setdefault("slug", slug)
                    col_rows.append(col)
            except NexusGraphQLError as exc:
                warnings.append(f"collection {slug}: {exc}")
        metadata.update(_apply_collection_tier(_unmatched(groups, decisions, 0.98), col_rows))
        if progress:
            progress("collections", len(col_rows), len(col_rows), "collection matching complete")

        # Tier 6: requirements of already strong matches can identify framework/dependency groups.
        metadata.update(_apply_requirements(groups, gql, decisions, warnings, progress))
        _enrich_candidates(groups, metadata)

        # Tier 7: Nexus name search + Jaccard/Jaro-Winkler fallback. Rate-limit the number of
        # searches; a user can rerun after reviewing/rejecting suggestions.
        fuzzy_targets = _unmatched(groups, decisions, threshold=0.86)
        searches = 0
        for group in fuzzy_targets:
            if searches >= max_fuzzy_searches:
                warnings.append(f"fuzzy Nexus search capped at {max_fuzzy_searches} group(s)")
                break
            query_name = normalize_name(group.name)
            if len(query_name) < 3:
                continue
            try:
                rows = gql.search_mods(GAME_DOMAIN, query_name, count=20)
            except NexusGraphQLError as exc:
                warnings.append(f"fuzzy search for {group.name}: {exc}")
                break
            searches += 1
            for mod in rows:
                try:
                    mid = str(int(mod.get("modId")))
                except (TypeError, ValueError):
                    continue
                jac, jw, score = fuzzy_similarity(group.name, str(mod.get("name") or ""))
                if score < 0.48:
                    continue
                pop = min(0.035, math.log1p(max(0, _int(mod.get("endorsements")))) / 300.0)
                conf = min(0.88, 0.45 + 0.42 * score + pop)
                group.add_candidate(mid, "fuzzy", conf,
                                    f"Jaccard={jac:.2f}, Jaro-Winkler={jw:.2f}, combined={score:.2f}", mod)
                metadata[mid] = mod
        if progress:
            progress("fuzzy", searches, max_fuzzy_searches, f"{searches} Nexus name searches")
    else:
        warnings.append("No API key configured: network correlation tiers were skipped.")

    _enrich_candidates(groups, metadata)

    # Apply persistent review decisions and deduplicate automatic Nexus assignments. Confirmed/manual
    # decisions win; suggested duplicates keep the strongest local group and demote the others.
    rows = [_row_for(group, decisions.get(group.key) or {}) for group in groups]
    claimed: dict[str, tuple[int, float]] = {}
    for idx, row in enumerate(rows):
        match = row.get("match") or {}
        mid = str(match.get("mod_id") or "")
        if not mid or row["status"] in {"confirmed", "manual"}:
            continue
        conf = float(match.get("confidence") or 0)
        prior = claimed.get(mid)
        if prior is None or conf > prior[1]:
            if prior is not None:
                old = rows[prior[0]]
                old["duplicate_of_group"] = row["group_key"]
                old["status"] = "ambiguous"
            claimed[mid] = (idx, conf)
        else:
            row["duplicate_of_group"] = rows[prior[0]]["group_key"]
            row["status"] = "ambiguous"

    return {
        "version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "game_root": str(game_root),
        "scanner_generated_at": snapshot.get("generated_at"),
        "groups": len(groups),
        "matched": sum(1 for row in rows if row.get("match")),
        "confirmed": sum(1 for row in rows if row.get("status") == "confirmed"),
        "manual": sum(1 for row in rows if row.get("status") == "manual"),
        "unmatched": sum(1 for row in rows if not row.get("match")),
        "ambiguous": sum(1 for row in rows if row.get("status") == "ambiguous"),
        "warnings": warnings,
        "results": rows,
    }


def save_run_state(path: Path, result: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    state = {
        "version": SCHEMA_VERSION,
        "game_root": result.get("game_root"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "decisions": previous.get("decisions") or {},
        "results": result.get("results") or [],
        "warnings": result.get("warnings") or [],
        "summary": {k: result.get(k, 0) for k in ("groups", "matched", "confirmed", "manual", "unmatched", "ambiguous")},
    }
    _atomic_json(path, state)
    return state


def _find_result(state: dict[str, Any], key: str) -> dict[str, Any] | None:
    return next((row for row in state.get("results") or [] if row.get("group_key") == key), None)


def apply_review(path: Path, group_key_value: str, action: str, mod_id: str = "",
                 *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    state = load_state(path)
    row = _find_result(state, group_key_value)
    if row is None:
        raise ValueError("Unknown correlation group. Run correlation again before reviewing it.")
    decisions = state.setdefault("decisions", {})
    decision = decisions.setdefault(group_key_value, {"rejected_mod_ids": []})
    rejected = set(map(str, decision.get("rejected_mod_ids") or []))
    current = str((row.get("match") or {}).get("mod_id") or "")

    if action == "confirm":
        target = str(mod_id or current)
        if not target:
            raise ValueError("There is no current match to confirm.")
        decision.update({"decision": "confirmed", "mod_id": str(int(target))})
        row["status"] = "confirmed"
        if row.get("match"):
            row["match"]["confidence"] = 1.0
            row["match"]["source"] = row["match"].get("source") or "manual"
    elif action == "reject":
        target = str(mod_id or current)
        if not target:
            raise ValueError("There is no current match to reject.")
        target = str(int(target)); rejected.add(target)
        decision.update({"decision": "rejected", "mod_id": "", "rejected_mod_ids": sorted(rejected)})
        alternatives = [x for x in row.get("alternatives") or [] if str(x.get("mod_id")) not in rejected]
        row["match"] = alternatives[0] if alternatives else None
        row["alternatives"] = alternatives
        row["status"] = "suggested" if row["match"] else "unmatched"
        row["rejected_mod_ids"] = sorted(rejected)
    elif action == "reassign":
        target = str(int(mod_id))
        decision.update({"decision": "manual", "mod_id": target, "rejected_mod_ids": sorted(rejected)})
        meta = meta or {}
        match = {
            "mod_id": target,
            "name": str(meta.get("name") or f"Nexus Mod #{target}"),
            "author": str(meta.get("author") or ""),
            "version": str(meta.get("version") or ""),
            "summary": str(meta.get("summary") or ""),
            "picture_url": str(meta.get("pictureUrl") or ""),
            "endorsements": _int(meta.get("endorsements")),
            "downloads": _int(meta.get("downloads")),
            "confidence": 1.0,
            "source": "manual",
            "evidence": [{"source": "manual", "confidence": 1.0, "detail": "manually reassigned by user"}],
        }
        row["match"] = match; row["status"] = "manual"
        row["alternatives"] = [match] + [x for x in row.get("alternatives") or [] if str(x.get("mod_id")) != target]
    else:
        raise ValueError(f"Unknown review action: {action}")

    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(path, state)
    return row


def parse_mod_reference(text: str) -> str | None:
    text = str(text).strip()
    if text.isdigit():
        return str(int(text))
    m = re.search(r"/mods/(\d+)", text)
    return str(int(m.group(1))) if m else None


def _emit(payload: dict[str, Any]) -> None:
    print("[correlation-result] " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _progress(phase: str, current: int, total: int, detail: str) -> None:
    safe = str(detail).replace("\n", " ")[:180]
    print(f"[correlation-progress] {phase} {current}/{total} {safe}")


def _slug(value: str) -> str:
    m = re.search(r"/collections/([^/?#]+)", value)
    return m.group(1) if m else value.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Auto-correlate scanned Cyberpunk 2077 mods with Nexus Mods")
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="scan and run the full multi-tier correlation pipeline")
    run.add_argument("--game-root", type=Path, required=True)
    run.add_argument("--downloads", type=Path, default=None)
    run.add_argument("--path", dest="scan_paths", action="append", default=[])
    run.add_argument("--collection", action="append", default=[])
    run.add_argument("--scanner-state", type=Path, default=None)
    run.add_argument("--state", type=Path, default=None)
    run.add_argument("--jobs", type=int, default=mod_scanner.DEFAULT_JOBS)
    run.add_argument("--max-content-queries", type=int, default=30)
    run.add_argument("--max-fuzzy-searches", type=int, default=30)
    run.add_argument("--api-key", default="", help=argparse.SUPPRESS)
    run.add_argument("--json-out", type=Path, default=None)

    status = sub.add_parser("status", help="return the saved correlation state")
    status.add_argument("--game-root", type=Path, required=True)
    status.add_argument("--state", type=Path, default=None)

    for action in ("confirm", "reject", "reassign"):
        p = sub.add_parser(action)
        p.add_argument("--game-root", type=Path, required=True)
        p.add_argument("--group-key", required=True)
        p.add_argument("--mod-id", default="")
        p.add_argument("--state", type=Path, default=None)
        p.add_argument("--api-key", default="", help=argparse.SUPPRESS)

    search = sub.add_parser("search", help="search Nexus mods for manual reassignment")
    search.add_argument("--query", required=True)
    search.add_argument("--api-key", default="", help=argparse.SUPPRESS)

    args = ap.parse_args(argv)
    state_path = getattr(args, "state", None) or default_state_path(args.game_root)

    if args.command == "status":
        _emit({"type": "status", "state": load_state(state_path)})
        return 0

    key = resolve_api_key(getattr(args, "api_key", ""))
    try:
        if args.command in {"confirm", "reject", "reassign"}:
            meta = None
            mod_id = args.mod_id
            if args.command == "reassign":
                parsed = parse_mod_reference(mod_id)
                if not parsed:
                    raise ValueError("Reassign expects a Nexus mod ID or /mods/<id> URL.")
                mod_id = parsed
                if key:
                    meta = NexusGraphQLClient(key).get_mod(GAME_DOMAIN, mod_id, requirements=False)
            row = apply_review(state_path, args.group_key, args.command, mod_id, meta=meta)
            _emit({"type": "review", "action": args.command, "row": row, "state_path": str(state_path)})
            return 0

        if args.command == "search":
            if not key:
                raise ValueError("Nexus API key is required for manual name search.")
            rows = NexusGraphQLClient(key).search_mods(GAME_DOMAIN, args.query, count=20)
            _emit({"type": "search", "query": args.query, "mods": rows})
            return 0

        if args.jobs < 1:
            raise ValueError("--jobs must be >= 1")
        game_root = args.game_root.expanduser().resolve(strict=False)
        if not game_root.is_dir():
            raise ValueError(f"Game root not found: {game_root}")
        scanner_state = args.scanner_state or mod_scanner.default_state_path(game_root)
        scan = mod_scanner.scan_mods(
            game_root,
            args.scan_paths or None,
            jobs=args.jobs,
            state_path=scanner_state,
            save_snapshot=True,
            progress=lambda ph, cur, total, detail: _progress("scan-" + ph, cur, total, detail),
        )
        previous = load_state(state_path)
        gql = NexusGraphQLClient(key) if key else None
        account = NexusAccountClient(key) if key else None
        result = correlate(
            scan["snapshot"], game_root,
            gql=gql,
            account=account,
            downloads=args.downloads,
            collections=[_slug(x) for x in args.collection],
            decisions=previous.get("decisions") or {},
            max_content_queries=max(0, args.max_content_queries),
            max_fuzzy_searches=max(0, args.max_fuzzy_searches),
            progress=_progress,
        )
        save_run_state(state_path, result, previous)
        if args.json_out:
            _atomic_json(args.json_out, result)
        print(f"[correlation] groups={result['groups']} matched={result['matched']} "
              f"unmatched={result['unmatched']} ambiguous={result['ambiguous']} warnings={len(result['warnings'])}")
        print(f"[correlation-state] {state_path}")
        _emit({"type": "run", "summary": {k: result[k] for k in
               ("groups", "matched", "unmatched", "ambiguous", "warnings")},
               "state_path": str(state_path), "json_out": str(args.json_out or "")})
        return 0
    except KeyboardInterrupt:
        # SIGINT is how the GUI requests a graceful stop.  Treat that as a
        # cancellation, not an unhandled Python exception / signal exit (-2).
        _emit({"type": "cancelled", "message": "Auto-Correlation cancelled."})
        return 130
    except (ValueError, NexusAPIError, NexusGraphQLError) as exc:
        _emit({"type": "error", "message": str(exc)})
        return 1
    except Exception as exc:  # noqa: BLE001
        _emit({"type": "error", "message": f"Unexpected correlation error: {type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
