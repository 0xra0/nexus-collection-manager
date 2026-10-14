"""Pure helpers: output-line parsing and small formatting utilities.

The backends print structured progress lines; these regexes and helpers turn that
stdout into a live progress bar and status text. Non-TTY output is ANSI-free.
"""
import os
import re
from pathlib import Path

from .config import FRAMEWORK_HINTS

_ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")
RE_QUEUED    = re.compile(r"Queued (\d+) files, ([\d.]+) GB")
RE_HEARTBEAT = re.compile(
    r"~~ (.+?)/(.+?) \(([\d.]+)%\)\s+([\d.]+) MB/s\s+ETA (\S+)\s+"
    r"active=(\d+)\s+done=(\d+)\s+failed=(\d+)")
RE_DONE      = re.compile(r"^✓ \[(\d+)/(\d+)\]\s+(.+?)(?:\s{2,}|$)")
RE_FAIL      = re.compile(r"^✗ \[(\d+)/(\d+)\]\s+(.+?)(?:\s{2,}|$)")
RE_PROCESS   = re.compile(r"^\[(\d+)/(\d+)\] Processing: (.+)")


def clean(line: str) -> str:
    """Strip the leading CR the dashboard emits and any stray ANSI codes."""
    return _ANSI.sub("", line).lstrip("\r").rstrip("\n")


def human_bytes(n: float) -> str:
    if n >= 1e9:
        return f"{n/1e9:.2f} GB"
    if n >= 1e6:
        return f"{n/1e6:.0f} MB"
    return f"{n/1e3:.0f} KB"


def dir_size(path: Path) -> int:
    """Sum of regular-file sizes in a directory (one level). Cheap enough to poll
    a few-hundred-file downloads folder every couple of seconds for a smooth bar."""
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file() and not entry.name.endswith(".aria2"):
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def slug_from(text: str) -> str:
    """Accept a bare slug or a full collection URL and return the slug (matching how
    nexus_core.downloader's main() normalizes its positional argument)."""
    m = re.search(r"/collections/([^/?#]+)", text)
    return m.group(1) if m else text.strip()


def is_framework(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in FRAMEWORK_HINTS)
