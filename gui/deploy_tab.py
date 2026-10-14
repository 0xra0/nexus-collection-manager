"""Tab 2 — extract & deploy downloaded archives into the game (nexus_core.deployer + bsdtar)."""
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton,
    QSpinBox, QCheckBox, QProgressBar, QMessageBox, QGroupBox,
)

from .config import DEF_DOWNLOADS, DEF_GAME_ROOT
from .parsing import RE_PROCESS
from .runner import ProcRunner, backend, fmt_argv
from .widgets import BrowseEdit, LogView
from .nexus_credentials import bind_api_key_edit

# Matches PERF_ONUPDATE_WARN in nexus_core.deployer — chosen (not measured) to sit
# well above the 28 per-frame mods that ran fine and well below the 143 that did not.
PERF_WARN = 40

# Missing-requirement lines from the deployer roll-up: "  - RED4ext   (needed by: Mod X)"
RE_MISSING_REQ = re.compile(r"^\s*-\s+(.+?)\s+\(needed by:\s*(.+?)\)\s*$")
# Health check: the machine summary and each collision item line.
# `heuristic-only` counts collisions the fixer deliberately will NOT touch without an
# explicit opt-in, because no version order settles them (see --apply-heuristics).
RE_HEALTH_SUMMARY = re.compile(
    r"^\[health\]\s+(\d+)\s+case-duplicate.*?;\s+(\d+)\s+auto-fixable;"
    r"(?:\s+(\d+)\s+heuristic-only;)?\s+(\d+)\s+in redscript")
RE_HEALTH_ITEM = re.compile(r"^\s*-\s+(\S.*?)/\s+\[(.+?)\]\s+→\s+(.+?)\s*$")
# Redscript verdict from the GAME's own log — never from a standalone scc run, whose raw
# error count is meaningless (it compiles without the game's native types). `delta` is n/a
# until a baseline is captured, and only a delta is worth alarming about.
RE_REDSCRIPT = re.compile(
    r"^\[redscript\]\s+(no-log|stale|ok|failed)(?:;\s+errors=(\d+);\s+delta=(n/a|-?\d+))?")
# Files on disk under a managed root that no manifest entry owns — hand-installed mods.
# Informational, never a failure: the user may have put them there on purpose.
RE_UNMANAGED = re.compile(r"^\[unmanaged\]\s+(\d+)\s+unmanaged mod unit\(s\);\s+(\d+)\s+payload")
# Redscript modules with more than one contributing mod. Parsed and kept, but deliberately
# NOT given a chain entry: it is legal, unfixable, and the actionable half of it is the
# uninstall refusal in the deployer, not a standing warning here.
RE_SHARED_MODULES = re.compile(
    r"^\[shared-modules\]\s+(\d+)\s+module\(s\) declared by more than one mod")
# A perf mod whose shipped budget is sized for a far smaller collection, so it sits pinned
# in its most aggressive state forever. Correctness, NOT frames — the measured result was
# that raising it changed the mod's behaviour and left the frame rate untouched.
RE_SCALE = re.compile(
    r"^\[scale\]\s+(\d+)\s+performance setting\(s\) below what this install needs")
# A patch file for a mod that is gone, shipped by a mod that stays — the one finding that
# survives a correct uninstall. Dead code that still runs, so it costs frame time, not just
# tidiness.
RE_DANGLING = re.compile(
    r"^\[dangling\]\s+(\d+)\s+companion file\(s\) targeting a mod that is not installed")
# The mirror image: paths the manifest owns that are gone from disk. Also informational —
# the user deleted them — but it means the manifest now overstates what is installed.
RE_MISSING = re.compile(
    r"^\[missing\]\s+(\d+)\s+mod\(s\) with\s+(\d+)\s+owned file\(s\) gone from disk")
# Several mods fighting over one subsystem. Advisory: which mod should win is the user's
# call, so this informs and never offers to change anything.
RE_CONFLICTS = re.compile(
    r"^\[conflicts\]\s+(\d+)\s+contested subsystem\(s\);\s+(\d+)\s+contested method\(s\)")
