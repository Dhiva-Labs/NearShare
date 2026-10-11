"""Windows BLE advertising and scanning, behind the same API as ble.py.

ble.py talks to BlueZ over D-Bus, which Windows doesn't have; the
Windows Bluetooth LE API is WinRT, and the Python this app runs on there
(MSYS2 UCRT64) has no WinRT bindings. So the real work happens in a small
native helper, windows/ble/nearshare-ble.exe (C++/WinRT, built with
windows/ble/build.cmd), and this module is its Python side: it spawns the
helper, speaks a line-oriented JSON protocol over its stdin/stdout and
exposes BleAdvertiser / BleScanner with the exact signatures of the Linux
classes, so callers don't care which platform they're on.

One helper process is shared by everything in this Python process. It's
started lazily on first use, a reader thread turns its output lines into
events, and it's shut down (stdin closed; the helper stops every
advertisement itself on EOF) when the last advertiser or scanner stops.

Everything degrades gracefully, as on Linux: no helper exe, no adapter,
no LE support, or Windows refusing an advertisement is reported as
BleUnavailable and mDNS-only operation continues.

Windows caveats: advertisements are always non-connectable (the WinRT
publisher can't do otherwise) and the advertising interval isn't
adjustable, so BleAdvertiser's `connectable` and `interval_ms` are
accepted for API compatibility and ignored.
"""

from __future__ import annotations

import atexit
import itertools
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("nearshare.ble")

# These two mirror SERVICE_UUID / SERVICE_DATA in nearshare/core/ble.py.
# They're duplicated rather than imported because ble.py imports this
# module on Windows (a circular import otherwise). Keep them in sync.
SERVICE_UUID = "0000fe2c-0000-1000-8000-00805f9b34fb"
SERVICE_DATA = bytes([
    0xFC, 0x12, 0x8E, 0x01, 0x42, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0xBF, 0x2D, 0x5B, 0xA0, 0xE1, 0xD8, 0x75, 0x24,
    0xCA, 0x00,
])

_QUICK_SHARE_UUID16 = "fe2c"
_BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"

_HELPER_NAME = "nearshare-ble.exe"
_HELPER_ENV = "NEARSHARE_BLE_HELPER"

# Blocking limits, in seconds (module-level so tests can shorten them).
_START_TIMEOUT = 10.0   # helper spawn + ready + status Started, together
_STOP_TIMEOUT = 5.0     # waiting for status Stopped
_CLOSE_TIMEOUT = 5.0    # helper exiting after its stdin is closed

_CREATE_NO_WINDOW = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     if sys.platform == "win32" else 0)

# What the helper's BluetoothError names usually mean, for the user.
_ERROR_HINTS = {
    "RadioNotAvailable": "the Bluetooth radio is off or missing",
    "DisabledByUser": "Bluetooth is turned off",
    "DisabledByPolicy": "Bluetooth is disabled by policy",
    "ConsentRequired": "Bluetooth access was not granted",
    "NotSupported": "not supported by this adapter",
    "TransportNotSupported": "not supported by this adapter",
    "ResourceInUse": "the adapter is busy",
    "OtherError": "the adapter rejected it (payload too large?)",
}


class BleUnavailable(Exception):
    """Bluetooth advertising can't be provided in this environment."""


# ----------------------------------------------------------------------
# Helper process


def _helper_argv() -> list[str]:
    """Command line for the helper: $NEARSHARE_BLE_HELPER (a .py file is
    run with this interpreter - the tests' fake helper), else the exe next
    to the Python executable, else the in-repo build output."""
    override = os.environ.get(_HELPER_ENV)
    if override:
        if not os.path.isfile(override):
            raise BleUnavailable(f"BLE helper not found: {override}")
        if override.lower().endswith(".py"):
            return [sys.executable, "-u", override]
        return [override]
    candidates = (
        Path(sys.executable).parent / _HELPER_NAME,
        Path(__file__).resolve().parents[2] / "windows" / "ble" / _HELPER_NAME,
    )
    for path in candidates:
        if path.is_file():
            return [str(path)]
    raise BleUnavailable(
        f"{_HELPER_NAME} not found (looked in "
        f"{', '.join(str(p.parent) for p in candidates)}); "
        f"build it with windows\\ble\\build.cmd")


class _Advert:
    """One advertisement as the helper last reported it. Mutated by the
    reader thread; every field is guarded by the owning session's `cond`."""

    def __init__(self, advert_id: str) -> None:
        self.id = advert_id
        self.status = "Created"
        self.error = ""
        self.message: str | None = None   # an "error" event for this id

    def terminal(self) -> bool:
        return self.status in ("Stopped", "Aborted")


