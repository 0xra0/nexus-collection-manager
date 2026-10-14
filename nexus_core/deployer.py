#!/usr/bin/env python3
"""
Cyberpunk 2077 Mod Deployer (v13 - TF-IDF Mod Scanner & Grouping)
Uses bsdtar (libarchive) to natively extract .zip, .7z and .rar files on Linux,
then applies Vortex-style heuristics to merge each mod into the game directory.

What v13 adds
-------------
* Mod Scanner & Grouping : Recursively scans configured mod roots and groups scattered
                           files using sparse TF-IDF vectors + cosine-distance DBSCAN.
* Integrity Tracking     : Computes SHA-256 for every discovered file and compares against
                           an atomic per-game snapshot stored outside the install.
* Scanner Analysis       : Reports file/group counts, disk usage and the largest inferred
                           mod groups without writing guessed ownership into the manifest.
* Update Checker         : Checks installed mods against NexusMods API for newer versions.
* Hash Verification      : Verifies archive MD5 hashes against NexusMods file list API.
* NexusMods API          : Fetches and displays mod metadata during deployment.
* Safe Deletion          : Moves removed mod files to a .mod_trash directory.
* Req. Informing         : Detects and verifies mod requirements.
* Case Normalization     : Forces standard CP2077 directories to lowercase for Linux/Proton.
"""
import argparse
import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

# ── Configuration & Logging ──────────────────────────────────────────────────
CONFIG_DIR = Path.home() / ".config" / "cp2077_modder"
CONFIG_FILE = CONFIG_DIR / "config.json"

def load_config():
    if CONFIG_FILE.exists():
        try: return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {}

def save_config(cfg: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception: pass

_cfg = load_config()
GAME_ROOT_DEFAULT = Path(_cfg.get("game_root", "/mnt/ssd/Cyberpunk 2077/game/prefix/drive_c/game"))
DOWNLOADS_DEFAULT = Path(_cfg.get("downloads", "/mnt/ssd/Downloads"))
TMP_BASE_DEFAULT  = Path(_cfg.get("tmp", "/mnt/ssd"))
NEXUS_API_KEY = _cfg.get("nexus_api_key", "")

class OutputLogger:
    def __init__(self, log_file: Path):
        self.terminal = sys.stdout
        self.log_file = log_file
        self.log = None
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            self.log = open(log_file, "a", encoding="utf-8")
        except Exception: pass

    def write(self, message):
        self.terminal.write(message)
        if self.log: self.log.write(message)

    def flush(self):
        self.terminal.flush()
        if self.log: self.log.flush()


def _install_cli_logger():
    """Create the config dir and tee stdout to mod_deployer.log. Called only from
    main() — importing this module (e.g. from the GUI) must not touch the filesystem
    or hijack the parent process's stdout."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    sys.stdout = OutputLogger(CONFIG_DIR / "mod_deployer.log")

TMP_DIR_NAME      = ".mod_extract_tmp"
MANIFEST_NAME     = ".mod_manifest.json"
KNOWN_DIRS   = {'archive', 'bin', 'r6', 'engine', 'red4ext', 'mods', 'soundbanks', 'local'}
ARCHIVE_EXTS = {'.zip', '.7z', '.rar'}
CET_MODS_REL = Path("bin/x64/plugins/cyber_engine_tweaks/mods")

STANDARD_DIRS_LOWER = {
    'archive', 'pc', 'mod', 'bin', 'x64', 'plugins', 'cyber_engine_tweaks', 'mods',
    'r6', 'scripts', 'tweaks', 'config', 'red4ext', 'engine', 'soundbanks', 'local',
    'cache', 'tools', 'data', 'core', 'saves', 'screenshots'
}

# The game's own directory structure, as lowercase path prefixes. Case is normalized only
# while a path is still INSIDE this structure; the first component that leaves it (a mod's
# own folder) ends normalization and everything below keeps the archive's casing verbatim.
#
# Normalizing at every depth instead is what manufactured the case-duplicate bugs: names
# like 'scripts', 'data' and 'core' are game dirs at the top but ordinary mod folders lower
# down, so 'red4ext/plugins/Codeware/Scripts' became '.../scripts' and
# 'r6/scripts/DisclosedHiddenGems/Core' became '.../core'. Where anything else had already
# written the true casing, that left two folders differing only by case: on Windows one
# folder, on this case-sensitive filesystem two — and redscript then declared every type in
# them twice (SYM_REDEFINITION).
# ── Concurrency guard ────────────────────────────────────────────────────────
# Nothing stopped two processes from mutating one install at the same time. When a second
# session ran the manager while the game was up, .mod_manifest.json, r6/cache/final.redscripts
# (+ .bk/.ts) and r6/logs/redscript_rCURRENT.log were all rewritten mid-diagnosis: A/B tests
# silently compared states that no longer existed, and one launch failed in an unrelated way
# purely from the interference. Two independent writers are the hazard, so there are two
# guards — a lock against other copies of this tool, and a process check against the game
# and the redscript compiler, which write the same files but know nothing about our lock.
LOCK_NAME = ".mod_manager.lock"
# Basenames that mean the install is live. scc.exe is the redscript compiler: it rewrites the
# script cache and the log the health check reads.
BUSY_EXE_NAMES = {"cyberpunk2077.exe", "scc.exe", "redprelauncher.exe"}

def _proc_cmdline(pid: str) -> list:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return [t for t in fh.read().decode("utf-8", "replace").split("\0") if t]
    except OSError:
        return []

def find_running_game_processes() -> list:
    """[(pid, exe_name)] for every live Cyberpunk / redscript-compiler process."""
    out = []
    me = str(os.getpid())
    if not os.path.isdir("/proc"):
        return out
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        hit = None
        try:
            with open(f"/proc/{pid}/comm", "r") as fh:
                comm = fh.read().strip().lower()
        except OSError:
            comm = ""
        # /proc/<pid>/comm is truncated at 15 chars, so Cyberpunk2077.exe appears as
        # 'cyberpunk2077.e' — match by prefix or the game is never detected at all.
        for name in BUSY_EXE_NAMES:
            if comm and (comm == name or (len(comm) == 15 and name.startswith(comm))):
                hit = name
                break
        if hit is None:
            for tok in _proc_cmdline(pid):
                if re.split(r'[\\/]', tok)[-1].lower() in BUSY_EXE_NAMES:
                    hit = re.split(r'[\\/]', tok)[-1].lower()
                    break
        if hit:
            out.append((int(pid), hit))
    return out

def _lock_holder(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return "another process"
    who = f"pid {data.get('pid', '?')}"
    if data.get("action"): who += f" running '{data['action']}'"
    if data.get("started"): who += f", started {data['started']}"
    if data.get("host"): who += f", on {data['host']}"
    return who

@contextlib.contextmanager
def game_lock(game_root: Path, action: str, exclusive: bool = True):
    """Serialize access to one game install via flock on <game_root>/.mod_manager.lock.

    Mutating work takes the lock exclusively; read-only work takes it shared, so a report
    can never be generated from a half-written tree. flock is released by the kernel when
    the process dies, so a crash cannot leave a stale lock behind — there is deliberately
    no override flag, because a live holder is never safe to ignore.

    A filesystem that cannot lock (some network mounts) warns and proceeds: refusing there
    would make the tool unusable, and the warning is honest about what is not guaranteed."""
    path = game_root / LOCK_NAME
    try:
        fh = open(path, "a+", encoding="utf-8")
    except OSError as e:
        print(f"  [!] Could not open lock file {path} ({e}) — proceeding WITHOUT a lock. "
              "Make sure nothing else is touching this install.")
        yield False
        return
    try:
        try:
            fcntl.flock(fh.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                        | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"[!] This game install is locked by {_lock_holder(path)}.\n"
                     f"    Refusing to run '{action}' — two processes writing one install is "
                     "what corrupts the manifest and the redscript cache.\n"
                     "    Wait for it to finish, or stop that process, then retry.")
        except OSError as e:
            print(f"  [!] Locking unsupported on this filesystem ({e}) — proceeding WITHOUT "
                  "a lock. Make sure nothing else is touching this install.")
            yield False
            return
        if exclusive:
            with contextlib.suppress(OSError):
                fh.seek(0); fh.truncate()
                json.dump({"pid": os.getpid(), "action": action, "host": os.uname().nodename,
                           "started": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                          fh)
                fh.flush()
        yield True
    finally:
        if exclusive:
            with contextlib.suppress(OSError):
                fh.seek(0); fh.truncate(); fh.flush()
        fh.close()          # releases the flock

def require_game_idle(action: str, ignore_running: bool = False):
    """Refuse to mutate the install while the game or the redscript compiler is running.
    They write the manifest's files, the script cache and the redscript log without any
    knowledge of our lock, so the lock alone does not make this safe."""
    procs = find_running_game_processes()
    if not procs:
        return
    listed = ", ".join(f"{name} (pid {pid})" for pid, name in sorted(procs))
    if ignore_running:
        print(f"  [!] {listed} still running — continuing anyway (--ignore-running). "
              "Changes may be overwritten or partially read by that process.")
        return
    sys.exit(f"[!] Refusing to run '{action}': {listed} is running.\n"
             "    Cyberpunk and scc.exe rewrite the script cache, the redscript log and mod "
             "config files on exit,\n    so deploying now can be silently undone or produce a "
             "half-written install.\n    Close the game and retry (or pass --ignore-running "
             "if you are certain it is safe).")

STANDARD_PATH_PREFIXES = frozenset({
    'archive', 'archive/pc', 'archive/pc/mod', 'archive/pc/content',
    'bin', 'bin/x64', 'bin/x64/plugins', 'bin/x64/plugins/cyber_engine_tweaks',
    'bin/x64/plugins/cyber_engine_tweaks/mods',
    'r6', 'r6/scripts', 'r6/tweaks', 'r6/config', 'r6/cache', 'r6/logs', 'r6/input',
    'red4ext', 'red4ext/plugins',
    'engine', 'engine/config', 'engine/tools',
    'mods', 'soundbanks', 'local',
})

def normalize_rel_path(rel_path: Path) -> Path:
    parts = list(rel_path.parts)
    if not parts: return rel_path
    filename = parts[-1]
    dirs = parts[:-1]
    normalized_dirs = []
    acc = ""
    inside = True
    for d in dirs:
        candidate = f"{acc}/{d.lower()}" if acc else d.lower()
        if inside and candidate in STANDARD_PATH_PREFIXES:
            normalized_dirs.append(d.lower())
            acc = candidate
        else:
            # Left the game's structure — this is mod-owned territory, keep its casing.
            inside = False
            normalized_dirs.append(d)
    return Path(*normalized_dirs, filename) if normalized_dirs else Path(filename)

ARCHIVE_MAGICS = (
    b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08',
    b'Rar!\x1a\x07',
    b'7z\xbc\xaf\x27\x1c',
)

LOOSE_DESTS = {
    '.archive': Path("archive/pc/mod"),
    '.xl':      Path("archive/pc/mod"),
    '.reds':    Path("r6/scripts"),
    '.tweak':   Path("r6/tweaks"),
    '.dll':     Path("red4ext/plugins"),
    '.wav':     Path("soundbanks"),
    '.bnk':     Path("soundbanks"),
}
LOOSE_EXTS      = set(LOOSE_DESTS)
LOOSE_WARN_EXTS = {'.dll'}

# `ver` must START with a digit (after an optional 'v') but may then contain letters, so
# real Nexus version strings like '1-4d' and '3-2-1b1' parse. Letting it start with a
# letter instead would let a name such as 'Mod-2-Something-12345-1-0-<epoch>' split at the
# '2' and report mod-id 2; the digit anchor forces the correct split at 12345.
NEXUS_NAME_RE = re.compile(r'^(?P<name>.+?)-(?P<mid>\d+)-(?P<ver>v?\d[\w.\-]*?)-(?P<epoch>1[0-9]{9})$')
NEXUS_NAME_RE2 = re.compile(r'^(?P<name>.+?) (?P<mid>\d+) (?P<ver>[\w.]+) (?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}Z) \S+$')

JUNK_FILES = {
    'thumbs.db', '.ds_store', 'desktop.ini', '.gitignore',
    'readme.txt', 'readme.md', 'license.txt', 'changelog.txt', 'manifest.json'
}
JUNK_DIRS = {'__macosx', '.git', 'fomod', 'options', '__pystache__', '.vscode'}

# ── MD5 Hashing ──────────────────────────────────────────────────────────────
def compute_md5(file_path: Path, chunk_size: int = 8192) -> str:
    md5 = hashlib.md5()
    try:
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk: break
                md5.update(chunk)
        return md5.hexdigest()
    except OSError as e:
        print(f"  [!] Could not read {file_path.name} for hashing: {e}")
        return ""

# ── NexusMods API ────────────────────────────────────────────────────────────
NEXUS_GAME_DOMAIN = "cyberpunk2077"

def _nexus_request_ex(url: str, api_key: str):
    """Perform a Nexus API GET, returning (data_or_None, http_status_or_None).
    Exposing the status lets callers tell a genuine 404 (resource/hash not found)
    from a transport error, which matters for hash verification."""
    req = urllib.request.Request(url)
    req.add_header('apikey', api_key)
    req.add_header('Application-Name', 'CP2077-Mod-Deployer')
    req.add_header('Application-Version', '1.0')
    req.add_header('User-Agent', 'CP2077-Mod-Deployer/1.0 (Linux)')
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode('utf-8')), response.status
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  [!] Nexus API error {e.code}: {e.reason}")
        return None, e.code
    except Exception as e:
        print(f"  [!] Nexus API request failed: {e}")
        return None, None

def _nexus_request(url: str, api_key: str) -> dict | list | None:
    data, _status = _nexus_request_ex(url, api_key)
    return data

def fetch_nexus_metadata(mod_id: str, api_key: str) -> dict | None:
    url = f"https://api.nexusmods.com/v1/games/{NEXUS_GAME_DOMAIN}/mods/{mod_id}.json"
    return _nexus_request(url, api_key)

def fetch_nexus_files(mod_id: str, api_key: str) -> list | None:
    url = f"https://api.nexusmods.com/v1/games/{NEXUS_GAME_DOMAIN}/mods/{mod_id}/files.json"
    data = _nexus_request(url, api_key)
    if data and isinstance(data, dict): return data.get("files", [])
    if isinstance(data, list): return data
    return None

def fetch_nexus_files_full(mod_id: str, api_key: str) -> dict | None:
    """The full files.json payload — {'files': [...], 'file_updates': [...]} — needed for
    precise update detection (file_updates links each file to its successor)."""
    url = f"https://api.nexusmods.com/v1/games/{NEXUS_GAME_DOMAIN}/mods/{mod_id}/files.json"
    data = _nexus_request(url, api_key)
    return data if isinstance(data, dict) else None

def _newest_file_in_chain(file_id, file_updates) -> int:
    """Follow Nexus's file_updates (old_file_id → new_file_id) from `file_id` to the end
    of its lineage, so a file is compared only against its own successors, never against
    an unrelated 'main' file of the same mod."""
    nxt = {u.get('old_file_id'): u.get('new_file_id') for u in file_updates
           if u.get('old_file_id') and u.get('new_file_id')}
    cur, seen = file_id, set()
    while cur in nxt and cur not in seen:
        seen.add(cur)
        cur = nxt[cur]
    return cur

def _norm_ver(v) -> str:
    return str(v).lower().strip().lstrip('v').replace('-', '.')

def _file_size_bytes(entry: dict) -> int:
    """Bytes for a Nexus files.json entry. The API reports 'size' in KiB (legacy) and
    'size_in_bytes' exactly; prefer the exact field, fall back to KiB*1024, then 0. A 0
    means 'unknown' downstream — is_complete() then trusts file presence over a size match,
    so an update whose size Nexus omits still downloads, it just can't be size-verified."""
    for k in ("size_in_bytes", "size_kb", "size"):
        v = entry.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v) if k == "size_in_bytes" else int(v) * 1024
    return 0

def _match_installed_file(files, installed_epoch: int, installed_ver):
    """Find the installed file among a mod's Nexus files. Primary match is the upload
    timestamp within a one-minute window — exact for the ...-<epoch> filename format, and
    tolerant of the collection format's minute-truncated timestamp. Falls back to a
    version match when the timestamp isn't representable."""
    window = [f for f in files if isinstance(f.get('uploaded_timestamp'), int)
              and 0 <= f['uploaded_timestamp'] - installed_epoch < 60]
    if window:
        exact = [f for f in window if f['uploaded_timestamp'] == installed_epoch]
        if exact:
            return exact[0]
        if installed_ver:                       # disambiguate same-minute uploads by version
            vmatch = [f for f in window if _norm_ver(f.get('version', '')) == _norm_ver(installed_ver)]
            if vmatch:
                return vmatch[0]
        return window[0]
    if installed_ver:
        norm = _norm_ver(installed_ver)
        cands = [f for f in files if _norm_ver(f.get('version', '')) == norm]
        cands.sort(key=lambda f: 0 if f.get('category_name') != 'OLD_VERSION' else 1)
        if cands:
            return cands[0]
    return None

def check_file_update(mod_id: str, installed_epoch: int, installed_ver, api_key: str, cache: dict):
    """Precise per-file update check. Locates the installed file on Nexus, then follows
    the file_updates chain to its newest successor. Returns (status, detail) with status
    'update' | 'current' | 'unknown'; for 'update', detail is a dict with keys
    installed_ver, latest_ver, latest_file, file_id, size, epoch — everything a download
    needs, so the report and the one-click fetch share one source of truth."""
    if mod_id not in cache:
        cache[mod_id] = fetch_nexus_files_full(mod_id, api_key)
    data = cache[mod_id]
    if not data:
        return ('unknown', 'could not fetch file list')
    files = data.get('files', [])
    by_id = {f.get('file_id'): f for f in files}
    inst = _match_installed_file(files, installed_epoch, installed_ver)
    if inst is None:
        return ('unknown', 'installed file not listed on Nexus (archived or removed)')
    newest_id = _newest_file_in_chain(inst.get('file_id'), data.get('file_updates', []))
    if newest_id == inst.get('file_id'):
        return ('current', inst.get('version', '?'))
    newest = by_id.get(newest_id, {})
    return ('update', {
        'installed_ver': inst.get('version', '?'),
        'latest_ver': newest.get('version', '?'),
        'latest_file': newest.get('file_name', f'file_id {newest_id}'),
        'file_id': newest_id,
        'size': _file_size_bytes(newest),
        'epoch': newest.get('uploaded_timestamp'),
    })

def verify_nexus_hash(archive_path: Path, api_key: str) -> tuple[str, str, str]:
    """Verify an archive against NexusMods by MD5.

    Uses the md5_search endpoint, NOT the files.json list: the files list carries no
    per-file md5, so comparing against it always fails (every file looked "corrupt").
    md5_search reverse-looks-up the hash — HTTP 200 means Nexus has a file with exactly
    this md5 (a byte-for-byte unmodified official file → verified); a 404 means the hash
    isn't indexed (a renamed/repacked/older upload, or a genuinely modified file), which
    can't be proven to be corruption, so it is reported as 'unknown', never 'mismatch'."""
    if not api_key:
        return ("no_key", "", "No API key configured")
    local_md5 = compute_md5(archive_path)
    if not local_md5:
        return ("error", "", "Failed to compute local MD5")
    url = (f"https://api.nexusmods.com/v1/games/{NEXUS_GAME_DOMAIN}"
           f"/mods/md5_search/{local_md5}.json")
    data, status = _nexus_request_ex(url, api_key)
    if status == 404:
        return ("unknown", "", f"MD5 {local_md5} is not indexed on Nexus "
                               "(renamed/repacked/older upload — not proof of corruption)")
    if data is None:
        return ("error", "", f"Could not reach Nexus to verify MD5 {local_md5}")
    if not isinstance(data, list) or not data:
        return ("unknown", "", f"No Nexus match for MD5 {local_md5}")

    # Any match confirms the bytes are an official Nexus file (md5 collisions are
    # not a practical concern). Prefer the entry whose mod_id matches the filename.
    key = nexus_key(archive_path)
    want_mod = str(key[1]) if key else None
    chosen = None
    for entry in data:
        mod = entry.get("mod", {}) or {}
        if want_mod is not None and str(mod.get("mod_id")) == want_mod:
            chosen = entry
            break
    chosen = chosen or data[0]
    fd = chosen.get("file_details", {}) or {}
    mod = chosen.get("mod", {}) or {}
    got = (fd.get("md5") or "").lower().strip()
    if got and got != local_md5.lower():
        return ("mismatch", "", f"Nexus returned md5 {got} for a {local_md5} search (unexpected)")
    name = fd.get("file_name") or fd.get("name") or "?"
    version = fd.get("version", "?")
    file_id = fd.get("file_id", "?")
    modname = mod.get("name", "?")
    return ("verified", name,
            f"MD5 match → {modname}: {name} (v{version}, file_id={file_id})")

# ── Extraction ───────────────────────────────────────────────────────────────
def extract_archive(archive_path: Path, dest_dir: Path) -> bool:
    try:
        subprocess.run(['bsdtar', '-xf', str(archive_path), '-C', str(dest_dir)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as e:
        if any(dest_dir.iterdir()): return True
        err = e.stderr.decode('utf-8', errors='ignore').strip()
        print(f"  [!] bsdtar failed for {archive_path.name}: {err}")
        return False
    except Exception as e:
        print(f"  [!] Unexpected extraction error for {archive_path.name}: {e}")
        return False

# ── Structure detection ──────────────────────────────────────────────────────
def find_candidates(extract_dir: Path):
    candidates, notes = [], set()
    for root, dirs, files in os.walk(extract_dir):
        root_path = Path(root)
        parts_lower = [p.lower() for p in root_path.parts]
        if 'fomod' in parts_lower:
            notes.add('fomod')
            continue
        if 'options' in parts_lower: notes.add('options')
        for d in dirs:
            if d.lower() in KNOWN_DIRS: candidates.append(('known', root_path / d))
        if 'init.lua' in files and root_path.name.lower() not in KNOWN_DIRS: candidates.append(('cet', root_path))
        if 'mod.json' in files or 'info.json' in files: candidates.append(('redmod', root_path))
        for fn in files:
            if os.path.splitext(fn)[1].lower() in LOOSE_EXTS: candidates.append(('loose', root_path / fn))
    return candidates, notes

def _under(path: Path, bases) -> bool:
    return any(path == b or b in path.parents for b in bases)

def resolve_candidates(candidates):
    known = sorted((p for k, p in candidates if k == 'known'), key=lambda p: len(p.parts))
    selected_known = []
    for p in known:
        if not _under(p, selected_known): selected_known.append(p)
    resolved = [('known', p) for p in selected_known]
    selected_other = []
    for kind, p in candidates:
        if kind not in ('cet', 'redmod'): continue
        if _under(p, selected_known) or _under(p, selected_other): continue
        selected_other.append(p)
        resolved.append((kind, p))
    covered = selected_known + selected_other
    for kind, p in candidates:
        if kind != 'loose': continue
        if _under(p, covered): continue
        resolved.append(('loose', p))
    return resolved

def dest_base_for(kind: str, cpath: Path, game_root: Path) -> Path:
    if kind == 'known':
        name = cpath.name.lower() if cpath.name.lower() in STANDARD_DIRS_LOWER else cpath.name
        return game_root / name
    if kind == 'cet': return game_root / CET_MODS_REL / cpath.name
    if kind == 'redmod': return game_root / "mods" / cpath.name
    raise ValueError(kind)

def iter_files(base: Path):
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d.lower() not in JUNK_DIRS]
        rp = Path(root)
        for f in files:
            if f.lower() in JUNK_FILES: continue
            yield rp / f

# ── FOMOD installer ──────────────────────────────────────────────────────────
def find_fomod_config(extract_dir: Path):
    for root, _, files in os.walk(extract_dir):
        if Path(root).name.lower() == 'fomod':
            for f in files:
                if f.lower() == 'moduleconfig.xml': return Path(root) / f
    return None

def _parse_xml(path: Path):
    raw = path.read_bytes()
    try: return ET.fromstring(raw)
    except ET.ParseError:
        if raw[:2] in (b'\xff\xfe', b'\xfe\xff'): txt = raw.decode('utf-16', 'ignore')
        elif raw[:3] == b'\xef\xbb\xbf': txt = raw.decode('utf-8-sig', 'ignore')
        else: txt = raw.decode('utf-8', 'ignore')
        txt = re.sub(r'^\s*<\?xml[^>]*\?>', '', txt)
        return ET.fromstring(txt)

def _ln(tag: str) -> str:
    """Local name of a possibly namespaced XML tag."""
    return tag.split('}')[-1]

def _safe_archive_rel(rel: str) -> str | None:
    """Return a normalized archive-relative path or ``None`` when it can escape.

    FOMOD XML is untrusted input.  On Linux ``Path("C:/x")`` is not absolute, so
    drive-style paths need an explicit check in addition to ``..``/leading slash.
    """
    text = (rel or '').replace('\\', '/').strip()
    if not text:
        return ''
    if text.startswith('/') or re.match(r'^[A-Za-z]:($|/)', text):
        return None
    parts = []
    for part in text.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            return None
        parts.append(part)
    return '/'.join(parts)

def _ci_resolve(base: Path, rel: str):
    """Resolve a safe, possibly wrong-cased FOMOD path under ``base``.

    The result is guaranteed to remain beneath ``base`` even if an existing archive
    component is a symlink.  Unsafe/escaping paths return ``None``.
    """
    safe = _safe_archive_rel(rel)
    if safe is None:
        return None
    root = base.resolve()
    cur = root
    for part in safe.split('/') if safe else []:
        nxt = cur / part
        if nxt.exists():
            cur = nxt
        else:
            match = None
            try:
                low = part.lower()
                for entry in os.scandir(cur):
                    if entry.name.lower() == low:
                        match = cur / entry.name
                        break
            except OSError:
                return None
            if match is None:
                return None
            cur = match
        try:
            cur.resolve().relative_to(root)
        except (OSError, ValueError):
            return None
    return cur

# ── FOMOD dependency evaluation ───────────────────────────────────────────────
# Flag dependencies are evaluated exactly (we own the flag state built from the
# plugin choices below). File/game/version dependencies can't be checked reliably
# against a Cyberpunk install from here, so they're treated permissively (assumed
# satisfied) — this errs toward installing rather than silently dropping files.
def _eval_deps(node, flags) -> bool:
    if node is None:
        return True
    op = (node.get('operator') or 'And').lower()
    results = []
    for child in node:
        tag = _ln(child.tag)
        if tag == 'dependencies':
            results.append(_eval_deps(child, flags))
        elif tag == 'flagDependency':
            results.append(flags.get(child.get('flag'), '') == (child.get('value') or ''))
        elif tag in ('fileDependency', 'gameDependency', 'fommDependency',
                     'foseDependency', 'globalDependency'):
            results.append(True)
        # unknown children are ignored
    if not results:
        return True
    return all(results) if op != 'or' else any(results)

def _plugin_type(plugin, flags) -> str:
    """Resolve a plugin's effective type, honouring a flag-conditional dependencyType."""
    td = plugin.find('./typeDescriptor')
    if td is None:
        return 'Optional'
    t = td.find('./type')
    if t is not None and t.get('name'):
        return t.get('name')
    dep = td.find('./dependencyType')
    if dep is not None:
        for pat in dep.findall('./patterns/pattern'):
            if _eval_deps(pat.find('./dependencies'), flags):
                pt = pat.find('./type')
                if pt is not None and pt.get('name'):
                    return pt.get('name')
        dt = dep.find('./defaultType')
        if dt is not None and dt.get('name'):
            return dt.get('name')
    return 'Optional'

def _select_plugins(group, flags):
    """Auto-select a group's plugins with sensible defaults (no GUI): Required always
    installs, Recommended is preferred, and the group's cardinality type is respected."""
    gtype = group.get('type', 'SelectExactlyOne')
    typed = [(p, _plugin_type(p, flags)) for p in group.findall('./plugins/plugin')]
    allp   = [p for p, _ in typed]
    req    = [p for p, t in typed if t == 'Required']
    rec    = [p for p, t in typed if t == 'Recommended']
    usable = [p for p, t in typed if t != 'NotUsable']
    if gtype == 'SelectAll':
        return allp
    if gtype == 'SelectAny':
        # "Any number, incl. all": honour curated defaults if the author set any,
        # otherwise install every usable option so nothing the mod offers is dropped
        # (a SelectAny of all-Optional plugins, e.g. Load Begone, would otherwise
        # auto-select nothing and install an empty mod).
        return (req + rec) or usable
    if gtype == 'SelectAtMostOne':
        return (rec[:1] or req[:1])           # prefer recommended; else none
    if gtype == 'SelectAtLeastOne':
        return (req + rec) or usable or allp[:1]
    # SelectExactlyOne (and any unknown type): exactly one, best available
    for pref in (rec, req, usable, allp):
        if pref:
            return [pref[0]]
    return []

def _apply_flags(plugin, flags):
    cf = plugin.find('./conditionFlags')
    if cf is None:
        return
    for flag in cf.findall('./flag'):
        name = flag.get('name')
        if name is not None:
            flags[name] = (flag.text or '').strip()

def _files_of(container):
    """(kind, source, destination, priority) from a container's <files> child."""
    out = []
    fe = container.find('./files')
    if fe is None:
        return out
    for e in fe:
        tag = _ln(e.tag)
        if tag in ('file', 'folder'):
            out.append((tag, e.get('source', ''), e.get('destination'),
                        int(e.get('priority') or 0)))
    return out

def _order_nodes(nodes, order):
    """Apply a FOMOD order attribute (Explicit keeps document order)."""
    if (order or 'Ascending').lower() == 'explicit':
        return list(nodes)
    ordered = sorted(nodes, key=lambda n: (n.get('name') or '').lower())
    if (order or '').lower() == 'descending':
        ordered.reverse()
    return ordered

def plan_fomod(config_path: Path):
    """Plan a FOMOD install by applying default selections. Handles requiredInstallFiles,
    ordered installSteps (with visibility conditions, group/plugin type descriptors and
    condition flags) and flag-gated conditionalFileInstalls. Returns (pairs, summary)
    where pairs is a list of (real_source_path, game-relative-dest) tuples."""
    fomod_root = config_path.parent.parent
    try:
        root = _parse_xml(config_path)
    except ET.ParseError as e:
        return [], f"unreadable ModuleConfig.xml ({e}) — skipped"

    flags: dict = {}
    elems: list = []          # (kind, source, destination, priority) in install order

    # 1. Required install files — always, regardless of any selection.
    rif = root.find('./requiredInstallFiles')
    if rif is not None:
        for e in rif:
            tag = _ln(e.tag)
            if tag in ('file', 'folder'):
                elems.append((tag, e.get('source', ''), e.get('destination'),
                              int(e.get('priority') or 0)))

    # 2. Install steps, in order — pick plugins, accumulate their files and flags.
    steps_root = root.find('./installSteps')
    steps = _order_nodes(root.findall('./installSteps/installStep'),
                         steps_root.get('order') if steps_root is not None else 'Ascending')
    groups = selections = 0
    for step in steps:
        vis = step.find('./visible')
        if vis is not None and not _eval_deps(vis, flags):
            continue
        og = step.find('./optionalFileGroups')
        for grp in _order_nodes(step.findall('./optionalFileGroups/group'),
                                og.get('order') if og is not None else 'Ascending'):
            groups += 1
            chosen = _select_plugins(grp, flags)
            for p in chosen:            # flags first, so later steps/conditions see them
                _apply_flags(p, flags)
            for p in chosen:
                selections += 1
                elems.extend(_files_of(p))

    # 3. Conditional file installs — evaluate each pattern against the final flags.
    conds = 0
    for pat in root.findall('./conditionalFileInstalls/patterns/pattern'):
        if _eval_deps(pat.find('./dependencies'), flags):
            got = _files_of(pat)
            if got:
                conds += 1
                elems.extend(got)

    # 4. Materialize (dest, src, priority, order) staging entries, resolving each
    #    source case-insensitively and expanding folders into their files.
    staged = []
    idx = 0
    for kind, source, destination, priority in elems:
        source_rel = _safe_archive_rel(source or '')
        if source_rel is None:
            continue
        src = _ci_resolve(fomod_root, source_rel)
        if src is None:
            continue
        # destination absent -> mirror the source path; empty string -> game root.
        # Reject traversal/absolute destinations before they ever reach _place().
        raw_dest = source_rel if destination is None else destination
        dest = _safe_archive_rel(raw_dest)
        if dest is None:
            continue
        if kind == 'file' and src.is_file():
            staged.append((dest or src.name, src, priority, idx)); idx += 1
        elif kind == 'folder' and src.is_dir():
            for f in iter_files(src):
                rel = f.relative_to(src).as_posix()
                staged.append((f"{dest}/{rel}" if dest else rel, f, priority, idx)); idx += 1
        elif src.is_file():                      # a "folder" author-pointed at one file
            staged.append((dest or src.name, src, priority, idx)); idx += 1

    # 5. Resolve destination collisions: highest FOMOD priority wins, ties by order.
    best: dict = {}
    for dest, src, priority, i in staged:
        cur = best.get(dest)
        if cur is None or (priority, i) >= (cur[1], cur[2]):
            best[dest] = (src, priority, i)
    pairs = [(entry[0], dest) for dest, entry
             in sorted(best.items(), key=lambda kv: kv[1][2])]

    summary = (f"applied defaults: {selections} option(s) over {groups} group(s)"
               + (f", {conds} conditional set(s)" if conds else "")
               + f" → {len(pairs)} file(s)")
    return pairs, summary

def check_dependencies(cpath: Path, mod_name: str, manifest_mods: dict, game_root: Path):
    deps_found = []
    for meta_file in ('mod.json', 'info.json', 'manifest.json'):
        meta_path = cpath / meta_file
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text(encoding='utf-8'))
                deps = data.get('dependencies', data.get('requirements', []))
                if isinstance(deps, list):
                    for d in deps:
                        if isinstance(d, str): deps_found.append(d)
                        elif isinstance(d, dict) and 'name' in d: deps_found.append(d['name'])
                elif isinstance(deps, dict): deps_found.extend(deps.keys())
            except Exception: pass
    has_lua = has_reds = has_dll = has_xl = False
    for root, _, files in os.walk(cpath):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext == '.lua': has_lua = True
            elif ext == '.reds': has_reds = True
            elif ext == '.dll': has_dll = True
            elif ext == '.xl': has_xl = True
        if has_lua and has_reds and has_dll and has_xl: break
    if has_lua and not any("cyber engine tweaks" in d.lower() or "cet" == d.lower() for d in deps_found): deps_found.append("Cyber Engine Tweaks (CET)")
    if has_reds and not any("redscript" in d.lower() for d in deps_found): deps_found.append("Redscript")
    if has_dll and not any("red4ext" in d.lower() for d in deps_found): deps_found.append("RED4ext")
    if has_xl and not any("archivexl" in d.lower() or "tweakxl" in d.lower() for d in deps_found): deps_found.append("ArchiveXL / TweakXL")
    if not deps_found: return
    unique_deps = list(dict.fromkeys(deps_found))
    print(f"  [i] {mod_name} requirements:")
    for dep in unique_deps:
        dep_lower = dep.lower()
        is_installed = False
        for installed_mod in manifest_mods.keys():
            if dep_lower in installed_mod.lower() or installed_mod.lower() in dep_lower:
                is_installed = True
                break
        if not is_installed:
            if "cyber engine tweaks" in dep_lower or "cet" in dep_lower:
                if (game_root / "bin/x64/plugins/cyber_engine_tweaks").exists(): is_installed = True
            elif "redscript" in dep_lower:
                if (game_root / "r6/scripts").exists() and any((game_root / "r6/scripts").iterdir()): is_installed = True
            elif "red4ext" in dep_lower:
                if (game_root / "red4ext").exists(): is_installed = True
            elif "archivexl" in dep_lower or "tweakxl" in dep_lower or "xl" in dep_lower:
                if (game_root / "archive/pc/mod").exists() or (game_root / "red4ext/plugins").exists(): is_installed = True
        status = "✓ Installed" if is_installed else "✗ MISSING"
        print(f"      - {dep} [{status}]")

def check_installed_requirements(game_root: Path, manifest_mods: dict):
    print("\n=== Requirements Check (Installed Mods) ===")
    if not manifest_mods:
        print("  No mods installed.")
        return
    missing_reqs = False
    installed_mod_names = set(manifest_mods.keys())
    def is_core_installed(dep_name: str) -> bool:
        name = dep_name.lower()
        if "cyber engine tweaks" in name or "cet" in name: return (game_root / "bin/x64/plugins/cyber_engine_tweaks").exists()
        if "redscript" in name: return (game_root / "r6/scripts").exists() or (game_root / "engine/tools").exists()
        if "red4ext" in name: return (game_root / "red4ext").exists()
        if "archivexl" in name or "tweakxl" in name or "xl" in name:
            plugins_dir = game_root / "red4ext/plugins"
            if plugins_dir.exists(): return any("xl" in p.name.lower() for p in plugins_dir.iterdir() if p.is_file())
            return False
        return False
    for mod_name, files in manifest_mods.items():
        deps = set()
        meta_files = [f for f in files if f.endswith('mod.json') or f.endswith('info.json') or f.endswith('manifest.json')]
        for meta_rel in meta_files:
            meta_path = game_root / meta_rel
            if meta_path.exists():
                try:
                    data = json.loads(meta_path.read_text(encoding='utf-8'))
                    raw_deps = data.get('dependencies', data.get('requirements', []))
                    if isinstance(raw_deps, list):
                        for d in raw_deps:
                            if isinstance(d, str): deps.add(d)
                            elif isinstance(d, dict) and 'name' in d: deps.add(d['name'])
                    elif isinstance(raw_deps, dict): deps.update(raw_deps.keys())
                except Exception: pass
        has_lua = any(f.lower().endswith('.lua') for f in files)
        has_reds = any(f.lower().endswith('.reds') for f in files)
        has_dll = any(f.lower().endswith('.dll') for f in files)
        has_xl = any(f.lower().endswith('.xl') for f in files)
        if has_lua: deps.add("Cyber Engine Tweaks (CET)")
        if has_reds: deps.add("Redscript")
        if has_dll: deps.add("RED4ext")
        if has_xl: deps.add("ArchiveXL / TweakXL")
        missing = []
        for dep in deps:
            dep_lower = dep.lower()
            if any(dep_lower in m.lower() or m.lower() in dep_lower for m in installed_mod_names): continue
            if is_core_installed(dep): continue
            missing.append(dep)
        if missing:
            missing_reqs = True
            print(f"  [!] {mod_name} is missing: {', '.join(missing)}")
    if not missing_reqs: print("  [✓] All installed mods have their requirements met.")

