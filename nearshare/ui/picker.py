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
import json
import logging
import socket
import sys
import time
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
from ..core.service import (NearShareService, control_socket_path,  # noqa: E402
                            default_device_name)
from . import prefs  # noqa: E402
from .style import APP_ID, install_css  # noqa: E402
from .asyncio_glue import AsyncioThread, Coalescer, idle  # noqa: E402
from .fmt import (count_files, describe_files, device_icon,  # noqa: E402
                  device_type_name, human_duration, human_size, human_speed,
                  split_sendable, total_size)

log = logging.getLogger("nearshare.ui.picker")

PROBE_TIMEOUT = 1.5   # quick "is the app running" check
SOCKET_TIMEOUT = 5.0  # normal peers-poll request/response
POLL_INTERVAL_S = 2


def _control_request(req: dict[str, Any],
                     timeout: float = SOCKET_TIMEOUT) -> dict[str, Any] | None:
    """Same shape as cli.py's helper of the same name, duplicated rather
    than imported so this module has no dependency on cli.py's internals
    (it's launched standalone, not through cli.main())."""
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


def _peer_from_socket_dict(d: dict[str, Any]) -> Peer:
    """The control socket's `peers` response has everything OutboundConnection
    needs (host/port) to send directly; endpoint_id isn't included (it's
    only used for peer *matching*, which we don't need -- we already
    have the exact peer picked by the user) so it's left blank."""
    return Peer(instance=d.get("instance", ""), endpoint_id="",
               device_name=d.get("name", "Unknown device"),
               device_type=d.get("type", 0), host=d["host"], port=d["port"])