class _Session:
    """A running helper process plus the thread reading its output."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.users: set = set()           # guarded by the manager's lock
        self.cond = threading.Condition()  # guards everything below
        self.ready = False
        self.central = True
        self.peripheral = True
        self.failure: str | None = None   # why the helper can't be used
        self.dead = False                 # reader saw EOF
        self.adverts: dict[str, _Advert] = {}
        self.scanners: list[BleScanner] = []
        self.scan_state = "idle"          # idle | starting | on
        self.scan_error: str | None = None
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(
            target=self._read_loop, name="nearshare-ble-helper", daemon=True)
        self._reader.start()

    @classmethod
    def spawn(cls) -> "_Session":
        argv = _helper_argv()
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=_CREATE_NO_WINDOW)
        except OSError as exc:
            raise BleUnavailable(f"cannot start the BLE helper: {exc}")
        log.debug("BLE helper started: %s (pid %d)", argv, proc.pid)
        return cls(proc)

    # -- talking to the helper ------------------------------------------

    def send(self, command: dict) -> None:
        data = (json.dumps(command, separators=(",", ":")) + "\n").encode()
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise BleUnavailable(f"the BLE helper is not running: {exc}")

    def wait_ready(self, deadline: float) -> None:
        """Block until the helper says ready; BleUnavailable otherwise."""
        with self.cond:
            self.cond.wait_for(
                lambda: self.ready or self.failure is not None or self.dead,
                timeout=max(0.0, deadline - time.monotonic()))
            if self.ready:
                return
            raise BleUnavailable(
                self.failure or _exit_message(self.proc, self.dead))

    def close(self) -> None:
        """Ask the helper to stop everything (EOF) and reap it."""
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=_CLOSE_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("BLE helper did not exit on EOF; killing it")
            self.proc.kill()
            self.proc.wait()
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=2)
        try:
            self.proc.stdout.close()
        except (OSError, ValueError):
            pass

    # -- reader thread ----------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    # Not protocol: stderr is merged into stdout.
                    log.debug("BLE helper: %s", line)
                    continue
                if isinstance(msg, dict):
                    try:
                        self._dispatch(msg)
                    except Exception:
                        log.exception("BLE helper event failed: %s", line)
        except (OSError, ValueError):
            pass
        finally:
            exit_message = _exit_message(self.proc, True)
            with self.cond:
                self.dead = True
                if self.failure is None:
                    self.failure = exit_message
                self.cond.notify_all()

    def _advert_for(self, msg: dict) -> "_Advert | None":
        advert_id = msg.get("id")
        if isinstance(advert_id, str):
            return self.adverts.get(advert_id)
        return None

    def _dispatch(self, msg: dict) -> None:
        event = msg.get("event")
        callbacks = []
        with self.cond:
            if event == "ready":
                self.ready = True
                self.peripheral = bool(msg.get("peripheral", True))
                self.central = bool(msg.get("central", True))
            elif event == "unavailable":
                self.failure = str(
                    msg.get("message") or "Bluetooth is unavailable")
            elif event == "advertise":
                advert = self._advert_for(msg)
                if advert is not None:
                    previous = advert.status
                    advert.status = str(msg.get("status") or "")
                    advert.error = str(msg.get("error") or "")
                    if (advert.status == "Aborted" and
                            previous in ("Started", "Waiting")):
                        log.warning("BLE advertisement %s aborted by "
                                    "Windows (%s)", advert.id,
                                    advert.error or "no reason given")
            elif event == "error":
                advert = self._advert_for(msg)
                message = str(msg.get("message") or "unknown error")
                if advert is not None:
                    advert.message = message
                elif msg.get("cmd") == "scan":
                    self.scan_error = message
                    self.scan_state = "idle"
                else:
                    log.warning("BLE helper error: %s", message)
            elif event == "scan":
                status = msg.get("status")
                error = str(msg.get("error") or "")
                if status == "Started":
                    self.scan_state = "on"
                elif status == "Stopped" and error:
                    # Stopped without an error is just our own scan_stop.
                    log.warning("BLE scan stopped by Windows (%s)", error)
                    self.scan_error = error
                    self.scan_state = "idle"
            elif event == "seen":
                if str(msg.get("uuid", "")).lower() == _QUICK_SHARE_UUID16:
                    callbacks = [s._on_activity for s in self.scanners]
            self.cond.notify_all()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                log.exception("BLE activity callback failed")


def _exit_message(proc: subprocess.Popen, exited: bool) -> str:
    if not exited:
        return "timed out waiting for the Bluetooth helper"
    try:
        code = proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return "the Bluetooth helper stopped responding"
    return f"the Bluetooth helper exited (code {code})"


class _Manager:
    """Owns the one shared helper session of this Python process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: _Session | None = None

    def acquire(self, owner: object) -> _Session:
        """The live session (spawned on demand), with `owner` registered
        as a user; pair with release()."""
        with self._lock:
            session = self._session
            if session is not None:
                with session.cond:
                    dead = session.dead
                if dead:
                    session = None
            if session is None:
                session = _Session.spawn()
                self._session = session
            session.users.add(owner)
            return session

    def release(self, session: _Session, owner: object) -> None:
        """Drop `owner`; the last one out shuts the helper down."""
        with self._lock:
            session.users.discard(owner)
            if session.users:
                return
            if self._session is session:
                self._session = None
        session.close()

    def shutdown(self) -> None:
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            session.close()