# ── Requirement detection (informational — deployment never blocks on it) ─────
def _dep_mentions(deps, *keywords) -> bool:
    return any(any(k in d.lower() for k in keywords) for d in deps)

def detect_requirements(extract_dir: Path) -> list:
    """Best-effort list of what a mod needs: dependencies declared in any metadata file
    it ships (mod.json/info.json/manifest.json) plus the frameworks implied by the file
    types it contains (.reds→Redscript, .dll→RED4ext, .lua→CET, .xl→ArchiveXL/TweakXL).
    Friendly names, deduplicated, order preserved."""
    deps: list = []
    exts: set = set()
    for root, _dirs, files in os.walk(extract_dir):
        rp = Path(root)
        for f in files:
            exts.add(os.path.splitext(f)[1].lower())
            if f.lower() in ('mod.json', 'info.json', 'manifest.json'):
                try:
                    data = json.loads((rp / f).read_text(encoding='utf-8'))
                except Exception:
                    continue
                raw = data.get('dependencies', data.get('requirements', []))
                if isinstance(raw, list):
                    for d in raw:
                        if isinstance(d, str): deps.append(d)
                        elif isinstance(d, dict) and d.get('name'): deps.append(d['name'])
                elif isinstance(raw, dict):
                    deps.extend(raw.keys())
    if '.lua' in exts and not _dep_mentions(deps, 'cyber engine tweaks', 'cet'):
        deps.append("Cyber Engine Tweaks (CET)")
    if '.reds' in exts and not _dep_mentions(deps, 'redscript'):
        deps.append("Redscript")
    if '.dll' in exts and not _dep_mentions(deps, 'red4ext'):
        deps.append("RED4ext")
    if '.xl' in exts and not _dep_mentions(deps, 'archivexl', 'tweakxl'):
        deps.append("ArchiveXL / TweakXL")
    return list(dict.fromkeys(deps))

# Each known framework: the keywords that identify it in a mod/archive name, and the
# specific marker file(s) it installs. Marker *files* (not bare dirs) matter — a mod
# that ships .reds creates r6/scripts and one that ships .dll creates red4ext/, so a
# directory check would let a dependent mod mask the very framework it's missing.
_FRAMEWORKS = [
    (("cyber engine tweaks", "cyberenginetweaks"),
     ("bin/x64/plugins/cyber_engine_tweaks.asi", "bin/x64/plugins/cyber_engine_tweaks/init.lua")),
    (("archivexl",), ("red4ext/plugins/ArchiveXL/ArchiveXL.dll",)),
    (("tweakxl",),   ("red4ext/plugins/TweakXL/TweakXL.dll",)),
    (("red4ext",),   ("red4ext/RED4ext.dll", "bin/x64/plugins/RED4ext.dll")),
    (("redscript",), ("engine/tools/scc.exe", "r6/config/cybercmd/scc.exe.toml")),
]

def _framework_for(dep_lower: str):
    """Map a requirement name to (keywords, markers) if it is a known framework.
    Handles the combined 'ArchiveXL / TweakXL' auto-label and the generic '…xl'."""
    if "cyber engine tweaks" in dep_lower or dep_lower == "cet" or dep_lower.startswith("cet "):
        return _FRAMEWORKS[0]
    has_axl = "archivexl" in dep_lower
    has_txl = "tweakxl" in dep_lower
    if has_axl and has_txl:            # combined label from mixed .xl files
        return (("archivexl", "tweakxl"),
                _FRAMEWORKS[1][1] + _FRAMEWORKS[2][1])
    if has_axl: return _FRAMEWORKS[1]
    if has_txl: return _FRAMEWORKS[2]
    if dep_lower.endswith("xl"):
        return (("archivexl", "tweakxl"), _FRAMEWORKS[1][1] + _FRAMEWORKS[2][1])
    if "red4ext" in dep_lower: return _FRAMEWORKS[3]
    if "redscript" in dep_lower: return _FRAMEWORKS[4]
    return None

def requirement_satisfied(dep: str, installed_names, game_root: Path) -> bool:
    """True if `dep` looks satisfied. Frameworks match by keyword against installed/
    queued mod names OR by their marker file on disk; other declared dependencies fall
    back to a loose name match."""
    dl = dep.lower()
    fw = _framework_for(dl)
    if fw is not None:
        keywords, markers = fw
        if any(any(k in m.lower() for k in keywords) for m in installed_names):
            return True
        return any((game_root / r).exists() for r in markers)
    # arbitrary declared dependency (a mod name from metadata) — loose substring match
    return any(dl in m.lower() or m.lower() in dl for m in installed_names)

# ── Framework version policy (frameworks only: always the latest, never downgrade) ─
# Core frameworks are libraries every dependent mod loads; the newest build is always the
# right one, and deploying an OLDER build — or leaving a stale duplicate manifest entry
# that re-deploys the old files — breaks every dependent. The classic symptom is an old
# red4ext/RED4ext.dll failing newer plugins' native validation ("Missing native class
# 'Audioware.*' / 'RedData.Json.*'"). Keyed by Nexus mod-id so a mod that merely *mentions*
# a framework in its name (e.g. "Judy's Apartment (ArchiveXL)") is never treated as one.
FRAMEWORK_MOD_IDS = {
    "2380":  "RED4ext",
    "1511":  "Redscript",
    "4198":  "ArchiveXL",
    "4197":  "TweakXL",
    "7780":  "Codeware",
    "107":   "Cyber Engine Tweaks",
    "12001": "Audioware",
    "14139": "RedData",
    "30726": "Redscript Config Framework",
}
_FW_MODID_RE = re.compile(r'^.+?[-_ ](\d{2,6})[-_ ]v?\d')
_FW_EPOCH_RE = re.compile(r'(1[0-9]{9})(?!\d)')

def framework_mod_id(stem: str):
    """The Nexus mod-id embedded in an archive/mod stem (first id after the name), or None.
    Tolerant of both the `Name-id-ver-epoch` and `Name id ver ts` filename formats and of
    a 'v'-prefixed version (e.g. Audioware-12001-v1-9-2-...)."""
    m = _FW_MODID_RE.match(stem)
    return m.group(1) if m else None

def framework_name(stem: str):
    """Friendly framework name if `stem` is a KNOWN core framework (by mod-id), else None."""
    return FRAMEWORK_MOD_IDS.get(framework_mod_id(stem) or "")

def framework_version_key(stem: str) -> int:
    """Total order for two builds of the same framework: the Nexus upload epoch (higher =
    newer). Epoch is reliable even when the version string is unusual."""
    eps = _FW_EPOCH_RE.findall(stem)
    if eps:
        return int(eps[-1])
    k = nexus_key_from_stem(stem)
    return k[2] if k else 0

def framework_version_str(stem: str) -> str:
    k = nexus_key_from_stem(stem)
    if k and k[3]:
        return str(k[3]).lstrip('v').replace('-', '.')
    return "?"

def installed_frameworks(manifest_mods: dict) -> dict:
    """mod_id -> [entry, ...] newest first, for every installed mod that is a known
    framework. Each entry is {stem, epoch, ver, files}. More than one entry for a mod_id
    means a stale duplicate — the exact 'old build re-deployed' cause."""
    out: dict = {}
    for stem, files in (manifest_mods or {}).items():
        mid = framework_mod_id(stem)
        if mid and mid in FRAMEWORK_MOD_IDS:
            out.setdefault(mid, []).append({
                "stem": stem, "epoch": framework_version_key(stem),
                "ver": framework_version_str(stem), "files": files,
            })
    for entries in out.values():
        entries.sort(key=lambda e: e["epoch"], reverse=True)
    return out

def newest_framework_downloads(downloads: Path) -> dict:
    """mod_id -> (archive_path, epoch) for the newest cached build of each known framework."""
    out: dict = {}
    if not downloads.exists():
        return out
    for f in downloads.iterdir():
        if not (f.is_file() and is_archive_file(f)):
            continue
        mid = framework_mod_id(f.stem)
        if mid and mid in FRAMEWORK_MOD_IDS:
            ep = framework_version_key(f.stem)
            if mid not in out or ep > out[mid][1]:
                out[mid] = (f, ep)
    return out

def dedupe_framework_manifest(game_root: Path, mods: dict, dry_run: bool, ts: str = "") -> list:
    """Keep only the newest manifest entry per framework mod-id. Older/duplicate entries are
    removed; any file an older entry owns that the kept entry does NOT (a stale old-framework
    file) is archived to the reversible backup dir. Mutates `mods`; returns a report list."""
    ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = game_root / DUP_BACKUP_DIRNAME / ts
    actions = []
    for mid, entries in installed_frameworks(mods).items():
        if len(entries) < 2:
            continue
        keep = entries[0]
        keep_files = set(keep["files"])
        for stale in entries[1:]:
            moved = 0
            for rel in (f for f in stale["files"] if f not in keep_files):
                fp = game_root / rel
                if not fp.exists():
                    continue
                if not dry_run:
                    dest = backup_root / f"framework_{FRAMEWORK_MOD_IDS[mid]}_{stale['ver']}" / rel
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(fp), str(dest))
                    except OSError:
                        continue
                moved += 1
            if not dry_run:
                mods.pop(stale["stem"], None)
            actions.append({"framework": FRAMEWORK_MOD_IDS[mid], "keep_ver": keep["ver"],
                            "drop": stale["stem"], "drop_ver": stale["ver"], "orphans_moved": moved})
    return actions

# ── Mod identity: one version of a mod may be deployed at a time ─────────────
# A manifest key is an ARCHIVE stem, so two versions of one mod are two different keys and
# nothing ever tied them together: deploying v1.1.0 added a second entry and left the
# v1.0.4 entry (and its files) in place. Both then "owned" the same paths, and whichever
# ran last decided what was on disk — that is the root cause behind the RED4ext downgrade,
# the Stealthrunner double-install, and the DisclosedHiddenGems `Core/` + `core/` split
# that aborted the whole redscript compile (SYM_REDEFINITION). The identity below is what
# ties versions of one mod together so the older one can be retired properly.
def _prune_empty_dirs(game_root: Path, dirs) -> int:
    """Remove directories left empty after files were taken out of them, walking upward.
    An emptied case-variant folder is not harmless: it keeps registering as a case-collision
    and drags the next --fix-duplicates run into choosing between a full folder and a husk."""
    pruned = 0
    for d in sorted(set(dirs), key=lambda p: len(p.parts), reverse=True):
        cur = d
        while cur != game_root and cur.is_dir() and not any(cur.iterdir()):
            parent = cur.parent
            try: cur.rmdir()
            except OSError: break
            pruned += 1
            cur = parent
    return pruned

def _ident_name(name: str) -> str:
    """Fold a mod name to its identity form. Spacing and punctuation drift between uploads
    ('Use Your Fridge -  Las Palapas Tower' vs '... - Las Palapas Tower' is the same file
    downloaded twice), so only the alphanumerics carry identity."""
    return re.sub(r'[^a-z0-9]', '', name.lower())

def mod_identity(stem: str):
    """(mod_id, folded_name) for an archive/manifest stem, or None when it carries no Nexus
    id. Deliberately parsed with the epoch-anchored NEXUS_NAME_RE rather than the tolerant
    framework_mod_id(): that one reads 'Additional Stuff 01-11077-...' as mod '01' and finds
    a bogus id in un-Nexus names like 'e6_07_60_e607608f-...'.

    The NAME is part of the key, not just the id: one Nexus mod-id routinely hosts many
    separately-downloadable files (mod 27740 ships one fridge per apartment), and those are
    siblings to be co-installed, not versions of each other."""
    key = nexus_key_from_stem(stem)
    if not key: return None
    return (key[1], _ident_name(key[0]))

# ─────────────────────── Human names for mods ───────────────────────
# A manifest key is whatever the download happened to be called, so a mod can be effectively
# unidentifiable: "City of Dreams Menu Backgrounds" is keyed
# `v2_All in one Vortex Installer-8333-1-5-1716634855` — the FOMOD installer's filename,
# which says nothing about a menu. Finding it meant tracing an archive filename
# (!CityOfDreamsLogo.archive) back through the manifest by hand.
#
# The key itself is deliberately NOT changed. It is unique, stable, and every part of this
# file plus 1442 live entries depend on it; re-keying would be a migration with nothing to
# gain, because the key is good PROVENANCE — it records exactly which file was installed.
# What was missing is the human NAME, so that is what gets stored: a mod-id -> name cache,
# consulted for display and matching. Cached in CONFIG_DIR rather than the game folder
# because mod-id -> name is a fact about Nexus, not about one install.
MOD_NAMES_NAME = "mod_names.json"

def load_mod_names() -> dict:
    try:
        data = json.loads((CONFIG_DIR / MOD_NAMES_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    names = data.get("names") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in names.items()} if isinstance(names, dict) else {}

def save_mod_names(names: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / MOD_NAMES_NAME
    payload = {"updated": datetime.now().isoformat(timespec="seconds"), "names": names}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')
    tmp.replace(path)

def stem_display_name(stem: str) -> str:
    """The best name obtainable from the filename alone: the name span of the Nexus filename
    pattern, with original casing (nexus_key_from_stem folds it for comparison).

    Uses the epoch-anchored patterns rather than a "strip everything after the first number"
    rule. That shortcut turns `Frame Mod 13-20013-1-0-1700000000` into "Frame Mod", because
    the 13 in the NAME looks exactly like a mod-id — the same mis-split the version regex is
    anchored to avoid."""
    for rx in (NEXUS_NAME_RE, NEXUS_NAME_RE2):
        m = rx.match(stem)
        if m:
            return m.group('name').replace('_', ' ').strip() or stem
    return stem

def mod_display_name(stem: str, names: dict | None = None) -> str:
    """The Nexus mod name when known, else the filename-derived one. Never raises and never
    returns empty — this is used in every listing, including offline."""
    ident = mod_identity(stem)
    mid = ident[0] if ident else framework_mod_id(stem)
    real = (names or {}).get(mid or "")
    return real or stem_display_name(stem)

def describe_mod(stem: str, names: dict | None = None) -> str:
    """'Nexus Name  [stem]' for listings — the name to read plus the provenance to act on.
    Collapses to just the stem when the two would be redundant."""
    disp = mod_display_name(stem, names)
    return stem if disp == stem else f"{disp}   [{stem}]"

def fetch_mod_names(mod_ids, api_key: str, names: dict | None = None,
                    refresh: bool = False) -> tuple:
    """Fill in missing mod-id -> name entries from the Nexus API. Returns (names, fetched,
    failed). Only unknown ids are requested unless refresh, so a second run costs nothing."""
    names = dict(names or {})
    fetched, failed = 0, []
    for mid in sorted({m for m in mod_ids if m}):
        if mid in names and not refresh:
            continue
        meta = fetch_nexus_metadata(mid, api_key)
        name = (meta or {}).get("name") if isinstance(meta, dict) else None
        if name:
            names[mid] = str(name)
            fetched += 1
        else:
            failed.append(mid)
    return names, fetched, failed

def manifest_mod_ids(mods: dict) -> set:
    out = set()
    for stem in mods or {}:
        ident = mod_identity(stem)
        mid = ident[0] if ident else framework_mod_id(stem)
        if mid:
            out.add(mid)
    return out

def action_fetch_names(game_root: Path, api_key: str, refresh: bool = False):
    mods = load_manifest(game_root)["mods"]
    ids = manifest_mod_ids(mods)
    names = load_mod_names()
    missing = {i for i in ids if i not in names}
    print("\n=== Mod Names (Nexus mod-id -> name) ===")
    print(f"  {len(ids)} mod-id(s) installed; {len(names)} cached; "
          f"{len(missing) if not refresh else len(ids)} to fetch.")
    if not api_key:
        print("  [!] No Nexus API key. Pass --nexus-api-key or set it in the config; "
              "listings fall back to the filename until then.")
        return
    if not missing and not refresh:
        print("  [✓] Nothing to fetch.")
        return
    names, fetched, failed = fetch_mod_names(ids, api_key, names, refresh)
    save_mod_names(names)
    print(f"  [✓] Fetched {fetched} name(s); cache now holds {len(names)}.")
    if failed:
        print(f"  [i] {len(failed)} id(s) returned nothing (deleted or hidden mod pages): "
              + ", ".join(failed[:10]) + ("..." if len(failed) > 10 else ""))

def installed_versions(manifest_mods: dict) -> dict:
    """identity -> [{stem, epoch, ver, files:set}, ...] newest first, for every manifest
    entry that carries a Nexus id."""
    out: dict = {}
    for stem, files in (manifest_mods or {}).items():
        ident = mod_identity(stem)
        if ident is None: continue
        out.setdefault(ident, []).append({
            "stem": stem, "epoch": framework_version_key(stem),
            "ver": framework_version_str(stem), "files": set(files),
        })
    for entries in out.values():
        entries.sort(key=lambda e: e["epoch"], reverse=True)
    return out

def find_version_duplicates(manifest_mods: dict) -> list:
    """Manifest entries that are older VERSIONS of a mod that is also installed newer.

    Sharing an identity is only a candidacy test; what proves two entries are versions of
    one another is that they claim the SAME game paths. Mod 27740's twenty fridges share an
    id, a name and even a version string while touching entirely disjoint files — retiring
    them as 'older versions' would uninstall nineteen working mods. So an older entry is
    superseded only where its path set intersects a newer one's; disjoint siblings are left
    alone.

    The intersection is case-INSENSITIVE. Two builds of one mod routinely differ only in
    folder casing (Codeware's `Scripts/` vs `scripts/`), which is two distinct paths to this
    filesystem but one path to the game through Wine — comparing exactly would call them
    disjoint and miss precisely the collision that breaks the redscript compile.
    Returns [{ident, keep, drops:[{entry, shared}], ...}]."""
    groups = []
    for ident, entries in sorted(installed_versions(manifest_mods).items()):
        if len(entries) < 2: continue
        for e in entries:
            e["_lower"] = {f.lower() for f in e["files"]}
        survivors = [entries[0]]
        drops = []
        for cand in entries[1:]:
            shared = set()
            for s in survivors:
                shared |= (cand["_lower"] & s["_lower"])
            if shared: drops.append({"entry": cand, "shared": shared})
            else: survivors.append(cand)          # a sibling file, not an older version
        if drops:
            groups.append({"ident": ident, "keep": survivors[0], "drops": drops})
    return groups

def dedupe_mod_versions(game_root: Path, mods: dict, dry_run: bool, ts: str = "") -> list:
    """Enforce one-version-per-mod. For each mod installed at several versions, keep the
    newest and retire the rest: files the retired version owns that the kept one does NOT
    are archived to the reversible backup dir (leaving them is what produced the stray
    `Core/` beside the new `core/`), and the stale manifest entries are dropped.

    A file another INSTALLED mod also owns is never touched. Mutates `mods`; returns a
    report list."""
    ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = game_root / DUP_BACKUP_DIRNAME / ts
    groups = find_version_duplicates(mods)
    retiring = {d["entry"]["stem"] for g in groups for d in g["drops"]}
    # Everything that stays installed keeps its files, whoever else listed them.
    protected = set()
    for stem, files in mods.items():
        if stem not in retiring: protected.update(files)
    actions = []
    emptied: list = []
    for g in groups:
        keep = g["keep"]
        for drop in g["drops"]:
            stale = drop["entry"]
            moved = 0
            for rel in sorted(f for f in stale["files"] if f not in protected):
                fp = game_root / rel
                if not fp.exists(): continue
                if not dry_run:
                    dest = backup_root / f"oldver_{stale['stem']}" / rel
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(fp), str(dest))
                    except OSError as e:
                        print(f"      [!] could not archive {rel}: {e}")
                        continue
                    emptied.append(fp.parent)
                moved += 1
            if not dry_run: mods.pop(stale["stem"], None)
            actions.append({"mod_id": g["ident"][0], "keep": keep["stem"], "keep_ver": keep["ver"],
                            "drop": stale["stem"], "drop_ver": stale["ver"],
                            "shared": len(drop["shared"]), "orphans_moved": moved})
    if not dry_run:
        _prune_empty_dirs(game_root, emptied)
    return actions

def report_version_health(manifest_mods: dict) -> int:
    """Report mods installed at more than one version. Read-only. Returns the group count."""
    groups = find_version_duplicates(manifest_mods)
    if groups:
        print("  [!] Mod installed at more than one version at once (both entries own the "
              "same game files, so the older one can silently win):")
        for g in groups:
            keep = g["keep"]
            print(f"      - mod {g['ident'][0]} '{keep['stem']}'")
            print(f"          keep  v{keep['ver']}  ({len(keep['files'])} file(s))")
            for d in g["drops"]:
                e = d["entry"]
                print(f"          drop  v{e['ver']}  ({len(e['files'])} file(s), "
                      f"{len(d['shared'])} of them claimed by both)  {e['stem']}")
        print("  Fix: run --fix-versions (keeps the newest, archives the older version's "
              "leftover files, reversible).")
    print(f"[versions] {len(groups)} mod(s) installed at more than one version")
    return len(groups)

def action_fix_versions(game_root: Path, dry_run: bool):
    """Migration/repair: reduce every multi-version mod to its newest version."""
    manifest = load_manifest(game_root)
    mods = manifest["mods"]
    groups = find_version_duplicates(mods)
    if not groups:
        print("No mod is installed at more than one version — nothing to fix.")
        return
    print(f"{'Would fix' if dry_run else 'Fixing'} {len(groups)} mod(s) "
          "installed at more than one version:\n")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    actions = dedupe_mod_versions(game_root, mods, dry_run, ts)
    for act in actions:
        print(f"  [{'~' if dry_run else '✓'}] mod {act['mod_id']}: keep v{act['keep_ver']}, "
              f"retire v{act['drop_ver']} ({act['drop']})")
        print(f"        {act['shared']} shared path(s) released to the kept version; "
              f"{act['orphans_moved']} stale file(s) "
              f"{'would be archived' if dry_run else 'archived'}")
    if not dry_run:
        save_manifest(game_root, mods)
    moved = sum(a["orphans_moved"] for a in actions)
    print(f"\n{'Would retire' if dry_run else 'Retired'} {len(actions)} stale version "
          f"entry(ies); {moved} file(s) "
          + ("would be archived." if dry_run else
             f"archived to {DUP_BACKUP_DIRNAME}/{ts}/ (move them back to undo)."))

# ── Health check: case-duplicate folders (redscript SYM_REDEFINITION) ─────────
# On a case-sensitive Linux/Proton filesystem, `Core/` and `core/` are two distinct
# folders. When a mod's module tree ends up in both (e.g. an older install left `Core/`
# and a newer one added `core/`), redscript sees every type in that module declared
# twice → SYM_REDEFINITION on every file → the whole compile aborts, so ALL script mods
# fail to load. This detector finds those collisions; the fixer resolves the redscript
# ones the way the manual fix does — keep the version the rest of the mod references,
# move the stray to a reversible backup.
DUP_BACKUP_DIRNAME = "_removed_duplicate_mods"
# Where a file is copied before a deploy overwrites content the user changed.
# Declared here rather than beside the pin logic because HEALTH_SKIP_DIRS, just
# below, must know to walk past it.
OVERWRITE_BACKUP_DIRNAME = "_overwritten_by_deploy"
HEALTH_SKIP_DIRS = {'.mod_trash', DUP_BACKUP_DIRNAME, OVERWRITE_BACKUP_DIRNAME,
                    TMP_DIR_NAME, '.git', '__macosx'}

_REDS_TYPE_RE = re.compile(r'\b(?:class|struct|enum)\s+([A-Za-z_]\w*)', re.IGNORECASE)
_WORD_RE = re.compile(r'[A-Za-z_]\w*')

def _mod_owners_index(manifest_mods: dict) -> dict:
    """rel-posix-file -> list of manifest mod names that list it."""
    idx: dict = {}
    for mod, files in (manifest_mods or {}).items():
        for rel in files:
            idx.setdefault(rel, []).append(mod)
    return idx

def _owners_under(owner_index: dict, folder_rel: str) -> set:
    prefix = folder_rel.rstrip('/') + '/'
    owners = set()
    for rel, mods in owner_index.items():
        if rel.startswith(prefix):
            owners.update(mods)
    return owners

def _count_files(path: Path) -> int:
    n = 0
    for _root, _dirs, files in os.walk(path):
        n += len(files)
    return n

def _contains_reds(folder: Path) -> bool:
    """True if the tree holds redscript sources. r6/scripts is not the only place the
    compiler reads: frameworks bundle their .reds API under red4ext/plugins/<Name>/Scripts
    and those are compiled too, so 'is it under r6/scripts' is the wrong question to ask
    about whether a folder is compile input."""
    return next(folder.rglob("*.reds"), None) is not None

def find_case_collisions(game_root: Path, manifest_mods: dict | None = None):
    """Every directory holding two or more child sub-directories whose names differ only
    by case. Returns a list of collision dicts (redscript ones first); each has parent_rel,
    parent_path, is_redscript, is_framework_bundle, has_reds and a `variants` list of
    {name, rel, path, files, owners}."""
    owner_index = _mod_owners_index(manifest_mods or {})
    try:
        scripts_root = (game_root / "r6" / "scripts").resolve()
    except OSError:
        scripts_root = game_root / "r6" / "scripts"
    collisions = []
    for root, dirs, _files in os.walk(game_root):
        dirs[:] = [d for d in dirs if d not in HEALTH_SKIP_DIRS]   # never flag our own trash
        root_path = Path(root)
        groups: dict = {}
        for d in dirs:
            groups.setdefault(d.lower(), []).append(d)
        for low, names in groups.items():
            if len(names) < 2:
                continue
            parent_rel = root_path.relative_to(game_root).as_posix()
            parent_rel = "" if parent_rel == "." else parent_rel
            try:
                rp = root_path.resolve()
                is_reds = rp == scripts_root or scripts_root in rp.parents
            except OSError:
                is_reds = parent_rel.split('/')[:2] == ['r6', 'scripts']
            variants = []
            for name in sorted(names):
                folder_rel = f"{parent_rel}/{name}" if parent_rel else name
                folder = root_path / name
                variants.append({
                    "name": name, "rel": folder_rel, "path": folder,
                    "files": _count_files(folder),
                    "owners": sorted(_owners_under(owner_index, folder_rel)),
                })
            has_reds = any(_contains_reds(v["path"]) for v in variants)
            # A framework's own folder: red4ext/plugins/<Name>. A case-split directly beneath
            # one means two builds of that framework, which is decided by build age, not by
            # the coherence test (there is no surrounding mod to reference the identifiers).
            pparts = parent_rel.split('/') if parent_rel else []
            is_fw_bundle = (len(pparts) == 3 and pparts[:2] == ['red4ext', 'plugins'])
            collisions.append({
                "parent_rel": parent_rel or ".", "parent_path": root_path,
                "lower": low, "is_redscript": is_reds or has_reds,
                "is_framework_bundle": is_fw_bundle, "has_reds": has_reds,
                "variants": variants,
            })
    collisions.sort(key=lambda c: (not c["is_redscript"], c["parent_rel"]))
    return collisions

def _owner_label(v: dict) -> str:
    owners = v["owners"]
    if not owners:
        return "unmanaged (no manifest owner)"
    return owners[0] if len(owners) == 1 else f"{owners[0]} +{len(owners) - 1} more"

# ── Redscript compile health ─────────────────────────────────────────────────
# DO NOT ADD AN scc.exe RUNNER HERE. Running the compiler standalone produces thousands of
# errors that are not real — it compiles without the game's registered native types, so
# every framework symbol reads as UNRESOLVED_TYPE/UNRESOLVED_METHOD/UNRESOLVED_IMPORT. One
# such run reported 7875 errors on a healthy install. Worse, it is not even reliably
# measuring the directory you point it at: a control probe containing nothing but invalid
# syntax also returned "Compilation complete", exit 0. And it rewrites r6/cache/final.redscripts
# and .ts as a side effect, corrupting the very state under test.
#
# The game's own compile is the only trustworthy source, and it already writes down what it
# did. So this reads r6/logs/redscript_rCURRENT.log after a real launch instead of compiling
# anything. Absolute error counts are never a verdict on their own — a count is only meaningful
# as a DELTA against a baseline captured from a known-good launch.
REDSCRIPT_LOG_REL = "r6/logs/redscript_rCURRENT.log"
# Where the game leaves the compiled output. Location moved between redscript versions, so
# both are accepted; its presence and freshness is what "the compile actually succeeded" means.
MODDED_CACHE_RELS = ("r6/cache/modded/final.redscripts", "r6/cache/final.redscripts.modded")
BASELINE_NAME = "redscript_baseline.json"
# Error codes appear as bracketed SCREAMING_SNAKE tokens ([UNRESOLVED_TYPE], [SYM_REDEFINITION]).
_REDS_ERR_CODE_RE = re.compile(r'\[([A-Z][A-Z_]{2,})\]')
_REDS_DONE_RE = re.compile(r'compilation complete', re.IGNORECASE)

def parse_redscript_log(path: Path) -> dict:
    """Counts of each error code in a redscript log, plus whether the compile finished.
    Deliberately loose about the surrounding line format, which has changed across redscript
    releases; the bracketed code and the completion line are the parts that have been stable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"readable": False, "error": str(e), "complete": False, "by_code": {}, "total": 0}
    by_code: dict = {}
    for line in text.splitlines():
        if "ERROR" not in line.upper():
            continue
        for code in _REDS_ERR_CODE_RE.findall(line):
            by_code[code] = by_code.get(code, 0) + 1
    return {"readable": True, "complete": bool(_REDS_DONE_RE.search(text)),
            "by_code": by_code, "total": sum(by_code.values())}

def _baseline_path() -> Path:
    return CONFIG_DIR / BASELINE_NAME

def load_redscript_baseline(game_root: Path) -> dict | None:
    """Baselines live in the app's config dir, NOT the game folder — the health check must
    stay provably non-mutating with respect to the install."""
    try:
        return json.loads(_baseline_path().read_text(encoding="utf-8")).get(str(game_root))
    except (OSError, json.JSONDecodeError, AttributeError):
        return None

def save_redscript_baseline(game_root: Path, parsed: dict) -> dict:
    path = _baseline_path()
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        store = {}
    entry = {"captured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "total": parsed["total"], "by_code": parsed["by_code"]}
    store[str(game_root)] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=1), encoding="utf-8")
    return entry

def redscript_health(game_root: Path, manifest: dict | None = None) -> dict:
    """Read the game's own record of its last compile. Never compiles anything.

    States:
      no-log  — the game has not run since the log was last cleared; nothing to report.
      stale   — mods were deployed AFTER this log was written, so it describes a tree that
                no longer exists. Reporting it as health would be a false signal in exactly
                the way a standalone scc run is.
      failed  — the compile did not complete, or completed without producing the artifact.
      ok      — completed and the modded cache is present."""
    log = game_root / REDSCRIPT_LOG_REL
    out = {"state": "no-log", "log": log, "log_mtime": None, "artifact": None,
           "parsed": None, "baseline": load_redscript_baseline(game_root), "delta": None}
    if not log.exists():
        return out
    try:
        out["log_mtime"] = log.stat().st_mtime
    except OSError:
        pass
    parsed = parse_redscript_log(log)
    out["parsed"] = parsed
    for rel in MODDED_CACHE_RELS:
        p = game_root / rel
        if p.exists():
            out["artifact"] = p
            break
    # A log older than the last deploy describes a previous install, not this one.
    updated = (manifest or {}).get("updated")
    if updated and out["log_mtime"]:
        try:
            deployed_at = datetime.fromisoformat(updated).timestamp()
            if out["log_mtime"] < deployed_at:
                out["state"] = "stale"
                return out
        except (ValueError, TypeError):
            pass
    out["state"] = "ok" if (parsed["complete"] and out["artifact"]) else "failed"
    if out["baseline"] is not None:
        out["delta"] = parsed["total"] - out["baseline"].get("total", 0)
    return out

def report_redscript_health(game_root: Path, manifest: dict | None = None) -> int:
    """Print the compile verdict. Returns 1 when the compile is known-broken, else 0.

    An absolute error count is never presented as the verdict: healthy installs log warnings
    and non-fatal errors, and it was a raw count read as health that made a working install
    look catastrophically broken. The verdict is completion + artifact; counts are shown only
    as a delta against a captured baseline."""
    h = redscript_health(game_root, manifest)
    state = h["state"]
    if state == "no-log":
        print("  [i] No redscript log yet — launch the game once, then re-check. "
              "(The game's own compile is the only reliable signal; never judge this by "
              "running scc.exe standalone.)")
        print("[redscript] no-log")
        return 0
    if state == "stale":
        print("  [i] The redscript log predates the last deploy, so it describes an older "
              "install. Launch the game, then re-check.")
        print("[redscript] stale")
        return 0
    parsed = h["parsed"]
    if state == "ok":
        print(f"  [✓] Last compile completed and wrote {h['artifact'].name} — script mods loaded.")
    else:
        why = ("no 'Compilation complete' in the log" if not parsed["complete"]
               else "compilation completed but the modded cache was not written")
        print(f"  [!] Redscript compile FAILED — {why}. ALL script mods fail to load "
              "until this is fixed.")
        top = sorted(parsed["by_code"].items(), key=lambda kv: -kv[1])[:5]
        for code, n in top:
            print(f"        {n}x {code}")
    base = h["baseline"]
    if base is None:
        print(f"      ({parsed['total']} error line(s) in the log. This number is NOT a health "
              "score on its own — capture a baseline from a known-good launch with "
              "--redscript-baseline to make it comparable.)")
    else:
        d = h["delta"]
        sign = f"+{d}" if d > 0 else str(d)
        print(f"      {parsed['total']} error line(s) vs baseline {base['total']} "
              f"captured {base['captured']} → {sign}."
              + ("  New errors since the baseline — this is the signal worth acting on."
                 if d > 0 else "  No regression against the baseline."))
    print(f"[redscript] {state}; errors={parsed['total']}; "
          f"delta={'n/a' if h['delta'] is None else h['delta']}")
    return 1 if state == "failed" else 0

def action_redscript_baseline(game_root: Path):
    """Record the current log's error profile as the known-good reference."""
    log = game_root / REDSCRIPT_LOG_REL
    if not log.exists():
        sys.exit(f"[!] No redscript log at {log}. Launch the game once, then capture.")
    parsed = parse_redscript_log(log)
    if not parsed["complete"]:
        sys.exit("[!] That log's compile did not complete — refusing to baseline a broken "
                 "state. Fix the compile, launch again, then capture.")
    entry = save_redscript_baseline(game_root, parsed)
    print(f"Baseline captured: {entry['total']} error line(s) from a completed compile.")
    for code, n in sorted(parsed["by_code"].items(), key=lambda kv: -kv[1])[:8]:
        print(f"    {n}x {code}")
    print(f"Stored in {_baseline_path()} (outside the game folder).")
    print("Future --check-health runs report the DELTA against this, not a raw count.")

# ── Attribution: does a mod really own the files it claims? ──────────────────
# A path is written by whichever mod deploys last, so ownership is single-valued. But the
# post-deploy merge only rewrites entries for mods processed in THAT run: an entry from an
# earlier run kept claiming paths a later mod had since overwritten. Across many runs an old
# entry gets whittled down to a few files it no longer owns — or one stray belonging to some
# unrelated mod — and the manager then reports it as installed when it is not.
def _release_stale_claims(mods: dict, new_ownership: dict) -> list:
    """Strip, from every OTHER manifest entry, the paths this run just gave a new owner.

    Only paths written in this run are touched: those are the ones whose true owner is known
    for certain. An entry left owning nothing is dropped — its bytes are gone from disk, so
    reporting it as installed is precisely the false 'patch is installed' this fixes.
    Returns [(stem, lost_count, dropped_bool)]."""
    owned_now = {rel: owner for owner, rels in new_ownership.items() for rel in rels}
    released = []
    for stem in list(mods):
        if stem in new_ownership:
            continue
        before = mods[stem]
        kept = [r for r in before if owned_now.get(r, stem) == stem]
        if len(kept) == len(before):
            continue
        if kept:
            mods[stem] = kept
        else:
            mods.pop(stem)
        released.append((stem, len(before) - len(kept), not kept))
    return released

def find_archive_for(stem: str, downloads: Path):
    """The download an installed mod came from: exact stem first, then any archive sharing
    its (mod-id, name) identity — the file may have been re-downloaded at another version."""
    if not downloads or not downloads.exists():
        return None
    archives = [f for f in downloads.iterdir() if f.is_file() and is_archive_file(f)]
    for a in archives:
        if a.stem == stem:
            return a
    ident = mod_identity(stem)
    if ident is None:
        return None
    for a in archives:
        if mod_identity(a.stem) == ident:
            return a
    return None

def find_orphan_entries(mods: dict, downloads: Path) -> list:
    """Installed mods with no archive left in Downloads. Cheap (a directory listing, no
    extraction), so it can run in the normal health check. Not proof of a bogus entry —
    downloads get cleaned up — but an entry that is ALSO mis-attributed is very likely stale."""
    if not downloads or not downloads.exists():
        return []
    archives = [f for f in downloads.iterdir() if f.is_file() and is_archive_file(f)]
    stems = {a.stem for a in archives}
    idents = {mod_identity(a.stem) for a in archives} - {None}
    orphans = []
    for stem in sorted(mods):
        if stem in stems:
            continue
        ident = mod_identity(stem)
        if ident is not None and ident in idents:
            continue
        orphans.append(stem)
    return orphans

def find_foreign_folder_entries(mods: dict) -> list:
    """Entries whose every owned file sits in a directory some OTHER single mod dominates.

    Works without the archive, which matters because the entries most likely to be stale are
    exactly the ones whose download is long gone. The reported case is the shape: a mod
    called 'Improved Vehicle Persistence patch' owning one file inside
    bin/.../cyber_engine_tweaks/mods/Dedratruckcustom/ — a folder belonging to a truck mod.

    Suspicion, not proof: patch mods legitimately ship files into another mod's folder. It is
    the combination with a missing archive that makes an entry a good bet for stale.
    Returns [{stem, files, dominators}]."""
    by_dir: dict = {}
    for stem, files in mods.items():
        for rel in files:
            by_dir.setdefault(rel.rsplit('/', 1)[0] if '/' in rel else ".", {})
            by_dir[rel.rsplit('/', 1)[0] if '/' in rel else "."][stem] = \
                by_dir[rel.rsplit('/', 1)[0] if '/' in rel else "."].get(stem, 0) + 1
    out = []
    for stem, files in sorted(mods.items()):
        if not files:
            continue
        dominators: dict = {}
        for rel in files:
            d = rel.rsplit('/', 1)[0] if '/' in rel else "."
            owners = by_dir.get(d, {})
            mine = owners.get(stem, 0)
            bigger = [(o, n) for o, n in owners.items() if o != stem and n > mine]
            if not bigger:
                break                       # this file is in a folder we are not a guest in
            top = max(bigger, key=lambda kv: kv[1])
            dominators[top[0]] = dominators.get(top[0], 0) + 1
        else:
            out.append({"stem": stem, "files": len(files), "dominators": dominators})
    return out

def check_attribution(mods: dict, downloads: Path, only: str = "") -> list:
    """Verify each mod's owned paths actually came out of its archive.

    Deployment relocates files but never renames them, so every owned file's BASENAME must
    appear in the archive listing. Zero matches means the entry is attributed to files that
    are not this mod's at all. Costs one bsdtar listing per mod, so this is its own action
    rather than part of --check-health.

    Returns [{stem, archive, owned, matched, missing, verdict}] for anything not clean."""
    findings = []
    for stem in sorted(mods):
        if only and only.lower() not in stem.lower():
            continue
        owned = mods[stem]
        if not owned:
            findings.append({"stem": stem, "archive": None, "owned": 0, "matched": 0,
                             "missing": [], "verdict": "empty"})
            continue
        archive = find_archive_for(stem, downloads)
        if archive is None:
            findings.append({"stem": stem, "archive": None, "owned": len(owned), "matched": 0,
                             "missing": [], "verdict": "no-archive"})
            continue
        listing = archive_listing(archive)
        if not listing:
            findings.append({"stem": stem, "archive": archive, "owned": len(owned),
                             "matched": 0, "missing": [], "verdict": "unreadable"})
            continue
        # archive_listing() already lowercases; compare basenames, since the deploy decides
        # the destination directory but always keeps the file's own name.
        in_archive = {e.rsplit('/', 1)[-1] for e in listing}
        missing = [r for r in owned if r.rsplit('/', 1)[-1].lower() not in in_archive]
        matched = len(owned) - len(missing)
        if not missing:
            continue                                    # clean
        findings.append({"stem": stem, "archive": archive, "owned": len(owned),
                         "matched": matched, "missing": missing,
                         "verdict": "mis-attributed" if matched == 0 else "partial"})
    return findings

def report_attribution(mods: dict, downloads: Path, only: str = "") -> int:
    """Full attribution audit. Returns the number of definitely-bogus entries."""
    findings = check_attribution(mods, downloads, only)
    bogus = [f for f in findings if f["verdict"] in ("mis-attributed", "empty")]
    partial = [f for f in findings if f["verdict"] == "partial"]
    no_arch = [f for f in findings if f["verdict"] == "no-archive"]
    unread = [f for f in findings if f["verdict"] == "unreadable"]
    if bogus:
        print(f"  [!] {len(bogus)} entry(ies) own files that did NOT come from their archive "
              "— the mod is reported installed when it is not:")
        for f in bogus:
            print(f"      - {f['stem']}")
            if f["verdict"] == "empty":
                print("          owns no files at all")
            else:
                print(f"          owns {f['owned']} file(s), none present in "
                      f"{f['archive'].name}:")
                for rel in f["missing"][:4]:
                    print(f"            {rel}")
                if len(f["missing"]) > 4:
                    print(f"            ... and {len(f['missing']) - 4} more")
    if partial:
        print(f"  [i] {len(partial)} entry(ies) own SOME files not found in their archive "
              "(often a file another mod overwrote, or an installer-renamed option):")
        for f in partial[:10]:
            print(f"      - {f['stem']}: {f['matched']}/{f['owned']} match "
                  f"{f['archive'].name}")
        if len(partial) > 10:
            print(f"      ... and {len(partial) - 10} more")
    if no_arch:
        print(f"  [i] {len(no_arch)} installed mod(s) have no archive left in Downloads, so "
              "their attribution cannot be verified:")
        for stem in [f["stem"] for f in no_arch][:10]:
            print(f"      - {stem}")
        if len(no_arch) > 10:
            print(f"      ... and {len(no_arch) - 10} more")
    if unread:
        print(f"  [i] {len(unread)} archive(s) could not be listed (bsdtar failed).")
    # The strongest available signal for an entry whose download is gone: no archive to
    # check against AND every file it claims sits in another mod's folder.
    orphan_stems = {f["stem"] for f in no_arch}
    likely_stale = [g for g in find_foreign_folder_entries(mods)
                    if g["stem"] in orphan_stems and (not only or only.lower() in g["stem"].lower())]
    if likely_stale:
        print(f"\n  [!] {len(likely_stale)} of those have no archive AND own only files inside "
              "another mod's folder — very likely stale entries:")
        for g in likely_stale:
            top = ", ".join(f"{o} ({n})" for o, n in sorted(g["dominators"].items(),
                                                            key=lambda kv: -kv[1])[:2])
            print(f"      - {g['stem']}")
            print(f"          all {g['files']} file(s) live in folders owned by: {top}")
    if not findings:
        print("  [✓] Every installed mod's files trace back to its archive.")
    print(f"[attribution] {len(bogus)} mis-attributed; {len(partial)} partial; "
          f"{len(no_arch)} unverifiable; {len(likely_stale)} likely-stale")
    return len(bogus) + len(likely_stale)

def action_check_attribution(game_root: Path, downloads: Path, needle: str = ""):
    mods = load_manifest(game_root).get("mods", {})
    print("\n=== Attribution Audit (do owned files come from the mod's archive?) ===")
    print(f"  Checking {len(mods)} installed mod(s) against {downloads} — "
          "one archive listing each, this takes a while.")
    n = report_attribution(mods, downloads, needle)
    if n:
        print(f"\n  {n} entry(ies) look bogus. Remove one with:\n"
              "      --remove-from-file <file listing the exact names>\n"
              "  Review first — a patch mod legitimately ships files into another mod's "
              "folder, which looks similar but is correct.")

# ─────────────────────────── Unmanaged files on disk ───────────────────────────
# The manifest is the manager's ONLY record of what it put on disk, so a file it did
# not write is invisible to it: never updated, never taken away by --remove, and free
# to shadow a managed copy of the same name. Found 2026-07-19: a hand-installed
# r6/scripts/REDCheatMenu2.reds, 238 KB, belonging to no manifest entry at all.
#
# The whole difficulty is separating a hand-installed MOD from the large amount of
# runtime state that legitimately lives under these same roots and never belongs to
# any mod: CET writes per-mod settings and saved state next to the mod's own Lua,
# every RED4ext plugin writes its .log beside its .dll, frameworks write caches. A
# naive walk reports hundreds of those, buries the one line that matters, and a
# report nobody reads is worth the same as no report. So only mod PAYLOAD is
# enumerated; everything else is counted and left unnamed.
# Roots that exist ONLY to hold mod content — archive/pc/mod, r6/tweaks and soundbanks are
# created by mods, `mods/` is REDmod, and nothing but a mod ever writes into them. Everything
# found here is payload, whatever its extension. Filtering these by extension is what would
# make the check miss a hand-installed TweakXL mod, whose payload is plain .yaml.
PAYLOAD_ROOTS = (
    "archive/pc/mod",
    "mods",
    "r6/scripts",
    "r6/tweaks",
    "r6/audioware",      # custom sound banks + their .yaml manifests, read by Audioware
    "soundbanks",
)
# Roots where mod content and runtime state are mixed together in the same folders, so the
# extension is the only thing that separates them: CET keeps each mod's saved settings and
# databases beside its Lua, and every RED4ext plugin writes its .log beside its .dll.
MIXED_ROOTS = (
    "bin/x64/plugins/cyber_engine_tweaks/mods",
    "red4ext/plugins",
)
MANAGED_ROOTS = PAYLOAD_ROOTS + MIXED_ROOTS

# What a download installs, for the mixed roots only.
PAYLOAD_EXTS = frozenset({
    '.archive', '.xl', '.reds', '.tweak', '.dll', '.lua', '.bnk', '.wav',
    '.opuspak', '.opusinfo', '.kark', '.asi', '.yaml', '.yml',
})

def _fmt_size(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "KB" else f"{n:.1f} {unit}"
    return f"{n:.1f} GB"

def find_unmanaged_files(game_root: Path, manifest_mods: dict) -> dict:
    """Files under the managed roots that no manifest entry owns, grouped by mod unit.

    Returns {"units": [...], "payload": int, "other": int, "bytes": int}, where a unit is
    the top-level file or folder under a managed root — one hand-installed mod, whether it
    dropped one .reds or four hundred files. Only units holding payload appear in "units";
    the rest are summed into "other".

    Matching is case-INSENSITIVE, as everywhere else here: the game reads this tree through
    Wine, so a manifest that says `Scripts/x.reds` and a disk that says `scripts/x.reds` are
    one file, and comparing them exactly would report every managed file on a case-drifted
    install as unmanaged — thousands of lines of pure noise."""
    owned = {rel.lower() for files in (manifest_mods or {}).values() for rel in files}
    units: dict = {}
    payload_n = other_n = 0
    for root_rel in MANAGED_ROOTS:
        root = game_root / root_rel
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in HEALTH_SKIP_DIRS]
            here = Path(dirpath)
            for fn in filenames:
                f = here / fn
                rel = f.relative_to(game_root).as_posix()
                if rel.lower() in owned:
                    continue
                # The unit is the first component below the root: one mod, one line.
                below = f.relative_to(root)
                unit_rel = f"{root_rel}/{below.parts[0]}"
                is_payload = (root_rel in PAYLOAD_ROOTS
                              or f.suffix.lower() in PAYLOAD_EXTS)
                try:
                    size = f.stat().st_size
                except OSError:
                    continue        # vanished mid-walk, or a dangling symlink
                if is_payload:
                    payload_n += 1
                else:
                    other_n += 1
                u = units.setdefault(unit_rel, {"rel": unit_rel, "files": 0, "payload": 0,
                                                "bytes": 0, "root": root_rel})
                u["files"] += 1
                u["bytes"] += size
                u["payload"] += 1 if is_payload else 0
    real = [u for u in units.values() if u["payload"]]
    real.sort(key=lambda u: (-u["bytes"], u["rel"]))
    return {"units": real, "payload": payload_n, "other": other_n,
            "bytes": sum(u["bytes"] for u in real)}

def report_unmanaged(game_root: Path, manifest_mods: dict) -> int:
    """Print the unmanaged-files section plus a machine-parseable line. Read-only.
    Returns the number of unmanaged mod units (payload only)."""
    if not manifest_mods:
        # With nothing recorded as installed, every file under every managed root is
        # unmanaged and saying so is true but useless. Stay quiet rather than print a
        # scary total that only means "the manifest is empty".
        print("[unmanaged] 0 unmanaged mod unit(s); 0 payload file(s) "
              "(manifest is empty — nothing to compare against)")
        return 0
    found = find_unmanaged_files(game_root, manifest_mods)
    units = found["units"]
    if units:
        print(f"  [!] {len(units)} mod file/folder(s) on disk belong to no manifest entry "
              f"({found['payload']} payload file(s), {_fmt_size(found['bytes'])}). The manager "
              "cannot update or remove these, and they can shadow a managed copy:")
        for u in units[:12]:
            detail = (f"{u['files']} file(s), {_fmt_size(u['bytes'])}"
                      if u["files"] > 1 else _fmt_size(u["bytes"]))
            print(f"      - {u['rel']}  ({detail})")
        if len(units) > 12:
            print(f"      ... and {len(units) - 12} more")
        print("  These are usually hand-installed mods. Reinstall them through this manager "
              "so it owns them, or delete them by hand — nothing here touches them.")
    print(f"[unmanaged] {len(units)} unmanaged mod unit(s); {found['payload']} payload file(s)")
    return len(units)

def action_check_unmanaged(game_root: Path):
    mf = load_manifest(game_root)
    print("\n=== Unmanaged Files (on disk, owned by no manifest entry) ===")
    report_unmanaged(game_root, mf.get("mods", {}))

# ──────────────────────── Mutually-conflicting mods ────────────────────────
# Nothing stops a collection installing four mods that fight over one subsystem. Observed
# 2026-07-19: four first-person camera mods (2202, 1786, 2675, 30558) — the vehicle camera
# toggle simply stopped working — and four main-menu/intro replacers (25272, 24294, 2621,
# 11426). Every one installed "successfully"; nothing overlapped on disk, so no existing
# check saw a thing.
#
# Two detectors, because two very different kinds of evidence are available:
#
#  1. PROOF, read off the files themselves. A redscript `@replaceMethod` swaps a game
#     method outright, so if two mods replace the SAME method only one of them exists in
#     the compiled output — the other silently does nothing. That is exactly the shape of
#     "the camera toggle is dead". `@wrapMethod` is deliberately NOT counted: wrappers
#     chain through wrappedMethod(), so any number of them coexist correctly, and treating
#     them as conflicts would flag most of the script mods ever written.
#
#  2. A GUESS, from what the mod is. Menu and intro replacers ship only .archive files
#     overriding the same game resource; there is no textual signal anywhere on disk, so
#     the only workable handle is the mod's identity. Curated mod-ids first (exact), then
#     name phrases (approximate). Both are advisory — see WHY_ADVISORY below.

# Subsystems where, by their nature, one mod wins and the rest are dead weight. Kept
# deliberately short: a domain that mods can legitimately share (textures, quests, weapons)
# produces noise and teaches the user to ignore the whole section.
DOMAIN_LABELS = {
    "first-person-camera":  "first-person / vehicle camera",
    "third-person-camera":  "third-person camera",
    "main-menu":            "main menu, intro and splash screens",
    "carry-capacity":       "carry weight / encumbrance",
    "stamina":              "stamina and sprint",
    "weather":              "weather and time-of-day",
    "time-scale":           "game time scale",
    "crowd-density":        "crowd and traffic density",
    "vehicle-handling":     "vehicle handling / physics",
    "save-system":          "save and autosave behaviour",
}

# Exact, by Nexus mod-id: no guessing at all. Seeded from the two conflicts observed on a
# real install; extend it in CONFIG_DIR/mod_domains.json rather than editing this table.
DOMAIN_BY_MOD_ID = {
    # four first-person camera mods installed together — vehicle camera toggle went dead
    "2202":  "first-person-camera",   # Better Vehicle First Person
    "1786":  "first-person-camera",   # First Person Camera Mod
    "2675":  "first-person-camera",   # Immersive First Person
    "30558": "first-person-camera",   # True First Person Camera 2.0
    # four main-menu / intro replacers installed together
    "25272": "main-menu",             # Lucy Walk Moon (no keyword in the name — curation
    "24294": "main-menu",             # Edgerunner Splash Screen   is the only way to catch
    "2621":  "main-menu",             # Intro Splashscreen and Song Change      that one)
    "11426": "main-menu",             # Official E3 2018 Trailer Intro Cinematic
    # carry weight: which mod actually owns the stat is famously non-obvious
    "3395":  "carry-capacity",        # Fully Adjustable Carry Capacity
}

# Approximate, by name. Phrases only, matched on word boundaries: a bare word like "camera"
# or "menu" matches half a mod list. `intro` as a word does not match "Introduction".
DOMAIN_NAME_RULES = {
    "first-person-camera": ("first person", "1st person", "firstperson"),
    "third-person-camera": ("third person", "3rd person", "thirdperson"),
    "main-menu":           ("main menu", "splash screen", "splashscreen", "start screen",
                            "intro cinematic", "trailer intro", "menu replacer",
                            "menu background", "menu backgrounds"),
    "carry-capacity":      ("carry capacity", "carry weight", "encumbrance"),
    "stamina":             ("infinite stamina", "no stamina", "stamina overhaul"),
    "weather":             ("weather overhaul", "nova city", "time of day"),
    "time-scale":          ("time scale", "timescale", "slower time"),
    "crowd-density":       ("crowd density", "traffic density", "population density"),
    "vehicle-handling":    ("vehicle handling", "driving overhaul", "car handling"),
    "save-system":         ("autosave", "auto save", "save system"),
}
DOMAIN_OVERRIDES_NAME = "mod_domains.json"

def load_domain_overrides() -> dict:
    """User-maintained {mod_id: domain} merged over the built-in table. Kept in CONFIG_DIR,
    never in the game folder: it is the user's knowledge about their collection, and it must
    survive reinstalling the game. A table only I can edit would be stale within a week."""
    path = CONFIG_DIR / DOMAIN_OVERRIDES_NAME
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

def mod_domain(stem: str, overrides: dict | None = None, names: dict | None = None):
    """(domain, how) for a mod, or None. `how` is 'known' (mod-id table) or 'name' (guess).

    `names` is passed in by callers that iterate — a full collection is 1442 stems and
    re-reading the name cache for each one would be 1442 JSON parses."""
    key = nexus_key_from_stem(stem)
    mid = key[1] if key else framework_mod_id(stem)
    table = dict(DOMAIN_BY_MOD_ID)
    table.update(overrides or {})
    if mid and mid in table:
        return (table[mid], "known")
    # The REAL Nexus name when it is known. This is the whole reason names are cached: the
    # filename "v2_All in one Vortex Installer" matches no rule any human would write, while
    # "City of Dreams Menu Backgrounds" matches the menu rules immediately.
    raw = mod_display_name(stem, names if names is not None else load_mod_names()) \
        if mid else (key[0] if key else stem)
    name = raw.lower().replace('_', ' ').replace('-', ' ')
    for domain, phrases in DOMAIN_NAME_RULES.items():
        for p in phrases:
            if re.search(r'\b' + re.escape(p) + r'\b', name):
                return (domain, "name")
    return None

def find_domain_conflicts(manifest_mods: dict, overrides: dict | None = None) -> list:
    """Domains claimed by more than one distinct mod. Returns [{domain, label, mods}] where
    each mod is {stem, how}.

    Collapsed by mod identity first: two entries of one mod are a VERSION duplicate, a
    different fault with its own fixer (--fix-versions), and reporting it here as a
    subsystem conflict would just be the same problem wearing a second hat."""
    overrides = overrides if overrides is not None else load_domain_overrides()
    names = load_mod_names()
    by_domain: dict = {}
    seen: dict = {}
    for stem in sorted(manifest_mods or {}):
        got = mod_domain(stem, overrides, names)
        if not got:
            continue
        domain, how = got
        ident = mod_identity(stem) or ("", stem.lower())
        if seen.get((domain, ident)):
            continue
        seen[(domain, ident)] = True
        by_domain.setdefault(domain, []).append({"stem": stem, "how": how})
    out = [{"domain": d, "label": DOMAIN_LABELS.get(d, d), "mods": m}
           for d, m in by_domain.items() if len(m) > 1]
    out.sort(key=lambda c: (-len(c["mods"]), c["domain"]))
    return out

# `@replaceMethod(Class)` / `@replaceGlobal()` followed by the func it decorates. Parsed in
# two steps rather than one big regex because other annotations, comments and blank lines
# routinely sit between the two, and a single pattern that tolerated all of that would also
# match across unrelated declarations.
_REDS_REPLACE_RE = re.compile(r'@(replaceMethod|replaceGlobal)\s*\(([^)]*)\)')
_REDS_FUNC_RE = re.compile(r'\bfunc\s+([A-Za-z_]\w*)')
_REDS_COMMENT_RE = re.compile(r'//[^\n]*|/\*.*?\*/', re.DOTALL)

def parse_reds_replacements(text: str) -> set:
    """{(class, method)} replaced outright by this source. Comments are stripped first so a
    commented-out annotation — which every half-finished mod contains — is not read as live
    code."""
    body = _REDS_COMMENT_RE.sub(' ', text)
    out = set()
    for m in _REDS_REPLACE_RE.finditer(body):
        cls = (m.group(2) or "").strip() or "<global>"
        nxt = _REDS_FUNC_RE.search(body, m.end(), m.end() + 400)
        if nxt:
            out.add((cls, nxt.group(1)))
    return out

def find_replace_conflicts(game_root: Path, manifest_mods: dict) -> list:
    """Methods replaced outright by more than one mod. Returns [{target, mods}] where mods
    is [{owner, rel}]. This is proof, not suspicion: only one replacement survives the
    compile, so every other mod's version of that method is not running."""
    owner_of = {rel.lower(): stem for stem, files in (manifest_mods or {}).items()
                for rel in files}
    targets: dict = {}
    roots = [game_root / "r6" / "scripts", game_root / "red4ext" / "plugins"]
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in HEALTH_SKIP_DIRS]
            for fn in filenames:
                if not fn.lower().endswith(".reds"):
                    continue
                f = Path(dirpath) / fn
                rel = f.relative_to(game_root).as_posix()
                try:
                    text = f.read_text(encoding='utf-8', errors='ignore')
                except OSError:
                    continue
                # An unowned file still conflicts — a hand-installed script is one of the
                # likeliest things to be silently overridden. Name it by its path.
                owner = owner_of.get(rel.lower(), f"(unmanaged) {rel}")
                for tgt in parse_reds_replacements(text):
                    targets.setdefault(tgt, {}).setdefault(owner, rel)
    out = [{"target": f"{c}::{m}" if c != "<global>" else f"{m}()",
            "mods": [{"owner": o, "rel": r} for o, r in sorted(owners.items())]}
           for (c, m), owners in targets.items() if len(owners) > 1]
    out.sort(key=lambda c: (-len(c["mods"]), c["target"]))
    return out

