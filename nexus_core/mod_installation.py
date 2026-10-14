#!/usr/bin/env python3
"""Interactive, preview-first mod installation backend.

The historical :mod:`nexus_core.deployer` remains the batch/sync engine.  This
module adds the user-facing transaction that a GUI installer needs:

    archive -> extracted session -> FOMOD choices -> file/conflict preview
            -> explicit skip/overwrite decisions -> apply -> manifest update

Previewing never writes to the game directory.  Installation reuses Deployer's
single write choke-point, pins/runtime-state preservation, backups, case policy,
manifest ownership, version policy and durable disable records.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import deployer as D
from . import smart_deployment as SD

SESSION_ROOT = Path.home() / ".cache" / "cp2077_modder" / "install_sessions"
SESSION_MARKER = ".install-session.json"
SESSION_MAX_AGE = 24 * 60 * 60
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar"}


def _json_line(payload: dict) -> None:
    print("[install-json] " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _load_json_arg(text: str | None) -> dict:
    if not text:
        return {}
    try:
        p = Path(text)
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
        else:
            data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _clean_stale_sessions() -> None:
    try:
        SESSION_ROOT.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for p in SESSION_ROOT.iterdir():
            try:
                if p.is_dir() and now - p.stat().st_mtime > SESSION_MAX_AGE:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def _session_path(value: str | Path) -> Path:
    p = Path(value).expanduser().resolve()
    root = SESSION_ROOT.expanduser().resolve()
    try:
        p.relative_to(root)
    except ValueError as e:
        raise ValueError("session is outside the installer session directory") from e
    if not (p / SESSION_MARKER).is_file():
        raise ValueError("invalid or expired installer session")
    return p


def create_session(archive: Path) -> Path:
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"archive not found: {archive}")
    if archive.suffix.lower() not in ARCHIVE_SUFFIXES:
        raise ValueError(f"unsupported archive type: {archive.suffix or '(none)'}")
    _clean_stale_sessions()
    SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="install_", dir=str(SESSION_ROOT)))
    extract = session / "extract"
    extract.mkdir()
    if not D.extract_archive(archive, extract):
        shutil.rmtree(session, ignore_errors=True)
        raise RuntimeError(f"failed to extract {archive.name}")
    st = archive.stat()
    marker = {
        "version": 1,
        "archive": str(archive),
        "archive_name": archive.name,
        "mod_name": archive.stem,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    (session / SESSION_MARKER).write_text(json.dumps(marker, indent=2), encoding="utf-8")
    return session


def session_info(session: Path) -> dict:
    session = _session_path(session)
    return json.loads((session / SESSION_MARKER).read_text(encoding="utf-8"))


def cleanup_session(session: Path) -> None:
    try:
        p = _session_path(session)
    except ValueError:
        return
    shutil.rmtree(p, ignore_errors=True)


def _session_extract(session: Path) -> Path:
    session = _session_path(session)
    extract = (session / "extract").resolve()
    try:
        extract.relative_to(session.resolve())
    except ValueError as e:
        raise ValueError("installer session extraction directory escaped its session") from e
    if not extract.is_dir():
        raise ValueError("installer session extraction directory is missing")
    return extract


def _plugin_description(plugin) -> str:
    node = plugin.find("./description")
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _normalize_selection(group_type: str, plugins: list, requested: list[str] | None,
                         flags: dict) -> list:
    """Validate a user selection while preserving FOMOD cardinality/Required rules."""
    typed = [(p, D._plugin_type(p, flags)) for p in plugins]
    usable = [p for p, t in typed if t != "NotUsable"]
    required = [p for p, t in typed if t == "Required"]
    by_name = {p.get("name", ""): p for p in usable}
    chosen = [by_name[n] for n in (requested or []) if n in by_name]
    for p in required:
        if p not in chosen:
            chosen.append(p)

    g = group_type or "SelectExactlyOne"
    if g == "SelectAll":
        return usable
    if g == "SelectAtMostOne":
        return (required[:1] or chosen[:1])
    if g == "SelectExactlyOne":
        return (required[:1] or chosen[:1])
    if g == "SelectAtLeastOne":
        return chosen or usable[:1]
    return chosen  # SelectAny


def _default_selection(group, flags: dict) -> list:
    return D._select_plugins(group, flags)


def build_fomod_plan(config_path: Path, selections: dict[str, list[str]] | None = None) -> tuple[list, dict]:
    """Resolve a FOMOD with explicit group selections and return file pairs + wizard model.

    ``selections`` keys are stable ordered ids (``s0.g1``).  Only groups visible after
    applying earlier choices appear in the model.  Calling this function again after a
    choice therefore naturally implements a real step-wise wizard: later groups can appear
    or disappear as condition flags change.
    """
    selections = selections or {}
    root = D._parse_xml(config_path)
    fomod_root = config_path.parent.parent.resolve()
    flags: dict[str, str] = {}
    elems: list[tuple[str, str, str | None, int]] = []
    model: dict[str, Any] = {
        "module_name": (root.findtext("./moduleName") or fomod_root.name).strip(),
        "groups": [],
        "flags": {},
    }

    rif = root.find("./requiredInstallFiles")
    if rif is not None:
        for e in rif:
            tag = D._ln(e.tag)
            if tag in ("file", "folder"):
                elems.append((tag, e.get("source", ""), e.get("destination"),
                              int(e.get("priority") or 0)))

    steps_root = root.find("./installSteps")
    steps = D._order_nodes(root.findall("./installSteps/installStep"),
                           steps_root.get("order") if steps_root is not None else "Ascending")
    for si, step in enumerate(steps):
        visible = step.find("./visible")
        if visible is not None and not D._eval_deps(visible, flags):
            continue
        og = step.find("./optionalFileGroups")
        groups = D._order_nodes(step.findall("./optionalFileGroups/group"),
                                og.get("order") if og is not None else "Ascending")
        for gi, group in enumerate(groups):
            group_id = f"s{si}.g{gi}"
            gtype = group.get("type", "SelectExactlyOne")
            plugins_node = group.find("./plugins")
            plugins = D._order_nodes(group.findall("./plugins/plugin"),
                                     plugins_node.get("order") if plugins_node is not None else "Explicit")
            # Plugin effective types depend on flags accumulated from previous groups.
            options = []
            for p in plugins:
                ptype = D._plugin_type(p, flags)
                options.append({
                    "name": p.get("name", "Unnamed option"),
                    "type": ptype,
                    "description": _plugin_description(p),
                    "usable": ptype != "NotUsable",
                })

            defaults = _default_selection(group, flags)
            if group_id in selections:
                chosen = _normalize_selection(gtype, plugins, selections.get(group_id), flags)
                # Invalid empty exactly/at-least-one choices fall back to authored defaults.
                if gtype in {"SelectExactlyOne", "SelectAtLeastOne"} and not chosen:
                    chosen = defaults
            else:
                chosen = defaults
            selected_names = [p.get("name", "Unnamed option") for p in chosen]
            model["groups"].append({
                "id": group_id,
                "step": step.get("name", f"Step {si + 1}"),
                "name": group.get("name", f"Options {gi + 1}"),
                "type": gtype,
                "options": options,
                "selected": selected_names,
                "default": [p.get("name", "Unnamed option") for p in defaults],
            })
            for p in chosen:
                D._apply_flags(p, flags)
            for p in chosen:
                elems.extend(D._files_of(p))

    conds = 0
    for pat in root.findall("./conditionalFileInstalls/patterns/pattern"):
        if D._eval_deps(pat.find("./dependencies"), flags):
            got = D._files_of(pat)
            if got:
                conds += 1
                elems.extend(got)
    model["flags"] = dict(flags)
    model["conditional_sets"] = conds

    staged: list[tuple[str, Path, int, int]] = []
    idx = 0
    unsafe = 0
    missing = 0
    for kind, source, destination, priority in elems:
        source_rel = D._safe_archive_rel(source or "")
        if source_rel is None:
            unsafe += 1
            continue
        src = D._ci_resolve(fomod_root, source_rel)
        if src is None:
            missing += 1
            continue
        raw_dest = source_rel if destination is None else destination
        dest = D._safe_archive_rel(raw_dest)
        if dest is None:
            unsafe += 1
            continue
        if kind == "file" and src.is_file():
            staged.append((dest or src.name, src, priority, idx)); idx += 1
        elif kind == "folder" and src.is_dir():
            for f in D.iter_files(src):
                rel = f.relative_to(src).as_posix()
                out = f"{dest}/{rel}" if dest else rel
                safe_out = D._safe_archive_rel(out)
                if safe_out is None:
                    unsafe += 1
                    continue
                staged.append((safe_out, f, priority, idx)); idx += 1
        elif src.is_file():
            staged.append((dest or src.name, src, priority, idx)); idx += 1

    best: dict[str, tuple[Path, int, int]] = {}
    for dest, src, priority, order in staged:
        current = best.get(dest)
        if current is None or (priority, order) >= (current[1], current[2]):
            best[dest] = (src, priority, order)
    pairs = [(entry[0], dest) for dest, entry in sorted(best.items(), key=lambda kv: kv[1][2])]
    model["file_count"] = len(pairs)
    model["unsafe_entries"] = unsafe
    model["missing_sources"] = missing
    return pairs, model


def _structure_pairs(extract_dir: Path, game_root: Path) -> tuple[list, set[str]]:
    extract_real = extract_dir.resolve()
    candidates, notes = D.find_candidates(extract_dir)
    resolved = D.resolve_candidates(candidates)
    staged: list[tuple[Path, str]] = []
    loose: dict[Path, list[Path]] = {}
    for kind, cpath in resolved:
        if kind == "loose":
            loose.setdefault(D.LOOSE_DESTS[cpath.suffix.lower()], []).append(cpath)
            continue
        base = D.dest_base_for(kind, cpath, game_root)
        rel_base = base.relative_to(game_root)
        for src in D.iter_files(cpath):
            try:
                src.resolve().relative_to(extract_real)
            except (OSError, ValueError):
                continue
            dest = (rel_base / src.relative_to(cpath)).as_posix()
            if D._safe_archive_rel(dest) is not None:
                staged.append((src, dest))
    for rel_dest, files in sorted(loose.items(), key=lambda kv: str(kv[0])):
        for src in files:
            try:
                src.resolve().relative_to(extract_real)
            except (OSError, ValueError):
                continue
            dest = (rel_dest / src.name).as_posix()
            if D._safe_archive_rel(dest) is not None:
                staged.append((src, dest))
    # Mirror deployer's last-write-wins behaviour for duplicate destinations inside one mod.
    best: dict[str, tuple[int, Path]] = {}
    for i, (src, dest) in enumerate(staged):
        best[dest] = (i, src)
    pairs = [(src, dest) for dest, (i, src) in sorted(best.items(), key=lambda kv: kv[1][0])]
    return pairs, notes


def _owners_by_path(manifest_mods: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for mod, rels in (manifest_mods or {}).items():
        for rel in rels or []:
            out.setdefault(str(rel).replace("\\", "/"), []).append(mod)
    return out


def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
    except OSError:
        return False
    return D._sha256(a) == D._sha256(b)


def _smart_owner_context(game_root: Path) -> tuple[dict[str, list[str]], bool]:
    """Return projected-path owners without creating smart-deployment state during preview."""
    if SD.db_path(game_root).is_file():
        out: dict[str, list[str]] = {}
        with SD.Store(game_root, create=False) as store:
            rows = store.conn.execute(
                "SELECT f.projected_rel,m.name FROM files f JOIN mods m ON m.id=f.mod_id ORDER BY m.id"
            ).fetchall()
            for row in rows:
                out.setdefault(str(row["projected_rel"]), []).append(str(row["name"]))
        return out, True
    manifest = D.load_manifest(game_root)
    return _owners_by_path(manifest.get("mods", {})), False


def preview_extracted(extract_dir: Path, game_root: Path, mod_name: str,
                      selections: dict[str, list[str]] | None = None) -> dict:
    game_root = game_root.expanduser().resolve()
    extract_dir = extract_dir.expanduser().resolve()
    owners, smart = _smart_owner_context(game_root)
    pins = D.load_pins(game_root)
    cfg = D.find_fomod_config(extract_dir)
    if cfg is not None:
        pairs, wizard = build_fomod_plan(cfg, selections)
        source = "fomod"
        notes = ["fomod"]
    else:
        pairs, note_set = _structure_pairs(extract_dir, game_root)
        wizard = None
        source = "structure"
        notes = sorted(note_set)

    rows = []
    counts: dict[str, int] = {}
    resolver = D.Deployer(game_root, dry_run=True, verbose=False,
                          manifest_mods=D.load_manifest(game_root).get("mods", {}) if not smart else {})
    for src, authored_rel in pairs:
        safe = D._safe_archive_rel(authored_rel)
        if safe is None:
            continue
        authored_rel = SD._safe_rel(safe)
        projected_rel, redmod_mode = SD._project_redmod(authored_rel)
        projected_rel = SD._safe_rel(projected_rel)
        rel_path = resolver._resolve_existing_case(D.normalize_rel_path(Path(projected_rel)))
        projected_rel = rel_path.as_posix()
        dest = game_root / rel_path
        try:
            dest.resolve(strict=False).relative_to(game_root)
        except (OSError, ValueError):
            raise RuntimeError(f"destination escapes game root via symlink: {projected_rel}")
        existing_owners = owners.get(projected_rel, [])
        status = "new"
        action = "install"
        allowed = ["install"]
        reason = "New managed staging file; deploy will materialise a hardlink"
        if redmod_mode == "native-compile-required":
            status, action, allowed = "redmod-blocked", "stage-only", ["stage-only"]
            reason = "REDmod source needs the official REDmod compilation step; it will be staged but not projected"
        elif dest.exists():
            if D.is_pinned(projected_rel, pins):
                status, action, allowed = "pinned", "protected", ["protected"]
                reason = "Pinned file; smart deployment will preserve the current game path"
            elif D.is_runtime_state(projected_rel, pins):
                status, action, allowed = "runtime-state", "protected", ["protected"]
                reason = "Runtime/user state; smart deployment will preserve the current game path"
            elif _same_file(src, dest):
                status, action, allowed = "identical", "skip", ["skip", "overwrite"]
                reason = "Existing file is byte-identical"
            elif mod_name in existing_owners:
                status, action, allowed = "reinstall", "overwrite", ["skip", "overwrite"]
                reason = "This staged mod already owns the projected path"
            elif existing_owners:
                status, action, allowed = "conflict", "skip", ["skip", "overwrite"]
                reason = "Projected path is provided by another managed staged mod"
            else:
                status, action, allowed = "unmanaged", "skip", ["skip", "overwrite"]
                reason = "Existing game file is not a managed hardlink; migrate or explicitly overwrite it"
        elif existing_owners:
            # An undeployed/profile-disabled managed path can still conflict even
            # when there is currently no game-tree file.
            if mod_name in existing_owners:
                status, action, allowed = "reinstall", "overwrite", ["skip", "overwrite"]
                reason = "This staged mod already owns the projected path"
            else:
                status, action, allowed = "conflict", "skip", ["skip", "overwrite"]
                reason = "Another staged mod provides this projected path"
        counts[status] = counts.get(status, 0) + 1
        rows.append({
            "path": authored_rel,
            "projected_path": projected_rel,
            "source": str(src),
            "size": src.stat().st_size if src.is_file() else 0,
            "status": status,
            "owners": existing_owners,
            "default_action": action,
            "allowed_actions": allowed,
            "reason": reason,
            "redmod_mode": redmod_mode,
        })

    disabled = False
    active_profile = SD.DEFAULT_PROFILE
    if smart:
        with SD.Store(game_root, create=False) as store:
            active_profile = store.active_profile
            row = store._mod_row(mod_name)
            if row:
                state = store.conn.execute(
                    "SELECT enabled FROM profile_mods WHERE profile=? AND mod_id=?",
                    (active_profile, row["id"]),
                ).fetchone()
                disabled = state is not None and not bool(state[0])
    else:
        disabled = D.is_disabled(mod_name, D.load_disabled(game_root))
    legacy_manifest = D.load_manifest(game_root).get("mods", {}) if not smart else {}
    return {
        "mod_name": mod_name,
        "source": source,
        "notes": notes,
        "file_count": len(rows),
        "counts": counts,
        "conflicts": counts.get("conflict", 0) + counts.get("unmanaged", 0),
        "rows": rows,
        "fomod": wizard,
        "requirements": D.detect_requirements(extract_dir),
        "disabled": disabled,
        "deployment_mode": "smart-hardlink",
        "active_profile": active_profile,
        "staging_root": str(SD.staging_root(game_root)),
        "migration_required": bool(legacy_manifest),
    }

def preview_session(session: Path, game_root: Path,
                    selections: dict[str, list[str]] | None = None) -> dict:
    session = _session_path(session)
    info = session_info(session)
    result = preview_extracted(_session_extract(session), game_root, info["mod_name"], selections)
    result["session"] = str(session)
    result["archive"] = info["archive"]
    result["archive_name"] = info["archive_name"]
    return result


def apply_session(session: Path, game_root: Path, selections: dict[str, list[str]] | None = None,
                  resolutions: dict[str, str] | None = None, default_conflict: str = "skip",
                  dry_run: bool = False, verbose: bool = False) -> dict:
    session = _session_path(session)
    game_root = game_root.expanduser().resolve()
    extract_dir = _session_extract(session)
    info = session_info(session)
    mod_name = info["mod_name"]
    preview = preview_extracted(extract_dir, game_root, mod_name, selections)
    if preview.get("migration_required"):
        raise RuntimeError(
            "legacy in-place managed files detected; run Clean Game Folder migration before installing new mods"
        )

    cfg = D.find_fomod_config(extract_dir)
    if cfg is not None:
        pairs, _wizard = build_fomod_plan(cfg, selections)
    else:
        pairs, _notes = _structure_pairs(extract_dir, game_root)
    if not pairs:
        raise RuntimeError("archive contains no installable files for this game")

    requested = {str(k).replace("\\", "/").lstrip("/"): str(v)
                 for k, v in (resolutions or {}).items() if str(v) in {"skip", "overwrite"}}
    actions: dict[str, str] = {}
    protected: list[str] = []
    for row in preview["rows"]:
        if row["default_action"] in {"skip", "overwrite"}:
            actions[row["path"]] = requested.get(
                row["path"],
                default_conflict if row["status"] in {"conflict", "unmanaged"}
                else row["default_action"],
            )
        elif row["default_action"] == "protected":
            # Preserve current game path by creating an explicit no-winner rule
            # for this authored payload.
            actions[row["path"]] = "skip"
            protected.append(row["path"])

    if dry_run:
        return {
            "ok": True, "mod_name": mod_name, "dry_run": True,
            "planned_files": len(pairs), "owned_files": len(pairs),
            "skipped": sorted(k for k, v in actions.items() if v == "skip"),
            "overwritten": sorted(k for k, v in actions.items() if v == "overwrite"),
            "pinned_preserved": protected, "runtime_preserved": [], "backed_up": [],
            "requirements": D.detect_requirements(extract_dir),
            "deployment_mode": "smart-hardlink",
        }

    smart = SD.stage_and_deploy(
        game_root, mod_name, pairs,
        resolutions=actions,
        source_archive=info.get("archive"),
        auto_deploy=True,
    )
    staged = smart["staged"]
    deployed = smart["deploy"] or {}
    skipped = sorted(k for k, v in actions.items() if v == "skip")
    overwritten = sorted(k for k, v in actions.items() if v == "overwrite")
    return {
        "ok": bool(deployed.get("ok", True)),
        "mod_name": mod_name,
        "dry_run": False,
        "planned_files": len(pairs),
        "owned_files": int(staged.get("staged_files", len(pairs))),
        "skipped": skipped,
        "overwritten": overwritten,
        "pinned_preserved": protected,
        "runtime_preserved": [],
        "backed_up": [],
        "requirements": D.detect_requirements(extract_dir),
        "deployment_mode": "smart-hardlink",
        "staging_root": str(SD.staging_root(game_root)),
        "profile": staged.get("profile"),
        "deploy": deployed,
        "archive": smart.get("archive"),
        "archive_warning": smart.get("archive_warning", ""),
        "redmod_native_compile_required": staged.get("redmod_native_compile_required", 0),
    }

def installed_state(game_root: Path) -> dict:
    game_root = game_root.expanduser().resolve()
    if SD.db_path(game_root).is_file():
        with SD.Store(game_root, create=False) as store:
            drift_paths = {item["path"] for item in store.drift().get("items", [])}
            mods = []
            for row in store.installed_mods():
                owned = store.conn.execute(
                    "SELECT projected_rel FROM files WHERE mod_id=?", (row["id"],)
                ).fetchall()
                missing = sum(1 for r in owned if not (game_root / Path(str(r[0]))).exists())
                drift = sum(1 for r in owned if str(r[0]) in drift_paths)
                mods.append({
                    "name": row["name"],
                    "status": "enabled" if row["enabled"] else "disabled",
                    "files": int(row["files"] or 0),
                    "missing": missing,
                    "drift": drift,
                    "redmod_blocked": int(row["redmod_blocked"] or 0),
                    "profile": store.active_profile,
                })
            return {
                "mods": mods, "count": len(mods), "deployment_mode": "smart-hardlink",
                "active_profile": store.active_profile,
            }

    # Legacy read-only fallback exists specifically so users can see their old
    # in-place install before running the one-time migration tool.
    manifest = D.load_manifest(game_root)
    disabled = D.load_disabled(game_root)
    mods = []
    seen_disabled = set()
    for stem, rels in sorted(manifest.get("mods", {}).items(), key=lambda kv: kv[0].lower()):
        key = D.disable_key(stem)
        scope = D.disabled_scope(stem, disabled)
        if scope:
            seen_disabled.add(key)
        mods.append({
            "name": stem,
            "status": "disabled" if scope == "all" else ("lua-disabled" if scope == "lua" else "legacy-in-place"),
            "files": len(rels or []),
            "missing": sum(1 for rel in rels or [] if not (game_root / rel).exists()),
            "disable_key": key,
        })
    for key, entry in sorted(disabled.items()):
        if key in seen_disabled:
            continue
        mods.append({
            "name": entry.get("stem", key), "status": "disabled", "files": 0,
            "missing": 0, "disable_key": key,
        })
    return {"mods": mods, "count": len(mods), "deployment_mode": "legacy-in-place"}

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Preview-first Cyberpunk 2077 mod installer")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preview", help="Extract an archive and return its install/conflict plan")
    p.add_argument("archive", type=Path)
    p.add_argument("--game-root", type=Path, required=True)
    p.add_argument("--selections", default="{}", help="FOMOD selection JSON or JSON file")

    p = sub.add_parser("fomod", help="Re-evaluate FOMOD wizard groups for an extracted session")
    p.add_argument("--session", type=Path, required=True)
    p.add_argument("--selections", default="{}")

    p = sub.add_parser("session-preview", help="Rebuild a conflict preview for an existing extracted session")
    p.add_argument("--session", type=Path, required=True)
    p.add_argument("--game-root", type=Path, required=True)
    p.add_argument("--selections", default="{}")

    p = sub.add_parser("apply", help="Install a prepared session using explicit conflict choices")
    p.add_argument("--session", type=Path, required=True)
    p.add_argument("--game-root", type=Path, required=True)
    p.add_argument("--selections", default="{}")
    p.add_argument("--resolutions", default="{}")
    p.add_argument("--default-conflict", choices=("skip", "overwrite"), default="skip")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--keep-session", action="store_true")
    p.add_argument("--ignore-running", action="store_true")

    p = sub.add_parser("list", help="List installed and durably-disabled mods")
    p.add_argument("--game-root", type=Path, required=True)

    for name in ("disable", "enable"):
        p = sub.add_parser(name, help=f"{name.title()} a managed mod")
        p.add_argument("name")
        p.add_argument("--game-root", type=Path, required=True)
        p.add_argument("--downloads", type=Path, default=D.DOWNLOADS_DEFAULT)
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--purge", action="store_true")
        p.add_argument("--ignore-running", action="store_true")

    p = sub.add_parser("cleanup", help="Remove an extracted installer session")
    p.add_argument("--session", type=Path, required=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "preview":
            if not args.game_root.exists():
                raise FileNotFoundError(f"game root not found: {args.game_root}")
            D.require_bsdtar()
            session = create_session(args.archive)
            try:
                result = preview_session(session, args.game_root, _load_json_arg(args.selections))
            except Exception:
                cleanup_session(session)
                raise
            _json_line(result)
            return 0

        if args.command == "fomod":
            session = _session_path(args.session)
            cfg = D.find_fomod_config(_session_extract(session))
            if cfg is None:
                _json_line({"fomod": None, "session": str(session)})
                return 0
            _pairs, model = build_fomod_plan(cfg, _load_json_arg(args.selections))
            _json_line({"fomod": model, "session": str(session)})
            return 0

        if args.command == "session-preview":
            if not args.game_root.exists():
                raise FileNotFoundError(f"game root not found: {args.game_root}")
            result = preview_session(args.session, args.game_root, _load_json_arg(args.selections))
            _json_line(result)
            return 0

        if args.command == "apply":
            if not args.game_root.exists():
                raise FileNotFoundError(f"game root not found: {args.game_root}")
            D.require_game_idle("mod install", args.ignore_running)
            result = apply_session(
                args.session, args.game_root,
                selections=_load_json_arg(args.selections),
                resolutions=_load_json_arg(args.resolutions),
                default_conflict=args.default_conflict,
                dry_run=args.dry_run, verbose=args.verbose,
            )
            print(f"[install] {'would install' if args.dry_run else 'installed'} {result['mod_name']}: "
                  f"{result['owned_files']} owned file(s), {len(result['skipped'])} skipped")
            _json_line(result)
            if not args.keep_session and not args.dry_run:
                cleanup_session(args.session)
            return 0

        if args.command == "list":
            if SD.db_path(args.game_root).is_file():
                with SD.deployment_lock(args.game_root, exclusive=False):
                    _json_line(installed_state(args.game_root))
            else:
                _json_line(installed_state(args.game_root))
            return 0

        if args.command in {"disable", "enable"}:
            D.require_game_idle(args.command, args.ignore_running)
            enabled = args.command == "enable"
            if SD.db_path(args.game_root).is_file():
                if args.dry_run:
                    result = installed_state(args.game_root)
                else:
                    SD.set_enabled(args.game_root, args.name, enabled, rebuild=True)
                    result = installed_state(args.game_root)
                _json_line(result)
                return 0
            # Compatibility path for a pre-migration install.
            with D.game_lock(args.game_root, f"installer {args.command}", exclusive=not args.dry_run):
                if enabled:
                    D.action_enable(args.game_root, args.name, args.dry_run)
                else:
                    D.action_disable(args.game_root, args.name, args.dry_run, purge=args.purge,
                                     force=False, downloads=args.downloads)
            _json_line(installed_state(args.game_root))
            return 0

        if args.command == "cleanup":
            cleanup_session(args.session)
            _json_line({"ok": True})
            return 0
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[!] {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
