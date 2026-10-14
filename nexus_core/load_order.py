#!/usr/bin/env python3
"""Profile-aware Cyberpunk 2077 archive load-order management.

The first archive in ``archive/pc/mod/modlist.txt`` has the highest priority:
when multiple archives contain the same resource, the first one wins.  This
module keeps the preference model in the smart-deployment SQLite database and
stores the generated ``modlist.txt`` canonically under ``downloaded_mods``.
Only a hardlink is projected into the game tree.

Manual archive order and pairwise mod preference rules are intentionally
separate:

* dragging/reordering archives produces a complete explicit archive order;
* ``before -> after`` rules are useful for durable compatibility preferences;
* adding a rule clears the complete manual archive order so the rule graph can
  become authoritative again;
* resetting can optionally preserve or clear those rules.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .smart_deployment import Store, deployment_lock

JSON_PREFIX = "[load-order-json] "


def _emit(payload: dict) -> None:
    print(JSON_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _normalise_keys(store: Store, keys: Sequence[str] | None) -> list[str]:
    current = store.archive_load_order()["items"]
    known = {str(row["key"]): row for row in current}
    out: list[str] = []
    seen: set[str] = set()
    for raw in keys or []:
        key = str(raw)
        if key in seen:
            continue
        if key not in known:
            raise KeyError(f"archive not found in active profile: {key}")
        seen.add(key); out.append(key)
    for row in current:
        key = str(row["key"])
        if key not in seen:
            seen.add(key); out.append(key)
    return out


def preview(store: Store, archive_keys: Sequence[str] | None = None) -> dict:
    """Preview modlist changes without mutating the database or game tree."""
    base = store.modlist_preview()
    if archive_keys is None:
        base["type"] = "preview"
        return base
    keys = _normalise_keys(store, archive_keys)
    by_key = {str(row["key"]): row for row in store.archive_load_order()["items"]}
    rows = [dict(by_key[key]) for key in keys]
    # Persisting a complete manual archive order clears the older mod-level
    # !ncm alias priorities. Preview the filenames that will actually exist
    # after that reconciliation, not the currently deployed aliases.
    desired = [str(row.get("natural_deployed_archive") or row["deployed_archive"]) for row in rows]
    link_changes = [
        {
            "mod": str(row.get("mod") or ""),
            "from": str(row.get("deployed_archive") or ""),
            "to": str(row.get("natural_deployed_archive") or row.get("deployed_archive") or ""),
        }
        for row in rows
        if str(row.get("deployed_archive") or "") != str(row.get("natural_deployed_archive") or row.get("deployed_archive") or "")
    ]
    current = list(base.get("current") or [])
    current_pos = {name: i for i, name in enumerate(current)}
    desired_pos = {name: i for i, name in enumerate(desired)}
    moved = [
        {"archive": name, "from": current_pos[name] + 1, "to": desired_pos[name] + 1}
        for name in desired if name in current_pos and current_pos[name] != desired_pos[name]
    ]
    for pos, row in enumerate(rows, 1):
        row["position"] = pos
    return {
        **base,
        "type": "preview",
        "desired": desired,
        "content": "".join(name + "\n" for name in desired),
        "items": rows,
        "added": [x for x in desired if x not in current_pos],
        "removed": [x for x in current if x not in desired_pos],
        "moved": moved,
        "link_changes": link_changes,
        "would_change": current != desired or bool(link_changes),
        "pending_manual_order": True,
    }


def apply_order(store: Store, archive_keys: Sequence[str] | None = None, *, force_external: bool = False) -> dict:
    if archive_keys is not None:
        keys = _normalise_keys(store, archive_keys)
        store.set_archive_order(keys)
    # Ordering changes can also remove legacy !ncm aliases left by the older
    # mod-priority mechanism. Reconcile archive hardlinks before writing a list
    # that names the final deployed files.
    deploy = store.deploy(sync_generated=False)
    result = store.sync_modlist(force_external=force_external, enable_management=True)
    return {
        "type": "apply",
        "profile": store.active_profile,
        "order": store.archive_load_order(),
        "deploy": deploy,
        "modlist": result,
        "preview": store.modlist_preview(),
    }


def disable_generation(store: Store, *, remove: bool = True) -> dict:
    """Stop auto-generation and optionally remove only our managed modlist link."""
    store.set_modlist_managed(False)
    rel = "archive/pc/mod/modlist.txt"
    row = store._generated_row(rel)
    removed = False
    preserved = False
    target = store.game_root / Path(rel)
    if remove and row is not None:
        if target.exists():
            try:
                st = target.stat()
                if (st.st_dev, st.st_ino) == (int(row["dev"]), int(row["inode"])):
                    target.unlink(); removed = True
                else:
                    preserved = True
            except OSError:
                preserved = True
        with store.conn:
            store.conn.execute("DELETE FROM generated_deployed WHERE rel_path=?", (rel,))
    return {"type": "disabled", "managed": False, "removed": removed, "preserved_external": preserved}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Manage Cyberpunk 2077 archive load order and modlist.txt")
    sub = ap.add_subparsers(dest="command", required=True)

    def root(name: str, help_text: str):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--game-root", type=Path, required=True)
        return p

    root("show", "Show effective archive order, preference rules and modlist state")
    p = root("preview", "Dry-run the effective or supplied archive order")
    p.add_argument("--archive-key", action="append", default=None,
                   help="archive key from 'show'; repeat in desired first-wins order")

    p = root("set", "Persist a complete explicit archive order without writing modlist.txt")
    p.add_argument("--archive-key", action="append", required=True,
                   help="archive key from 'show'; repeat in desired first-wins order")

    p = root("apply", "Persist optional order and generate/hardlink modlist.txt")
    p.add_argument("--archive-key", action="append", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-external", action="store_true",
                   help="back up and replace an existing unmanaged modlist.txt")

    p = root("rule-add", "Add a durable 'before mod -> after mod' preference")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.add_argument("--note", default="")

    p = root("rule-remove", "Remove a pairwise preference rule")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)

    p = root("reset", "Return to deterministic ASCII order")
    p.add_argument("--keep-rules", action="store_true")
    p.add_argument("--apply", action="store_true", help="also regenerate modlist.txt")
    p.add_argument("--force-external", action="store_true")

    p = root("disable", "Stop managing modlist.txt and remove our hardlink")
    p.add_argument("--keep-file", action="store_true", help="leave current modlist.txt in place")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    game = args.game_root.expanduser().resolve()
    try:
        if not game.is_dir():
            raise FileNotFoundError(f"game root not found: {game}")
        readonly = args.command in {"show", "preview"} or (args.command == "apply" and args.dry_run)
        with deployment_lock(game, exclusive=not readonly):
            with Store(game) as store:
                if args.command == "show":
                    result = store.modlist_preview()
                    result.update({"type": "show", "order": store.archive_load_order()})
                elif args.command == "preview":
                    result = preview(store, args.archive_key)
                elif args.command == "set":
                    result = store.set_archive_order(_normalise_keys(store, args.archive_key))
                    result["type"] = "set"
                elif args.command == "apply":
                    if args.dry_run:
                        result = preview(store, args.archive_key)
                    else:
                        result = apply_order(store, args.archive_key, force_external=args.force_external)
                elif args.command == "rule-add":
                    result = store.set_load_rule(args.before, args.after, note=args.note)
                    result["type"] = "rule-add"
                elif args.command == "rule-remove":
                    result = store.remove_load_rule(args.before, args.after)
                    result["type"] = "rule-remove"
                elif args.command == "reset":
                    result = store.clear_archive_order(clear_rules=not args.keep_rules)
                    result["type"] = "reset"
                    if args.apply:
                        result["deploy"] = store.deploy(sync_generated=False)
                        result["modlist"] = store.sync_modlist(
                            force_external=args.force_external, enable_management=True
                        )
                elif args.command == "disable":
                    result = disable_generation(store, remove=not args.keep_file)
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
