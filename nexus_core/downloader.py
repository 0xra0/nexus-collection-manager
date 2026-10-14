#!/usr/bin/env python3
"""
NexusMods Collection Downloader (aria2c multi-connection edition)

Fetches every Nexus-hosted file in a collection and downloads it with aria2c
using many parallel connections per file. Nexus throttles FREE downloads
per-connection (~1.5 MB/s each), so splitting a file into ~16 connections
multiplies throughput (measured ~5.5x on this machine: 1.5 -> ~8.5 MB/s).

Notes / honesty:
  * This automates the free "slow download" endpoint. The clean, uncapped,
    fully-supported path is Nexus Premium (uncapped speed + official API key,
    no page scraping). If you have Premium, use the official API instead.
  * Hitting the download-link endpoint ~2300 times can trip rate limits (429)
    or flag an account. Keep --jobs modest and leave the polite delays on.
  * Resumable: re-run any time. Completed files are skipped, partial files are
    resumed by aria2c (--continue), so an interrupted 80 GB job just continues.

Usage:
    python3 test.py                         # defaults to the bhiwpn collection
    python3 test.py bhiwpn                   # by slug
    python3 test.py https://www.nexusmods.com/games/cyberpunk2077/collections/bhiwpn
    python3 test.py bhiwpn --jobs 3 --connections 16
    python3 test.py --verify                 # prune corrupt archives, then fill gaps
    python3 test.py --gentle                 # crawl politely to dodge the free throttle
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    # Import-safe: a missing dep must not kill a process that merely *imports* this
    # module (the GUI does, to build its argv). The CLI re-checks in main(); network
    # calls re-check in session() and raise a clear error there.
    cffi_requests = None

# ── Configuration ────────────────────────────────────────────────────────────
DEFAULT_SLUG   = "bhiwpn"
GAME_DOMAIN    = "cyberpunk2077"
GAME_ID        = 3333
DOWNLOAD_DIR   = Path("/mnt/ssd/Downloads/Mods/")
COOKIES_FILE   = Path("cookies.json")

GRAPHQL_URL    = "https://api.nexusmods.com/v2/graphql"
API_BASE       = "https://api.nexusmods.com/v1"
POPUP_URL      = "https://www.nexusmods.com/Core/Libs/Common/Widgets/DownloadPopUp"

# Download tuning
CONNECTIONS      = 8      # aria2c connections per file (-x/-s); ~8 already saturates one file
JOBS             = 4      # parallel files; more files in flight masks slow throttled tails
LINK_MIN_GAP     = 1.5    # min seconds between download-link requests (all threads)
LINK_RETRIES     = 4      # retries per file on transient/429 errors
COOLDOWN_429     = 60     # seconds to pause the link pipeline after a 429
DL_RETRIES       = 4      # whole-file attempts, each with a FRESH CDN link + resume

# --gentle preset: low, polite concurrency to avoid tripping Nexus's anti-abuse
# throttle (which drops you to ~30 KB/s aggregate). Plus a watchdog that pauses
# and waits it out if the throttle is detected anyway.
GENTLE_JOBS      = 1
GENTLE_CONNS     = 4      # ~4 x 1.5 MB/s ≈ 6 MB/s when healthy, but stays under the radar
GENTLE_GAP       = 4.0    # seconds between link requests in gentle mode
THROTTLE_FLOOR   = 0.15   # MB/s below which a sizeable file counts as "throttled"
COOLDOWN_MIN     = 600    # first throttle pause: 10 min
COOLDOWN_MAX     = 3600   # capped at 60 min (exponential backoff between)

# ── Cookie loading ───────────────────────────────────────────────────────────
def load_cookies(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"Cookie file not found: {path}")
    raw = json.load(open(path, encoding="utf-8"))
    out = []
    for c in raw:
        out.append({k.strip(): (v.strip() if isinstance(v, str) else v)
                    for k, v in c.items()})
    return out

_tls = threading.local()
_COOKIES: list[dict] = []

def session() -> "cffi_requests.Session":
    """One curl_cffi session per thread, sharing the loaded cookies."""
    if cffi_requests is None:
        raise RuntimeError("curl-cffi is required for downloading:  pip install curl-cffi")
    s = getattr(_tls, "s", None)
    if s is None:
        s = cffi_requests.Session(impersonate="chrome124")
        for c in _COOKIES:
            if c.get("name") and c.get("value"):
                s.cookies.set(c["name"], c["value"],
                              domain=c.get("domain") or "", path=c.get("path", "/"),
                              secure=bool(c.get("secure", False)))
        _tls.s = s
    return s

# ── Collection metadata ──────────────────────────────────────────────────────
COLLECTION_QUERY = """
query getCollection($slug: String!) {
  collection(slug: $slug) {
    name
    latestPublishedRevision {
      revisionNumber
      totalSize
      modFiles {
        fileId
        optional
        file { modId name uri version sizeInBytes }
      }
    }
  }
}
"""

def _int(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0

def fetch_collection(slug: str, include_optional: bool):
    print(f"Fetching collection '{slug}' via GraphQL ...")
    resp = session().post(GRAPHQL_URL,
                          json={"query": COLLECTION_QUERY, "variables": {"slug": slug}},
                          timeout=30)
    if resp.status_code != 200:
        sys.exit(f"GraphQL HTTP {resp.status_code}")
    data = resp.json()
    if "errors" in data:
        sys.exit(f"GraphQL errors: {data['errors']}")
    col = (data.get("data") or {}).get("collection")
    if not col:
        sys.exit(f"Collection '{slug}' not found (check the slug / your cookies).")
    rev = col.get("latestPublishedRevision")
    if not rev:
        sys.exit(f"Collection '{slug}' has no published revision.")

    files, manual = [], []
    for mf in rev.get("modFiles", []):
        fi = mf.get("file") or {}
        mod_id, file_id = fi.get("modId"), mf.get("fileId")
        entry = {
            "modId": mod_id,
            "fileId": file_id,
            "fileName": fi.get("uri") or fi.get("name") or f"{mod_id}-{file_id}.zip",
            "size": _int(fi.get("sizeInBytes")),
            "optional": bool(mf.get("optional")),
        }
        if not mod_id or not file_id:
            manual.append(entry)          # externally-hosted / manual download
            continue
        if entry["optional"] and not include_optional:
            continue
        files.append(entry)

    total = sum(f["size"] for f in files)
    print(f"Collection: {col['name']}  (revision {rev.get('revisionNumber')})")
    print(f"Queued {len(files)} files, {total/1e9:.1f} GB"
          + (f"  ({len(manual)} manual/external skipped)" if manual else ""))
    return files, manual, total

# ── Download-link fetching (rate limited across all threads) ──────────────────
_link_lock = threading.Lock()
_next_ok   = [0.0]     # earliest monotonic time the next link request may start

def _throttle_gate():
    now = time.monotonic()
    wait = _next_ok[0] - now
    if wait > 0:
        time.sleep(wait)
    _next_ok[0] = time.monotonic() + LINK_MIN_GAP

def get_download_urls(mod_id: int, file_id: int) -> list[str]:
    """Resolve the free CDN download URL(s) for a file. Serialized + retried."""
    mod_page = f"https://www.nexusmods.com/{GAME_DOMAIN}/mods/{mod_id}?tab=files"
    last_err = None
    for attempt in range(LINK_RETRIES):
        with _link_lock:
            _throttle_gate()
            try:
                pop = session().get(
                    POPUP_URL,
                    params={"id": file_id, "nmm": "1", "game_id": GAME_ID},
                    headers={"Referer": mod_page,
                             "X-Requested-With": "XMLHttpRequest", "Accept": "*/*"},
                    timeout=30)
                if pop.status_code == 429:
                    raise RuntimeError("429 on popup")
                pop.raise_for_status()
                txt = pop.text

                m = re.search(
                    r"nxm://[^\"'<\s]*[?&]key=([^&\"'<\s]+)[^\"'<\s]*expires=([^\"'<\s&]+)",
                    txt, re.IGNORECASE)
                if m:
                    key, expires = m.group(1), m.group(2)
                else:
                    km = re.search(r'"key"\s*:\s*"([^"]+)"', txt)
                    em = re.search(r'"expires"\s*:\s*"?(\d+)"?', txt)
                    if not (km and em):
                        raise RuntimeError("could not parse key/expires from popup")
                    key, expires = km.group(1), em.group(1)

                dl = (f"{API_BASE}/games/{GAME_DOMAIN}/mods/{mod_id}/files/{file_id}"
                      f"/download_link.json?key={key}&expires={expires}")
                r = session().get(dl, timeout=30,
                                  headers={"Referer": mod_page,
                                           "X-Requested-With": "XMLHttpRequest",
                                           "Accept": "application/json"})
                if r.status_code == 429:
                    raise RuntimeError("429 on download_link")
                r.raise_for_status()
                urls = [x.get("URI") for x in r.json() if x.get("URI")]
                if not urls:
                    raise RuntimeError("download_link returned no URLs")
                return urls
            except Exception as e:  # noqa: BLE001
                last_err = e
                if "429" in str(e):
                    _next_ok[0] = time.monotonic() + COOLDOWN_429  # global cooldown
        time.sleep(2 * (attempt + 1))     # backoff outside the lock
    raise RuntimeError(f"link fetch failed: {last_err}")

# ── File download via aria2c ─────────────────────────────────────────────────
def is_complete(dest: Path, expected: int) -> bool:
    if not dest.exists() or (dest.parent / (dest.name + ".aria2")).exists():
        return False
    if expected <= 0:
        return True                       # size unknown -> presence is good enough
    return abs(dest.stat().st_size - expected) <= 1

def aria2c_download(urls: list[str], dest: Path, conns: int) -> bool:
    cmd = [
        "aria2c",
        "--continue=true",
        f"--max-connection-per-server={conns}",
        f"--split={conns}",
        "--min-split-size=1M",
        "--max-concurrent-downloads=1",
        "--file-allocation=none",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--console-log-level=error",
        "--summary-interval=0",
        # ---- anti-stall: never let a dead CDN socket hang the file ----
        "--connect-timeout=15",           # give up on a connection that won't open
        "--timeout=30",                    # drop a connection idle >30s, retry its piece
        "--lowest-speed-limit=15K",        # if whole download drops below this, abort+retry
        "--max-tries=5",                   # then bubble up so the outer loop gets a FRESH url
        "--retry-wait=5",
        "--user-agent=Mozilla/5.0",
        "-d", str(dest.parent),
        "-o", dest.name,
        *urls,                            # multiple URLs are treated as mirrors
    ]
    return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0

def download_with_retries(mod: dict, dest: Path, conns: int) -> tuple[bool, str]:
    """Download one file, retrying the WHOLE thing with a fresh CDN link each time.

    aria2c's own --max-tries reuses the single URL it was given; if that URL has
    expired or the CDN starts refusing it, all those retries fail identically and
    the file is abandoned (this is what left 1000+ .aria2 partials). Here we catch
    that: on failure we fetch a brand-new link and let aria2c --continue resume
    from the bytes already on disk, so no progress is lost between attempts.
    """
    last = "unknown"
    for attempt in range(1, DL_RETRIES + 1):
        try:
            urls = get_download_urls(mod["modId"], mod["fileId"])
        except Exception as e:  # noqa: BLE001
            last = f"link: {e}"
            time.sleep(3 * attempt)
            continue
        aria2c_download(urls, dest, conns)          # rc ignored; verify by size
        if is_complete(dest, mod["size"]):
            return True, ""
        got = dest.stat().st_size if dest.exists() else 0
        last = f"incomplete after try {attempt} ({got/1e6:.0f}MB)"
        time.sleep(3)                                # brief pause, then fresh link
    return False, last

# ── Integrity verification (--verify) ────────────────────────────────────────
def verify_downloads(directory: Path, workers: int = 6) -> int:
    """bsdtar -tf every *finished* archive; delete any that fail so the download
    pass re-fetches them. Catches files that are the right size but silently
    truncated/corrupt (which a size check alone can't see). Partial downloads
    (those with a .aria2 control file) are left alone -- they'll just resume.
    Returns the number of corrupt files deleted."""
    if not shutil.which("bsdtar"):
        sys.exit("bsdtar not found (needed for --verify). "
                 "Install: sudo pacman -S libarchive  /  sudo apt install libarchive-tools")
    exts = {".zip", ".7z", ".rar"}
    archives = [p for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in exts
                and not (p.parent / (p.name + ".aria2")).exists()]
    total = len(archives)
    if not total:
        print("Verify: no finished archives to check.")
        return 0
    print(f"Verifying {total} finished archives in {directory} (bsdtar -tf) ...")

    counter = {"tested": 0, "bad": 0}
    bad: list[Path] = []
    lock = threading.Lock()

    def check(p: Path):
        r = subprocess.run(["bsdtar", "-tf", str(p)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with lock:
            counter["tested"] += 1
            if r.returncode != 0:
                counter["bad"] += 1
                bad.append(p)
                print(f"  [corrupt] {p.name}", flush=True)
            if counter["tested"] % 100 == 0 or counter["tested"] == total:
                print(f"  {counter['tested']}/{total} tested, "
                      f"{counter['bad']} corrupt so far", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(check, archives))

    deleted = 0
    for p in bad:
        try:
            p.unlink()
            ctl = p.parent / (p.name + ".aria2")
            if ctl.exists():
                ctl.unlink()
            deleted += 1
        except OSError as e:
            print(f"  [!] could not delete {p.name}: {e}")
    print(f"Verify: {total - counter['bad']} OK, {deleted} corrupt deleted "
          f"of {total} finished archives.\n")
    return deleted

# ── Orchestration + live dashboard ───────────────────────────────────────────
_stat_lock   = threading.Lock()
# done_bytes = every byte we "have" (downloaded + skipped) -> drives the progress bar.
# skipped_bytes is subtracted out when computing SPEED so pre-existing files don't
# register as a fake burst of throughput at startup.
_progress    = {"done_bytes": 0, "skipped_bytes": 0,
                "done": 0, "skipped": 0, "failed": 0}
_active      = {}                 # dest Path -> expected size (bytes), downloading now
_active_lock = threading.Lock()

# gentle-mode run state (set in main)
_GENTLE      = False
_STOP: "threading.Event | None" = None
_throttle      = {"streak": 0, "cooldown": COOLDOWN_MIN}
_throttle_lock = threading.Lock()

def _throttle_watch(got: int, seconds: float):
    """Gentle mode only: if a couple of sizeable files in a row arrive under the
    speed floor, Nexus's anti-abuse cap is (still) active. Pause for a while to let
    it reset, backing off exponentially, then resume. Interruptible via _STOP."""
    if not _GENTLE or got < 5_000_000:        # too small to judge speed reliably
        return
    mbps = got / 1e6 / seconds
    with _throttle_lock:
        if mbps >= THROTTLE_FLOOR:
            _throttle["streak"] = 0
            _throttle["cooldown"] = COOLDOWN_MIN
            return
        _throttle["streak"] += 1
        if _throttle["streak"] < 2:           # one slow file could just be a bad tail
            return
        cd = _throttle["cooldown"]
        _throttle["cooldown"] = min(cd * 2, COOLDOWN_MAX)

    DASH.log(f"{C_YEL}⏸  throttled ({mbps:.2f} MB/s) — Nexus free cap looks active. "
             f"Pausing {cd//60} min to let it reset (Ctrl-C to stop).{C_RESET}")
    end = time.monotonic() + cd
    while _STOP is not None and not _STOP.is_set() and time.monotonic() < end:
        _STOP.wait(min(5.0, max(0.0, end - time.monotonic())))
    DASH.log(f"{C_CYN}▶  resuming — probing whether the cap has lifted…{C_RESET}")

_IS_TTY = sys.stdout.isatty()
if _IS_TTY:
    C_RESET, C_DIM, C_BOLD = "\033[0m", "\033[2m", "\033[1m"
    C_GRN, C_RED, C_CYN, C_YEL = "\033[32m", "\033[31m", "\033[36m", "\033[33m"
else:
    C_RESET = C_DIM = C_BOLD = C_GRN = C_RED = C_CYN = C_YEL = ""

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")

def _fit(s: str, width: int) -> str:
    """Truncate a possibly-colored string to `width` VISIBLE chars so it never
    wraps (wrapping would desync the dashboard's cursor-up line count)."""
    if len(_ANSI_RE.sub("", s)) <= width:
        return s
    out, vis, i = [], 0, 0
    while i < len(s) and vis < width:
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group()); i = m.end(); continue
        out.append(s[i]); vis += 1; i += 1
    out.append(C_RESET)
    return "".join(out)

class Dashboard:
    """A live, FIXED-HEIGHT status block pinned to the bottom of the terminal.
    Completion/failure lines scroll normally above it.

    Robustness choices (learned the hard way):
      * Constant height -> the redraw always moves up exactly `height` lines, so a
        varying number of active files can't desync the cursor math.
      * Every physical line ends with '\\r\\n' and begins with '\\r' -> output is
        correct even if the terminal lost onlcr (NL->CRNL) mapping, which is what
        made an earlier version staircase across the screen.
      * '\\033[A' (cursor-up) + '\\033[K' (clear line) only -> the most universally
        supported control codes, no reliance on CPL('F') or clear-screen('J').
      * On a non-TTY (piped to a file) it prints nothing here; the monitor emits a
        plain one-liner instead.
    """
    def __init__(self, height: int = 6):
        self.lock = threading.Lock()
        self.height = height
        self.shown = False            # is a block currently on screen?

    def _erase(self):
        if self.shown:
            # up to the top of the block, then clear each line downward
            parts = [f"\033[{self.height}A"]
            for _ in range(self.height):
                parts.append("\r\033[K")
                parts.append("\033[B")            # down one
            parts.append(f"\033[{self.height}A")  # back to top
            sys.stdout.write("".join(parts))
            self.shown = False

    def log(self, text: str):
        with self.lock:
            if _IS_TTY and self.shown:
                self._erase()
            sys.stdout.write("\r" + text + "\r\n")
            sys.stdout.flush()

    def render(self, lines: list[str]):
        if not _IS_TTY:
            return
        width = shutil.get_terminal_size((100, 20)).columns - 1
        lines = (lines + [""] * self.height)[:self.height]
        lines = [_fit(ln, width) for ln in lines]
        with self.lock:
            out = []
            if self.shown:
                out.append(f"\033[{self.height}A")     # up to top of existing block
            for ln in lines:
                out.append("\r\033[K" + ln + "\r\n")   # col0, clear, content, CR+LF
            self.shown = True
            sys.stdout.write("".join(out))
            sys.stdout.flush()

    def close(self):
        with self.lock:
            if _IS_TTY:
                self._erase()
                sys.stdout.write("\033[?25h")          # ensure cursor visible again
            sys.stdout.flush()

DASH = Dashboard()

def _bar(frac: float, width: int = 24) -> str:
    frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
    fill = int(round(frac * width))
    return "█" * fill + "░" * (width - fill)

def _gb(n: float) -> str:
    return f"{n/1e9:.2f} GB" if n >= 1e9 else f"{n/1e6:.0f} MB"

def _dur(mins: float) -> str:
    if mins <= 0:
        return "--"
    if mins < 60:
        return f"{mins:.0f}m"
    h = int(mins // 60)
    return f"{h}h{int(mins - h * 60):02d}m"

def monitor(total_bytes: int, total_files: int, slots: int,
            stop: threading.Event, interval: float = 1.0):
    """Redraw a smooth, fixed-height dashboard. aria2c runs with
    --file-allocation=none, so a file's on-disk size == bytes actually fetched.

    Speed is a ~20s rolling average of DOWNLOADED bytes only (skipped/pre-existing
    bytes are subtracted), which kills both the 0.0/175 MB/s flapping and the fake
    multi-GB/s burst that skipped files used to cause at startup."""
    samples: deque = deque(maxlen=20)     # (t, downloaded_bytes_now)
    last_plain = 0.0
    while not stop.is_set():
        now = time.monotonic()
        with _active_lock:
            items = sorted(_active.items(), key=lambda kv: kv[0].name)
        live, rows = 0, []
        for dest, exp in items:
            try:
                cur = dest.stat().st_size
            except OSError:
                cur = 0
            live += cur
            rows.append((dest.name, cur, exp))
        with _stat_lock:
            done_bytes    = _progress["done_bytes"]
            skipped_bytes = _progress["skipped_bytes"]
            done, failed, skipped = (_progress["done"], _progress["failed"],
                                     _progress["skipped"])

        total_now = done_bytes + live                 # everything we have -> bar/ETA
        dl_now    = (done_bytes - skipped_bytes) + live  # only bytes we pulled -> speed
        samples.append((now, dl_now))
        if len(samples) >= 2 and samples[-1][0] > samples[0][0]:
            spd = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0])
        else:
            spd = 0.0
        spd = max(spd, 0.0)
        pct = total_now / total_bytes if total_bytes else 0.0
        eta = (total_bytes - total_now) / spd / 60 if spd > 1e4 else 0.0

        if _IS_TTY:
            lines = [
                f"{C_BOLD}{C_CYN}▼ {_gb(total_now)}{C_RESET}/{_gb(total_bytes)}  "
                f"{C_BOLD}{_bar(pct)}{C_RESET} {pct*100:5.1f}%   "
                f"{C_GRN}{spd/1e6:5.1f} MB/s{C_RESET}   ETA {C_YEL}{_dur(eta)}{C_RESET}"
            ]
            for name, cur, exp in rows[:slots]:
                frac = cur / exp if exp else 0.0
                lines.append(
                    f"  {C_DIM}{name[:34]:<34}{C_RESET} {_bar(frac, 16)} "
                    f"{frac*100:4.0f}%  {cur/1e6:5.0f}/{exp/1e6:.0f}MB")
            while len(lines) < slots + 1:              # pad idle slots -> constant height
                lines.append(f"  {C_DIM}(idle){C_RESET}")
            lines.append(
                f"  {C_DIM}done {C_GRN}{done}{C_DIM}  skipped {skipped}  "
                f"failed {C_RED}{failed}{C_DIM}  of {total_files}{C_RESET}")
            DASH.render(lines)
        elif now - last_plain >= 15:          # non-tty: one line every 15s
            last_plain = now
            DASH.log(f"~~ {_gb(total_now)}/{_gb(total_bytes)} ({pct*100:.1f}%)  "
                     f"{spd/1e6:.1f} MB/s  ETA {_dur(eta)}  active={len(rows)}  "
                     f"done={done} failed={failed}")
        stop.wait(interval)

