"""Shared Nexus API-key persistence for every GUI surface.

Historically each tab stored its own copy of the Nexus key.  That allowed a
stale/blank tab to overwrite a valid key during MainWindow.closeEvent().  This
module provides one authoritative QSettings entry and keeps legacy keys only as
compatibility mirrors.

The module intentionally does not import PySide6.  ``bind_api_key_edit`` works
with any QLineEdit-like object exposing ``setText`` and ``textEdited.connect``;
that keeps the persistence logic independently testable.
"""
from __future__ import annotations

import json
import weakref
from pathlib import Path
from typing import Any

CANONICAL_KEY = "credentials/nexus_api_key"

# Old releases read/wrote these independently.  They are migration sources and
# mirrors for users who temporarily run an older build against the same config.
LEGACY_KEYS = (
    "nx/api_key",
    "dp/api_key",
    "dm/api_key",
    "trend/api_key",
    "endorsed/api_key",
    "tracked/api_key",
    "updates/api_key",
    "cf/api_key",
    "ac/api_key",
)

CLI_CONFIG = Path.home() / ".config" / "cp2077_modder" / "config.json"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _contains(cfg: Any, key: str) -> bool:
    contains = getattr(cfg, "contains", None)
    if callable(contains):
        try:
            return bool(contains(key))
        except Exception:
            pass
    # Fallback for simple settings doubles.
    sentinel = object()
    try:
        return cfg.value(key, sentinel) is not sentinel
    except Exception:
        return False


def _sync(cfg: Any) -> None:
    sync = getattr(cfg, "sync", None)
    if callable(sync):
        try:
            sync()
        except Exception:
            pass


def _read_cli_config_key() -> str:
    try:
        loaded = json.loads(CLI_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    return _text(loaded.get("nexus_api_key")) if isinstance(loaded, dict) else ""


def _write_cli_config(api_key: str) -> None:
    """Keep standalone backend CLIs aligned with the GUI key.

    Failure is deliberately non-fatal: QSettings remains authoritative for the
    GUI, including read-only/home-less environments.
    """
    try:
        data: dict[str, Any] = {}
        if CLI_CONFIG.is_file():
            try:
                loaded = json.loads(CLI_CONFIG.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data.update(loaded)
            except (OSError, ValueError, TypeError):
                pass
        data["nexus_api_key"] = api_key
        CLI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        tmp = CLI_CONFIG.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(CLI_CONFIG)
    except OSError:
        pass


def get_nexus_api_key(cfg: Any) -> str:
    """Return the authoritative key, migrating old per-tab settings once.

    A *present* canonical key is authoritative even when empty, so intentionally
    clearing the credential stays cleared.  Only installations that predate the
    canonical key scan legacy entries for the first non-empty value.
    """
    if _contains(cfg, CANONICAL_KEY):
        return _text(cfg.value(CANONICAL_KEY, ""))

    legacy_values: list[tuple[str, str]] = []
    for key in LEGACY_KEYS:
        try:
            value = _text(cfg.value(key, ""))
        except Exception:
            value = ""
        if value:
            legacy_values.append((key, value))

    unique = list(dict.fromkeys(value for _key, value in legacy_values))
    cli_key = _read_cli_config_key()
    if len(unique) == 1:
        migrated = unique[0]
    elif len(unique) > 1 and cli_key in unique:
        # Old releases could leave conflicting per-tab values.  The shared CLI
        # config is the best tiebreaker because onboarding/deployer persisted it
        # independently of MainWindow.closeEvent().
        migrated = cli_key
    elif unique:
        # Prefer the historic Nexus-account key when no independent tiebreaker
        # exists; otherwise take the first surviving legacy value.
        by_name = dict(legacy_values)
        migrated = by_name.get("nx/api_key", unique[0])
    else:
        migrated = cli_key

    # Establish the canonical marker even when no old key exists.  This makes a
    # later intentional clear distinguishable from a failed legacy fallback.
    cfg.setValue(CANONICAL_KEY, migrated)
    if migrated:
        for key in LEGACY_KEYS:
            cfg.setValue(key, migrated)
        _write_cli_config(migrated)
    _sync(cfg)
    return migrated


def set_nexus_api_key(cfg: Any, value: str, *, update_cli: bool = True) -> str:
    """Persist one Nexus key everywhere that old/new releases expect it."""
    key = _text(value)
    cfg.setValue(CANONICAL_KEY, key)
    for legacy in LEGACY_KEYS:
        cfg.setValue(legacy, key)
    _sync(cfg)
    if update_cli:
        _write_cli_config(key)
    return key


class _CredentialHub:
    """Process-local synchronizer for all API-key QLineEdits."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self._key = get_nexus_api_key(cfg)
        self._edits: list[weakref.ReferenceType] = []

    @property
    def key(self) -> str:
        return self._key

    def bind(self, edit: Any) -> str:
        self._prune()
        try:
            edit.setText(self._key)
        except Exception:
            return self._key
        self._edits.append(weakref.ref(edit))
        signal = getattr(edit, "textEdited", None)
        connect = getattr(signal, "connect", None)
        if callable(connect):
            connect(self.set)
        return self._key

    def set(self, value: str) -> None:
        key = set_nexus_api_key(self.cfg, value)
        self._key = key
        alive: list[weakref.ReferenceType] = []
        for ref in self._edits:
            edit = ref()
            if edit is None:
                continue
            alive.append(ref)
            try:
                current = _text(edit.text())
                if current != key:
                    edit.setText(key)
            except Exception:
                continue
        self._edits = alive

    def _prune(self) -> None:
        self._edits = [ref for ref in self._edits if ref() is not None]


_HUB: _CredentialHub | None = None


def credential_hub(cfg: Any) -> _CredentialHub:
    global _HUB
    if _HUB is None:
        _HUB = _CredentialHub(cfg)
    return _HUB


def bind_api_key_edit(edit: Any, cfg: Any) -> str:
    """Populate and live-sync a QLineEdit-like API-key editor."""
    return credential_hub(cfg).bind(edit)


def reset_credential_hub_for_tests() -> None:
    global _HUB
    _HUB = None
