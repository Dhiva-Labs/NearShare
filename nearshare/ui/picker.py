"""Standalone GTK4 + libadwaita "send to nearby device" dialog.

Launched by `nearshare send-picker FILE...` (see cli.py's
cmd_send_picker), which in turn is what the Nautilus "Send with
NearShare" script installed by `nearshare install` execs for the
files selected in the file manager -- see cli.py's
_install_nautilus_script. That means this module must come up and work
with **no terminal attached** (stdout/stderr may go nowhere) and
**regardless of whether the main NearShare app (ui/app.py) is already
running**, since a user might right-click a file whether or not they
have the NearShare window open.

Two independently-varying things, each with its own fallback:

  * Peer discovery: if the main app is running, its control socket
    (core/service.py's `peers` command) already has a live mDNS browser
    doing this work, so we just poll it rather than duplicate a second
    browser. If nothing answers the socket, this process starts its own
    ephemeral, UI-less NearShareService and drives its Browser directly
    (mirrors cli.py's `_send_ephemeral`, adapted to push updates into a
    dialog instead of a terminal).

  * Sending: always done as our own in-process `OutboundConnection`,
    never via the control socket's `send` command, even when the main
    app is running. That command blocks until the whole transfer
    finishes and only ever reports {"ok": ...}/{"error": ...} -- no PIN,
    no progress -- because core/service.py's control protocol was never
    extended to stream either (and core/ isn't ours to touch this
    round). Sending doesn't require binding a receive port, so running
    our own OutboundConnection alongside an already-running app is safe
    and gets us both a live progress bar and the PIN this dialog is
    required to show.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

# --- gi import glue ---------------------------------------------------
# Copied from nearshare/__main__.py (owned by another engineer this
# round, and not something this task may edit or import from): PyGObject
# and the GTK4/libadwaita typelibs come from the distro's python3-gi /
# gir1.2-gtk-4.0 / gir1.2-adw-1 packages under
# /usr/lib/python3/dist-packages, not from this project's venv (built
# without --system-site-packages), so a bare `import gi` fails unless we
# add the system dist-packages directory to sys.path ourselves first.


def _ensure_gi_importable() -> None:
    try:
        import gi  # noqa: F401
        return
    except ImportError:
        pass

    candidates = [
        "/usr/lib/python3/dist-packages",
        f"/usr/lib/python3.{sys.version_info.minor}/dist-packages",
        "/usr/local/lib/python3/dist-packages",
    ]
    for path in candidates:
        if path not in sys.path:
            sys.path.append(path)

    try:
        import gi  # noqa: F401
    except ImportError as exc:
        sys.exit(
            "nearshare: could not import PyGObject ('gi') even after "
            "adding the system dist-packages directories to sys.path. "
            "Install python3-gi, gir1.2-gtk-4.0, and gir1.2-adw-1 (e.g. "
            "`sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`) "
            f"and try again.\n({exc})")


_ensure_gi_importable()

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ..core.connection import Events, OutboundConnection  # noqa: E402
from ..core.mdns import Peer  # noqa: E402
from ..core import control  # noqa: E402
from ..core.service import (NearShareService,  # noqa: E402
                            default_device_name)
from .asyncio_glue import AsyncioThread, idle  # noqa: E402

log = logging.getLogger("nearshare.ui.picker")

PROBE_TIMEOUT = 1.5   # quick "is the app running" check
SOCKET_TIMEOUT = 5.0  # normal peers-poll request/response
POLL_INTERVAL_S = 2
# An AirDrop send goes through the running app's `send` command, whose
# answer arrives only once the recipient has accepted and the files are
# across.
AIRDROP_SEND_TIMEOUT = 3600.0
# Marks a Peer built from an AirDrop entry of the `peers` response; such a
# peer is sent to through the app, never by an in-process connection.
AIRDROP = "airdrop"


def _control_request(req: dict[str, Any],
                     timeout: float = SOCKET_TIMEOUT) -> dict[str, Any] | None:
    """One request to the running app over its control channel, or None
    when no app is listening (see core/control.py)."""
    return control.request(req, timeout)


def _peer_from_socket_dict(d: dict[str, Any]) -> Peer:
    """The control socket's `peers` response has everything OutboundConnection
    needs (host/port) to send directly; endpoint_id isn't included (it's
    only used for peer *matching*, which we don't need -- we already
    have the exact peer picked by the user) so it's left blank."""
    return Peer(instance=d.get("instance", ""),
               endpoint_id=AIRDROP if d.get("protocol") == AIRDROP else "",
               device_name=d.get("name", "Unknown device"),
               device_type=d.get("type", 0), host=d["host"], port=d["port"])


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class PickerWindow(Adw.ApplicationWindow):
    """Single-purpose dialog: pick a peer, then watch the send happen."""

    def __init__(self, application: "PickerApplication",
                files: list[Path]) -> None:
        super().__init__(application=application,
                         title="Send with NearShare",
                         default_width=420, default_height=480)
        self.app = application
        self.files = files
        self._peer_rows: dict[str, Adw.ActionRow] = {}
        self._build_ui()

    # -------------------------------------------------------------- build

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)
        toolbar_view.add_top_bar(Adw.HeaderBar())

        self.stack = Gtk.Stack(vexpand=True,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        toolbar_view.set_content(self.stack)

        self.stack.add_named(self._build_peers_page(), "peers")
        self.stack.add_named(self._build_progress_page(), "progress")
        self.stack.set_visible_child_name("peers")

    def _build_peers_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12, margin_start=12,
                      margin_end=12)
        count = len(self.files)
        plural = "file" if count == 1 else "files"
        box.append(Gtk.Label(
            label=f"Send {count} {plural} to:", xalign=0,
            css_classes=["title-4"]))

        self.peers_group = Adw.PreferencesGroup()
        self._empty_row = Adw.ActionRow(
            title="Looking for nearby devices…",
            subtitle="Open Quick Share (or the share sheet) on the "
                    "target device so it becomes discoverable.",
            selectable=False, sensitive=False)
        self.peers_group.add(self._empty_row)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.peers_group)
        box.append(scroller)
        return box

    def _build_progress_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                      margin_top=24, margin_bottom=24, margin_start=24,
                      margin_end=24)
        self.progress_title = Gtk.Label(css_classes=["title-3"], wrap=True,
                                        justify=Gtk.Justification.CENTER)
        self.pin_label = Gtk.Label(css_classes=["title-1"])
        self.progress_bar = Gtk.ProgressBar(width_request=280)
        self.result_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.close_button = Gtk.Button(label="Close", visible=False,
                                       halign=Gtk.Align.CENTER,
                                       css_classes=["suggested-action"])
        self.close_button.connect("clicked", lambda _b: self.close())
        for widget in (self.progress_title, self.pin_label,
                      self.progress_bar, self.result_label,
                      self.close_button):
            box.append(widget)
        return box

    # ------------------------------------------------------------- peers

    def update_peers(self, peers: dict[str, Peer]) -> None:
        if self._sending():
            return  # don't reshuffle the list mid-send
        for key in [k for k in self._peer_rows if k not in peers]:
            self.peers_group.remove(self._peer_rows.pop(key))
        for key, peer in peers.items():
            if key in self._peer_rows:
                self._update_peer_row(self._peer_rows[key], peer)
            else:
                self._peer_rows[key] = self._add_peer_row(peer)
        self._empty_row.set_visible(not peers)

    def _sending(self) -> bool:
        return self.stack.get_visible_child_name() == "progress"

    def _update_peer_row(self, row: Adw.ActionRow, peer: Peer) -> None:
        row.set_title(peer.device_name)
        row.set_subtitle("AirDrop" if peer.endpoint_id == AIRDROP else peer.host)

    def _add_peer_row(self, peer: Peer) -> Adw.ActionRow:
        row = Adw.ActionRow()
        self._update_peer_row(row, peer)
        button = Gtk.Button(label="Send", valign=Gtk.Align.CENTER,
                            css_classes=["suggested-action"])
        button.connect("clicked", lambda _b: self._start_send(peer))
        row.add_suffix(button)
        self.peers_group.add(row)
        return row

    def show_connection_lost(self) -> None:
        self._empty_row.set_title("Lost the NearShare app")
        self._empty_row.set_subtitle(
            "The running NearShare app stopped responding; try again, or "
            "quit and relaunch it.")
        self._empty_row.set_visible(True)

    # ---------------------------------------------------------- sending

    def _start_send(self, peer: Peer) -> None:
        self.progress_title.set_text(f"Sending to {peer.device_name}")
        self.pin_label.set_text("")
        self.result_label.set_text(
            f"Waiting for {peer.device_name} to accept…"
            if peer.endpoint_id == AIRDROP else "Connecting…")
        self.progress_bar.set_fraction(0.0)
        self.close_button.set_visible(False)
        self.stack.set_visible_child_name("progress")
        self.app.start_send(peer, self.files)

    def on_progress(self, done: int, total: int) -> None:
        self.progress_bar.set_fraction(min(1.0, done / total) if total else 0.0)
        self.result_label.set_text(
            f"{_human_size(done)} / {_human_size(total)}")

    def show_pin(self, pin: str) -> None:
        self.pin_label.set_text(f"PIN: {pin}")
        self.result_label.set_text(
            "Confirm this matches the PIN on the other device's screen "
            "to complete the transfer.")

    def show_result(self, success: bool, message: str | None) -> None:
        self.progress_bar.set_fraction(1.0 if success else
                                       self.progress_bar.get_fraction())
        if success:
            self.result_label.set_text("Transfer complete.")
        else:
            self.result_label.set_text(f"Transfer failed: {message}")
        self.close_button.set_visible(True)


