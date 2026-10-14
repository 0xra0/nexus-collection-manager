# Guided Onboarding

Nexus Collection Manager v0.15 adds a three-step first-run wizard. It does not copy,
install, deploy, or delete a mod until the user reaches the normal manager UI.

## 1. Nexus Mods account

The wizard accepts a Nexus Mods Personal API Key and validates it against the same
`/users/validate.json` endpoint used by the rest of the application. The result shows
the Nexus user name and Free/Premium account tier. The key is passed to the validation
subprocess through `NEXUS_API_KEY`, never as a visible command-line argument.

A user can explicitly continue offline. Local scanning, profiles, installation and
Smart Deployment remain available, while Nexus-backed discovery/download/update/account
features remain disabled until a key is configured later.

The **Open Nexus API Access** button opens the Nexus account API-access page in the
system browser. Nexus SSO is deliberately not impersonated: public applications need
to be registered with Nexus before using its SSO flow.

## 2. Game configuration

The user selects:

- the actual Cyberpunk 2077 root (the directory containing `bin/`, `archive/`, `r6/`),
- the regular downloads folder.

**Auto-detect Linux install** checks common native/Flatpak Steam roots plus libraries
listed in Steam's `libraryfolders.vdf`. Detection is bounded and does not recursively
walk every mounted disk.

Validation looks for vanilla-owned markers such as:

- `bin/x64/Cyberpunk2077.exe`
- `archive/pc/content`
- `r6/`
- `engine/`

The wizard also displays the future Smart Deployment staging location:
`<game-root>/downloaded_mods/`.

## 3. Initial mod scan

The final page runs the normal read-only `nexus_core.mod_scanner` backend over the
configured mod roots. It performs the same recursive discovery, SHA-256/MD5 hashing and
TF-IDF + DBSCAN grouping as the Scanner tab and saves the initial integrity snapshot
outside the game install.

The Finish button remains disabled until a successful scan (an empty mod install is a
valid successful scan). Setup settings are committed only after Finish.

## First-run and rerun behavior

A genuinely new QSettings profile opens the wizard automatically. Existing users who
upgrade from an older release are not interrupted if a game-root setting already
exists.

The wizard can be run again through **Setup -> Guided Setup...** or:

```bash
nexus-collection-manager --setup
```

The backend game discovery helper is also available as:

```bash
nexus-onboarding detect-game
nexus-onboarding probe-game --game-root "/path/to/Cyberpunk 2077"
```
