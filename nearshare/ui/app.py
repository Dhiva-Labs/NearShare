"""GTK4 + libadwaita front-end for NearShare, built on the headless
`NearShareService` core (see core/service.py, core/connection.py,
core/mdns.py).

The window has four parts: the visibility switch, the nearby devices
list (click a device, or drop files on it, to send), the transfers list
with live progress, and the experimental Direct-mode hotspot. Incoming
transfers raise a PIN dialog and a desktop notification with Accept and
Decline buttons, so they can be answered while the window is hidden.

Threading model: see asyncio_glue.py's module docstring. In short, the
asyncio loop the service depends on runs on its own background thread;
every `Events` callback below re-enters the GTK thread via `idle(...)`
before touching a widget, and every button handler that needs to call
into the service goes back out via `self.app.asyncio_thread.run_coroutine`.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .. import __version__, autostart, cli  # noqa: E402
from ..core.connection import Events, TransferRequest  # noqa: E402
from ..core.hotspot import Hotspot, HotspotError  # noqa: E402
from ..core.mdns import Peer  # noqa: E402
from ..core.service import NearShareService, default_device_name  # noqa: E402
from . import prefs  # noqa: E402
from .asyncio_glue import (AsyncioThread, Coalescer,  # noqa: E402
                           bridge_future_to_asyncio, idle)
from .fmt import (count_files, describe_files, device_icon,  # noqa: E402
                  device_type_name, human_duration, human_size, human_speed,
                  split_sendable, total_size)
from .qrcode import QrCodeArea, wifi_qr_payload  # noqa: E402
from .style import APP_ID, install_css as _install_css  # noqa: E402,F401

_APT_INSTALL_COMMAND = ("sudo add-apt-repository ppa:dhiva-labs/apps && "
                       "sudo apt install nearshare")

log = logging.getLogger("nearshare.ui.app")

WEBSITE = "https://github.com/Dhiva-Labs/NearShare"

# Inside a strictly-confined snap, AppArmor only permits owning a
# session-bus name matching the snap's own name: registering as APP_ID
# fails with "not allowed to own the service" and the GUI never starts.
# A dbus slot would grant it, but that forces the snap into the Snap
# Store's manual review queue, so use the already-permitted name. The
# desktop file and icon keep their reverse-DNS identity either way --
# only the bus name differs, and only under snap confinement.
BUS_ID = (os.environ.get("SNAP_INSTANCE_NAME", APP_ID)
          if os.environ.get("SNAP") else APP_ID)

# The snap interfaces snapd does not connect on its own, and what each
# one is for. Shown in the setup group until connected.
_SNAP_INTERFACES = (
    ("bluez", "Bluetooth discovery",
     "Lets phones find this computer without opening Quick Share first."),
    ("network-manager", "Direct mode",
     "Needed for the built-in hotspot when there is no shared Wi-Fi."),
)

# Kept for tools/ and anything else that imported the old private names.
_human_size = human_size
_human_speed = human_speed
_human_duration = human_duration


def _esc(text: str) -> str:
    """Row titles and subtitles are Pango markup: a device called
    "Tom & Ana's phone" must not be parsed as a broken entity."""
    return GLib.markup_escape_text(text)


class _TransferRow:
    """One row in the Transfers section, tracking a single (device,
    direction) session -- see NearShareWindow._active / _completed for
    how rows are keyed and retired.

    The row is a title and status line over a full-width progress bar.
    A bar squeezed in beside the text, as a row suffix, left no room for
    either in a narrow window.
    """

    def __init__(self, group: Adw.PreferencesGroup, title: str,
                 total: int, on_remove: Callable[["_TransferRow"], None],
                 icon_name: str = "emblem-synchronizing-symbolic",
                 on_cancel: Callable[["_TransferRow"], None] | None = None,
                 detail: str = "") -> None:
        # What is being transferred ("report.pdf and 2 more"). Kept out
        # of the title so a long file name cannot crowd out the device.
        self.detail = detail
        self.total = total
        self.done = 0
        self.finished = False
        # Reset when the first byte moves: a send spends its first
        # seconds waiting for the other person to accept, and counting
        # that would understate the speed for the whole transfer.
        self.started_at = self._created_at = time.monotonic()
        self._moving = False
        self._waiting_text = "Connecting…"
        self._on_remove = on_remove
        self._on_cancel = on_cancel

        self.row = Adw.PreferencesRow(activatable=False, selectable=False)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.row.set_child(outer)

        line = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10,
                       margin_start=12, margin_end=8)
        outer.append(line)
        self.status_icon = Gtk.Image.new_from_icon_name(icon_name)
        line.append(self.status_icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                       hexpand=True, valign=Gtk.Align.CENTER)
        line.append(text)
        # A long device name ellipsizes; the status line wraps, so a long
        # error message stays fully readable.
        self.title_label = Gtk.Label(
            label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END,
            css_classes=["heading"])
        self.subtitle_label = Gtk.Label(
            xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
            css_classes=["caption", "dim-label"])
        text.append(self.title_label)
        text.append(self.subtitle_label)

        self.open_button = Gtk.Button(
            icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Show in Files", visible=False,
            css_classes=["flat"])
        line.append(self.open_button)
        self.remove_button = Gtk.Button(
            icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
            css_classes=["flat"])
        self.remove_button.connect("clicked", self._on_button_clicked)
        line.append(self.remove_button)

        self.progress = Gtk.ProgressBar(css_classes=["transfer-progress"])
        outer.append(self.progress)

        group.add(self.row)
        self._group = group
        self._sync_button()
        self._update_subtitle()

    # The one button cancels a running transfer and dismisses a finished
    # one. Before, it only ever removed the row, and the transfer it
    # appeared to stop kept running out of sight.
    def _sync_button(self) -> None:
        if self.finished or self._on_cancel is None:
            self.remove_button.set_tooltip_text("Remove from this list")
        else:
            self.remove_button.set_tooltip_text("Cancel transfer")

    def _on_button_clicked(self, _button: Gtk.Button) -> None:
        if not self.finished and self._on_cancel is not None:
            self._on_cancel(self)
        else:
            self._on_remove(self)

    def set_waiting_text(self, text: str) -> None:
        """Status shown until bytes start moving (connecting, the PIN)."""
        self._waiting_text = text
        self._update_subtitle()

    def _update_subtitle(self) -> None:
        if self.finished:
            return
        if not self._moving and self.done == 0:
            self.subtitle_label.set_label(self._waiting_text)
            return
        pct = int(self.done * 100 / self.total) if self.total else 0
        parts = [f"{human_size(self.done)} of {human_size(self.total)} ({pct}%)"]
        # Held back for the first half second so a burst of early
        # callbacks doesn't show a wildly wrong rate.
        elapsed = time.monotonic() - self.started_at
        if elapsed > 0.5 and self.done > 0:
            speed = self.done / elapsed
            parts.append(human_speed(speed))
            if self.total > self.done and speed > 0:
                eta = (self.total - self.done) / speed
                parts.append(f"{human_duration(eta)} left")
        self.subtitle_label.set_label(" · ".join(parts))

    def update_progress(self, done: int, total: int) -> None:
        if self.finished:
            return
        if not self._moving and done > 0:
            self._moving = True
            # Unless something set the clock deliberately (the
            # screenshot tools backdate it to show a realistic speed).
            if self.started_at == self._created_at:
                self.started_at = time.monotonic()
        self.done, self.total = done, total or self.total
        self.progress.set_fraction(
            min(1.0, self.done / self.total) if self.total else 0.0)
        self._update_subtitle()

    def _finish(self, icon_name: str, css_class: str, text: str) -> None:
        self.finished = True
        self.progress.set_visible(False)
        self.status_icon.set_from_icon_name(icon_name)
        self.status_icon.add_css_class(css_class)
        self.subtitle_label.set_label(text)
        self._sync_button()

    def mark_complete(self, folder: Path | None, count: int) -> None:
        elapsed = human_duration(time.monotonic() - self.started_at)
        size = f" · {human_size(self.total)}" if self.total else ""
        what = self.detail or count_files(count)
        self._finish("emblem-ok-symbolic", "success",
                     f"Done · {what}{size} · {elapsed}")
        if folder is not None:
            self.open_button.set_visible(True)
            self.open_button.set_tooltip_text(f"Show in {folder}")
            self.open_button.connect(
                "clicked", lambda _b: _open_folder(folder))

    def mark_error(self, message: str) -> None:
        self._finish("dialog-error-symbolic", "error", f"Failed: {message}")
        self.subtitle_label.remove_css_class("dim-label")
        self.subtitle_label.add_css_class("error")

    def mark_cancelled(self) -> None:
        self._finish("process-stop-symbolic", "dim-label", "Cancelled")

    def remove(self) -> None:
        self._group.remove(self.row)


def _open_folder(folder: Path) -> None:
    uri = GLib.filename_to_uri(str(folder), None)
    try:
        Gio.AppInfo.launch_default_for_uri(uri, None)
    except GLib.Error as exc:
        log.warning("could not open %s: %s", folder, exc)


def _paths_from_drop(value: Any) -> list[Path]:
    """Local paths out of a Gdk.FileList drop (or a single Gio.File)."""
    files = value.get_files() if hasattr(value, "get_files") else [value]
    return [Path(f.get_path()) for f in files if f.get_path()]


def _copy_row_button(text_getter: Callable[[], str | None],
                     copy: Callable[[str], None]) -> Gtk.Button:
    button = Gtk.Button(icon_name="edit-copy-symbolic",
                        valign=Gtk.Align.CENTER, tooltip_text="Copy",
                        css_classes=["flat"])

    def on_clicked(_button: Gtk.Button) -> None:
        value = text_getter()
        if value:
            copy(value)

    button.connect("clicked", on_clicked)
    return button


class NearShareWindow(Adw.ApplicationWindow):
    """The single main window; closing it hides rather than destroys it
    (see NearShareApplication's hidden-window pattern) so the service
    keeps serving the control socket/mDNS/TCP in the background."""

    def __init__(self, application: "NearShareApplication") -> None:
        super().__init__(application=application, title="NearShare",
                         default_width=480, default_height=680,
                         width_request=360, height_request=440)
        self.app = application
        self._peer_rows: dict[str, Adw.ActionRow] = {}
        self._peer_buttons: dict[str, Gtk.Button] = {}
        self._peers: dict[str, Peer] = {}
        # One row per (device, direction) *active* transfer, updated in
        # place as progress events arrive (done/total are cumulative
        # across the whole batch, not per-file -- see connection.py's
        # _send_file/_receive_payloads). On completion/error the row
        # moves into _completed (capped at the 5 most recent) so the
        # outcome stays visible. See _find_active_row for how
        # progress/complete/error callbacks (which only carry a device
        # name, not a direction) are matched back to a key.
        self._active: dict[tuple[str, str], _TransferRow] = {}
        self._completed: list[_TransferRow] = []
        # Files chosen (dropped on the window, or picked with the header
        # button) before a device was: the next device clicked gets them.
        self._staged: list[Path] = []
        # Incoming requests waiting for an answer, by id, so the dialog
        # and the notification buttons can both resolve the same one.
        self._requests: dict[str, tuple[TransferRequest, Callable[[bool], None],
                                        Adw.AlertDialog]] = {}
        self._request_ids = itertools.count(1)
        self._updating_visibility_switch = False
        self._updating_hotspot_switch = False
        # Only nag once per process run that closing the window doesn't
        # quit the app -- the first-ever close already explains this at
        # length (see _show_background_explainer); repeating a dialog
        # every time would be the annoying kind of "helpful".
        self._background_notice_sent = False

        self.connect("close-request", self._on_close_request)
        self._install_actions()
        self._build_ui()

    # -------------------------------------------------------------- build

    def _install_actions(self) -> None:
        for name, handler in (
            ("send-files", lambda *_: self._choose_files_to_stage()),
            ("show-setup", lambda *_: self._show_setup_again()),
            ("clear-transfers", lambda *_: self._clear_finished()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        self.window_title = Adw.WindowTitle(
            title="NearShare", subtitle=self.app.service.device_name)
        header = Adw.HeaderBar(title_widget=self.window_title)

        send_button = Gtk.Button(
            icon_name="document-send-symbolic",
            tooltip_text="Send Files… (Ctrl+O)",
            action_name="win.send-files")
        header.pack_start(send_button)

        menu = Gio.Menu()
        primary = Gio.Menu()
        primary.append("Send Files…", "win.send-files")
        primary.append("Preferences", "app.preferences")
        menu.append_section(None, primary)
        secondary = Gio.Menu()
        secondary.append("Setup Tips", "win.show-setup")
        secondary.append("About NearShare", "app.about")
        secondary.append("Quit", "app.quit")
        menu.append_section(None, secondary)
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic", menu_model=menu,
            tooltip_text="Main Menu", primary=True)
        header.pack_end(menu_button)
        toolbar_view.add_top_bar(header)

        # Shown while files are staged and no device has been picked yet.
        self.staged_banner = Adw.Banner(button_label="Cancel", revealed=False)
        self.staged_banner.connect(
            "button-clicked", lambda _b: self._set_staged([]))
        toolbar_view.add_top_bar(self.staged_banner)

        self.toast_overlay = Adw.ToastOverlay()
        toolbar_view.set_content(self.toast_overlay)

        # Adw.PreferencesPage scrolls by itself; wrapping it in another
        # ScrolledWindow only nested two scrollers.
        page = Adw.PreferencesPage()
        self.toast_overlay.set_child(page)

        page.add(self._build_setup_group())
        page.add(self._build_visibility_group())
        page.add(self._build_devices_group())
        page.add(self._build_transfers_group())
        page.add(self._build_direct_mode_group())
        # The service is usually still starting when the window is built
        # (registering on mDNS takes a second or two). Show the state it
        # is heading for rather than an "off" switch that flips itself.
        self.update_visibility(
            self.app.service.visible or self.app.starting_visible)

        # Dropping files anywhere on the window stages them; dropping on
        # a device row (which has its own target) sends straight to it.
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_window_drop)
        drop.connect("enter", self._on_drop_enter, page)
        drop.connect("leave", self._on_drop_leave, page)
        self.toast_overlay.add_controller(drop)

        self.refresh_setup_status()

    def _build_setup_group(self) -> Adw.PreferencesGroup:
        """The setup tips: whatever desktop integration isn't done yet
        (launcher on PATH, desktop entries, Nautilus integration,
        keyboard shortcut, and under snap the interfaces snapd leaves
        unconnected), with one button that runs `nearshare install`'s
        logic to fix everything fixable here.

        One collapsed row, so optional housekeeping never pushes the
        device list off the first screen. It starts hidden and is filled
        in by refresh_setup_status once the checks have run off the main
        thread. The user can hide it for good; "Setup Tips" in the menu
        brings it back."""
        self.setup_group = Adw.PreferencesGroup(visible=False)
        self.setup_expander = Adw.ExpanderRow(title="Finish setting up")
        self.setup_expander.add_prefix(
            Gtk.Image.new_from_icon_name("emblem-system-symbolic"))
        self.setup_group.add(self.setup_expander)

        self._setup_rows: dict[str, Adw.ActionRow] = {}
        self._setup_row_icons: dict[str, Gtk.Image] = {}
        for key, title in (
            ("launcher_on_path", "Command-line launcher on PATH"),
            ("desktop_entries", "Desktop app entries (app grid, search)"),
            ("nautilus", "Right-click “Send with NearShare” in Files"),
        ):
            row = Adw.ActionRow(title=title, selectable=False)
            icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            row.add_prefix(icon)
            self.setup_expander.add_row(row)
            self._setup_rows[key] = row
            self._setup_row_icons[key] = icon

        # One row per snap interface that is not connected yet, each with
        # the exact command and a copy button: a confined app cannot
        # connect its own interfaces, so the best it can do is make the
        # one command needed impossible to get wrong.
        self._snap_interface_rows: dict[str, Adw.ActionRow] = {}
        for name, title, why in _SNAP_INTERFACES:
            command = f"sudo snap connect nearshare:{name}"
            row = Adw.ActionRow(
                title=f"{title} needs one command",
                subtitle=f"{why} Run in a terminal: {command}",
                selectable=False, visible=False)
            row.set_subtitle_lines(0)
            row.add_prefix(
                Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
            row.add_suffix(_copy_row_button(
                lambda command=command: command, self._copy_to_clipboard))
            self.setup_expander.add_row(row)
            self._snap_interface_rows[name] = row

        # The shortcut row stays visible even once bound (unlike the
        # three above, which just disappear when done) so it always
        # shows the actual accelerator in human-readable form (e.g.
        # "Ctrl+Alt+N") rather than gsettings' raw "<Control><Alt>n" --
        # see _apply_setup_status.
        self._shortcut_row = Adw.ActionRow(title="Keyboard shortcut",
                                           selectable=False)
        self._shortcut_row.set_subtitle_lines(0)
        self._shortcut_icon = Gtk.Image.new_from_icon_name(
            "dialog-warning-symbolic")
        self._shortcut_row.add_prefix(self._shortcut_icon)
        self.setup_expander.add_row(self._shortcut_row)

        self._setup_deb_row = Adw.ActionRow(
            title="Right-click sharing needs the .deb build",
            subtitle=_esc("A confined snap cannot add a Files extension. "
                          f"Run in a terminal: {_APT_INSTALL_COMMAND}"),
            selectable=False)
        self._setup_deb_row.set_subtitle_lines(0)
        self._setup_deb_row.add_prefix(
            Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
        self._setup_deb_row.add_suffix(_copy_row_button(
            lambda: _APT_INSTALL_COMMAND, self._copy_to_clipboard))
        self.setup_expander.add_row(self._setup_deb_row)

        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.END, margin_top=10,
                          margin_bottom=10, margin_start=12, margin_end=12)
        hide_button = Gtk.Button(label="Don't Show Again",
                                 css_classes=["flat"])
        hide_button.connect("clicked", self._on_hide_setup_clicked)
        buttons.append(hide_button)
        self._setup_button = Gtk.Button(
            label="Finish Setup", css_classes=["suggested-action"])
        self._setup_button.connect("clicked", self._on_finish_setup_clicked)
        buttons.append(self._setup_button)
        self.setup_expander.add_row(
            Adw.PreferencesRow(child=buttons, activatable=False,
                               selectable=False))

        return self.setup_group

    def refresh_setup_status(self) -> None:
        """Re-check the desktop integration and update the setup group.

        cli.integration_status() shells out to gsettings (and snapctl
        under snap) several times. Run on the GTK thread, as it used to
        be, that delayed the window's first frame; it now runs on a
        worker thread and the group appears when the answer is in.
        """
        self.app.asyncio_thread.run_coroutine(
            asyncio.to_thread(cli.integration_status),
            lambda status, exc: (
                log.warning("setup status check failed: %s", exc)
                if exc is not None else self._apply_setup_status(status)))

    def _apply_setup_status(self, status: dict[str, Any]) -> None:
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

        interfaces = status.get("snap_interfaces") or {}
        missing_interface = False
        for name, row in self._snap_interface_rows.items():
            missing = interfaces.get(name) is False
            row.set_visible(missing)
            missing_interface = missing_interface or missing

        if status["shortcut"] and status["shortcut_accel"]:
            label = cli.accelerator_label(status["shortcut_accel"])
            self._shortcut_row.set_subtitle(
                f"{label} shows or hides this computer")
            self._shortcut_icon.set_from_icon_name("emblem-ok-symbolic")
        elif status["shortcut_fixable"]:
            default_label = cli.accelerator_label(cli.DEFAULT_SHORTCUT_BINDING)
            self._shortcut_row.set_subtitle(
                f"Not set yet — Finish setup will use {default_label}")
            self._shortcut_icon.set_from_icon_name("dialog-warning-symbolic")
        else:
            self._shortcut_row.set_subtitle(
                "Not available in the snap. Add one yourself in Settings ▸ "
                "Keyboard ▸ Custom Shortcuts with the command "
                "`nearshare toggle`.")
            self._shortcut_icon.set_from_icon_name(
                "dialog-information-symbolic")

        show_deb_hint = status["snap"] and not status["nautilus"]
        self._setup_deb_row.set_visible(show_deb_hint)
        # Nothing to press when nothing here is actually fixable.
        self._setup_button.set_visible(status["anything_fixable"])
        needs_attention = not status["complete"] or missing_interface
        # Count what is actually outstanding, not rows that only inform.
        todo = sum(1 for key in self._setup_rows
                   if not status[key] and fixable_for[key])
        todo += sum(1 for row in self._snap_interface_rows.values()
                    if row.get_visible())
        todo += 1 if (not status["shortcut"]
                      and status["shortcut_fixable"]) else 0
        self.setup_expander.set_subtitle(
            f"{todo} optional step{'' if todo == 1 else 's'} left"
            if todo else "Notes about this install")
        self.setup_group.set_visible(
            needs_attention and not prefs.setup_dismissed())

    def _on_hide_setup_clicked(self, _button: Gtk.Button) -> None:
        prefs.set_setup_dismissed(True)
        self.setup_group.set_visible(False)
        self.toast("Setup tips hidden — find them again in the menu")

    def _show_setup_again(self) -> None:
        prefs.set_setup_dismissed(False)
        self.refresh_setup_status()

    def _on_finish_setup_clicked(self, _button: Gtk.Button) -> None:
        self._setup_button.set_sensitive(False)
        self._setup_button.set_label("Working…")

        async def _do() -> dict[str, Any]:
            await asyncio.to_thread(cli.run_install)
            return await asyncio.to_thread(cli.integration_status)

        def after(status: dict[str, Any] | None,
                  exc: BaseException | None) -> None:
            self._setup_button.set_sensitive(True)
            self._setup_button.set_label("Finish Setup")
            if exc is not None or status is None:
                self.toast(f"Setup couldn't finish: {exc}")
                return
            # Report what actually changed. Claiming success while
            # rows still show warnings is how this looked broken.
            if status["complete"]:
                self.toast("Setup finished")
            elif status["anything_fixable"]:
                self.toast("Setup partly finished — see remaining items")
            else:
                self.toast("Done — the rest needs the .deb build")
            self._apply_setup_status(status)

        self.app.asyncio_thread.run_coroutine(_do(), after)

    def _build_visibility_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        self.visibility_row = Adw.SwitchRow(
            title="Visible to nearby devices",
            subtitle=_esc(self.app.service.device_name))
        self.visibility_row.set_subtitle_lines(0)
        self.visibility_row.add_prefix(
            Gtk.Image.new_from_icon_name("network-wireless-symbolic"))
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
        self._ble_health_row.set_subtitle_lines(0)
        self._ble_health_row.add_prefix(
            Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        group.add(self._ble_health_row)
        return group

    def _build_devices_group(self) -> Adw.PreferencesGroup:
        self.devices_group = Adw.PreferencesGroup(
            title="Nearby devices",
            description="Click a device to send it files, or drop files "
                        "on it.")

        # Discovery has no "I've started looking and found nothing yet"
        # event (mdns.Browser only calls back on an actual change), so
        # the searching/empty distinction is time-based: assume we're
        # still warming up until either a peer shows up or a few seconds
        # pass with none -- see _on_discovery_settle_timeout.
        self._devices_settled = False
        self._searching_status = Adw.StatusPage(
            title="Looking for Nearby Devices…",
            description="This can take a few seconds.",
            css_classes=["compact"])
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
        spinner.set_size_request(32, 32)
        self._searching_status.set_child(spinner)
        self.devices_group.add(self._searching_status)

        self._empty_devices_status = Adw.StatusPage(
            icon_name="phone-symbolic",
            title="No Devices Nearby",
            description="On the other device, open Quick Share and keep "
                        "it on screen. Both devices need to be on the "
                        "same Wi-Fi network.",
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
        has_peers = bool(self._peers)
        self._searching_status.set_visible(
            not has_peers and not self._devices_settled)
        self._empty_devices_status.set_visible(
            not has_peers and self._devices_settled)

    def _build_transfers_group(self) -> Adw.PreferencesGroup:
        self.transfers_group = Adw.PreferencesGroup(title="Transfers")
        self._clear_button = Gtk.Button(
            label="Clear", valign=Gtk.Align.CENTER, css_classes=["flat"],
            tooltip_text="Remove finished transfers from this list",
            action_name="win.clear-transfers", visible=False)
        self.transfers_group.set_header_suffix(self._clear_button)
        self._empty_transfers_status = Adw.StatusPage(
            icon_name="folder-download-symbolic",
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
            self._ble_health_row.set_subtitle(_esc(
                f"{reason} — phones may need Quick Share open on their "
                "screen to see this device."))

    def _build_direct_mode_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="No shared Wi-Fi?")
        self.direct_expander = Adw.ExpanderRow(
            title="Direct mode",
            subtitle="Experimental. Turns this computer into a temporary "
                     "Wi-Fi hotspot; it leaves its current network while "
                     "this is on.",
            show_enable_switch=True, enable_expansion=False)
        self.direct_expander.set_subtitle_lines(0)
        self.direct_expander.add_prefix(
            Gtk.Image.new_from_icon_name("network-wireless-hotspot-symbolic"))
        self.direct_expander.connect(
            "notify::enable-expansion", self._on_direct_mode_toggled)
        group.add(self.direct_expander)

        self.hotspot_ssid_row = Adw.ActionRow(title="Network name")
        self.hotspot_password_row = Adw.ActionRow(title="Password")
        self.hotspot_status_row = Adw.ActionRow(
            title="Status", subtitle="Off", selectable=False)
        self.hotspot_ssid_row.add_suffix(_copy_row_button(
            lambda: self.app.hotspot.ssid, self._copy_to_clipboard))
        self.hotspot_password_row.add_suffix(_copy_row_button(
            lambda: self.app.hotspot.password, self._copy_to_clipboard))

        self.qr_area = QrCodeArea()
        qr_box = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=12,
                         margin_bottom=12)
        qr_box.append(self.qr_area)

        self.direct_expander.add_row(self.hotspot_status_row)
        self.direct_expander.add_row(self.hotspot_ssid_row)
        self.direct_expander.add_row(self.hotspot_password_row)
        self.direct_expander.add_row(qr_box)
        return group

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
            "You can quit any time from the menu (≡) or with Ctrl+Q.")
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
        self.set_visible(False)
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
        notification.set_default_action("app.present")
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
        self.visibility_row.set_subtitle(_esc(subtitle))

    def update_device_name(self, name: str) -> None:
        self.window_title.set_subtitle(name)
        self.update_visibility(self.visibility_row.get_active())

    def _on_visibility_switch_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        if self._updating_visibility_switch:
            return  # programmatic update from on_visibility_changed, not a click
        wanted = row.get_active()
        self.app.asyncio_thread.run_coroutine(
            self.app.service.set_visible(wanted))

    # ------------------------------------------------------------- peers

    def update_peers(self, peers: dict[str, Peer]) -> None:
        self._peers = peers
        for key in [k for k in self._peer_rows if k not in peers]:
            self.devices_group.remove(self._peer_rows.pop(key))
            self._peer_buttons.pop(key, None)
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

    def _update_peer_row(self, row: Adw.ActionRow, peer: Peer) -> None:
        row.set_title(_esc(peer.device_name))
        row.set_subtitle(_esc(
            f"{device_type_name(peer.device_type)} · {peer.host}"))

    def _add_peer_row(self, key: str, peer: Peer) -> Adw.ActionRow:
        row = Adw.ActionRow(activatable=True)
        # Device names and the "type · IP" subtitle are arbitrarily long;
        # without a line cap they push the button off the row at narrow
        # widths instead of ellipsizing.
        row.set_title_lines(1)
        row.set_subtitle_lines(1)
        self._update_peer_row(row, peer)
        row.add_prefix(
            Gtk.Image.new_from_icon_name(device_icon(peer.device_type)))
        button = Gtk.Button(valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda _b: self._on_peer_activated(key))
        row.add_suffix(button)
        row.set_activatable_widget(button)
        self._peer_buttons[key] = button
        self._sync_peer_button(button)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_peer_drop, key)
        drop.connect("enter", self._on_drop_enter, row)
        drop.connect("leave", self._on_drop_leave, row)
        row.add_controller(drop)

        self.devices_group.add(row)
        return row

    def _sync_peer_button(self, button: Gtk.Button) -> None:
        if self._staged:
            button.set_label("Send")
            button.add_css_class("suggested-action")
            button.set_tooltip_text(
                f"Send {describe_files(self._staged)} to this device")
        else:
            button.set_label("Send Files…")
            button.remove_css_class("suggested-action")
            button.set_tooltip_text("Choose files to send to this device")

    # ------------------------------------------------------- choosing files

    def _on_drop_enter(self, _target: Gtk.DropTarget, _x: float, _y: float,
                       widget: Gtk.Widget) -> Gdk.DragAction:
        widget.add_css_class("drop-highlight")
        return Gdk.DragAction.COPY

    def _on_drop_leave(self, _target: Gtk.DropTarget,
                       widget: Gtk.Widget) -> None:
        widget.remove_css_class("drop-highlight")

    def _on_window_drop(self, target: Gtk.DropTarget, value: Any,
                        _x: float, _y: float) -> bool:
        target.get_widget().get_child().remove_css_class("drop-highlight")
        self._stage_paths(_paths_from_drop(value))
        return True

    def _on_peer_drop(self, _target: Gtk.DropTarget, value: Any,
                      _x: float, _y: float, key: str) -> bool:
        row = self._peer_rows.get(key)
        if row is not None:
            row.remove_css_class("drop-highlight")
        files = self._sendable(_paths_from_drop(value))
        peer = self._peers.get(key)
        if files and peer is not None:
            self._start_send(peer, files)
        return True

    def _sendable(self, paths: list[Path]) -> list[Path]:
        """Drop what cannot be sent, telling the user why."""
        files, rejected = split_sendable(paths)
        if rejected:
            if any(p.is_dir() for p in rejected):
                self.toast("Folders can't be sent — compress the folder "
                           "into a .zip first")
            else:
                self.toast(f"Couldn't read {describe_files(rejected)}")
        return files

    def _stage_paths(self, paths: list[Path]) -> None:
        files = self._sendable(paths)
        if not files:
            return
        # With exactly one device in range there is nothing to choose.
        if len(self._peers) == 1 and not self._staged:
            self._start_send(next(iter(self._peers.values())), files)
            return
        self._set_staged(self._staged + [f for f in files
                                         if f not in self._staged])

    def _set_staged(self, files: list[Path]) -> None:
        self._staged = files
        if files:
            size = human_size(total_size(files))
            where = ("Choose a device below" if self._peers
                     else "Waiting for a device to appear")
            self.staged_banner.set_title(
                f"{count_files(len(files))} ready ({size}) — {where}")
        self.staged_banner.set_revealed(bool(files))
        for button in self._peer_buttons.values():
            self._sync_peer_button(button)

    def _choose_files(self, title: str,
                      on_files: Callable[[list[Path]], None]) -> None:
        dialog = Gtk.FileDialog(title=title, modal=True)

        def on_chosen(source: Gtk.FileDialog, result: Gio.AsyncResult,
                      _data=None) -> None:
            try:
                files = source.open_multiple_finish(result)
            except GLib.Error:
                return  # user cancelled
            paths = [Path(f.get_path()) for f in files if f.get_path()]
            if paths:
                on_files(paths)

        dialog.open_multiple(self, None, on_chosen)

    def _choose_files_to_stage(self) -> None:
        self._choose_files("Choose Files to Send", self._stage_paths)

    def _on_peer_activated(self, key: str) -> None:
        peer = self._peers.get(key)
        if peer is None:
            return  # peer vanished between click and callback
        if self._staged:
            files, self._staged = self._staged, []
            self._set_staged([])
            self._start_send(peer, files)
            return
        self._choose_files(
            f"Send Files to {peer.device_name}",
            lambda paths: self._send_chosen(key, paths))

    def _send_chosen(self, key: str, paths: list[Path]) -> None:
        peer = self._peers.get(key)
        files = self._sendable(paths)
        if not files:
            return
        if peer is None:
            # The device went away while the file chooser was open; keep
            # the selection rather than making the user pick it again.
            self._set_staged(files)
            self.toast("That device is no longer nearby")
            return
        self._start_send(peer, files)

    def _start_send(self, peer: Peer, files: list[Path]) -> None:
        key = (peer.device_name, "send")
        if key in self._active:
            self.toast(f"Already sending to {peer.device_name}")
            return
        row = self._new_active_row(
            key, f"Sending to {peer.device_name}", total_size(files),
            "document-send-symbolic", describe_files(files))
        row.set_waiting_text(f"{row.detail} · Connecting…")

        def after(_result, exc: BaseException | None) -> None:
            # Success/failure UI is normally driven by the service's
            # Events callbacks (on_complete/on_error), which fire from
            # inside OutboundConnection.run() before it re-raises. This
            # is a backstop for exceptions raised outside that path
            # (a refused connection, an unreadable file).
            if exc is not None and not row.finished:
                self._active.pop(key, None)
                row.mark_error(str(exc) or type(exc).__name__)
                self._retire_row(row)

        self.app.asyncio_thread.run_coroutine(
            self.app.service.send_files(peer, files), after)

    def on_pin(self, device: str, pin: str) -> None:
        """An outgoing connection finished its key exchange: show the PIN
        the other person is being asked to check."""
        row = self._active.get((device, "send"))
        if row is None:
            found = self._find_active_row(device)
            row = found[1] if found and found[0][1] == "send" else None
        if row is not None:
            row.set_waiting_text(
                f"{row.detail} · PIN {pin} · Waiting for {device} to accept")

    # --------------------------------------------------------- transfers

    def _new_active_row(self, key: tuple[str, str], title: str,
                        total: int, icon_name: str,
                        detail: str = "") -> _TransferRow:
        """Create (replacing any stale row already at `key`) the active
        row for a freshly-started session and register it."""
        self._empty_transfers_status.set_visible(False)
        old = self._active.pop(key, None)
        if old is not None:
            old.remove()

        def on_remove(row: _TransferRow) -> None:
            if row in self._completed:
                self._completed.remove(row)
            row.remove()
            self._update_empty_transfers_visibility()

        def on_cancel(row: _TransferRow) -> None:
            if self._active.get(key) is row:
                del self._active[key]
            self.app.progress.drop(key[0])
            loop = self.app.asyncio_thread.loop
            loop.call_soon_threadsafe(
                self.app.service.cancel_transfer, key[0], key[1])
            row.mark_cancelled()
            self._retire_row(row)

        row = _TransferRow(self.transfers_group, title, total, on_remove,
                           icon_name=icon_name, on_cancel=on_cancel,
                           detail=detail)
        self._active[key] = row
        return row

    def _retire_row(self, row: _TransferRow) -> None:
        """Move a finished row from _active into the capped _completed
        list (removing its widget once the cap is exceeded)."""
        self._completed.append(row)
        while len(self._completed) > 5:
            self._completed.pop(0).remove()
        self._update_empty_transfers_visibility()

    def _clear_finished(self) -> None:
        for row in self._completed:
            row.remove()
        self._completed.clear()
        self._update_empty_transfers_visibility()

    def _update_empty_transfers_visibility(self) -> None:
        self._empty_transfers_status.set_visible(
            not self._active and not self._completed)
        self._clear_button.set_visible(bool(self._completed))

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
        # Only for that placeholder name: a real name that matches no
        # row belongs to some other session (a text-only receive, say),
        # and must not be pinned on an unrelated send.
        sends = [(k, r) for k, r in self._active.items() if k[1] == "send"]
        if device == "Unknown device" and len(sends) == 1:
            return sends[0]
        return None

    def on_progress(self, device: str, done: int, total: int) -> None:
        found = self._find_active_row(device)
        if found is not None:
            found[1].update_progress(done, total)

    def on_transfer_complete(self, device: str, paths: list[Path]) -> None:
        found = self._find_active_row(device)
        received = True
        folder = None
        if found is not None:
            key, row = found
            del self._active[key]
            received = key[1] == "receive"
            folder = paths[0].parent if received and paths else None
            row.mark_complete(folder, len(paths))
            self._retire_row(row)
        elif not paths:
            return  # a text-only transfer: show_text already told the user
        summary = (f"Received {count_files(len(paths))} from {device}"
                   if received else
                   f"Sent {count_files(len(paths))} to {device}")
        toast = Adw.Toast.new(summary)
        if folder is not None:
            toast.set_button_label("Show")
            toast.connect("button-clicked", lambda _t: _open_folder(folder))
        self.toast_overlay.add_toast(toast)
        # The window is usually hidden or behind something else when a
        # phone finishes sending: say so where it will be seen.
        if not self.is_active():
            notification = Gio.Notification.new(summary)
            if folder is not None:
                notification.set_body(f"Saved to {folder}")
                notification.add_button_with_target(
                    "Show in Files", "app.open-folder",
                    GLib.Variant("s", str(folder)))
            notification.set_default_action("app.present")
            self.app.send_notification("transfer-finished", notification)

    def on_transfer_error(self, device: str, message: str) -> None:
        # The sender gave up (or the link dropped) while its request was
        # still waiting for an answer: an Accept would go nowhere.
        for req_id, (request, _set, dialog) in list(self._requests.items()):
            if request.device_name == device:
                self._finish_request(req_id, False)
                dialog.force_close()
        found = self._find_active_row(device)
        if found is not None:
            key, row = found
            del self._active[key]
            row.mark_error(message)
            self._retire_row(row)
        self.toast(f"Transfer with {device} failed: {message}")

    # ---------------------------------------------------- incoming request

    def show_transfer_request(self, request: TransferRequest,
                              set_result: Callable[[bool], None]) -> None:
        """Runs on the GTK thread (see app.py's Events wiring: the async
        core callback marshals here via idle()). `set_result(bool)` is
        the thread-safe setter from bridge_future_to_asyncio that resumes
        the awaiting coroutine on the asyncio thread."""
        req_id = str(next(self._request_ids))
        count = len(request.files)
        device = request.device_name
        if count:
            names = describe_files([Path(f.name) for f in request.files])
            body = (f"{names} ({human_size(request.total_size)})"
                    if count <= 2 else
                    f"{count_files(count)}, {human_size(request.total_size)}"
                    f" — {names}")
        else:
            body = "A piece of text"
        if request.text_preview and count:
            body += f"\nWith text: “{request.text_preview}”"
        elif request.text_preview:
            body = f"“{request.text_preview}”"

        dialog = Adw.AlertDialog.new(f"{device} Wants to Share", body)
        pin_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                          margin_top=6)
        pin_box.append(Gtk.Label(label=request.pin,
                                 css_classes=["pin-display"]))
        pin_box.append(Gtk.Label(
            label=f"Accept only if {device} shows the same PIN",
            wrap=True, justify=Gtk.Justification.CENTER,
            css_classes=["caption", "dim-label"]))
        dialog.set_extra_child(pin_box)
        dialog.add_response("decline", "Decline")
        dialog.add_response("accept", "Accept")
        dialog.set_response_appearance(
            "accept", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("accept")
        dialog.set_close_response("decline")
        self._requests[req_id] = (request, set_result, dialog)

        def on_response(source: Adw.AlertDialog, result: Gio.AsyncResult) -> None:
            self._finish_request(req_id, source.choose_finish(result) == "accept")

        # Someone is waiting on the other device: bring the window up so
        # the PIN is on screen. Where the compositor refuses to raise a
        # window on its own, the notification's buttons answer instead.
        self.present()
        dialog.choose(self, None, on_response)
        self._notify_incoming(req_id, request)

    def _finish_request(self, req_id: str, accepted: bool) -> None:
        entry = self._requests.pop(req_id, None)
        if entry is None:
            return  # already answered from the other place
        request, set_result, _dialog = entry
        self.app.withdraw_notification(f"transfer-request-{req_id}")
        if accepted and request.files:
            names = describe_files([Path(f.name) for f in request.files])
            row = self._new_active_row(
                (request.device_name, "receive"),
                f"Receiving from {request.device_name}",
                request.total_size, "folder-download-symbolic", names)
            row.set_waiting_text(f"{names} · Starting…")
        set_result(accepted)

    def answer_request(self, req_id: str, accepted: bool) -> None:
        """Accept/Decline pressed on the notification rather than in the
        dialog: resolve the request and take the dialog down."""
        entry = self._requests.get(req_id)
        if entry is None:
            return
        self._finish_request(req_id, accepted)
        entry[2].force_close()

    def _notify_incoming(self, req_id: str, request: TransferRequest) -> None:
        """Desktop notification so the request can be seen and answered
        even if the window is hidden or behind another one."""
        count = len(request.files)
        what = count_files(count) if count else "text"
        notification = Gio.Notification.new(
            f"{request.device_name} wants to share {what}")
        notification.set_body(f"PIN {request.pin} — accept only if it "
                              "matches the other device")
        notification.set_priority(Gio.NotificationPriority.URGENT)
        notification.set_default_action("app.present")
        notification.add_button_with_target(
            "Accept", "app.respond-transfer",
            GLib.Variant("s", f"accept:{req_id}"))
        notification.add_button_with_target(
            "Decline", "app.respond-transfer",
            GLib.Variant("s", f"decline:{req_id}"))
        self.app.send_notification(f"transfer-request-{req_id}", notification)

    # ------------------------------------------------------------- text

    def show_text(self, device: str, text: str) -> None:
        dialog = Adw.AlertDialog.new(f"Text from {device}", "")
        buffer = Gtk.TextBuffer(text=text)
        text_view = Gtk.TextView(
            buffer=buffer, editable=False, cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8,
            left_margin=8, right_margin=8,
            css_classes=["card"], margin_top=6, margin_bottom=6)
        scroller = Gtk.ScrolledWindow(
            child=text_view, min_content_height=120, max_content_height=300)
        dialog.set_extra_child(scroller)
        dialog.add_response("close", "Close")
        link = text.strip()
        is_link = (link.startswith(("http://", "https://"))
                   and not any(c.isspace() for c in link))
        if is_link:
            dialog.add_response("open", "Open Link")
        dialog.add_response("copy", "Copy")
        dialog.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("copy")
        dialog.set_close_response("close")

        def on_response(source: Adw.AlertDialog, result: Gio.AsyncResult) -> None:
            response = source.choose_finish(result)
            if response == "copy":
                self._copy_to_clipboard(text)
            elif response == "open":
                Gtk.UriLauncher.new(link).launch(self, None, None)

        self.present()
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
            self.hotspot_status_row.set_subtitle(
                "On — join this network from the other device")
            self.hotspot_ssid_row.set_subtitle(_esc(ssid))
            self.hotspot_password_row.set_subtitle(_esc(password))
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
        if os.environ.get("SNAP"):
            message += ("\n\nIn the snap, Direct mode needs this once:\n"
                        "sudo snap connect nearshare:network-manager")
        dialog = Adw.AlertDialog.new("Direct Mode Couldn't Start", message)
        dialog.add_response("ok", "OK")
        dialog.set_close_response("ok")
        dialog.choose(self, None, lambda *_: None)

    # -------------------------------------------------------------- misc

    def toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast.new(message))


class PreferencesDialog(Adw.PreferencesDialog):
    """Device name, download folder and start-at-login."""

    def __init__(self, app: "NearShareApplication") -> None:
        super().__init__(title="Preferences", content_width=440,
                         content_height=420)
        self.app = app
        page = Adw.PreferencesPage()
        self.add(page)

        device = Adw.PreferencesGroup(
            title="This device",
            description="The name other devices see when this computer "
                        "is visible.")
        self.name_row = Adw.EntryRow(title="Device name",
                                     show_apply_button=True)
        self.name_row.set_text(app.service.device_name)
        self.name_row.connect("apply", self._on_name_applied)
        reset = Gtk.Button(icon_name="edit-undo-symbolic",
                           valign=Gtk.Align.CENTER, css_classes=["flat"],
                           tooltip_text="Use the default name")
        reset.connect("clicked", self._on_name_reset)
        self.name_row.add_suffix(reset)
        device.add(self.name_row)
        page.add(device)

        receiving = Adw.PreferencesGroup(title="Receiving")
        self.folder_row = Adw.ActionRow(
            title="Save received files to", activatable=True)
        self.folder_row.set_subtitle_lines(1)
        self.folder_row.add_suffix(
            Gtk.Image.new_from_icon_name("folder-open-symbolic"))
        self.folder_row.connect("activated", self._on_choose_folder)
        self._show_folder()
        receiving.add(self.folder_row)
        page.add(receiving)

        if autostart.supported():
            startup = Adw.PreferencesGroup(title="Startup")
            self.autostart_row = Adw.SwitchRow(
                title="Start at login",
                subtitle="Starts hidden, so this computer is ready to "
                         "receive without opening NearShare first")
            self.autostart_row.set_subtitle_lines(0)
            self.autostart_row.set_active(autostart.enabled())
            self.autostart_row.connect(
                "notify::active", self._on_autostart_toggled)
            startup.add(self.autostart_row)
            page.add(startup)

    def _on_name_applied(self, row: Adw.EntryRow) -> None:
        name = row.get_text().strip()
        if not name:
            self._on_name_reset(None)
            return
        prefs.set_device_name(name)
        self.app.rename_device(name)
        self.add_toast(Adw.Toast.new(f"Now visible as {name}"))

    def _on_name_reset(self, _button: Gtk.Button | None) -> None:
        name = default_device_name()
        prefs.set_device_name(None)
        self.name_row.set_text(name)
        self.app.rename_device(name)

    def _show_folder(self) -> None:
        folder = str(self.app.service.download_dir)
        home = str(Path.home())
        if folder == home or folder.startswith(home + os.sep):
            folder = "~" + folder[len(home):]
        self.folder_row.set_subtitle(_esc(folder))

    def _on_choose_folder(self, _row: Adw.ActionRow) -> None:
        dialog = Gtk.FileDialog(title="Save Received Files To", modal=True)
        current = self.app.service.download_dir
        if current.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(current)))

        def on_chosen(source: Gtk.FileDialog, result: Gio.AsyncResult,
                      _data=None) -> None:
            try:
                folder = source.select_folder_finish(result)
            except GLib.Error:
                return  # cancelled
            path = folder.get_path() if folder else None
            if not path:
                return
            if not os.access(path, os.W_OK):
                self.add_toast(Adw.Toast.new("NearShare can't write to "
                                             "that folder"))
                return
            prefs.set_download_dir(Path(path))
            self.app.service.download_dir = Path(path)
            self._show_folder()

        dialog.select_folder(self.app.window, None, on_chosen)

    def _on_autostart_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        try:
            autostart.set_enabled(row.get_active())
        except OSError as exc:
            self.add_toast(Adw.Toast.new(f"Couldn't change that: {exc}"))


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
        self._start_hidden = False
        # True from launch until the service has finished starting.
        self.starting_visible = True
        # Progress arrives once per 512 KB chunk on the asyncio thread;
        # the window only needs the latest figure a few times a second.
        self.progress = Coalescer(self._deliver_progress)

        self.add_main_option(
            "background", ord("b"), GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Start without showing the window (used by Start at login)",
            None)
        self.add_main_option(
            "version", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Print the version and exit", None)

        for name, param, handler in (
            ("quit", None, lambda *_: self.quit()),
            ("present", None, lambda *_: self.activate()),
            ("preferences", None, lambda *_: self._show_preferences()),
            ("about", None, lambda *_: self._show_about()),
            ("respond-transfer", "s", self._on_respond_transfer),
            ("open-folder", "s",
             lambda _a, target: _open_folder(Path(target.get_string()))),
        ):
            action = Gio.SimpleAction.new(
                name, GLib.VariantType.new(param) if param else None)
            action.connect("activate", handler)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<primary>q"])
        self.set_accels_for_action("app.preferences", ["<primary>comma"])
        self.set_accels_for_action("win.send-files", ["<primary>o"])

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        if options.contains("version"):
            print(f"NearShare {__version__}")
            return 0
        if options.contains("background"):
            # Asked to start hidden. If NearShare is already running that
            # request is already satisfied: exit without activating, which
            # would otherwise raise the running instance's window.
            try:
                self.register(None)
            except GLib.Error as exc:
                log.debug("could not register on the session bus: %s", exc)
            else:
                if self.get_is_remote():
                    return 0
            self._start_hidden = True
        return -1  # carry on with normal startup

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self.asyncio_thread.start()

        events = Events(
            on_transfer_request=self._on_transfer_request,
            on_progress=lambda dev, done, total: self.progress.push(
                dev, (done, total)),
            on_complete=lambda dev, paths: (
                self.progress.drop(dev),
                idle(self._with_window(
                    lambda w: w.on_transfer_complete(dev, paths)))),
            on_error=lambda dev, err: (
                self.progress.drop(dev),
                idle(self._with_window(
                    lambda w: w.on_transfer_error(dev, err)))),
            on_text=lambda dev, text: idle(
                self._with_window(lambda w: w.show_text(dev, text))),
            on_pin=lambda dev, pin: idle(
                self._with_window(lambda w: w.on_pin(dev, pin))),
        )
        self.service = NearShareService(
            device_name=prefs.device_name(),
            download_dir=prefs.download_dir(),
            events=events)
        self.service.on_visibility_changed = lambda visible: idle(
            self._with_window(lambda w: w.update_visibility(visible)))
        self.service.on_peers_changed = lambda peers: idle(
            self._with_window(lambda w: w.update_peers(peers)))

        def after_start(_result, exc: BaseException | None) -> None:
            self.starting_visible = False
            if exc is not None:
                log.error("service failed to start: %s", exc)
                idle(self._with_window(lambda w: (
                    w.update_visibility(False),
                    w.toast(f"NearShare couldn't start: {exc}"))))
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
        if self.window is None:
            _install_css()
            self.window = NearShareWindow(self)
        if self._start_hidden:
            # Started at login: the window exists, so an incoming
            # transfer can raise it, but stays out of the way until then.
            self._start_hidden = False
            return
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

    # ------------------------------------------------------------ actions

    def rename_device(self, name: str) -> None:
        def after(_result, exc: BaseException | None) -> None:
            if self.window is None:
                return
            if exc is not None:
                self.window.toast(f"Couldn't rename this device: {exc}")
            else:
                self.window.update_device_name(name)

        self.asyncio_thread.run_coroutine(
            self.service.set_device_name(name), after)

    def _show_preferences(self) -> None:
        self.activate()
        PreferencesDialog(self).present(self.window)

    def _show_about(self) -> None:
        self.activate()
        if os.environ.get("SNAP"):
            build = f"snap (revision {os.environ.get('SNAP_REVISION', '?')})"
        elif Path("/.flatpak-info").exists():
            build = "Flatpak"
        else:
            build = cli._install_layout()
        about = Adw.AboutDialog(
            application_name="NearShare",
            application_icon=APP_ID,
            version=__version__,
            developer_name="Dhiva Labs",
            comments="Share files with nearby Android, Windows, macOS and "
                     "Linux devices over Quick Share.",
            website=WEBSITE,
            issue_url=f"{WEBSITE}/issues",
            license_type=Gtk.License.MIT_X11,
            copyright="© 2026 Dhiva Labs",
            debug_info=(
                f"NearShare {__version__} ({build})\n"
                f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}."
                f"{Gtk.get_micro_version()}, libadwaita "
                f"{Adw.get_major_version()}.{Adw.get_minor_version()}."
                f"{Adw.get_micro_version()}\n"
                f"Python {sys.version.split()[0]}\n"
                f"Bluetooth: {self.service.ble_status}\n"),
            debug_info_filename="nearshare-debug.txt")
        about.add_link("Guide", "https://learn.dhivalabs.com/apps/nearshare/")
        about.add_legal_section(
            "Not affiliated with Google or Samsung", None,
            Gtk.License.CUSTOM,
            "NearShare is an independent implementation of the Quick Share "
            "protocol. Quick Share and Nearby Share are trademarks of their "
            "respective owners.")
        about.present(self.window)

    def _on_respond_transfer(self, _action: Gio.SimpleAction,
                             target: GLib.Variant) -> None:
        verb, _, req_id = target.get_string().partition(":")
        if self.window is not None:
            self.window.answer_request(req_id, verb == "accept")

    # ---------------------------------------------------- event dispatch

    def _deliver_progress(self, device: str, value: tuple[int, int]) -> None:
        if self.window is not None:
            self.window.on_progress(device, *value)

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
            lambda w: w.show_transfer_request(request, set_result)))
        if self.window is None:
            # No window yet (the service started before activation):
            # there's no dialog to drive a decision, so decline rather
            # than hang the sender forever waiting on a UI that doesn't
            # exist.
            set_result(False)
        return await future


def main() -> int:
    app = NearShareApplication()
    return app.run(sys.argv)
