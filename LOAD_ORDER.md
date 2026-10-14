# Load Order

Nexus Collection Manager v0.7 adds profile-aware archive load-order management for the Linux smart-deployment backend.

## Cyberpunk semantics

For legacy `.archive` files in `archive/pc/mod`, Cyberpunk resolves a contested resource using the archive that loads **first**. Without an explicit list, archive names are loaded in ASCII/binary alphabetical order. When `archive/pc/mod/modlist.txt` exists, the filenames in that file define the explicit order; the first line has the highest conflict priority.

REDmods under `mods/` are a separate load-order system. This project remains Linux-native and does not invoke proprietary `redMod.exe`; REDmod compiler-only payloads therefore stay staged and are reported separately by Smart Deployment.

Useful upstream references:

- https://wiki.redmodding.org/cyberpunk-2077-modding/for-mod-users/users-modding-cyberpunk-2077/load-order
- https://github.com/Nexus-Mods/Vortex/blob/master/packages/adaptors/cyberpunk2077/src/index.ts

## Model

Load-order state belongs to a Smart Deployment profile and is stored in `<game>/downloaded_mods/deployment.sqlite3`.

There are two preference modes:

1. **Manual archive order** — a complete first-wins list, edited with the Load Order UI.
2. **Pairwise rules** — durable rules such as `Compatibility Patch before Base Mod`.

A complete manual archive order supersedes the older mod-level alias priority. Adding a pairwise rule leaves manual-list mode and returns to rule-driven order. Cycles are rejected.

If no explicit order/rules exist, the manager falls back to deterministic ASCII filename order.

## Clean-game modlist generation

`modlist.txt` is generated canonically beneath `downloaded_mods/generated/load_order/<profile-id>/modlist.txt`. The copy in `archive/pc/mod/modlist.txt` is a Linux hardlink to that staged file.

The manager records the generated hardlink separately from mod payload links so drift detection and undeploy can prove ownership before deleting it. An existing unmanaged `modlist.txt` is never silently replaced. Explicit Apply can back it up under `downloaded_mods/external_backups/archive/pc/mod/` and take ownership.

Once Load Order management has been enabled, ordinary Smart Deployment rebuilds regenerate the managed `modlist.txt` automatically after archive changes/profile switches.

## Dry run

The **Dry-run preview** compares the working order with the current game `modlist.txt` and reports:

- archives added or removed from the list;
- position changes;
- legacy manager alias filenames that would be replaced during reconciliation;
- the exact future `modlist.txt` content;
- preference-cycle warnings if corrupted/stale state is encountered.

The preview does not modify the database, staging directory, hardlinks, or the game directory.

## CLI

```bash
# Inspect effective order and current modlist.txt
nexus-load-order show --game-root /path/to/game

# Dry-run a complete first-wins archive sequence
nexus-load-order preview --game-root /path/to/game \
  --archive-key '12:archive/pc/mod/patch.archive' \
  --archive-key '7:archive/pc/mod/base.archive'

# Persist + reconcile hardlinks + generate modlist.txt
nexus-load-order apply --game-root /path/to/game \
  --archive-key '12:archive/pc/mod/patch.archive' \
  --archive-key '7:archive/pc/mod/base.archive'

# Add/remove durable pairwise preferences
nexus-load-order rule-add --game-root /path/to/game \
  --before 'Compatibility Patch' --after 'Base Mod' --note 'Patch must win'

nexus-load-order rule-remove --game-root /path/to/game \
  --before 'Compatibility Patch' --after 'Base Mod'

# Reset to ASCII fallback, rebuild links, and regenerate modlist.txt
nexus-load-order reset --game-root /path/to/game --apply

# Stop managing the generated list and remove only our proven hardlink
nexus-load-order disable --game-root /path/to/game
```
