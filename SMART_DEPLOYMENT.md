# Clean Game Folder / Smart Deployment (Linux)

Version 0.5 moves the user-facing installer to a staged deployment model.
Canonical mod content lives in:

```
<Cyberpunk 2077>/downloaded_mods/
```

The game-facing paths contain hardlinks to that content. `Undeploy` removes only
links that the deployment database can prove it owns; it never recursively wipes
mod directories and it preserves paths replaced by Steam, a game patch, or an
external tool.

## State

`downloaded_mods/deployment.sqlite3` stores installed mods, per-file hashes,
per-profile enabled flags, conflict winners, and the currently materialised
links. No new `.mod_manifest.json`, `.mod_disabled.json`, or `.mod_manager.lock`
is written by the smart installer. Its flock file and SQLite state both live
under `downloaded_mods/`.

Profiles are cheap: switching the active profile changes one SQLite active flag
and immediately reconciles the hardlink projection.

## CLI

```bash
nexus-links status --game-root /path/to/game
nexus-links deploy --game-root /path/to/game
nexus-links undeploy --game-root /path/to/game
nexus-links drift --game-root /path/to/game
nexus-links migrate --game-root /path/to/game
nexus-links profile-create "No gameplay mods" --game-root /path/to/game
nexus-links profile-switch "No gameplay mods" --game-root /path/to/game
```

A local control endpoint can be started with:

```bash
nexus-links serve --game-root /path/to/game --host 127.0.0.1 --port 8765
curl -X POST http://127.0.0.1:8765/deploy
```

Non-loopback binding is refused unless an API token is supplied.

## Migration

`nexus-links migrate` imports the previous manager's `.mod_manifest.json`. It
copies every claimed file into staging first, translates legacy disabled state,
removes only manifest-owned in-place files, recreates the active projection as
hardlinks, and archives the old JSON state under
`downloaded_mods/legacy_state/`.

The new installer intentionally refuses to mix a legacy manifest install with a
new smart install. Migrate once, then continue installing normally.

## Drift detection

The database stores the device/inode and SHA-256 baseline for every deployed
link. `drift` distinguishes:

* `missing` — game-facing link was removed;
* `replaced` — path now points at another inode;
* `stale-link` — staging has a newer revision and the projection needs rebuild;
* `modified-in-place` — someone wrote through the hardlink and changed the
  canonical inode itself.

A relink is atomic: the manager creates a temporary hardlink beside the target
and swaps it in with `os.replace()`. A hardlink failure leaves the old game path
untouched.

## REDmod on Linux

This project does **not** execute `redMod.exe` or Windows junctions. REDmod
archives are handled conservatively:

* `mods/<name>/archives/*.archive` -> projected as legacy
  `archive/pc/mod/<name>__*.archive` hardlinks;
* already-compatible `.reds` payloads -> `r6/scripts/<name>/...`;
* TweakXL-readable yaml/json tweaks -> `r6/tweaks/<name>/...`;
* `info.json` / `mod.json` -> retained only as staged metadata;
* REDmod `.script`, custom sound, or other compiler-only payloads -> staged but
  marked `native-compile-required` and **not** falsely treated as compiled.

Official REDmod script/tweak deployment performs compilation. There is no
bundled proprietary compiler in this Linux-only manager, so unsupported REDmod
compiler payloads are surfaced to the user rather than silently running a
Windows executable.