_manager = _Manager()
atexit.register(_manager.shutdown)


def _short_uuid(uuid: str) -> str:
    """'0000fe2c-0000-1000-8000-00805f9b34fb' (or 'fe2c') -> 'fe2c'."""
    text = uuid.strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if len(text) == 36 and text.startswith("0000") and \
            text.endswith(_BASE_UUID_SUFFIX):
        text = text[4:8]
    if len(text) != 4 or any(c not in "0123456789abcdef" for c in text):
        raise BleUnavailable(
            f"service UUID {uuid!r} can't be advertised on Windows "
            f"(only 16-bit UUIDs are supported)")
    return text


# ----------------------------------------------------------------------
# Public API (mirrors nearshare.core.ble)


class BleAdvertiser:
    """Broadcasts a BLE advertisement while started: by default the Quick
    Share trigger beacon, or any service/manufacturer data given (the
    AirDrop wake advert in nearshare.airdrop.wake uses this).

    Thread-safe start()/stop(); both block until Windows confirms. All
    advertisers in the process share one helper process, each with its own
    advertisement id.
    """

    _ids = itertools.count(1)

    def __init__(self, service_data: dict[str, bytes] | None = None,
                 manufacturer_data: dict[int, bytes] | None = None,
                 name: str = "advertisement0",
                 label: str = "BLE trigger advertising",
                 connectable: bool = False,
                 interval_ms: int | None = None) -> None:
        if service_data is None and manufacturer_data is None:
            service_data = {SERVICE_UUID: SERVICE_DATA}
        self._service_data = service_data or {}
        self._manufacturer_data = manufacturer_data or {}
        self._name = name
        self._label = label
        self._connectable = connectable
        self._interval_ms = interval_ms
        self._session: _Session | None = None
        self._advert: _Advert | None = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        session, advert = self._session, self._advert
        if session is None or advert is None:
            return False
        with session.cond:
            return not session.dead and not advert.terminal()

    def start(self) -> None:
        with self._lock:
            if self._session is not None:
                if self.active:
                    return
                self._abandon(send_stop=True)   # helper died / aborted
            command = self._command()
            deadline = time.monotonic() + _START_TIMEOUT
            session = self._session = _manager.acquire(self)
            try:
                session.wait_ready(deadline)
                advert = self._advert = _Advert(command["id"])
                with session.cond:
                    session.adverts[advert.id] = advert
                session.send(command)
                self._wait_started(session, advert, deadline)
            except BleUnavailable:
                self._abandon(send_stop=True)
                raise
            if self._connectable:
                log.debug("%s: Windows advertises non-connectable only",
                          self._label)
            log.info("%s started", self._label)

    def stop(self) -> None:
        with self._lock:
            if self._session is None:
                return
            session, advert = self._session, self._advert
            try:
                session.send({"cmd": "stop", "id": advert.id})
                with session.cond:
                    stopped = session.cond.wait_for(
                        lambda: advert.terminal() or session.dead,
                        timeout=_STOP_TIMEOUT)
                if not stopped:
                    log.warning("%s: no stop confirmation from the BLE "
                                "helper", self._label)
            except BleUnavailable:
                pass   # helper already gone, so is the advertisement
            self._abandon(send_stop=False)
            log.info("%s stopped", self._label)

    # ------------------------------------------------------------------

    def _command(self) -> dict:
        command: dict = {"cmd": "advertise"}
        command["id"] = f"{self._name}-{next(BleAdvertiser._ids)}"
        if self._service_data:
            command["service_data"] = {
                _short_uuid(uuid): bytes(data).hex()
                for uuid, data in self._service_data.items()}
        if self._manufacturer_data:
            command["manufacturer_data"] = {
                str(int(company)): bytes(data).hex()
                for company, data in self._manufacturer_data.items()}
        return command

    def _wait_started(self, session: _Session, advert: _Advert,
                      deadline: float) -> None:
        with session.cond:
            session.cond.wait_for(
                lambda: (advert.status == "Started" or advert.terminal() or
                         advert.message is not None or session.dead),
                timeout=max(0.0, deadline - time.monotonic()))
            if advert.status == "Started":
                return
            if advert.message is not None:
                raise BleUnavailable(advert.message)
            if advert.status == "Aborted":
                hint = _ERROR_HINTS.get(advert.error)
                raise BleUnavailable(
                    "Windows aborted the BLE advertisement: "
                    f"{advert.error or 'no reason given'}"
                    f"{f' ({hint})' if hint else ''}")
            if advert.status == "Stopped":
                raise BleUnavailable(
                    "the BLE advertisement stopped right after starting")
            if session.dead:
                raise BleUnavailable(
                    session.failure or "the Bluetooth helper exited")
            raise BleUnavailable(
                "timed out waiting for Windows to start the BLE "
                "advertisement")

    def _abandon(self, send_stop: bool) -> None:
        """Forget this advertisement and let go of the helper; with
        send_stop, first tell the helper to drop it (unless it already
        reported Stopped)."""
        session, advert = self._session, self._advert
        self._session = self._advert = None
        if session is None:
            return
        if advert is not None:
            with session.cond:
                session.adverts.pop(advert.id, None)
                send_stop = (send_stop and not session.dead and
                             advert.status != "Stopped")
            if send_stop:
                try:
                    session.send({"cmd": "stop", "id": advert.id})
                except BleUnavailable:
                    pass
        _manager.release(session, self)


