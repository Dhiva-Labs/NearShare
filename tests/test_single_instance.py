"""Regression test: a second instance must never start a second service.

A real phone listed this machine four times because four independent
instances were each advertising over mDNS. The cause was
_start_control_socket unconditionally unlinking the running instance's
socket and taking over, so every launch became a full second service
(own mDNS advertisement, own TCP listener, own BLE beacon).

Run:  .venv/bin/python -m tests.test_single_instance
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from nearshare.core import control
from nearshare.core.paths import IS_WINDOWS
from nearshare.core.service import AlreadyRunning, NearShareService

from tests import _control


async def main() -> int:
    os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="ns-run-")
    downloads = Path(tempfile.mkdtemp(prefix="ns-dl-"))

    first = NearShareService(device_name="first", download_dir=downloads)
    await first.start(visible=False)
    print(f"first instance started on port {first.port}")

    # --- a second instance must refuse ---------------------------------
    second = NearShareService(device_name="second", download_dir=downloads)
    try:
        await second.start(visible=False)
    except AlreadyRunning as exc:
        print(f"second instance correctly refused: {exc}")
    else:
        await second.stop()
        print("FAIL: second instance started alongside the first")
        return 1

    # The first instance must be untouched and still serving.
    marker = control._info_path() if IS_WINDOWS else control.control_socket_path()
    assert marker.exists(), "first instance's control endpoint was removed"
    status = await _control.request({"cmd": "status"}, timeout=5)
    assert status.get("device_name") == "first", status
    print("first instance still serving its control socket")
    await first.stop()

    # --- a stale socket must be reclaimed, not treated as live ---------
    marker.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:   # a control.json naming a port nobody listens on
        marker.write_text('{"port": 9, "token": "stale", "pid": 1}')
    else:
        marker.touch()  # looks like a socket file, nothing listening
    third = NearShareService(device_name="third", download_dir=downloads)
    try:
        await third.start(visible=False)
    except AlreadyRunning:
        print("FAIL: stale socket wrongly treated as a live instance")
        return 1
    print("stale socket correctly reclaimed")
    await third.stop()

    print("\nSINGLE-INSTANCE REGRESSION TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
