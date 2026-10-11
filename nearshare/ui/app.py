"""GTK4 + libadwaita front-end for NearShare, built on the headless
`NearShareService` core (see core/service.py, core/connection.py,
core/mdns.py).

Implements PLAN.md's M2 UI flows: the visibility toggle, the nearby-
devices/send-files list, the transfers/progress section, the incoming-
transfer PIN confirmation dialog (plus a desktop notification so it's
visible when the window is hidden), received-text dialogs, and the
experimental Direct-mode hotspot panel (M3).

Threading model: see asyncio_glue.py's module docstring. In short, the
asyncio loop the service depends on runs on its own background thread;
every `Events` callback below re-enters the GTK thread via `idle(...)`
before touching a widget, and every button handler that needs to call
into the service goes back out via `self.app.asyncio_thread.run_coroutine`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import cli  # noqa: E402
from ..core.paths import IS_WINDOWS  # noqa: E402
from ..airdrop.radio import RadioError  # noqa: E402
from ..airdrop.sender import AirDropPeer  # noqa: E402
from ..core.connection import Events, TransferRequest  # noqa: E402
from ..core.hotspot import Hotspot, HotspotError  # noqa: E402
from ..core.mdns import Peer  # noqa: E402
from ..core.service import NearShareService  # noqa: E402
from . import prefs  # noqa: E402
from .asyncio_glue import AsyncioThread, bridge_future_to_asyncio, idle  # noqa: E402
from .qrcode import QrCodeArea, wifi_qr_payload  # noqa: E402

_APT_INSTALL_COMMAND = ("sudo add-apt-repository ppa:dhiva-labs/apps && "
                       "sudo apt install nearshare")

log = logging.getLogger("nearshare.ui.app")

APP_ID = "dev.dhivalabs.nearshare"

# Inside a strictly-confined snap, AppArmor only permits owning a
# session-bus name matching the snap's own name: registering as APP_ID
# fails with "not allowed to own the service" and the GUI never starts.
# A dbus slot would grant it, but that forces the snap into the Snap
# Store's manual review queue, so use the already-permitted name. The
# desktop file and icon keep their reverse-DNS identity either way --
# only the bus name differs, and only under snap confinement.
BUS_ID = (os.environ.get("SNAP_INSTANCE_NAME", APP_ID)
          if os.environ.get("SNAP") else APP_ID)

_DEVICE_TYPE_NAMES = {0: "Unknown device", 1: "Phone", 2: "Tablet", 3: "Laptop"}


def _peer_name(peer: "Peer | AirDropPeer") -> str:
    return peer.display_name if isinstance(peer, AirDropPeer) else peer.device_name


def _apple_device_kind(model: str | None) -> str:
    """"iPhone17,1" -> "iPhone"; Apple model identifiers lead with it."""
    for kind in ("iPhone", "iPad", "MacBook", "iMac", "Mac", "Watch", "Vision"):
        if model and model.startswith(kind):
            return "Mac" if kind in ("MacBook", "iMac") else kind
    return "Apple device"


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _human_speed(bytes_per_second: float) -> str:
    return f"{_human_size(int(bytes_per_second))}/s"


def _human_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class _TransferRow:
    """One row in the Transfers section, tracking a single (device,
    direction) session -- see NearShareWindow._active / _completed for
    how rows are keyed and retired."""

    def __init__(self, group: Adw.PreferencesGroup, title: str,
                total: int, on_remove: Callable[["_TransferRow"], None]) -> None:
        self.total = total
        self.done = 0
        self.finished = False
        self.started_at = time.monotonic()

        self.row = Adw.ActionRow(title=title)
        # Long error messages must stay fully readable rather than being
        # ellipsized to one line (see mark_error).
        self.row.set_subtitle_lines(0)
        # A long device name must ellipsize rather than push the suffix
        # widgets past the right edge -- that overlap is what made the
        # window unusable at its default width.
        self.row.set_title_lines(1)
        self.status_icon = Gtk.Image.new_from_icon_name(
            "emblem-synchronizing-symbolic")
        self.row.add_prefix(self.status_icon)
        # Narrow, and allowed to shrink: at 360px there is no room for a
        # 120px bar beside text and two buttons. The subtitle always
        # carries the percentage, so hiding the bar loses nothing.
        # halign=CENTER as well as hexpand=False: inside an ActionRow's
        # suffix box a FILL-aligned bar stretches to whatever space is
        # left (measured at 150px in a 360px window), squeezing the title
        # and buttons into each other.
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER,
                                        halign=Gtk.Align.CENTER,
                                        hexpand=False,
                                        css_classes=["transfer-progress"])
        self.row.add_suffix(self.progress)
        self.open_button = Gtk.Button(
            icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Open containing folder", visible=False)
        self.row.add_suffix(self.open_button)
        self.remove_button = Gtk.Button(
            icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Remove from this list", css_classes=["flat"])
        self.remove_button.connect("clicked", lambda _b: on_remove(self))
        self.row.add_suffix(self.remove_button)
        group.add(self.row)
        self._group = group
        self._update_subtitle()

    def _update_subtitle(self) -> None:
        if self.finished:
            return
        pct = int(self.done * 100 / self.total) if self.total else 0
        parts = [f"{_human_size(self.done)} / {_human_size(self.total)} ({pct}%)"]
        # Speed/ETA are cheap derivatives of the progress callbacks
        # (done/total over wall-clock elapsed since this row started) --
        # no core changes needed. Held back for the first half second so
        # a burst of early callbacks doesn't show a wildly wrong rate.
        elapsed = time.monotonic() - self.started_at
        if elapsed > 0.5 and self.done > 0:
            speed = self.done / elapsed
            parts.append(_human_speed(speed))
            if self.total > self.done and speed > 0:
                eta = (self.total - self.done) / speed
                parts.append(f"{_human_duration(eta)} left")
        self.row.set_subtitle(" • ".join(parts))

    def update_progress(self, done: int, total: int) -> None:
        if self.finished:
            return
        self.done, self.total = done, total or self.total
        self.progress.set_fraction(
            min(1.0, self.done / self.total) if self.total else 0.0)
        self._update_subtitle()

    def mark_complete(self, folder: Path | None, count: int) -> None:
        self.finished = True
        self.progress.set_fraction(1.0)
        self.progress.set_visible(False)
        self.status_icon.set_from_icon_name("emblem-ok-symbolic")
        self.status_icon.add_css_class("success")
        # Dim the whole row so a glance at the list separates "still
        # going" from "done" without reading the text.
        self.row.add_css_class("dim-label")
        plural = "file" if count == 1 else "files"
        elapsed = _human_duration(time.monotonic() - self.started_at)
        self.row.set_subtitle(f"Completed • {count} {plural} • {elapsed}")
        if folder is not None:
            self.open_button.set_visible(True)
            self.open_button.set_tooltip_text(f"Open {folder}")
            self.open_button.connect(
                "clicked", lambda _b: _open_folder(folder))

    def mark_error(self, message: str) -> None:
        self.finished = True
        self.progress.set_visible(False)
        self.status_icon.set_from_icon_name("dialog-error-symbolic")
        self.row.add_css_class("error")
        self.row.set_subtitle(f"Failed: {message}")
        self.row.set_tooltip_text(message)

    def remove(self) -> None:
        self._group.remove(self.row)


# Gtk.ProgressBar's default trough carries a ~150px CSS min-width, and
# set_size_request only raises a floor -- it cannot bring that down. In a
# 360px window a 150px bar beside a device name and two buttons is what
# made rows overlap, so cap it in CSS instead.
_CSS = b"""
progressbar.transfer-progress > trough { min-width: 64px; }
"""


def _install_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _open_folder(folder: Path) -> None:
    uri = GLib.filename_to_uri(str(folder), None)
    Gio.AppInfo.launch_default_for_uri(uri, None)


class NearShareWindow(Adw.ApplicationWindow):
    """The single main window; closing it hides rather than destroys it
    (see NearShareApplication's hidden-window pattern) so the service
    keeps serving the control socket/mDNS/TCP in the background."""

    def __init__(self, application: "NearShareApplication") -> None:
        super().__init__(application=application, title="NearShare",
                         default_width=480, default_height=640,
                         width_request=360, height_request=440)
        self.app = application
        self._peer_rows: dict[str, Adw.ActionRow] = {}
        self._peers: dict[str, Peer] = {}
        # Apple devices found over AirDrop, keyed "airdrop:<mac>" so they
        # never collide with a Quick Share instance name.
        self._airdrop_rows: dict[str, Adw.ActionRow] = {}
        self._airdrop_peers: dict[str, AirDropPeer] = {}
        self._updating_airdrop_switch = False
        # One row per (device, direction) *active* transfer, updated in
        # place as progress events arrive (done/total are cumulative
        # across the whole batch, not per-file -- see connection.py's
        # _send_file/_receive_payloads). On completion/error the row
        # moves into _completed (capped at the 5 most recent) instead of
        # being torn down immediately, so the user has a moment to see
        # the outcome; any row (active or completed) can be dismissed
        # early with its X button. See _find_active_row for how
        # progress/complete/error callbacks (which only carry a device
        # name, not a direction) are matched back to a key.
        self._active: dict[tuple[str, str], _TransferRow] = {}
        self._completed: list[_TransferRow] = []
        # Set by the responsive breakpoint; new rows inherit it.
        self._compact = False
        self._updating_visibility_switch = False
        self._updating_hotspot_switch = False
        # Only nag once per process run that closing the window doesn't
        # quit the app -- the first-ever close already explains this at
        # length (see _show_background_explainer); repeating a dialog
        # every time would be the annoying kind of "helpful".
        self._background_notice_sent = False

        self.connect("close-request", self._on_close_request)
        self._build_ui()

    # -------------------------------------------------------------- build

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        # Below ~420px there is no room for a progress bar beside a
        # device name and two buttons, so drop the bars at that width;
        # every row's subtitle still carries the percentage. Wrapped in
        # a try/except because add_breakpoint needs libadwaita >= 1.4 and
        # a missing breakpoint must not stop the window from building.
        try:
            breakpoint_ = Adw.Breakpoint.new(
                Adw.BreakpointCondition.parse("max-width: 420sp"))
            breakpoint_.connect(
                "apply", lambda _b: self._set_compact(True))
            breakpoint_.connect(
                "unapply", lambda _b: self._set_compact(False))
            self.add_breakpoint(breakpoint_)
        except (AttributeError, TypeError) as exc:  # older libadwaita
            log.debug("responsive breakpoint unavailable: %s", exc)
        self.set_content(toolbar_view)

        self.window_title = Adw.WindowTitle(
            title="NearShare", subtitle=self.app.service.device_name)
        header = Adw.HeaderBar(title_widget=self.window_title)

        menu = Gio.Menu()
        menu.append("Quit NearShare", "app.quit")
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic", menu_model=menu,
            tooltip_text="Main Menu")
        header.pack_end(menu_button)
        toolbar_view.add_top_bar(header)

        self.toast_overlay = Adw.ToastOverlay()
        toolbar_view.set_content(self.toast_overlay)

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.toast_overlay.set_child(scroller)
        page = Adw.PreferencesPage()
        scroller.set_child(page)

        page.add(self._build_setup_group())
        page.add(self._build_visibility_group())
        page.add(self._build_devices_group())
        page.add(self._build_transfers_group())
        # Direct mode drives NetworkManager; there is none on Windows.
        if not IS_WINDOWS:
            page.add(self._build_direct_mode_group())
        self.refresh_setup_status()
        self.update_airdrop()
        # The AirDrop window counts down, and the radio backend's probe
        # finishes after startup; a cheap periodic refresh covers both.
        GLib.timeout_add_seconds(5, self._airdrop_tick)

    def _build_setup_group(self) -> Adw.PreferencesGroup:
        """The "Finish setup" group: lists whatever piece of `nearshare
        install`'s desktop integration isn't done yet (launcher on PATH,
        desktop entries, Nautilus integration, keyboard shortcut), with
        one button that runs the same install logic to fix everything
        fixable in this environment. Hidden entirely once
        cli.integration_status() reports everything fixable here is
        done -- see refresh_setup_status. Under snap, Nautilus
        integration can never be fixed from inside the app (strict
        confinement), so that row and the apt-get hint stay visible
        until the user installs the .deb separately; they don't block
        the group from being considered "complete" for what IS fixable
        here (see cli.integration_status)."""
        self.setup_group = Adw.PreferencesGroup(
            title="Finish setup",
            description="A few desktop-integration steps from `nearshare "
                       "install` aren't done yet.")

        self._setup_rows: dict[str, Adw.ActionRow] = {}
        self._setup_row_icons: dict[str, Gtk.Image] = {}
        if IS_WINDOWS:
            items = (("launcher_on_path", "Command-line launcher on PATH"),
                     ("desktop_entries", "Start menu entry"),
                     ("nautilus", "Explorer “Send to ▸ NearShare”"))
        else:
            items = (("launcher_on_path", "Command-line launcher on PATH"),
                     ("desktop_entries", "Desktop app entries (app grid, search)"),
                     ("nautilus", "Nautilus right-click “Send with NearShare”"))
        for key, title in items:
            row = Adw.ActionRow(title=title, selectable=False)
            icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            row.add_prefix(icon)
            self.setup_group.add(row)
            self._setup_rows[key] = row
            self._setup_row_icons[key] = icon

        # The shortcut row stays visible even once bound (unlike the
        # three above, which just disappear when done) so it always
        # shows the actual accelerator in human-readable form (e.g.
        # "Ctrl+Alt+N") rather than gsettings' raw "<Control><Alt>n" --
        # see refresh_setup_status.
        self._shortcut_row = Adw.ActionRow(title="Keyboard shortcut",
                                           selectable=False)
        self._shortcut_icon = Gtk.Image.new_from_icon_name(
            "dialog-warning-symbolic")
        self._shortcut_row.add_prefix(self._shortcut_icon)
        self.setup_group.add(self._shortcut_row)

        self._setup_deb_row = Adw.ActionRow(
            title="Right-click integration needs the .deb build",
            subtitle="Strict snap confinement can't write the Files "
                    "(Nautilus) extension. Run in a terminal:",
            selectable=False)
        # A Gtk.Entry clips the command to one un-scrollable line (the
        # command is longer than the window is wide). A wrapping,
        # selectable label shows all of it.
        self._deb_command_entry = Gtk.Label(
            label=_APT_INSTALL_COMMAND, selectable=True, wrap=True,
            xalign=0.0, hexpand=True, valign=Gtk.Align.CENTER,
            css_classes=["monospace"])
        copy_button = Gtk.Button(
            icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Copy")
        copy_button.connect(
            "clicked", lambda _b: self._copy_to_clipboard(_APT_INSTALL_COMMAND))
        self._setup_deb_box = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6,
                                      margin_start=12, margin_end=12)
        self._setup_deb_box.append(self._deb_command_entry)
        self._setup_deb_box.append(copy_button)
        self.setup_group.add(self._setup_deb_row)
        self.setup_group.add(self._setup_deb_box)

        self._setup_button = Gtk.Button(
            label="Finish setup", css_classes=["suggested-action"],
            halign=Gtk.Align.END, margin_top=6, margin_bottom=6)
        self._setup_button.connect("clicked", self._on_finish_setup_clicked)
        button_box = Gtk.Box(halign=Gtk.Align.END)
        button_box.append(self._setup_button)
        self.setup_group.add(button_box)

        return self.setup_group

    def refresh_setup_status(self) -> None:
        """Re-check cli.integration_status() and show/hide the Finish-
        setup group and its per-item rows accordingly. Called on
        startup and again after the Finish-setup button's install run
        completes."""
        status = cli.integration_status()
        # A row is hidden when done. When it is not done but also cannot
        # be fixed from here (snap confinement), it stays visible as
        # information -- an "unavailable" icon and a reason -- rather
        # than a warning the button silently fails to clear.
        fixable_for = {"launcher_on_path": status["home_fixable"],
                       "desktop_entries": status["home_fixable"],
                       "nautilus": status["nautilus_fixable"]}
        reason_for = {
            "launcher_on_path": "Provided by the snap as `nearshare`",
            "desktop_entries": "Installed by the snap itself",
            "nautilus": "Needs the .deb build — see below",
        }
        for key, row in self._setup_rows.items():
            done = status[key]
            row.set_visible(not done)
            if done:
                continue
            if fixable_for[key]:
                row.set_subtitle("")
                self._setup_row_icons[key].set_from_icon_name(
                    "dialog-warning-symbolic")
            else:
                row.set_subtitle(reason_for[key])
                self._setup_row_icons[key].set_from_icon_name(
                    "dialog-information-symbolic")

        # Windows has no GNOME keybinding to manage.
        self._shortcut_row.set_visible(not status.get("windows"))
        if status["shortcut"] and status["shortcut_accel"]:
            label = cli.accelerator_label(status["shortcut_accel"])
            self._shortcut_row.set_subtitle(f"Bound: {label}")
            self._shortcut_icon.set_from_icon_name("emblem-ok-symbolic")
        elif status["shortcut_fixable"]:
            default_label = cli.accelerator_label(cli.DEFAULT_SHORTCUT_BINDING)
            self._shortcut_row.set_subtitle(
                f"Not bound yet — Finish setup will bind {default_label}")
            self._shortcut_icon.set_from_icon_name("dialog-warning-symbolic")
        else:
            self._shortcut_row.set_subtitle(
                "Unavailable in the snap — GNOME's keyboard settings "
                "aren't reachable from inside confinement. Bind it in "
                "Settings ▸ Keyboard, or use the .deb.")
            self._shortcut_icon.set_from_icon_name(
                "dialog-information-symbolic")

        show_deb_hint = status["snap"] and not status["nautilus"]
        self._setup_deb_row.set_visible(show_deb_hint)
        self._setup_deb_box.set_visible(show_deb_hint)
        # Nothing to press when nothing here is actually fixable.
        self._setup_button.set_visible(status["anything_fixable"])
        self.setup_group.set_visible(not status["complete"])

    def _on_finish_setup_clicked(self, _button: Gtk.Button) -> None:
        self._setup_button.set_sensitive(False)
        self._setup_button.set_label("Working…")

        async def _do() -> str:
            return await asyncio.to_thread(cli.run_install)

        def after(_result: str | None, exc: BaseException | None) -> None:
            self._setup_button.set_sensitive(True)
            self._setup_button.set_label("Finish setup")
            if exc is not None:
                self.toast(f"Setup couldn't finish: {exc}")
            else:
                # Report what actually changed. Claiming success while
                # rows still show warnings is how this looked broken.
                after_status = cli.integration_status()
                if after_status["complete"]:
                    self.toast("Setup finished")
                elif after_status["anything_fixable"]:
                    self.toast("Setup partly finished — see remaining items")
                else:
                    self.toast("Done — the rest needs the .deb build")
            self.refresh_setup_status()

        self.app.asyncio_thread.run_coroutine(_do(), after)

    def _build_visibility_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        self.visibility_row = Adw.SwitchRow(
            title="Visible to nearby devices",
            subtitle=self.app.service.device_name)
        self.visibility_row.set_tooltip_text(
            "When on, nearby phones and laptops with Quick Share open "
            "can see this device and send you files. You can always "
            "send to others, visible or not.")
        self.visibility_row.connect(
            "notify::active", self._on_visibility_switch_toggled)
        group.add(self.visibility_row)

        # Health of our own BLE trigger-advertising, surfaced right next
        # to the switch it affects instead of as a separate banner
        # elsewhere in the window -- see update_ble_status. Hidden
        # unless service.ble_status actually degrades.
        self._ble_health_row = Adw.ActionRow(
            title="Bluetooth discovery unavailable",
            selectable=False, visible=False)
        self._ble_health_row.add_prefix(
            Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        group.add(self._ble_health_row)

        self.airdrop_row = Adw.SwitchRow(
            title="Visible to Apple devices",
            subtitle="AirDrop")
        self.airdrop_row.set_tooltip_text(
            "When on, nearby iPhones, iPads and Macs with AirDrop set to "
            "Everyone can see this device for a few minutes, and you can "
            "send to them. Needs an AWDL radio backend (omdrop-owl).")
        self.airdrop_row.connect("notify::active",
                                 self._on_airdrop_switch_toggled)
        group.add(self.airdrop_row)
        return group

    def _build_devices_group(self) -> Adw.PreferencesGroup:
        self.devices_group = Adw.PreferencesGroup(
            title="Nearby devices",
            description="Devices with Quick Share receiving open on the "
                        "same network, and Apple devices while AirDrop "
                        "is on.")

        # Discovery has no "I've started looking and found nothing yet"
        # event (mdns.Browser only calls back on an actual change), so
        # the searching/empty distinction is time-based: assume we're
        # still warming up until either a peer shows up or a few seconds
        # pass with none -- see _on_discovery_settle_timeout.
        self._devices_settled = False
        self._searching_status = Adw.StatusPage(
            title="Searching for Nearby Devices…",
            description="This can take a few seconds.",
            css_classes=["compact"])
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
        spinner.set_size_request(32, 32)
        self._searching_status.set_child(spinner)
        self.devices_group.add(self._searching_status)

        self._empty_devices_status = Adw.StatusPage(
            icon_name="phone-symbolic",
            title="No Nearby Devices Yet",
            description="Open Quick Share (or the share sheet) on your "
                        "phone so it becomes discoverable.",
            css_classes=["compact"], visible=False)
        self.devices_group.add(self._empty_devices_status)

        GLib.timeout_add_seconds(5, self._on_discovery_settle_timeout)
        return self.devices_group

    def _on_discovery_settle_timeout(self) -> bool:
        if not self._devices_settled:
            self._devices_settled = True
            self._update_devices_empty_state()
        return GLib.SOURCE_REMOVE  # one-shot

    def _update_devices_empty_state(self) -> None:
        has_peers = bool(self._peers) or bool(self._airdrop_rows)
        self._searching_status.set_visible(
            not has_peers and not self._devices_settled)
        self._empty_devices_status.set_visible(
            not has_peers and self._devices_settled)

    def _build_transfers_group(self) -> Adw.PreferencesGroup:
        self.transfers_group = Adw.PreferencesGroup(title="Transfers")
        self._empty_transfers_status = Adw.StatusPage(
            icon_name="send-to-symbolic",
            title="No Transfers Yet",
            description="Files you send or receive will show up here.",
            css_classes=["compact"])
        self.transfers_group.add(self._empty_transfers_status)
        return self.transfers_group

    def update_ble_status(self, ble_status: str) -> None:
        degraded = ble_status.startswith("unavailable")
        self._ble_health_row.set_visible(degraded)
        if degraded:
            reason = (ble_status.split(":", 1)[1].strip()
                      if ":" in ble_status else ble_status)
            self._ble_health_row.set_subtitle(
                f"{reason} — phones may need Quick Share open on their "
                "screen to see this device.")

    def _build_direct_mode_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Experimental")
        self.direct_expander = Adw.ExpanderRow(
            title="Direct mode (no shared Wi-Fi)",
            subtitle="Experimental. Creates a temporary Wi-Fi hotspot; "
                    "your laptop leaves its current network while it's on.",
            show_enable_switch=True, enable_expansion=False)
        self.direct_expander.connect(
            "notify::enable-expansion", self._on_direct_mode_toggled)
        group.add(self.direct_expander)

        self.hotspot_ssid_row = Adw.ActionRow(title="Network name (SSID)")
        self.hotspot_password_row = Adw.ActionRow(title="Password")
        self.hotspot_status_row = Adw.ActionRow(
            title="Status", subtitle="Off", selectable=False)
        for row, getter in (
            (self.hotspot_ssid_row, lambda: self.app.hotspot.ssid),
            (self.hotspot_password_row, lambda: self.app.hotspot.password),
        ):
            button = Gtk.Button(
                icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                tooltip_text="Copy")
            button.connect("clicked", self._make_copy_handler(getter))
            row.add_suffix(button)

        self.qr_area = QrCodeArea()
        qr_box = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=12,
                         margin_bottom=12)
        qr_box.append(self.qr_area)

        self.direct_expander.add_row(self.hotspot_status_row)
        self.direct_expander.add_row(self.hotspot_ssid_row)
        self.direct_expander.add_row(self.hotspot_password_row)
        self.direct_expander.add_row(qr_box)
        return group

    def _make_copy_handler(self, getter):
        def handler(_button: Gtk.Button) -> None:
            value = getter()
            if value:
                self._copy_to_clipboard(value)
        return handler

    # ---------------------------------------------------------- lifecycle

    def _on_close_request(self, _window: Adw.ApplicationWindow) -> bool:
        if not prefs.has_seen_background_explainer():
            self._show_background_explainer()
        else:
            self._hide_to_background()
        return True  # keep the window (and app.hold()) alive, don't destroy

    def _show_background_explainer(self) -> None:
        """First-ever close: explain that NearShare keeps running so it
        can keep receiving, and give an explicit way out, before ever
        hiding the window. Remembered via prefs so this shows exactly
        once regardless of which button the user picks."""
        dialog = Adw.AlertDialog.new(
            "NearShare Keeps Running in the Background",
            "Closing this window doesn't quit NearShare — it keeps "
            "running so nearby devices can still send you files. "
            "You can quit any time from the menu (≡) in the top "
            "corner.")
        dialog.add_response("keep-running", "Keep Running")
        dialog.add_response("quit", "Quit Now")
        dialog.set_response_appearance(
            "quit", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("keep-running")
        dialog.set_close_response("keep-running")

        def on_response(source: Adw.AlertDialog, result: Gio.AsyncResult) -> None:
            response = source.choose_finish(result)
            prefs.mark_background_explainer_seen()
            if response == "quit":
                self.app.quit()
            else:
                self._hide_to_background()

        dialog.choose(self, None, on_response)

    def _hide_to_background(self) -> None:
        self.hide()
        if self._background_notice_sent:
            return
        self._background_notice_sent = True
        # No system tray on GNOME, so a low-priority desktop notification
        # is the only affordance that survives the window disappearing --
        # shown once per run, not on every close.
        notification = Gio.Notification.new("NearShare is still running")
        notification.set_body(
            "It's in the background so nearby devices can send you "
            "files. Reopen NearShare and use the menu to quit.")
        notification.set_priority(Gio.NotificationPriority.LOW)
        self.app.send_notification("background-reminder", notification)

    # --------------------------------------------------------- visibility

    def update_visibility(self, visible: bool) -> None:
        self._updating_visibility_switch = True
        try:
            self.visibility_row.set_active(visible)
        finally:
            self._updating_visibility_switch = False
        name = self.app.service.device_name
        if visible:
            subtitle = f"Visible as {name} — nearby devices can send you files"
        else:
            subtitle = "Hidden — you can still send to nearby devices"
        self.visibility_row.set_subtitle(subtitle)

    def _on_visibility_switch_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        if self._updating_visibility_switch:
            return  # programmatic update from on_visibility_changed, not a click
        wanted = row.get_active()
        self.app.asyncio_thread.run_coroutine(
            self.app.service.set_visible(wanted))

    # ------------------------------------------------------------ airdrop

    def _airdrop_tick(self) -> bool:
        self.update_airdrop()
        return GLib.SOURCE_CONTINUE

    def update_airdrop(self) -> None:
        """Mirror the AirDrop window's state into its switch row."""
        status = self.app.service.airdrop.status()
        state = status["state"]
        self._updating_airdrop_switch = True
        try:
            self.airdrop_row.set_active(state in ("on", "starting"))
        finally:
            self._updating_airdrop_switch = False
        self.airdrop_row.set_sensitive(state in ("on", "off"))
        if state == "on":
            remaining = status.get("remaining")
            if isinstance(remaining, int):
                minutes = max(1, round(remaining / 60))
                subtitle = (f"AirDrop — visible for {minutes} more "
                            f"minute{'s' if minutes != 1 else ''}")
            else:
                subtitle = "AirDrop — visible until turned off"
        elif state == "starting":
            subtitle = "AirDrop — starting the radio…"
        elif state == "stopping":
            subtitle = "AirDrop — stopping…"
        elif status.get("ready") is False and status.get("explain"):
            subtitle = f"AirDrop unavailable — {status['explain']}"
        else:
            subtitle = "AirDrop — off"
        self.airdrop_row.set_subtitle(subtitle)

    def _on_airdrop_switch_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        if self._updating_airdrop_switch:
            return
        wanted = row.get_active()
        airdrop = self.app.service.airdrop

        async def change() -> None:
            await airdrop.set_visible(wanted)
            if wanted:
                # Turning AirDrop on is usually about to share something:
                # start looking for Apple devices straight away.
                await airdrop.discover(120)

        def after(_result, exc: BaseException | None) -> None:
            self.update_airdrop()
            if exc is not None:
                message = (str(exc) if isinstance(exc, RadioError)
                           else f"Unexpected error: {exc}")
                dialog = Adw.AlertDialog.new("AirDrop couldn't start", message)
                dialog.add_response("ok", "OK")
                dialog.set_close_response("ok")
                dialog.choose(self, None, lambda *_: None)

        self.app.asyncio_thread.run_coroutine(change(), after)
        self.update_airdrop()

    def update_airdrop_peers(self, peers: dict[str, AirDropPeer]) -> None:
        """Only devices that told us their name get a row: one that never
        answered /Discover is not one a send is likely to reach."""
        named = {f"airdrop:{k}": p for k, p in peers.items() if p.name}
        self._airdrop_peers = named
        for key in [k for k in self._airdrop_rows if k not in named]:
            self.devices_group.remove(self._airdrop_rows.pop(key))
        for key, peer in named.items():
            if key in self._airdrop_rows:
                self._update_peer_row(self._airdrop_rows[key], peer)
            else:
                self._airdrop_rows[key] = self._add_peer_row(key, peer)
        self._devices_settled = True
        self._update_devices_empty_state()

    # ------------------------------------------------------------- peers

    def update_peers(self, peers: dict[str, Peer]) -> None:
        self._peers = peers
        for key in [k for k in self._peer_rows if k not in peers]:
            self.devices_group.remove(self._peer_rows.pop(key))
        for key, peer in peers.items():
            if key in self._peer_rows:
                self._update_peer_row(self._peer_rows[key], peer)
            else:
                self._peer_rows[key] = self._add_peer_row(key, peer)
        # Any callback at all -- even one that empties the list again --
        # proves discovery is up and running, so drop the "might still
        # be warming up" spinner state for good.
        self._devices_settled = True
        self._update_devices_empty_state()

    def _update_peer_row(self, row: Adw.ActionRow,
                         peer: Peer | AirDropPeer) -> None:
        if isinstance(peer, AirDropPeer):
            row.set_title(peer.display_name)
            row.set_subtitle(f"AirDrop • {_apple_device_kind(peer.model)}")
            return
        row.set_title(peer.device_name)
        row.set_subtitle(
            f"{_DEVICE_TYPE_NAMES.get(peer.device_type, 'Device')} "
            f"• {peer.host}")

    def _set_compact(self, compact: bool) -> None:
        """Narrow-window layout: hide per-row progress bars.

        Driven by the window's Adw.Breakpoint. Applies to rows that
        already exist and is remembered for rows created later.
        """
        self._compact = compact
        for row in list(self._active.values()) + list(self._completed):
            if not row.finished:
                row.progress.set_visible(not compact)

    def _add_peer_row(self, key: str, peer: Peer) -> Adw.ActionRow:
        row = Adw.ActionRow()
        # Device names and the "IP • type" subtitle are arbitrarily long;
        # without a line cap they push the Send button off the row at
        # narrow widths instead of ellipsizing.
        row.set_title_lines(1)
        row.set_subtitle_lines(1)
        self._update_peer_row(row, peer)
        button = Gtk.Button(label="Send files…", valign=Gtk.Align.CENTER,
                            css_classes=["suggested-action"])
        button.connect("clicked", lambda _b: self._on_send_clicked(key))
        row.add_suffix(button)
        self.devices_group.add(row)
        return row

    def _on_send_clicked(self, key: str) -> None:
        peer = self._peers.get(key) or self._airdrop_peers.get(key)
        if peer is None:
            return  # peer vanished between click and callback
        dialog = Gtk.FileDialog(title=f"Send files to {_peer_name(peer)}")

        def on_chosen(source: Gtk.FileDialog, result: Gio.AsyncResult,
                     _data=None) -> None:
            try:
                files = source.open_multiple_finish(result)
            except GLib.Error:
                return  # user cancelled
            paths = []
            for i in range(files.get_n_items()):
                gfile = files.get_item(i)
                path = gfile.get_path()
                if path:
                    paths.append(Path(path))
            if paths:
                self._start_send(peer, paths)

        dialog.open_multiple(self, None, on_chosen)

    def _start_send(self, peer: Peer | AirDropPeer,
                    files: list[Path]) -> None:
        name = _peer_name(peer)
        key = (name, "send")
        total = sum(f.stat().st_size for f in files if f.is_file())
        row = self._new_active_row(key, f"Sending to {name}", total)

        def after(_result, exc: BaseException | None) -> None:
            # Success/failure UI is normally driven by the service's
            # Events callbacks (on_complete/on_error), which fire from
            # inside OutboundConnection.run() before it re-raises. This
            # is a backstop for exceptions raised outside that path.
            if exc is not None and not row.finished:
                self._active.pop(key, None)
                row.mark_error(str(exc))
                self._retire_row(row)

        self.app.asyncio_thread.run_coroutine(
            self.app.service.send_files(peer, files), after)

    # --------------------------------------------------------- transfers

    def _new_active_row(self, key: tuple[str, str], title: str,
                        total: int) -> _TransferRow:
        """Create (replacing any stale row already at `key`) the active
        row for a freshly-started session and register it."""
        self._empty_transfers_status.set_visible(False)
        old = self._active.pop(key, None)
        if old is not None:
            old.remove()

        def on_remove(row: _TransferRow) -> None:
            self._active.pop(key, None)
            if row in self._completed:
                self._completed.remove(row)
            row.remove()
            self._update_empty_transfers_visibility()

        row = _TransferRow(self.transfers_group, title, total, on_remove)
        if self._compact:
            row.progress.set_visible(False)
        self._active[key] = row
        return row

    def _retire_row(self, row: _TransferRow) -> None:
        """Move a finished row from _active into the capped _completed
        list (removing its widget once the cap is exceeded)."""
        self._completed.append(row)
        while len(self._completed) > 5:
            self._completed.pop(0).remove()
        self._update_empty_transfers_visibility()

    def _update_empty_transfers_visibility(self) -> None:
        self._empty_transfers_status.set_visible(
            not self._active and not self._completed)

    def _find_active_row(
            self, device: str) -> tuple[tuple[str, str], _TransferRow] | None:
        """Events callbacks only carry a device name, not a direction, so
        match it back to whichever active session is at (device,
        "receive") or (device, "send")."""
        for direction in ("receive", "send"):
            key = (device, direction)
            row = self._active.get(key)
            if row is not None:
                return key, row
        # Outbound connections fall back to "Unknown device" until the
        # handshake sets peer_name from the mDNS-discovered name we
        # passed in (core/connection.py); if that hasn't landed yet and
        # there's exactly one active send, it's almost certainly this
        # one.
        sends = [(k, r) for k, r in self._active.items() if k[1] == "send"]
        if len(sends) == 1:
            return sends[0]
        return None

    def on_progress(self, device: str, done: int, total: int) -> None:
        found = self._find_active_row(device)
        if found is not None:
            found[1].update_progress(done, total)

    def on_transfer_complete(self, device: str, paths: list[Path]) -> None:
        found = self._find_active_row(device)
        if found is not None:
            key, row = found
            del self._active[key]
            folder = (paths[0].parent if key[1] == "receive" and paths
                     else None)
            row.mark_complete(folder, len(paths))
            self._retire_row(row)
        self.toast(f"Transfer with {device} complete")

    def on_transfer_error(self, device: str, message: str) -> None:
        found = self._find_active_row(device)
        if found is not None:
            key, row = found
            del self._active[key]
            row.mark_error(message)
            self._retire_row(row)
        self.toast(f"Transfer with {device} failed: {message}")

    # ---------------------------------------------------- incoming request

    def show_transfer_request(self, request: TransferRequest,
                              set_result) -> None:
        """Runs on the GTK thread (see app.py's Events wiring: the async
        core callback marshals here via idle()). `set_result(bool)` is
        the thread-safe setter from bridge_future_to_asyncio that resumes
        the awaiting coroutine on the asyncio thread."""
        count = len(request.files)
        plural = "file" if count == 1 else "files"
        body = (f"{request.device_name} wants to share {count} {plural} "
               f"({_human_size(request.total_size)}).")
        if request.text_preview:
            body += f'\nIncludes text: "{request.text_preview}"'

        # AirDrop has no PIN; an empty one is how its requests arrive.
        via = "" if request.pin else " over AirDrop"
        dialog = Adw.AlertDialog.new(
            f"Incoming transfer from {request.device_name}{via}", body)
        if request.pin:
            pin_label = Gtk.Label(
                label=f"Verify PIN: {request.pin}",
                css_classes=["title-2"], margin_top=12, margin_bottom=6)
            dialog.set_extra_child(pin_label)
        dialog.add_response("decline", "Decline")
        dialog.add_response("accept", "Accept")
        dialog.set_response_appearance(
            "decline", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance(
            "accept", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("accept")
        dialog.set_close_response("decline")

        def on_response(source: Adw.AlertDialog, result: Gio.AsyncResult) -> None:
            response = source.choose_finish(result)
            accepted = response == "accept"
            if accepted:
                key = (request.device_name, "receive")
                self._new_active_row(
                    key, f"Receiving from {request.device_name}",
                    request.total_size)
            set_result(accepted)

        dialog.choose(self, None, on_response)

    def notify_incoming(self, request: TransferRequest) -> None:
        """Desktop notification so the request is visible even if the
        window is hidden/backgrounded (GNOME has no tray icon, see
        PLAN.md risk 'no system tray')."""
        count = len(request.files)
        notification = Gio.Notification.new(
            f"{request.device_name} wants to share {count} "
            f"{'file' if count == 1 else 'files'}")
        notification.set_body(
            f"Verify PIN: {request.pin}" if request.pin
            else "AirDrop — open NearShare to accept or decline.")
        notification.set_priority(Gio.NotificationPriority.URGENT)
        self.app.send_notification("transfer-request", notification)

    # ------------------------------------------------------------- text

    def show_text(self, device: str, text: str) -> None:
        dialog = Adw.AlertDialog.new(f"Text from {device}", "")
        buffer = Gtk.TextBuffer(text=text)
        text_view = Gtk.TextView(
            buffer=buffer, editable=False, cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            css_classes=["card"], margin_top=6, margin_bottom=6)
        scroller = Gtk.ScrolledWindow(
            child=text_view, min_content_height=120, max_content_height=300)
        dialog.set_extra_child(scroller)
        dialog.add_response("close", "Close")
        dialog.add_response("copy", "Copy")
        # A lone web link (what an AirDropped link is) can be opened, but
        # only by the person clicking: it came from an unauthenticated peer.
        stripped = text.strip()
        is_link = (stripped.lower().startswith(("http://", "https://"))
                   and not any(c.isspace() for c in stripped))
        if is_link:
            dialog.add_response("open", "Open Link")
            dialog.set_response_appearance("open",
                                           Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("open")
        else:
            dialog.set_response_appearance("copy",
                                           Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("copy")
        dialog.set_close_response("close")

        def on_response(source: Adw.AlertDialog, result: Gio.AsyncResult) -> None:
            response = source.choose_finish(result)
            if response == "copy":
                self._copy_to_clipboard(text)
            elif response == "open":
                Gtk.UriLauncher.new(stripped).launch(self, None, None)

        dialog.choose(self, None, on_response)

    def _copy_to_clipboard(self, text: str) -> None:
        display = self.get_display()
        if display is not None:
            display.get_clipboard().set(text)
            self.toast("Copied to clipboard")

    # --------------------------------------------------------- direct mode

    def _on_direct_mode_toggled(self, expander: Adw.ExpanderRow, _pspec) -> None:
        if self._updating_hotspot_switch:
            return  # programmatic reset below, not a user click
        if expander.get_enable_expansion():
            self._start_hotspot()
        else:
            self._stop_hotspot()

    def _set_hotspot_switch(self, enabled: bool) -> None:
        """Change the switch without re-triggering _on_direct_mode_toggled
        (used when a failed start needs to snap the switch back off)."""
        self._updating_hotspot_switch = True
        try:
            self.direct_expander.set_enable_expansion(enabled)
        finally:
            self._updating_hotspot_switch = False

    def _start_hotspot(self) -> None:
        self.hotspot_status_row.set_subtitle("Starting…")

        def after(result, exc: BaseException | None) -> None:
            if exc is not None:
                message = (str(exc) if isinstance(exc, HotspotError)
                          else f"unexpected error: {exc}")
                self.hotspot_status_row.set_subtitle("Off")
                self._set_hotspot_switch(False)
                self._show_hotspot_error(message)
                return
            ssid, password = result
            self.hotspot_status_row.set_subtitle("On")
            self.hotspot_ssid_row.set_subtitle(ssid)
            self.hotspot_password_row.set_subtitle(password)
            self.qr_area.set_text(wifi_qr_payload(ssid, password))

        self.app.asyncio_thread.run_coroutine(
            self.app.hotspot.start(), after)

    def _stop_hotspot(self) -> None:
        self.hotspot_status_row.set_subtitle("Stopping…")

        def after(_result, exc: BaseException | None) -> None:
            self.hotspot_status_row.set_subtitle("Off")
            self.hotspot_ssid_row.set_subtitle("")
            self.hotspot_password_row.set_subtitle("")
            self.qr_area.set_text("")
            if exc is not None:
                self.toast(f"Direct mode teardown reported: {exc}")

        self.app.asyncio_thread.run_coroutine(self.app.hotspot.stop(), after)

    def _show_hotspot_error(self, message: str) -> None:
        dialog = Adw.AlertDialog.new("Direct mode couldn't start", message)
        dialog.add_response("ok", "OK")
        dialog.set_close_response("ok")
        dialog.choose(self, None, lambda *_: None)

    # -------------------------------------------------------------- misc

    def toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast.new(message))


class NearShareApplication(Adw.Application):
    """Owns the service, the asyncio background thread, and the hotspot;
    the window is created lazily and hidden (not destroyed) on close so
    the service keeps running -- `app.hold()` is what keeps the process
    alive with zero windows open."""

    def __init__(self) -> None:
        super().__init__(application_id=BUS_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.asyncio_thread = AsyncioThread()
        self.hotspot = Hotspot()
        self.service: NearShareService | None = None
        self.window: NearShareWindow | None = None

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<primary>q"])

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self.asyncio_thread.start()

        events = Events(
            on_transfer_request=self._on_transfer_request,
            on_progress=lambda dev, done, total: idle(
                self._with_window(lambda w: w.on_progress(dev, done, total))),
            on_complete=lambda dev, paths: idle(
                self._with_window(
                    lambda w: w.on_transfer_complete(dev, paths))),
            on_error=lambda dev, err: idle(
                self._with_window(lambda w: w.on_transfer_error(dev, err))),
            on_text=lambda dev, text: idle(
                self._with_window(lambda w: w.show_text(dev, text))),
        )
        self.service = NearShareService(events=events)
        self.service.on_visibility_changed = lambda visible: idle(
            self._with_window(lambda w: w.update_visibility(visible)))
        self.service.on_peers_changed = lambda peers: idle(
            self._with_window(lambda w: w.update_peers(peers)))
        self.service.airdrop.on_state_changed = lambda _state: idle(
            self._with_window(lambda w: w.update_airdrop()))
        self.service.airdrop.on_peers_changed = lambda peers: idle(
            self._with_window(lambda w: w.update_airdrop_peers(peers)))

        def after_start(_result, exc: BaseException | None) -> None:
            if exc is not None:
                log.error("service failed to start: %s", exc)
                return
            idle(self._with_window(
                lambda w: w.update_ble_status(self.service.ble_status)))

        self.asyncio_thread.run_coroutine(
            self.service.start(visible=True), after_start)
        # Keep the process alive even with zero windows open (window
        # close hides rather than destroys; the service must keep
        # serving the control socket for the CLI regardless).
        self.hold()

    def do_activate(self) -> None:
        # Icon association on GNOME/Wayland comes from the .desktop file
        # matching APP_ID; the explicit default covers X11 and non-GNOME.
        Gtk.Window.set_default_icon_name(APP_ID)
        _install_css()
        if self.window is None:
            self.window = NearShareWindow(self)
        self.window.present()

    def do_shutdown(self) -> None:
        if self.hotspot.active:
            fut = self.asyncio_thread.run_coroutine(self.hotspot.stop())
            try:
                fut.result(timeout=5)
            except Exception as exc:  # best-effort on the way out
                log.warning("hotspot teardown on shutdown: %s", exc)
        if self.service is not None:
            fut = self.asyncio_thread.run_coroutine(self.service.stop())
            try:
                fut.result(timeout=5)
            except Exception as exc:
                log.warning("service stop on shutdown: %s", exc)
        self.asyncio_thread.stop()
        Adw.Application.do_shutdown(self)

    # ---------------------------------------------------- event dispatch

    def _with_window(self, fn):
        """Wrap a window-method call so callbacks that fire before any
        window has ever been created (service starts in do_startup,
        before do_activate) are silently no-ops instead of crashing."""
        def call() -> None:
            if self.window is not None:
                fn(self.window)
        return call

    async def _on_transfer_request(self, request: TransferRequest) -> bool:
        """Awaited on the asyncio thread inside InboundConnection; must
        not return until the user (on the GTK thread) responds. See
        asyncio_glue.bridge_future_to_asyncio for how the two threads
        meet."""
        future, set_result = bridge_future_to_asyncio(self.asyncio_thread.loop)
        idle(self._with_window(
            lambda w: (w.notify_incoming(request),
                      w.show_transfer_request(request, set_result))))
        if self.window is None:
            # No window yet (app started headless via the control
            # socket/CLI): still fire the notification, but there's no
            # dialog to drive a decision, so decline rather than hang
            # the sender forever waiting on a UI that doesn't exist.
            set_result(False)
        return await future


def main() -> int:
    app = NearShareApplication()
    return app.run(sys.argv)