# WHY_ADVISORY: never auto-fixed, and never counted as a health failure. Two mods in one
# domain is sometimes correct — an addon, a patch, or a compatibility build of exactly the
# mod it sits next to — and even a contested @replaceMethod may be what the user wants, if
# the mod that wins is the one they care about. Only the user knows which. So this reports
# and stops.
def report_conflicts(game_root: Path, manifest_mods: dict) -> int:
    """Print the conflict section plus a machine-parseable line. Read-only. Returns the
    number of contested domains + contested methods."""
    domains = find_domain_conflicts(manifest_mods)
    methods = find_replace_conflicts(game_root, manifest_mods)
    if domains:
        print(f"  [!] {len(domains)} subsystem(s) claimed by more than one mod — these fight, "
              "and usually only one of them works:")
        for c in domains:
            guessed = " (some matched by name — verify)" if any(
                m["how"] == "name" for m in c["mods"]) else ""
            print(f"      - {c['label']}: {len(c['mods'])} mods{guessed}")
            for m in c["mods"][:6]:
                print(f"          {m['stem']}  [{m['how']}]")
            if len(c["mods"]) > 6:
                print(f"          ... and {len(c['mods']) - 6} more")
    if methods:
        print(f"  [!] {len(methods)} redscript method(s) replaced outright by more than one "
              "mod. Only one replacement survives the compile; the others do nothing:")
        for c in methods[:10]:
            print(f"      - {c['target']}")
            for m in c["mods"][:4]:
                print(f"          {m['owner']}")
            if len(c["mods"]) > 4:
                print(f"          ... and {len(c['mods']) - 4} more")
        if len(methods) > 10:
            print(f"      ... and {len(methods) - 10} more contested method(s)")
    if domains or methods:
        print("  Nothing is changed automatically — pick which mod should own each and remove "
              "the rest with --remove. Tag mods yourself in "
              f"{CONFIG_DIR / DOMAIN_OVERRIDES_NAME} ({{\"<mod-id>\": \"<domain>\"}}).")
    print(f"[conflicts] {len(domains)} contested subsystem(s); "
          f"{len(methods)} contested method(s)")
    return len(domains) + len(methods)

def action_check_conflicts(game_root: Path):
    mf = load_manifest(game_root)
    print("\n=== Conflicting Mods (more than one mod claiming the same subsystem) ===")
    print(f"  Known domains: {', '.join(sorted(DOMAIN_LABELS))}")
    report_conflicts(game_root, mf.get("mods", {}))

# ─────────────────── Broken CET cross-mod lookups (renames) ───────────────────
# A CET mod finds its companion by folder name: GetMod("BetterVehicleFirstPerson"). When the
# companion renames its folder — Better Vehicle First Person 1.4.2 now installs as
# "EnhancedVehicleCamera" — the call returns nil forever. Nothing logs anything, because nil
# is a perfectly ordinary answer meaning "that mod isn't installed"; the integration simply
# never runs and both mods drive the vehicle camera without coordinating.
#
# The trick is that a missing lookup is only sometimes a fault. Most GetMod calls are
# OPTIONAL integration, and nil is the designed answer when the user hasn't installed the
# companion. What separates a break from normal operation is evidence that the companion IS
# there under another name — which is exactly what the rename table and the case check
# provide, and why those two are reported loudly while a plain absence is only counted.
_LUA_GETMOD_RE = re.compile(r'\bGetMod\s*\(\s*([\'"])(.+?)\1\s*\)')
_LUA_COMMENT_RE = re.compile(r'--\[\[.*?\]\]|--[^\n]*', re.DOTALL)

# Folder renames that silently break every mod still looking for the old name.
# {old_folder: new_folder}, extend in CONFIG_DIR/cet_renames.json.
CET_FOLDER_RENAMES = {
    # Better Vehicle First Person 1.4.2 — Immersive First Person still calls the old name
    "BetterVehicleFirstPerson": "EnhancedVehicleCamera",
}
CET_RENAMES_NAME = "cet_renames.json"

def load_cet_renames() -> dict:
    """User-maintained {old_folder: new_folder} merged over the built-in table, in CONFIG_DIR
    for the same reason as the domain table: it is knowledge about a live collection and has
    to outlive both the game folder and this file."""
    path = CONFIG_DIR / CET_RENAMES_NAME
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

def installed_cet_mods(game_root: Path) -> list:
    """CET mod folder names present on disk. Read from the FILESYSTEM, not the manifest: this
    has to match what CET itself will enumerate at runtime, which includes hand-installed
    mods the manifest knows nothing about (see find_unmanaged_files)."""
    root = game_root / CET_MODS_REL
    if not root.is_dir():
        return []
    try:
        return sorted(p.name for p in root.iterdir()
                      if p.is_dir() and p.name not in HEALTH_SKIP_DIRS)
    except OSError:
        return []

def parse_lua_getmod(text: str) -> set:
    """Folder names this Lua source looks up. Comments are stripped first — both `--` and
    `--[[ ]]` — so commented-out or example calls are not read as live lookups."""
    body = _LUA_COMMENT_RE.sub(' ', text)
    return {m.group(2) for m in _LUA_GETMOD_RE.finditer(body) if m.group(2).strip()}

def find_cet_lookup_breaks(game_root: Path, manifest_mods: dict,
                           renames: dict | None = None) -> list:
    """Cross-mod CET lookups that will return nil. Returns [{caller, owner, target, verdict,
    resolved, rel}] with verdict one of:

      renamed        the target folder was renamed and the NEW name is installed. A real
                     break: the companion is right there and the caller cannot see it.
      case-mismatch  a folder differs only by case. Reported because it is wrong either way
                     and costs one line to fix, but note the limit of the claim: whether it
                     actually returns nil depends on whether CET folds case when it indexes
                     mod names, which has NOT been verified against CET's source. On a
                     case-sensitive filesystem the folder name itself is exact, so a
                     mod written against a Windows install is the usual origin.
      absent         nothing resembling it is installed. Usually optional integration
                     behaving correctly, so this is counted rather than alarmed about.
    """
    table = dict(CET_FOLDER_RENAMES)
    table.update(renames if renames is not None else load_cet_renames())
    installed = installed_cet_mods(game_root)
    exact = set(installed)
    by_lower = {n.lower(): n for n in installed}
    owner_of = {rel.lower(): stem for stem, files in (manifest_mods or {}).items()
                for rel in files}
    root = game_root / CET_MODS_REL
    out = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in HEALTH_SKIP_DIRS]
        for fn in filenames:
            if not fn.lower().endswith(".lua"):
                continue
            f = Path(dirpath) / fn
            rel = f.relative_to(game_root).as_posix()
            try:
                text = f.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            caller = f.relative_to(root).parts[0]
            for target in sorted(parse_lua_getmod(text)):
                if target in exact or target == caller:
                    continue
                new = table.get(target)
                if new and new in exact:
                    verdict, resolved = "renamed", new
                elif target.lower() in by_lower:
                    verdict, resolved = "case-mismatch", by_lower[target.lower()]
                else:
                    verdict, resolved = "absent", ""
                out.append({"caller": caller, "owner": owner_of.get(rel.lower(), ""),
                            "target": target, "verdict": verdict, "resolved": resolved,
                            "rel": rel})
    order = {"renamed": 0, "case-mismatch": 1, "absent": 2}
    out.sort(key=lambda b: (order[b["verdict"]], b["caller"], b["target"]))
    return out

def report_cet_links(game_root: Path, manifest_mods: dict) -> int:
    """Print the CET cross-mod section plus a machine-parseable line. Read-only. Returns the
    number of CONFIRMED breaks (renamed + case-mismatch); a plain absence is not counted,
    because a mod whose optional companion you chose not to install is working as designed."""
    breaks = find_cet_lookup_breaks(game_root, manifest_mods)
    broken = [b for b in breaks if b["verdict"] != "absent"]
    absent = [b for b in breaks if b["verdict"] == "absent"]
    if broken:
        print(f"  [!] {len(broken)} cross-mod CET lookup(s) name a mod that IS installed, "
              "under a different folder name. The integration never runs, and nothing logs "
              "an error because nil normally just means 'not installed':")
        for b in broken:
            why = ("renamed its folder" if b["verdict"] == "renamed"
                   else "differs only by capitalisation — wrong either way, though whether "
                        "CET itself folds case here is unverified")
            print(f"      - {b['caller']} calls GetMod(\"{b['target']}\") — {why}; "
                  f"installed as \"{b['resolved']}\"")
            print(f"          {b['rel']}")
        print("  Fix: edit the caller's Lua to the installed name, or install a build of the "
              "caller that knows it. Record renames in "
              f"{CONFIG_DIR / CET_RENAMES_NAME} ({{\"<old>\": \"<new>\"}}).")
    if absent:
        # Not a fault by itself: nil is the designed answer for a companion you did not
        # install. Counted so a rename that nobody has recorded yet is still visible.
        print(f"  [i] {len(absent)} other CET lookup(s) find nothing installed — normally "
              "optional integration, but an unrecorded rename looks identical:")
        for b in absent[:6]:
            print(f"      - {b['caller']} calls GetMod(\"{b['target']}\")")
        if len(absent) > 6:
            print(f"      ... and {len(absent) - 6} more")
    print(f"[cet-links] {len(broken)} broken cross-mod lookup(s); "
          f"{len(absent)} companion(s) not installed")
    return len(broken)

def action_check_cet_links(game_root: Path):
    mf = load_manifest(game_root)
    installed = installed_cet_mods(game_root)
    print("\n=== CET Cross-Mod Lookups (GetMod folder names) ===")
    print(f"  {len(installed)} CET mod folder(s) installed.")
    report_cet_links(game_root, mf.get("mods", {}))

# ───────────────── Locale-sensitive crashes (/language config vars) ─────────────────
# GetVar() on a /language var that does not exist does not raise a Lua error — it trips a
# CDPR engine assert (inGameConfigGroup.cpp:76, "Could not find in-game config var 'Text' in
# group '/language'") which aborts the process. A Lua pcall around it CANNOT catch a C++
# assert, so no amount of defensive mod code helps.
#
# Observed 2026-07-16: DedraExoticDancers/modules/translation_multilang.lua probes the
# candidate list {OnScreen, Text, Subtitle, VoiceOver} and returns on the first locale it can
# normalise. Players on a supported locale return at OnScreen and never reach the landmine;
# this user's ua-ua is absent from the mod's 20-locale table, so they fall through to `Text`
# and the game dies seconds into loading a save. Both `Text` (nonexistent) and `Subtitle`
# (the real one is `Subtitles`) are wrong.
#
# Note the shape the pattern must match: the mod stores these as TABLE LITERALS,
# {"/language", "Text"}, and passes them to GetVar elsewhere. A scanner looking only for
# GetVar("/language", ...) would miss the very bug that motivated it.
_LUA_LANG_PAIR_RE = re.compile(r'([\'"])/language\1\s*,\s*([\'"])([A-Za-z_]\w*)\2')
_LUA_LANG_PATH_RE = re.compile(r'[\'"]/language/([A-Za-z_]\w*)[\'"]')
# A locale table: 3+ distinct xx-yy keys in one file is a translation map, not a coincidence.
_LUA_LOCALE_RE = re.compile(r'[\'"]([a-z]{2}-[a-z]{2})[\'"]')

# Fallback only. The authoritative list is read from the game's own config below — the same
# principle as reading the redscript log instead of re-running the compiler.
FALLBACK_LANGUAGE_VARS = ("VoiceOver", "Subtitles", "OnScreen", "Platform")
LANGUAGE_OPTION_FILES = ("r6/config/settings/options.json",
                         "r6/config/settings/platform/pc/options.json")

def game_language_vars(game_root: Path) -> tuple:
    """The /language config vars the ENGINE actually defines, read from the game's own
    options.json. Falls back to the known four if those files are unreadable — never returns
    empty, because an empty set would mark every probe in every mod as a crash."""
    found = set()
    for rel in LANGUAGE_OPTION_FILES:
        try:
            data = json.loads((game_root / rel).read_text(encoding='utf-8', errors='ignore'))
        except (OSError, ValueError):
            continue
        groups = data.get("groups") if isinstance(data, dict) else None
        for g in groups or []:
            if not isinstance(g, dict) or g.get("group_name") != "/language":
                continue
            for v in g.get("vars") or []:
                if isinstance(v, dict) and v.get("name"):
                    found.add(str(v["name"]))
    return tuple(sorted(found)) if found else FALLBACK_LANGUAGE_VARS

def parse_language_vars(text: str) -> set:
    """Every /language var name this Lua source names, in either the paired form
    ("/language", "X") — which covers both GetVar calls and table literals — or the joined
    path form "/language/X"."""
    body = _LUA_COMMENT_RE.sub(' ', text)
    return ({m.group(3) for m in _LUA_LANG_PAIR_RE.finditer(body)} |
            {m.group(1) for m in _LUA_LANG_PATH_RE.finditer(body)})

def parse_locale_keys(text: str) -> set:
    return {m.group(1) for m in _LUA_LOCALE_RE.finditer(_LUA_COMMENT_RE.sub(' ', text))}

def _iter_cet_lua(game_root: Path):
    root = game_root / CET_MODS_REL
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in HEALTH_SKIP_DIRS]
        for fn in filenames:
            if not fn.lower().endswith(".lua"):
                continue
            f = Path(dirpath) / fn
            try:
                text = f.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            yield f.relative_to(root).parts[0], f.relative_to(game_root).as_posix(), text

def find_language_var_bugs(game_root: Path, valid: tuple | None = None) -> list:
    """Probes of a /language var the engine does not define — each one an unavoidable hard
    crash on any code path that reaches it. Returns [{caller, var, verdict, suggestion, rel}];
    verdict is 'typo' when the name is close to a real var (Subtitle → Subtitles) or 'unknown'
    when it resembles nothing (Text)."""
    real = tuple(valid) if valid else game_language_vars(game_root)
    lower = {v.lower(): v for v in real}
    out = []
    for caller, rel, text in _iter_cet_lua(game_root):
        for var in sorted(parse_language_vars(text)):
            if var in real:
                continue
            near = lower.get(var.lower()) or next(
                iter(difflib.get_close_matches(var, real, n=1, cutoff=0.7)), "")
            out.append({"caller": caller, "var": var, "rel": rel,
                        "verdict": "typo" if near else "unknown", "suggestion": near})
    out.sort(key=lambda b: (b["caller"], b["var"]))
    return out

def find_locale_gaps(game_root: Path, locale: str) -> list:
    """CET mods carrying a translation table with no entry for the user's locale. Returns
    [{caller, locales, rel}].

    Not a fault on its own — the usual outcome is falling back to English, which is fine.
    It matters because the fallback branch is the one nobody tests, and it is what carries a
    player into a landmine like the one above. Combined with a bad /language var in the same
    mod it stops being a guess and becomes a diagnosis."""
    if not locale:
        return []
    out = []
    for caller, rel, text in _iter_cet_lua(game_root):
        locales = parse_locale_keys(text)
        if len(locales) >= 3 and locale.lower() not in locales:
            out.append({"caller": caller, "locales": sorted(locales), "rel": rel})
    out.sort(key=lambda g: g["caller"])
    return out

