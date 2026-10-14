# Profile Manager

Nexus Collection Manager profiles are lightweight smart-deployment configurations. They do not copy the game directory or duplicate staged mod payloads. A profile stores which staged mods are enabled together with conflict-owner preferences and archive/load-order rules. Loading a profile changes the active database flag and rebuilds the managed hardlink projection.

## Desktop workflow

The **8 · Profiles** tab provides:

- **Save current as…** — capture the current profile configuration under a new name (or deliberately overwrite an existing saved profile).
- **Load + rebuild** — activate a profile and reconcile game-facing hardlinks immediately.
- **Duplicate…** — clone any saved profile without activating it.
- **Export…** — write a portable JSON profile.
- **Import…** — validate and dry-run a portable profile before writing it. The preview reports matched staged mods, missing referenced mods, and preferences that cannot be resolved locally.
- **Compare** — compare two profiles by enabled/disabled mods, file-conflict owner preferences, archive/mod ordering, and pairwise load-order rules.

Profile edits made elsewhere in the manager are persisted immediately. “Save” is therefore a named snapshot/save-as operation, not a flush button.

## Portable export format

Exports use mod names plus Nexus/source metadata when available. They never store installation-local SQLite mod IDs. Conflict and load-order references are expressed with mod names and normalized relative paths.

When importing into a different installation:

- installed mods are matched by exact staged name first;
- if available, a unique explicit Nexus mod association can match a renamed local staged mod;
- referenced mods that are not installed are reported as missing and are **not fabricated**;
- local-only mods absent from the imported profile are disabled in that imported profile;
- conflict/load-order entries that depend on unavailable mods/files are reported and skipped;
- unsafe relative paths and cyclic load-order rules are rejected before mutation.

Import does not download missing mods automatically. Use the Download/Trending/Endorsed/Tracked workflows to acquire them, then update or re-import the profile as desired.

## CLI

```bash
nexus-profiles list --game-root /path/to/game
nexus-profiles show --game-root /path/to/game "Default"

nexus-profiles save --game-root /path/to/game "My Setup"
nexus-profiles duplicate --game-root /path/to/game "My Setup" "My Setup Copy"
nexus-profiles load --game-root /path/to/game "My Setup"

nexus-profiles export --game-root /path/to/game "My Setup" --output profile.json
nexus-profiles import --game-root /path/to/game profile.json --dry-run
nexus-profiles import --game-root /path/to/game profile.json --name "Imported Setup"

nexus-profiles compare --game-root /path/to/game "Default" "My Setup"
```
