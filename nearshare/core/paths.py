"""Where NearShare keeps its files, per platform.

Linux follows the XDG base directories; Windows uses the per-user
AppData folders, which only that user (and administrators) can read.
An XDG_* variable that is explicitly set wins on Windows too, which is
how the tests keep each run in a temporary directory there. Everything
is resolved at call time, not import time, for the same reason.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def _windows_dir(xdg: str, var: str, fallback: str) -> Path:
    if os.environ.get(xdg):
        return Path(os.environ[xdg]) / "nearshare"
    return Path(os.environ.get(var) or str(Path.home() / fallback)) / "NearShare"


def config_dir() -> Path:
    """Small settings files: known device names, UI state."""
    if IS_WINDOWS:
        return _windows_dir("XDG_CONFIG_HOME", "APPDATA", "AppData/Roaming")
    home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(home) / "nearshare"


def data_dir() -> Path:
    """State that belongs to this machine: the AirDrop TLS identity."""
    if IS_WINDOWS:
        return _windows_dir("XDG_DATA_HOME", "LOCALAPPDATA", "AppData/Local")
    home = os.environ.get("XDG_DATA_HOME") or str(
        Path.home() / ".local" / "share")
    return Path(home) / "nearshare"


def runtime_dir() -> Path:
    """Where the running app publishes its control endpoint."""
    if IS_WINDOWS:
        if os.environ.get("XDG_RUNTIME_DIR"):
            return Path(os.environ["XDG_RUNTIME_DIR"])
        return _windows_dir("XDG_RUNTIME_DIR", "LOCALAPPDATA", "AppData/Local")
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
