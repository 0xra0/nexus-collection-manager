"""Core backends for the Nexus Collection Manager.

Each module is a standalone CLI (``python -m nexus_core.<module>``) *and* an
importable library — importing one has no side effects (no filesystem writes, no
stdout hijack, no process exit on a missing optional dependency). The GUI runs
them as ``python -m`` subprocesses so their many ``sys.exit()`` paths and the
deployer's stdout tee stay isolated from the Qt process.

    downloader  (was test.py)      download every file in a Nexus collection (aria2c)
    deployer    (was extract2.py)  extract + deploy archives into the game folder (bsdtar)
    modctl      (was modctl.py)    park / unpark deployed mods without re-extracting
    smart_deployment               Linux staging + hardlink projection, drift and profiles
    load_order                     archive preference rules + managed modlist.txt
"""
