"""Tiny UI-only persistence, deliberately independent of nearshare.core.

Right now this holds exactly one flag: whether the user has already seen
the "NearShare keeps running in the background" explainer dialog (see
NearShareWindow._show_background_explainer in app.py) so it's shown at
most once, ever. A single small JSON file under
$XDG_CONFIG_HOME/nearshare/ui-state.json is enough for that -- no need
to pull in GSettings schemas (which would need installing a schema
file) for one boolean.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..core.paths import config_dir
from typing import Any

log = logging.getLogger("nearshare.ui.prefs")


def _state_path() -> Path:
    return config_dir() / "ui-state.json"


def _load() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}  # missing file, unreadable, or corrupt -- start fresh


def _save(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError as exc:
        # Worst case the explainer dialog reappears next launch -- never
        # worth crashing or blocking the UI over.
        log.warning("could not persist UI state to %s: %s", path, exc)


def has_seen_background_explainer() -> bool:
    return bool(_load().get("seen_background_explainer"))


def mark_background_explainer_seen() -> None:
    state = _load()
    state["seen_background_explainer"] = True
    _save(state)
