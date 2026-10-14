#!/usr/bin/env python3
"""First-run onboarding helpers for Nexus Collection Manager.

The module intentionally contains no Qt code so game detection/validation can be
unit-tested and reused by the GUI or CLI.  Nexus account validation stays in
``nexus_account``; this module focuses on Linux game discovery and a compact setup
summary.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Iterable

APP_VERSION = "0.17.6"
GAME_NAME = "Cyberpunk 2077"
APP_ID = "1091500"

# Strong markers are vanilla-owned and therefore useful before any mods exist.
GAME_MARKERS: tuple[tuple[str, int], ...] = (
    ("bin/x64/Cyberpunk2077.exe", 5),
    ("archive/pc/content", 4),
    ("r6", 2),
    ("engine", 2),
    ("archive/pc", 1),
)


def inspect_game_root(path: str | Path) -> dict:
    """Inspect a candidate Cyberpunk install root without modifying it."""
    raw = Path(path).expanduser()
    resolved = raw.resolve(strict=False)
    exists = resolved.is_dir()
    found: list[str] = []
    score = 0
    if exists:
        for rel, weight in GAME_MARKERS:
            if (resolved / rel).exists():
                found.append(rel)
                score += weight

    exe = "bin/x64/Cyberpunk2077.exe" in found
    content = "archive/pc/content" in found
    # The exe is the strongest signal.  A content+r6/engine combination is also
    # accepted because some test/minimal installs may not expose the exe yet.
    valid = bool(exists and (exe or (content and score >= 6)))
    return {
        "path": str(resolved),
        "exists": exists,
        "valid": valid,
        "score": score,
        "markers": found,
        "missing_markers": [rel for rel, _ in GAME_MARKERS if rel not in found],
        "staging_dir": str(resolved / "downloaded_mods"),
    }


def _steam_roots(home: Path | None = None) -> list[Path]:
    home = (home or Path.home()).expanduser()
    roots = [
        home / ".steam/steam",
        home / ".local/share/Steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ]
    env = os.environ.get("STEAM_COMPAT_CLIENT_INSTALL_PATH", "").strip()
    if env:
        roots.insert(0, Path(env).expanduser())
    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve(strict=False))
        if key not in seen:
            seen.add(key)
            out.append(root)
    return out


def _vdf_library_paths(vdf: Path) -> list[Path]:
    """Extract Steam library ``path`` values with a deliberately tiny VDF parser."""
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    paths: list[Path] = []
    for match in re.finditer(r'"path"\s+"([^"]+)"', text, flags=re.IGNORECASE):
        value = match.group(1).replace("\\\\", "\\").strip()
        if value:
            paths.append(Path(value).expanduser())
    return paths


def detect_game_roots(*, home: Path | None = None, extra: Iterable[str | Path] = ()) -> list[dict]:
    """Find likely Linux Cyberpunk installs and return best candidates first.

    Discovery is bounded: common Steam roots and libraries listed in
    ``libraryfolders.vdf`` are inspected.  We intentionally do not recursively walk
    arbitrary mounted disks during startup.
    """
    candidates: list[Path] = [Path(x).expanduser() for x in extra if str(x).strip()]
    for steam in _steam_roots(home):
        candidates.append(steam / "steamapps/common" / GAME_NAME)
        for library in _vdf_library_paths(steam / "steamapps/libraryfolders.vdf"):
            candidates.append(library / "steamapps/common" / GAME_NAME)

    # A handful of common externally mounted Steam-library locations are cheap to
    # inspect and useful on Linux gaming machines without causing a recursive scan.
    for parent in (Path("/mnt"), Path("/media"), Path("/run/media")):
        try:
            first_level = list(parent.iterdir()) if parent.is_dir() else []
        except OSError:
            first_level = []
        for mount in first_level[:64]:
            candidates.extend([
                mount / "SteamLibrary/steamapps/common" / GAME_NAME,
                mount / "steamapps/common" / GAME_NAME,
            ])
            try:
                second = list(mount.iterdir())[:32] if parent.name == "media" or str(parent).endswith("run/media") else []
            except OSError:
                second = []
            for child in second:
                candidates.extend([
                    child / "SteamLibrary/steamapps/common" / GAME_NAME,
                    child / "steamapps/common" / GAME_NAME,
                ])

    reports: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates:
        report = inspect_game_root(candidate)
        key = report["path"]
        if key in seen:
            continue
        seen.add(key)
        # Do not clutter the UI with completely absent/no-signal guesses.
        if report["exists"] and report["score"] > 0:
            reports.append(report)
    reports.sort(key=lambda r: (not r["valid"], -int(r["score"]), str(r["path"]).lower()))
    return reports


def _emit(payload: dict) -> None:
    print("[onboarding] " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Nexus Collection Manager guided-setup helpers")
    sub = ap.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe-game", help="validate a Cyberpunk 2077 game root")
    probe.add_argument("--game-root", type=Path, required=True)

    detect = sub.add_parser("detect-game", help="discover likely Linux Steam installs")
    detect.add_argument("--candidate", action="append", default=[], help="extra candidate path")

    args = ap.parse_args(argv)
    if args.command == "probe-game":
        result = inspect_game_root(args.game_root)
        _emit({"type": "game", **result})
        return 0 if result["valid"] else 1
    if args.command == "detect-game":
        rows = detect_game_roots(extra=args.candidate)
        _emit({"type": "games", "games": rows})
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
