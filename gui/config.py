"""User-facing defaults and a few constant names shared across tabs.

Every path is absolute so the app works regardless of where it is launched from;
each one is editable in the UI and remembered via QSettings.
"""

DEF_COLLECTION = "bhiwpn"
DEF_DOWNLOADS  = "/mnt/ssd/Downloads/Mods"
DEF_COOKIES    = "/mnt/ssd/cookies.json"
DEF_GAME_ROOT  = "/mnt/ssd/Cyberpunk 2077/game/prefix/drive_c/game"
DEF_PARK_DIR   = "/mnt/ssd/.mod_parked"

DEF_SCAN_PATHS = (
    "archive/pc/mod",
    "bin/x64/plugins/cyber_engine_tweaks/mods",
    "mods",
    "red4ext/plugins",
    "r6/scripts",
    "r6/tweaks",
    "soundbanks",
)

MANIFEST_NAME   = ".mod_manifest.json"   # written by nexus_core.deployer, in the game root
PARK_STATE_NAME = "parked.json"          # written by nexus_core.modctl, in the park dir

# Framework mods nexus_core.modctl refuses to park without --force; replicated here
# only so the Manage table can flag them (the actual guard still lives in modctl).
FRAMEWORK_HINTS = (
    "red4ext", "cyber engine tweaks", "cet ", "archivexl", "tweakxl", "codeware",
    "redscript", "input_loader", "mod_settings", "cybercmd", "reddata",
    "redfilesystem", "redhttpclient", "audioware", "equipment-ex", "cyberware-ex",
    "virtual atelier", "native interactions", "customhackingsystem",
)
