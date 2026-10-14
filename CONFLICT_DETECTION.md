# Conflict Detection

The smart-deployment conflict engine inspects canonical staged mod content in
`<game>/downloaded_mods/`; scans do not modify the game directory.

## Layers

1. **File overlap** — detects enabled mods that project to the same game path.
   The inbox shows the current owner and can persist a preferred owner.
2. **Archive resources** — reads RED4 `.archive` indexes directly on Linux and
   compares 64-bit resource path hashes. Readable resource paths are shown when
   archive metadata exposes them; hash-only evidence is still sufficient to
   prove a collision.
3. **redscript annotations** — scans `.reds` files for competing
   `@replaceMethod`, `@replaceGlobal`, `@addMethod`, and `@addField` targets.
   Multiple wrappers alone are not treated as a hard conflict.
4. **TweakXL** — scans YAML/JSON/Tweak files for multiple mods changing the same
   flat/record path. Direct assignments are ranked above mutation-only
   append/prepend/remove overlaps.

Severity is dependency-aware. Confirmed Nexus requirements and conservative
local patch/compatibility-name relationships downgrade expected overlaps one
level while keeping them visible for review.

## Conflict inbox

Conflict rows have stable IDs and are stored in the smart-deployment SQLite
DB. They survive restarts and retain resolution history. The GUI **Conflicts**
tab supports:

- disable a participating mod;
- uninstall a participating mod;
- choose a preferred owner for exact file conflicts;
- set a managed archive load-order preference for archive resource conflicts;
- open Nexus Mods links for every source archive/mod entry.

redscript and TweakXL semantic conflicts are intentionally not "resolved" by
archive load order because that would give a false sense of safety; disable,
uninstall, or manually patch those mods instead.

## Linux archive load order

The manager does not invoke REDmod/redMod.exe. Explicit archive preferences are
implemented by changing only the **deployed hardlink alias name** under
`archive/pc/mod/`; staged canonical archives keep their original names and
contents. This makes preferences profile-specific and instantly reversible by
rebuilding the link projection.

## CLI

```bash
nexus-conflicts scan --game-root /path/to/game
nexus-conflicts inbox --game-root /path/to/game
nexus-conflicts prefer --game-root /path/to/game --conflict-id ID --mod NAME
nexus-conflicts disable --game-root /path/to/game --conflict-id ID --mod NAME
nexus-conflicts uninstall --game-root /path/to/game --conflict-id ID --mod NAME
```

Set `NEXUS_API_KEY` in the environment (or enter it in the GUI) to enrich
severity classification with live Nexus requirement relationships. A failed
Nexus lookup is non-fatal; local conflict scanning still completes.
