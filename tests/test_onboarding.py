from __future__ import annotations

import json
from pathlib import Path

from nexus_core import nexus_account, onboarding


def _vanilla(root: Path):
    (root / "bin/x64").mkdir(parents=True)
    (root / "bin/x64/Cyberpunk2077.exe").write_bytes(b"exe")
    (root / "archive/pc/content").mkdir(parents=True)
    (root / "r6").mkdir()
    (root / "engine").mkdir()


def test_inspect_game_root_accepts_real_layout(tmp_path):
    game = tmp_path / "Cyberpunk 2077"
    _vanilla(game)
    row = onboarding.inspect_game_root(game)
    assert row["valid"] is True
    assert row["exists"] is True
    assert "bin/x64/Cyberpunk2077.exe" in row["markers"]
    assert row["staging_dir"] == str(game.resolve() / "downloaded_mods")


def test_inspect_game_root_rejects_arbitrary_directory(tmp_path):
    row = onboarding.inspect_game_root(tmp_path)
    assert row["valid"] is False
    assert row["markers"] == []


def test_detect_game_from_steam_libraryfolders(tmp_path):
    home = tmp_path / "home"
    steam = home / ".local/share/Steam"
    library = tmp_path / "games"
    game = library / "steamapps/common/Cyberpunk 2077"
    _vanilla(game)
    (steam / "steamapps").mkdir(parents=True)
    (steam / "steamapps/libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n  "0" { "path" "' + str(library).replace("\\", "\\\\") + '" }\n}\n',
        encoding="utf-8",
    )
    rows = onboarding.detect_game_roots(home=home)
    assert rows
    assert rows[0]["path"] == str(game.resolve())
    assert rows[0]["valid"] is True


def test_onboarding_probe_cli_json(tmp_path, capsys):
    game = tmp_path / "game"
    _vanilla(game)
    assert onboarding.main(["probe-game", "--game-root", str(game)]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("[onboarding] ")
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["type"] == "game"
    assert payload["valid"] is True


def test_nexus_account_validate_cli(monkeypatch, capsys):
    monkeypatch.setenv("NEXUS_API_KEY", "secret")
    monkeypatch.setattr(
        nexus_account.NexusAccountClient,
        "validate",
        lambda self: {"name": "V", "user_id": 42, "is_premium": True, "is_supporter": False},
    )
    assert nexus_account.main(["validate"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("[nexus-account] ")
    payload = json.loads(line.split(" ", 1)[1])
    assert payload == {
        "type": "validate",
        "user": {"name": "V", "user_id": 42, "is_premium": True, "is_supporter": False},
    }