def configured_locale() -> str:
    """The locale to check against. Never guessed from the game folder: the setting the mods
    branch on is the player's chosen /language, and reading it wrongly would be worse than
    not checking. Set it with --check-locale <xx-yy>, which stores it in CONFIG_DIR."""
    return str(load_config().get("locale", "") or "").lower()

def report_locale(game_root: Path, locale: str = "") -> int:
    """Print the locale-sensitivity section plus a machine-parseable line. Read-only. Returns
    the number of crash-class findings (bad /language vars)."""
    bugs = find_language_var_bugs(game_root)
    locale = (locale or configured_locale()).lower()
    gaps = find_locale_gaps(game_root, locale)
    gap_mods = {g["caller"] for g in gaps}
    if bugs:
        print(f"  [!] {len(bugs)} probe(s) of a /language config var the engine does not "
              "define. Each one HARD-CRASHES the game on any path that reaches it — this is "
              "a C++ engine assert, so the mod's own pcall cannot catch it:")
        for b in bugs:
            hint = (f' — did it mean "{b["suggestion"]}"?' if b["suggestion"]
                    else " — no such var exists")
            danger = "  <-- and this mod has no strings for your locale" \
                if b["caller"] in gap_mods else ""
            print(f'      - {b["caller"]}: GetVar("/language", "{b["var"]}"){hint}{danger}')
            print(f"          {b['rel']}")
        if gap_mods & {b["caller"] for b in bugs}:
            print("  The marked mod(s) are the dangerous combination: the bad probe sits on "
                  "the fallback path that an unlisted locale takes, so it crashes for you and "
                  "for nobody else.")
        print(f"  Real /language vars: {', '.join(game_language_vars(game_root))}. "
              "Fix by editing the mod's Lua; there is no safe way to call the others.")
    if gaps:
        print(f"  [i] {len(gaps)} CET mod(s) carry translations but list no '{locale}' — they "
              "fall back to another language. Usually harmless, but the fallback branch is "
              "the least-tested code in any mod:")
        for g in gaps[:6]:
            print(f"      - {g['caller']} ({len(g['locales'])} locales, none of them {locale})")
        if len(gaps) > 6:
            print(f"      ... and {len(gaps) - 6} more")
    if not locale:
        print("  [i] No locale configured, so translation coverage was not checked. "
              "Run --check-locale <xx-yy> (e.g. ua-ua) once to record yours.")
    print(f"[locale] {len(bugs)} invalid /language probe(s); {len(gaps)} mod(s) missing "
          f"'{locale or 'n/a'}'")
    return len(bugs)

def action_check_locale(game_root: Path, locale: str = ""):
    if locale:
        cfg = load_config()
        cfg["locale"] = locale.lower()
        save_config(cfg)
    print("\n=== Locale Sensitivity (/language vars and translation coverage) ===")
    print(f"  Engine /language vars: {', '.join(game_language_vars(game_root))}")
    report_locale(game_root, locale)

# ─────────────────────── File-descriptor headroom ───────────────────────
# A working session peaked at ~3411 open fds against a hard `ulimit -n` of 4096 (83%), with
# 1135 mod archives and 320 CET mods installed. Adding mods can hit the real 4096 wall.
#
# The obvious model — archives + CET mods x2 — gives 1775 for that install, barely half the
# measured 3411. Shipping it would report 43% headroom-used where the truth was 83%, and a
# capacity check that says "plenty of room" right up to the wall is worse than no check at
# all. So the model carries an explicit constant for everything that is NOT a mod (engine,
# graphics and audio drivers, fonts, shader cache, sockets), derived from that one session:
#
#     3411 measured  -  (1135 archives + 320 CET x 2)  =  1636
#
# That constant is by far the weakest number here: ONE observation, on ONE machine, fitted
# after the fact. It is honest about the order of magnitude and nothing more. The MARGINAL
# figures are the trustworthy part — one fd per archive, two per CET mod (db.sqlite3 + .log,
# 260 of each seen in a census at 2147 handles) — so "how many more mods fit" is much better
# grounded than "how many fds am I using".
#
# Deliberately NOT modelled: wine's msvcrt caps CRT stdio handles at 2048, and three runs
# peaking at exactly 2146 once looked like proof of it. It was a coincidence — .archive files
# are opened through Win32 CreateFile and never count against that cap, and a later run
# reached 3411. Do not reintroduce 2048 into this arithmetic.
FD_PER_ARCHIVE = 1
FD_PER_CET_MOD = 2          # db.sqlite3 + .log, one each
FD_BASELINE_OVERHEAD = 1636
FD_WARN_FRACTION = 0.75
FD_ALARM_FRACTION = 0.90

def fd_limits() -> tuple:
    """(soft, hard) RLIMIT_NOFILE for THIS process. A caveat worth stating wherever it is
    used: the game is launched by Steam/Proton, which may set its own limits, so this is the
    manager's view and only matches the game's when both inherit from the same session."""
    try:
        import resource
        return resource.getrlimit(resource.RLIMIT_NOFILE)
    except Exception:
        return (0, 0)

def count_loadable_archives(game_root: Path) -> int:
    """.archive files the game will open at startup — both loose in archive/pc/mod and inside
    REDmods under mods/<Name>/archive. Counted from disk rather than the manifest so that
    hand-installed archives, which consume a descriptor exactly like managed ones, are
    included."""
    n = 0
    for root_rel in ("archive/pc/mod", "mods"):
        root = game_root / root_rel
        if not root.is_dir():
            continue
        for _dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in HEALTH_SKIP_DIRS]
            n += sum(1 for f in filenames if f.lower().endswith(".archive"))
    return n

def estimate_fd_usage(game_root: Path, limit: int = 0) -> dict:
    archives = count_loadable_archives(game_root)
    cet = len(installed_cet_mods(game_root))
    mod_fds = archives * FD_PER_ARCHIVE + cet * FD_PER_CET_MOD
    est = mod_fds + FD_BASELINE_OVERHEAD
    soft, hard = fd_limits()
    cap = limit or soft or hard
    free = max(cap - est, 0)
    return {"archives": archives, "cet": cet, "mod_fds": mod_fds, "estimate": est,
            "soft": soft, "hard": hard, "limit": cap,
            "fraction": (est / cap) if cap else 0.0,
            "room_archives": free // FD_PER_ARCHIVE,
            "room_cet": free // FD_PER_CET_MOD}

def report_fd_headroom(game_root: Path, limit: int = 0) -> int:
    """Print the descriptor-headroom section plus a machine-parseable line. Read-only.
    Returns 1 when the estimate is past the warning threshold, else 0."""
    e = estimate_fd_usage(game_root, limit)
    if not e["limit"]:
        print("[fds] limit unknown; estimate not comparable")
        return 0
    pct = e["fraction"] * 100
    over = e["fraction"] >= FD_ALARM_FRACTION
    warn = e["fraction"] >= FD_WARN_FRACTION
    if warn:
        word = "AT THE LIMIT" if over else "getting tight"
        print(f"  [{'!' if over else 'i'}] File-descriptor headroom is {word}: "
              f"~{e['estimate']} of {e['limit']} ({pct:.0f}%).")
        print(f"      {e['archives']} archive(s) x{FD_PER_ARCHIVE} + {e['cet']} CET mod(s) "
              f"x{FD_PER_CET_MOD} = {e['mod_fds']} from mods, plus ~{FD_BASELINE_OVERHEAD} "
              "for the engine, drivers and fonts.")
        print(f"      Room for roughly {e['room_archives']} more archive mod(s) or "
              f"{e['room_cet']} more CET mod(s) before the wall.")
        print("      Raising the limit is cheap insurance and fixes nothing else; note "
              "wine's 2048 CRT stdio cap is NOT what binds here (.archive files go through "
              "Win32 CreateFile and do not count against it).")
        print("      The estimate's fixed overhead is calibrated from a single measured "
              "session — treat the trend as reliable and the absolute number as rough.")
    print(f"[fds] ~{e['estimate']} of {e['limit']} ({pct:.0f}%); "
          f"{e['archives']} archive(s); {e['cet']} CET mod(s)")
    return 1 if warn else 0

def action_check_fds(game_root: Path, limit: int = 0):
    soft, hard = fd_limits()
    print("\n=== File-Descriptor Headroom ===")
    print(f"  This process: soft={soft}, hard={hard}. The game is launched by Steam/Proton "
          "and may inherit different limits; pass a number to --check-fds to override.")
    report_fd_headroom(game_root, limit)

# ─────────────────── Disabled mods and deploy-time pruning ───────────────────
# Deploy only ever ADDED. Removing a mod dropped its manifest entry, but its archive was
# still sitting in Downloads, so the very next deploy saw "not installed" and put it back —
# every hand-fix undone. Observed 2026-07-17: the user switched the main menu to Lucy Walk
# Moon, but City of Dreams (mod 8333) was still fully deployed and still in the manifest,
# and its 84 MB !v2_Altered Carbon1.archive kept winning the menu. The 33 items in
# _removed_duplicate_mods/ are the scar tissue from doing this by hand.
#
# So the missing piece is not deletion — _remove_mods() already trashes files safely — but a
# DURABLE record of intent that survives the next deploy. That is what this is.
#
# Keyed by mod IDENTITY, not by archive stem: "I don't want City of Dreams" has to keep
# holding when the author uploads 1.6, which is a different stem entirely. Same identity as
# the one-version-per-mod rule, so the two agree about what "the same mod" means.
DISABLED_NAME = ".mod_disabled.json"

def disable_key(stem: str) -> str:
    ident = mod_identity(stem)
    return f"id:{ident[0]}/{ident[1]}" if ident else f"stem:{stem.lower()}"

def load_disabled(game_root: Path) -> dict:
    try:
        data = json.loads((game_root / DISABLED_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    entries = data.get("disabled") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}

def save_disabled(game_root: Path, entries: dict):
    path = game_root / DISABLED_NAME
    payload = {"game_root": str(game_root),
               "updated": datetime.now().isoformat(timespec="seconds"),
               "disabled": entries}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    tmp.replace(path)

def disabled_scope(stem: str, disabled: dict) -> str:
    """'' (enabled) | 'all' | 'lua'. A 'lua' disable drops only the mod's CET Lua half and
    keeps its r6/scripts, so other mods' `import` statements still resolve."""
    entry = (disabled or {}).get(disable_key(stem))
    if entry is None:
        return ""
    return str(entry.get("scope", "all")) or "all"

def is_disabled(stem: str, disabled: dict) -> bool:
    """Fully disabled — the archive is skipped entirely. A 'lua' scope is NOT this: that mod
    still deploys, it just does not get its CET files written."""
    return disabled_scope(stem, disabled) == "all"

def _is_cet_path(rel: str) -> bool:
    return rel.lower().startswith(CET_MODS_REL.as_posix().lower() + "/")

def desired_paths(mods: dict, disabled: dict) -> set:
    """Case-folded paths owned by mods that are still ENABLED — the set the install should
    contain. Case-folded because this tree is read through Wine, where Scripts/x.reds and
    scripts/x.reds are one file; comparing exactly would mark a case-drifted managed file as
    unwanted and delete something that is in active use."""
    keep = set()
    for stem, files in (mods or {}).items():
        scope = disabled_scope(stem, disabled)
        if scope == "all":
            continue
        # A lua-scoped disable keeps everything except the mod's CET files.
        keep |= {r.lower() for r in files if scope != "lua" or not _is_cet_path(r)}
    return keep

def plan_prune(game_root: Path, mods: dict, disabled: dict) -> dict:
    """What a sync would remove: files owned ONLY by disabled mods.

    The guard that makes this safe is that a path survives if ANY enabled mod owns it. The
    manifest is known to carry bad attribution — entries whittled down to files that were
    never theirs (see _release_stale_claims) — so "this entry claims it" is not sufficient
    grounds to delete anything. "No mod you still want claims it" is.

    Returns {stems, paths, missing, bytes, total_managed}."""
    keep = desired_paths(mods, disabled)
    stems = [s for s in sorted(mods or {}) if disabled_scope(s, disabled)]
    paths, missing, size = [], [], 0
    seen = set()
    for stem in stems:
        partial = disabled_scope(stem, disabled) == "lua"
        for rel in mods[stem]:
            low = rel.lower()
            if low in keep or low in seen:
                continue
            if partial and not _is_cet_path(rel):
                continue
            seen.add(low)
            f = game_root / rel
            if f.is_file():
                try:
                    size += f.stat().st_size
                except OSError:
                    pass
                paths.append(rel)
            else:
                # Already taken out by hand. Still worth counting: it is the desync the
                # report is about, and dropping the entry is what stops it coming back.
                missing.append(rel)
    total = len({r.lower() for fs in (mods or {}).values() for r in fs})
    return {"stems": stems, "paths": paths, "missing": missing, "bytes": size,
            "total_managed": total}

# A manifest bug that emptied `keep` would make every managed file look unwanted, and the
# sync would then delete the entire install in one run. Nothing in the logic above should
# ever produce that, which is exactly why it is worth a hard stop: the cost of the check is
# nil and the cost of being wrong is the user's collection.
#
# BOTH conditions must hold. A fraction alone is useless at small scale — disabling two mods
# out of three is a perfectly normal thing to do and trips 50% instantly — while the failure
# this guards against, a bug that unowns everything, would target thousands of files on a
# 1442-entry install. Below the floor the blast radius is small and it all goes to
# .mod_trash anyway, so there is nothing worth blocking.
PRUNE_RUNAWAY_FRACTION = 0.5
PRUNE_RUNAWAY_MIN_FILES = 50

def prune_is_runaway(plan: dict) -> bool:
    total = plan["total_managed"]
    hit = len(plan["paths"]) + len(plan["missing"])
    return (bool(total) and hit >= PRUNE_RUNAWAY_MIN_FILES
            and hit > total * PRUNE_RUNAWAY_FRACTION)

def sync_prune(game_root: Path, mods: dict, disabled: dict, dry_run: bool,
               purge: bool = False, force: bool = False) -> dict:
    """Remove the files of disabled mods and drop their manifest entries. Returns the plan,
    with 'refused' set when the runaway guard tripped. Mutates `mods` when not a dry run."""
    plan = plan_prune(game_root, mods, disabled)
    plan["refused"] = False
    if not plan["stems"]:
        return plan
    if prune_is_runaway(plan) and not force:
        plan["refused"] = True
        plan["refused_why"] = "runaway"
        return plan
    # The redscript graph: orphaning an import fails the compile for the WHOLE install, so
    # this is a refusal rather than a warning. Checked here, at the one place that actually
    # deletes, so no caller can route around it.
    scopes = {s: disabled_scope(s, disabled) for s in plan["stems"]}
    impact = uninstall_impact(game_root, mods, plan["stems"], scopes, load_mod_names())
    plan["impact"] = impact
    if (impact["blockers"] or impact.get("symbols")) and not force:
        plan["refused"] = True
        plan["refused_why"] = "imports"
        return plan
    if not dry_run:
        only = {r.lower() for r in plan["paths"]} | {r.lower() for r in plan["missing"]}
        partial = any(v == "lua" for v in scopes.values())
        _remove_mods(game_root, mods, plan["stems"], dry_run=False, purge=purge,
                     only_paths=only if partial else None)
    return plan

def report_prune_plan(plan: dict, dry_run: bool):
    if not plan["stems"]:
        return
    verb = "Would remove" if dry_run else "Removed"
    print(f"\n=== Uninstall (disabled mods) ===")
    if plan.get("refused"):
        if plan.get("refused_why") == "imports":
            report_uninstall_impact(plan.get("impact", {"blockers": [], "warnings": []}),
                                    load_mod_names())
            return
        print(f"  [!] REFUSED: the plan covers {len(plan['paths']) + len(plan['missing'])} of "
              f"{plan['total_managed']} managed file(s) — more than half the install. That "
              "looks like a broken manifest rather than an intended uninstall.")
        print("      Review with --dry-run; pass --force-prune if it really is what you want.")
        return
    imp = plan.get("impact", {})
    if imp.get("warnings") or imp.get("companions"):
        # Blockers stripped: reaching here means there were none, or --force-prune overrode
        # them. The advisory halves still have to be said.
        report_uninstall_impact({"blockers": [], "warnings": imp.get("warnings", []),
                                 "companions": imp.get("companions", [])}, load_mod_names())
    print(f"  {verb} {len(plan['stems'])} disabled mod(s): {len(plan['paths'])} file(s)"
          f"{'' if dry_run else ' moved to .mod_trash'}"
          f" ({_fmt_size(plan['bytes'])}).")
    for s in plan["stems"][:8]:
        print(f"      - {s}")
    if len(plan["stems"]) > 8:
        print(f"      ... and {len(plan['stems']) - 8} more")
    if plan["missing"]:
        print(f"  [i] {len(plan['missing'])} owned file(s) were already gone from disk — "
              "removed by hand at some point. Dropping the manifest entry is what stops the "
              "next deploy putting them back.")

def report_disabled(game_root: Path, mods: dict) -> int:
    """Machine line for --check-health: how many mods are disabled and whether any of their
    files are still on disk (i.e. a prune is pending)."""
    disabled = load_disabled(game_root)
    plan = plan_prune(game_root, mods, disabled)
    pending = len(plan["paths"])
    if pending:
        print(f"  [!] {len(plan['stems'])} disabled mod(s) still have {pending} file(s) on "
              "disk. They are still loaded by the game:")
        for s in plan["stems"][:6]:
            print(f"      - {s}")
        print("  Fix: run --prune (or any deploy) to complete the uninstall.")
    print(f"[disabled] {len(disabled)} mod(s) disabled; {pending} file(s) awaiting removal")
    return pending

def _matches_needle(stem: str, needle: str, names: dict | None = None) -> bool:
    """Substring on the manifest key, on the mod's real Nexus name, or an exact mod-id.

    All three are needed because the key is only the downloaded filename: mod 8333 is
    "City of Dreams Menu Backgrounds" on Nexus but its key is
    `v2_All in one Vortex Installer-8333-1-5-1716634855`, which contains none of the words a
    user would type. The mod-id always works; the name works once --fetch-names has run and
    is the one people actually reach for."""
    if needle.isdigit():
        ident = mod_identity(stem)
        if (ident and ident[0] == needle) or framework_mod_id(stem) == needle:
            return True
    if needle.lower() in stem.lower():
        return True
    return needle.lower() in mod_display_name(stem, names).lower()

def _match_disable_targets(needle: str, mods: dict, downloads: Path | None):
    """Manifest entries first — those are what is actually installed. Falls back to Downloads
    so a mod can be disabled before it is ever deployed."""
    names = load_mod_names()
    hits = [m for m in mods if _matches_needle(m, needle, names)]
    if hits or not downloads or not downloads.exists():
        return hits, "manifest"
    try:
        arcs = [f.stem for f in downloads.iterdir() if f.is_file() and is_archive_file(f)]
    except OSError:
        return [], "manifest"
    return [s for s in sorted(arcs) if _matches_needle(s, needle, names)], "downloads"

def action_disable(game_root: Path, needle: str, dry_run: bool, purge: bool = False,
                   force: bool = False, downloads: Path | None = None, scope: str = "all"):
    manifest = load_manifest(game_root)
    mods = manifest["mods"]
    targets, source = _match_disable_targets(needle, mods, downloads)
    if not targets:
        print(f"No installed mod or download matches '{needle}'. Try --list-deployed.")
        return
    disabled = load_disabled(game_root)
    print(f"\n=== Disable ({len(targets)} match(es) from {source}) ===")
    names = load_mod_names()
    for t in targets:
        print(f"  - {describe_mod(t, names)}   [{disable_key(t)}]")
    # Applied to the in-memory set either way: a dry run has to plan against the state it
    # WOULD create, or it reports "nothing to remove" for the very mod being disabled.
    # Only the persisting is conditional.
    # Assessed BEFORE anything is recorded: a disable that would break the compile must not
    # leave a disabled entry behind for the next deploy to act on.
    pre = uninstall_impact(game_root, mods, targets, {t: scope for t in targets}, names)
    if (pre["blockers"] or pre.get("symbols")) and not force:
        report_uninstall_impact(pre, names)
        print("\n  Nothing was changed.")
        return
    # Companions are NOT reported here: report_prune_plan below prints them, still ahead of
    # the "Would remove"/"Removed" line, so the user sees them while deciding. Printing at
    # both points just says the same thing twice in one run.
    for t in targets:
        disabled[disable_key(t)] = {
            "stem": t, "when": datetime.now().isoformat(timespec="seconds"), "scope": scope}
    if not dry_run:
        save_disabled(game_root, disabled)
        what = ("as disabled" if scope == "all"
                else "as Lua-disabled (their r6/scripts stay, so other mods still compile)")
        print(f"  Recorded {len(targets)} mod(s) {what}.")
    plan = sync_prune(game_root, mods, disabled, dry_run, purge, force)
    report_prune_plan(plan, dry_run)
    if not plan["stems"]:
        print("  Nothing deployed for these yet — they simply will not be installed.")

def action_enable(game_root: Path, needle: str, dry_run: bool):
    disabled = load_disabled(game_root)
    hits = [k for k, v in disabled.items()
            if needle.lower() in k.lower() or needle.lower() in str(v.get("stem", "")).lower()]
    if not hits:
        print(f"No disabled mod matches '{needle}'. Try --list-disabled.")
        return
    print(f"\n=== Enable ({len(hits)} match(es)) ===")
    for k in hits:
        print(f"  - {disabled[k].get('stem', k)}")
    if dry_run:
        return
    for k in hits:
        disabled.pop(k, None)
    save_disabled(game_root, disabled)
    print(f"  Re-enabled {len(hits)} mod(s). Run a deploy to install them again.")

def action_list_disabled(game_root: Path):
    disabled = load_disabled(game_root)
    print(f"\n=== Disabled mods ({len(disabled)}) ===")
    if not disabled:
        print("  None. Disable one with --disable <name>.")
        return
    mods = load_manifest(game_root)["mods"]
    _names = load_mod_names()
    plan = plan_prune(game_root, mods, disabled)
    pending = {s for s in plan["stems"]}
    for k, v in sorted(disabled.items(), key=lambda kv: str(kv[1].get("stem", kv[0]))):
        mark = "  <-- files still on disk, run --prune" if v.get("stem") in pending else ""
        print(f"  - {describe_mod(str(v.get('stem', k)), _names)}   "
              f"(since {v.get('when', '?')}){mark}")

def action_prune(game_root: Path, dry_run: bool, purge: bool = False, force: bool = False):
    manifest = load_manifest(game_root)
    disabled = load_disabled(game_root)
    if not disabled:
        print("\nNothing is disabled, so there is nothing to prune. "
              "Disable a mod with --disable <name>.")
        return
    plan = sync_prune(game_root, manifest["mods"], disabled, dry_run, purge, force)
    report_prune_plan(plan, dry_run)
    if not plan["stems"]:
        print("\nAll disabled mods are already fully uninstalled.")

# ──────────────── Pinned files and runtime-state preservation ────────────────
# Deploy rewrote every managed file from its mod source with no notion of a file the user
# had deliberately changed. Two live losses, 2026-07-17:
#
#   * red4ext/plugins/audioware/audioware.dll, owned by Audioware-12001. A locally rebuilt
#     DLL (fixing the 100%-CPU busy-spin and the All Foods crash) is silently reverted to
#     stock 1.9.2 on the next deploy.
#   * red4ext/plugins/mod_settings/user.ini, owned by ONE mod (Perfect City Config-24423)
#     but actually a SHARED runtime store: 177 sections holding every ModSettings mod's
#     config plus the user's own in-game choices. A deploy overwrites the whole file and
#     the settings are gone.
#
# These need different mechanisms, which is why there are two.
#
# A PIN is explicit intent about one path: never overwrite it, whatever it is. That is the
# only thing that can protect a hand-built binary, because nothing about audioware.dll looks
# different from any other DLL.
#
# PRESERVE is a policy about a class of file, and it is deliberately write-ONCE rather than
# never-write: a mod legitimately ships defaults that must land on first install, but once
# the file exists it belongs to the user and the game, not to the mod. Overwriting is what
# destroys settings; refusing to create would break the mod on a fresh install.
PINS_NAME = ".mod_pins.json"

# Shared, mutable, written by the game or a framework at runtime. Kept short and only for
# cases that are actually known — a wrong entry here silently stops a mod updating its own
# config, which is a quiet failure of the same kind this exists to prevent.
RUNTIME_STATE_FILES = frozenset({
    "red4ext/plugins/mod_settings/user.ini",     # every ModSettings mod's config, shared
})
RUNTIME_STATE_DIRS = (
    "r6/storages/",         # RedscriptConfigFramework rewrites its JSON here on exit
)
# Hand-edited formats worth backing up before an overwrite. Binaries are excluded on
# purpose: a changed .archive or .dll is a new mod version 99.9% of the time, and copying
# 84 MB archives aside on every update would be pure churn.
BACKUP_EXTS = frozenset({'.ini', '.json', '.xml', '.yaml', '.yml', '.cfg', '.conf',
                         '.txt', '.lua', '.reds', '.tweak'})
BACKUP_MAX_BYTES = 4 * 1024 * 1024

def load_pins(game_root: Path) -> dict:
    """{"pinned": {rel: {...}}, "preserve": [rel or dir/]} — user intent about individual
    files, kept beside the manifest so it travels with the install it describes."""
    try:
        data = json.loads((game_root / PINS_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    pinned = data.get("pinned")
    preserve = data.get("preserve")
    return {"pinned": pinned if isinstance(pinned, dict) else {},
            "preserve": [str(p) for p in preserve] if isinstance(preserve, list) else []}

def save_pins(game_root: Path, pins: dict):
    path = game_root / PINS_NAME
    payload = {"game_root": str(game_root),
               "updated": datetime.now().isoformat(timespec="seconds"),
               "pinned": pins.get("pinned", {}), "preserve": pins.get("preserve", [])}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    tmp.replace(path)

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 16), b''):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()

def is_pinned(rel: str, pins: dict) -> bool:
    return rel.lower() in {k.lower() for k in pins.get("pinned", {})}

def is_runtime_state(rel: str, pins: dict | None = None) -> bool:
    low = rel.lower().lstrip('/')
    if low in RUNTIME_STATE_FILES:
        return True
    if any(low.startswith(d) for d in RUNTIME_STATE_DIRS):
        return True
    for extra in (pins or {}).get("preserve", []):
        e = extra.lower().lstrip('/')
        if low == e or (e.endswith('/') and low.startswith(e)):
            return True
    return False

def _worth_backing_up(dest: Path) -> bool:
    if dest.suffix.lower() not in BACKUP_EXTS:
        return False
    try:
        return dest.stat().st_size <= BACKUP_MAX_BYTES
    except OSError:
        return False

def deploy_disposition(rel: str, src: Path, dest: Path, pins: dict) -> str:
    """What deploy should do with one file: 'write' | 'pinned' | 'preserved' | 'backup'.

    Order matters. A pin is explicit user intent and outranks everything. Preservation only
    applies to a file that ALREADY exists — a missing one is a first install and the mod's
    defaults must land. A backup is only worth taking when the contents actually differ;
    rewriting an identical file is not a loss and does not need saving."""
    if is_pinned(rel, pins):
        return "pinned"
    if not dest.exists():
        return "write"
    if is_runtime_state(rel, pins):
        return "preserved"
    if _worth_backing_up(dest) and _sha256(dest) != _sha256(src):
        return "backup"
    return "write"

def action_pin(game_root: Path, rel_path: str, dry_run: bool):
    rel = Path(rel_path).as_posix().lstrip('/')
    target = game_root / rel
    if not target.is_file():
        print(f"  [!] Not a file in the game folder: {rel}")
        print("      Pass a path relative to the game root, e.g. "
              "red4ext/plugins/audioware/audioware.dll")
        return
    pins = load_pins(game_root)
    if dry_run:
        print(f"  Would pin {rel} ({_fmt_size(target.stat().st_size)}).")
        return
    pins["pinned"][rel] = {"sha256": _sha256(target),
                           "size": target.stat().st_size,
                           "when": datetime.now().isoformat(timespec="seconds")}
    save_pins(game_root, pins)
    print(f"  [✓] Pinned {rel}. Deploy will never overwrite it; --check-health reports if it "
          "changes.")

def action_unpin(game_root: Path, rel_path: str, dry_run: bool):
    pins = load_pins(game_root)
    needle = Path(rel_path).as_posix().lstrip('/').lower()
    hits = [k for k in pins["pinned"] if needle in k.lower()]
    if not hits:
        print(f"  [!] No pinned file matches '{rel_path}'. Try --list-pins.")
        return
    for k in hits:
        print(f"  - {k}")
    if dry_run:
        return
    for k in hits:
        pins["pinned"].pop(k, None)
    save_pins(game_root, pins)
    print(f"  [✓] Unpinned {len(hits)} file(s). Deploy may overwrite them again.")

def check_pins(game_root: Path) -> list:
    """State of each pinned file: 'ok' | 'changed' | 'missing'. 'changed' is not necessarily
    bad — the user may have edited it again — but it is the only way to notice that something
    overwrote a file that was supposed to be untouchable."""
    pins = load_pins(game_root)
    out = []
    for rel, meta in sorted(pins["pinned"].items()):
        f = game_root / rel
        if not f.is_file():
            out.append({"rel": rel, "state": "missing"})
        elif _sha256(f) != meta.get("sha256", ""):
            out.append({"rel": rel, "state": "changed"})
        else:
            out.append({"rel": rel, "state": "ok"})
    return out

def action_list_pins(game_root: Path):
    pins = load_pins(game_root)
    states = {c["rel"]: c["state"] for c in check_pins(game_root)}
    print(f"\n=== Pinned files ({len(pins['pinned'])}) ===")
    if not pins["pinned"]:
        print("  None. Pin one with --pin <path relative to the game root>.")
    for rel, meta in sorted(pins["pinned"].items()):
        mark = {"ok": "", "changed": "   <-- CHANGED since pinning",
                "missing": "   <-- MISSING"}[states.get(rel, "ok")]
        print(f"  - {rel}  ({_fmt_size(meta.get('size', 0))}, since "
              f"{meta.get('when', '?')}){mark}")
    print(f"\n  Runtime-state files preserved by policy (never overwritten once they exist):")
    for f in sorted(RUNTIME_STATE_FILES):
        print(f"  - {f}")
    for d in RUNTIME_STATE_DIRS:
        print(f"  - {d}**")
    for extra in pins["preserve"]:
        print(f"  - {extra}   (yours)")

def report_pins(game_root: Path) -> int:
    """Machine line for --check-health. Returns the number of pinned files that no longer
    match what was pinned."""
    checks = check_pins(game_root)
    bad = [c for c in checks if c["state"] != "ok"]
    if bad:
        print(f"  [!] {len(bad)} pinned file(s) no longer match what you pinned:")
        for c in bad:
            why = ("gone from disk" if c["state"] == "missing"
                   else "contents changed — something overwrote it, or you edited it again")
            print(f"      - {c['rel']}: {why}")
        print("  If you edited it deliberately, re-pin it to record the new contents "
              "(--pin <path>).")
    print(f"[pins] {len(checks)} pinned file(s); {len(bad)} changed or missing")
    return len(bad)

# ───────────────────── Per-frame script budget ─────────────────────
# The largest finding of the 2026-07-19 session, and the only one backed by a controlled A/B
# rather than inference. Of 322 installed CET mods, 143 register an `onUpdate` handler: Lua
# that runs EVERY frame, all of it on the game's single main thread. The manager judged each
# mod install-time only — does it deploy, does it conflict — and had no concept that this
# cost is additive and unbounded.
#
# Measured, gameplay frames only (0 < fps < 100 AND gpu_load >= 8):
#
#     322 mods  n=639   p5 24   p50 32   p95 51   GPU 18%
#     207 mods  n=127   p5 44   p50 68   p95 92   GPU 33%
#     frames below 30 fps: 31.8% of the session  ->  0.0%
#
# Frame rate DOUBLED. GPU load rose 18% -> 33%: the GPU was starved by a CPU-bound main
# thread and had ~3x headroom it could not use. On a Ryzen 7 5700X (8C/16T) — core count is
# irrelevant, CET Lua is single-threaded, so this is a pure single-thread throughput wall.
# The A/B is n=127 vs n=639; the effect (31.8% -> 0.0% of frames under 30) is far beyond
# noise but the exact p50 delta is soft.
#
# ── THE RULE THAT GOVERNS THIS WHOLE SECTION ───────────────────────────────────────────
# STATIC COUNTS ARE NOT A PER-MOD CPU RANKING. A handler that early-outs on a flag costs
# almost nothing; one that walks the entity list every frame costs everything, and they look
# identical to a parser. Counting is only trustworthy as an AGGREGATE SCALE signal — "this
# collection is far past any sane budget" — never as "mod X is your problem". Nothing here
# sorts mods by hook count or presents one as more expensive than another. Only a profiler
# or an A/B can rank them.
# ───────────────────────────────────────────────────────────────────────────────────────
_LUA_ONUPDATE_RE = re.compile(r'registerForEvent\s*\(\s*([\'"])onUpdate\1')
_LUA_ONDRAW_RE = re.compile(r'registerForEvent\s*\(\s*([\'"])onDraw\1')
_LUA_HOOK_RE = re.compile(r'\b(Observe|ObserveAfter|ObserveBefore|Override)\s*\(')

# The two points that were actually measured on this install. Everything between them is
# interpolation and is labelled as such wherever it is shown.
PERF_ONUPDATE_MEASURED_OK = 28      # p50 68 fps, no frames under 30
PERF_ONUPDATE_MEASURED_BAD = 143    # p50 32 fps, 31.8% of frames under 30
# Chosen, not measured: comfortably above the configuration that ran fine and well below the
# one that halved the frame rate.
PERF_ONUPDATE_WARN = 40

def scan_cet_cost(game_root: Path) -> dict:
    """Per-frame and hook registrations per CET mod. Returns {"mods": {folder: {...}},
    "onupdate": n, "ondraw": n, "hooks": n, "depended_upon": set}.

    `depended_upon` reuses the GetMod() graph from the cross-mod link check: a mod some other
    mod looks up cannot be removed without breaking that integration. That is a SAFETY
    property, and it is the only basis on which anything here suggests removing a mod —
    deliberately not a cost estimate."""
    per: dict = {}
    depended: set = set()
    for folder, rel, text in _iter_cet_lua(game_root):
        body = _LUA_COMMENT_RE.sub(' ', text)
        e = per.setdefault(folder, {"folder": folder, "onupdate": 0, "ondraw": 0, "hooks": 0})
        e["onupdate"] += len(_LUA_ONUPDATE_RE.findall(body))
        e["ondraw"] += len(_LUA_ONDRAW_RE.findall(body))
        e["hooks"] += len(_LUA_HOOK_RE.findall(body))
        # A self-lookup is not a dependency. Counting it would mark the mod as depended-upon
        # by itself and quietly protect it from every suggestion below.
        depended |= {t for t in parse_lua_getmod(text) if t.lower() != folder.lower()}
    return {"mods": per,
            "onupdate": sum(1 for e in per.values() if e["onupdate"]),
            "ondraw": sum(1 for e in per.values() if e["ondraw"]),
            "hooks": sum(e["hooks"] for e in per.values()),
            "depended_upon": depended}

def cet_mod_owner(folder: str, manifest_mods: dict) -> str:
    """The manifest entry that installed a CET mod folder, so a suggestion can be acted on.
    Empty when the folder is hand-installed and owned by nothing."""
    prefix = f"{CET_MODS_REL.as_posix()}/{folder}/".lower()
    for stem, files in (manifest_mods or {}).items():
        if any(r.lower().startswith(prefix) for r in files):
            return stem
    return ""

def perf_profile_candidates(game_root: Path, manifest_mods: dict) -> dict:
    """Per-frame mods that nothing else depends on — the set the measured A/B moved out.

    Selected purely by "no other mod reaches this one via GetMod()", never by how much Lua it
    contains. That criterion is what made the A/B safe, and it is honest: it says "removing
    this breaks no other mod", not "this one is slow"."""
    scan = scan_cet_cost(game_root)
    depended = {d.lower() for d in scan["depended_upon"]}
    safe, needed = [], []
    for folder, e in sorted(scan["mods"].items()):
        if not e["onupdate"]:
            continue
        row = dict(e, owner=cet_mod_owner(folder, manifest_mods))
        (needed if folder.lower() in depended else safe).append(row)
    return {"scan": scan, "safe": safe, "needed": needed}

def report_perf(game_root: Path, manifest_mods: dict | None = None) -> int:
    """Print the per-frame budget section plus a machine-parseable line. Read-only. Returns
    1 when the collection is past the warning threshold, else 0."""
    scan = scan_cet_cost(game_root)
    n_upd, n_hooks = scan["onupdate"], scan["hooks"]
    total = len(scan["mods"])
    over = n_upd >= PERF_ONUPDATE_WARN
    if over:
        word = ("far past" if n_upd >= PERF_ONUPDATE_MEASURED_BAD else "past")
        print(f"  [!] {n_upd} of {total} CET mod(s) run Lua on EVERY frame (onUpdate), "
              f"{word} a sane budget. All of it is on the game's single main thread:")
        print(f"      Measured on this install: {PERF_ONUPDATE_MEASURED_BAD} such mods gave a "
              "median 32 fps with 31.8% of frames under 30; cutting back to "
              f"{PERF_ONUPDATE_MEASURED_OK} gave a median 68 fps and no frames under 30. "
              "GPU load went 18% -> 33%, so the GPU was being starved, not saturated.")
        print(f"      {n_hooks} Observe/Override hook(s) are a second cost axis — they fire "
              "on the game's call frequency rather than per frame.")
        print("      More cores do not help: CET Lua is single-threaded.")
        print("      See --perf-profile for the mods that can be removed without breaking "
              "any other mod.")
        # Said explicitly and every time, because a list of counts invites exactly the wrong
        # conclusion and the fix direction called this out by name.
        print("      NOTE: these are counts, not costs. A handler that early-outs is nearly "
              "free and looks identical here to one that walks the entity list. Use this as "
              "a scale signal only — never to decide which single mod is slow.")
    print(f"[perf] {n_upd} per-frame mod(s) of {total}; {n_hooks} hook(s)")
    return 1 if over else 0

def action_perf_profile(game_root: Path, dry_run: bool, apply: bool = False,
                        purge: bool = False):
    manifest = load_manifest(game_root)
    mods = manifest["mods"]
    names = load_mod_names()
    prof = perf_profile_candidates(game_root, mods)
    safe, needed, scan = prof["safe"], prof["needed"], prof["scan"]
    print("\n=== Performance Profile (per-frame script cost) ===")
    print(f"  {scan['onupdate']} of {len(scan['mods'])} CET mod(s) run Lua every frame; "
          f"{scan['hooks']} Observe/Override hook(s) in total.")
    print(f"  {len(needed)} of them are looked up by another mod via GetMod() and are kept — "
          "removing those breaks the mod that depends on them.")
    if not safe:
        print("  [✓] Nothing to suggest: every per-frame mod is depended on by another mod.")
        return
    print(f"\n  {len(safe)} per-frame mod(s) that NO other mod depends on. This is the same "
          "criterion as the measured A/B, and it is a safety test, not a cost estimate — "
          "the list is alphabetical precisely because ranking it by size would be a lie:")
    for row in safe[:40]:
        owner = (f"   [{mod_display_name(row['owner'], names)}]" if row["owner"]
                 else "   [hand-installed — not managed, cannot be disabled here]")
        print(f"      - {row['folder']}{owner}")
    if len(safe) > 40:
        print(f"      ... and {len(safe) - 40} more")
    owned = [r for r in safe if r["owner"]]
    unowned = len(safe) - len(owned)
    if unowned:
        print(f"\n  {unowned} of these are hand-installed and owned by no manifest entry, so "
              "they must be moved out by hand (see --check-unmanaged).")
    if not apply:
        print(f"\n  Nothing has been changed. Re-run with --apply to disable the {len(owned)} "
              "managed one(s); every one is reversible with --enable.")
        return
    if dry_run:
        print(f"\n  [DRY RUN] Would disable {len(owned)} mod(s).")
        return
    disabled = load_disabled(game_root)
    for row in owned:
        disabled[disable_key(row["owner"])] = {
            "stem": row["owner"], "when": datetime.now().isoformat(timespec="seconds"),
            "reason": "performance profile (per-frame script cost)"}
    save_disabled(game_root, disabled)
    print(f"\n  Disabled {len(owned)} mod(s) for performance. Restore any of them with "
          "--enable <name>.")
    plan = sync_prune(game_root, mods, disabled, dry_run=False, purge=purge, force=False)
    report_prune_plan(plan, dry_run=False)

# ──────────── Scale-dependent settings in performance mods ────────────
# A perf mod ships defaults sized for an install far smaller than this one, and its own
# profile ladder tops out below what a large collection actually uses. Then it sits pinned
# in its most aggressive state forever, working on a number it can never bring down.
#
# Measured on this install (EXRC / Runtime Controller, mod 28727): the LuaJIT heap's LIVE
# floor after a forced full GC is ~577 MB and it peaks over 1000 MB. Its shipped default
# limit is 128 MB, escalating at 0.9x and 1.25x — so CRIT_MEM at 115 MB and EMERGENCY at
# 160 MB, both crossed before the game finishes loading. EXRC was in permanent EMERGENCY
# from 58 s after load, cycling EMERGENCY -> AUTO RECOVERY every ~33 s. NONE of its four
# built-in profiles fit: even OVER (512 MB, labelled "500+ MOD") is below this install's
# live floor. A hand-set 800 was needed.
#
# READ THIS BEFORE ACTING ON THE CHECK: raising the limit demonstrably changed EXRC's own
# behaviour (first EMERGENCY moved from 58 s to 197 s) and did NOT improve the frame rate.
# GC pacing was never the bottleneck on this install — per-frame CET script cost was, see
# the per-frame budget section. This check buys correct behaviour from the perf mod and an
# end to pointless GC churn. It does not buy frames, and every message it prints says so,
# because the whole reason this was written down is so the experiment is not re-run.
#
# What is NOT done here: predicting the heap from the mod count. There is exactly ONE
# measurement (577 MB live at 322 CET mods) and fitting a curve to a single point is the
# same mistake as reading a per-mod CPU ranking out of static counts. The check compares
# against the mod's own profile ladder, and cites the measurement only on an install of
# comparable size.
SCALE_SETTINGS_NAME = "scale_settings.json"
# Below this share of the measured install's CET mod count, the measurement is not evidence
# about THIS install and only the profile-ladder half of the check applies.
SCALE_COMPARABLE_FRACTION = 0.75

PERF_SCALE_MODS = {
    "28727": {
        "name": "EXRC (Runtime Controller)",
        "folders": ("EXRC", "RuntimeController", "ExRC"),
        "key": "limit",
        "unit": "MB",
        "what": "LuaJIT heap budget",
        "default": 128,
        # (label, limit MB, mod count the profile is labelled for)
        "profiles": [("VANILLA", 64, 0), ("STABLE", 128, 100),
                     ("ULTRA", 256, 300), ("OVER", 512, 500)],
        "escalate": (0.9, 1.25),
        "escalate_labels": ("CRIT_MEM", "EMERGENCY"),
        "measured_live_floor": 577,
        "measured_peak": 1000,
        "measured_at_cet_mods": 322,
        "suggest": 800,
    },
}

def load_scale_settings() -> dict:
    """User-maintained scale-sensitive settings merged over the built-in table, in CONFIG_DIR.
    Same reasoning as the domain and companion-root overrides: this table describes other
    people's mods and goes stale on their release schedule, not mine."""
    out = {k: dict(v) for k, v in PERF_SCALE_MODS.items()}
    try:
        data = json.loads((CONFIG_DIR / SCALE_SETTINGS_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        for mid, spec in data.items():
            if isinstance(spec, dict):
                out.setdefault(str(mid), {}).update(spec)
    return out

def mod_id_installed(mod_id: str, mods: dict) -> str:
    """The manifest key for an installed mod-id, or ''."""
    for stem in sorted(mods or {}):
        ident = mod_identity(stem)
        if (ident and ident[0] == mod_id) or framework_mod_id(stem) == mod_id:
            return stem
    return ""

def find_scale_config(game_root: Path, spec: dict, stem: str, mods: dict):
    """(value, path) for the scale-sensitive key, or (None, None).

    The config file is found by SEARCHING the mod's own folder for a JSON object carrying the
    key, not by hardcoding a filename: CET mods write config under whatever name they like,
    and a wrong guessed path would report 'not configured' on an install where it plainly is.
    A CET mod that has never been run has no config yet, which is a real and different
    answer from 'misconfigured'."""
    key = spec["key"]
    roots = []
    for folder in spec.get("folders", ()):
        roots.append(game_root / CET_MODS_REL / folder)
        roots.append(game_root / "r6/storages" / folder)
    for rel in (mods or {}).get(stem, []):
        low = rel.replace('\\', '/')
        prefix = CET_MODS_REL.as_posix() + "/"
        if low.lower().startswith(prefix.lower()):
            roots.append(game_root / prefix / low[len(prefix):].split('/')[0])
    seen = set()
    for root in roots:
        if not root.is_dir() or root in seen:
            continue
        seen.add(root)
        for f in sorted(root.rglob("*.json")):
            try:
                data = json.loads(f.read_text(encoding='utf-8', errors='ignore'))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get(key), (int, float)):
                return data[key], f.relative_to(game_root).as_posix()
    return None, None

def profile_for_count(profiles, n: int):
    """The profile the mod's own ladder picks for this many CET mods."""
    pick = profiles[0]
    for p in profiles:
        if n >= p[2]:
            pick = p
    return pick

def check_scale_settings(game_root: Path, mods: dict, names: dict | None = None,
                         table: dict | None = None) -> list:
    """Scale-sensitive perf-mod settings that are too small for this install. Read-only.

    Each row: {"mod_id","name","stem","value","source","configured","cet_mods","profile",
               "top","verdict","basis","thresholds","spec"}. `verdict` is 'below-measured'
    (this install is comparable to where the floor was measured, and the setting is under
    it), 'below-profile' (the mod count is past the ladder's top rung), or 'ok'."""
    table = table if table is not None else load_scale_settings()
    cet_mods = len(installed_cet_mods(game_root))
    out = []
    for mod_id, spec in sorted(table.items()):
        stem = mod_id_installed(mod_id, mods)
        if not stem:
            continue
        value, source = find_scale_config(game_root, spec, stem, mods)
        configured = value is not None
        if not configured:
            value = spec.get("default")
        if not isinstance(value, (int, float)):
            continue
        profiles = [tuple(p) for p in spec.get("profiles", [])]
        top = max(profiles, key=lambda p: p[1]) if profiles else None
        floor = spec.get("measured_live_floor")
        at = spec.get("measured_at_cet_mods") or 0
        comparable = bool(at) and cet_mods >= at * SCALE_COMPARABLE_FRACTION
        verdict, basis = "ok", ""
        if comparable and floor and value < floor:
            verdict = "below-measured"
            basis = (f"measured on an install of {at} CET mods; this one has {cet_mods}")
        elif top and cet_mods > top[2] and value <= top[1]:
            verdict = "below-profile"
            basis = (f"{cet_mods} CET mods is past the top rung of this mod's own ladder "
                     f"({top[0]} = {top[1]} {spec.get('unit','')}, labelled {top[2]}+ mods)")
        if verdict == "ok":
            continue
        lo, hi = spec.get("escalate", (0.9, 1.25))
        out.append({
            "mod_id": mod_id, "name": spec.get("name") or mod_display_name(stem, names),
            "stem": stem, "value": value, "source": source, "configured": configured,
            "cet_mods": cet_mods, "profile": profile_for_count(profiles, cet_mods) if profiles
            else None, "top": top, "verdict": verdict, "basis": basis,
            "thresholds": (round(value * lo), round(value * hi)), "spec": spec,
        })
    return out

def report_scale(game_root: Path, mods: dict, names: dict | None = None) -> int:
    """Print the scale-sensitive settings section plus a machine-parseable line. Read-only.
    Returns the number of settings that are too small for this install."""
    rows = check_scale_settings(game_root, mods, names)
    for r in rows:
        spec = r["spec"]
        unit = spec.get("unit", "")
        lo_label, hi_label = spec.get("escalate_labels", ("warn", "critical"))
        where = f"in {r['source']}" if r["configured"] else "shipped default (not configured)"
        print(f"  [!] {r['name']}: {spec['what']} {spec['key']}={r['value']} {unit} "
              f"({where}) is too small for this install.")
        print(f"      It escalates to {lo_label} at {r['thresholds'][0]} {unit} and "
              f"{hi_label} at {r['thresholds'][1]} {unit} — {r['basis']}.")
        if r["verdict"] == "below-measured":
            print(f"      The live floor here was measured at {spec['measured_live_floor']} "
                  f"{unit} after a forced full GC, peaking over {spec['measured_peak']} "
                  f"{unit}. The mod cannot bring the heap under its own threshold, so it "
                  "stays pinned in its most aggressive state and never recovers.")
        if r["top"] and r["value"] <= r["top"][1]:
            print(f"      None of its built-in profiles fit: even {r['top'][0]} "
                  f"({r['top'][1]} {unit}) is below what this install uses.")
        if spec.get("suggest"):
            print(f"      Set {r['name']}'s {spec['key']} to about {spec['suggest']} {unit} "
                  f"— use --set-scale {r['mod_id']} {spec['suggest']}, which also pins the "
                  "file so the next deploy does not revert it.")
        # Said every time, on purpose. The measured result is that this changes the perf
        # mod's behaviour and NOT the frame rate; without this line the finding reads like
        # free fps and the experiment gets re-run.
        print("      This stops pointless GC churn. It does NOT raise the frame rate — "
              "measured: the first escalation moved from 58 s to 197 s after load and the "
              "frame rate did not change. Per-frame script cost is the frame-rate problem "
              "here; see --perf-profile.")
    print(f"[scale] {len(rows)} performance setting(s) below what this install needs")
    return len(rows)

def action_check_scale(game_root: Path):
    mf = load_manifest(game_root)
    print("\n=== Scale-Dependent Performance Settings ===")
    n = report_scale(game_root, mf.get("mods", {}), load_mod_names())
    if not n:
        print("  Every known scale-sensitive setting is sized for this install "
              "(or the mod that owns it is not installed).")

def action_set_scale(game_root: Path, needle: str, value: float, dry_run: bool):
    """Write a scale-sensitive setting and pin the file. The pin is the part that is easy to
    forget and expensive to miss: the config lives in the mod's own folder, so the next
    deploy of that mod would put the shipped default straight back."""
    manifest = load_manifest(game_root)
    mods = manifest.get("mods", {})
    names = load_mod_names()
    table = load_scale_settings()
    print("\n=== Set Scale-Dependent Setting ===")
    hits = [(mid, spec) for mid, spec in sorted(table.items())
            if needle == mid or needle.lower() in (spec.get("name") or "").lower()]
    if not hits:
        known = ", ".join(f"{m} ({sp.get('name')})" for m, sp in sorted(table.items()))
        print(f"  No known scale-sensitive setting matches '{needle}'. Known: {known}")
        return
    for mod_id, spec in hits:
        stem = mod_id_installed(mod_id, mods)
        if not stem:
            print(f"  [i] {spec.get('name')} (mod {mod_id}) is not installed — nothing to set.")
            continue
        current, source = find_scale_config(game_root, spec, stem, mods)
        if source is None:
            print(f"  [!] {spec.get('name')} is installed but no config file carrying "
                  f"'{spec['key']}' was found. CET mods usually write theirs on first run — "
                  "launch the game once, then retry.")
            continue
        path = game_root / source
        print(f"  {spec.get('name')}: {spec['key']} {current} -> {value} in {source}")
        if dry_run:
            print("  [dry-run] Nothing written, nothing pinned.")
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            data[spec["key"]] = value
            path.write_text(json.dumps(data, indent=2), encoding='utf-8')
        except (OSError, ValueError) as e:
            print(f"  [!] Could not write {source}: {e}")
            continue
        # Reuses the pin store rather than a parallel mechanism, so --list-pins and the
        # changed-pin health check see this file like any other protected one.
        pins = load_pins(game_root)
        pins["pinned"][source] = {
            "sha256": _sha256(path), "size": path.stat().st_size,
            "when": datetime.now().isoformat(timespec="seconds"),
            "why": f"scale-sensitive setting ({spec['key']}={value})"}
        save_pins(game_root, pins)
        print(f"  Written and pinned. Deploy will no longer overwrite {source}; "
              "undo with --unpin.")

# ─────────────── Uninstall safety: two dependency graphs ───────────────
# A real uninstall (see the disable/prune section) is unsafe unless it checks BOTH graphs.
# They are independent and their blast radii differ by orders of magnitude:
#
#   (a) Lua       mod A calls GetMod("B"). Remove B and the call returns nil forever;
#                 A's integration silently stops and nothing logs an error.
#                 Blast radius: one mod.  ->  WARN.
#   (b) redscript mod A does `import B.Something`. Remove B's r6/scripts and A fails to
#                 COMPILE, which fails the whole script blob — every redscript mod in the
#                 install stops loading, not just A.
#                 Blast radius: the entire install.  ->  BLOCK.
#
# Found live 2026-07-19 while removing unwanted mods: StealthRunner (7616) is imported by
# InformativeHealthbar (`import StealthRunner.*`), StreetSense
# (`import StealthRunner.StealthRunnerConfig`) and LHUD. Removing it on request would have
# broken the compile for the whole install; it was kept for exactly this reason.
#
# Note the asymmetry that makes partial uninstall worth having: StealthRunner is 75 .reds /
# 279 wrap-replace annotations versus 22 Lua / 116 Observe-Override. The two halves are
# separable and only the reds half is load-bearing for other mods, so dropping the Lua half
# alone is a real option — it is where the per-frame cost lives (see the perf section) while
# the reds half is what everything else imports.
_REDS_MODULE_RE = re.compile(r'^[ \t]*module[ \t]+([A-Za-z_][\w.]*)', re.MULTILINE)
# The trailing `*` of `import X.*` is part of the token, so it is captured and stripped
# below rather than matched as an optional group — a greedy [\w.]* eats the dot first and
# leaves 'X.', which then only compares correctly by accident.
_REDS_IMPORT_RE = re.compile(r'^[ \t]*import[ \t]+([A-Za-z_][\w.]*\*?)', re.MULTILINE)

def _iter_reds(game_root: Path):
    for root_rel in ("r6/scripts", "red4ext/plugins"):
        root = game_root / root_rel
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in HEALTH_SKIP_DIRS]
            for fn in filenames:
                if not fn.lower().endswith(".reds"):
                    continue
                f = Path(dirpath) / fn
                try:
                    text = f.read_text(encoding='utf-8', errors='ignore')
                except OSError:
                    continue
                yield f.relative_to(game_root).as_posix(), text

def parse_reds_modules(text: str) -> tuple:
    """(declared modules, imported paths) for one source. Comments are stripped first."""
    body = _REDS_COMMENT_RE.sub(' ', text)
    return (set(_REDS_MODULE_RE.findall(body)),
            {p.rstrip('*').rstrip('.') for p in _REDS_IMPORT_RE.findall(body)} - {''})

def _import_satisfied_by(import_path: str, module: str) -> bool:
    """`import StealthRunner.StealthRunnerConfig` is satisfied by module `StealthRunner`
    (a symbol from it) and by module `StealthRunner.StealthRunnerConfig` (the module
    itself). Prefix matching covers both without needing to know which."""
    return import_path == module or import_path.startswith(module + ".")

def find_reds_module_graph(game_root: Path, manifest_mods: dict) -> dict:
    """{"provides": {module: {owner}}, "imports": {owner: {path}}}.

    Owners are manifest keys. Module identity comes from the `module` DECLARATION, never the
    folder name — the same rule that makes a case-duplicate folder a SYM_REDEFINITION."""
    owner_of = {rel.lower(): stem for stem, files in (manifest_mods or {}).items()
                for rel in files}
    provides: dict = {}
    imports: dict = {}
    for rel, text in _iter_reds(game_root):
        owner = owner_of.get(rel.lower())
        if not owner:
            continue                    # unmanaged script: cannot be attributed to a mod
        mods_, imps = parse_reds_modules(text)
        for m in mods_:
            provides.setdefault(m, set()).add(owner)
        if imps:
            imports.setdefault(owner, set()).update(imps)
    return {"provides": provides, "imports": imports}

# ──────────── One redscript module declared by several mods ────────────
# A module may legally span files, so several MODS can contribute to one module. Observed on
# this install — 6 modules with more than one contributor:
#
#   BetterLeveling   <- Better Leveling (Revamp of Custom Level Cap), Better Leveling Addon
#   Codeware.UI      <- CustomHackingSystem, StealthRunner
#   EquipmentEx      <- EquipmentEx, NPC Outfit Manager, WEAVE
#   MisterChedda     <- fishbones_projectile, jinx_grenade, relic_shower_sickness, ...
#   NightCitySkies   <- NCS_SKYWATCH, NIGHT_CITY_SKIES
#   StashSearch      <- backpackSearch.reds, stashSearch.reds
#
# Being legal is what makes it dangerous: removing one contributor deletes the classes it
# declared while the module itself survives, so the module-level check in the section above
# stays silent — `import Codeware.UI` still resolves, and the compile fails anyway on the
# types that went with StealthRunner. Same failure mode as the Scripts/ vs scripts/ incident,
# but across mods, where nothing was even looking.
#
# So the granularity of the uninstall check has to be the SYMBOL, not the module. The rule:
# a type that the departing mods declare, that nothing surviving re-declares, and that a
# surviving file still names, is an unresolved reference — which fails the whole script blob.
# Note this is a source-level reference check and not a compile, so every message names the
# file and line and it is overridable with --force-prune.
REDS_SYMBOL_MIN_LEN = 4

def _reds_strip_comments(text: str) -> str:
    """Comments must not create references: a class name in a leftover `// use FooBar` would
    otherwise block a legitimate uninstall."""
    return re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.DOTALL)

def reds_declared_symbols(game_root: Path, manifest_mods: dict) -> dict:
    """{owner: {type names}} for managed .reds. Types (class/struct/enum) only: those are
    what another file names directly, whereas a method is reached through an instance and
    matching its bare name would be noise."""
    owner_of = {rel.lower(): stem for stem, files in (manifest_mods or {}).items()
                for rel in files}
    out: dict = {}
    for rel, text in _iter_reds(game_root):
        owner = owner_of.get(rel.lower())
        if owner:
            out.setdefault(owner, set()).update(
                _REDS_TYPE_RE.findall(_reds_strip_comments(text)))
    return out

def find_shared_modules(game_root: Path, manifest_mods: dict) -> list:
    """Modules declared by more than one mod. Read-only.

    Each row: {"module", "owners": [stem], "dirs": [script dir], "symbols": {stem: n}}."""
    owner_of = {rel.lower(): stem for stem, files in (manifest_mods or {}).items()
                for rel in files}
    mods_by_module: dict = {}
    dirs_by_module: dict = {}
    syms: dict = {}
    for rel, text in _iter_reds(game_root):
        owner = owner_of.get(rel.lower())
        if not owner:
            continue                    # unmanaged: cannot be attributed to a mod
        declared, _imports = parse_reds_modules(_reds_strip_comments(text))
        for m in declared:
            mods_by_module.setdefault(m, set()).add(owner)
            parts = rel.split('/')
            dirs_by_module.setdefault(m, set()).add(
                '/'.join(parts[2:-1]) or parts[-1])   # below r6/scripts, else the file
            syms.setdefault(m, {}).setdefault(owner, set()).update(
                _REDS_TYPE_RE.findall(_reds_strip_comments(text)))
    out = []
    for m, owners in sorted(mods_by_module.items()):
        if len(owners) < 2:
            continue
        out.append({"module": m, "owners": sorted(owners),
                    "dirs": sorted(dirs_by_module.get(m, set())),
                    "symbols": {o: len(s) for o, s in sorted(syms.get(m, {}).items())}})
    return out

def report_shared_modules(game_root: Path, manifest_mods: dict,
                          names: dict | None = None) -> int:
    """Print the shared-module section plus a machine-parseable line. Read-only. Returns the
    number of modules with more than one contributing mod."""
    rows = find_shared_modules(game_root, manifest_mods)
    if rows:
        print(f"  [i] {len(rows)} redscript module(s) are declared by more than one mod. This "
              "is legal — a module may span files — but it means removing one contributor "
              "deletes types the others may use, while the module itself survives:")
        for r in rows[:10]:
            who = ", ".join(f"{mod_display_name(o, names)} ({r['symbols'].get(o, 0)} type(s))"
                            for o in r["owners"][:4])
            more = f" and {len(r['owners']) - 4} more" if len(r["owners"]) > 4 else ""
            print(f"      - module {r['module']}  <- {who}{more}")
        if len(rows) > 10:
            print(f"      ... and {len(rows) - 10} more")
        print("  Nothing to fix: this is how these mods are built. It is recorded because an "
              "uninstall has to check symbols, not just modules — which it now does.")
    print(f"[shared-modules] {len(rows)} module(s) declared by more than one mod")
    return len(rows)

def action_check_shared_modules(game_root: Path):
    mf = load_manifest(game_root)
    print("\n=== Redscript Modules With Several Contributing Mods ===")
    report_shared_modules(game_root, mf.get("mods", {}), load_mod_names())

def symbols_at_risk(game_root: Path, manifest_mods: dict, targets) -> list:
    """Types that would be deleted while a surviving file still names them. Read-only.

    Each row: {"symbol", "providers": [stem], "refs": [{"rel","owner","line"}]}.

    Unmanaged .reds count on BOTH sides here, unlike the module graph. That graph needs an
    owner to attribute a dependency to, so an unattributable file must not create one; this
    check only asks whether the compile breaks, and an unmanaged file stays on disk and gets
    compiled exactly like a managed one."""
    targets = set(targets)
    going_files = {rel.lower() for t in targets for rel in (manifest_mods or {}).get(t, [])
                   if rel.lower().endswith('.reds')}
    owner_of = {rel.lower(): stem for stem, files in (manifest_mods or {}).items()
                for rel in files}
    declared_going: dict = {}
    declared_staying: set = set()
    staying: list = []
    for rel, text in _iter_reds(game_root):
        clean = _reds_strip_comments(text)
        types = set(_REDS_TYPE_RE.findall(clean))
        if rel.lower() in going_files:
            for t in types:
                declared_going.setdefault(t, set()).add(owner_of.get(rel.lower(), rel))
        else:
            declared_staying |= types
            staying.append((rel, clean))
    at_risk = {s for s in declared_going
               if s not in declared_staying and len(s) >= REDS_SYMBOL_MIN_LEN}
    if not at_risk:
        return []
    refs: dict = {}
    for rel, clean in staying:
        for i, line in enumerate(clean.splitlines(), 1):
            for tok in set(_WORD_RE.findall(line)) & at_risk:
                refs.setdefault(tok, [])
                if not any(r["rel"] == rel for r in refs[tok]):
                    refs[tok].append({"rel": rel, "owner": owner_of.get(rel.lower(), ""),
                                      "line": i})
    return [{"symbol": s, "providers": sorted(declared_going[s]), "refs": refs[s]}
            for s in sorted(refs)]

# ─────────────── Companion/patch files and dangling references ───────────────
# Mods reference each other by path and by string, not by declared dependency, so even a
# correct uninstall leaves references behind. Observed:
#   r6/scripts/TSU_mods/NCestate.reds        — a tooltip patch keyed on "TSU_NCestate".
#       After removing NCestate it is dead code that STILL wraps WorldMapTooltipController
#       on every world-map tooltip.
#   AMM Collabs/Custom Props/boe6_gambling_system_roulette.lua — a spawnable prop pointing
#       at assets from "Gambling System - Roulette". After removal it stays listed in AMM
#       and simply fails to spawn.
#
# Both share one shape: a file in a COMPANION ROOT whose FILENAME names the mod it patches.
# That is a precise signal, unlike grepping every source file for every removed mod's name —
# which cannot beat its own false-positive rate and is deliberately not attempted here.
#
# Note the ownership question these raise, and how it is NOT answered. The fix direction
# said to model a companion as belonging to the mod it patches. Rewriting manifest ownership
# would be wrong: the file genuinely came out of TSU's archive, so --check-attribution would
# then correctly report it as not tracing to its owner, and a re-deploy of TSU would rewrite
# it anyway. Ownership stays with whoever shipped the file; the companion LINK is separate
# data, consulted at uninstall time.
COMPANION_ROOTS = {
    "r6/scripts/TSU_mods":
        "Tooltip/Stats UI per-mod patch",
    "bin/x64/plugins/cyber_engine_tweaks/mods/AppearanceMenuMod/Collabs/Custom Props":
        "AMM spawnable prop contributed for another mod",
    "bin/x64/plugins/cyber_engine_tweaks/mods/AppearanceMenuMod/Collabs/Custom Entities":
        "AMM custom entity contributed for another mod",
    "bin/x64/plugins/cyber_engine_tweaks/mods/AppearanceMenuMod/Collabs/Custom Appearances":
        "AMM custom appearance contributed for another mod",
}
COMPANION_ROOTS_NAME = "companion_roots.json"
# Below this length a name fragment matches by coincidence rather than by identity.
COMPANION_MIN_MATCH = 5

def load_companion_roots() -> dict:
    """User-maintained {root: label} merged over the built-in table, in CONFIG_DIR. Every
    UI framework grows this pattern and a table only I can edit is stale within a week —
    same reasoning as the domain overrides."""
    path = CONFIG_DIR / COMPANION_ROOTS_NAME
    out = dict(COMPANION_ROOTS)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        out.update({str(k).replace('\\', '/').strip('/'): str(v) for k, v in data.items()})
    return out

def _compact(s: str) -> str:
    """Fold to comparable identity: lowercase, letters and digits only. "Gambling System -
    Roulette" and "gambling_system_roulette" are the same mod written two ways."""
    return re.sub(r'[^a-z0-9]+', '', (s or "").lower())

def installed_identifiers(game_root: Path, mods: dict, names: dict | None = None,
                          roots: dict | None = None) -> set:
    """Every compacted string that identifies an installed mod: its manifest key, its Nexus
    name, its CET folder, its r6/scripts folder and its archive basenames.

    A companion is called dangling only when it matches NONE of these. Erring toward "still
    installed" is the safe direction: a missed dangling file costs a stale patch, while a
    false accusation tells the user to delete a working one.

    Files inside a companion root are EXCLUDED. A companion is named after the mod it
    patches, so counting its basename here makes every companion prove its own target is
    installed — the detector then reports nothing, ever, on any install."""
    roots = roots if roots is not None else load_companion_roots()
    root_prefixes = tuple(r.lower() + "/" for r in roots)
    out = set()
    for stem, files in (mods or {}).items():
        out.add(_compact(stem))
        out.add(_compact(mod_display_name(stem, names)))
        out.add(_compact(stem_display_name(stem)))
        for rel in files:
            low = rel.replace('\\', '/')
            if low.lower().startswith(root_prefixes):
                continue
            parts = low.split('/')
            out.add(_compact(parts[-1].rsplit('.', 1)[0]))
            for root_rel in (CET_MODS_REL.as_posix(), "r6/scripts", "red4ext/plugins"):
                if low.lower().startswith(root_rel.lower() + "/"):
                    tail = low[len(root_rel) + 1:].split('/')
                    if len(tail) > 1:
                        out.add(_compact(tail[0]))
    # Folders on disk too: a hand-installed mod is still installed, and calling its patch
    # dangling would be a false accusation.
    for folder in installed_cet_mods(game_root):
        out.add(_compact(folder))
    return {o for o in out if len(o) >= COMPANION_MIN_MATCH}

def _identifies(target: str, identifiers: set) -> bool:
    """Does this companion's target name refer to something installed? Substring in either
    direction, because the filename may carry an author prefix the mod name does not
    ("boe6_gambling_system_roulette" vs "Gambling System - Roulette") or be an abbreviation
    the name spells out."""
    t = _compact(target)
    if len(t) < COMPANION_MIN_MATCH:
        return True                      # too short to judge — never accuse
    return any(t == i or t in i or i in t for i in identifiers)

def find_companions(game_root: Path, mods: dict, names: dict | None = None,
                    roots: dict | None = None) -> list:
    """Every file in a companion root, with the mod it patches and whether that mod is still
    installed. Read-only.

    Each entry: {"rel", "root", "label", "target", "owner", "dangling"}. `owner` is whoever
    SHIPPED the file (TSU, AMM), which is not the mod it patches."""
    roots = roots if roots is not None else load_companion_roots()
    owner_by_path = {r.lower(): stem for stem, files in (mods or {}).items() for r in files}
    identifiers = installed_identifiers(game_root, mods, names, roots)
    out = []
    for root_rel, label in sorted(roots.items()):
        root = game_root / root_rel
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in HEALTH_SKIP_DIRS]
            here = Path(dirpath)
            for fn in sorted(filenames):
                if Path(fn).suffix.lower() not in ('.reds', '.lua', '.json', '.yaml', '.yml'):
                    continue
                rel = (here / fn).relative_to(game_root).as_posix()
                target = Path(fn).stem
                out.append({"rel": rel, "root": root_rel, "label": label, "target": target,
                            "owner": owner_by_path.get(rel.lower(), ""),
                            "dangling": not _identifies(target, identifiers)})
    return out

