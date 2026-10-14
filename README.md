# Nexus Collection Manager

A Linux desktop manager for Cyberpunk 2077 mods and Nexus Mods collections. The PySide6 interface combines downloads, archive installation, staged deployment, profiles, conflict inspection, load order, updates, and mod scanning. The backends are also available as command-line tools.

## Requirements

- Linux desktop and an installed copy of Cyberpunk 2077. Select the game root containing `bin/`, `archive/`, `r6/`, and `engine/` during setup.
- Python 3.10 or newer, `pip`, and a Python virtual environment.
- `aria2c` for downloads and `bsdtar` for archive extraction.
- A Nexus Mods Personal API Key for Nexus account, discovery, update, and direct Premium download features. You can run the local tools without a key.
- For the **Collections** tab, a JSON export of your own Nexus Mods browser cookies. Keep this file private. Collection preview and download require it even when an API key is configured.

Install the external tools with your distribution's package manager, for example:

```bash
# Debian / Ubuntu
sudo apt install python3 python3-venv python3-pip aria2 libarchive-tools

# Arch Linux
sudo pacman -S python python-pip aria2 libarchive
```

## Install from source

```bash
git clone https://github.com/0xra0/nexus-collection-manager.git
cd nexus-collection-manager
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
nexus-collection-manager
```

The same GUI can be run directly from a source checkout after installing dependencies:

```bash
python -m pip install -r requirements.txt
python main.py
```

### Build a wheel and source archive

From the project root, with the virtual environment active:

```bash
python -m pip install --upgrade build
python -m build
```

The artifacts are written to `dist/`. Install the built wheel with `python -m pip install dist/*.whl`, then start `nexus-collection-manager`. No compiled game assets or bundled REDmod executable are produced by this build.

## First run

1. Start `nexus-collection-manager` (or `python main.py` from the source tree). The guided setup opens on a new profile.
2. Enter a Nexus Mods Personal API Key and validate it, or choose **Continue without Nexus login** for local features. The setup screen links to Nexus API access.
3. Select the Cyberpunk 2077 game root and a downloads folder. Linux Steam install detection is available, or you can browse to the root manually.
4. Run the initial read-only mod scan and finish setup.

To rerun setup, use **Setup → Guided Setup…** in the app or launch `nexus-collection-manager --setup`. Restart the app after changing setup paths or account details so all open tabs reload them.

## Typical workflow

1. **Get mods:** Use **Downloads** for direct Nexus files and `nxm://` links, **Trending / Endorsed / Tracked** for discovery, or **Collections** for a collection slug or URL. Premium accounts can resolve direct downloads with an API key; free accounts need a fresh Nexus **Download with Manager** NXM authorization for individual files. In **Collections**, select your private cookies JSON file, preview the collection, then start the download.
2. **Install archives:** In **Install**, select downloaded `.zip`, `.7z`, or `.rar` archives and install them into the game's `downloaded_mods/` staging area. Review the operation before applying it.
3. **Deploy:** Use **Deploy** to project staged files into game-facing paths with hardlinks. Keep staging and the game root on the same filesystem so hardlinks work. **Undeploy** removes only links owned by the manager.
4. **Organize:** Use **Profiles** to save and switch enabled mod sets, **Conflicts** and **Load Order** to inspect priorities, and **Archives** / **Manage** to review installed content.
5. **Maintain:** Use **Updates** to find newer Nexus files and **Scanner** to inspect the game tree and integrity snapshot.

The **Downloads** tab can register a Linux `nxm://` handler. Once registered, a Nexus **Download with Manager** click can enqueue a file in the app.

Some REDmod files require the proprietary Windows REDmod compiler. This Linux manager stages and reports compiler-only payloads; it does not compile them or run `redMod.exe`.

## Command-line tools

Installing the project also installs backend commands. For example:

```bash
nexus-onboarding detect-game
nexus-onboarding probe-game --game-root "/path/to/Cyberpunk 2077"
nexus-links status --game-root "/path/to/Cyberpunk 2077"
nexus-links deploy --game-root "/path/to/Cyberpunk 2077"
nexus-links drift --game-root "/path/to/Cyberpunk 2077"
nexus-profiles list --game-root "/path/to/Cyberpunk 2077"
```

Run any command with `--help` for its options. The project also provides `nexus-download`, `nexus-install`, `nexus-conflicts`, `nexus-load-order`, `nexus-archives`, `nexus-updates`, and other `nexus-*` commands declared in `pyproject.toml`.

## Data and account notes

Smart Deployment stores its database and staged mod files under `<game-root>/downloaded_mods/`. Nexus account configuration is stored locally for the application; do not commit your API key, cookies JSON, downloaded archives, or game files to this repository. Back up your game and mod data before making large changes.

For more detail, see [Guided Onboarding](GUIDED_ONBOARDING.md), [Download Manager](DOWNLOAD_MANAGER.md), [Smart Deployment](SMART_DEPLOYMENT.md), [Profiles](PROFILE_MANAGER.md), and [Load Order](LOAD_ORDER.md).
