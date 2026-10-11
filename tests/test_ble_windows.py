"""Windows BLE wrapper against a fake helper.

nearshare.core.ble_windows drives a native helper (nearshare-ble.exe) over
a JSON-lines protocol. This test stands a tiny Python script in for that
exe (selected through NEARSHARE_BLE_HELPER), so the wrapper's protocol
handling - start/stop advertising, error replies, an unavailable helper,
scan events, process cleanup - is checked on any OS, Linux included.

Run:  .venv/bin/python -m tests.test_ble_windows
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from nearshare.core import ble_windows
from nearshare.core.ble_windows import BleAdvertiser, BleScanner, BleUnavailable

TMP = Path(tempfile.mkdtemp(prefix="ble-win-test-"))
LOG = TMP / "helper.log"
HELPER = TMP / "fake-nearshare-ble.py"

BEACON_HEX = "fc128e0142000000000000000000bf2d5ba0e1d87524ca00"  # 24 bytes
APPLE_HEX = "0512400000000000000000030000000000000000"

# Behaviour is chosen with FAKE_BLE_MODE:
#   normal           ready, then Waiting -> Started (after a delay) per advert
#   unavailable      "unavailable" event, exit code 2 (no adapter)
#   crash            exit code 3 without saying anything
#   silent           ready, then never answers advertise
#   nostop           like normal, but never answers stop
#   die_after_start  Started, then the process vanishes
# A service-data UUID of "dead" gets an error reply, "bad0" an Aborted status.
FAKE_HELPER = r'''
import json, os, sys, threading, time

mode = os.environ.get("FAKE_BLE_MODE", "normal")
log_path = os.environ["FAKE_BLE_LOG"]
out_lock = threading.Lock()


def log(*parts):
    with open(log_path, "a") as f:
        f.write(" ".join(str(p) for p in parts) + "\n")


def emit(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


log("start", os.getpid(), mode)
if mode == "unavailable":
    emit({"event": "unavailable", "message": "no Bluetooth adapter found"})
    sys.exit(2)
if mode == "crash":
    sys.exit(3)

# Noise the wrapper must shrug off: stderr (merged into stdout), garbage,
# valid JSON that isn't an event object, and an event nobody knows.
sys.stderr.write("fake helper stderr noise\n")
sys.stderr.flush()
with out_lock:
    sys.stdout.write("this is not json\n[1, 2]\n\n")
    sys.stdout.flush()
emit({"event": "mystery"})
emit({"event": "ready", "peripheral": True, "central": True})


def seen_burst():
    for uuid in ("fe2c", "fe2d", "fe2c", "fe2c"):
        time.sleep(0.05)
        emit({"event": "seen", "uuid": uuid, "rssi": -60})


for line in sys.stdin:
    cmd = json.loads(line)
    log("cmd", json.dumps(cmd, sort_keys=True))
    kind = cmd["cmd"]
    if kind == "advertise":
        ident = cmd["id"]
        uuids = cmd.get("service_data", {})
        if "dead" in uuids:
            emit({"event": "error", "id": ident,
                  "message": "0x80070057: the parameter is incorrect"})
            continue
        if "bad0" in uuids:
            emit({"event": "advertise", "id": ident,
                  "status": "Aborted", "error": "OtherError"})
            continue
        emit({"event": "advertise", "id": ident,
              "status": "Waiting", "error": ""})
        if mode == "silent":
            continue
        time.sleep(0.3)
        emit({"event": "advertise", "id": ident,
              "status": "Started", "error": ""})
        if mode == "die_after_start":
            os._exit(1)
    elif kind == "stop":
        if mode != "nostop":
            emit({"event": "advertise", "id": cmd["id"],
                  "status": "Stopped", "error": ""})
    elif kind == "scan":
        emit({"event": "scan", "status": "Started", "error": ""})
        threading.Thread(target=seen_burst, daemon=True).start()
    elif kind == "scan_stop":
        emit({"event": "scan", "status": "Stopped", "error": ""})
log("eof")
'''


# --------------------------------------------------------------- helpers --


@contextmanager
def fake(mode: str = "normal"):
    """Point the wrapper at the fake helper in the given mode, with a
    fresh log. The wrapper must have no helper left running afterwards."""
    LOG.unlink(missing_ok=True)
    os.environ["NEARSHARE_BLE_HELPER"] = str(HELPER)
    os.environ["FAKE_BLE_LOG"] = str(LOG)
    os.environ["FAKE_BLE_MODE"] = mode
    try:
        yield
    except BaseException:
        ble_windows._manager.shutdown()   # don't mask the real failure
        raise
    assert ble_windows._manager._session is None, "helper left running"


def log_lines() -> list[list[str]]:
    if not LOG.exists():
        return []
    return [line.split(" ", 1) for line in LOG.read_text().splitlines()]


def commands(kind: str | None = None) -> list[dict]:
    cmds = [json.loads(parts[1]) for parts in log_lines()
            if parts[0] == "cmd"]
    return [c for c in cmds if kind is None or c["cmd"] == kind]


def helper_starts() -> int:
    return sum(1 for parts in log_lines() if parts[0] == "start")


def helper_saw_eof() -> bool:
    return any(parts[0] == "eof" for parts in log_lines())


def current_proc():
    return ble_windows._manager._session.proc


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def expect_unavailable(callable_, *needles: str) -> BleUnavailable:
    try:
        callable_()
    except BleUnavailable as exc:
        for needle in needles:
            assert needle in str(exc), (needle, str(exc))
        return exc
    raise AssertionError("BleUnavailable not raised")


# ----------------------------------------------------------------- tests --


def test_constants_mirror_linux() -> None:
    from nearshare.core import ble
    assert ble_windows.SERVICE_UUID == ble.SERVICE_UUID
    assert ble_windows.SERVICE_DATA == ble.SERVICE_DATA
    assert ble_windows.SERVICE_DATA.hex() == BEACON_HEX
    print("constants: SERVICE_UUID/SERVICE_DATA mirror ble.py")


def test_default_beacon() -> None:
    with fake():
        adv = BleAdvertiser()
        assert not adv.active
        t0 = time.monotonic()
        adv.start()
        elapsed = time.monotonic() - t0
        # The fake delays Started by 0.3s: start() must wait for it.
        assert elapsed >= 0.25, f"start() returned before Started ({elapsed:.2f}s)"
        assert adv.active
        proc = current_proc()

        (cmd,) = commands("advertise")
        assert cmd["service_data"] == {"fe2c": BEACON_HEX}, cmd
        assert "manufacturer_data" not in cmd, cmd

        adv.start()  # idempotent
        assert len(commands("advertise")) == 1

        adv.stop()
        assert not adv.active
        (stop,) = commands("stop")
        assert stop["id"] == cmd["id"], (stop, cmd)
        assert proc.poll() is not None, "helper still running after stop()"
        assert helper_saw_eof()
        adv.stop()  # idempotent
        assert len(commands("stop")) == 1

        adv.start()  # restartable: fresh helper process
        assert adv.active and helper_starts() == 2
        adv.stop()
    print(f"default beacon: start waited for Started ({elapsed:.2f}s), "
          "stop cleaned up the helper, restart works")


def test_manufacturer_data() -> None:
    with fake():
        adv = BleAdvertiser(
            manufacturer_data={0x004C: bytes.fromhex(APPLE_HEX)},
            name="airdrop0", label="AirDrop wake advertising",
            connectable=True, interval_ms=100)
        adv.start()
        assert adv.active
        (cmd,) = commands("advertise")
        assert cmd["manufacturer_data"] == {"76": APPLE_HEX}, cmd
        assert "service_data" not in cmd, cmd
        assert cmd["id"].startswith("airdrop0"), cmd
        adv.stop()

        both = BleAdvertiser(
            service_data={"0000fd6f-0000-1000-8000-00805f9b34fb": b"\x01\x02",
                          "FE2D": b""},
            manufacturer_data={0xFFFF: b"\xaa"})
        both.start()
        cmd = commands("advertise")[-1]
        assert cmd["service_data"] == {"fd6f": "0102", "fe2d": ""}, cmd
        assert cmd["manufacturer_data"] == {"65535": "aa"}, cmd
        both.stop()
    print("manufacturer data: company id sent as decimal, UUIDs shortened")


def test_error_replies() -> None:
    with fake():
        adv = BleAdvertiser(service_data={"dead": b"\x01"})
        expect_unavailable(adv.start, "parameter is incorrect")
        assert not adv.active
    with fake():
        adv = BleAdvertiser(service_data={"bad0": b"\x01" * 40})
        expect_unavailable(adv.start, "aborted", "OtherError")
        assert not adv.active
        # The aborted advertisement is dropped in the helper too.
        assert [c["id"] for c in commands("stop")] == \
            [c["id"] for c in commands("advertise")]
    with fake():
        # A UUID the helper can't carry never even starts a helper.
        adv = BleAdvertiser(
            service_data={"12345678-1234-1234-1234-123456789abc": b""})
        expect_unavailable(adv.start, "16-bit")
        assert helper_starts() == 0
    print("errors: error reply and Aborted raise BleUnavailable, "
          "helper cleaned up")


def test_helper_unavailable() -> None:
    with fake("unavailable"):
        adv = BleAdvertiser()
        expect_unavailable(adv.start, "no Bluetooth adapter found")
        assert not adv.active
        scanner = BleScanner(on_activity=lambda: None)
        expect_unavailable(scanner.start, "no Bluetooth adapter found")
        assert not scanner.active
        assert helper_starts() == 2   # each attempt retries a fresh helper
    with fake("crash"):
        expect_unavailable(BleAdvertiser().start, "code 3")
    with fake():
        # ...and a later attempt works once the adapter is back.
        adv = BleAdvertiser()
        adv.start()
        assert adv.active
        adv.stop()

    saved = os.environ["NEARSHARE_BLE_HELPER"]
    try:
        os.environ["NEARSHARE_BLE_HELPER"] = str(TMP / "nope.exe")
        expect_unavailable(BleAdvertiser().start, "not found")
        del os.environ["NEARSHARE_BLE_HELPER"]
        built = Path(ble_windows.__file__).resolve().parents[2] / \
            "windows" / "ble" / "nearshare-ble.exe"
        if not built.exists() and \
                not (Path(sys.executable).parent / "nearshare-ble.exe").exists():
            expect_unavailable(BleScanner(lambda: None).start,
                               "nearshare-ble.exe", "build.cmd")
    finally:
        os.environ["NEARSHARE_BLE_HELPER"] = saved
    print("unavailable: adapter missing, helper crash and missing exe all "
          "raise BleUnavailable")


def test_scan_events() -> None:
    with fake():
        hits: list[int] = []
        lock = threading.Lock()

        def on_activity() -> None:
            with lock:
                hits.append(1)

        def bad_callback() -> None:
            raise RuntimeError("callback blew up")

        scanner = BleScanner(on_activity=on_activity)
        assert not scanner.active
        scanner.start()
        assert scanner.active
        proc = current_proc()
        (scan,) = commands("scan")
        assert scan["uuid"] == "fe2c", scan

        # Three fe2c sightings and one fe2d: only fe2c counts.
        assert wait_until(lambda: len(hits) >= 3)
        time.sleep(0.3)
        with lock:
            assert len(hits) == 3, hits

        # A second scanner shares the helper and its scan; one that
        # raises must not break the reader or the other scanner.
        grumpy = BleScanner(on_activity=bad_callback)
        grumpy.start()
        assert grumpy.active
        assert len(commands("scan")) == 1, "second scanner re-sent scan"
        assert helper_starts() == 1

        grumpy.stop()
        assert not grumpy.active
        assert scanner.active and not commands("scan_stop")
        assert proc.poll() is None, "helper stopped while a scanner is left"

        scanner.stop()
        assert not scanner.active
        assert len(commands("scan_stop")) == 1
        assert proc.poll() is not None and helper_saw_eof()
        scanner.stop()  # idempotent
    print("scan: fe2c sightings call on_activity (fe2d ignored), "
          "scanners share one scan, stop cleans up")


def test_shared_helper() -> None:
    with fake():
        a = BleAdvertiser(name="a")
        b = BleAdvertiser(manufacturer_data={76: b"\x05\x00"}, name="b")
        scanner = BleScanner(on_activity=lambda: None)
        a.start()
        b.start()
        scanner.start()
        assert helper_starts() == 1, "advertisers/scanner must share a helper"
        proc = current_proc()
        ids = [c["id"] for c in commands("advertise")]
        assert len(set(ids)) == 2, ids

        a.stop()
        scanner.stop()
        assert b.active and proc.poll() is None, "helper must stay for b"
        b.stop()
        assert proc.poll() is not None and not a.active and not b.active
    print("shared helper: one process for everyone, closed with the last user")


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_timeouts() -> None:
    start_t, stop_t = ble_windows._START_TIMEOUT, ble_windows._STOP_TIMEOUT
    records = _Records()
    logging.getLogger("nearshare.ble").addHandler(records)
    try:
        ble_windows._START_TIMEOUT = 0.6
        with fake("silent"):
            adv = BleAdvertiser()
            t0 = time.monotonic()
            expect_unavailable(adv.start, "timed out")
            assert time.monotonic() - t0 < 3
            assert not adv.active
            assert len(commands("stop")) == 1, "timed-out advert not stopped"

        ble_windows._START_TIMEOUT = start_t
        ble_windows._STOP_TIMEOUT = 0.5
        with fake("nostop"):
            adv = BleAdvertiser()
            adv.start()
            proc = current_proc()
            t0 = time.monotonic()
            adv.stop()  # the helper never confirms: gives up, still cleans up
            assert 0.4 <= time.monotonic() - t0 < 3
            assert not adv.active
            assert proc.poll() is not None
            assert any("no stop confirmation" in m for m in records.messages)
    finally:
        logging.getLogger("nearshare.ble").removeHandler(records)
        ble_windows._START_TIMEOUT, ble_windows._STOP_TIMEOUT = start_t, stop_t
    print("timeouts: silent helper -> BleUnavailable, missing stop reply "
          "-> stop() gives up and cleans up")


def test_helper_dies() -> None:
    with fake("die_after_start"):
        adv = BleAdvertiser()
        adv.start()
        # The helper vanishes right after Started: the advert is gone.
        assert wait_until(lambda: not adv.active), "active stayed True"
        adv.stop()  # must not raise or hang
        assert ble_windows._manager._session is None
    with fake():
        adv.start()  # and it comes back with a fresh helper
        assert adv.active
        adv.stop()
    print("helper death: active goes False, stop() copes, start() recovers")


def main() -> int:
    HELPER.write_text(FAKE_HELPER)
    saved = {k: os.environ.get(k) for k in
             ("NEARSHARE_BLE_HELPER", "FAKE_BLE_LOG", "FAKE_BLE_MODE")}
    try:
        test_constants_mirror_linux()
        test_default_beacon()
        test_manufacturer_data()
        test_error_replies()
        test_helper_unavailable()
        test_scan_events()
        test_shared_helper()
        test_timeouts()
        test_helper_dies()
    finally:
        ble_windows._manager.shutdown()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(TMP, ignore_errors=True)
    print("\nBLE WINDOWS WRAPPER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
