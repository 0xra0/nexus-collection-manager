# Archive Management

Archive Management keeps downloaded mod packages in a managed package library at:

```text
<game>/downloaded_mods/archives/
```

This is separate from the extracted canonical mod payloads in `downloaded_mods/mods/`.
Deleting a package archive therefore does **not** uninstall a mod and does not alter any
deployed game hardlinks. It only means the original package would need to be downloaded
again if you wanted to reinstall/re-run its installer later.

## Index and browse

`nexus-archives list --game-root ...` indexes `.zip`, `.7z`, and `.rar` files already
present in the archive library. ZIP contents are browsed directly with Python's standard
library; 7z/RAR listing uses `bsdtar`. Browsing never extracts entries. Suspicious absolute
or `..` member paths are marked as unsafe.

An installed package is automatically copied into this archive library when the interactive
installer commits the staged mod. SHA-256 and MD5 hashes are stored for integrity/correlation.

## Nexus links

Each package can be linked to a Cyberpunk 2077 Nexus mod ID (and optional file ID). The GUI
accepts either a numeric ID or a Nexus URL. Links are metadata only; they do not make network
requests and do not change the installed mod.

A filename-derived Nexus ID may be shown as a **suggestion** but is not persisted until the
user explicitly links it.

## Orphan cleanup

An archive is an orphan only when:

1. no staged/installed mod references it as its source package; and
2. it has no explicit Nexus mod link.

This intentionally retains downloaded-but-not-installed packages that the user has linked to
Nexus. Unlink one to make it cleanup-eligible.

Cleanup is dry-run by default:

```bash
nexus-archives cleanup --game-root /path/to/game
nexus-archives cleanup --game-root /path/to/game --apply
```

Manual deletion of an archive referenced by an installed mod requires `--force`. Even then,
only the raw package is removed; the staged payload and deployment are preserved.
