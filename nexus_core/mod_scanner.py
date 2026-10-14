#!/usr/bin/env python3
"""Read-only Cyberpunk 2077 mod scanner with TF-IDF + DBSCAN grouping.

The scanner deliberately keeps its state *outside* the game install.  It recursively
walks one or more configured mod roots, hashes every discovered file with SHA-256,
groups files whose names/paths look related, and compares the resulting snapshot with
the previous run so manual edits and missing files are visible.

No third-party ML dependency is required: the TF-IDF vectors are sparse dictionaries
and DBSCAN uses cosine distance over those vectors.  That keeps the backend usable in
the same minimal environment as the rest of nexus_core.
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
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

SCHEMA_VERSION = 2
CONFIG_DIR = Path.home() / ".config" / "cp2077_modder"
SHARED_CONFIG_FILE = CONFIG_DIR / "config.json"
SCANNER_STATE_DIR = CONFIG_DIR / "scanner"

DEFAULT_SCAN_PATHS = (
    "archive/pc/mod",
    "bin/x64/plugins/cyber_engine_tweaks/mods",
    "mods",
    "red4ext/plugins",
    "r6/scripts",
    "r6/tweaks",
    "soundbanks",
)

DEFAULT_EPS = 0.65            # cosine distance <= .65  <=> similarity >= .35
DEFAULT_MIN_SAMPLES = 1       # keep legitimate one-file mods as singleton clusters
DEFAULT_JOBS = min(8, max(1, (os.cpu_count() or 4)))
_HASH_CHUNK = 1024 * 1024

# Common CP2077/path words carry location rather than mod identity.  Generic source-file
# words are included too so 100 different mods with init.lua do not become one cluster.
STOP_TOKENS = {
    "archive", "pc", "mod", "mods", "bin", "x64", "plugin", "plugins",
    "cyber", "engine", "tweaks", "cyberengine", "cyberenginetweaks", "cet",
    "red4ext", "r6", "scripts", "script", "tweak", "tweaks", "soundbanks",
    "soundbank", "config", "configs", "configuration", "resources", "resource",
    "assets", "asset", "data", "files", "file", "src", "source", "lib", "libs",
    "main", "init", "index", "module", "modules", "common", "core", "default",
    "settings", "setting", "localization", "localisation", "lang", "language",
    "json", "xml", "yaml", "yml", "toml", "lua", "reds", "archive", "xl",
    "dll", "asi", "ini", "cfg", "txt", "bin", "dat", "bk2", "wem", "opusinfo",
    "version", "release", "final", "latest", "update", "updated", "installer",
}

ProgressFn = Callable[[str, int, int, str], None]


def _load_default_game_root() -> Path:
    try:
        data = json.loads(SHARED_CONFIG_FILE.read_text(encoding="utf-8"))
        if data.get("game_root"):
            return Path(data["game_root"])
    except (OSError, ValueError, TypeError):
        pass
    return Path("/mnt/ssd/Cyberpunk 2077/game/prefix/drive_c/game")


def default_state_path(game_root: Path) -> Path:
    """Return a stable per-install state file without writing inside the install."""
    root = str(game_root.expanduser().resolve(strict=False))
    digest = hashlib.sha1(root.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", game_root.name or "game")[:48] or "game"
    return SCANNER_STATE_DIR / f"{safe_name}-{digest}.json"


def resolve_scan_paths(game_root: Path, configured: Iterable[str | Path] | None) -> tuple[list[Path], list[Path]]:
    """Resolve configured roots. Relative entries are interpreted under ``game_root``.

    Returns ``(existing_roots, missing_roots)`` with duplicates removed.  We intentionally
    do not collapse nested roots here; discovery de-duplicates files by absolute path, while
    retaining the most specific configured root for useful relative names.
    """
    values = list(configured) if configured else list(DEFAULT_SCAN_PATHS)
    existing: list[Path] = []
    missing: list[Path] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        p = Path(text).expanduser()
        if not p.is_absolute():
            p = game_root / p
        p = p.resolve(strict=False)
        key = os.path.normcase(str(p))
        if key in seen:
            continue
        seen.add(key)
        (existing if p.is_dir() else missing).append(p)
    return existing, missing


def _tokenize_piece(text: str) -> list[str]:
    # Split camelCase/PascalCase before punctuation, then throw away versions and noise.
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    raw = re.findall(r"[A-Za-z0-9]+", text.lower())
    out: list[str] = []
    for tok in raw:
        if len(tok) < 2 or tok in STOP_TOKENS or tok.isdigit():
            continue
        if re.fullmatch(r"v?\d+(?:\d+)*", tok):
            continue
        out.append(tok)
    return out


def _document_tokens(rel: Path) -> list[str]:
    """Build weighted name/path tokens for one file.

    Basename tokens are repeated three times and the immediate parent twice.  This is a
    simple TF weighting that makes ``foo.archive`` and ``foo/init.lua`` meet across roots,
    while deeper generic directory names have less influence.
    """
    parts = list(rel.parts)
    tokens: list[str] = []
    for part in parts[:-1]:
        tokens.extend(_tokenize_piece(part))
    if len(parts) > 1:
        tokens.extend(_tokenize_piece(parts[-2]))
    stem_tokens = _tokenize_piece(Path(parts[-1]).stem if parts else "")
    tokens.extend(stem_tokens * 3)
    return tokens


def discover_files(roots: list[Path], exclude: Iterable[Path] = ()) -> tuple[list[dict], list[str]]:
    """Recursively discover regular files under all configured roots.

    Directory symlinks are not followed, avoiding loops and accidental traversal outside a
    configured tree.  File symlinks are included and hashed through their target, matching
    what the game sees when it opens them.
    """
    excluded = {str(p.expanduser().resolve(strict=False)) for p in exclude}
    found: dict[str, dict] = {}
    errors: list[str] = []

    # Most-specific roots first means overlapping configurations get the shortest relative
    # path, which improves name similarity (e.g. r6/scripts/foo rather than r6/scripts/foo).
    ordered_roots = sorted(roots, key=lambda p: len(p.parts), reverse=True)
    for root in ordered_roots:
        def onerror(exc: OSError, root=root):
            errors.append(f"{root}: {exc}")

        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
            # os.walk already avoids following symlink dirs with followlinks=False.  Sorting
            # makes snapshots deterministic across filesystems.
            dirnames.sort(key=str.casefold)
            filenames.sort(key=str.casefold)
            base = Path(dirpath)
            for filename in filenames:
                path = base / filename
                abs_key = str(path.absolute())
                try:
                    resolved_key = str(path.resolve(strict=False))
                    if abs_key in excluded or resolved_key in excluded:
                        continue
                    if not path.is_file():
                        continue
                    st = path.stat()
                    rel = path.relative_to(root)
                except (OSError, ValueError) as exc:
                    errors.append(f"{path}: {exc}")
                    continue
                # If overlapping roots found this file already, the earlier (more specific)
                # root wins.
                if resolved_key in found:
                    continue
                found[resolved_key] = {
                    "path": path,
                    "path_key": resolved_key,
                    "root": root,
                    "rel": rel,
                    "size": int(st.st_size),
                    "mtime_ns": int(st.st_mtime_ns),
                    "tokens": _document_tokens(rel),
                    "md5": None,
                    "sha256": None,
                    "hash_error": None,
                }
    records = sorted(found.values(), key=lambda r: str(r["path"]).casefold())
    return records, errors


def _hash_file(path: Path) -> tuple[str, str]:
    """Return ``(md5, sha256)`` in one read pass.

    MD5 is retained only as the Nexus file-identity key; SHA-256 remains the
    integrity primitive. ``usedforsecurity=False`` makes that intent explicit
    on FIPS-aware Python builds.
    """
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


def _sha256_file(path: Path) -> str:
    """Backward-compatible helper used by older callers/tests."""
    return _hash_file(path)[1]


def hash_files(records: list[dict], jobs: int = DEFAULT_JOBS, progress: ProgressFn | None = None) -> list[str]:
    """Hash every discovered file.  Returns human-readable per-file errors."""
    errors: list[str] = []
    total = len(records)
    if not total:
        return errors
    jobs = max(1, int(jobs))

    def done_one(index: int, digests: tuple[str, str] | None, err: str | None):
        rec = records[index]
        if digests is not None:
            rec["md5"], rec["sha256"] = digests
        else:
            rec["md5"] = rec["sha256"] = None
        rec["hash_error"] = err
        if err:
            errors.append(f"{rec['path']}: {err}")
        if progress:
            progress("hash", index + 1, total, str(rec["path"]))

    if jobs == 1:
        for i, rec in enumerate(records):
            try:
                done_one(i, _hash_file(rec["path"]), None)
            except OSError as exc:
                done_one(i, None, str(exc))
        return errors

    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="modhash") as pool:
        future_to_idx = {pool.submit(_hash_file, rec["path"]): i for i, rec in enumerate(records)}
        completed = 0
        for fut in as_completed(future_to_idx):
            i = future_to_idx[fut]
            rec = records[i]
            try:
                rec["md5"], rec["sha256"] = fut.result()
            except OSError as exc:
                rec["hash_error"] = str(exc)
                errors.append(f"{rec['path']}: {exc}")
            except Exception as exc:  # keep one bad file from aborting the whole inventory
                rec["hash_error"] = f"{type(exc).__name__}: {exc}"
                errors.append(f"{rec['path']}: {rec['hash_error']}")
            completed += 1
            if progress:
                progress("hash", completed, total, str(rec["path"]))
    return errors


def compute_tfidf(corpus: list[list[str]]) -> list[dict[str, float]]:
    """Return L2-normalized sparse TF-IDF vectors.

    Smoothed IDF is used so a token present in every document still has a positive weight;
    this makes small scans and singleton groups behave sensibly.
    """
    n_docs = len(corpus)
    if not n_docs:
        return []
    df: Counter[str] = Counter()
    for doc in corpus:
        df.update(set(doc))
    vectors: list[dict[str, float]] = []
    for doc in corpus:
        counts = Counter(doc)
        if not counts:
            vectors.append({})
            continue
        total = sum(counts.values()) or 1
        vec: dict[str, float] = {}
        for token, count in counts.items():
            tf = count / total
            idf = math.log((1.0 + n_docs) / (1.0 + df[token])) + 1.0
            vec[token] = tf * idf
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm:
            vec = {k: v / norm for k, v in vec.items()}
        vectors.append(vec)
    return vectors


def cosine_similarity(v1: dict[str, float], v2: dict[str, float]) -> float:
    if not v1 or not v2:
        return 0.0
    if len(v1) > len(v2):
        v1, v2 = v2, v1
    return sum(weight * v2.get(token, 0.0) for token, weight in v1.items())


def dbscan(vectors: list[dict[str, float]], eps: float = DEFAULT_EPS,
           min_samples: int = DEFAULT_MIN_SAMPLES) -> list[int]:
    """Cluster sparse TF-IDF vectors with DBSCAN using cosine distance.

    Labels are ``0..N``; ``-1`` is noise.  An inverted token index keeps region queries from
    comparing unrelated files: when ``eps < 1`` two vectors with no shared token can never be
    neighbors because their cosine distance is exactly 1.
    """
    if not 0 < eps < 1:
        raise ValueError("eps must be > 0 and < 1 for cosine-distance clustering")
    if min_samples < 1:
        raise ValueError("min_samples must be >= 1")
    n = len(vectors)
    if not n:
        return []

    inverted: dict[str, list[int]] = defaultdict(list)
    for i, vec in enumerate(vectors):
        for token in vec:
            inverted[token].append(i)

    similarity_floor = 1.0 - eps
    neighbor_cache: dict[int, list[int]] = {}

    def region_query(i: int) -> list[int]:
        cached = neighbor_cache.get(i)
        if cached is not None:
            return cached
        candidates = {i}
        for token in vectors[i]:
            candidates.update(inverted[token])
        neighbors = [i]
        for j in candidates:
            if j == i:
                continue
            if cosine_similarity(vectors[i], vectors[j]) + 1e-12 >= similarity_floor:
                neighbors.append(j)
        neighbors.sort()
        neighbor_cache[i] = neighbors
        return neighbors

    UNASSIGNED = -99
    labels = [UNASSIGNED] * n
    visited = [False] * n
    cluster_id = 0

    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        neighbors = region_query(i)
        if len(neighbors) < min_samples:
            labels[i] = -1
            continue

        labels[i] = cluster_id
        queue = deque(j for j in neighbors if j != i)
        queued = set(queue)
        queued.add(i)
        while queue:
            j = queue.popleft()
            if not visited[j]:
                visited[j] = True
                neighbors_j = region_query(j)
                if len(neighbors_j) >= min_samples:
                    for k in neighbors_j:
                        if k not in queued:
                            queue.append(k)
                            queued.add(k)
            # A DBSCAN noise point may later become a border point of a real cluster.
            if labels[j] in (UNASSIGNED, -1):
                labels[j] = cluster_id
        cluster_id += 1

    return labels


def _directory_labels(records: list[dict]) -> list[int]:
    """Fallback grouping when clustering is explicitly disabled."""
    keys: dict[str, int] = {}
    labels: list[int] = []
    for rec in records:
        rel: Path = rec["rel"]
        if len(rel.parts) > 1:
            key = f"{rec['root']}::{rel.parts[0].casefold()}"
        else:
            key = f"{rec['root']}::{rel.stem.casefold()}"
        if key not in keys:
            keys[key] = len(keys)
        labels.append(keys[key])
    return labels


def _group_name(indices: list[int], records: list[dict], vectors: list[dict[str, float]]) -> str:
    scores: Counter[str] = Counter()
    doc_freq: Counter[str] = Counter()
    for idx in indices:
        for token, weight in vectors[idx].items():
            scores[token] += weight
            doc_freq[token] += 1

    # Multi-file groups are best named by terms shared by multiple members.  For a singleton
    # use its filename/parent naturally.
    if len(indices) > 1:
        shared = [t for t, _ in scores.most_common() if doc_freq[t] >= 2]
    else:
        shared = []
    chosen = shared[:3] or [t for t, _ in scores.most_common(3)]
    if chosen:
        return " ".join(tok.replace("_", " ").title() for tok in chosen)

    rec = records[indices[0]]
    rel: Path = rec["rel"]
    fallback = rel.parts[0] if len(rel.parts) > 1 else rel.stem
    return fallback or "Unnamed Mod"


def build_groups(records: list[dict], vectors: list[dict[str, float]], labels: list[int]) -> list[dict]:
    by_label: dict[int, list[int]] = defaultdict(list)
    noise: list[int] = []
    for i, label in enumerate(labels):
        if label == -1:
            noise.append(i)
        else:
            by_label[label].append(i)

    buckets = list(by_label.values()) + [[i] for i in noise]
    buckets.sort(key=lambda idxs: min(str(records[i]["path"]).casefold() for i in idxs))

    groups: list[dict] = []
    for number, indices in enumerate(buckets, 1):
        members = [records[i] for i in indices]
        paths = [m["path_key"] for m in members]
        group_id = f"cluster-{number:04d}"
        name = _group_name(indices, records, vectors)
        total_size = sum(m["size"] for m in members)
        for m in members:
            m["group_id"] = group_id
            m["group_name"] = name
        groups.append({
            "id": group_id,
            "name": name,
            "files": paths,
            "file_count": len(paths),
            "size": total_size,
            "noise": len(indices) == 1 and labels[indices[0]] == -1,
        })
    return groups


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"version": SCHEMA_VERSION, "files": {}}


def compare_state(previous: dict, current_files: dict[str, dict]) -> dict[str, list[str]]:
    old = previous.get("files", {}) if isinstance(previous, dict) else {}
    old_paths = set(old)
    new_paths = set(current_files)
    added = sorted(new_paths - old_paths, key=str.casefold)
    removed = sorted(old_paths - new_paths, key=str.casefold)
    changed: list[str] = []
    unchanged: list[str] = []
    for path in sorted(old_paths & new_paths, key=str.casefold):
        old_hash = old.get(path, {}).get("sha256")
        new_hash = current_files[path].get("sha256")
        if old_hash and new_hash and old_hash != new_hash:
            changed.append(path)
        elif old_hash == new_hash and new_hash:
            unchanged.append(path)
        elif old_hash != new_hash:
            changed.append(path)
    return {"added": added, "changed": changed, "removed": removed, "unchanged": unchanged}


def _serializable_file(rec: dict) -> dict:
    return {
        "root": str(rec["root"]),
        "relative_path": rec["rel"].as_posix(),
        "size": rec["size"],
        "mtime_ns": rec["mtime_ns"],
        "md5": rec.get("md5"),
        "sha256": rec["sha256"],
        "hash_error": rec["hash_error"],
        "group_id": rec.get("group_id"),
        "group_name": rec.get("group_name"),
    }


def save_state(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace prevents Ctrl-C/power loss from destroying the last good integrity map.
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass


def scan_mods(game_root: Path, scan_paths: Iterable[str | Path] | None = None,
              *, eps: float = DEFAULT_EPS, min_samples: int = DEFAULT_MIN_SAMPLES,
              jobs: int = DEFAULT_JOBS, state_path: Path | None = None,
              save_snapshot: bool = True, clustering: bool = True,
              progress: ProgressFn | None = None) -> dict:
    """Run a complete recursive scan and return a structured result."""
    game_root = game_root.expanduser().resolve(strict=False)
    roots, missing_roots = resolve_scan_paths(game_root, scan_paths)
    state_path = (state_path or default_state_path(game_root)).expanduser().resolve(strict=False)
    previous = load_state(state_path)

    if progress:
        progress("discover", 0, 0, "Discovering files")
    records, walk_errors = discover_files(roots, exclude=[state_path])
    if progress:
        progress("discover", len(records), len(records), "Discovery complete")

    hash_errors = hash_files(records, jobs=jobs, progress=progress)
    corpus = [rec["tokens"] for rec in records]
    vectors = compute_tfidf(corpus)
    if progress:
        progress("cluster", 0, len(records), "Clustering")
    labels = dbscan(vectors, eps=eps, min_samples=min_samples) if clustering else _directory_labels(records)
    groups = build_groups(records, vectors, labels)
    if progress:
        progress("cluster", len(records), len(records), "Clustering complete")

    files = {rec["path_key"]: _serializable_file(rec) for rec in records}
    changes = compare_state(previous, files)
    generated_at = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "game_root": str(game_root),
        "roots": [str(p) for p in roots],
        "missing_roots": [str(p) for p in missing_roots],
        "algorithm": {
            "clustering": "tfidf-dbscan" if clustering else "directory",
            "metric": "cosine",
            "eps": eps,
            "min_samples": min_samples,
            "hash": "sha256",
            "nexus_identity_hash": "md5",
        },
        "files": files,
        "groups": groups,
        "summary": {
            "files": len(files),
            "groups": len(groups),
            "bytes": sum(rec["size"] for rec in records),
            "added": len(changes["added"]),
            "changed": len(changes["changed"]),
            "removed": len(changes["removed"]),
            "unchanged": len(changes["unchanged"]),
            "hash_errors": len(hash_errors),
            "walk_errors": len(walk_errors),
        },
    }
    if save_snapshot:
        save_state(state_path, snapshot)

    return {
        "snapshot": snapshot,
        "changes": changes,
        "state_path": state_path,
        "walk_errors": walk_errors,
        "hash_errors": hash_errors,
    }


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{n} B"


def print_report(result: dict, *, top: int = 10) -> None:
    snapshot = result["snapshot"]
    summary = snapshot["summary"]
    changes = result["changes"]

    print("\n" + "=" * 64)
    print("=== Mod Scanner — TF-IDF + DBSCAN + SHA-256 integrity / MD5 identity ===")
    print("=" * 64)
    print("Configured roots:")
    for root in snapshot["roots"]:
        print(f"  [✓] {root}")
    for root in snapshot["missing_roots"]:
        print(f"  [~] {root}  (not present; skipped)")

    print("\nScan summary:")
    print(f"  Files              : {summary['files']}")
    print(f"  Groups             : {summary['groups']}")
    print(f"  Disk space         : {_human_bytes(summary['bytes'])}")
    print(f"  Added since scan   : {summary['added']}")
    print(f"  Changed hash       : {summary['changed']}")
    print(f"  Removed since scan : {summary['removed']}")
    print(f"  Unchanged          : {summary['unchanged']}")
    print(f"  Read/hash errors   : {summary['walk_errors'] + summary['hash_errors']}")

    groups = sorted(snapshot["groups"], key=lambda g: (g["size"], g["file_count"]), reverse=True)
    if groups:
        print(f"\nLargest groups (top {min(top, len(groups))}):")
        for group in groups[:top]:
            print(f"  {group['id']}  {group['name']:<32} "
                  f"{group['file_count']:>5} file(s)  {_human_bytes(group['size']):>10}")

    for title, key in (("Changed", "changed"), ("Added", "added"), ("Removed", "removed")):
        paths = changes[key]
        if paths:
            print(f"\n{title} files:")
            for path in paths[:25]:
                print(f"  {path}")
            if len(paths) > 25:
                print(f"  ... and {len(paths) - 25} more")

    for err in (result["walk_errors"] + result["hash_errors"])[:20]:
        print(f"  [!] {err}")

    print(f"\nIntegrity state: {result['state_path']}")
    print("[scanner] "
          f"roots={len(snapshot['roots'])} files={summary['files']} groups={summary['groups']} "
          f"added={summary['added']} changed={summary['changed']} removed={summary['removed']} "
          f"unchanged={summary['unchanged']} errors={summary['walk_errors'] + summary['hash_errors']}")
    print(f"[scanner-state] {result['state_path']}")


def _cli_progress(phase: str, current: int, total: int, detail: str) -> None:
    if phase == "discover" and current == 0:
        print("[1/3] Recursively discovering files...", flush=True)
    elif phase == "discover" and total and current == total:
        print(f"      discovered {total} file(s)", flush=True)
    elif phase == "hash":
        if current == 1:
            print("[2/3] Computing SHA-256 hashes...", flush=True)
        # At most ~20 progress lines even for huge installs.
        step = max(1, total // 20)
        if current == 1 or current == total or current % step == 0:
            print(f"[scanner-progress] hash {current}/{total}", flush=True)
    elif phase == "cluster" and current == 0:
        print("[3/3] Building TF-IDF vectors and DBSCAN clusters...", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Recursively discover Cyberpunk mod files, cluster them with TF-IDF + "
                    "DBSCAN, and track SHA-256 integrity between scans.")
    ap.add_argument("--game-root", type=Path, default=_load_default_game_root(),
                    help="Cyberpunk game root; relative --path entries are resolved under it")
    ap.add_argument("--path", dest="scan_paths", action="append", default=[], metavar="PATH",
                    help="root to scan recursively; repeat for multiple paths (relative = under game root)")
    ap.add_argument("--eps", type=float, default=DEFAULT_EPS,
                    help=f"DBSCAN cosine-distance epsilon (default {DEFAULT_EPS})")
    ap.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES,
                    help=f"DBSCAN minimum neighbors including self (default {DEFAULT_MIN_SAMPLES})")
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                    help=f"parallel SHA-256 workers (default {DEFAULT_JOBS})")
    ap.add_argument("--state", type=Path, default=None,
                    help="integrity snapshot path (default: per-game file under ~/.config/cp2077_modder/scanner)")
    ap.add_argument("--json-out", type=Path, default=None,
                    help="also write this run's full structured result as JSON")
    ap.add_argument("--no-state", action="store_true",
                    help="do not update the persistent integrity snapshot")
    ap.add_argument("--no-cluster", action="store_true",
                    help="group by directory instead of TF-IDF + DBSCAN")
    ap.add_argument("--top", type=int, default=10, help="number of largest groups to print")
    args = ap.parse_args(argv)

    if not 0 < args.eps < 1:
        ap.error("--eps must be > 0 and < 1 (cosine distance)")
    if args.min_samples < 1:
        ap.error("--min-samples must be >= 1")
    if args.jobs < 1:
        ap.error("--jobs must be >= 1")
    if not args.game_root.exists():
        ap.error(f"game root does not exist: {args.game_root}")

    state_path = args.state or default_state_path(args.game_root)
    result = scan_mods(
        args.game_root,
        args.scan_paths or None,
        eps=args.eps,
        min_samples=args.min_samples,
        jobs=args.jobs,
        state_path=state_path,
        save_snapshot=not args.no_state,
        clustering=not args.no_cluster,
        progress=_cli_progress,
    )
    print_report(result, top=max(0, args.top))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(result["snapshot"], indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        print(f"[scanner-json] {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
