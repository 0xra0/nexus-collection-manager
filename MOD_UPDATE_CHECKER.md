# Mod Update Checker

Version 0.14 adds a profile-aware update checker for mods installed through the
smart staging library (`<game>/downloaded_mods`).  It does not scan or rewrite
the live game directory while checking for updates.

## Identity and update matching

For each staged mod, the checker takes the newest archive recorded as an actual
install source and resolves its Nexus identity in this order:

1. confirmed Nexus **mod + file ID** stored by Download/Archive Management;
2. exact package **MD5 reverse lookup** on Nexus;
3. Nexus-style source filename / installed **version** as a conservative fallback.

A mod with no Nexus identity remains visible as `Not linked` and can be fixed in
Archive Management or Auto-Correlation.  The checker never invents an identity.

After the installed Nexus file is identified, update detection follows Nexus'
`file_updates` lineage (`old_file_id -> new_file_id`) to the end of that chain.
This is deliberately more precise than choosing the newest file on the mod page:
a Nexus mod may publish several co-installable MAIN/OPTIONAL files, and an update
to one sibling must not mark another sibling as outdated.

## Update states

* **UPDATE AVAILABLE** — Nexus names a newer successor in the same file lineage.
* **Current** — the installed Nexus file is the end of its update lineage.
* **Needs review** — a Nexus mod is known but the installed file cannot be
  identified confidently from file ID, MD5 or version.
* **Not linked** — no Nexus mod identity is known.
* **API error** — the local identity is retained but Nexus metadata could not be
  fetched for that entry.

One failing/hidden mod does not discard results for the other installed mods.

## Downloading

`Download update` uses the existing persistent Download Manager.  It downloads
the replacement archive into `downloaded_mods/archives/` and links it to its
Nexus mod/file identity.  It **does not auto-install the update**; use Install or
Archives to preview/apply it so FOMOD choices and file conflicts remain explicit.

Premium Nexus accounts can complete the download directly.  Free accounts still
require Nexus' Download-with-Manager authorization.  In that case the Updates UI
opens the exact Nexus Files page; the existing Linux `nxm://` handler receives
the authorized download.

## CLI

```bash
nexus-updates check --game-root "/path/to/Cyberpunk 2077"
nexus-updates check --game-root "/path/to/Cyberpunk 2077" --enabled-only

nexus-updates download \
  --game-root "/path/to/Cyberpunk 2077" \
  --managed-mod-id 12
```

The API key can be supplied through `NEXUS_API_KEY`; it is intentionally hidden
from normal command logging.
