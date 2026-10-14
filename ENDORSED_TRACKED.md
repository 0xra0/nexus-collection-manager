# Endorsed & Tracked Tabs

Version 0.11 adds two Nexus account-library views to the desktop application:

- **3 · Endorsed** — all active Cyberpunk 2077 endorsements returned by the configured Nexus account.
- **4 · Tracked** — all Cyberpunk 2077 mods currently tracked by the configured Nexus account.

Both views reuse the same discovery cards and install path as Trending Mods. Each card can:

- install the safest MAIN file through Download Manager → Archive Management → Smart Installer;
- open the mod directly on Nexus Mods;
- endorse/unendorse;
- track/untrack.

For free accounts an Install action keeps the existing Nexus `nxm://` authorization flow. Premium accounts can continue directly through the download queue.

## Account-list hydration

The authenticated account list is authoritative for membership. Entries are filtered to the `cyberpunk2077` domain and explicit abstained endorsement records are excluded. The manager preserves account-list order and collapses duplicate IDs.

To avoid one REST metadata request per account entry, the manager attempts a batched GraphQL basic-mod lookup. If batch metadata is unavailable, it reuses metadata embedded in the account response and finally falls back to the v1 mod-detail endpoint. A missing/deleted mod is therefore isolated to that card rather than failing the whole account list.

Cross-state is live: the Endorsed tab also shows whether each mod is tracked, and the Tracked tab shows whether each mod is endorsed. If the secondary account-state request fails, the primary list still loads.

Removing an endorsement from **Endorsed**, or stopping tracking from **Tracked**, removes that card only after Nexus confirms the mutation.

## CLI

The existing discovery CLI now accepts the two account feeds:

```bash
nexus-trending feed --feed endorsed
nexus-trending feed --feed tracked
```

The API key is taken from `NEXUS_API_KEY`, the hidden `--api-key` option, or the existing application configuration.