def find_dangling_companions(game_root: Path, mods: dict, names: dict | None = None) -> list:
    return [c for c in find_companions(game_root, mods, names) if c["dangling"]]

def companions_orphaned_by(game_root: Path, mods: dict, targets, names: dict | None = None) -> list:
    """Companions that are NOT dangling today but would be once `targets` are removed. This
    is the one that matters: it fires before the removal, not after it."""
    targets = set(targets)
    survivors = {s: f for s, f in (mods or {}).items() if s not in targets}
    identifiers = installed_identifiers(game_root, survivors, names)
    # Also drop the CET folders of the mods on their way out: installed_identifiers reads
    # those from DISK, where they are still present until the prune actually runs.
    going = {_compact(f) for f in installed_cet_mods(game_root)
             if cet_mod_owner(f, mods) in targets}
    identifiers -= going
    out = []
    for c in find_companions(game_root, mods, names):
        if c["dangling"] or c["owner"] in targets:
            continue                      # already dead, or leaving with its own mod anyway
        if not _identifies(c["target"], identifiers):
            out.append(c)
    return out

def report_dangling(game_root: Path, mods: dict, names: dict | None = None) -> int:
    """Print the dangling-companion section plus a machine-parseable line. Read-only.
    Returns the number of dangling companion files."""
    found = find_dangling_companions(game_root, mods, names)
    if found:
        print(f"  [i] {len(found)} companion/patch file(s) target a mod that is no longer "
              "installed. Each one is dead code shipped by a mod that IS installed, so it "
              "keeps running — a stale tooltip patch still wraps every tooltip:")
        for c in found[:12]:
            who = f"shipped by {mod_display_name(c['owner'], names)}" if c["owner"] \
                else "hand-installed"
            print(f"      - {c['rel']}")
            print(f"          patches \"{c['target']}\" (not installed); {who}")
        if len(found) > 12:
            print(f"      ... and {len(found) - 12} more")
        print("  Remove them with --prune-dangling. They come back if you re-deploy the mod "
              "that ships them, which is correct — that archive really does contain them.")
    print(f"[dangling] {len(found)} companion file(s) targeting a mod that is not installed")
    return len(found)

def action_check_dangling(game_root: Path):
    mf = load_manifest(game_root)
    print("\n=== Dangling Companion Files (patches for mods that are gone) ===")
    report_dangling(game_root, mf.get("mods", {}), load_mod_names())

def action_prune_dangling(game_root: Path, dry_run: bool, purge: bool = False):
    manifest = load_manifest(game_root)
    mods = manifest.get("mods", {})
    names = load_mod_names()
    found = find_dangling_companions(game_root, mods, names)
    print("\n=== Prune Dangling Companion Files ===")
    if not found:
        print("  Nothing to do — every companion file targets an installed mod.")
        return
    print(f"  {len(found)} dangling companion file(s):")
    for c in found:
        who = f"shipped by {mod_display_name(c['owner'], names)}" if c["owner"] \
            else "hand-installed"
        print(f"    - {c['rel']}  (patches \"{c['target']}\", not installed; {who})")
    if dry_run:
        print("  [dry-run] Nothing removed.")
        return
    # Grouped by owner so each entry keeps the rest of its files: this is a PARTIAL removal
    # of a mod that stays installed, which is exactly what only_paths is for.
    by_owner: dict = {}
    for c in found:
        by_owner.setdefault(c["owner"], set()).add(c["rel"].lower())
    removed = 0
    for owner, paths in sorted(by_owner.items()):
        if owner:
            _remove_mods(game_root, mods, [owner], dry_run=False, purge=purge,
                         only_paths=paths)
            removed += len(paths)
        else:
            for c in found:                          # unowned: no manifest entry to update
                if not c["owner"]:
                    with contextlib.suppress(OSError):
                        (game_root / c["rel"]).unlink()
                        removed += 1
    print(f"  Removed {removed} dangling companion file(s)"
          + ("" if purge else " (moved to .mod_trash — recoverable)") + ".")

def uninstall_impact(game_root: Path, manifest_mods: dict, targets, scopes=None,
                     names: dict | None = None) -> dict:
    """What breaks if `targets` are removed.
    Returns {"blockers": [...], "warnings": [...], "companions": [...]}.

    A target removed with scope 'lua' keeps its r6/scripts, so it cannot orphan an import and
    is only assessed on the Lua graph. That asymmetry is the whole point of partial
    uninstall."""
    targets = list(targets)
    scopes = scopes or {}
    full = {t for t in targets if scopes.get(t, "all") != "lua"}
    graph = find_reds_module_graph(game_root, manifest_mods)
    blockers = []
    for module, providers in sorted(graph["provides"].items()):
        going = providers & full
        if not going or (providers - full):
            continue                    # not provided by a target, or still provided after
        importers = sorted(o for o, paths in graph["imports"].items()
                           if o not in set(targets)
                           and any(_import_satisfied_by(p, module) for p in paths))
        if importers:
            blockers.append({"module": module, "providers": sorted(going),
                             "importers": importers})
    # Lua side: a CET folder some surviving mod looks up by name.
    consumers: dict = {}
    for folder, _rel, text in _iter_cet_lua(game_root):
        for want in parse_lua_getmod(text):
            consumers.setdefault(want.lower(), set()).add(folder)
    warnings = []
    going_folders = {}
    for folder in installed_cet_mods(game_root):
        owner = cet_mod_owner(folder, manifest_mods)
        if owner in set(targets):
            going_folders[folder] = owner
    for folder, owner in sorted(going_folders.items()):
        users = sorted(c for c in consumers.get(folder.lower(), set())
                       if cet_mod_owner(c, manifest_mods) not in set(targets)
                       and c.lower() != folder.lower())
        if users:
            warnings.append({"folder": folder, "provider": owner, "consumers": users})
    # Third graph, and the only one that survives a CORRECT uninstall: patch files shipped by
    # a mod that stays, targeting a mod that goes. Reported before the removal rather than
    # discovered afterwards. Never a blocker — the surviving mod still loads.
    companions = companions_orphaned_by(game_root, manifest_mods, full, names)
    # Symbol level. The module check above only fires when EVERY provider of a module is
    # going; when several mods contribute to one module, the module survives the removal and
    # the compile still breaks on the types that left with it. That case is invisible above.
    orphan_symbols = symbols_at_risk(game_root, manifest_mods, full) if full else []
    return {"blockers": blockers, "warnings": warnings, "companions": companions,
            "symbols": orphan_symbols}

