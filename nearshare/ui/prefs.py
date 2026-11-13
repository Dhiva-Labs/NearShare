"""Small persistent settings for the UI, independent of nearshare.core.

One JSON file under $XDG_CONFIG_HOME/nearshare/ui-state.json. It holds
the device name and download folder the user picked in Preferences, and
a couple of "already shown" flags. A JSON file rather than GSettings
because a schema would have to be installed and compiled, which a snap
and a source checkout cannot both do the same way.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("nearshare.ui.prefs")


def _state_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "nearshare" / "ui-state.json"


def _load() -> dict[str, Any]:
    try:
        state = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}  # missing file, unreadable, or corrupt -- start fresh
    return state if isinstance(state, dict) else {}


def _save(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write cannot leave a truncated
        # file that would silently reset every setting.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, path)
    except OSError as exc:
        # Worst case a setting does not survive a restart -- never worth
        # crashing or blocking the UI over.
        log.warning("could not persist UI state to %s: %s", path, exc)


def get(key: str, default: Any = None) -> Any:
    return _load().get(key, default)


def put(key: str, value: Any) -> None:
    state = _load()
    if value is None:
        state.pop(key, None)
    else:
        state[key] = value
    _save(state)


def has_seen_background_explainer() -> bool:
    return bool(get("seen_background_explainer"))


def mark_background_explainer_seen() -> None:
    put("seen_background_explainer", True)


def device_name() -> str | None:
    """The name the user chose for this machine, or None for the default."""
    name = get("device_name")
    return name.strip() or None if isinstance(name, str) else None


def set_device_name(name: str | None) -> None:
    put("device_name", name.strip() if name and name.strip() else None)


def download_dir() -> Path | None:
    """Where received files go, or None for ~/Downloads. A folder that no
    longer exists (an unplugged drive) falls back to the default."""
    value = get("download_dir")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_dir() else None


def set_download_dir(path: Path | None) -> None:
    put("download_dir", str(path) if path else None)


def setup_dismissed() -> bool:
    return bool(get("setup_dismissed"))


def set_setup_dismissed(dismissed: bool) -> None:
    put("setup_dismissed", True if dismissed else None)
