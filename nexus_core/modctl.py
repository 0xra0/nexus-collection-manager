#!/usr/bin/env python3
"""Enable/disable deployed mods without re-extracting them.

extract.py's manifest records every file each mod owns, so a mod can be taken out
of the game by moving its files aside and put back by moving them in. Moves stay on
one filesystem, so they are renames: a 4 GB mod parks in milliseconds.

Whole mods move at once. Parking loose .archive files by hand would strip an archive
away from the .xl / .reds / .tweak that reference it, and the resulting breakage would
look like a mod fault rather than the missing dependency it is.

  modctl.py status
  modctl.py list [--parked] [FILTER]
  modctl.py park   <name-substring> ...      # --force to park a framework
  modctl.py unpark <name-substring> ... | --all
  modctl.py cost                             # what each mod costs at startup
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

from . import deployer          # shared install lock + running-game check

GAME_ROOT = Path("/mnt/ssd/Cyberpunk 2077/game/prefix/drive_c/game")
PARK_DIR = Path("/mnt/ssd/.mod_parked")
PARK_STATE = PARK_DIR / "parked.json"
MANIFEST = ".mod_manifest.json"

# Parking any of these breaks every other mod, so they are held back unless forced.
FRAMEWORK_HINTS = (
    "red4ext", "cyber engine tweaks", "cet ", "archivexl", "tweakxl", "codeware",
    "redscript", "input_loader", "mod_settings", "cybercmd", "reddata",
    "redfilesystem", "redhttpclient", "audioware", "equipment-ex", "cyberware-ex",
    "virtual atelier", "native interactions", "customhackingsystem",
)
# Files that belong to the base game and were overwritten in place. extract.py kept no
# backup, so deleting or moving them damages the install rather than the mod.
VANILLA_OVERWRITES = {
    "r6/cache/inputusermappings.xml", "r6/cache/inputcontexts.xml",
    "engine/config/base/scripts.ini", "bin/x64/redengineerrorreporter.exe",
    "engine/tools/scc.exe", "engine/tools/scc_lib.dll",
}


def load_manifest():
    path = GAME_ROOT / MANIFEST
    if not path.is_file():
        sys.exit(f"No manifest at {path}")
    return json.loads(path.read_text())["mods"]


def load_parked():
    if PARK_STATE.is_file():
        return json.loads(PARK_STATE.read_text())
    return {}


def save_parked(state):
    PARK_DIR.mkdir(parents=True, exist_ok=True)
    PARK_STATE.write_text(json.dumps(state, indent=1))


def is_framework(name):
    low = name.lower()
    return any(h in low for h in FRAMEWORK_HINTS)


def classify(rel):
    """What this file costs the engine at startup."""
    s = rel.lower().replace("\\", "/")
    if s.endswith(".archive"):
        return "archive"     # an open file handle + an index the engine builds
    if s.endswith(".xl"):
        return "xl"          # ArchiveXL parses each one
    if s.endswith(".tweak"):
        return "tweak"       # TweakXL applies each one
    if s.startswith("r6/audioware/") or "/radios/" in s:
        return "audio"       # Audioware registers every clip
    if s.endswith(".reds"):
        return "reds"        # redscript compiles each one
    return "other"


def match(mods, needles):
    hits = []
    for m in mods:
        if any(n.lower() in m.lower() for n in needles):
            hits.append(m)
    return hits


def prune_empty(d: Path):
    while d != GAME_ROOT and d.is_dir():
        try:
            next(d.iterdir())
            return
        except StopIteration:
            pass
        except OSError:
            return
        parent = d.parent
        try:
            d.rmdir()
        except OSError:
            return
        d = parent


def cmd_status(mods, parked, _args):
    active = {m: f for m, f in mods.items() if m not in parked}
    counts = {}
    for files in active.values():
        for rel in files:
            counts[classify(rel)] = counts.get(classify(rel), 0) + 1
    print(f"active mods : {len(active)}")
    print(f"parked mods : {len(parked)}")
    print()
    print("startup load of the ACTIVE set:")
    for k in ("archive", "xl", "tweak", "audio", "reds", "other"):
        if counts.get(k):
            print(f"  {k:8} {counts[k]:6}")
    n = counts.get("archive", 0)
    print()
    print(f"  archives {n} -> ~{n + 57 + 1600} fds needed "
          f"(engine holds one per archive; vanilla adds 57)")
    if n > 600:
        print(f"  [!] {n} archives is far past a normal heavy load (~100-400).")
        print("      The engine watchdog aborts if startup blocks the game thread >120s.")


def cmd_list(mods, parked, args):
    src = parked if args.parked else {m: f for m, f in mods.items() if m not in parked}
    for m in sorted(src):
        if args.filter and args.filter.lower() not in m.lower():
            continue
        tag = "FRAMEWORK" if is_framework(m) else ""
        print(f"  {len(mods[m]):5} files  {m[:70]:<72} {tag}")
    print(f"\n{len(src)} mod(s) {'parked' if args.parked else 'active'}")


def cmd_cost(mods, parked, args):
    """Rank active mods by what they cost the engine at startup."""
    rows = []
    for m, files in mods.items():
        if m in parked:
            continue
        c = {}
        for rel in files:
            k = classify(rel)
            c[k] = c.get(k, 0) + 1
        # audio clips and archives dominate startup; weight them accordingly
        score = c.get("archive", 0) * 3 + c.get("audio", 0) + c.get("xl", 0) + c.get("tweak", 0)
        rows.append((score, m, c))
    rows.sort(reverse=True)
    print(f"{'score':>6}  {'arch':>5} {'audio':>6} {'xl':>4} {'tweak':>6}  mod")
    for score, m, c in rows[:args.top]:
        print(f"{score:6}  {c.get('archive',0):5} {c.get('audio',0):6} "
              f"{c.get('xl',0):4} {c.get('tweak',0):6}  {m[:60]}")


def cmd_park(mods, parked, args):
    if args.from_file:
        # exact names, one per line -- how the bisect feeds a computed set back in
        wanted = {ln.strip() for ln in Path(args.from_file).read_text().splitlines()
                  if ln.strip()}
        targets = [m for m in mods if m in wanted]
        missing = wanted - set(targets)
        if missing:
            print(f"[!] {len(missing)} name(s) in the file are not in the manifest")
    else:
        targets = match(mods, args.names)
    targets = [t for t in targets if t not in parked]
    if not targets:
        sys.exit("Nothing to park (no match, or already parked).")

    fw = [t for t in targets if is_framework(t)]
    if fw and not args.force:
        print("Refusing to park framework mods (everything else depends on them):")
        for t in fw:
            print(f"  - {t}")
        print("Re-run with --force if you really mean it.")
        targets = [t for t in targets if t not in fw]
        if not targets:
            return

    # A file shared with a mod that stays active must not be moved.
    keep = set()
    for m, files in mods.items():
        if m not in targets and m not in parked:
            keep.update(f.lower() for f in files)

    moved = shared = skipped = 0
    for mod in targets:
        stored = []
        for rel in mods[mod]:
            low = rel.lower().replace("\\", "/")
            if low in VANILLA_OVERWRITES:
                skipped += 1
                continue
            if rel.lower() in keep:
                shared += 1
                continue
            src = GAME_ROOT / rel
            if not src.is_file():
                continue
            dst = PARK_DIR / mod / rel
            if args.dry_run:
                moved += 1
                stored.append(rel)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            prune_empty(src.parent)
            moved += 1
            stored.append(rel)
        if not args.dry_run:
            parked[mod] = stored
    if not args.dry_run:
        save_parked(parked)
    verb = "Would park" if args.dry_run else "Parked"
    print(f"{verb} {len(targets)} mod(s): {moved} file(s) moved out"
          + (f", {shared} shared file(s) left in place" if shared else "")
          + (f", {skipped} vanilla file(s) left alone" if skipped else ""))


def cmd_unpark(_mods, parked, args):
    targets = list(parked) if args.all else match(parked, args.names)
    if not targets:
        sys.exit("Nothing to unpark.")
    restored = 0
    for mod in targets:
        for rel in parked[mod]:
            src = PARK_DIR / mod / rel
            if not src.is_file():
                continue
            dst = GAME_ROOT / rel
            if args.dry_run:
                restored += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            restored += 1
        if not args.dry_run:
            parked.pop(mod, None)
            shutil.rmtree(PARK_DIR / mod, ignore_errors=True)
    if not args.dry_run:
        save_parked(parked)
    verb = "Would restore" if args.dry_run else "Restored"
    print(f"{verb} {len(targets)} mod(s): {restored} file(s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    p = sub.add_parser("list")
    p.add_argument("filter", nargs="?")
    p.add_argument("--parked", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("cost")
    p.add_argument("--top", type=int, default=25)
    p.set_defaults(fn=cmd_cost)

    p = sub.add_parser("park")
    p.add_argument("names", nargs="*")
    p.add_argument("--from-file", metavar="FILE",
                   help="park the exact mod names listed in FILE, one per line")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_park)

    p = sub.add_parser("unpark")
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_unpark)

    ap.add_argument("--ignore-running", action="store_true",
                    help="proceed even if Cyberpunk2077.exe/scc.exe is running (unsafe)")
    args = ap.parse_args()
    if args.cmd == "unpark" and not args.names and not args.all:
        ap.error("give a name or --all")

    # Same install, same lock as the deployer. This is the OTHER process that mutates the
    # game tree, so it has to participate in the guard or the guard means nothing: park and
    # unpark move real files and rewrite the manifest the deployer is reading.
    mutating = args.cmd in ("park", "unpark") and not args.dry_run
    if mutating:
        deployer.require_game_idle(f"modctl {args.cmd}", args.ignore_running)
    with deployer.game_lock(GAME_ROOT, f"modctl {args.cmd}", exclusive=mutating):
        args.fn(load_manifest(), load_parked(), args)


if __name__ == "__main__":
    main()