def report_uninstall_impact(impact: dict, names: dict | None = None) -> bool:
    """Print the impact. Returns True when there is a blocker (an orphaned redscript import)."""
    for w in impact["warnings"]:
        users = ", ".join(w["consumers"][:4]) + ("..." if len(w["consumers"]) > 4 else "")
        print(f"  [i] {w['folder']} is looked up via GetMod() by: {users}")
        print("      Those integrations will silently stop working — nil is indistinguishable "
              "from 'not installed'. Not a blocker: only those mods are affected.")
    comps = impact.get("companions") or []
    if comps:
        print(f"  [i] {len(comps)} companion/patch file(s) would be left targeting a mod that "
              "is gone. They belong to mods that STAY installed, so they keep running as "
              "dead code:")
        for c in comps[:6]:
            who = f"shipped by {mod_display_name(c['owner'], names)}" if c["owner"] \
                else "hand-installed"
            print(f"      - {c['rel']}  ({c['label']}; {who})")
        if len(comps) > 6:
            print(f"      ... and {len(comps) - 6} more")
        print("      Clear them afterwards with --prune-dangling. Not a blocker: nothing "
              "fails to load, the patch just does nothing.")
    syms = impact.get("symbols") or []
    if syms:
        print(f"\n  [!] REFUSED — {len(syms)} redscript type(s) would be deleted while "
              "surviving files still name them:")
        for s in syms[:8]:
            provs = ", ".join(mod_display_name(p, names) for p in s["providers"][:3])
            print(f"      - {s['symbol']}  (declared by {provs})")
            for r in s["refs"][:3]:
                who = f" [{mod_display_name(r['owner'], names)}]" if r["owner"] else \
                    " [unmanaged]"
                print(f"          used at {r['rel']}:{r['line']}{who}")
            if len(s["refs"]) > 3:
                print(f"          ... and {len(s['refs']) - 3} more file(s)")
        if len(syms) > 8:
            print(f"      ... and {len(syms) - 8} more type(s)")
        print("      These mods share a redscript MODULE, so the module itself survives and "
              "the import still resolves — but the types do not, and an unresolved reference "
              "fails the whole script blob exactly like a missing import.")
        print("      This is a source-level reference check, not a compile: the files and "
              "lines above are the whole evidence. Override with --force-prune.")
    if impact["blockers"]:
        print(f"\n  [!] REFUSED — {len(impact['blockers'])} redscript module(s) are imported "
              "by mods that would remain installed:")
        for b in impact["blockers"]:
            imps = ", ".join(mod_display_name(i, names) for i in b["importers"][:4])
            print(f"      - module {b['module']}  (from {mod_display_name(b['providers'][0], names)})")
            print(f"          imported by: {imps}"
                  + (f" and {len(b['importers']) - 4} more" if len(b["importers"]) > 4 else ""))
        print("      Removing it means those imports resolve to nothing, redscript "
              "compilation FAILS, and EVERY redscript mod in the install stops loading — not "
              "just the ones listed. This is why it is a refusal and not a warning.")
        print("      Options: keep it; or remove the importers too; or drop only its CET Lua "
              "half with --disable-lua <name>, which leaves r6/scripts in place.")
    return bool(impact["blockers"] or syms)

def report_framework_health(manifest_mods: dict) -> int:
    """Report stale duplicate framework entries in the manifest (the same framework mod-id
    installed at more than one version — the 'old build still deployed' cause). Read-only;
    prints a section plus a machine-parseable line. Returns the number of frameworks with
    duplicates."""
    dupes = {mid: e for mid, e in installed_frameworks(manifest_mods).items() if len(e) > 1}
    if dupes:
        print("  [!] Framework installed at more than one version (older build shadows the "
              "newest and breaks dependents):")
        for mid, entries in sorted(dupes.items()):
            name = FRAMEWORK_MOD_IDS[mid]
            newest = entries[0]["ver"]
            older = ", ".join(e["ver"] for e in entries[1:])
            print(f"      - {name}: keep v{newest}, drop stale v{older}")
        print("  Fix: run --fix-frameworks (keeps the newest, archives older builds, "
              "reversible).")
    print(f"[frameworks] {len(dupes)} framework(s) with duplicate/older builds")
    return len(dupes)

def report_health(game_root: Path, manifest_mods: dict, manifest: dict | None = None,
                  downloads: Path | None = None) -> int:
    """Print the health section and machine-parseable summary lines. Read-only; safe to run
    after every deploy (including dry runs). Returns collisions + framework-dupes + version
    dupes + a broken redscript compile."""
    collisions = find_case_collisions(game_root, manifest_mods)
    reds = [c for c in collisions if c["is_redscript"]]
    print("\n=== Health Check (installed mods) ===")
    if not collisions:
        print("  [✓] No case-duplicate folders — installed mod tree is coherent.")
        print("[health] 0 case-duplicate folder(s); 0 auto-fixable; 0 in redscript")
    else:
        print(f"  [!] {len(collisions)} case-duplicate folder(s) detected"
              + (f"; {len(reds)} in the redscript tree — these abort redscript compilation, "
                 "so ALL script mods fail to load" if reds else "") + ":")
        planned_word = {"owner-version": "auto-fix (newest version owns it)",
                        "merge": "auto-fix (merge)",
                        "redscript": "needs --apply-heuristics (redscript coherence)",
                        "framework": "needs --apply-heuristics (newer build by mtime)",
                        "keep-owned": "needs --apply-heuristics (keep managed copy)",
                        "skip": "MANUAL"}
        fixable = guessy = 0
        for c in collisions:
            keep, _drops, mode, reason = classify_collision(c)
            if mode in DECISIVE_MODES:
                fixable += 1
            elif mode != "skip":
                guessy += 1
            tag = ("framework bundle — two builds" if c["is_framework_bundle"] else
                   "redscript — SYM_REDEFINITION" if c["is_redscript"] else "duplicate load")
            print(f"  - {c['parent_rel']}/  [{tag}]  → {planned_word[mode]}")
            print(f"        {reason}")
            for v in c["variants"]:
                keep_mark = "  (keep)" if keep is not None and v is keep else ""
                print(f"        {v['name']}/  ({v['files']} file(s); {_owner_label(v)}){keep_mark}")
        if fixable:
            print(f"  Fix: run --fix-duplicates to resolve the {fixable} auto-fixable case(s) "
                  "(stray/older copies are moved to a reversible backup, never deleted).")
        if guessy:
            print(f"  {guessy} further case(s) have no version order to settle them; "
                  "--fix-duplicates leaves those alone unless you add --apply-heuristics.")
        print(f"[health] {len(collisions)} case-duplicate folder(s); {fixable} auto-fixable; "
              f"{guessy} heuristic-only; {len(reds)} in redscript")
    fw_dupes = report_framework_health(manifest_mods)
    ver_dupes = report_version_health(manifest_mods)
    reds_broken = report_redscript_health(game_root, manifest)
    # Cheap half of the attribution check (a directory listing, no extraction). The full
    # audit costs one bsdtar per mod and lives in --check-attribution.
    orphans = find_orphan_entries(manifest_mods, downloads) if downloads else []
    if orphans:
        print(f"  [i] {len(orphans)} installed mod(s) have no archive in Downloads. Usually "
              "just a cleaned-up download, but a mod whose files also do not trace back to "
              "an archive is a stale entry reported as installed:")
        for stem in orphans[:8]:
            print(f"      - {stem}")
        if len(orphans) > 8:
            print(f"      ... and {len(orphans) - 8} more")
        print("  Check with --check-attribution.")
    if downloads:
        print(f"[orphans] {len(orphans)} installed mod(s) with no archive in Downloads")
    # The mirror image of an orphan entry: an orphan FILE. Deliberately not added to the
    # returned problem count — a hand-installed mod is a thing the user chose to do, not
    # a break — but it is the reason a mod survives --remove and reappears after a
    # --fix-duplicates, so it has to be visible.
    report_unmanaged(game_root, manifest_mods)
    # The other direction of the same drift: a path the manifest owns that is no longer on
    # disk. Advisory for the same reason — a hand-deletion is a thing the user chose to do —
    # but it makes every footprint and removal plan built from the manifest overstate what
    # is installed, which is what made removal guesswork in the first place.
    report_missing_files(game_root, manifest_mods, load_mod_names())
    # Also advisory (see WHY_ADVISORY): two mods in one domain is sometimes correct, and
    # which of them should win is a preference, not a defect the tool can settle.
    report_conflicts(game_root, manifest_mods)
    # Legal, unfixable, and recorded because it changes what an uninstall must check.
    report_shared_modules(game_root, manifest_mods, load_mod_names())
    # Survives a CORRECT uninstall, so it is the one finding here that no other check can
    # reach: a patch file for a mod that is gone, shipped by a mod that stays.
    report_dangling(game_root, manifest_mods, load_mod_names())
    # A confirmed break (the companion is installed under another name) IS a defect rather
    # than a preference, but it is a defect inside a mod's own Lua that this tool must not
    # edit, so it stays out of the returned count alongside the other advisory sections.
    report_cet_links(game_root, manifest_mods)
    # Crash-class, but the fix is an edit inside a mod's own Lua that this tool must not
    # make, so like the other advisory sections it reports and stops.
    report_locale(game_root)
    # Capacity, not correctness: nothing is wrong yet, but the wall is silent when you hit it.
    report_fd_headroom(game_root)
    # A disabled mod whose files are still on disk is still being loaded by the game — an
    # uninstall that was started and never finished.
    report_disabled(game_root, manifest_mods)
    # A pinned file that no longer matches means something got past the protection, or the
    # user edited it again — either way they should know before they next look for the fix.
    report_pins(game_root)
    # Measured, not inferred: this is what halved the frame rate. Reported as scale only —
    # see the rule at the top of that section.
    report_perf(game_root, manifest_mods)
    # A perf mod whose shipped budget is sized for a much smaller install. Reported right
    # after the per-frame section on purpose: its own output has to say that fixing it buys
    # correct behaviour and NOT frames, and that only reads right next to the finding that
    # does cost frames.
    report_scale(game_root, manifest_mods, load_mod_names())
    return len(collisions) + fw_dupes + ver_dupes + reds_broken

def _reds_types_defined(folder: Path) -> set:
    types = set()
    for f in folder.rglob("*.reds"):
        try: text = f.read_text(encoding='utf-8', errors='ignore')
        except OSError: continue
        types.update(_REDS_TYPE_RE.findall(text))
    return types

def _reds_tokens(files) -> set:
    toks = set()
    for f in files:
        try: text = f.read_text(encoding='utf-8', errors='ignore')
        except OSError: continue
        toks.update(_WORD_RE.findall(text))
    return toks

def decide_redscript_duplicate(collision: dict):
    """Pick which case-variant of a redscript collision to KEEP, by the coherence test
    the manual fix used: the rest of the mod references the identifiers of the real
    version and none of the stray one's. Returns (keep, [drop...], reason); keep is None
    when it can't be decided safely (reported for manual resolution, never guessed away)."""
    variants = collision["variants"]
    if len(variants) != 2:
        return None, [], "more than two case-variants — resolve manually"
    parent = collision["parent_path"]
    collision_dirs = {v["path"].resolve() for v in variants}
    rest_files = [f for f in parent.rglob("*.reds")
                  if not any(cd == f.resolve() or cd in f.resolve().parents
                             for cd in collision_dirs)]
    rest_tokens = _reds_tokens(rest_files)
    a, b = variants
    ta, tb = _reds_types_defined(a["path"]), _reds_types_defined(b["path"])
    uniq_a, uniq_b = ta - tb, tb - ta
    score_a, score_b = len(uniq_a & rest_tokens), len(uniq_b & rest_tokens)
    if score_a != score_b:
        keep, drop = (a, b) if score_a > score_b else (b, a)
        return keep, [drop], (f"the rest of the mod references {max(score_a, score_b)} "
                              f"identifier(s) unique to '{keep['name']}' and "
                              f"{min(score_a, score_b)} unique to '{drop['name']}'")
    if a["files"] != b["files"]:               # inconclusive coherence → keep the fuller copy
        keep, drop = (a, b) if a["files"] > b["files"] else (b, a)
        return keep, [drop], (f"identifier coherence inconclusive; kept the larger copy "
                              f"('{keep['name']}', {keep['files']} vs {drop['files']} files)")
    return None, [], "identifier coherence and file count both inconclusive — resolve manually"

def _prune_manifest_under(mods: dict, folder_rel: str):
    """Drop manifest entries pointing inside a moved folder; remove any mod left empty."""
    prefix = folder_rel.rstrip('/') + '/'
    for mod in list(mods):
        kept = [f for f in mods[mod] if not f.startswith(prefix)]
        if len(kept) != len(mods[mod]):
            mods[mod] = kept
        if not mods[mod]:
            mods.pop(mod, None)

def _repoint_manifest(mods: dict, drop_rel: str, keep_rel: str):
    """After merging a case-variant folder into another, rewrite manifest paths from the
    dropped casing to the kept one so ownership stays accurate."""
    dp, kp = drop_rel.rstrip('/') + '/', keep_rel.rstrip('/') + '/'
    for mod in mods:
        seen, out = set(), []
        for f in mods[mod]:
            nf = kp + f[len(dp):] if f.startswith(dp) else f
            if nf not in seen:
                seen.add(nf); out.append(nf)
        mods[mod] = out

def _rel_file_set(folder: Path) -> set:
    out = set()
    for root, _dirs, files in os.walk(folder):
        rp = Path(root)
        for f in files:
            out.add((rp / f).relative_to(folder).as_posix())
    return out

def _same_bytes(p1: Path, p2: Path) -> bool:
    try:
        if p1.stat().st_size != p2.stat().st_size:
            return False
    except OSError:
        return False
    m1 = compute_md5(p1)
    return bool(m1) and m1 == compute_md5(p2)

def _newest_mtime(folder: Path) -> float:
    best = 0.0
    for f in folder.rglob("*"):
        if f.is_file():
            try: best = max(best, f.stat().st_mtime)
            except OSError: pass
    return best

def decide_framework_bundle_duplicate(collision: dict):
    """Pick which case-variant of a framework's bundled folder to KEEP.

    Frameworks ship their redscript API and data under red4ext/plugins/<Name>/, and a
    case-split there means two BUILDS of the framework are sitting side by side. The newest
    build is always the right one — an older API is exactly what breaks dependents.

    Reached only after decide_by_owner_version() declines, i.e. when one of the two builds
    has no manifest entry to date it. mtime is then all that is left, and it is weak: it
    survives a copy, an extract or a stray touch, so this mode is a heuristic and is not
    applied without --apply-heuristics."""
    variants = collision["variants"]
    if len(variants) != 2:
        return None, [], "more than two case-variants — resolve manually"
    a, b = variants
    ma, mb = _newest_mtime(a["path"]), _newest_mtime(b["path"])
    if ma == mb:
        return None, [], ("both case-variants are the same build age — resolve manually")
    keep, drop = (a, b) if ma > mb else (b, a)
    kts = time.strftime('%Y-%m-%d', time.localtime(max(ma, mb)))
    dts = time.strftime('%Y-%m-%d', time.localtime(min(ma, mb)))
    return keep, [drop], (f"two builds of a framework bundle — keep the newer '{keep['name']}/' "
                          f"({kts}), archive the older '{drop['name']}/' ({dts})")

def decide_by_owner_version(collision: dict):
    """Keep the case-variant owned by the NEWEST version of the mod that owns them.

    This is the one signal on a case-split that is evidence rather than inference: a split
    that survives the one-version-per-mod policy is two versions of a single mod, and each
    manifest entry records the casing its own build wrote. Version order comes from the
    Nexus upload epoch, which — unlike folder mtime — cannot be perturbed by a copy, an
    extract or a later touch.

    Requires EVERY variant to be owned. An unmanaged folder is routinely the copy the
    compiler actually reads: Codeware's correct Scripts/ (with `class JobQueue`) had no
    manifest owner while the stale scripts/ did, so reading ownership as 'the managed one
    is the right one' is what archived the working API and broke every dependent mod.
    Absence of an owner is absence of evidence, and returns no decision."""
    variants = collision["variants"]
    if len(variants) != 2:
        return None, [], ""
    best = []
    for v in variants:
        if not v["owners"]:
            return None, [], ""              # unmanaged: ownership proves nothing here
        best.append(max(((framework_version_key(o), o) for o in v["owners"])))
    idents = {mod_identity(o) for _, o in best}
    if len(idents) != 1 or None in idents:
        return None, [], ""                  # different mods (or unparseable) — not versions
    (ea, oa), (eb, ob) = best
    if ea == eb:
        return None, [], ""                  # same entry owns both casings — no version signal
    (keep, kv), (drop, dv) = (((variants[0], oa), (variants[1], ob)) if ea > eb
                              else ((variants[1], ob), (variants[0], oa)))
    return keep, [drop], (f"two versions of one mod — '{keep['name']}/' belongs to the newest "
                          f"(v{framework_version_str(kv)}), '{drop['name']}/' to the superseded "
                          f"v{framework_version_str(dv)}")

# Modes whose decision follows from evidence (an explicit version order, or a merge that
# loses nothing). Everything else is a guess about compile input, and guessing there is what
# stripped `class JobQueue` from the compile path — those need --apply-heuristics.
DECISIVE_MODES = {"owner-version", "merge"}

def classify_collision(c: dict):
    """Decide how a case-collision can be resolved. Returns (keep, [drop...], mode, reason):
      owner-version — two versions of one mod; keep the casing the newest version owns.
                      Decisive, and tried first: it is the only rule backed by evidence.
      redscript  — two versions in the redscript tree; keep the one the rest of the mod
                   references (SYM_REDEFINITION source). Heuristic.
      framework  — two builds of a framework's bundled folder; keep the newer build by
                   file mtime. Heuristic, and a weak one — mtime survives a copy.
      merge      — non-conflicting content (disjoint or byte-identical overlaps); fold the
                   smaller casing into the larger, losslessly. Decisive.
      keep-owned — conflicting content where one casing is an unmanaged leftover (no manifest
                   owner) shadowing the managed copy; keep the managed one. Never applied to
                   compile input — see below. Heuristic.
      skip       — can't be decided safely; reported for manual resolution, never guessed."""
    variants = c["variants"]
    keep, drops, reason = decide_by_owner_version(c)
    if keep is not None:
        return keep, drops, "owner-version", reason
    if c["is_framework_bundle"]:
        keep, drops, reason = decide_framework_bundle_duplicate(c)
        return (keep, drops, "framework", reason) if keep is not None else (None, [], "skip", reason)
    if c["is_redscript"]:
        keep, drops, reason = decide_redscript_duplicate(c)
        return (keep, drops, "redscript", reason) if keep is not None else (None, [], "skip", reason)
    if len(variants) != 2:
        return None, [], "skip", "more than two case-variants — resolve manually"
    a, b = variants
    set_a, set_b = _rel_file_set(a["path"]), _rel_file_set(b["path"])
    conflicting = any(not _same_bytes(a["path"] / r, b["path"] / r) for r in (set_a & set_b))
    if not conflicting:
        keep, drop = (a, b) if len(set_a) >= len(set_b) else (b, a)
        return keep, [drop], "merge", (f"non-conflicting content — fold '{drop['name']}/' "
                                       f"({drop['files']} file(s)) into '{keep['name']}/'")
    # Manifest ownership is NOT evidence about compile input. Both case-variants of a mod's
    # scripts can hold .reds the compiler reads, and the manifest records only the casing the
    # deployer wrote — so an 'unmanaged' folder is routinely the one redscript actually reads.
    # Archiving it on that basis is what removed Codeware's bundled API (class JobQueue) and
    # broke every dependent mod with UNRESOLVED_TYPE. Only non-script folders may use this.
    if not (c["has_reds"] or _contains_reds(a["path"]) or _contains_reds(b["path"])):
        a_orphan, b_orphan = not a["owners"], not b["owners"]
        if a_orphan != b_orphan:
            keep, drop = (a, b) if b_orphan else (b, a)
            return keep, [drop], "keep-owned", (f"'{drop['name']}/' is an unmanaged leftover "
                                                f"(no manifest owner) shadowing the managed "
                                                f"'{keep['name']}/' — keep the managed copy")
        return None, [], "skip", ("both case-variants are managed but their files differ "
                                  "(two versions) — resolve manually")
    return None, [], "skip", ("case-variants hold differing redscript sources the compiler "
                              "reads — removing either can strip types the compile needs; "
                              "resolve manually")

def action_fix_duplicates(game_root: Path, dry_run: bool, apply_heuristics: bool = False):
    """Resolve case-duplicate folders. By default only DECISIVE cases are applied — those
    settled by an explicit version order or by a lossless merge. Cases that rest on a guess
    about which copy the compiler wants (identifier coherence, folder mtime, manifest
    ownership) are reported and left alone unless apply_heuristics is set: applying such a
    guess unattended is what archived Codeware's live API and broke the redscript compile."""
    manifest = load_manifest(game_root)
    mods = manifest.get("mods", {})
    collisions = find_case_collisions(game_root, mods)
    print("\n=== Fix Case-Duplicate Folders ===")
    if not collisions:
        print("  [✓] Nothing to fix — no case-duplicate folders found.")
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = game_root / DUP_BACKUP_DIRNAME / ts
    manifest_dirty = False
    moved_total = merged_total = resolved = skipped = deferred = 0
    for c in collisions:
        keep, drop_list, mode, reason = classify_collision(c)
        print(f"\n  {c['parent_rel']}/  ({' vs '.join(v['name'] + '/' for v in c['variants'])})")
        if mode == "skip":
            skipped += 1
            print(f"      [?] Left for manual resolution — {reason}:")
            for v in c["variants"]:
                print(f"          {v['name']}/ ({v['files']} file(s); {_owner_label(v)})")
            continue
        if mode not in DECISIVE_MODES and not apply_heuristics:
            deferred += 1
            assert keep is not None
            print(f"      [?] Not applied — this rests on a guess, not on a version order: "
                  f"{reason}.")
            print(f"          Would keep '{keep['name']}/'. Re-run with --apply-heuristics "
                  "to act on it, and verify the game's redscript log afterwards.")
            continue
        assert keep is not None                    # every non-skip mode yields a kept variant
        print(f"      [i] {reason}. Keep '{keep['name']}/'.")
        for drop in drop_list:
            label = c["parent_rel"].replace("/", "_") + f"__{drop['name']}_DUPLICATE"
            dest = backup_root / label
            rel_backup = f"{DUP_BACKUP_DIRNAME}/{ts}/{label}"
            merged_here = 0
            if mode == "merge":
                # fold the smaller casing's files into the kept one, then archive the folder
                for rel in sorted(_rel_file_set(drop["path"])):
                    tgt = keep["path"] / rel
                    if tgt.exists():
                        continue
                    merged_here += 1
                    if not dry_run:
                        tgt.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(drop["path"] / rel, tgt)
            if dry_run:
                extra = f", merging {merged_here} new file(s) into '{keep['name']}/'" if mode == "merge" else ""
                print(f"      [~] Would move '{drop['rel']}/' -> {rel_backup}/ "
                      f"({drop['files']} file(s), reversible){extra}")
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(drop["path"]), str(dest))
            except OSError as e:
                print(f"      [!] Could not move {drop['rel']}: {e}")
                continue
            moved_total += drop["files"]
            merged_total += merged_here
            if mode in ("merge", "framework"):
                # Ownership follows the surviving casing — the manifest records the casing the
                # deployer wrote, which is not necessarily the one that stays on disk.
                _repoint_manifest(mods, drop["rel"], keep["rel"])
                if mode == "merge":
                    print(f"      [✓] Merged {merged_here} new file(s) into '{keep['name']}/' and "
                          f"archived '{drop['name']}/' -> {rel_backup}/ (reversible)")
                else:
                    print(f"      [✓] Kept newer build '{keep['name']}/' and archived older "
                          f"'{drop['name']}/' -> {rel_backup}/ ({drop['files']} file(s), reversible)")
            else:
                # owner-version / redscript: the dropped casing's files are gone, so the
                # entry that owned them stops claiming them. Never repoint here — that would
                # hand the surviving casing a second owner and recreate the dual-ownership
                # this whole policy exists to remove.
                _prune_manifest_under(mods, drop["rel"])
                print(f"      [✓] Moved '{drop['rel']}/' -> {rel_backup}/ "
                      f"({drop['files']} file(s), reversible)")
            manifest_dirty = True
        resolved += 1
    if not dry_run and manifest_dirty:
        save_manifest(game_root, mods)
    verb = "Would resolve" if dry_run else "Resolved"
    print(f"\n{verb} {resolved} case-duplicate(s)"
          + (f", {deferred} needing --apply-heuristics" if deferred else "")
          + (f", {skipped} left for manual review" if skipped else "")
          + ("." if dry_run else
             f"; {moved_total} file(s) archived to {DUP_BACKUP_DIRNAME}/{ts}/ (restore to undo)."))
    if deferred:
        print(f"  {deferred} case(s) were NOT applied: no version order settles them, so the "
              "choice would be a guess about what the compiler reads. Review them above, or "
              "re-run with --apply-heuristics and check r6/logs/redscript_rCURRENT.log after "
              "the next launch.")
    if not dry_run and resolved:
        print("  Launch the game to regenerate the redscript cache. To undo any change, "
              f"move a folder from {DUP_BACKUP_DIRNAME}/{ts}/ back to its original location.")

# ── Mod Scanner compatibility entry point ───────────────────────────────────
def action_scan(game_root: Path, dry_run: bool, cluster: bool = True,
                scan_paths=None, eps: float = 0.65, min_samples: int = 1,
                jobs: int = 4, state_path: Path | None = None):
    """Compatibility wrapper for the dedicated read-only mod scanner.

    Historically ``--scan`` inferred groups with a union-find threshold and then wrote
    those guesses directly into .mod_manifest.json.  Scanning is now intentionally
    separate from deployment ownership: it uses TF-IDF + DBSCAN, computes SHA-256 for
    every file, and stores integrity state outside the game directory.
    """
    from . import mod_scanner

    result = mod_scanner.scan_mods(
        game_root,
        scan_paths,
        eps=eps,
        min_samples=min_samples,
        jobs=jobs,
        state_path=state_path,
        save_snapshot=not dry_run,
        clustering=cluster,
        progress=mod_scanner._cli_progress,
    )
    mod_scanner.print_report(result)
    if dry_run:
        print("[~] Dry run: integrity snapshot was not updated.")
    return result

# ── Deployer ─────────────────────────────────────────────────────────────────
class Deployer:
    def __init__(self, game_root: Path, dry_run: bool, verbose: bool, manifest_mods: dict = None,
                 install_resolutions: dict | None = None, default_existing_action: str = "overwrite"):
        self.game_root = game_root
        self.dry_run = dry_run
        self.verbose = verbose
        self.writers: dict[str, list[str]] = {}
        self.deployed_mods = 0
        self.empty_mods = 0
        self.manifest_mods = manifest_mods or {}
        self.requirements: dict[str, list] = {}   # mod_name -> detected requirement names
        self.batch_names: set = set()             # every mod/archive name in this run
        self._case_cache: dict[str, str] = {}     # lower(dir path) -> casing already on disk
        # User intent about individual files, loaded once per run rather than per file: a
        # full deploy calls _place() tens of thousands of times.
        self.pins = load_pins(game_root)
        self.pinned_skipped: list = []            # paths left alone because they are pinned
        self.preserved: list = []                 # runtime-state files left as the user has them
        self.backed_up: list = []                 # paths copied aside before being overwritten
        self.run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Interactive installer policy.  The historical deploy path keeps the old
        # overwrite behaviour; preview/apply installs can explicitly skip individual
        # existing paths without falsely claiming their ownership in the manifest.
        self.install_resolutions = {str(k).replace('\\', '/').lstrip('/'): str(v)
                                    for k, v in (install_resolutions or {}).items()}
        self.default_existing_action = (default_existing_action
                                        if default_existing_action in {"skip", "overwrite"}
                                        else "overwrite")
        self.resolution_skipped: list[str] = []

    def seed(self, manifest_mods: dict, skip: set):
        for mod, rels in manifest_mods.items():
            if mod in skip: continue
            for rel in rels: self.writers.setdefault(rel, []).append(mod)

    def deploy(self, extract_dir: Path, mod_name: str) -> bool:
        arrow = "would deploy" if self.dry_run else "deployed"

        # Detect what this mod needs (frameworks / declared deps) once, over the whole
        # extracted tree, so the info is the same for FOMOD, structured and loose mods.
        reqs = detect_requirements(extract_dir)
        if reqs:
            self.requirements[mod_name] = reqs

        # FOMOD is authoritative: when a ModuleConfig.xml is present and its default
        # selection resolves real files on disk, install exactly those and do NOT also
        # merge the raw option folders (which would install de-selected options too).
        cfg = find_fomod_config(extract_dir)
        if cfg is not None:
            try:
                fomod_pairs, fomod_summary = plan_fomod(cfg)
            except Exception as e:
                fomod_pairs, fomod_summary = [], f"unreadable ({e})"
                print(f"  [!] FOMOD installer error ({e}); falling back to structure scan.")
            if fomod_pairs:
                print(f"  [i] FOMOD installer — {fomod_summary}")
                n = self._deploy_pairs(fomod_pairs, mod_name)
                print(f"    -> {arrow} FOMOD selection: {n} file(s)")
                if n:
                    self.deployed_mods += 1
                    print(f"  [{'~' if self.dry_run else '✓'}] "
                          f"{'Would deploy' if self.dry_run else 'Deployed'}: {mod_name}")
                    self._print_requirements(mod_name)
                    return True
                print("  [!] FOMOD selected files but none resolved on disk; "
                      "falling back to structure scan.")
            elif fomod_summary and 'unreadable' not in fomod_summary:
                print(f"  [i] FOMOD installer — {fomod_summary}; falling back to structure scan.")

        # Non-FOMOD (or FOMOD fallback): detect Cyberpunk structure and merge.
        candidates, notes = find_candidates(extract_dir)
        resolved = resolve_candidates(candidates)
        notes.discard('fomod')
        if not resolved:
            print("  [!] No recognizable Cyberpunk 2077 structure found.")
            self.empty_mods += 1
            return False
        if notes: print(f"  [i] {'/'.join(sorted(notes))} installer detected — all options will be merged.")
        total_files = 0
        loose_files = [p for k, p in resolved if k == 'loose']
        for kind, cpath in resolved:
            if kind == 'loose': continue
            base = dest_base_for(kind, cpath, self.game_root)
            label = {'known': f"{cpath.name.lower() if cpath.name.lower() in STANDARD_DIRS_LOWER else cpath.name}/",
                     'cet': f"CET mod '{cpath.name}'",
                     'redmod': f"REDmod '{cpath.name}'"}[kind]
            n = self._deploy_tree(cpath, base, mod_name)
            total_files += n
            rel_dest = base.relative_to(self.game_root)
            print(f"    -> {arrow} {label}: {n} file(s) to {rel_dest}/")
        if loose_files:
            groups: dict[Path, list[Path]] = {}
            for p in loose_files: groups.setdefault(LOOSE_DESTS[p.suffix.lower()], []).append(p)
            for rel_dest, files in sorted(groups.items(), key=lambda kv: str(kv[0])):
                n = self._deploy_loose(files, rel_dest, mod_name)
                total_files += n
                exts = "/".join(sorted({p.suffix.lower() for p in files}))
                print(f"    -> {arrow} loose {exts}: {n} file(s) to {rel_dest}/ (unstructured mod)")
                if any(p.suffix.lower() in LOOSE_WARN_EXTS for p in files):
                    print(f"  [!] verify: bare .dll assumed to be a RED4ext plugin.")
        if total_files:
            self.deployed_mods += 1
            print(f"  [{'~' if self.dry_run else '✓'}] {'Would deploy' if self.dry_run else 'Deployed'}: {mod_name}")
            self._print_requirements(mod_name)
            return True
        self.empty_mods += 1
        print(f"  [!] Nothing deployed for: {mod_name}")
        return False

    def _installed_names(self) -> set:
        """Names counting as 'present' for requirement checks: everything already in the
        manifest plus every mod/archive queued in this run (so a framework installed in
        the same batch satisfies mods that depend on it, even before it lands on disk)."""
        return set(self.manifest_mods) | set(self.batch_names) | set(self.requirements)

    def _print_requirements(self, mod_name: str):
        reqs = self.requirements.get(mod_name)
        if not reqs:
            return
        installed = self._installed_names()
        parts = [f"{d} [{'✓' if requirement_satisfied(d, installed, self.game_root) else '✗ MISSING'}]"
                 for d in reqs]
        print(f"      requires: {' · '.join(parts)}")

    def report_requirements(self, installed_names: set):
        """Aggregated end-of-run requirements roll-up: for every mod deployed this run,
        which of its requirements are still unmet, and who needs each. Purely advisory."""
        print("\n=== Requirements ===")
        if not self.requirements:
            print("  [✓] No framework requirements detected for the mod(s) installed this run.")
            return
        missing: dict[str, list] = {}
        for mod, reqs in self.requirements.items():
            for dep in reqs:
                if not requirement_satisfied(dep, installed_names, self.game_root):
                    missing.setdefault(dep, []).append(mod)
        if not missing:
            print("  [✓] All detected requirements are satisfied.")
            return
        print("  [!] Missing requirements — install these too (mods still work only once present):")
        for dep, mods in sorted(missing.items()):
            who = ", ".join(sorted(set(mods)))
            print(f"      - {dep}   (needed by: {who})")

    def _resolve_existing_case(self, rel: Path) -> Path:
        """Reuse the casing already on disk for each directory component of `rel`, so a
        later deploy never creates a second case-variant of an existing folder (a
        lowercase `core/` beside an existing `Core/`) — which on this case-sensitive FS
        makes redscript compile every module twice (SYM_REDEFINITION). Filename case is
        left alone. A per-run cache keeps choices consistent, incl. on dry runs where the
        directories are never physically created."""
        parts = rel.parts
        if len(parts) <= 1:
            return rel
        out: list[str] = []
        cur = self.game_root
        acc_low = ""
        for part in parts[:-1]:
            acc_low = f"{acc_low}/{part.lower()}" if acc_low else part.lower()
            chosen = self._case_cache.get(acc_low)
            if chosen is None:
                chosen = part
                if not (cur / part).exists():
                    low = part.lower()
                    try:
                        for entry in os.scandir(cur):
                            if entry.is_dir() and entry.name != part and entry.name.lower() == low:
                                chosen = entry.name
                                break
                    except OSError:
                        pass
                self._case_cache[acc_low] = chosen
            out.append(chosen)
            cur = cur / chosen
        out.append(parts[-1])
        return Path(*out)

    def _place(self, src: Path, dest: Path, mod_name: str):
        # No caller — including a scripted FOMOD — may escape the configured game root.
        root_real = self.game_root.resolve()
        try:
            rel = dest.resolve(strict=False).relative_to(root_real)
        except (OSError, ValueError) as e:
            raise ValueError(f"destination escapes game root: {dest}") from e
        normalized_rel = self._resolve_existing_case(normalize_rel_path(rel))
        final_dest = self.game_root / normalized_rel
        try:
            final_dest.resolve(strict=False).relative_to(root_real)
        except (OSError, ValueError) as e:
            raise ValueError(f"destination escapes game root via symlink: {normalized_rel}") from e
        key = normalized_rel.as_posix()

        # Interactive conflict resolution is checked BEFORE ownership is recorded.  A
        # skipped existing file still belongs to whoever supplied it previously; claiming
        # it for this mod would make a later uninstall delete another mod's file.
        if final_dest.exists():
            action = self.install_resolutions.get(key, self.default_existing_action)
            if action == "skip":
                self.resolution_skipped.append(key)
                if self.verbose:
                    print(f"       [skip] {key}: kept existing file")
                return

        chain = self.writers.setdefault(key, [])
        if chain and chain[-1] != mod_name and self.verbose: print(f"       [conflict] {key}: {chain[-1]} -> {mod_name}")
        # The chain is appended to regardless of pin/runtime preservation below: in those
        # policy cases the mod still logically owns the path even when the user's bytes stay.
        chain.append(mod_name)
        # Single choke point for every write in the whole deployer — pins, runtime-state
        # preservation and overwrite backups all hang off this one decision.
        what = deploy_disposition(key, src, final_dest, self.pins)
        if what == "pinned":
            self.pinned_skipped.append(key)
            return
        if what == "preserved":
            self.preserved.append(key)
            return
        if not self.dry_run:
            if what == "backup":
                self._backup_before_overwrite(normalized_rel, final_dest)
            final_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, final_dest)
        elif what == "backup":
            self.backed_up.append(key)

    def _backup_before_overwrite(self, rel: Path, dest: Path):
        """Copy a file aside before its content is replaced. A copy, not a move: the deploy
        must still succeed, and a failed backup must never be a reason to lose the file."""
        try:
            stash = (self.game_root / OVERWRITE_BACKUP_DIRNAME / self.run_stamp / rel)
            stash.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, stash)
            self.backed_up.append(rel.as_posix())
        except OSError as e:
            print(f"  [!] could not back up {rel} before overwriting: {e}")

    def _deploy_loose(self, files, rel_dest: Path, mod_name: str) -> int:
        base = self.game_root / rel_dest
        for src in files: self._place(src, base / src.name, mod_name)
        return len(files)

    def _deploy_pairs(self, pairs, mod_name: str) -> int:
        count = 0
        for src, dest_rel in pairs:
            if not src.is_file(): continue
            self._place(src, self.game_root / dest_rel, mod_name)
            count += 1
        return count

    def _deploy_tree(self, src_base: Path, dest_base: Path, mod_name: str) -> int:
        count = 0
        for src in iter_files(src_base):
            self._place(src, dest_base / src.relative_to(src_base), mod_name)
            count += 1
        return count

    def conflicts(self): return {k: v for k, v in self.writers.items() if len(set(v)) > 1}

    def ownership(self, restrict: set | None = None) -> dict:
        mods: dict[str, list[str]] = {}
        for key, chain in self.writers.items():
            owner = chain[-1]
            if restrict is not None and owner not in restrict: continue
            mods.setdefault(owner, []).append(key)
        for v in mods.values(): v.sort()
        return mods