def handle_file(mod: dict, idx: int, total_files: int, total_bytes: int,
                conns: int) -> str:
    dest = DOWNLOAD_DIR / re.sub(r'[\\/:*?"<>|]', "_", mod["fileName"])
    tag = f"{C_DIM}[{idx}/{total_files}]{C_RESET}"

    if is_complete(dest, mod["size"]):
        with _stat_lock:
            _progress["skipped"] += 1
            _progress["skipped_bytes"] += mod["size"]
            _progress["done_bytes"] += mod["size"]
        return "skip"

    with _active_lock:
        _active[dest] = mod["size"]
    t0 = time.monotonic()
    ok, err = download_with_retries(mod, dest, conns)
    dt = max(time.monotonic() - t0, 0.001)
    with _active_lock:
        _active.pop(dest, None)
    got = dest.stat().st_size if dest.exists() else 0

    if ok:
        with _stat_lock:
            _progress["done"] += 1
            _progress["done_bytes"] += got
            pct = _progress["done_bytes"] / total_bytes * 100 if total_bytes else 0
        DASH.log(f"{C_GRN}✓{C_RESET} {tag} {dest.name[:52]}  "
                 f"{C_DIM}{got/1e6:.0f} MB · {got/1e6/dt:.1f} MB/s · "
                 f"{pct:.0f}% overall{C_RESET}")
        _throttle_watch(got, dt)      # gentle mode: pause if the free cap re-engages
        return "ok"

    with _stat_lock:
        _progress["failed"] += 1
    DASH.log(f"{C_RED}✗{C_RESET} {tag} {dest.name[:52]}  {C_DIM}{err}{C_RESET}")
    return "fail"

