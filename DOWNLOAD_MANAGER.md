# Download Manager

The v0.9 Download Manager stores its queue in the Smart Deployment SQLite database and writes completed packages directly to `<game>/downloaded_mods/archives/`. Archive Management indexes the package immediately and records its Nexus mod/file identity.

## Account modes

- **Premium** — an authenticated Nexus API key can resolve `/download_link` directly from a Mod ID + File ID. No NXM token is required.
- **Free** — downloads require a fresh `nxm://.../mods/<mod>/files/<file>?key=...&expires=...` manager authorization from Nexus. The manager verifies the optional `user_id` against the configured API key before resolving the CDN link.

Temporary NXM authorization is kept only in the local deployment database while the job is incomplete and is removed after a successful download. Queue JSON/UI responses never expose the token.

## Linux NXM handler

The GUI button **Register Linux NXM handler** writes a desktop entry for `x-scheme-handler/nxm` and asks `xdg-mime` to make it the default handler. Browser **Download with Manager** clicks then open the app with the NXM URL, switch to the Downloads tab, enqueue it, and start the download.

You can also register or handle links manually:

```bash
nexus-download-manager register-nxm --game-root /path/to/game
nexus-download-manager handle-nxm 'nxm://cyberpunk2077/mods/123/files/456?...'
```

## Queue commands

```bash
# Premium/direct queue by IDs
nexus-download-manager add --game-root /path/to/game --mod-id 123 --file-id 456

# Free/Premium manager link
nexus-download-manager nxm --game-root /path/to/game 'nxm://...'

nexus-download-manager queue --game-root /path/to/game
nexus-download-manager run --game-root /path/to/game
nexus-download-manager run --game-root /path/to/game --job-id 7
nexus-download-manager retry --game-root /path/to/game --job-id 7
nexus-download-manager remove --game-root /path/to/game --job-id 7 --delete-partial
```

Set `NEXUS_API_KEY` in the environment for CLI downloads. The GUI passes its configured key to the backend through the child environment rather than rendering it in command logs.

## Resume and crash behavior

Transfers use HTTP Range when a `.part` file exists. Pausing or SIGINT keeps the partial. If the process dies abruptly, a `downloading` row whose worker PID no longer exists is recovered as `paused` the next time the queue is read/run.