# ── Manifest I/O ─────────────────────────────────────────────────────────────
def load_manifest(game_root: Path) -> dict:
    path = game_root / MANIFEST_NAME
    if not path.exists(): return {"game_root": str(game_root), "updated": None, "mods": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("mods", {})
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"[!] Could not read manifest ({e}); starting a fresh one.")
        return {"game_root": str(game_root), "updated": None, "mods": {}}

def save_manifest(game_root: Path, mods: dict):
    path = game_root / MANIFEST_NAME
    tmp = path.with_suffix(".json.tmp")
    payload = {"game_root": str(game_root), "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mods": mods}
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(path)

# ── Archive discovery ────────────────────────────────────────────────────────
def is_archive_file(path: Path) -> bool:
    if path.suffix.lower() in ARCHIVE_EXTS: return True
    try:
        with open(path, 'rb') as fh: head = fh.read(8)
    except OSError: return False
    return any(head.startswith(m) for m in ARCHIVE_MAGICS)

def archive_listing(path: Path) -> set:
    try: out = subprocess.run(["bsdtar", "-tf", str(path)], capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError): return set()
    files = set()
    for line in out.splitlines():
        entry = line.strip().replace('\\', '/').rstrip('/')
        if entry and '.' in entry.rsplit('/', 1)[-1]: files.add(entry.lower())
    return files

def nexus_key_from_stem(stem: str):
    m = NEXUS_NAME_RE.match(stem)
    if m: return (m['name'].lower(), m['mid'], int(m['epoch']), m['ver'])
    m = NEXUS_NAME_RE2.match(stem)
    if m:
        stamp = datetime.strptime(m['ts'], "%Y-%m-%dT%H-%MZ").replace(tzinfo=timezone.utc)
        return (m['name'].lower(), m['mid'], int(stamp.timestamp()), m['ver'])
    return None

def nexus_key(path: Path):
    res = nexus_key_from_stem(path.stem)
    if res: return (res[0], res[1], res[2])
    return None

def sort_key(path: Path):
    key = nexus_key(path)
    if key: return (key[0], key[1], key[2], path.name.lower())
    return (path.stem.lower(), '', 0, path.name.lower())

def drop_superseded(archives):
    """Split downloads into (kept, superseded, ambiguous). Only ONE version of a mod may
    ever be deployed — two versions writing the same paths is the root cause behind the
    RED4ext downgrade and the DisclosedHiddenGems `Core/`+`core/` compile abort.

    Grouping is by mod_identity(); what settles whether an older archive is a previous
    VERSION (drop it) or a co-installable SIBLING file of the same mod page (keep it) is
    whether their contents overlap. Mod 27740 publishes one fridge per apartment under a
    single id, name and version string with entirely disjoint contents; treating those as
    versions would install exactly one of twenty."""
    groups: dict[tuple, list[tuple[int, Path]]] = {}
    kept, superseded, ambiguous = [], [], []
    for a in archives:
        ident = mod_identity(a.stem)
        if ident: groups.setdefault(ident, []).append((framework_version_key(a.stem), a))
        else: kept.append(a)
    for (mid, _name), versions in groups.items():
        versions.sort(key=lambda t: t[0])
        newest = versions[-1][1]
        kept.append(newest)
        if len(versions) == 1: continue
        if mid in FRAMEWORK_MOD_IDS:
            # frameworks: the newest build is always the only correct one — never keep an
            # older build as "ambiguous", it would re-introduce the downgrade.
            superseded.extend((older, newest) for _, older in versions[:-1])
            continue
        newest_files = archive_listing(newest)
        for _, older in versions[:-1]:
            older_files = archive_listing(older)
            if not older_files or not newest_files:
                # Could not read one of them — refuse to guess, keep both and say so.
                kept.append(older)
                ambiguous.append((older, newest, []))
            elif not (older_files & newest_files):
                kept.append(older)                     # disjoint: a sibling file, not a version
            else:
                # Overlapping contents = the same mod at two versions. The older one goes,
                # even when it holds files the newer dropped: those are files the author
                # renamed or removed, and keeping them alongside the new ones is precisely
                # what left two case-variants of one script folder on disk.
                superseded.append((older, newest))
    return kept, superseded, ambiguous

def find_archives(downloads: Path, selectors, keep_superseded: bool = False):
    archives = [f for f in downloads.iterdir() if f.is_file() and is_archive_file(f)]
    if selectors:
        def matches(f: Path) -> bool:
            return any(fnmatch(f.name, s) or s.lower() in f.name.lower() or f.name == s for s in selectors)
        archives = [f for f in archives if matches(f)]
    superseded, ambiguous = [], []
    if not keep_superseded: archives, superseded, ambiguous = drop_superseded(archives)
    return (sorted(archives, key=sort_key), sorted(superseded, key=lambda t: t[0].name.lower()), sorted(ambiguous, key=lambda t: t[0].name.lower()))

# ───────────────── Mod file discovery (--list-files / --which) ─────────────────
# Removing a mod by hand means searching the install by name, and mod files are NOT named
# after their mod. Observed: "GTA - JoyRide" spanned 8 archives, an r6/scripts dir, an
# r6/audioware dir AND r6/storages/CustomMapMarkers/joyride_icons.json — filed under ANOTHER
# mod's storage folder. A by-hand pass found half; missing the rest leaves dead archives
# loading forever. Going the other way, a CET folder is often an abbreviation with no
# relation to the Nexus name (NCestate = "Night City Estate", mod 12857), so the user could
# not identify their own installed mods.
#
# The manifest already records every path deploy placed, so BOTH directions are exact
# lookups and need no name matching at all. Heuristics are confined to the one thing the
# manifest cannot know: files the mod wrote at RUNTIME, or that a hand-install put there.
# Those are swept for by name, reported in their own bucket, and never acted on.

# Swept in addition to MANAGED_ROOTS: where mods keep runtime state and settings. The base
# game's tens of thousands of files are deliberately out of scope — none of them is a mod's.
DISCOVERY_EXTRA_ROOTS = ("r6/storages", "r6/config", "r6/input", "engine/config")
DISCOVERY_MIN_TOKEN = 4
# A token matching more files than this is a common word, not a name. Self-calibrating,
# which beats a curated stopword list that would be wrong for the next mod.
DISCOVERY_TOKEN_CAP = 40
# Words that identify no mod: generic mod-name filler, and the fixed path vocabulary of the
# install itself. The frequency cap is the real backstop; this just keeps the output clean.
DISCOVERY_STOPWORDS = frozenset({
    "cyberpunk", "2077", "mod", "mods", "pack", "redux", "version", "final", "update",
    "addon", "patch", "fix", "fixes", "the", "and", "for", "with", "all", "one", "vortex",
    "installer", "collection", "standalone", "optional", "main", "file", "files", "night",
    "city", "vanilla", "custom", "better", "improved", "enhanced", "ultimate", "edition",
    "scripts", "tweaks", "plugins", "archive", "archives", "init", "config", "settings",
    "cyber", "engine", "red4ext", "storages", "soundbanks", "data", "bin", "x64", "true",
})

def _discovery_tokens(stem: str, names: dict | None = None, owned=()) -> set:
    """Distinctive words for the runtime-file sweep, drawn from both of the mod's names AND
    from the paths it already owns.

    The owned paths matter more than the names do. "GTA - JoyRide" yields `joyride` from its
    name, but "Night City Estate" yields only `estate` — while its manifest entry owns
    `.../mods/NCestate/init.lua`, which yields `ncestate` and then finds the mod's storage
    folder. Deriving the abbreviation from disk beats guessing it from the name, so no
    acronym rule is needed here."""
    out: set = set()
    def add(text: str):
        for tok in re.split(r'[^A-Za-z0-9]+', text or ""):
            t = tok.lower()
            if (len(t) >= DISCOVERY_MIN_TOKEN and t not in DISCOVERY_STOPWORDS
                    and not t.isdigit()):
                out.add(t)
    add(mod_display_name(stem, names))
    add(stem_display_name(stem))
    for rel in owned:
        low = rel.replace('\\', '/')
        # Only the part below the managed root: above it is path structure shared by every
        # mod in the install, and tokenising that matches everything.
        for root_rel in MANAGED_ROOTS + DISCOVERY_EXTRA_ROOTS:
            if low.lower().startswith(root_rel + "/"):
                low = low[len(root_rel) + 1:]
                break
        add(low.rsplit('.', 1)[0])
    return out

def _walk_discovery_files(game_root: Path):
    """Every file under the roots a mod could plausibly have written to, as posix rel paths."""
    for root_rel in MANAGED_ROOTS + DISCOVERY_EXTRA_ROOTS:
        root = game_root / root_rel
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in HEALTH_SKIP_DIRS]
            here = Path(dirpath)
            for fn in filenames:
                yield (here / fn).relative_to(game_root).as_posix()

def _stat_size(game_root: Path, rel: str):
    """(bytes, missing). A manifest path that is gone is a finding, not an error."""
    try:
        return (game_root / rel).stat().st_size, False
    except OSError:
        return 0, True

def find_mod_files(game_root: Path, mods: dict, target: str, names: dict | None = None,
                   swept: list | None = None) -> dict:
    """Every file belonging to `target` (an exact manifest key), plus files that only LOOK
    like they do. Read-only.

    `swept` is the walk of the mod roots, hoisted so a needle matching many mods costs ONE
    walk rather than one per mod — the same trap as reloading the name cache per mod. Callers
    that ask about a single mod can leave it out.

    Three buckets, because each needs a different action:
      owned   — the manifest placed it. Exact, no guessing. `missing` means it is gone from
                disk: a hand-removal the manifest never learned about.
      related — name-matches, but ANOTHER manifest entry owns it. Removing the target must
                not touch these; they are listed so a by-hand pass does not delete them.
      loose   — name-matches and nothing owns it. Runtime state the mod wrote, or a
                hand-install. This is the joyride_icons.json class, and the only bucket
                produced by a heuristic rather than by the manifest."""
    owned_rel = sorted(mods.get(target, []), key=str.lower)
    owned_folded = {r.lower() for r in owned_rel}
    owner_by_path = {}
    for stem, files in (mods or {}).items():
        if stem == target:
            continue
        for r in files:
            owner_by_path[r.lower()] = stem
    tokens = _discovery_tokens(target, names, owned_rel)
    hits: dict = {t: [] for t in tokens}
    if tokens:
        for rel in (swept if swept is not None else _walk_discovery_files(game_root)):
            low = rel.lower()
            if low in owned_folded:
                continue
            for t in tokens:
                if t in low:
                    hits[t].append(rel)
    dropped = sorted(t for t, v in hits.items() if len(v) > DISCOVERY_TOKEN_CAP)
    matched: dict = {}
    for t in sorted(hits):
        if len(hits[t]) > DISCOVERY_TOKEN_CAP:
            continue
        for rel in hits[t]:
            matched.setdefault(rel, t)
    owned, related, loose = [], [], []
    for rel in owned_rel:
        size, missing = _stat_size(game_root, rel)
        owned.append({"rel": rel, "bytes": size, "missing": missing})
    for rel in sorted(matched, key=str.lower):
        size, _ = _stat_size(game_root, rel)
        row = {"rel": rel, "bytes": size, "token": matched[rel]}
        owner = owner_by_path.get(rel.lower(), "")
        if owner:
            row["owner"] = owner
            related.append(row)
        else:
            loose.append(row)
    return {"target": target, "owned": owned, "related": related, "loose": loose,
            "tokens": sorted(tokens), "dropped": dropped,
            "bytes": sum(r["bytes"] for r in owned),
            "missing": sum(1 for r in owned if r["missing"])}

def _group_by_root(rows):
    """Rows grouped by their top-level managed root, so eight archives read as one line item
    rather than eight scattered ones."""
    groups: dict = {}
    for r in rows:
        rel = r["rel"]
        root = next((x for x in MANAGED_ROOTS + DISCOVERY_EXTRA_ROOTS
                     if rel.lower().startswith(x + "/")), rel.rsplit('/', 1)[0])
        groups.setdefault(root, []).append(r)
    return sorted(groups.items())

def report_mod_files(found: dict, names: dict | None = None) -> None:
    """Print one mod's full footprint. Read-only; prints nothing machine-parseable because
    this is an on-demand query, not a health check."""
    target = found["target"]
    print(f"\n{describe_mod(target, names)}")
    owned = found["owned"]
    if not owned:
        print("  (the manifest records no files for this mod)")
    else:
        print(f"  {len(owned)} file(s), {_fmt_size(found['bytes'])} — owned, exact:")
        for root, rows in _group_by_root(owned):
            print(f"    {root}/")
            for r in rows[:40]:
                tail = r["rel"][len(root) + 1:] if r["rel"].lower().startswith(root + "/") \
                    else r["rel"]
                mark = "  [MISSING from disk]" if r["missing"] else ""
                print(f"      {tail}  ({_fmt_size(r['bytes'])}){mark}")
            if len(rows) > 40:
                print(f"      ... and {len(rows) - 40} more")
    if found["missing"]:
        print(f"  [!] {found['missing']} owned file(s) are gone from disk. The manifest still "
              "claims them, so this mod's footprint is smaller than it looks — someone "
              "removed them by hand.")
    if found["related"]:
        print(f"\n  {len(found['related'])} file(s) match this mod's name but ANOTHER mod "
              "owns them — do not delete these:")
        for r in found["related"][:15]:
            print(f"      {r['rel']}  ({_fmt_size(r['bytes'])})  owned by "
                  f"{mod_display_name(r['owner'], names)}")
        if len(found["related"]) > 15:
            print(f"      ... and {len(found['related']) - 15} more")
    if found["loose"]:
        print(f"\n  {len(found['loose'])} file(s) match this mod's name and NO manifest entry "
              "owns them — runtime state it wrote, or a hand-install. A manifest-driven "
              "uninstall leaves these behind:")
        for r in found["loose"][:15]:
            print(f"      {r['rel']}  ({_fmt_size(r['bytes'])})  [matched \"{r['token']}\"]")
        if len(found["loose"]) > 15:
            print(f"      ... and {len(found['loose']) - 15} more")
    if found["related"] or found["loose"]:
        print("  Name matching is a guess — the owned list above is the authoritative one.")
    if found["dropped"]:
        print(f"  [i] Ignored non-distinctive search word(s): {', '.join(found['dropped'])} "
              f"(each matched more than {DISCOVERY_TOKEN_CAP} files).")
    elif not found["tokens"]:
        print("  [i] This mod's name yielded no distinctive search words, so only the owned "
              "list above was produced.")

def action_list_files(game_root: Path, needle: str):
    manifest = load_manifest(game_root)
    mods = manifest.get("mods", {})
    if not mods:
        print("No manifest yet — deploy something first.")
        return
    names = load_mod_names()
    targets = sorted(m for m in mods if _matches_needle(m, needle, names))
    if not targets:
        print(f"No installed mod matches '{needle}'. Try --list to see what is installed, "
              "or --which <path> to look up a file.")
        return
    print(f"=== Files of {len(targets)} mod(s) matching '{needle}' ===")
    swept = list(_walk_discovery_files(game_root))
    for t in targets:
        report_mod_files(find_mod_files(game_root, mods, t, names, swept), names)

def find_path_owner(game_root: Path, mods: dict, needle: str) -> list:
    """Reverse lookup: which mod owns this path? Matches a manifest path case-insensitively
    as a substring, so a bare CET folder name ("NCestate"), a file name ("joyride_1.0.xl")
    and a full relative path all work. This is what turns an unidentifiable folder on disk
    back into a Nexus mod."""
    low = needle.lower().replace('\\', '/').strip('/')
    out = []
    for stem, files in (mods or {}).items():
        matches = sorted((r for r in files if low in r.lower()), key=str.lower)
        if matches:
            out.append({"stem": stem, "matches": matches, "total": len(files)})
    out.sort(key=lambda d: (-len(d["matches"]), d["stem"].lower()))
    return out

def action_which(game_root: Path, needle: str):
    manifest = load_manifest(game_root)
    mods = manifest.get("mods", {})
    names = load_mod_names()
    hits = find_path_owner(game_root, mods, needle)
    print(f"=== Owner of '{needle}' ===")
    if hits:
        for h in hits:
            ident = mod_identity(h["stem"])
            mid = ident[0] if ident else framework_mod_id(h["stem"])
            url = f"  https://www.nexusmods.com/cyberpunk2077/mods/{mid}" if mid else ""
            print(f"\n  {describe_mod(h['stem'], names)}{url}")
            print(f"    {len(h['matches'])} matching file(s) of {h['total']} owned:")
            for r in h["matches"][:15]:
                print(f"      {r}")
            if len(h["matches"]) > 15:
                print(f"      ... and {len(h['matches']) - 15} more")
        if not names:
            print("\n  [i] Showing archive filenames. Run --fetch-names (needs a Nexus API "
                  "key) to resolve these to real mod names.")
        return
    on_disk = [rel for rel in _walk_discovery_files(game_root)
               if needle.lower().replace('\\', '/').strip('/') in rel.lower()]
    if on_disk:
        print(f"  No manifest entry owns this, but {len(on_disk)} matching file(s) are on "
              "disk — a hand-install. See --check-unmanaged.")
        for rel in sorted(on_disk, key=str.lower)[:15]:
            print(f"      {rel}")
        if len(on_disk) > 15:
            print(f"      ... and {len(on_disk) - 15} more")
    else:
        print("  Nothing on disk or in the manifest matches that.")

def find_missing_owned_files(game_root: Path, mods: dict) -> list:
    """Manifest paths that are gone from disk — a hand-removal the manifest never learned
    about (the `!CityOfDreamsLogo.archive` case). The entry keeps claiming the file, so every
    footprint, attribution and removal plan built from the manifest overstates what is
    really installed."""
    out = []
    for stem, files in sorted((mods or {}).items()):
        gone = [r for r in files if not (game_root / r).exists()]
        if gone:
            out.append({"stem": stem, "missing": len(gone), "total": len(files),
                        "sample": sorted(gone, key=str.lower)[:3]})
    out.sort(key=lambda d: (-d["missing"], d["stem"].lower()))
    return out

def report_missing_files(game_root: Path, mods: dict, names: dict | None = None) -> int:
    """Print the missing-owned-files section plus a machine-parseable line. Read-only.
    Returns the number of affected mods."""
    rows = find_missing_owned_files(game_root, mods)
    total = sum(r["missing"] for r in rows)
    if rows:
        print(f"  [!] {len(rows)} mod(s) claim {total} file(s) that are not on disk. Someone "
              "removed them by hand; the manifest still lists them, so listings and removal "
              "plans overstate what is installed:")
        for r in rows[:10]:
            print(f"      - {describe_mod(r['stem'], names)}: {r['missing']} of {r['total']} "
                  "missing")
            for rel in r["sample"]:
                print(f"          {rel}")
        if len(rows) > 10:
            print(f"      ... and {len(rows) - 10} more")
        print("  Re-deploy the mod to restore the files, or remove it properly with "
              "--disable <mod> so the manifest agrees with the disk.")
    print(f"[missing] {len(rows)} mod(s) with {total} owned file(s) gone from disk")
    return len(rows)

def action_check_missing(game_root: Path):
    mf = load_manifest(game_root)
    print("\n=== Missing Owned Files (in the manifest, not on disk) ===")
    report_missing_files(game_root, mf.get("mods", {}), load_mod_names())

# ── Actions ──────────────────────────────────────────────────────────────────
def action_list(game_root: Path, needle: str | None):
    manifest = load_manifest(game_root)
    mods = manifest["mods"]
    if not mods:
        print("No manifest yet — deploy something first.")
        return
    names = load_mod_names()
    items = sorted(mods.items(), key=lambda kv: mod_display_name(kv[0], names).lower())
    if needle: items = [(m, f) for m, f in items if _matches_needle(m, needle, names)]
    total_files = sum(len(f) for _, f in items)
    print(f"Deployed mods (manifest updated {manifest.get('updated')}):")
    for mod, files in items: print(f"  {len(files):5d} files  {describe_mod(mod, names)}")
    if not names:
        print("\n  [i] Showing archive filenames. Run --fetch-names (needs a Nexus API key) "
              "to show real mod names instead.")
    print(f"\n{len(items)} mod(s), {total_files} tracked file(s).")

def _remove_mods(game_root: Path, mods: dict, targets: list, dry_run: bool, purge: bool,
                 only_paths: set | None = None):
    """Safely remove `targets` (exact manifest keys) from the game. A file also owned
    by a mod that stays installed is left in place; the rest are moved to
    .mod_trash/<timestamp>/ (recoverable) unless purge=True. Empty dirs are pruned and
    the manifest is rewritten. A dry run mutates nothing (neither disk nor manifest).

    `only_paths` restricts removal to a subset (case-folded rel paths) — that is how a
    PARTIAL uninstall works: drop a mod's CET Lua half and keep its r6/scripts, so other
    mods' `import` statements still resolve. An entry whose files are only partly removed
    keeps its remaining paths instead of being dropped."""
    # Captured before the manifest is touched: a target that keeps some of its files was
    # only partly removed, and saying "Removed 1 mod(s)" for it is wrong. It is what
    # --disable-lua and --prune-dangling both do.
    partial = [] if only_paths is None else [
        m for m in targets if any(r.lower() not in only_paths for r in mods.get(m, []))]
    kept_files = set()
    for m, files in mods.items():
        if m not in targets: kept_files.update(files)
    deleted, kept_shared, pruned = 0, 0, 0
    dirs_touched = set()
    trash_dir = None
    safe_delete = not purge
    if safe_delete and not dry_run:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        trash_dir = game_root / ".mod_trash" / timestamp
        try:
            trash_dir.mkdir(parents=True, exist_ok=True)
            print(f"  [i] Safe delete: moving files to {trash_dir.relative_to(game_root)}/")
        except Exception as e:
            print(f"  [!] Could not create trash directory: {e}. Falling back to permanent delete.")
            safe_delete = False
    removed_by_mod: dict = {}
    for mod in targets:
        for rel in mods[mod]:
            if only_paths is not None and rel.lower() not in only_paths:
                continue
            if rel in kept_files:
                kept_shared += 1
                continue
            fp = game_root / rel
            if fp.exists():
                if dry_run: print(f"  would delete {rel}")
                else:
                    try:
                        if not os.access(fp, os.W_OK):
                            try: os.chmod(fp, stat.S_IRUSR | stat.S_IWUSR)
                            except OSError: pass
                        if safe_delete and trash_dir:
                            dest_trash = trash_dir / rel
                            dest_trash.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(fp), str(dest_trash))
                        else: fp.unlink()
                        dirs_touched.add(fp.parent)
                    except OSError as e:
                        print(f"  [!] could not delete/move {rel}: {e}")
                        continue
                deleted += 1
            removed_by_mod.setdefault(mod, []).append(rel)
    if not dry_run:
        for d in sorted(dirs_touched, key=lambda p: len(p.parts), reverse=True):
            cur = d
            while cur != game_root and cur.is_dir() and not any(cur.iterdir()):
                parent = cur.parent
                try:
                    cur.rmdir()
                    pruned += 1
                except OSError: break
                cur = parent
        for mod in targets:
            if only_paths is None:
                mods.pop(mod, None)
                continue
            # Partial: keep the entry, minus the paths just taken away. Dropping it would
            # make the surviving r6/scripts unmanaged — orphaned, and prunable by mistake.
            left = [r for r in mods.get(mod, []) if r.lower() not in only_paths]
            if left:
                mods[mod] = left
            else:
                mods.pop(mod, None)
        save_manifest(game_root, mods)
    verb = "Would remove" if dry_run else "Removed"
    action_str = "permanently deleted" if purge or not safe_delete else "moved to trash"
    if not partial:
        head = f"{verb} {len(targets)} mod(s)"
    elif len(partial) == len(targets):
        head = f"{verb} part of {len(targets)} mod(s) (the rest of their files stay)"
    else:
        head = f"{verb} {len(targets) - len(partial)} mod(s) and part of {len(partial)} more"
    print(f"\n{head}: {deleted} file(s) {action_str}"
          + (f", {pruned} empty dir(s) pruned" if not dry_run else "")
          + (f", {kept_shared} shared file(s) left for other mods" if kept_shared else "") + ".")

def action_remove(game_root: Path, needle: str, dry_run: bool, purge: bool = False):
    manifest = load_manifest(game_root)
    mods = manifest["mods"]
    targets = [m for m in mods if needle.lower() in m.lower()]
    if not targets:
        print(f"No deployed mod matches '{needle}'. Try --list-deployed.")
        return
    if len(targets) > 1:
        print(f"'{needle}' matches {len(targets)} mods:")
        for t in targets: print(f"  - {t}")
        print("Removing all of them." + (" (dry-run)" if dry_run else ""))
    _remove_mods(game_root, mods, targets, dry_run, purge)

def action_remove_names(game_root: Path, names, dry_run: bool, purge: bool = False):
    """Exact-name safe removal (no substring matching) — how the GUI deletes a specific
    selection. Names absent from the manifest are reported and skipped, never guessed."""
    manifest = load_manifest(game_root)
    mods = manifest["mods"]
    seen, targets = set(), []
    for n in names:                         # preserve order, drop dups
        if n in mods and n not in seen:
            targets.append(n); seen.add(n)
    missing = [n for n in names if n not in mods]
    for n in missing:
        print(f"  [!] not in manifest, skipping: {n}")
    if not targets:
        print("Nothing to remove (no exact manifest match).")
        return
    _remove_mods(game_root, mods, targets, dry_run, purge)

def action_verify_archives(downloads: Path, selectors, api_key: str):
    archives = [f for f in downloads.iterdir() if f.is_file() and is_archive_file(f)]
    if selectors:
        def matches(f: Path) -> bool:
            return any(fnmatch(f.name, s) or s.lower() in f.name.lower() or f.name == s for s in selectors)
        archives = [f for f in archives if matches(f)]
    archives = sorted(archives, key=sort_key)
    if not archives:
        print("No matching archives found to verify." if selectors else "No archives found in Downloads folder.")
        return
    if not api_key:
        print("[!] No NexusMods API key configured. Use --nexus-api-key KEY to set one.")
        print("    Falling back to local MD5 display only.\n")
    print(f"Verifying {len(archives)} archive(s)...\n")
    verified_count = mismatch_count = unknown_count = error_count = 0
    for i, archive in enumerate(archives, 1):
        print(f"[{i}/{len(archives)}] {archive.name}")
        local_md5 = compute_md5(archive)
        if local_md5: print(f"  Local MD5: {local_md5}")
        else:
            print(f"  [!] Could not compute MD5")
            error_count += 1
            continue
        if not api_key:
            unknown_count += 1
            print(f"  Status:    SKIPPED (no API key)\n")
            continue
        status, matched_name, message = verify_nexus_hash(archive, api_key)
        if status == 'verified':
            verified_count += 1
            print(f"  Status:    ✓ VERIFIED — {message}\n")
        elif status == 'mismatch':
            mismatch_count += 1
            print(f"  Status:    ✗ MISMATCH — {message}\n")
        elif status == 'unknown':
            unknown_count += 1
            print(f"  Status:    ? UNKNOWN — {message}\n")
        else:
            error_count += 1
            print(f"  Status:    ! ERROR — {message}\n")
    print("=== Verification Summary ===")
    print(f"Archives checked : {len(archives)}")
    print(f"✓ Verified       : {verified_count}")
    print(f"✗ Mismatch       : {mismatch_count}")
    print(f"? Unknown        : {unknown_count}")
    print(f"! Errors         : {error_count}")

# ── Mod update checking + one-click download ─────────────────────────────────
# The comparison (check_file_update) is precise and already existed; what was missing is a
# machine-readable surface and a way to ACT on a result. A found update is only actionable
# if the download knows the exact file to fetch, so every update row carries the newest
# file's id, byte size and a synthesized filename in the collection's own
# <name>-<modid>-<ver>-<epoch> convention. That name is the load-bearing part: it is what
# lets the NEXT deploy recognise the fetched archive as a newer version of the installed mod
# and retire the old one (drop_superseded groups by mod_identity, parsed straight out of the
# stem). Downloading Nexus's own display-named file would leave BOTH versions installed.
#
# Deliberately NOT folded into --check-health. Every other health probe is a local, offline,
# instant filesystem read; this one is a network round-trip per mod-id, needs an API key and
# is rate-limited. Folding it in would turn the default health sweep into a slow, key-gated,
# throttled operation. It keeps the same house honesty (machine line + GUI parser + sync
# test) but stays its own on-demand action.

UPDATE_EPOCH_RE = re.compile(r'^1[0-9]{9}$')   # a 10-digit unix ts that NEXUS_NAME_RE parses
UPDATE_DL_CONNS = 8                            # aria2 connections per update file

def _stem_name_prefix(stem: str) -> str:
    """The human-name portion of a collection stem (before the -id-ver-epoch, or the
    'space id ver ts' tail), original case preserved. Falls back to the whole stem."""
    m = NEXUS_NAME_RE.match(stem) or NEXUS_NAME_RE2.match(stem)
    return m['name'] if m else stem

def canonical_update_filename(stem: str, mod_id, latest_ver, epoch, ext: str) -> str:
    """Name a fetched update in the collection convention <name>-<modid>-<ver>-<epoch><ext>
    so a subsequent deploy parses its identity and supersedes the installed version. The
    epoch is the new file's Nexus upload timestamp; if it isn't a parseable 10-digit unix
    time the name still reads sensibly, it just won't auto-supersede (rare — real uploads are
    all ~1.7e9)."""
    name = _stem_name_prefix(stem)
    ver = re.sub(r'[^\w.\-]', '_', str(latest_ver).lstrip('vV')) or "0"
    ext = ext if ext.startswith('.') else ('.' + ext if ext else '.zip')
    if epoch is not None and UPDATE_EPOCH_RE.match(str(epoch)):
        return f"{name}-{mod_id}-{ver}-{epoch}{ext}"
    return f"{name}-{mod_id}-{ver}{ext}"

def scan_updates(game_root: Path, api_key: str, cache: dict | None = None,
                 only: str | None = None) -> dict:
    """Compare every installed mod against Nexus. Returns
    {'updates': [row,...], 'current': int, 'unknown': [(stem, reason)], 'checked': int}.
    Each row is a dict: stem, name (human), mod_id, installed_ver, latest_ver, latest_file,
    file_id, size, epoch, download_name. `only` is a case-insensitive substring filter on the
    stem, or a bare mod-id, so a one-click 'update this one' fetches exactly that mod."""
    mods = load_manifest(game_root).get("mods", {})
    names = load_mod_names()
    if cache is None:
        cache = {}
    needle = (only or "").strip().lower()
    updates: list[dict] = []
    current = 0
    unknown: list[tuple[str, str]] = []
    checked = 0
    for stem in sorted(mods.keys()):
        key_info = nexus_key_from_stem(stem)
        if not key_info:
            if not needle:
                unknown.append((stem, "cannot parse a Nexus mod ID / file from the name"))
            continue
        _name, mod_id, epoch, installed_ver = key_info
        if needle and needle not in stem.lower() and needle != str(mod_id):
            continue
        checked += 1
        status, detail = check_file_update(mod_id, epoch, installed_ver, api_key, cache)
        if status == 'update' and isinstance(detail, dict):
            ext = Path(detail['latest_file']).suffix or '.zip'
            updates.append({
                "stem": stem,
                "name": mod_display_name(stem, names),
                "mod_id": mod_id,
                "installed_ver": detail['installed_ver'],
                "latest_ver": detail['latest_ver'],
                "latest_file": detail['latest_file'],
                "file_id": detail['file_id'],
                "size": detail['size'],
                "epoch": detail['epoch'],
                "download_name": canonical_update_filename(
                    stem, mod_id, detail['latest_ver'], detail['epoch'], ext),
            })
        elif status == 'current':
            current += 1
        else:
            unknown.append((stem, str(detail)))
    return {"updates": updates, "current": current, "unknown": unknown, "checked": checked}

def report_updates(scan: dict) -> int:
    """Human report + machine lines for an update scan. One [update] line per updatable mod
    (carrying file_id/size/download name), then the [updates] summary. Returns the count."""
    updates = scan["updates"]
    print("\n=== Update Check Results ===")
    if updates:
        print(f"\n[!] {len(updates)} installed file(s) have a newer version on Nexus:")
        for r in updates:
            print(f"  - {r['name']}")
            print(f"    Installed v{r['installed_ver']}  ->  Latest v{r['latest_ver']}"
                  f"   ({r['latest_file']})")
            print(f"    https://www.nexusmods.com/cyberpunk2077/mods/{r['mod_id']}?tab=files")
    else:
        print("\n[✓] All checkable installed files are up to date.")
    if scan["current"]:
        print(f"[✓] {scan['current']} file(s) up to date.")
    if scan["unknown"]:
        print(f"\n[?] {len(scan['unknown'])} mod(s) could not be checked:")
        for stem, reason in scan["unknown"]:
            print(f"  - {stem}: {reason}")
    # Machine lines: per-row detail (everything a download needs), then the summary count.
    for r in updates:
        print(f"[update] {r['stem']} | mod_id={r['mod_id']} file_id={r['file_id']} "
              f"size={r['size']} v{r['installed_ver']} -> v{r['latest_ver']} "
              f"| dl={r['download_name']}")
    print(f"[updates] {len(updates)} installed mod(s) have a newer version on Nexus")
    if updates:
        print("  Fetch them with --download-updates (into Downloads), then deploy to apply.")
    return len(updates)

def action_check_updates(game_root: Path, api_key: str):
    mods = load_manifest(game_root).get("mods", {})
    if not mods:
        print("No mods installed in manifest.")
        return
    if not api_key:
        print("[!] No NexusMods API key configured. Use --nexus-api-key KEY to set one.")
        return
    print(f"Checking updates for {len(mods)} installed mod(s)...")
    report_updates(scan_updates(game_root, api_key))

def update_download_mod(row: dict) -> dict:
    """Shape an update row into the dict the downloader consumes (modId/fileId/size/fileName).
    fileName is the canonical supersede-friendly name, NOT Nexus's display name, so the
    fetched archive lands in Downloads already named for the next deploy to retire the old
    version."""
    return {
        "modId": int(row["mod_id"]),
        "fileId": int(row["file_id"]),
        "size": int(row["size"]),
        "fileName": row["download_name"],
    }

def action_download_updates(game_root: Path, downloads: Path, api_key: str,
                            only: str | None = None, dry_run: bool = False):
    """One-click fetch of available updates INTO the Downloads folder — never the game folder;
    deploy stays a separate, explicit step. Resolves the free CDN link for each newer file
    through the existing cookies-session downloader and writes it named for supersede.
    Reports [downloaded] K of N. `only` narrows to one mod for a per-row 'update this'."""
    if not api_key:
        print("[!] No NexusMods API key configured. Use --nexus-api-key KEY to set one.")
        return
    rows = scan_updates(game_root, api_key, only=only)["updates"]
    if not rows:
        print("No updates to download" + (f" matching '{only}'." if only else "."))
        print(f"[downloaded] 0 of 0 update(s) fetched to {downloads}")
        return
    downloads.mkdir(parents=True, exist_ok=True)
    total_bytes = sum(r["size"] for r in rows)
    print(f"\n{'Would fetch' if dry_run else 'Fetching'} {len(rows)} update(s) "
          f"(~{total_bytes / 1e6:.0f} MB) to {downloads}:")
    for r in rows:
        print(f"  - {r['name']}  v{r['installed_ver']} -> v{r['latest_ver']}")
        print(f"      {r['download_name']}  ({r['size'] / 1e6:.1f} MB)")
    if dry_run:
        print(f"[download-plan] {len(rows)} update(s) would be fetched to {downloads}")
        return
    try:
        from . import downloader as DL
    except Exception as e:  # noqa: BLE001
        print(f"[!] Downloader unavailable ({e}); cannot fetch. Open the Nexus URLs above "
              "to download manually.")
        print(f"[downloaded] 0 of {len(rows)} update(s) fetched to {downloads}")
        return
    ok = 0
    for r in rows:
        dest = downloads / r["download_name"]
        mod = update_download_mod(r)
        if DL.is_complete(dest, mod["size"]):
            print(f"  [skip] already present: {dest.name}")
            ok += 1
            continue
        good, err = DL.download_with_retries(mod, dest, UPDATE_DL_CONNS)
        if good:
            print(f"  [ok] {dest.name}")
            ok += 1
        else:
            print(f"  [fail] {dest.name}: {err}")
    print(f"[downloaded] {ok} of {len(rows)} update(s) fetched to {downloads}")
    if ok:
        print("  Now deploy (or --sync) to install the fetched update(s); the older version "
              "is retired automatically.")

def action_check_frameworks(game_root: Path, downloads: Path):
    """Report, per known framework: the installed version, any stale duplicate entries, and
    whether a newer build is cached in Downloads (or an older one that will be ignored)."""
    mods = load_manifest(game_root).get("mods", {})
    inst = installed_frameworks(mods)
    dl = newest_framework_downloads(downloads)
    print("\n=== Framework Version Policy (check) ===")
    ids = sorted(set(inst) | set(dl), key=lambda m: FRAMEWORK_MOD_IDS[m])
    if not ids:
        print("  No known frameworks installed or cached in Downloads.")
        return
    updates = dupes = 0
    for mid in ids:
        name = FRAMEWORK_MOD_IDS[mid]
        entries = inst.get(mid, [])
        have = entries[0] if entries else None
        cand = dl.get(mid)
        line = f"  {name}: " + (f"installed v{have['ver']}" if have else "not installed")
        if have and len(entries) > 1:
            dupes += 1
            line += f"  [!] +{len(entries) - 1} stale: " + ", ".join('v' + e['ver'] for e in entries[1:])
        if cand:
            cv = framework_version_str(cand[0].stem)
            if not have or cand[1] > have["epoch"]:
                updates += 1
                line += f"  → newer v{cv} in Downloads"
            elif cand[1] < have["epoch"]:
                line += f"  (older v{cv} in Downloads — ignored)"
        print(line)
    print(f"\n  {updates} update(s) available, {dupes} framework(s) with stale duplicates.")
    if updates or dupes:
        print("  Run --fix-frameworks to apply (newest only; older builds archived, reversible).")