class PickerWindow(Adw.ApplicationWindow):
    """Single-purpose dialog: pick a peer, then watch the send happen."""

    def __init__(self, application: "PickerApplication",
                files: list[Path]) -> None:
        super().__init__(application=application,
                         title="Send with NearShare",
                         default_width=420, default_height=520,
                         width_request=340, height_request=380)
        self.app = application
        self.files, self.rejected = split_sendable(files)
        self.total = total_size(self.files)
        self._peer_rows: dict[str, Adw.ActionRow] = {}
        self._started_at = 0.0
        self._build_ui()

    # -------------------------------------------------------------- build

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)
        count = len(self.files)
        self.window_title = Adw.WindowTitle(
            title="Send with NearShare",
            subtitle=(f"{describe_files(self.files)} · "
                      f"{human_size(self.total)}" if count else ""))
        toolbar_view.add_top_bar(Adw.HeaderBar(title_widget=self.window_title))

        self.stack = Gtk.Stack(vexpand=True,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        toolbar_view.set_content(self.stack)

        self.stack.add_named(self._build_peers_page(), "peers")
        self.stack.add_named(self._build_progress_page(), "progress")
        self.stack.add_named(self._build_nothing_page(), "nothing")
        self.stack.set_visible_child_name("peers" if self.files else "nothing")

        # Escape closes, like any other dialog.
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("Escape"),
            Gtk.CallbackAction.new(lambda *_: (self.close(), True)[1])))
        self.add_controller(keys)

    def _build_nothing_page(self) -> Gtk.Widget:
        """Shown when nothing that was passed in can be sent."""
        if any(p.is_dir() for p in self.rejected):
            description = ("Folders can't be sent. Compress the folder "
                           "into a .zip and send that instead.")
        else:
            description = "The selected files could not be read."
        page = Adw.StatusPage(icon_name="dialog-warning-symbolic",
                              title="Nothing to Send",
                              description=description)
        button = Gtk.Button(label="Close", halign=Gtk.Align.CENTER,
                            css_classes=["pill"])
        button.connect("clicked", lambda _b: self.close())
        page.set_child(button)
        return page

    def _build_peers_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self.peers_group = Adw.PreferencesGroup(title="Nearby devices")
        if self.rejected:
            self.peers_group.set_description(
                f"{count_files(len(self.rejected))} skipped — folders "
                "can't be sent.")
        self._searching = Adw.StatusPage(
            title="Looking for Nearby Devices…",
            description="On the other device, open Quick Share and keep "
                        "it on screen.",
            css_classes=["compact"])
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
        spinner.set_size_request(32, 32)
        self._searching.set_child(spinner)
        self.peers_group.add(self._searching)
        page.add(self.peers_group)
        return page

    def _build_progress_page(self) -> Gtk.Widget:
        clamp = Adw.Clamp(maximum_size=340, valign=Gtk.Align.CENTER,
                          margin_top=24, margin_bottom=24, margin_start=24,
                          margin_end=24)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(box)
        self.result_icon = Gtk.Image(pixel_size=64, visible=False,
                                     margin_bottom=6)
        self.progress_title = Gtk.Label(css_classes=["title-3"], wrap=True,
                                        justify=Gtk.Justification.CENTER)
        self.pin_label = Gtk.Label(css_classes=["pin-display"],
                                   visible=False)
        self.progress_bar = Gtk.ProgressBar()
        self.result_label = Gtk.Label(wrap=True,
                                      justify=Gtk.Justification.CENTER,
                                      css_classes=["dim-label"])
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=6)
        self.cancel_button = Gtk.Button(label="Cancel",
                                        css_classes=["pill"])
        self.cancel_button.connect("clicked", lambda _b: self.app.cancel_send())
        self.back_button = Gtk.Button(label="Choose Another Device",
                                      visible=False, css_classes=["pill"])
        self.back_button.connect("clicked", lambda _b: self._back_to_peers())
        self.close_button = Gtk.Button(
            label="Done", visible=False,
            css_classes=["pill", "suggested-action"])
        self.close_button.connect("clicked", lambda _b: self.close())
        for button in (self.cancel_button, self.back_button,
                       self.close_button):
            buttons.append(button)
        for widget in (self.result_icon, self.progress_title, self.pin_label,
                       self.progress_bar, self.result_label, buttons):
            box.append(widget)
        return clamp

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
        self._searching.set_visible(not peers)

    def _sending(self) -> bool:
        return self.stack.get_visible_child_name() == "progress"

    def _update_peer_row(self, row: Adw.ActionRow, peer: Peer) -> None:
        row.set_title(GLib.markup_escape_text(peer.device_name))
        row.set_subtitle(GLib.markup_escape_text(
            f"{device_type_name(peer.device_type)} · {peer.host}"))

    def _add_peer_row(self, peer: Peer) -> Adw.ActionRow:
        # The whole row is the button: this dialog exists to pick one
        # device, so a separate Send button per row was a smaller target
        # for the same action.
        row = Adw.ActionRow(activatable=True)
        row.set_title_lines(1)
        row.set_subtitle_lines(1)
        self._update_peer_row(row, peer)
        row.add_prefix(
            Gtk.Image.new_from_icon_name(device_icon(peer.device_type)))
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect("activated", lambda _r: self._start_send(peer))
        self.peers_group.add(row)
        return row

    def show_connection_lost(self) -> None:
        self._searching.set_title("Lost the NearShare App")
        self._searching.set_description(
            "The running NearShare app stopped responding. Close this "
            "window and try again.")
        self._searching.set_visible(True)

    # ---------------------------------------------------------- sending

    def _start_send(self, peer: Peer) -> None:
        self._peer = peer
        self._started_at = 0.0
        self.progress_title.set_text(f"Sending to {peer.device_name}")
        self.pin_label.set_visible(False)
        self.result_icon.set_visible(False)
        self.result_label.set_text("Connecting…")
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_visible(True)
        self.cancel_button.set_visible(True)
        self.back_button.set_visible(False)
        self.close_button.set_visible(False)
        self.stack.set_visible_child_name("progress")
        self.app.start_send(peer, self.files)

    def _back_to_peers(self) -> None:
        self.stack.set_visible_child_name("peers")

    def on_progress(self, done: int, total: int) -> None:
        if not self._sending() or self.close_button.get_visible():
            return  # a late update after the result is already shown
        if not self._started_at:
            self._started_at = time.monotonic()
            self.pin_label.set_visible(False)
        self.progress_bar.set_fraction(min(1.0, done / total) if total else 0.0)
        parts = [f"{human_size(done)} of {human_size(total)}"]
        elapsed = time.monotonic() - self._started_at
        if elapsed > 0.5 and done > 0:
            speed = done / elapsed
            parts.append(human_speed(speed))
            if total > done and speed > 0:
                parts.append(f"{human_duration((total - done) / speed)} left")
        self.result_label.set_text(" · ".join(parts))

    def show_pin(self, pin: str) -> None:
        self.pin_label.set_text(pin)
        self.pin_label.set_visible(True)
        self.result_label.set_text(
            f"Waiting for {self._peer.device_name} to accept. "
            "Check that it shows the same PIN.")

    def show_result(self, success: bool, message: str | None) -> None:
        self.pin_label.set_visible(False)
        self.progress_bar.set_visible(False)
        self.cancel_button.set_visible(False)
        self.result_icon.set_visible(True)
        self.close_button.set_visible(True)
        for css in ("success", "error", "dim-label"):
            self.result_icon.remove_css_class(css)
        if success:
            self.result_icon.set_from_icon_name("emblem-ok-symbolic")
            self.result_icon.add_css_class("success")
            self.progress_title.set_text(
                f"Sent to {self._peer.device_name}")
            self.result_label.set_text(
                f"{count_files(len(self.files))} · {human_size(self.total)}")
            self.close_button.grab_focus()
            return
        # Failure or cancel: the device list is one click away, instead
        # of making the user close the window and right-click again.
        self.back_button.set_visible(True)
        if message is None:
            self.result_icon.set_from_icon_name("process-stop-symbolic")
            self.result_icon.add_css_class("dim-label")
            self.progress_title.set_text("Cancelled")
            self.result_label.set_text("Nothing more was sent.")
        else:
            self.result_icon.set_from_icon_name("dialog-error-symbolic")
            self.result_icon.add_css_class("error")
            self.progress_title.set_text("Couldn't Send")
            self.result_label.set_text(message)


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
        self._send_future = None
        # Only the newest progress figure matters, a few times a second.
        self._progress = Coalescer(
            lambda _key, value: self._safe_window_call(
                "on_progress", *value)())

    # ---------------------------------------------------------- lifecycle

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self.asyncio_thread.start()

    def do_activate(self) -> None:
        if self.window is None:
            Gtk.Window.set_default_icon_name(APP_ID)
            install_css()
            self.window = PickerWindow(self, self.files)
            self.window.connect("close-request", self._on_close_request)
        self.window.present()
        self._detect_mode()

    def _on_close_request(self, _window) -> bool:
        return False  # let it close/destroy normally -> app quits

    def do_shutdown(self) -> None:
        self._closed = True
        self.cancel_send()
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
        peers = {d.get("instance", d["host"]): _peer_from_socket_dict(d)
                for d in result.get("peers", [])}
        self.window.update_peers(peers)

    def _start_ephemeral_browser(self) -> None:
        self.service = NearShareService(device_name=prefs.device_name())
        self.device_name = self.service.device_name
        self.service.on_peers_changed = lambda peers: idle(
            self._safe_window_call("update_peers", peers))
        self.asyncio_thread.run_coroutine(self.service.browser.start())

    # ------------------------------------------------------------- sending

    def start_send(self, peer: Peer, files: list[Path]) -> None:
        self._send_future = self.asyncio_thread.run_coroutine(
            self._run_send(peer, files))

    def cancel_send(self) -> None:
        """Stop the send in progress. The connection says goodbye to the
        receiver as the cancelled task unwinds."""
        if self._send_future is not None and not self._send_future.done():
            self._send_future.cancel()

    async def _run_send(self, peer: Peer, files: list[Path]) -> None:
        device_name = (self.device_name or prefs.device_name()
                       or default_device_name())
        events = Events(
            on_progress=lambda dev, done, total: self._progress.push(
                "send", (done, total)),
            on_pin=lambda dev, pin: idle(
                self._safe_window_call("show_pin", pin)))
        conn = OutboundConnection(peer.host, peer.port, events, device_name,
                                  files, peer_name=peer.device_name)
        try:
            await conn.run()
        except asyncio.CancelledError:
            self._progress.drop("send")
            idle(self._safe_window_call("show_result", False, None))
            raise
        except Exception as exc:
            self._progress.drop("send")
            idle(self._safe_window_call(
                "show_result", False, str(exc) or type(exc).__name__))
        else:
            self._progress.drop("send")
            idle(self._safe_window_call("show_result", True, None))


def main(files: list[str]) -> int:
    paths = [Path(f) for f in files]
    app = PickerApplication(paths)
    return app.run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