def main():
    global _COOKIES, DOWNLOAD_DIR, LINK_MIN_GAP, _GENTLE, _STOP
    if cffi_requests is None:
        sys.exit("Please install curl-cffi:  pip install curl-cffi")
    ap = argparse.ArgumentParser(description="Nexus collection downloader (aria2c)")
    ap.add_argument("collection", nargs="?", default=DEFAULT_SLUG,
                    help="collection slug or full collection URL")
    ap.add_argument("--jobs", type=int, default=None, help="parallel files")
    ap.add_argument("--connections", type=int, default=None,
                    help="aria2c connections per file")
    ap.add_argument("--gentle", action="store_true",
                    help="low, polite concurrency (1 file x 4 conns, longer delays) to "
                         "avoid Nexus's anti-abuse throttle; auto-pauses if it detects "
                         "the ~30 KB/s cap and waits it out")
    ap.add_argument("--out", type=Path, default=DOWNLOAD_DIR, help="download directory")
    ap.add_argument("--cookies", type=Path, default=COOKIES_FILE)
    ap.add_argument("--include-optional", action="store_true",
                    help="also download files marked optional in the collection")
    ap.add_argument("--verify", action="store_true",
                    help="integrity-check finished archives (bsdtar -tf) and delete "
                         "corrupt ones before downloading, so they get re-fetched")
    args = ap.parse_args()

    # resolve concurrency: explicit flags win; else gentle preset; else normal defaults
    _GENTLE = args.gentle
    jobs = args.jobs if args.jobs is not None else (GENTLE_JOBS if _GENTLE else JOBS)
    conns = (args.connections if args.connections is not None
             else (GENTLE_CONNS if _GENTLE else CONNECTIONS))
    if _GENTLE:
        LINK_MIN_GAP = GENTLE_GAP

    if not shutil.which("aria2c"):
        sys.exit("aria2c not found. Install it:  sudo pacman -S aria2  /  sudo apt install aria2")

    # A previous hard-killed run (kill -9) can leave the tty in a broken mode
    # (e.g. no NL->CRNL), which makes everything staircase. Best-effort restore.
    if _IS_TTY:
        try:
            subprocess.run(["stty", "sane"], check=False)
        except Exception:  # noqa: BLE001
            pass

    # slug can be a bare slug or a full URL
    m = re.search(r"/collections/([^/?#]+)", args.collection)
    slug = m.group(1) if m else args.collection

    DOWNLOAD_DIR = args.out
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    if args.verify:
        verify_downloads(DOWNLOAD_DIR)     # prune corrupt archives, then download refetches them

    _COOKIES = load_cookies(args.cookies)
    names = {c.get("name") for c in _COOKIES}
    if "nexusmods_session" not in names:
        print("WARNING: 'nexusmods_session' cookie missing — downloads will likely fail.")

    files, manual, total = fetch_collection(slug, args.include_optional)
    if not files:
        sys.exit("Nothing to download.")

    per_stream = 1.47 * conns          # ~1.47 MB/s per connection when NOT capped
    eta_hr = total / 1e6 / (per_stream * jobs) / 3600
    print(f"\nDownloading with {jobs} parallel file(s) x {conns} "
          f"connections -> {DOWNLOAD_DIR}")
    if _GENTLE:
        print(f"{C_CYN}Gentle mode:{C_RESET} polite concurrency + {GENTLE_GAP:.0f}s link "
              f"gaps to stay under Nexus's anti-abuse throttle. If the ~30 KB/s cap is "
              f"detected it will pause and wait it out automatically.")
    print(f"Rough ETA if NOT throttled: ~{eta_hr:.1f} h. "
          f"(If you're currently capped at ~30 KB/s, wait ~24 h before expecting this.)\n")

    n = len(files)
    DASH.height = jobs + 2                # header + one row per job slot + footer
    if _IS_TTY:
        sys.stdout.write("\033[?25l")     # hide cursor while the block animates
        sys.stdout.flush()
    stop = threading.Event()
    _STOP = stop                          # let the throttle watchdog observe Ctrl-C
    mon = threading.Thread(target=monitor, args=(total, n, jobs, stop, 1.0),
                           daemon=True)
    mon.start()
    try:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = [ex.submit(handle_file, mod, i, n, total, conns)
                    for i, mod in enumerate(files, 1)]
            for _ in as_completed(futs):
                pass
    finally:
        stop.set()
        mon.join(timeout=2)
        DASH.close()

    print("\n=== Summary ===")
    print(f"Downloaded: {_progress['done']}   "
          f"Already had: {_progress['skipped']}   "
          f"Failed: {_progress['failed']}   of {n}")
    if manual:
        print(f"\n{len(manual)} file(s) are externally hosted and must be fetched "
              f"manually from their mod pages:")
        for e in manual:
            print(f"  - modId {e['modId']}  {e['fileName']}")
    if _progress["failed"]:
        print("\nRe-run the same command to retry failed/incomplete files "
              "(completed ones are skipped, partials resume).")

if __name__ == "__main__":
    main()