def action_fix_frameworks(game_root: Path, downloads: Path, tmp_base: Path, api_key: str,
                          dry_run: bool, verbose: bool = False):
    """Enforce 'newest framework only': deploy the newest cached build of any framework that
    is older/absent on disk (never downgrade), then drop stale duplicate manifest entries and
    archive files unique to the older builds. Reversible."""
    manifest = load_manifest(game_root)
    mods = manifest.get("mods", {})
    inst = installed_frameworks(mods)
    dl = newest_framework_downloads(downloads)
    print("\n=== Fix Frameworks (newest only) ===")
    to_deploy = []
    for mid in sorted(dl, key=lambda m: FRAMEWORK_MOD_IDS[m]):
        arch, ep = dl[mid]
        entries = inst.get(mid, [])
        have_ep = entries[0]["epoch"] if entries else -1
        name = FRAMEWORK_MOD_IDS[mid]
        if ep > have_ep:
            to_deploy.append(arch)
            act = "install" if not entries else f"update v{entries[0]['ver']} →"
            print(f"  [i] {name}: {act} v{framework_version_str(arch.stem)} (from Downloads)")
        elif entries and ep < have_ep:
            print(f"  [protect] {name}: keep installed v{entries[0]['ver']}; Downloads has "
                  f"older v{framework_version_str(arch.stem)} — not downgrading.")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if to_deploy and dry_run:
        for a in to_deploy:
            print(f"  [~] Would deploy (force) {a.name}")
    elif to_deploy:
        scratch = tmp_base / TMP_DIR_NAME
        force_rmtree(scratch); scratch.mkdir(parents=True, exist_ok=True)
        dep = Deployer(game_root, dry_run=False, verbose=verbose, manifest_mods=mods)
        stems = {a.stem for a in to_deploy}
        dep.batch_names = stems
        dep.seed(mods, skip=stems)
        try:
            run_deploy(sorted(to_deploy, key=sort_key), dep, 1, scratch, api_key)
        finally:
            force_rmtree(scratch)
        merged = dict(mods); merged.update(dep.ownership(restrict=stems))
        mods.clear(); mods.update(merged)
    fw_actions = dedupe_framework_manifest(game_root, mods, dry_run, ts)
    for act in fw_actions:
        extra = f", archived {act['orphans_moved']} stale file(s)" if act['orphans_moved'] else ""
        print(f"  [{'~' if dry_run else '✓'}] {act['framework']}: keep v{act['keep_ver']}, "
              f"{'would drop' if dry_run else 'dropped'} stale v{act['drop_ver']}{extra}")
    if not dry_run and (to_deploy or fw_actions):
        save_manifest(game_root, mods)
    if not to_deploy and not fw_actions:
        print("  [✓] All frameworks are already newest and free of duplicates.")
    else:
        print(f"\n{'Would apply' if dry_run else 'Applied'}: {len(to_deploy)} framework deploy(s), "
              f"{len(fw_actions)} stale entr(y/ies) removed"
              + ("." if dry_run else f" (backup under {DUP_BACKUP_DIRNAME}/{ts}/ where files moved)."))

# ── Deploy orchestration ─────────────────────────────────────────────────────
def force_rmtree(path: Path):
    for root, dirs, _ in os.walk(path):
        for d in (root, *(os.path.join(root, x) for x in dirs)):
            try: os.chmod(d, os.stat(d).st_mode | stat.S_IRWXU)
            except OSError: pass
    shutil.rmtree(path, ignore_errors=True)

def _extract_to_tmp(archive: Path, tmp_base: Path):
    tmp = Path(tempfile.mkdtemp(prefix="mod_", dir=str(tmp_base)))
    ok = extract_archive(archive, tmp)
    return tmp, ok

def run_deploy(archives, deployer: Deployer, jobs: int, tmp_base: Path, api_key: str):
    total = len(archives)
    fail_extract = 0
    hash_results = {}
    def process(i, archive, tmp, ok):
        print(f"[{i}/{total}] Processing: {archive.name}")
        if api_key:
            status, matched_name, message = verify_nexus_hash(archive, api_key)
            hash_results[archive.stem] = status
            if status == 'verified': print(f"  [✓] Hash: VERIFIED — {message}")
            elif status == 'mismatch': print(f"  [✗] Hash: MISMATCH — {message}")
            elif status == 'unknown': print(f"  [?] Hash: {message}")
            else: print(f"  [!] Hash: {message}")
        else:
            local_md5 = compute_md5(archive)
            hash_results[archive.stem] = "no_key"
            if local_md5: print(f"  [i] Local MD5: {local_md5} (no API key to verify)")
        if api_key:
            key = nexus_key(archive)
            if key:
                mod_id = key[1]
                meta = fetch_nexus_metadata(mod_id, api_key)
                if meta:
                    name = meta.get('name', 'Unknown')
                    version = meta.get('version', '?')
                    author = meta.get('author', 'Unknown')
                    category = meta.get('category_name', 'Uncategorized')
                    print(f"  [i] Nexus: {name} (v{version}) by {author} [{category}]")
                    summary = meta.get('summary', '')
                    if summary:
                        clean_summary = re.sub('<[^<]+>', '', summary).replace('\n', ' ').replace('\r', '').strip()
                        if len(clean_summary) > 120: clean_summary = clean_summary[:117] + "..."
                        print(f"      {clean_summary}")
                else: print(f"  [i] Nexus: Metadata not found or hidden (Mod ID: {mod_id})")
        if not ok:
            print(f"  [!] Failed to extract {archive.name}. Skipping.")
            return False
        deployer.deploy(tmp, archive.stem)
        return True
    if jobs <= 1:
        for i, archive in enumerate(archives, 1):
            tmp, ok = _extract_to_tmp(archive, tmp_base)
            try:
                if not process(i, archive, tmp, ok): fail_extract += 1
            finally: force_rmtree(tmp)
        print("-" * 60)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futures = {}
            window = jobs + 1
            nxt = 0
            for k in range(min(window, total)):
                futures[k] = ex.submit(_extract_to_tmp, archives[k], tmp_base)
                nxt = k + 1
            for i in range(total):
                tmp, ok = futures.pop(i).result()
                if nxt < total:
                    futures[nxt] = ex.submit(_extract_to_tmp, archives[nxt], tmp_base)
                    nxt += 1
                try:
                    if not process(i + 1, archives[i], tmp, ok): fail_extract += 1
                finally: force_rmtree(tmp)
        print("-" * 60)
    return fail_extract, hash_results

# ── main ─────────────────────────────────────────────────────────────────────
def is_installed(mod_name: str, manifest_mods: dict, game_root: Path) -> bool:
    files = manifest_mods.get(mod_name)
    if not files: return False
    return all((game_root / rel).exists() for rel in files)

def require_bsdtar():
    if not shutil.which("bsdtar"):
        sys.exit("[!] 'bsdtar' not found. Install: sudo pacman -S libarchive  /  sudo apt install libarchive-tools")

def main():
    _install_cli_logger()      # tee stdout to the log file (CLI only, never on import)
    ap = argparse.ArgumentParser(description="Cyberpunk 2077 mod deployer (bsdtar).")
    ap.add_argument("selectors", nargs="*", help="only process archives matching these name(s)/glob(s)")
    ap.add_argument("--only", action="append", default=[], metavar="GLOB")
    ap.add_argument("--game-root", type=Path, default=GAME_ROOT_DEFAULT)
    ap.add_argument("--downloads", type=Path, default=DOWNLOADS_DEFAULT)
    ap.add_argument("--tmp", type=Path, default=TMP_BASE_DEFAULT)
    ap.add_argument("--jobs", "-j", type=int, default=4)
    ap.add_argument("--force", "--redeploy", action="store_true", dest="force")
    ap.add_argument("--keep-superseded", action="store_true")
    ap.add_argument("--conflict-report", metavar="FILE")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--no-manifest", action="store_true")
    ap.add_argument("--list-deployed", nargs="?", const="", metavar="FILTER")
    ap.add_argument("--remove", metavar="NAME", help="Safely remove deployed mods whose name contains NAME (substring)")
    ap.add_argument("--remove-from-file", metavar="FILE", help="Safely remove the exact mod names listed (one per line) in FILE")
    ap.add_argument("--purge", action="store_true", help="Permanently delete files instead of moving them to .mod_trash (Safe Delete)")
    ap.add_argument("--check-reqs", action="store_true", help="Check if all installed mods have their requirements met")
    ap.add_argument("--check-updates", action="store_true", help="Check if installed mods have newer versions on NexusMods")
    ap.add_argument("--download-updates", nargs="?", const="", default=None, metavar="NEEDLE",
                    help="Fetch available updates into the Downloads folder (optional NEEDLE = stem substring or mod-id to fetch just one); then deploy to apply")
    ap.add_argument("--check-health", action="store_true", help="Detect case-duplicate mod folders that break redscript (SYM_REDEFINITION)")
    ap.add_argument("--fix-duplicates", action="store_true", help="Auto-resolve redscript case-duplicate folders (keeps a reversible backup)")
    ap.add_argument("--check-attribution", nargs="?", const="", metavar="FILTER", help="Audit whether each installed mod's owned files actually came from its archive (slow: one listing per mod)")
    ap.add_argument("--perf-profile", action="store_true", help="Report per-frame CET script cost and list the every-frame mods that no other mod depends on")
    ap.add_argument("--apply", action="store_true", help="With --perf-profile: actually disable the listed mods (reversible with --enable)")
    ap.add_argument("--fetch-names", action="store_true", help="Look up the real Nexus mod name for every installed mod-id, so listings and searches use names instead of archive filenames")
    ap.add_argument("--refresh-names", action="store_true", help="With --fetch-names: re-fetch names already cached")
    ap.add_argument("--pin", metavar="PATH", help="Never overwrite this file (path relative to the game root) — protects a hand-built DLL or a hand-edited config from the next deploy")
    ap.add_argument("--unpin", metavar="PATH", help="Undo --pin: deploy may overwrite the file again")
    ap.add_argument("--list-pins", action="store_true", help="List pinned files and the runtime-state files preserved by policy")
    ap.add_argument("--disable", metavar="NAME", help="Uninstall mods matching NAME (substring, or a bare Nexus mod-id) and remember the choice, so later deploys never reinstall them")
    ap.add_argument("--disable-lua", metavar="NAME", help="Partial uninstall: drop a mod's CET Lua half (where per-frame cost lives) but keep its r6/scripts, so mods that import it still compile")
    ap.add_argument("--enable", metavar="NAME", help="Undo --disable: the mod is installed again on the next deploy")
    ap.add_argument("--list-disabled", action="store_true", help="List mods disabled with --disable")
    ap.add_argument("--prune", action="store_true", help="Finish uninstalling every disabled mod whose files are still on disk")
    ap.add_argument("--no-prune", action="store_true", help="With a deploy: do not remove the files of disabled mods this run")
    ap.add_argument("--force-prune", action="store_true", help="Proceed even when a prune would remove more than half the install (normally refused as a likely manifest bug)")
    ap.add_argument("--check-fds", nargs="?", const="0", metavar="LIMIT", help="Estimate open file descriptors at play time against ulimit -n and warn when headroom is tight (optionally against a given limit)")
    ap.add_argument("--check-locale", nargs="?", const="", metavar="LOCALE", help="Find CET mods that probe a non-existent /language config var (hard engine crash) or ship no strings for your locale; pass e.g. ua-ua once to record your locale")
    ap.add_argument("--check-cet-links", action="store_true", help="Find CET mods whose GetMod(\"folder\") lookup returns nil because the other mod renamed its folder (silent broken integration)")
    ap.add_argument("--check-conflicts", action="store_true", help="Warn when several mods claim the same subsystem (first-person camera, main menu...) or replace the same redscript method")
    ap.add_argument("--check-unmanaged", action="store_true", help="List mod files on disk that no manifest entry owns (hand-installed mods the manager cannot update or remove)")
    ap.add_argument("--list-files", metavar="NAME", help="Show every file a mod installed — its real footprint, which is not named after the mod and spans several roots")
    ap.add_argument("--which", metavar="PATH", help="Reverse lookup: which mod owns this file, path or CET folder name (turns an unidentifiable folder like NCestate back into a Nexus mod)")
    ap.add_argument("--check-missing", action="store_true", help="List files the manifest owns that are gone from disk (a hand-removal the manifest never learned about)")
    ap.add_argument("--check-shared-modules", action="store_true", help="List redscript modules declared by more than one mod (removing one contributor deletes types the others use)")
    ap.add_argument("--check-scale", action="store_true", help="Check performance mods whose shipped budgets (heap, pool sizes) are sized for a much smaller collection than this one")
    ap.add_argument("--set-scale", nargs=2, metavar=("MOD", "VALUE"), help="Set a scale-dependent performance setting and pin the file so the next deploy cannot revert it")
    ap.add_argument("--check-dangling", action="store_true", help="List companion/patch files (TSU_mods, AMM Collabs) that target a mod which is no longer installed — dead code that still runs")
    ap.add_argument("--prune-dangling", action="store_true", help="Remove the companion/patch files reported by --check-dangling (moved to .mod_trash)")
    ap.add_argument("--check-redscript", action="store_true", help="Report the game's own last redscript compile (reads r6/logs/redscript_rCURRENT.log; never runs scc)")
    ap.add_argument("--redscript-baseline", action="store_true", help="Record the current redscript log as the known-good reference, so later checks report a delta instead of a raw count")
    ap.add_argument("--ignore-running", action="store_true", help="Proceed even if Cyberpunk2077.exe/scc.exe is running (unsafe: they rewrite the script cache and mod configs)")
    ap.add_argument("--apply-heuristics", action="store_true", help="With --fix-duplicates: also apply case-splits that rest on a guess (coherence/mtime/ownership) rather than a version order")
    ap.add_argument("--fix-versions", action="store_true", help="Enforce one version per mod: keep the newest, retire older versions of the same mod (reversible)")
    ap.add_argument("--check-frameworks", action="store_true", help="Report framework versions: installed, stale duplicates, and newer builds cached in Downloads")
    ap.add_argument("--fix-frameworks", action="store_true", help="Enforce newest-only frameworks: deploy the newest cached build, drop stale duplicates (never downgrades)")
    ap.add_argument("--verify", action="store_true", help="Verify archive MD5 hashes against NexusMods (no deployment)")
    ap.add_argument("--scan", action="store_true", help="Recursively scan configured mod roots, group files with TF-IDF + DBSCAN, and track SHA-256 integrity")
    ap.add_argument("--scan-path", action="append", default=[], metavar="PATH", help="With --scan: root to scan recursively; repeat for multiple paths (relative = under game root)")
    ap.add_argument("--scan-eps", type=float, default=0.65, metavar="N", help="With --scan: DBSCAN cosine-distance epsilon (default 0.65)")
    ap.add_argument("--scan-min-samples", type=int, default=1, metavar="N", help="With --scan: DBSCAN minimum neighborhood size (default 1)")
    ap.add_argument("--scan-jobs", type=int, default=4, metavar="N", help="With --scan: parallel SHA-256 workers")
    ap.add_argument("--scan-state", type=Path, default=None, metavar="FILE", help="With --scan: integrity snapshot file (default: per-game file under ~/.config/cp2077_modder/scanner)")
    ap.add_argument("--no-cluster", action="store_true", help="With --scan: group by directory instead of TF-IDF + DBSCAN")
    ap.add_argument("--skip-hash", action="store_true", help="Skip MD5 hash verification during deployment")
    ap.add_argument("--nexus-api-key", metavar="KEY", default=NEXUS_API_KEY, help="NexusMods API Key for fetching mod metadata and hash verification")
    args = ap.parse_args()

    new_cfg = {
        "game_root": str(args.game_root),
        "downloads": str(args.downloads),
        "tmp": str(args.tmp),
        "nexus_api_key": args.nexus_api_key
    }
    if new_cfg != _cfg: save_config(new_cfg)

    game_root: Path = args.game_root
    if not game_root.exists(): sys.exit(f"[!] Game directory not found: {game_root}")

    if args.list_deployed is not None:
        with game_lock(game_root, "--list-deployed", exclusive=False):
            action_list(game_root, args.list_deployed or None)
        return

    if args.remove:
        require_game_idle("--remove", args.ignore_running)
        with game_lock(game_root, "--remove"):
            action_remove(game_root, args.remove, args.dry_run, args.purge)
        return

    if args.remove_from_file:
        try:
            names = [ln.strip() for ln in Path(args.remove_from_file).read_text().splitlines()
                     if ln.strip()]
        except OSError as e:
            sys.exit(f"[!] Could not read name list {args.remove_from_file}: {e}")
        require_game_idle("--remove-from-file", args.ignore_running)
        with game_lock(game_root, "--remove-from-file"):
            action_remove_names(game_root, names, args.dry_run, args.purge)
        return

    if args.check_reqs:
        with game_lock(game_root, "--check-reqs", exclusive=False):
            manifest = load_manifest(game_root)
            check_installed_requirements(game_root, manifest.get("mods", {}))
        return

    if args.check_updates:
        with game_lock(game_root, "--check-updates", exclusive=False):
            action_check_updates(game_root, args.nexus_api_key)
        return

    if args.download_updates is not None:
        only = args.download_updates or None
        with game_lock(game_root, "--download-updates", exclusive=False):
            action_download_updates(game_root, args.downloads, args.nexus_api_key,
                                    only=only, dry_run=args.dry_run)
        return

    if args.check_attribution is not None:
        require_bsdtar()
        with game_lock(game_root, "--check-attribution", exclusive=False):
            action_check_attribution(game_root, args.downloads, args.check_attribution)
        return

    if args.redscript_baseline:
        action_redscript_baseline(game_root)
        return

    if args.check_redscript:
        with game_lock(game_root, "--check-redscript", exclusive=False):
            mf = load_manifest(game_root)
            print("\n=== Redscript Compile (from the game's own log) ===")
            report_redscript_health(game_root, mf)
        return

    if args.check_health:
        # Read-only by construction: a shared lock, no writer, and nothing here opens a
        # file for writing. Held shared so the report can never describe a tree that a
        # concurrent deploy is halfway through rewriting.
        with game_lock(game_root, "--check-health", exclusive=False):
            mf = load_manifest(game_root)
            report_health(game_root, mf.get("mods", {}), mf, args.downloads)
        return

    if args.perf_profile:
        if args.apply and not args.dry_run:
            require_game_idle("--perf-profile --apply", args.ignore_running)
        with game_lock(game_root, "--perf-profile",
                       exclusive=args.apply and not args.dry_run):
            action_perf_profile(game_root, args.dry_run, args.apply, args.purge)
        return

    if args.fetch_names:
        with game_lock(game_root, "--fetch-names", exclusive=False):
            action_fetch_names(game_root, args.nexus_api_key, args.refresh_names)
        return

    if args.list_pins:
        with game_lock(game_root, "--list-pins", exclusive=False):
            action_list_pins(game_root)
        return

    if args.pin or args.unpin:
        with game_lock(game_root, "--pin", exclusive=not args.dry_run):
            print("\n=== Pin ===")
            if args.pin:
                action_pin(game_root, args.pin, args.dry_run)
            if args.unpin:
                action_unpin(game_root, args.unpin, args.dry_run)
        return

    if args.list_disabled:
        with game_lock(game_root, "--list-disabled", exclusive=False):
            action_list_disabled(game_root)
        return

    if args.enable:
        require_game_idle("--enable", args.ignore_running)
        with game_lock(game_root, "--enable", exclusive=not args.dry_run):
            action_enable(game_root, args.enable, args.dry_run)
        return

    if args.disable or args.disable_lua:
        needle = args.disable or args.disable_lua
        scope = "all" if args.disable else "lua"
        require_game_idle("--disable", args.ignore_running)
        with game_lock(game_root, "--disable", exclusive=not args.dry_run):
            action_disable(game_root, needle, args.dry_run, args.purge,
                           args.force_prune, args.downloads, scope)
        return

    if args.prune:
        require_game_idle("--prune", args.ignore_running)
        with game_lock(game_root, "--prune", exclusive=not args.dry_run):
            action_prune(game_root, args.dry_run, args.purge, args.force_prune)
        return

    if args.check_fds is not None:
        # Read-only: counts files and reads this process's own rlimits.
        with game_lock(game_root, "--check-fds", exclusive=False):
            action_check_fds(game_root, int(args.check_fds or 0))
        return

    if args.check_locale is not None:
        # Read-only against the game (it may write the chosen locale to CONFIG_DIR, never
        # into the install).
        with game_lock(game_root, "--check-locale", exclusive=False):
            action_check_locale(game_root, args.check_locale)
        return

    if args.check_cet_links:
        # Read-only: reads .lua sources and lists directories, writes nothing.
        with game_lock(game_root, "--check-cet-links", exclusive=False):
            action_check_cet_links(game_root)
        return

    if args.check_conflicts:
        # Read-only: reads .reds sources and the manifest, writes nothing.
        with game_lock(game_root, "--check-conflicts", exclusive=False):
            action_check_conflicts(game_root)
        return

    if args.check_unmanaged:
        # Read-only: a directory walk and a set difference. Shared lock so it cannot
        # describe a tree a concurrent deploy is halfway through writing.
        with game_lock(game_root, "--check-unmanaged", exclusive=False):
            action_check_unmanaged(game_root)
        return

    if args.list_files:
        # Read-only: the manifest plus a walk of the mod roots.
        with game_lock(game_root, "--list-files", exclusive=False):
            action_list_files(game_root, args.list_files)
        return

    if args.which:
        with game_lock(game_root, "--which", exclusive=False):
            action_which(game_root, args.which)
        return

    if args.check_missing:
        with game_lock(game_root, "--check-missing", exclusive=False):
            action_check_missing(game_root)
        return

    if args.check_dangling:
        with game_lock(game_root, "--check-dangling", exclusive=False):
            action_check_dangling(game_root)
        return

    if args.check_shared_modules:
        with game_lock(game_root, "--check-shared-modules", exclusive=False):
            action_check_shared_modules(game_root)
        return

    if args.check_scale:
        with game_lock(game_root, "--check-scale", exclusive=False):
            action_check_scale(game_root)
        return

    if args.set_scale:
        require_game_idle("--set-scale", args.ignore_running)
        try:
            scale_value = float(args.set_scale[1])
        except ValueError:
            sys.exit(f"[!] --set-scale VALUE must be a number, got {args.set_scale[1]!r}")
        with game_lock(game_root, "--set-scale", exclusive=not args.dry_run):
            action_set_scale(game_root, args.set_scale[0],
                             int(scale_value) if scale_value.is_integer() else scale_value,
                             args.dry_run)
        return

    if args.prune_dangling:
        require_game_idle("--prune-dangling", args.ignore_running)
        with game_lock(game_root, "--prune-dangling", exclusive=not args.dry_run):
            action_prune_dangling(game_root, args.dry_run, args.purge)
        return

    if args.fix_duplicates:
        require_game_idle("--fix-duplicates", args.ignore_running)
        with game_lock(game_root, "--fix-duplicates", exclusive=args.dry_run is False):
            action_fix_duplicates(game_root, args.dry_run, args.apply_heuristics)
        return

    if args.fix_versions:
        require_game_idle("--fix-versions", args.ignore_running)
        with game_lock(game_root, "--fix-versions", exclusive=args.dry_run is False):
            action_fix_versions(game_root, args.dry_run)
        return

    if args.check_frameworks:
        with game_lock(game_root, "--check-frameworks", exclusive=False):
            action_check_frameworks(game_root, args.downloads)
        return

    if args.fix_frameworks:
        require_bsdtar()
        require_game_idle("--fix-frameworks", args.ignore_running)
        args.tmp.mkdir(parents=True, exist_ok=True)
        with game_lock(game_root, "--fix-frameworks", exclusive=args.dry_run is False):
            action_fix_frameworks(game_root, args.downloads, args.tmp,
                                  "" if args.skip_hash else args.nexus_api_key,
                                  args.dry_run, args.verbose)
        return

    if args.scan:
        if not 0 < args.scan_eps < 1:
            sys.exit("[!] --scan-eps must be > 0 and < 1 (cosine distance)")
        if args.scan_min_samples < 1:
            sys.exit("[!] --scan-min-samples must be >= 1")
        if args.scan_jobs < 1:
            sys.exit("[!] --scan-jobs must be >= 1")
        # Read-only against the install; the only write is the scanner snapshot under
        # ~/.config (or --scan-state), so a shared game lock is sufficient.
        with game_lock(game_root, "--scan", exclusive=False):
            action_scan(game_root, args.dry_run, cluster=not args.no_cluster,
                        scan_paths=args.scan_path or None, eps=args.scan_eps,
                        min_samples=args.scan_min_samples, jobs=args.scan_jobs,
                        state_path=args.scan_state)
        return

    if not args.downloads.exists(): sys.exit(f"[!] Downloads directory not found: {args.downloads}")

    if args.verify:
        selectors = list(args.selectors) + list(args.only)
        action_verify_archives(args.downloads, selectors, args.nexus_api_key)
        return

    # Deploy path: hold the install lock for the WHOLE run — extract, write, manifest
    # rewrite and the health scan are one transaction as far as other processes go.
    require_bsdtar()
    require_game_idle("deploy", args.ignore_running)
    with game_lock(game_root, "deploy", exclusive=not args.dry_run):
        _run_deploy(args, game_root)

def _run_deploy(args, game_root: Path):
    """The deploy pipeline. Split out of main() so the whole of it runs inside one
    game_lock() context rather than leaving the tail of a deploy unprotected."""
    args.tmp.mkdir(parents=True, exist_ok=True)

    selectors = list(args.selectors) + list(args.only)
    archives, superseded, ambiguous = find_archives(args.downloads, selectors, keep_superseded=args.keep_superseded)
    if not archives:
        print("No matching archives found." if selectors else "No archives found in Downloads folder.")
        return

    partial = bool(selectors)
    manifest = {"mods": {}} if args.no_manifest else load_manifest(game_root)
    skipped = []

    # The reinstall loop, cut here. Removing a mod dropped its manifest entry but left the
    # archive in Downloads, so the next deploy read "not installed" and put it straight back.
    # Intent has to be consulted BEFORE anything is extracted, not cleaned up afterwards.
    disabled = {} if args.no_manifest else load_disabled(game_root)
    if disabled:
        blocked = [a for a in archives if is_disabled(a.stem, disabled)]
        if blocked:
            archives = [a for a in archives if not is_disabled(a.stem, disabled)]
            print(f"Skipping {len(blocked)} archive(s) for mods you disabled "
                  "(--enable <name> to bring one back):")
            for a in blocked[:8]:
                print(f"  [off ] {a.name}")
            if len(blocked) > 8:
                print(f"  ... and {len(blocked) - 8} more")

    # Framework policy: never install an OLDER framework than what's already deployed, and
    # always (re)deploy the newest framework even if its files look present on disk — an
    # older build may be sitting there, and file-existence can't tell versions apart.
    fw_force: set = set()
    fw_blocked = []
    if not args.no_manifest:
        newest_inst = {mid: e[0] for mid, e in installed_frameworks(manifest["mods"]).items()}
        surviving = []
        for a in archives:
            mid = framework_mod_id(a.stem)
            if mid and mid in FRAMEWORK_MOD_IDS:
                have = newest_inst.get(mid)
                if have and have["epoch"] > framework_version_key(a.stem):
                    fw_blocked.append((a, have))
                    continue
                fw_force.add(a.stem)
            surviving.append(a)
        archives = surviving

    if not args.force and not args.no_manifest:
        keep = []
        for a in archives:
            if a.stem in fw_force:                      # frameworks: always deploy the newest
                keep.append(a)
            elif is_installed(a.stem, manifest["mods"], game_root):
                skipped.append(a)
            else:
                keep.append(a)
        to_process = keep
    else:
        to_process = archives

    print(f"Found {len(archives)} matching archive(s)"
          f"{' (subset)' if partial else ''}"
          f"{'  [DRY RUN]' if args.dry_run else ''}.")

    if superseded:
        print(f"Ignoring {len(superseded)} superseded older version(s)")
        if args.verbose:
            for old, newer in superseded:
                print(f"  [old ] {old.name}\n         superseded by {newer.name}")

    if fw_blocked:
        print(f"Protecting {len(fw_blocked)} framework(s) from downgrade "
              "(a newer build is already installed):")
        for a, have in fw_blocked:
            print(f"  [protect] {framework_name(a.stem)}: keeping v{have['ver']}, "
                  f"ignoring older {a.name}")

    if ambiguous:
        print(f"\n[!] {len(ambiguous)} download(s) share a mod name + Nexus id with a newer one but contain DIFFERENT files:")
        for old, newer, lost in ambiguous:
            print(f"  {old.name}\n     has files the newer '{newer.name}' does not:")
            for f in lost[:3]: print(f"        {f}")
            if len(lost) > 3: print(f"        ... and {len(lost) - 3} more")
        print()

    if skipped:
        print(f"Skipping {len(skipped)} already installed (use --force to re-deploy)")
        if args.verbose:
            for a in skipped: print(f"  [skip] {a.stem}")

    print(f"Game root: {game_root}")

    if not to_process:
        print("\nNothing to deploy — every matching mod is already installed.")
        return

    print(f"Deploying {len(to_process)} mod(s).\n")
    deployer = Deployer(game_root, dry_run=args.dry_run, verbose=args.verbose, manifest_mods=manifest.get("mods", {}))
    processing = {a.stem for a in to_process}
    deployer.batch_names = processing        # a framework queued this run counts as present
    if not args.no_manifest: deployer.seed(manifest["mods"], skip=processing)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    scratch = args.tmp / TMP_DIR_NAME
    force_rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)

    effective_api_key = args.nexus_api_key if not args.skip_hash else ""

    t0 = time.monotonic()
    try: fail_extract, hash_results = run_deploy(to_process, deployer, max(1, args.jobs), scratch, effective_api_key)
    finally: force_rmtree(scratch)
    dt = time.monotonic() - t0

    if not args.no_manifest and not args.dry_run:
        merged = dict(manifest["mods"])
        new_ownership = deployer.ownership(restrict=processing)
        merged.update(new_ownership)
        # A path has exactly one owner: whoever wrote it last. Without this, an entry from an
        # earlier run keeps claiming paths this run just overwrote, and over time is whittled
        # down to files that are not its own — the mis-attributed entry in the bug report.
        released = _release_stale_claims(merged, new_ownership)
        # One version per mod: a deploy adds a NEW manifest key (the archive stem), so
        # without this the previous version's entry and its files would both survive and
        # keep claiming the same paths. Retiring it here — after the new files are safely
        # written — makes an update a real replace instead of an accumulate.
        ver_actions = dedupe_mod_versions(game_root, merged, dry_run=False)
        # Framework policy: after writing this run's newest builds, drop any stale duplicate
        # framework entry so a later run can't re-deploy the old files (the described bug).
        fw_actions = dedupe_framework_manifest(game_root, merged, dry_run=False)
        save_manifest(game_root, merged)
        # Deploy is a sync, not an append: finish any uninstall the user asked for. Runs
        # AFTER the manifest is saved, so a crash mid-prune leaves a consistent manifest and
        # the next run simply resumes. Files go to .mod_trash like every other removal here.
        prune_plan = {"stems": []}
        if disabled and not getattr(args, "no_prune", False):
            prune_plan = sync_prune(game_root, merged, disabled, dry_run=False,
                                    purge=args.purge, force=getattr(args, "force_prune", False))
        if released:
            dropped = [(m, n) for m, n, gone in released if gone]
            print("\n=== Ownership Reconciliation ===")
            for stem, n, gone in released:
                if not gone:
                    print(f"  [i] {stem}: released {n} path(s) now owned by a mod deployed "
                          "this run")
            for stem, n in dropped:
                print(f"  [✓] {stem}: all {n} of its files are now owned by other mods — "
                      "entry removed (it was being reported as installed)")
        report_prune_plan(prune_plan, dry_run=False)
        if ver_actions:
            print("\n=== Mod Version Policy (one version per mod) ===")
            for act in ver_actions:
                print(f"  [✓] mod {act['mod_id']}: kept v{act['keep_ver']}, retired "
                      f"v{act['drop_ver']} ({act['shared']} shared path(s) released"
                      + (f", {act['orphans_moved']} stale file(s) archived"
                         if act['orphans_moved'] else "") + ")")
        if fw_actions:
            print("\n=== Framework Version Policy ===")
            for act in fw_actions:
                extra = (f", archived {act['orphans_moved']} stale file(s)"
                         if act['orphans_moved'] else "")
                print(f"  [✓] {act['framework']}: kept v{act['keep_ver']}, "
                      f"removed stale v{act['drop_ver']}{extra}")

    # Requirement roll-up for the mods installed this run — advisory, and shown on
    # dry runs too (projecting the manifest plus everything queued this batch).
    installed_names = set(manifest["mods"]) | set(deployer.requirements) | processing
    deployer.report_requirements(installed_names)

    # Integrity detector — run over the actual on-disk tree after every deploy so a
    # case-duplicate module folder (the classic redscript SYM_REDEFINITION cause) is
    # caught the moment a collection finishes installing, not at the next game launch.
    _mf = load_manifest(game_root)
    report_health(game_root, _mf.get("mods", {}), _mf, args.downloads)

    verified_n = sum(1 for s in hash_results.values() if s == 'verified')
    mismatch_n = sum(1 for s in hash_results.values() if s == 'mismatch')
    unknown_n  = sum(1 for s in hash_results.values() if s in ('unknown', 'no_key', 'error'))

    if hash_results and not args.skip_hash:
        print("\n=== Hash Verification ===")
        print(f"Verified (match) : {verified_n}")
        print(f"Mismatch         : {mismatch_n}")
        print(f"Unknown/Skipped  : {unknown_n}")
        if mismatch_n > 0:
            print("\n  [✗] WARNING: The following archives did NOT match any NexusMods file hash:")
            for mod_name, status in hash_results.items():
                if status == 'mismatch': print(f"      - {mod_name}")
            print("  This may indicate a corrupted download, a re-upload by the author,")
            print("  or a manually repacked archive. Re-download from NexusMods if unsure.")

    # Anything the deploy deliberately did NOT write. Reported even when empty is boring, so
    # only when it happened — but it must be reported, because "your edit survived" and "your
    # edit was silently reverted" look identical from the outside otherwise.
    if deployer.pinned_skipped or deployer.preserved or deployer.backed_up:
        print("\n=== Protected files (not overwritten) ===")
        if deployer.pinned_skipped:
            print(f"  [✓] {len(deployer.pinned_skipped)} pinned file(s) left as you have them:")
            for k in deployer.pinned_skipped[:8]:
                print(f"      - {k}")
            if len(deployer.pinned_skipped) > 8:
                print(f"      ... and {len(deployer.pinned_skipped) - 8} more")
        if deployer.preserved:
            print(f"  [✓] {len(deployer.preserved)} runtime-state file(s) preserved (shared "
                  "config stores — overwriting these wipes your in-game settings):")
            for k in deployer.preserved[:8]:
                print(f"      - {k}")
            if len(deployer.preserved) > 8:
                print(f"      ... and {len(deployer.preserved) - 8} more")
        if deployer.backed_up:
            where = f"{OVERWRITE_BACKUP_DIRNAME}/{deployer.run_stamp}/"
            verb = "would be copied" if args.dry_run else "copied"
            print(f"  [i] {len(deployer.backed_up)} changed file(s) {verb} to {where} before "
                  "being overwritten — your version is recoverable:")
            for k in deployer.backed_up[:8]:
                print(f"      - {k}")
            if len(deployer.backed_up) > 8:
                print(f"      ... and {len(deployer.backed_up) - 8} more")

    conflicts = deployer.conflicts()
    print("\n=== Conflict report ===")
    if not conflicts: print("No file conflicts among the processed mods.")
    else:
        print(f"{len(conflicts)} game file(s) written by more than one mod (last listed wins):")
        def winner_chain(key):
            order = []
            for m in conflicts[key]:
                if not order or order[-1] != m: order.append(m)
            return order
        for shown, key in enumerate(sorted(conflicts)):
            if shown >= 40:
                print(f"  ... and {len(conflicts) - shown} more.")
                break
            print(f"  {key}\n    {'  ->  '.join(winner_chain(key))}")
        if args.conflict_report:
            with open(args.conflict_report, 'w') as fh:
                for key in sorted(conflicts):
                    order = winner_chain(key)
                    fh.write(f"{key}\n")
                    for i, m in enumerate(order): fh.write(f"    {'WINS' if i == len(order) - 1 else 'lost'}  {m}\n")
            print(f"  Full list written to: {args.conflict_report}")

    print("\n=== Deployment Summary ===")
    print(f"Archives processed : {len(to_process)} of {len(archives)} matched")
    print(f"Superseded (older) : {len(superseded)}")
    print(f"Skipped (installed): {len(skipped)}")
    print(f"Mods deployed      : {deployer.deployed_mods}" + (" (dry run — nothing written)" if args.dry_run else ""))
    print(f"No structure/empty : {deployer.empty_mods}")
    print(f"Extraction failures: {fail_extract}")
    print(f"Conflicted files   : {len(conflicts)}")
    print(f"Elapsed            : {dt:.1f}s")
    if not args.no_manifest and not args.dry_run: print(f"Manifest           : {game_root / MANIFEST_NAME}")
    print(f"Log File           : {CONFIG_DIR / 'mod_deployer.log'}")

if __name__ == "__main__":
    main()