class BleScanner:
    """Listens for Quick Share BLE beacons from nearby devices.

    Android broadcasts service data on 0xFE2C while its share sheet (and,
    on many models, the receive screen) is open. Seeing that beacon tells
    us a Quick Share peer is physically nearby *right now* - we surface it
    via the callback so the app can react (e.g. prompt to go visible, or
    nudge the user that a sender is looking for targets). The beacon
    carries no address/identity; actual connection still runs over mDNS.

    The helper throttles to one report per second.
    """

    def __init__(self, on_activity) -> None:
        """on_activity: callable() -> None, invoked from the helper's
        reader thread each time a Quick Share beacon is reported."""
        self._on_activity = on_activity
        self._session: _Session | None = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        session = self._session
        if session is None:
            return False
        with session.cond:
            return (not session.dead and session.scan_state == "on" and
                    self in session.scanners)

    def start(self) -> None:
        with self._lock:
            if self._session is not None:
                if self.active:
                    return
                self._abandon()   # the helper died or Windows stopped it
            deadline = time.monotonic() + _START_TIMEOUT
            session = self._session = _manager.acquire(self)
            try:
                session.wait_ready(deadline)
                if not session.central:
                    raise BleUnavailable("the Bluetooth adapter does not "
                                         "support the LE central role")
                with session.cond:
                    session.scanners.append(self)
                    first = session.scan_state == "idle"
                    if first:
                        session.scan_state = "starting"
                        session.scan_error = None
                if first:
                    session.send({"cmd": "scan", "uuid": _QUICK_SHARE_UUID16})
                with session.cond:
                    session.cond.wait_for(
                        lambda: (session.scan_state == "on" or
                                 session.scan_error is not None or
                                 session.dead),
                        timeout=max(0.0, deadline - time.monotonic()))
                    if session.scan_state != "on":
                        if session.scan_error is not None:
                            reason = session.scan_error
                        elif session.dead:
                            reason = (session.failure or
                                      "the Bluetooth helper exited")
                        else:
                            reason = ("timed out waiting for Windows to "
                                      "start the BLE scan")
                        raise BleUnavailable(reason)
            except BleUnavailable:
                self._abandon()
                raise
            log.info("BLE scanning for Quick Share beacons started")

    def stop(self) -> None:
        with self._lock:
            if self._session is None:
                return
            self._abandon()
            log.info("BLE scanning stopped")

    def _abandon(self) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        with session.cond:
            if self in session.scanners:
                session.scanners.remove(self)
            last = not session.scanners
            if last:
                was_scanning = session.scan_state != "idle"
                session.scan_state = "idle"
                session.scan_error = None
        if last and was_scanning:
            try:
                session.send({"cmd": "scan_stop"})
            except BleUnavailable:
                pass
        _manager.release(session, self)
