"""The application id and the small stylesheet shared by the main window
and the send picker. Kept apart from app.py so the picker, which must
open fast from a right-click, does not import the whole main window."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

APP_ID = "dev.dhivalabs.nearshare"

_CSS = b"""
progressbar.transfer-progress > trough,
progressbar.transfer-progress > trough > progress {
  min-height: 3px;
  border-radius: 0;
}
.pin-display {
  font-size: 2.4em;
  font-weight: 800;
  letter-spacing: 0.25em;
  font-feature-settings: "tnum";
}
.drop-highlight {
  background-color: alpha(@accent_bg_color, 0.12);
}
"""


def install_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
