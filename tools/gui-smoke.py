#!/usr/bin/env python3
"""Start the real GTK application headlessly for a few seconds, then quit.

CI runs this under xvfb (see .github/workflows/ci.yml) against the installed
.deb and the installed snap, to prove the GTK4/libadwaita stack loads and the
main window is created on every architecture. It does not need the nearshare
package next to it: point PYTHONPATH at the installed copy.

Exit status is 0 only if the window was created, the receive service was
listening, and the application quit cleanly after RUN_SECONDS.
"""

from __future__ import annotations

import sys

# Importing the entry module applies its PyGObject sys.path fix-up, exactly as
# `python -m nearshare` does.
import nearshare.__main__  # noqa: F401  (imported for its side effect)
from gi.repository import Gdk, GLib

from nearshare.ui.app import NearShareApplication

RUN_SECONDS = 8


def main() -> int:
    app = NearShareApplication()
    seen: dict[str, bool] = {}

    def finish() -> bool:
        window = app.window
        seen["window"] = window is not None
        seen["visible"] = bool(window and window.get_visible())
        service = app.service
        seen["listening"] = bool(service and service.port)
        display = Gdk.Display.get_default()
        print(f"display : {display.get_name() if display else None}")
        print(f"window  : created={seen['window']} visible={seen['visible']}")
        print(f"service : port={service.port if service else None} "
              f"ble={service.ble_status if service else None}")
        app.quit()
        return GLib.SOURCE_REMOVE

    GLib.timeout_add_seconds(RUN_SECONDS, finish)
    status = app.run([sys.argv[0]])
    print(f"exit    : {status}")
    if not (seen.get("window") and seen.get("visible")):
        print("FAIL: the main window was not created", file=sys.stderr)
        return 1
    if not seen.get("listening"):
        print("FAIL: the receive service never started listening",
              file=sys.stderr)
        return 1
    return status


if __name__ == "__main__":
    sys.exit(main())