# A CET mod looking up a companion by a folder name nobody provides any more. `broken` counts
# only the confirmed cases (the companion IS installed, under another name); a companion that
# simply isn't installed is normal and is not counted.
RE_CET_LINKS = re.compile(
    r"^\[cet-links\]\s+(\d+)\s+broken cross-mod lookup\(s\);\s+(\d+)\s+companion")
# A CET mod probing a /language config var the engine does not define. This is the only
# advisory finding that is a HARD CRASH — a C++ engine assert no pcall can catch — so it
# outranks every other one in the summary.
RE_LOCALE = re.compile(
    r"^\[locale\]\s+(\d+)\s+invalid /language probe\(s\);\s+(\d+)\s+mod\(s\) missing")
# Estimated open file descriptors at play time vs ulimit -n. Capacity, not correctness —
# nothing is broken yet, which is exactly why it needs saying before the wall is hit.
RE_FDS = re.compile(r"^\[fds\]\s+~(\d+)\s+of\s+(\d+)\s+\((\d+)%\)")
# Mods the user disabled. `pending` counts files still on disk — an uninstall that was
# started and never finished, which means the game is still loading them.
RE_DISABLED = re.compile(
    r"^\[disabled\]\s+(\d+)\s+mod\(s\) disabled;\s+(\d+)\s+file\(s\) awaiting")
# Files the user pinned so deploy never overwrites them. `changed` means something got past
# the protection, or they edited it again — either way worth surfacing.
RE_PINS = re.compile(r"^\[pins\]\s+(\d+)\s+pinned file\(s\);\s+(\d+)\s+changed")
# CET mods running Lua every frame, all on the game's single main thread. Measured to halve
# the frame rate on this collection. The counts are a SCALE signal only, never a per-mod
# ranking — a cheap early-out handler is indistinguishable from an expensive one here.
RE_PERF = re.compile(
    r"^\[perf\]\s+(\d+)\s+per-frame mod\(s\) of\s+(\d+);\s+(\d+)\s+hook")
# Update check: one machine [update] line per updatable mod carrying everything a one-click
# download needs (mod_id, file_id, size, the supersede-friendly download name), then the
# [updates] summary count. Parsed instead of the human bullets so the GUI can act, not just
# display. [downloaded] closes a one-click fetch.
RE_UPDATE_ROW = re.compile(
    r"^\[update\]\s+(?P<stem>.+?)\s+\|\s+mod_id=(?P<mod>\d+)\s+file_id=(?P<file>\d+)\s+"
    r"size=(?P<size>\d+)\s+v(?P<iv>.+?)\s+->\s+v(?P<lv>.+?)\s+\|\s+dl=(?P<dl>.+?)\s*$")
RE_UPDATES = re.compile(
    r"^\[updates\]\s+(\d+)\s+installed mod\(s\) have a newer version on Nexus")
RE_DOWNLOADED = re.compile(
    r"^\[downloaded\]\s+(\d+)\s+of\s+(\d+)\s+update\(s\) fetched")


