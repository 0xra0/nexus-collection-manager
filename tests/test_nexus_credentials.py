from __future__ import annotations

from pathlib import Path

from gui import nexus_credentials as NC


class FakeSettings:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.sync_count = 0

    def contains(self, key):
        return key in self.data

    def value(self, key, default=None):
        return self.data.get(key, default)

    def setValue(self, key, value):
        self.data[key] = value

    def sync(self):
        self.sync_count += 1


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, value):
        for callback in list(self.callbacks):
            callback(value)


class FakeEdit:
    def __init__(self):
        self._text = ""
        self.textEdited = FakeSignal()

    def setText(self, value):
        self._text = str(value)

    def text(self):
        return self._text

    def user_edit(self, value):
        self._text = str(value)
        self.textEdited.emit(self._text)


def setup_function():
    NC.reset_credential_hub_for_tests()


def test_migrates_nonempty_legacy_key_even_when_old_global_is_blank(tmp_path, monkeypatch):
    monkeypatch.setattr(NC, "CLI_CONFIG", tmp_path / "config.json")
    cfg = FakeSettings({"nx/api_key": "", "dp/api_key": "  correct-key  "})

    assert NC.get_nexus_api_key(cfg) == "correct-key"
    assert cfg.data[NC.CANONICAL_KEY] == "correct-key"
    assert all(cfg.data[key] == "correct-key" for key in NC.LEGACY_KEYS)
    assert cfg.sync_count >= 1


def test_canonical_blank_is_intentional_and_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(NC, "CLI_CONFIG", tmp_path / "config.json")
    cfg = FakeSettings({NC.CANONICAL_KEY: "", "dp/api_key": "stale-old-key"})

    assert NC.get_nexus_api_key(cfg) == ""



def test_conflicting_legacy_values_use_shared_cli_config_as_tiebreaker(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text('{"nexus_api_key": "new-correct"}\n', encoding="utf-8")
    monkeypatch.setattr(NC, "CLI_CONFIG", config)
    cfg = FakeSettings({"nx/api_key": "old-stale", "dp/api_key": "new-correct"})

    assert NC.get_nexus_api_key(cfg) == "new-correct"
    assert cfg.data[NC.CANONICAL_KEY] == "new-correct"


def test_setting_key_mirrors_legacy_and_cli_config(tmp_path, monkeypatch):
    config = tmp_path / "cp2077_modder" / "config.json"
    monkeypatch.setattr(NC, "CLI_CONFIG", config)
    cfg = FakeSettings()

    assert NC.set_nexus_api_key(cfg, "  abc123  ") == "abc123"
    assert cfg.data[NC.CANONICAL_KEY] == "abc123"
    assert all(cfg.data[key] == "abc123" for key in NC.LEGACY_KEYS)
    assert '"nexus_api_key": "abc123"' in config.read_text(encoding="utf-8")


def test_bound_editors_share_one_key_and_persist_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(NC, "CLI_CONFIG", tmp_path / "config.json")
    cfg = FakeSettings({"nx/api_key": "initial"})
    first = FakeEdit()
    second = FakeEdit()

    NC.bind_api_key_edit(first, cfg)
    NC.bind_api_key_edit(second, cfg)
    assert first.text() == second.text() == "initial"

    first.user_edit("new-secret")

    assert second.text() == "new-secret"
    assert cfg.data[NC.CANONICAL_KEY] == "new-secret"
    assert cfg.sync_count >= 2  # migration + live edit


def test_tabs_no_longer_write_independent_api_key_settings_on_close():
    root = Path(__file__).resolve().parents[1] / "gui"
    tab_files = (
        "download_manager_tab.py", "nexus_tab.py", "conflict_tab.py", "deploy_tab.py",
        "trending_tab.py", "update_tab.py", "correlation_tab.py",
    )
    for name in tab_files:
        text = (root / name).read_text(encoding="utf-8")
        assert 'setValue("nx/api_key"' not in text
        assert 'setValue("dp/api_key"' not in text
        assert 'setValue("updates/api_key"' not in text
        assert 'setValue("cf/api_key"' not in text
        assert 'setValue("ac/api_key"' not in text
        assert 'setValue(f"{self.settings_prefix}/api_key"' not in text
        assert "bind_api_key_edit(self.api_key, cfg)" in text
