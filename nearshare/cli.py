"""Command-line control for the running NearShare app.

Talks JSON-lines to the Unix control socket the app's NearShareService
opens at nearshare.core.service.control_socket_path(): connect, send one
JSON request, read one JSON response line, print a human-friendly summary
(or the raw JSON with --json). See NearShareService._dispatch_control
for the request/response shapes.

Reality check: an Android phone (or any peer) only shows up in
`nearshare peers`, and can only be sent to, while its own Quick Share
receive screen (or share sheet) is open — Quick Share has no persistent
background discovery, so "not visible right now" is the normal state.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from .core.mdns import Peer
from .core.service import control_socket_path

SOCKET_TIMEOUT = 5.0
PEER_WAIT_TIMEOUT = 20.0

NOT_RUNNING_MSG = "NearShare app is not running"
NO_PEERS_MSG = ("No nearby devices found. On the target device, open Quick "
               "Share's receive screen (or the share sheet) so it becomes "
               "discoverable.")

# ---------------------------------------------------- install/uninstall

# This file lives at <project>/nearshare/cli.py, so its grandparent is
# the project root -- resolved once here rather than re-derived per call.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN_SCRIPT = PROJECT_ROOT / "bin" / "nearshare"
DESKTOP_DIR = PROJECT_ROOT / "data"
NAUTILUS_SCRIPT_NAME = "Send with NearShare"


def _desktop_file_names() -> list[str]:
    """Every data/*.desktop file, by name -- globbed at call time (not a
    hardcoded list) so a new .desktop file dropped into data/ is picked
    up by install/uninstall without a code change. In a package install
    there is no data/ directory, so fall back to the canonical names the
    package ships; an empty list here would make every "is it installed"
    check vacuously true."""
    names = sorted(p.name for p in DESKTOP_DIR.glob("*.desktop"))
    return names or ["dev.dhivalabs.nearshare.desktop",
                     "nearshare-send.desktop",
                     "nearshare-toggle.desktop"]


# ---------------------------------------------------------- control socket

def _control_request(req: dict[str, Any],
                     timeout: float = SOCKET_TIMEOUT) -> dict[str, Any] | None:
    """Send one JSON request to the running app; return its JSON response,
    or None if no app is listening on the control socket."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(control_socket_path()))
    except OSError:
        return None
    try:
        sock.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf) if buf else {"error": "empty response"}
    except OSError as exc:
        return {"error": str(exc)}
    finally:
        sock.close()


def _not_running() -> int:
    print(NOT_RUNNING_MSG, file=sys.stderr)
    return 1


def _launch_gui() -> None:
    """Start the GUI app detached, so the CLI/shortcut can return right
    away and the app keeps running after this process exits."""
    subprocess.Popen([sys.executable, "-m", "nearshare"],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _notify(summary: str, body: str) -> None:
    """Best-effort desktop notification; silently does nothing if
    notify-send isn't installed or fails."""
    if shutil.which("notify-send") is None:
        return
    try:
        subprocess.run(["notify-send", summary, body], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=2)
    except OSError:
        pass


# ---------------------------------------------------------------- status

def cmd_status(args: argparse.Namespace) -> int:
    resp = _control_request({"cmd": "status"})
    if resp is None:
        return _not_running()
    if args.json:
        print(json.dumps(resp))
        return 0
    state = "visible" if resp.get("visible") else "hidden"
    print(f"NearShare is {state}")
    print(f"  device name : {resp.get('device_name', '?')}")
    if resp.get("port") is not None:
        print(f"  listening   : port {resp['port']}")
    print(f"  peers seen  : {resp.get('peers', 0)}")
    return 0


# ------------------------------------------------------------ visibility

def _cmd_visibility(args: argparse.Namespace, cmd: str) -> int:
    resp = _control_request({"cmd": cmd})
    if resp is None:
        if cmd in ("on", "toggle"):
            _launch_gui()
            print(f"{NOT_RUNNING_MSG}; starting it now (python -m "
                 "nearshare)...")
            return 0
        return _not_running()
    if args.json:
        print(json.dumps(resp))
        return 0
    state = ("Visible to nearby devices" if resp.get("visible")
            else "Hidden from nearby devices")
    print(state)
    if cmd == "toggle":
        _notify("NearShare", state)
    return 0


def cmd_on(args: argparse.Namespace) -> int:
    return _cmd_visibility(args, "on")


def cmd_off(args: argparse.Namespace) -> int:
    return _cmd_visibility(args, "off")


def cmd_toggle(args: argparse.Namespace) -> int:
    return _cmd_visibility(args, "toggle")


# ------------------------------------------------------------------ peers

def _format_peers(peers: list[dict[str, Any]]) -> str:
    if not peers:
        return NO_PEERS_MSG
    lines = [f"  {p['name']!r}  ({p['host']}:{p['port']})" for p in peers]
    return "Nearby devices:\n" + "\n".join(lines)


def cmd_peers(args: argparse.Namespace) -> int:
    resp = _control_request({"cmd": "peers"})
    if resp is None:
        return _not_running()
    if args.json:
        print(json.dumps(resp))
        return 0
    print(_format_peers(resp.get("peers", [])))
    return 0


# ------------------------------------------------------------------ names

def _resolve_target_ip(target: str) -> str | None:
    """Accept an IP directly, or match a peer by its displayed name."""
    if all(part.isdigit() for part in target.split(".")) and \
            target.count(".") == 3:
        return target
    resp = _control_request({"cmd": "peers"})
    if resp is None:
        return None
    needle = target.lower()
    for peer in resp.get("peers", []):
        if needle in peer.get("name", "").lower():
            return peer.get("host")
    return None


def cmd_rename(args: argparse.Namespace) -> int:
    from .core import names
    ip = _resolve_target_ip(args.target)
    if ip is None:
        print(f"No device matching {args.target!r}. Pass an IP, or run "
              "`nearshare peers` while the app is running.", file=sys.stderr)
        return 1
    names.remember(ip, args.name, manual=True)
    print(f"{ip} will now show as {args.name!r}")
    print("Reopen the send picker (or restart the app) to see it.")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    from .core import names
    saved = names.all_names()
    if not args.target:
        if not saved:
            print("No saved device names.")
        for ip, name in sorted(saved.items()):
            print(f"  {ip:<16} {name}")
        return 0
    ip = _resolve_target_ip(args.target) or args.target
    if names.forget(ip):
        print(f"Forgot the saved name for {ip}")
        return 0
    print(f"No saved name for {ip}", file=sys.stderr)
    return 1


# -------------------------------------------------------------------- gui

def cmd_gui(args: argparse.Namespace) -> int:
    """Exec the GUI app in place of this process."""
    argv = [sys.executable, "-m", "nearshare"]
    if getattr(args, "background", False):
        argv.append("--background")
    os.execv(sys.executable, argv)
    return 0  # pragma: no cover - unreachable, execv replaces the process


# ------------------------------------------------------------------- send

def _resolve_files(paths: list[str]) -> tuple[list[str], list[str]]:
    """Return (absolute existing paths, originally-given missing paths)."""
    found, missing = [], []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_file():
            found.append(str(p))
        else:
            missing.append(raw)
    return found, missing


def cmd_send(args: argparse.Namespace) -> int:
    files, missing = _resolve_files(args.files)
    if missing:
        print(f"No such file(s): {', '.join(missing)}", file=sys.stderr)
        return 1

    status = _control_request({"cmd": "status"})
    if status is not None:
        return _send_via_socket(files, args.to, args.json)
    return asyncio.run(_send_ephemeral(files, args.to))


def _send_via_socket(files: list[str], to: str | None,
                     as_json: bool) -> int:
    peer_name = to
    if not peer_name:
        presp = _control_request({"cmd": "peers"})
        peers = (presp or {}).get("peers", [])
        if not peers:
            print(NO_PEERS_MSG, file=sys.stderr)
            return 2
        if len(peers) > 1:
            print("Multiple devices found; pick one with --to:")
            for p in peers:
                print(f"  {p['name']}")
            return 2
        peer_name = peers[0]["name"]

    resp = _control_request({"cmd": "send", "peer": peer_name,
                             "files": files})
    if resp is None:
        return _not_running()
    if as_json:
        print(json.dumps(resp))
        return 1 if "error" in resp else 0
    if "error" in resp:
        print(f"Send failed: {resp['error']}", file=sys.stderr)
        return 1
    print(f"Sent to {resp.get('sent_to', peer_name)}. Check that device's "
         "screen — it shows the PIN so you can confirm the transfer.")
    return 0


async def _wait_for_peer(service: "Any", to: str | None) -> Peer | None:
    """Poll the service's mDNS browser for up to PEER_WAIT_TIMEOUT seconds.
    Returns the matching/only peer, or None (ambiguous or none found)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + PEER_WAIT_TIMEOUT
    while loop.time() < deadline:
        if to:
            peer = service._match_peer(to)
            if peer is not None:
                return peer
        elif service.browser.peers:
            peers = list(service.browser.peers.values())
            return peers[0] if len(peers) == 1 else None
        await asyncio.sleep(0.3)
    return None


async def _send_ephemeral(files: list[str], to: str | None) -> int:
    """No app is running: start just enough of the service in-process to
    browse for peers and send directly, then tear it down."""
    from .core.connection import Events, OutboundConnection
    from .core.service import NearShareService

    service = NearShareService()
    print(f"{NOT_RUNNING_MSG}; looking for nearby devices as "
         f"{service.device_name!r} (waiting up to "
         f"{int(PEER_WAIT_TIMEOUT)}s)...")
    await service.browser.start()
    try:
        peer = await _wait_for_peer(service, to)
        if peer is None:
            if to:
                print(f"No device matching {to!r} found. On the target "
                     "device, open Quick Share's receive screen (or the "
                     "share sheet).", file=sys.stderr)
            elif len(service.browser.peers) > 1:
                print("Multiple devices found; pick one with --to:")
                for p in service.browser.peers.values():
                    print(f"  {p.device_name}")
            else:
                print(NO_PEERS_MSG, file=sys.stderr)
            return 2

        print(f"Found {peer.device_name!r} at {peer.host}:{peer.port}; "
             "connecting...")
        errors: list[str] = []
        events = Events(on_error=lambda dev, err: errors.append(err))
        paths = [Path(f) for f in files]
        conn = OutboundConnection(peer.host, peer.port, events,
                                  service.device_name, paths)
        run_task = asyncio.create_task(conn.run())
        pin_shown = False
        while not run_task.done():
            if conn.pin and not pin_shown:
                print(f"PIN: {conn.pin} — confirm it matches the "
                     f"other device's screen to complete the transfer.")
                pin_shown = True
            await asyncio.sleep(0.2)
        await run_task
        if errors:
            print(f"Send failed: {errors[0]}", file=sys.stderr)
            return 1
        print(f"Sent {len(paths)} file(s) to {peer.device_name}.")
        return 0
    except Exception as exc:  # surfaced as a clean CLI error, not a trace
        print(f"Send failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await service.browser.stop()


# --------------------------------------------------------------- install
#
# `nearshare install` wires the project into the desktop: a PATH symlink,
# the .desktop launchers, a Nautilus right-click script, and (see the
# gsettings section below) a GNOME keyboard shortcut. Every step is
# idempotent (safe to re-run) and every path is resolved through
# Path.home() / $XDG_DATA_HOME at call time (not import time) so tests
# can monkeypatch HOME/XDG_DATA_HOME and exercise the real code against
# a throwaway fake home directory. `nearshare uninstall` reverses each
# step, including the keyboard shortcut it created.
#
# Running inside the snap changes what's possible: strict confinement's
# `home` interface does not grant a snap access to hidden dot-directories
# under $HOME, so ~/.local/bin, ~/.local/share/applications, and
# ~/.local/share/nautilus* can never be written from in there -- every
# step that touches one of those is skipped with a one-line reason
# instead of crashing with a PermissionError. gsettings, however, DOES
# work under snap confinement (the gnome extension plugs it), so the
# keyboard shortcut is still bound. See _in_snap and _install_snap.


def _in_snap() -> bool:
    """True when this process is running inside the snap package (snapd
    always sets $SNAP for confined apps)."""
    return bool(os.environ.get("SNAP"))


def _xdg_data_home() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"


def _bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def _applications_dir() -> Path:
    return _xdg_data_home() / "applications"


def _nautilus_scripts_dir() -> Path:
    return _xdg_data_home() / "nautilus" / "scripts"


def _installed_bin_path() -> Path:
    return _bin_dir() / "nearshare"


def _path_has_dir(target: Path) -> bool:
    target_norm = os.path.normpath(str(target))
    return any(os.path.normpath(p) == target_norm
              for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def _install_symlink() -> None:
    target = _installed_bin_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() and target.resolve() == BIN_SCRIPT.resolve():
        print(f"  symlink already in place: {target} -> {BIN_SCRIPT}")
    else:
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(BIN_SCRIPT)
        print(f"  symlinked {target} -> {BIN_SCRIPT}")
    if not _path_has_dir(_bin_dir()):
        print(f"  NOTE: {_bin_dir()} is not on your PATH. Add a line like "
             f"`export PATH=\"{_bin_dir()}:$PATH\"` to your shell profile "
             "(e.g. ~/.bashrc), or keep using the full path to this "
             "symlink.")


def _uninstall_symlink() -> None:
    target = _installed_bin_path()
    if target.is_symlink() or target.exists():
        target.unlink()
        print(f"  removed symlink {target}")
    else:
        print(f"  no symlink at {target} (already removed)")


def _rewrite_exec(content: str, nearshare_bin: Path) -> str:
    """Point a .desktop file's Exec= line at the installed launcher.

    e.g. "Exec=nearshare gui" -> "Exec=/home/alice/.local/bin/nearshare
    gui" -- the absolute path so the entry works even if ~/.local/bin
    isn't on PATH for the process that launches .desktop files."""
    return re.sub(r"^Exec=nearshare\b", f"Exec={nearshare_bin}", content,
                 flags=re.MULTILINE)


def _update_desktop_database(apps_dir: Path) -> None:
    """Best-effort re-index so the new/removed launchers show up in GNOME's
    app grid and search without a re-login; silently skipped if the tool
    isn't installed."""
    if shutil.which("update-desktop-database") is None:
        print("  update-desktop-database not found; skipping (best-effort, "
             "not required for the entries to work)")
        return
    try:
        subprocess.run(["update-desktop-database", str(apps_dir)],
                       check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10)
        print(f"  ran update-desktop-database {apps_dir}")
    except OSError as exc:
        print(f"  update-desktop-database failed (non-fatal): {exc}")


def _install_desktop_files() -> None:
    apps_dir = _applications_dir()
    apps_dir.mkdir(parents=True, exist_ok=True)
    nearshare_bin = _installed_bin_path()
    for name in _desktop_file_names():
        content = (DESKTOP_DIR / name).read_text()
        rewritten = _rewrite_exec(content, nearshare_bin)
        dest = apps_dir / name
        if dest.exists() and dest.read_text() == rewritten:
            print(f"  {dest} already up to date")
        else:
            dest.write_text(rewritten)
            print(f"  installed {dest} (Exec rewritten to {nearshare_bin})")
    _update_desktop_database(apps_dir)


def _uninstall_desktop_files() -> None:
    apps_dir = _applications_dir()
    removed_any = False
    for name in _desktop_file_names():
        dest = apps_dir / name
        if dest.exists():
            dest.unlink()
            print(f"  removed {dest}")
            removed_any = True
        else:
            print(f"  {dest} not present (already removed)")
    if removed_any:
        _update_desktop_database(apps_dir)


_NAUTILUS_SCRIPT_TEMPLATE = """#!/usr/bin/env bash
# Installed by `nearshare install` -- re-run install to update, don't
# hand-edit (your changes would just be overwritten next install).
#
# Nautilus runs this with the user's selection in
# $NAUTILUS_SCRIPT_SELECTED_FILE_PATHS, one absolute path per line.
set -euo pipefail

files=()
while IFS= read -r line; do
    [[ -n "$line" ]] && files+=("$line")
done <<< "${{NAUTILUS_SCRIPT_SELECTED_FILE_PATHS:-}}"

if [[ ${{#files[@]}} -eq 0 ]]; then
    exit 0
fi

exec "{nearshare_bin}" send-picker "${{files[@]}}"
"""


_ICON_NAME = "dev.dhivalabs.nearshare.svg"
_ICON_SOURCE = PROJECT_ROOT / "data" / "icons" / _ICON_NAME
# Desktop files from older installs that current data/ no longer ships;
# cleaned up on install and uninstall.
_LEGACY_DESKTOP_FILES = ("nearshare.desktop",)


def _icon_dest() -> Path:
    return (_xdg_data_home() / "icons" / "hicolor" / "scalable" / "apps" /
            _ICON_NAME)


def _install_icon() -> None:
    dest = _icon_dest()
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = _ICON_SOURCE.read_text()
    if dest.exists() and dest.read_text() == content:
        print(f"  icon already in place: {dest}")
    else:
        dest.write_text(content)
        print(f"  installed icon {dest}")
    # Refresh the icon cache so GNOME picks it up without re-login.
    if shutil.which("gtk-update-icon-cache"):
        subprocess.run(
            ["gtk-update-icon-cache", "-t",
             str(_xdg_data_home() / "icons" / "hicolor")],
            check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10)


def _uninstall_icon() -> None:
    dest = _icon_dest()
    if dest.exists():
        dest.unlink()
        print(f"  removed icon {dest}")
    else:
        print(f"  {dest} not present (already removed)")


def _remove_legacy_desktop_files() -> None:
    apps_dir = _applications_dir()
    for name in _LEGACY_DESKTOP_FILES:
        dest = apps_dir / name
        if dest.exists():
            dest.unlink()
            print(f"  removed legacy {dest}")


# A nautilus-python extension gives a TOP-LEVEL context-menu item (the
# Scripts submenu is the best plain scripts can do). Requires the distro
# package `python3-nautilus`; installing the file without it is harmless.
_NAUTILUS_EXTENSION_TEMPLATE = '''"""Top-level "Send with NearShare" context-menu item for Nautilus.

Installed by `nearshare install`. Requires the python3-nautilus package
(apt). Nautilus loads this from ~/.local/share/nautilus-python/extensions.
"""
import subprocess
from urllib.parse import unquote, urlparse

from gi.repository import GObject, Nautilus


class NearShareMenu(GObject.GObject, Nautilus.MenuProvider):
    def get_file_items(self, files):
        paths = []
        for f in files:
            if f.get_uri_scheme() != "file" or f.is_directory():
                return []
            paths.append(unquote(urlparse(f.get_uri()).path))
        if not paths:
            return []
        item = Nautilus.MenuItem(
            name="NearShareMenu::send",
            label="Send with NearShare",
            tip="Send the selected files to a nearby device")
        item.connect("activate", self._activate, paths)
        return [item]

    def _activate(self, _item, paths):
        subprocess.Popen(["{launcher}", "send-picker", *paths],
                         start_new_session=True)
'''


def _nautilus_extensions_dir() -> Path:
    return _xdg_data_home() / "nautilus-python" / "extensions"


def _nautilus_python_available() -> bool:
    """True if the python3-nautilus distro package is importable."""
    try:
        import gi
        gi.require_version("Nautilus", "4.0")
        return True
    except (ImportError, ValueError):
        return False


def _install_nautilus_extension() -> None:
    ext_dir = _nautilus_extensions_dir()
    ext_dir.mkdir(parents=True, exist_ok=True)
    dest = ext_dir / "nearshare_menu.py"
    content = _NAUTILUS_EXTENSION_TEMPLATE.replace(
        "{launcher}", str(_installed_bin_path()))
    if dest.exists() and dest.read_text() == content:
        print(f"  extension already in place: {dest}")
    else:
        dest.write_text(content)
        print(f"  installed {dest}")
    if _nautilus_python_available():
        print("  python3-nautilus detected -- top-level right-click item "
              "active after: nautilus -q")
    else:
        print("  NOTE: the top-level right-click item needs one system "
              "package. Run this yourself, then restart Files:")
        print("      sudo apt install python3-nautilus && nautilus -q")
        print("  (Until then the item lives under right-click -> Scripts.)")


def _uninstall_nautilus_extension() -> None:
    dest = _nautilus_extensions_dir() / "nearshare_menu.py"
    if dest.exists():
        dest.unlink()
        print(f"  removed {dest} (restart Files: nautilus -q)")
    else:
        print(f"  {dest} not present (already removed)")


def _install_nautilus_script() -> None:
    scripts_dir = _nautilus_scripts_dir()
    scripts_dir.mkdir(parents=True, exist_ok=True)
    dest = scripts_dir / NAUTILUS_SCRIPT_NAME
    content = _NAUTILUS_SCRIPT_TEMPLATE.format(
        nearshare_bin=_installed_bin_path())
    already_current = (dest.exists() and dest.read_text() == content and
                       os.access(dest, os.X_OK))
    if already_current:
        print(f"  {dest} already up to date")
    else:
        dest.write_text(content)
        dest.chmod(0o755)
        print(f"  installed Nautilus script: {dest}")
    print("  restart Nautilus for the script to appear: nautilus -q")


def _uninstall_nautilus_script() -> None:
    dest = _nautilus_scripts_dir() / NAUTILUS_SCRIPT_NAME
    if dest.exists():
        dest.unlink()
        print(f"  removed Nautilus script {dest}")
        print("  restart Nautilus for the removal to take effect: "
             "nautilus -q")
    else:
        print(f"  {dest} not present (already removed)")


# ---------------------------------------------------- GNOME shortcut
#
# `nearshare install` binds a GNOME custom keybinding to `nearshare
# toggle` via the gsettings CLI (rather than the Gio bindings module:
# querying schema existence through Gio.Settings.new() aborts the whole
# process with a fatal GLib-GIO-ERROR if the schema isn't installed --
# not a catchable exception -- whereas the gsettings CLI just exits
# non-zero, which we can check for cleanly. This matters because inside
# the snap, org.gnome.settings-daemon.plugins.media-keys' schema is NOT
# among the schemas the confined process can see (it ships on the host
# via the gnome-settings-daemon package, not the gnome platform content
# snap, and AppArmor blocks reading the host's
# /usr/share/glib-2.0/schemas directly) -- verified empirically with
# `snap run --shell nearshare -c gsettings list-schemas`, which lists ~55
# schemas from the bundled gnome platform but not this one. So
# _gsettings_available() below returns False under snap today; if a
# future base snap ever bundles that schema this code picks it up
# automatically, no snap-specific branch needed here.
#
# Default key combo is Ctrl+Alt+N, not a Super-chord: some keyboards
# (and some window managers/remote desktops) have no usable Super key.
# Checked for collisions against a real GNOME session's
# org.gnome.desktop.wm.keybindings, org.gnome.shell.keybindings,
# org.gnome.mutter(.wayland).keybindings, and
# org.gnome.settings-daemon.plugins.media-keys (`gsettings
# list-recursively <schema>`) -- nothing in any of them uses
# <Control><Alt>n. `--key` (cmd_install) overrides it with any other
# accelerator string.
#
# All gsettings CLI invocations funnel through _run_gsettings so tests
# can monkeypatch that single choke point instead of touching the real
# user's GNOME settings.

_MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
_KEYBINDING_SCHEMA = f"{_MEDIA_KEYS_SCHEMA}.custom-keybinding"
_KEYBINDING_ID = "nearshare-toggle"
_KEYBINDING_PATH = ("/org/gnome/settings-daemon/plugins/media-keys/"
                    f"custom-keybindings/{_KEYBINDING_ID}/")
DEFAULT_SHORTCUT_BINDING = "<Control><Alt>n"
_SHORTCUT_NAME = "NearShare: Toggle Visibility"

_ACCEL_MODIFIER_LABELS = {"control": "Ctrl", "primary": "Ctrl", "alt": "Alt",
                         "shift": "Shift", "super": "Super", "meta": "Meta",
                         "hyper": "Hyper"}
_ACCEL_RE = re.compile(
    r"^(?:<(?:Control|Primary|Alt|Shift|Super|Meta|Hyper)>)+\S+$",
    re.IGNORECASE)


def _try_gtk_accelerator_parse(accel: str) -> tuple[int, int] | bool | None:
    """(keyval, mods) via Gtk.accelerator_parse if PyGObject/GTK4 is
    importable in this process; False if GTK is importable but says
    `accel` itself is invalid; None if GTK isn't importable at all here
    (cli.py has to keep working without GTK installed -- e.g. a
    headless box only ever running `nearshare on/off/status` -- so
    validate_accelerator()/accelerator_label() fall back to a regex/
    manual parse in that case, not to a hard dependency on GTK)."""
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
    except (ImportError, ValueError):
        return None
    ok, keyval, mods = Gtk.accelerator_parse(accel)
    if not ok or keyval == 0:
        return False
    return (keyval, mods)


def validate_accelerator(accel: str) -> str | None:
    """None if `accel` is a syntactically valid GNOME/GTK accelerator
    string (e.g. "<Control><Alt>n"); otherwise a human-readable reason
    it was rejected, for `nearshare install --key` to print instead of
    writing a broken binding."""
    if not accel:
        return "empty accelerator"
    result = _try_gtk_accelerator_parse(accel)
    if result is False:
        return (f"{accel!r} isn't a valid accelerator -- try something "
               "like \"<Control><Alt>n\"")
    if result is not None:
        return None
    # GTK unavailable in this process: fall back to a syntax check.
    if not _ACCEL_RE.match(accel):
        return (f"{accel!r} doesn't look like a valid accelerator -- "
               "expected modifiers in angle brackets followed by a key "
               "name, e.g. \"<Control><Alt>n\"")
    return None


def accelerator_label(accel: str) -> str:
    """Human-readable form of a gsettings accelerator string, e.g.
    "<Control><Alt>n" -> "Ctrl+Alt+N" -- used for display in the CLI's
    own messages and the GUI's setup panel instead of raw gsettings
    syntax."""
    result = _try_gtk_accelerator_parse(accel)
    if result and result is not False:
        keyval, mods = result
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        label = Gtk.accelerator_get_label(keyval, mods)
        if label:
            return label
    # Fallback: translate our own modifier tokens and title-case the key.
    mod_tokens = re.findall(r"<(\w+)>", accel)
    key = re.sub(r"<\w+>", "", accel)
    parts = [_ACCEL_MODIFIER_LABELS.get(m.lower(), m) for m in mod_tokens]
    parts.append(key.upper() if len(key) == 1 else key.capitalize())
    return "+".join(parts)


def _run_gsettings(args: list[str]) -> subprocess.CompletedProcess:
    """The sole entry point to the gsettings CLI -- monkeypatched by
    tests so shortcut bind/unbind logic never touches the real user's
    dconf database.

    Never raises: a transient failure (gsettings hanging because dconf
    is momentarily busy, the binary disappearing mid-run, etc.) must
    surface as an ordinary non-zero-returncode result that callers
    already handle, not an exception that could crash the GTK app's
    Finish-setup banner or a bare `nearshare install`."""
    try:
        return subprocess.run(["gsettings", *args], capture_output=True,
                              text=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def _gsettings_available() -> bool:
    """True if the gsettings CLI is installed AND the media-keys schema
    it needs is visible to this process (see the module comment above
    for why that second check can fail even with gsettings itself
    present, e.g. under snap confinement)."""
    if shutil.which("gsettings") is None:
        return False
    try:
        result = _run_gsettings(["list-schemas"])
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and _MEDIA_KEYS_SCHEMA in result.stdout.split()


def _parse_gsettings_strv(text: str) -> list[str]:
    """Parse gsettings' own literal syntax for an array of strings, e.g.
    "@as []" (empty, type-annotated) or "['/a/', '/b/']" (a valid Python
    list literal once the optional "@as " type prefix is stripped)."""
    text = text.strip()
    if text.startswith("@as "):
        text = text[len("@as "):]
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _custom_keybindings() -> list[str] | None:
    """Current custom-keybindings path list, or None if the read failed."""
    result = _run_gsettings(["get", _MEDIA_KEYS_SCHEMA, "custom-keybindings"])
    if result.returncode != 0:
        return None
    return _parse_gsettings_strv(result.stdout)


def _keybinding_get(path: str, key: str) -> str | None:
    result = _run_gsettings(["get", f"{_KEYBINDING_SCHEMA}:{path}", key])
    if result.returncode != 0:
        return None
    return result.stdout.strip().strip("'\"")


def _write_custom_keybindings_list(paths: list[str]) -> bool:
    value = ("[" + ", ".join(f"'{p}'" for p in paths) + "]") if paths else "@as []"
    result = _run_gsettings(["set", _MEDIA_KEYS_SCHEMA, "custom-keybindings", value])
    if result.returncode != 0:
        print(f"  gsettings set custom-keybindings failed: {result.stderr.strip()}")
        return False
    return True


def _find_binding_conflict(existing_paths: list[str], accel: str) -> str | None:
    """Return the display name of whatever *other* custom keybinding
    already uses `accel`, or None if the combo is free -- so we never
    clobber an unrelated shortcut that happens to sit on the same key."""
    for path in existing_paths:
        if path == _KEYBINDING_PATH:
            continue
        if _keybinding_get(path, "binding") == accel:
            return _keybinding_get(path, "name") or path
    return None


def _shortcut_is_ours() -> bool:
    """True if our keybinding is currently registered and pointed at our
    own launcher (used by `nearshare install`'s idempotency check and by
    the GUI's integration_status())."""
    if not _gsettings_available():
        return False
    existing = _custom_keybindings()
    if existing is None or _KEYBINDING_PATH not in existing:
        return False
    return _keybinding_get(_KEYBINDING_PATH, "command") == _wanted_shortcut_command()


def bound_shortcut_accelerator() -> str | None:
    """The accelerator string currently bound at our keybinding path (in
    raw gsettings syntax, e.g. "<Control><Alt>n"), or None if nothing of
    ours is bound -- used by the GUI to show the actual accelerator in
    human-readable form via accelerator_label()."""
    if not _shortcut_is_ours():
        return None
    return _keybinding_get(_KEYBINDING_PATH, "binding")


def _wanted_shortcut_command() -> str:
    return f"{_installed_bin_path()} toggle"


def _bind_shortcut(key: str | None = None) -> None:
    """Bind `key` (default DEFAULT_SHORTCUT_BINDING) to `nearshare
    toggle` as a GNOME custom keybinding. Safe to call repeatedly: does
    nothing if already bound to us with this same key, updates the
    entry if our launcher's path or the requested key changed, and
    refuses (with a warning) to touch a key combo something else
    already owns. Rejects a syntactically invalid `key` (from
    `nearshare install --key`) up front, before writing anything."""
    accel = key or DEFAULT_SHORTCUT_BINDING
    error = validate_accelerator(accel)
    if error is not None:
        print(f"  invalid --key value: {error}")
        return
    label = accelerator_label(accel)

    if not _gsettings_available():
        print("  skipped: the GNOME media-keys gsettings schema isn't "
             "available in this environment (see docs/SHORTCUT.md to "
             "bind it manually)")
        return
    existing = _custom_keybindings()
    if existing is None:
        print("  skipped: couldn't read existing GNOME keybindings "
             "(gsettings failed)")
        return

    wanted_command = _wanted_shortcut_command()
    if _KEYBINDING_PATH in existing:
        current_command = _keybinding_get(_KEYBINDING_PATH, "command")
        current_binding = _keybinding_get(_KEYBINDING_PATH, "binding")
        if current_command == wanted_command and current_binding == accel:
            print(f"  keyboard shortcut already bound: {label} -> "
                 f"{wanted_command}")
            return
        # Ours, but stale (e.g. the launcher moved, or --key changed) --
        # refresh it.
        if _write_keybinding_entry(accel):
            print(f"  updated keyboard shortcut: {label} -> {wanted_command}")
        return

    conflict = _find_binding_conflict(existing, accel)
    if conflict is not None:
        print(f"  NOTE: {label} is already bound to {conflict!r}; leaving "
             "it alone. Pass a different --key to `nearshare install` if "
             "you want NearShare's toggle shortcut too.")
        return

    if _write_custom_keybindings_list(existing + [_KEYBINDING_PATH]) and \
            _write_keybinding_entry(accel):
        print(f"  bound {label} to toggle visibility ({wanted_command})")


def _write_keybinding_entry(accel: str) -> bool:
    for key, value in (("name", _SHORTCUT_NAME), ("command", _wanted_shortcut_command()),
                       ("binding", accel)):
        result = _run_gsettings(["set", f"{_KEYBINDING_SCHEMA}:{_KEYBINDING_PATH}",
                                 key, value])
        if result.returncode != 0:
            print(f"  gsettings set {key} failed: {result.stderr.strip()}")
            return False
    return True


def _unbind_shortcut() -> None:
    """Remove the keybinding `_bind_shortcut` created, if any. Never
    touches a binding at our key path that isn't ours, and never fails
    uninstall -- gsettings being unavailable just means there was
    nothing of ours to remove."""
    if not _gsettings_available():
        print("  skipped: the GNOME media-keys gsettings schema isn't "
             "available in this environment")
        return
    existing = _custom_keybindings()
    if existing is None:
        print("  skipped: couldn't read existing GNOME keybindings "
             "(gsettings failed)")
        return
    if _KEYBINDING_PATH not in existing:
        print("  no keyboard shortcut to remove (already unbound)")
        return
    label = accelerator_label(
        _keybinding_get(_KEYBINDING_PATH, "binding") or DEFAULT_SHORTCUT_BINDING)
    remaining = [p for p in existing if p != _KEYBINDING_PATH]
    if not _write_custom_keybindings_list(remaining):
        return
    for key in ("name", "command", "binding"):
        _run_gsettings(["reset", f"{_KEYBINDING_SCHEMA}:{_KEYBINDING_PATH}", key])
    print(f"  removed keyboard shortcut ({label})")


# --------------------------------------------------------- install/uninstall

def _fs_step(fn) -> bool:
    """Run one filesystem install/uninstall step, catching PermissionError
    so a locked-down ~/.local (or anything else denying access) prints a
    readable one-line message instead of an unhandled traceback. Returns
    False if the step failed."""
    try:
        fn()
        return True
    except PermissionError as exc:
        path = exc.filename or str(exc)
        print(f"  permission denied: {path} -- skipping this step "
             "(fix the permissions, or run from an account that owns "
             "that path, and re-run install)")
        return False
    except OSError as exc:
        # Anything else filesystem-shaped (missing source assets, a
        # read-only mount, ...) must degrade to a message too -- an
        # unhandled [Errno 2] traceback reaching the GUI toast is
        # exactly the bug this wrapper exists to prevent.
        path = getattr(exc, "filename", None) or str(exc)
        print(f"  step failed ({exc.__class__.__name__}): {path} -- "
              "skipping")
        return False


def _skip(what: str, reason: str) -> None:
    print(f"  skipped: {what} -- {reason}")


_SNAP_HOME_REASON = ("strict snap confinement's `home` interface doesn't "
                     "grant access to hidden dot-directories under $HOME")


# Plugs snapd does not connect automatically (see snap/snapcraft.yaml).
SNAP_MANUAL_INTERFACES = ("bluez", "network-manager")


def snap_interface_connected(name: str) -> bool | None:
    """Whether the snap's plug `name` is connected; None if that cannot
    be determined (not in a snap, or snapctl missing or failing)."""
    if not _in_snap():
        return None
    try:
        proc = subprocess.run(["snapctl", "is-connected", name],
                              capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def integration_status() -> dict[str, Any]:
    """What `nearshare install` has already accomplished -- used by its
    own idempotency messages and by the GTK app's Finish-setup banner
    (nearshare/ui/app.py) to decide what to show. `complete` accounts for
    the snap's structural limits: under snap, desktop entries/Nautilus
    integration can never be done from inside the app (see module
    docstring), so completeness there only requires what IS possible
    (the launcher already being on PATH via /snap/bin, and the
    shortcut)."""
    snap = _in_snap()
    # Inside the snap the sandbox's own PATH doesn't contain /snap/bin,
    # so which() misses the launcher the user's shell can actually see.
    launcher_on_path = (shutil.which("nearshare") is not None or
                        (snap and Path("/snap/bin/nearshare").exists()))
    # Per-user (~/.local, source-checkout install) or system-wide (.deb)
    # both count as done -- the .deb ships these under /usr.
    desktop_entries = all(
        (_applications_dir() / name).exists() or
        (_SYSTEM_APPLICATIONS_DIR / name).exists()
        for name in _desktop_file_names())
    nautilus = ((_nautilus_scripts_dir() / NAUTILUS_SCRIPT_NAME).exists() or
               (_nautilus_extensions_dir() / "nearshare_menu.py").exists() or
               _SYSTEM_NAUTILUS_EXTENSION.exists())
    shortcut = _shortcut_is_ours()
    shortcut_accel = bound_shortcut_accelerator() if shortcut else None
    # Distinguish "not done" from "cannot be done here". Under snap the
    # home directory is off-limits and the GNOME keybinding schema is
    # invisible, so offering to fix either would be an empty promise --
    # the button would run and change nothing.
    shortcut_fixable = _gsettings_available()
    home_fixable = not snap
    nautilus_fixable = not snap
    if snap:
        complete = launcher_on_path and (shortcut or not shortcut_fixable)
    else:
        complete = launcher_on_path and desktop_entries and nautilus and shortcut
    anything_fixable = ((not shortcut and shortcut_fixable) or
                        (not nautilus and nautilus_fixable) or
                        (not desktop_entries and home_fixable) or
                        (not launcher_on_path and home_fixable))
    snap_interfaces = ({name: snap_interface_connected(name)
                        for name in SNAP_MANUAL_INTERFACES} if snap else {})
    return {"snap": snap, "snap_interfaces": snap_interfaces,
            "launcher_on_path": launcher_on_path,
            "desktop_entries": desktop_entries, "nautilus": nautilus,
            "shortcut": shortcut, "shortcut_accel": shortcut_accel,
            "shortcut_fixable": shortcut_fixable,
            "home_fixable": home_fixable,
            "nautilus_fixable": nautilus_fixable,
            "anything_fixable": anything_fixable,
            "complete": complete}


def _install_snap(shortcut: bool, key: str | None) -> int:
    print("Installing NearShare desktop integration (running inside the "
         "snap)...")
    print("[1/2] CLI launcher symlink, desktop entries, Nautilus integration")
    _skip("~/.local/bin launcher symlink", _SNAP_HOME_REASON +
         " (the snap's own launcher is already on PATH as `nearshare`)")
    _skip("desktop entries + app icon (~/.local/share)", _SNAP_HOME_REASON)
    _skip("Nautilus \"Send with NearShare\" script", _SNAP_HOME_REASON)
    _skip("Nautilus top-level right-click item (extension)", _SNAP_HOME_REASON)
    print(f"[2/2] Keyboard shortcut ({accelerator_label(key or DEFAULT_SHORTCUT_BINDING)}"
         " -> toggle visibility)")
    if shortcut:
        _bind_shortcut(key)
    else:
        print("  --no-shortcut passed; leaving any existing binding alone")
    print("\nInstall complete for everything possible from inside the snap.")
    print("For right-click \"Send with NearShare\" in Files, install the "
         "Debian package instead:")
    print("    sudo add-apt-repository ppa:dhiva-labs/apps")
    print("    sudo apt install nearshare")
    return 0


# The .deb installs everything system-wide; only /usr/bin/nearshare is a
# reliable marker for "this is a package install, not a source checkout".
_SYSTEM_LAUNCHER = Path("/usr/bin/nearshare")
_SYSTEM_APPLICATIONS_DIR = Path("/usr/share/applications")
_SYSTEM_NAUTILUS_EXTENSION = Path(
    "/usr/share/nautilus-python/extensions/nearshare_menu.py")


def _install_layout() -> str:
    """Which of the three install layouts this process runs from.

    "source"  -- a git/source checkout: bin/ and data/ exist next to the
                 package, and install copies them into ~/.local.
    "system"  -- the .deb: the launcher, desktop entries, icon and the
                 Nautilus extension were installed by the package into
                 /usr; there is nothing to copy (and PROJECT_ROOT has no
                 bin/ or data/, which is exactly why running the
                 source-checkout steps here dies with [Errno 2]).
    "snap"    -- strict confinement; see _install_snap.
    """
    if _in_snap():
        return "snap"
    if BIN_SCRIPT.exists() and DESKTOP_DIR.exists():
        return "source"
    if _SYSTEM_LAUNCHER.exists():
        return "system"
    return "source"  # unknown -> attempt the full path, _fs_step catches


def _install_system(shortcut: bool, key: str | None) -> int:
    print("Installing NearShare desktop integration (package install)...")
    print("[1/2] Launcher, desktop entries, icon, Nautilus integration")
    print("  all provided system-wide by the package -- nothing to do")
    if not _SYSTEM_NAUTILUS_EXTENSION.exists():
        print("  NOTE: the Files right-click extension is missing; "
              "reinstall the package, and make sure python3-nautilus is "
              "installed, then restart Files (nautilus -q)")
    print(f"[2/2] Keyboard shortcut ({accelerator_label(key or DEFAULT_SHORTCUT_BINDING)}"
         " -> toggle visibility)")
    if shortcut:
        _bind_shortcut(key)
    else:
        print("  --no-shortcut passed; leaving any existing binding alone")
    print("\nInstall complete.")
    return 0


def _install_full(shortcut: bool, key: str | None) -> int:
    print("Installing NearShare desktop integration...")
    print("[1/5] CLI launcher symlink")
    _fs_step(_install_symlink)
    print("[2/5] Desktop entries + app icon (~/.local/share)")
    _fs_step(_remove_legacy_desktop_files)
    _fs_step(_install_desktop_files)
    _fs_step(_install_icon)
    print("[3/5] Nautilus \"Send with NearShare\" script (Scripts submenu)")
    _fs_step(_install_nautilus_script)
    print("[4/5] Nautilus top-level right-click item (extension)")
    _fs_step(_install_nautilus_extension)
    print(f"[5/5] Keyboard shortcut ({accelerator_label(key or DEFAULT_SHORTCUT_BINDING)}"
         " -> toggle visibility)")
    if shortcut:
        _bind_shortcut(key)
    else:
        print("  --no-shortcut passed; leaving any existing binding alone")
    print("\nInstall complete.")
    return 0


def _run_install(shortcut: bool, key: str | None = None) -> int:
    layout = _install_layout()
    if layout == "snap":
        return _install_snap(shortcut, key)
    if layout == "system":
        return _install_system(shortcut, key)
    return _install_full(shortcut, key)


def run_install(shortcut: bool = True, key: str | None = None) -> str:
    """Run the same install logic `nearshare install` uses, capturing its
    printed output as a string instead of writing to stdout -- what the
    GTK app's Finish-setup button (nearshare/ui/app.py) calls, since it
    has no terminal to show output in."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _run_install(shortcut, key)
    return buf.getvalue()


def cmd_install(args: argparse.Namespace) -> int:
    key = getattr(args, "key", None)
    if key is not None:
        error = validate_accelerator(key)
        if error is not None:
            print(f"invalid --key value: {error}", file=sys.stderr)
            return 1
    return _run_install(getattr(args, "shortcut", True), key)


def _uninstall_snap() -> int:
    print("Uninstalling NearShare desktop integration (running inside the "
         "snap)...")
    print("[1/2] CLI launcher symlink, desktop entries, Nautilus integration")
    _skip("~/.local/bin launcher symlink", _SNAP_HOME_REASON)
    _skip("desktop entries + app icon", _SNAP_HOME_REASON)
    _skip("Nautilus \"Send with NearShare\" script", _SNAP_HOME_REASON)
    _skip("Nautilus extension", _SNAP_HOME_REASON)
    print("[2/2] Keyboard shortcut")
    _unbind_shortcut()
    print("\nUninstall complete for everything possible from inside the "
         "snap.")
    return 0


def _uninstall_full() -> int:
    print("Uninstalling NearShare desktop integration...")
    print("[1/5] CLI launcher symlink")
    _fs_step(_uninstall_symlink)
    print("[2/5] Desktop entries + app icon")
    _fs_step(_remove_legacy_desktop_files)
    _fs_step(_uninstall_desktop_files)
    _fs_step(_uninstall_icon)
    print("[3/5] Nautilus \"Send with NearShare\" script")
    _fs_step(_uninstall_nautilus_script)
    print("[4/5] Nautilus extension")
    _fs_step(_uninstall_nautilus_extension)
    print("[5/5] Keyboard shortcut")
    _unbind_shortcut()
    print("\nUninstall complete.")
    return 0


def _uninstall_system() -> int:
    print("Uninstalling NearShare desktop integration (package install)...")
    print("[1/2] Launcher, desktop entries, icon, Nautilus integration")
    print("  owned by the package -- remove with: sudo apt remove nearshare")
    print("[2/2] Keyboard shortcut")
    _unbind_shortcut()
    print("\nDone.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    layout = _install_layout()
    if layout == "snap":
        return _uninstall_snap()
    if layout == "system":
        return _uninstall_system()
    return _uninstall_full()


# -------------------------------------------------------------- picker

def cmd_send_picker(args: argparse.Namespace) -> int:
    """Launch the GTK "send to nearby device" dialog (nearshare/ui/
    picker.py) for files already resolved on the command line -- this is
    what the installed Nautilus script execs, so it must work with no
    terminal attached and whether or not the main GUI app is running."""
    files, missing = _resolve_files(args.files)
    if missing:
        print(f"No such file(s): {', '.join(missing)}", file=sys.stderr)
    if not files:
        return 1
    from .ui.picker import main as picker_main
    return picker_main(files)


# ------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nearshare",
        description="Control the NearShare app: toggle visibility, list "
                    "nearby devices, and send files, all without opening "
                    "the GUI.",
        epilog="Reality check: nearby devices (including an Android phone) "
              "only show up here while their own Quick Share receive "
              "screen is open; there is no persistent background "
              "discovery.")

    from . import __version__
    parser.add_argument("--version", action="version",
                        version=f"NearShare {__version__}")

    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true",
                             help="print the raw JSON response")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("on", parents=[json_parent],
                       help="make this machine visible to nearby devices")
    p.set_defaults(func=cmd_on)

    p = sub.add_parser("off", parents=[json_parent],
                       help="hide this machine from nearby devices")
    p.set_defaults(func=cmd_off)

    p = sub.add_parser("toggle", parents=[json_parent],
                       help="flip visibility and notify the new state")
    p.set_defaults(func=cmd_toggle)

    p = sub.add_parser("status", parents=[json_parent],
                       help="show visibility, device name, and peer count")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("peers", parents=[json_parent],
                       help="list nearby discoverable devices")
    p.set_defaults(func=cmd_peers)

    p = sub.add_parser("gui", help="launch the NearShare GUI")
    p.add_argument("--background", action="store_true",
                   help="start without showing the window (what Start at "
                        "login uses); does nothing if already running")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser(
        "rename", help="give a nearby device a friendly name",
        description="Assign a permanent name to a device by IP. Modern "
                    "Android hides its user-set name in advertisements, "
                    "so NearShare otherwise shows e.g. 'Phone (6SWJ)' "
                    "until that device sends you something.")
    p.add_argument("target", metavar="IP_OR_NAME",
                   help="device IP (see `nearshare peers`), or its "
                        "currently-shown name")
    p.add_argument("name", help="the name to show from now on")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("forget", help="remove a saved device name")
    p.add_argument("target", metavar="IP_OR_NAME", nargs="?",
                   help="device to forget; omit to list saved names")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser(
        "send", parents=[json_parent],
        help="send file(s) to a nearby device",
        description="Send file(s) to a nearby device. The target only "
                    "shows up while its Quick Share receive screen (or "
                    "share sheet) is open.")
    p.add_argument("files", nargs="+", help="file(s) to send")
    p.add_argument("--to", metavar="NAME",
                  help="target device name (default: the only peer "
                       "found, or an error if there are several)")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser(
        "send-picker",
        help="GTK dialog to pick a nearby device to send file(s) to",
        description="Open a small GTK dialog listing nearby devices for "
                    "the given file(s); used by the Nautilus right-click "
                    "script installed by `nearshare install`, but can be "
                    "run directly too.")
    p.add_argument("files", nargs="+", help="file(s) to send")
    p.set_defaults(func=cmd_send_picker)

    p = sub.add_parser(
        "install",
        help="install the PATH symlink, .desktop launchers, Nautilus "
            "right-click script, and keyboard shortcut (idempotent; "
            "does whatever subset is possible when run inside the snap)")
    p.add_argument(
        "--shortcut", action=argparse.BooleanOptionalAction, default=True,
        help=f"bind a keyboard shortcut to toggle visibility, default "
            f"{DEFAULT_SHORTCUT_BINDING} i.e. Ctrl+Alt+N (default: yes); "
            "--no-shortcut leaves any existing binding alone")
    p.add_argument(
        "--key", metavar="ACCELERATOR", default=None,
        help="use this accelerator instead of the default, e.g. "
            "\"<Control><Alt>s\" (GTK/gsettings accelerator syntax: "
            "modifiers in angle brackets, then a key name); rejected "
            "with an error if it doesn't parse as a valid accelerator")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser(
        "uninstall",
        help="remove everything `nearshare install` set up")
    p.set_defaults(func=cmd_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