class PickerApplication(Adw.Application):
    def __init__(self, files: list[Path]) -> None:
        # NON_UNIQUE, and no application id at all: the picker is a
        # one-shot dialog launched per invocation (from the file
        # manager's Open With, or `nearshare send-picker`), so it has no
        # use for single-instance behaviour. Registering an id also made
        # it die on startup inside the snap -- AppArmor refuses to let a
        # confined process own "dev.dhivalabs.nearshare.picker", so
        # clicking Open With silently did nothing. Owning no name at all
        # cannot be refused.
        super().__init__(flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.files = files
        self.asyncio_thread = AsyncioThread()
        self.window: PickerWindow | None = None
        self.service: NearShareService | None = None  # ephemeral mode only
        self.device_name: str | None = None
        self._closed = False

    # ---------------------------------------------------------- lifecycle

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self.asyncio_thread.start()

    def do_activate(self) -> None:
        if self.window is None:
            self.window = PickerWindow(self, self.files)
            self.window.connect("close-request", self._on_close_request)
        self.window.present()
        self._detect_mode()

    def _on_close_request(self, _window) -> bool:
        return False  # let it close/destroy normally -> app quits

    def do_shutdown(self) -> None:
        self._closed = True
        if self.service is not None:
            fut = self.asyncio_thread.run_coroutine(self.service.browser.stop())
            try:
                fut.result(timeout=5)
            except Exception as exc:  # best-effort on the way out
                log.warning("ephemeral browser teardown: %s", exc)
        self.asyncio_thread.stop()
        Adw.Application.do_shutdown(self)

    def _safe_window_call(self, method_name: str, *args):
        def call() -> None:
            if self.window is not None:
                getattr(self.window, method_name)(*args)
        return call

    # ------------------------------------------------------- peer discovery

    def _detect_mode(self) -> None:
        async def probe():
            return await asyncio.to_thread(
                _control_request, {"cmd": "status"}, PROBE_TIMEOUT)

        def after(result, _exc) -> None:
            if result is not None and "error" not in result:
                self.device_name = result.get("device_name")
                if (result.get("airdrop") or {}).get("visible"):
                    # The app only looks for Apple devices on request.
                    self.asyncio_thread.run_coroutine(asyncio.to_thread(
                        _control_request, {"cmd": "airdrop",
                                           "action": "discover",
                                           "seconds": 120}))
                self._start_socket_polling()
            else:
                self._start_ephemeral_browser()

        self.asyncio_thread.run_coroutine(probe(), after)

    def _start_socket_polling(self) -> None:
        def tick() -> bool:
            if self._closed:
                return False
            self.asyncio_thread.run_coroutine(
                asyncio.to_thread(_control_request, {"cmd": "peers"}),
                self._on_socket_peers)
            return True
        tick()
        GLib.timeout_add_seconds(POLL_INTERVAL_S, tick)

    def _on_socket_peers(self, result, _exc) -> None:
        if self.window is None:
            return
        if result is None or "error" in result:
            self.window.show_connection_lost()
            return
        # An Apple device that hasn't answered with its name yet isn't
        # one a send is likely to reach; list it once it has.
        peers = {d.get("instance", d["host"]): _peer_from_socket_dict(d)
                for d in result.get("peers", [])
                if d.get("named", True)}
        self.window.update_peers(peers)

    def _start_ephemeral_browser(self) -> None:
        self.service = NearShareService()
        self.device_name = self.service.device_name
        self.service.on_peers_changed = lambda peers: idle(
            self._safe_window_call("update_peers", peers))
        self.asyncio_thread.run_coroutine(self.service.browser.start())

    # ------------------------------------------------------------- sending

    def start_send(self, peer: Peer, files: list[Path]) -> None:
        self.asyncio_thread.run_coroutine(self._run_send(peer, files))

    async def _run_send(self, peer: Peer, files: list[Path]) -> None:
        if peer.endpoint_id == AIRDROP:
            await self._run_airdrop_send(peer, files)
            return
        device_name = self.device_name or default_device_name()
        events = Events(on_progress=lambda dev, done, total: idle(
            self._safe_window_call("on_progress", done, total)))
        conn = OutboundConnection(peer.host, peer.port, events, device_name,
                                  files, peer_name=peer.device_name)
        run_task = asyncio.ensure_future(conn.run())
        pin_shown = False
        while not run_task.done():
            if conn.pin and not pin_shown:
                idle(self._safe_window_call("show_pin", conn.pin))
                pin_shown = True
            await asyncio.sleep(0.2)
        try:
            await run_task
            idle(self._safe_window_call("show_result", True, None))
        except Exception as exc:
            idle(self._safe_window_call("show_result", False, str(exc)))

    async def _run_airdrop_send(self, peer: Peer, files: list[Path]) -> None:
        """Through the running app, which owns the AirDrop link: no
        progress comes back, only the outcome."""
        resp = await asyncio.to_thread(
            _control_request,
            {"cmd": "send", "peer": peer.instance, "protocol": AIRDROP,
             "files": [str(f) for f in files]}, AIRDROP_SEND_TIMEOUT)
        if resp is None:
            idle(self._safe_window_call("show_result", False,
                                        "the NearShare app stopped"))
        elif "error" in resp:
            idle(self._safe_window_call("show_result", False, resp["error"]))
        else:
            idle(self._safe_window_call("show_result", True, None))


def main(files: list[str]) -> int:
    paths = [Path(f) for f in files]
    app = PickerApplication(paths)
    return app.run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
