"""Start NearShare when the user logs in.

NearShare can only receive while it is running, so "start at login,
hidden" is what makes a laptop reliably show up on a phone. Each install
layout has its own place for the autostart entry:

  * .deb and source checkout: an XDG autostart entry in
    ~/.config/autostart, the same mechanism every desktop reads.
  * snap: the snap declares `autostart: nearshare-autostart.desktop`
    for its gui app, and snapd's session agent runs it at login if a file
    of that name exists in $SNAP_USER_DATA/.config/autostart. A confined
    snap cannot write ~/.config/autostart itself. snapd takes only the
    arguments from the entry's Exec line and runs the snap's own command.
  * Flatpak: needs the Background portal, which is not wired up, so the
    setting is not offered there.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("nearshare.autostart")

SNAP_ENTRY_NAME = "nearshare-autostart.desktop"
XDG_ENTRY_NAME = "dev.dhivalabs.nearshare.desktop"

_ENTRY = """[Desktop Entry]
Type=Application
Name=NearShare
Comment=Start NearShare hidden so nearby devices can send files
Exec={exec}
Icon=dev.dhivalabs.nearshare
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
"""


def _in_snap() -> bool:
    return bool(os.environ.get("SNAP"))


def _in_flatpak() -> bool:
    return Path("/.flatpak-info").exists()


def supported() -> bool:
    return not _in_flatpak()


def entry_path() -> Path:
    if _in_snap():
        base = Path(os.environ.get("SNAP_USER_DATA")
                    or Path.home() / "snap" / "nearshare" / "current")
        return base / ".config" / "autostart" / SNAP_ENTRY_NAME
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "autostart" / XDG_ENTRY_NAME


def _exec_line() -> str:
    if _in_snap():
        return "nearshare.gui --background"
    from . import cli  # deferred: cli is a large module
    if cli._install_layout() == "system":
        launcher = cli._SYSTEM_LAUNCHER
    else:
        launcher = cli.BIN_SCRIPT
    # Desktop-entry quoting: a path with spaces must be double-quoted.
    return f'"{launcher}" gui --background'


def enabled() -> bool:
    return entry_path().is_file()


def set_enabled(on: bool) -> None:
    """Create or remove the autostart entry. Raises OSError on failure."""
    path = entry_path()
    if not on:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ENTRY.format(exec=_exec_line()))