class DeployTab(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.runner = None
        self._missing_reqs = []
        self._updates = []              # dict rows from [update] lines (carry download info)
        self._downloaded = None         # (ok, total) from a [downloaded] line
        self._health = []               # (path, tag, planned action) per collision
        self._health_counts = (0, 0, 0, 0)  # (total, auto-fixable, heuristic-only, in-redscript)
        self._redscript = None           # (state, errors, delta) from the game's own log
        self._unmanaged = (0, 0)         # (mod units, payload files) owned by no entry
        self._missing = (0, 0)           # (mods, owned files) the manifest claims but disk lacks
        self._dangling = 0               # companion/patch files targeting an absent mod
        self._scale = 0                  # perf settings sized for a smaller collection
        self._shared_modules = 0         # reds modules with more than one contributing mod
        self._conflicts = (0, 0)         # (contested subsystems, contested reds methods)
        self._cet_links = (0, 0)         # (confirmed broken GetMod lookups, absent companions)
        self._locale = (0, 0)            # (invalid /language probes, mods missing our locale)
        self._fds = None                 # (estimate, limit, percent) or None if not reported
        self._disabled = (0, 0)          # (disabled mods, files still awaiting removal)
        self._pins = (0, 0)              # (pinned files, pinned files that no longer match)
        self._perf = (0, 0, 0)           # (per-frame mods, total CET mods, Observe/Override hooks)
        self.total = 0

        self.downloads = BrowseEdit(cfg.value("dp/downloads", DEF_DOWNLOADS), "dir")
        self.game_root = BrowseEdit(cfg.value("dp/game_root", DEF_GAME_ROOT), "dir")
        self.selectors = QLineEdit(cfg.value("dp/selectors", ""))
        self.selectors.setPlaceholderText("optional: names / globs to limit (space separated)")
        self.api_key   = QLineEdit()
        bind_api_key_edit(self.api_key, cfg)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("optional NexusMods API key (deployer hash/metadata)")

        self.jobs = QSpinBox(); self.jobs.setRange(1, 16)
        self.jobs.setValue(int(cfg.value("dp/jobs", 4)))
        self.force     = QCheckBox("Force re-deploy installed mods")
        self.verbose   = QCheckBox("Verbose (per-file conflicts)")
        self.keep_old  = QCheckBox("Keep superseded older versions")
        self.skip_hash = QCheckBox("Skip hash verification"); self.skip_hash.setChecked(True)

        form = QFormLayout()
        form.addRow("Downloads folder:", self.downloads)
        form.addRow("Game root:", self.game_root)
        form.addRow("Selectors:", self.selectors)
        form.addRow("Nexus API key:", self.api_key)
        jrow = QHBoxLayout(); jrow.addWidget(QLabel("Jobs:")); jrow.addWidget(self.jobs)
        jrow.addStretch(1)
        jw = QWidget(); jw.setLayout(jrow)
        form.addRow("Parallel extract:", jw)
        form.addRow("", self.force)
        form.addRow("", self.verbose)
        form.addRow("", self.keep_old)
        form.addRow("", self.skip_hash)

        self.dry_btn    = QPushButton("Dry run")
        self.deploy_btn = QPushButton("Deploy")
        self.deploy_btn.setProperty("accent", True)     # green primary action
        self.list_btn   = QPushButton("List deployed")
        self.updates_btn = QPushButton("Check updates")
        self.health_btn = QPushButton("Health check")
        self.stop_btn   = QPushButton("Stop"); self.stop_btn.setEnabled(False)
        self.dry_btn.clicked.connect(lambda: self.start(dry=True))
        self.deploy_btn.clicked.connect(lambda: self.start(dry=False))
        self.list_btn.clicked.connect(self.list_deployed)
        self.updates_btn.clicked.connect(self.check_updates)
        self.health_btn.clicked.connect(self.check_health)
        self.stop_btn.clicked.connect(self.stop)
        btns = QHBoxLayout()
        for b in (self.dry_btn, self.deploy_btn, self.list_btn, self.updates_btn,
                  self.health_btn, self.stop_btn):
            btns.addWidget(b)
        btns.addStretch(1)

        self.bar = QProgressBar(); self.bar.setRange(0, 100)
        self.status = QLabel("Idle."); self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.log = LogView()

        lay = QVBoxLayout(self)
        box = QGroupBox("Extract & deploy into the game folder")
        box.setLayout(form)
        lay.addWidget(box)
        lay.addLayout(btns)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        lay.addWidget(self.log, 1)

    def start(self, dry):
        if self.runner:
            return
        if not Path(self.game_root.text()).exists():
            QMessageBox.warning(self, "Missing", f"Game root not found:\n{self.game_root.text()}")
            return
        argv = backend("deployer")
        argv += self.selectors.text().split()
        argv += ["--game-root", self.game_root.text(),
                 "--downloads", self.downloads.text(),
                 "--jobs", str(self.jobs.value())]
        if dry:
            argv.append("--dry-run")
        if self.force.isChecked():
            argv.append("--force")
        if self.verbose.isChecked():
            argv.append("--verbose")
        if self.keep_old.isChecked():
            argv.append("--keep-superseded")
        if self.skip_hash.isChecked() or not self.api_key.text().strip():
            argv.append("--skip-hash")
        if self.api_key.text().strip():
            argv += ["--nexus-api-key", self.api_key.text().strip()]
        self._run(argv, f"{'Dry run' if dry else 'Deploy'} started…")

    def list_deployed(self):
        if self.runner:
            return
        self._run(backend("deployer") + ["--game-root", self.game_root.text(),
                  "--list-deployed"], "Listing deployed mods…")

    def check_updates(self):
        if self.runner:
            return
        if not Path(self.game_root.text()).exists():
            QMessageBox.warning(self, "Missing", f"Game root not found:\n{self.game_root.text()}")
            return
        argv = backend("deployer") + ["--game-root", self.game_root.text(), "--check-updates"]
        if self.api_key.text().strip():
            argv += ["--nexus-api-key", self.api_key.text().strip()]
        self._run(argv, "Checking NexusMods for updates…")

    def download_updates(self, only=None):
        """One-click fetch of available updates INTO the Downloads folder. Deploy stays a
        separate step (that is the game folder). `only` = a stem/mod-id to fetch just one."""
        if self.runner:
            return
        if not Path(self.game_root.text()).exists():
            QMessageBox.warning(self, "Missing", f"Game root not found:\n{self.game_root.text()}")
            return
        argv = backend("deployer") + ["--game-root", self.game_root.text(),
                                      "--downloads", self.downloads.text()]
        argv += ["--download-updates"] + ([only] if only else [])
        if self.api_key.text().strip():
            argv += ["--nexus-api-key", self.api_key.text().strip()]
        self._run(argv, "Downloading available updates into Downloads…")

    def check_health(self):
        if self.runner:
            return
        if not Path(self.game_root.text()).exists():
            QMessageBox.warning(self, "Missing", f"Game root not found:\n{self.game_root.text()}")
            return
        self._run(backend("deployer") + ["--game-root", self.game_root.text(), "--check-health"],
                  "Scanning installed mods for case-duplicate folders…")

    def fix_duplicates(self, dry=False):
        if self.runner:
            return
        argv = backend("deployer") + ["--game-root", self.game_root.text(), "--fix-duplicates"]
        if dry:
            argv.append("--dry-run")
        self._run(argv, "Resolving case-duplicate folders…")

    def _run(self, argv, status):
        self.total = 0
        self._missing_reqs = []          # (requirement, needed-by) captured from output
        self._updates = []               # dict rows from [update] lines (carry download info)
        self._downloaded = None          # (ok, total) captured from a [downloaded] line
        self._health = []                # (path, tag, action) captured from output
        self._health_counts = (0, 0, 0, 0)
        self._redscript = None
        self._unmanaged = (0, 0)
        self._missing = (0, 0)
        self._dangling = 0
        self._scale = 0
        self._shared_modules = 0
        self._conflicts = (0, 0)
        self._cet_links = (0, 0)
        self._locale = (0, 0)
        self._fds = None
        self._disabled = (0, 0)
        self._pins = (0, 0)
        self._perf = (0, 0, 0)
        self.bar.setValue(0)
        self.status.setText(status)
        self.log.clear()
        self.log.add("$ " + fmt_argv(argv))
        self._set_running(True)
        self.runner = ProcRunner(argv)
        self.runner.line.connect(self._on_line)
        self.runner.finished_rc.connect(self._on_done)
        self.runner.start()

    def stop(self):
        if self.runner:
            self.status.setText("Stopping…")
            self.runner.stop()
            self.stop_btn.setEnabled(False)

    def _set_running(self, running):
        for b in (self.dry_btn, self.deploy_btn, self.list_btn, self.updates_btn, self.health_btn):
            b.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def _on_line(self, text):
        self.log.add(text)
        m = RE_PROCESS.match(text)
        if m:
            i, total = int(m.group(1)), int(m.group(2))
            self.total = total
            self.bar.setValue(int(i / total * 100) if total else 0)
            self.status.setText(f"[{i}/{total}] {m.group(3)}")
            return
        rq = RE_MISSING_REQ.match(text)
        if rq:
            self._missing_reqs.append((rq.group(1).strip(), rq.group(2).strip()))
            return
        rs = RE_REDSCRIPT.match(text)
        if rs:
            self._redscript = (rs.group(1), rs.group(2), rs.group(3))
            return
        pf = RE_PERF.match(text)
        if pf:
            self._perf = (int(pf.group(1)), int(pf.group(2)), int(pf.group(3)))
            return
        pn = RE_PINS.match(text)
        if pn:
            self._pins = (int(pn.group(1)), int(pn.group(2)))
            return
        ds = RE_DISABLED.match(text)
        if ds:
            self._disabled = (int(ds.group(1)), int(ds.group(2)))
            return
        fd = RE_FDS.match(text)
        if fd:
            self._fds = (int(fd.group(1)), int(fd.group(2)), int(fd.group(3)))
            return
        lc = RE_LOCALE.match(text)
        if lc:
            self._locale = (int(lc.group(1)), int(lc.group(2)))
            return
        cl = RE_CET_LINKS.match(text)
        if cl:
            self._cet_links = (int(cl.group(1)), int(cl.group(2)))
            return
        cf = RE_CONFLICTS.match(text)
        if cf:
            self._conflicts = (int(cf.group(1)), int(cf.group(2)))
            return
        um = RE_UNMANAGED.match(text)
        if um:
            self._unmanaged = (int(um.group(1)), int(um.group(2)))
            return
        sm = RE_SHARED_MODULES.match(text)
        if sm:
            self._shared_modules = int(sm.group(1))
            return
        sc = RE_SCALE.match(text)
        if sc:
            self._scale = int(sc.group(1))
            return
        dg = RE_DANGLING.match(text)
        if dg:
            self._dangling = int(dg.group(1))
            return
        ms = RE_MISSING.match(text)
        if ms:
            self._missing = (int(ms.group(1)), int(ms.group(2)))
            return
        hs = RE_HEALTH_SUMMARY.match(text)
        if hs:
            self._health_counts = (int(hs.group(1)), int(hs.group(2)),
                                   int(hs.group(3) or 0), int(hs.group(4)))
            return
        hi = RE_HEALTH_ITEM.match(text)
        if hi:
            self._health.append((hi.group(1).strip(), hi.group(2).strip(), hi.group(3).strip()))
            return
        ur = RE_UPDATE_ROW.match(text)
        if ur:
            self._updates.append({
                "stem": ur.group("stem"), "mod_id": ur.group("mod"),
                "file_id": ur.group("file"), "size": int(ur.group("size")),
                "installed_ver": ur.group("iv"), "latest_ver": ur.group("lv"),
                "download_name": ur.group("dl"),
            })
            return
        us = RE_UPDATES.match(text)
        if us:
            return                                # summary count; rows already captured
        dn = RE_DOWNLOADED.match(text)
        if dn:
            self._downloaded = (int(dn.group(1)), int(dn.group(2)))
            return

    def _on_done(self, rc):
        self._set_running(False)
        self.runner = None
        if rc != 0:
            self.status.setText(f"Exited with code {rc}. See log above.")
            return
        self.bar.setValue(100)
        # A failed compile means NO script mod loaded, which outranks every other finding —
        # report it before the case-duplicate dialog. Only 'failed' is alarming: 'ok' with a
        # nonzero error count is normal, and a raw count is not a health score.
        if self._redscript and self._redscript[0] == "failed":
            _state, errors, delta = self._redscript
            extra = (f"\n\n{errors} error line(s) logged"
                     + (f", {delta} more than your baseline." if delta not in (None, "n/a")
                        else ". Capture a baseline (--redscript-baseline) from a known-good "
                             "launch to make this number comparable."))
            QMessageBox.critical(
                self, "Redscript compile failed",
                "The game's last redscript compile did not finish, so NO script mods "
                "loaded.\n\nThis verdict comes from the game's own log "
                "(r6/logs/redscript_rCURRENT.log) — see the log above for the top error "
                "codes." + extra)
        total_h, fixable_h, guess_h, reds_h = self._health_counts
        if total_h:
            lines = "\n".join(f"• {path}/  [{tag}]\n    → {action}"
                              for path, tag, action in self._health)
            self.status.setText(
                f"Done — {total_h} case-duplicate folder(s), {fixable_h} auto-fixable (see log).")
            reds_note = ("\nAt least one is in the redscript tree — that aborts redscript "
                         "compilation, so ALL script mods fail to load until it is fixed.\n"
                         if reds_h else "")
            # Cases with no version order behind them are NOT offered here. Choosing a side
            # on a guess is what archived Codeware's live API and broke the compile; those
            # need a deliberate --apply-heuristics run, not a Yes/No prompt after a scan.
            guess_note = (f"\n{guess_h} further case(s) cannot be settled by version order. "
                          "They are listed in the log and left untouched — resolving those "
                          "means choosing which copy the compiler reads, which is a guess.\n"
                          if guess_h else "")
            if fixable_h:
                r = QMessageBox.question(
                    self, "Case-duplicate folders found",
                    f"{total_h} case-duplicate folder(s) were found in the game install; "
                    f"{fixable_h} can be resolved automatically.\n{reds_note}{guess_note}\n"
                    f"{lines}\n\n"
                    "Resolve them now? Stray/older copies are moved to a reversible backup "
                    "(_removed_duplicate_mods/), never deleted.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if r == QMessageBox.StandardButton.Yes:
                    self.fix_duplicates()
            else:
                QMessageBox.warning(
                    self, "Case-duplicate folders found",
                    f"{total_h} case-duplicate folder(s) need manual resolution:"
                    f"\n{guess_note}\n{lines}")
            return
        if self._downloaded is not None:
            # A one-click fetch just ran. The files are in Downloads named for supersede, so
            # the only thing left is a deploy — never done automatically (that is the game
            # folder), always the user's explicit next step.
            ok, total = self._downloaded
            if total == 0:
                self.status.setText("Done — no updates to download.")
            elif ok == total:
                self.status.setText(
                    f"Done — fetched {ok} update(s) to Downloads. Deploy to apply.")
                QMessageBox.information(
                    self, "Updates downloaded",
                    f"{ok} update(s) were fetched into your Downloads folder, each named so "
                    "a deploy retires the old version automatically.\n\nRun Deploy on the "
                    "Install tab to apply them.")
            else:
                self.status.setText(
                    f"Done — fetched {ok} of {total} update(s); {total - ok} failed (see log).")
                QMessageBox.warning(
                    self, "Some updates failed",
                    f"{ok} of {total} update(s) downloaded; {total - ok} failed — see the log.\n\n"
                    "The ones that succeeded are in Downloads; deploy to apply them.")
            return
        if self._updates:
            n = len(self._updates)
            self.status.setText(f"Done — {n} update(s) available (see log).")
            lines = "\n".join(f"• {r['stem']}\n    v{r['installed_ver']} → v{r['latest_ver']}"
                              for r in self._updates)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("Updates available")
            box.setText(f"{n} installed file(s) have a newer version on NexusMods.")
            box.setInformativeText(
                "Download them now into your Downloads folder, then Deploy (Install tab) to "
                "apply — each is named so the deploy retires the old version automatically.\n\n"
                + lines)
            dl_btn = box.addButton("Download now", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is dl_btn:
                self.download_updates()
        elif self._missing_reqs:
            self.status.setText(f"Done — {len(self._missing_reqs)} requirement(s) missing (see log).")
            lines = "\n".join(f"• {dep}\n    needed by: {who}" for dep, who in self._missing_reqs)
            QMessageBox.warning(
                self, "Missing requirements",
                "These mods installed, but they need framework(s) that aren't present.\n"
                "Install these too (via the Download/Install tabs) or the mods won't work:\n\n"
                + lines)
        else:
            units, payload = self._unmanaged
            missing_mods, missing_files = self._missing
            domains, meths = self._conflicts
            cet_broken, _cet_absent = self._cet_links
            bad_lang, locale_gaps = self._locale
            if bad_lang:
                # First in the chain: everything else here degrades a feature, this one ends
                # the process. And it only fires for players whose locale takes the fallback
                # path, so "it works for everyone else" is the expected report.
                self.status.setText(
                    f"Done — {bad_lang} crash-causing /language probe(s) (see log).")
                QMessageBox.critical(
                    self, "Mod will crash the game",
                    f"{bad_lang} CET mod probe(s) ask the engine for a /language config var "
                    "that does not exist.\n\nThat is a C++ engine assert, not a Lua error: "
                    "it aborts the game outright and the mod's own pcall cannot catch it. "
                    "It typically fires seconds into loading a save.\n\n"
                    + (f"{locale_gaps} mod(s) also ship no strings for your locale, which is "
                       "what sends you down the untested branch where these probes live.\n\n"
                       if locale_gaps else "")
                    + "The log names the mod, the file and the correct var.")
            elif cet_broken:
                # Ranked above the conflict warning because it is proof rather than a
                # judgement: the companion mod is installed, the caller cannot see it, and
                # there is no preference involved — the lookup is simply wrong.
                self.status.setText(
                    f"Done — {cet_broken} broken cross-mod CET lookup(s) (see log).")
                QMessageBox.warning(
                    self, "Broken cross-mod lookups",
                    f"{cet_broken} CET mod(s) look up a companion by a folder name that no "
                    "longer exists, usually because the companion renamed its folder.\n\n"
                    "The lookup returns nil forever, so the integration between the two mods "
                    "never runs — and nothing logs an error, because nil normally just means "
                    "'not installed'.\n\nThe log names the caller, the name it wants and the "
                    "name actually installed.")
            elif self._perf[0] >= PERF_WARN:
                # Ranked above the rest: this is the only finding here measured to halve the
                # frame rate, and unlike the others it gets worse with every mod added.
                n_upd, n_total, n_hooks = self._perf
                self.status.setText(
                    f"Done — {n_upd} of {n_total} CET mod(s) run Lua every frame (see log).")
                QMessageBox.warning(
                    self, "Per-frame script cost is high",
                    f"{n_upd} of {n_total} CET mods run Lua on EVERY frame, all of it on the "
                    f"game's single main thread, plus {n_hooks} Observe/Override hooks.\n\n"
                    "Measured on this collection: 143 such mods gave a median 32 fps with "
                    "31.8% of frames under 30. Cutting back to 28 gave a median 68 fps and "
                    "no frames under 30 — the GPU was being starved, not saturated.\n\n"
                    "Run --perf-profile to see which of them no other mod depends on. Note "
                    "these are counts, not measured costs: use them as a scale signal, not "
                    "to pick out one slow mod.")
            elif self._pins[1]:
                # Ranked here because a pin that stopped holding means a fix the user
                # deliberately applied may already be gone, and they will otherwise only
                # find out when the symptom comes back.
                n_pins, n_bad = self._pins
                self.status.setText(
                    f"Done — {n_bad} of {n_pins} pinned file(s) no longer match (see log).")
                QMessageBox.warning(
                    self, "A pinned file changed",
                    f"{n_bad} file(s) you pinned no longer match what was pinned — they were "
                    "overwritten, deleted, or you edited them again.\n\nPinned files are "
                    "meant to be untouchable, so if you did not change them yourself this is "
                    "worth looking at. The log names them.")
            elif self._disabled[1]:
                # An uninstall the user started and that never completed: the mod is still on
                # disk and still being loaded, which is the opposite of what they asked for.
                n_mods, n_files = self._disabled
                self.status.setText(
                    f"Done — {n_mods} disabled mod(s) still have {n_files} file(s) on disk "
                    "(see log).")
                QMessageBox.warning(
                    self, "Unfinished uninstall",
                    f"{n_mods} mod(s) you disabled still have {n_files} file(s) installed, so "
                    "the game is still loading them.\n\nRun a deploy, or --prune, to finish "
                    "removing them. Files are moved to .mod_trash and can be recovered.")
            elif domains or meths:
                # Worth a dialog even though nothing is auto-fixable: the failure it explains
                # is silent. Four camera mods all install cleanly and the camera just stops
                # working, with nothing in any log to connect the two.
                bits = []
                if domains:
                    bits.append(f"{domains} subsystem(s) are claimed by more than one mod")
                if meths:
                    bits.append(f"{meths} redscript method(s) are replaced outright by more "
                                "than one mod, so only one of them is actually running")
                self.status.setText(f"Done — {domains + meths} mod conflict(s) (see log).")
                QMessageBox.information(
                    self, "Conflicting mods",
                    " and ".join(bits) + ".\n\nThese install fine and then quietly fight: "
                    "typically one wins and the others do nothing. See the log for which "
                    "mods, then keep the one you want and remove the rest.\n\nNothing has "
                    "been changed — which mod should win is your call.")
            elif self._scale:
                # Below the per-frame warning on purpose. It is real, but the measured result
                # is that fixing it does not change the frame rate, and ranking it above the
                # finding that DOES would send the user after the wrong thing.
                self.status.setText(
                    f"Done — {self._scale} performance mod setting(s) are sized for a much "
                    "smaller collection (see log).")
            elif self._dangling:
                # Ranked above the other bookkeeping findings because it is not bookkeeping:
                # a stale tooltip patch still wraps every tooltip, so this costs frame time.
                self.status.setText(
                    f"Done — {self._dangling} patch file(s) target a mod that is no longer "
                    "installed (see log).")
            elif missing_mods:
                # Ranked with the other bookkeeping findings: nothing is broken, but the
                # manifest is now claiming files that are not there, so every footprint and
                # removal plan built from it overstates what is really installed.
                self.status.setText(
                    f"Done — {missing_mods} mod(s) claim {missing_files} file(s) that are "
                    "not on disk (see log).")
            elif units:
                # The quietest finding, and the last reported before pure capacity: it breaks
                # nothing today. No dialog — the user may well have put those files there on
                # purpose — but the status line has to say so, or the manager silently claims
                # to manage an install it only half manages.
                self.status.setText(
                    f"Done — {units} hand-installed mod file/folder(s) on disk that this "
                    f"manager does not own ({payload} file(s); see log).")
            elif self._fds and self._fds[2] >= 75:
                # Capacity, not correctness: last in the chain because nothing is wrong yet.
                # Only surfaced past the warning threshold, and never as a dialog — running
                # out of descriptors is a future problem, not this run's.
                est, cap, pct = self._fds
                self.status.setText(
                    f"Done — file-descriptor headroom is tight: ~{est} of {cap} ({pct}%). "
                    "See log.")
            else:
                self.status.setText("Done.")

    def save(self):
        c = self.cfg
        c.setValue("dp/downloads", self.downloads.text())
        c.setValue("dp/game_root", self.game_root.text())
        c.setValue("dp/selectors", self.selectors.text())
        c.setValue("dp/jobs", self.jobs.value())
