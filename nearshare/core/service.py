"""NearShareService: the headless core the GTK UI and CLI both drive.

Owns the TCP receive server, the mDNS advertiser (visibility on/off), the
mDNS browser (nearby peers), and outbound sends. Also serves a JSON-lines
control socket at $XDG_RUNTIME_DIR/nearshare.sock so the `nearshare`
CLI can toggle visibility, query status, and start sends against the
running app:

    {"cmd": "status"}                      -> {"visible": true, ...}
    {"cmd": "on"} / {"cmd": "off"} / {"cmd": "toggle"}
    {"cmd": "peers"}                       -> {"peers": [...]}
    {"cmd": "send", "peer": "<instance>", "files": ["/path", ...]}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from typing import Callable

from .ble import BleAdvertiser, BleScanner, BleUnavailable
from .connection import (Events, InboundConnection, OutboundConnection,
                         random_endpoint_id)
from .mdns import Advertiser, Browser, Peer

log = logging.getLogger("nearshare.service")

# Concurrent inbound connections allowed before new ones are refused.
# One sender at a time is the norm; the headroom covers a retrying phone
# overlapping with a finishing transfer.
MAX_CONCURRENT_INBOUND = 8


def control_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime) / "nearshare.sock"


class AlreadyRunning(RuntimeError):
    """Another instance already owns the control socket.

    Without this check every launch would happily start a second full
    service: a second mDNS advertisement, a second TCP listener and a
    second BLE beacon, so phones list this machine once per running
    instance. Raised by start() before anything is advertised.
    """


async def _instance_is_alive(path: Path) -> bool:
    """True if something is actually serving the control socket.

    A socket file left behind by a killed process still `exists()`, so
    liveness has to be probed by connecting, not by stat-ing.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=2)
    except (ConnectionRefusedError, FileNotFoundError, OSError,
            asyncio.TimeoutError):
        return False
    try:
        writer.write(json.dumps({"cmd": "status"}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2)
        return bool(line.strip())
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass


def default_device_name() -> str:
    return f"{os.environ.get('USER', 'user')}@{socket.gethostname()}"


class NearShareService:
    def __init__(self, device_name: str | None = None,
                 download_dir: Path | None = None,
                 events: Events | None = None) -> None:
        self.device_name = device_name or default_device_name()
        self.download_dir = download_dir or Path.home() / "Downloads"
        self.events = events or Events()
        self.endpoint_id = random_endpoint_id()
        self.port: int | None = None
        self._server: asyncio.Server | None = None
        self._control: asyncio.Server | None = None
        self._advertiser: Advertiser | None = None
        self._ble = BleAdvertiser()
        self.ble_status = "off"  # "on", "off", or "unavailable: <why>"
        self._ble_scanner = BleScanner(on_activity=self._ble_activity_seen)
        self.ble_scan_status = "off"
        self._inbound_count = 0
        self.last_ble_activity: float = 0.0
        self._ble_notified_at: float = 0.0
        self.on_ble_activity: Callable[[], None] | None = None
        self.browser = Browser(on_change=self._peers_changed)
        self.on_visibility_changed: Callable[[bool], None] | None = None
        self.on_peers_changed: Callable[[dict[str, Peer]], None] | None = None
        # Running transfers by (device name, "send" | "receive"), so the
        # UI can cancel one. A cancelled task closes its connection on the
        # way out, which is what tells the other side.
        self._transfers: dict[tuple[str, str], asyncio.Task] = {}

    # ------------------------------------------------------------ lifecycle

    async def start(self, visible: bool = True) -> None:
        """Bind the TCP server + control socket; optionally go visible."""
        self._server = await asyncio.start_server(
            self._handle_inbound, host="0.0.0.0", port=0)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("receive server on port %d", self.port)

        self._advertiser = Advertiser(self.device_name, self.endpoint_id,
                                      self.port)
        self.browser.ignore_instance(self.endpoint_id)
        # Discovery runs regardless of our own visibility: being hidden
        # means "don't advertise", not "don't see others".
        await self.browser.start()
        await self._start_ble_scan()
        await self._start_control_socket()
        if visible:
            await self.set_visible(True)

    async def stop(self) -> None:
        await self.set_visible(False)
        if self._ble_scanner.active:
            await asyncio.to_thread(self._ble_scanner.stop)
        await self.browser.stop()
        for server in (self._server, self._control):
            if server is not None:
                server.close()
                await server.wait_closed()
        sock = control_socket_path()
        if sock.exists():
            sock.unlink()

    # ------------------------------------------------------------ visibility

    @property
    def visible(self) -> bool:
        return self._advertiser is not None and self._advertiser.active

    async def set_visible(self, visible: bool) -> bool:
        if self._advertiser is None:
            return False
        if visible and not self._advertiser.active:
            await self._advertiser.start()
            await self._set_ble(True)
        elif not visible and self._advertiser.active:
            await self._advertiser.stop()
            await self._set_ble(False)
        if self.on_visibility_changed:
            self.on_visibility_changed(self.visible)
        return self.visible

    async def _start_ble_scan(self) -> None:
        """Passive scan for Quick Share BLE beacons from phones; presence
        signal only (the beacon has no address), never fails startup."""
        try:
            await asyncio.to_thread(self._ble_scanner.start)
            self.ble_scan_status = "on"
        except BleUnavailable as exc:
            self.ble_scan_status = f"unavailable: {exc}"
            log.info("BLE scanning unavailable: %s", exc)

    def _ble_activity_seen(self) -> None:
        """Called from the BLE thread whenever a nearby device broadcasts
        the Quick Share beacon (phone share sheet / receive screen open)."""
        import subprocess
        import time
        now = time.monotonic()
        self.last_ble_activity = now
        if self.on_ble_activity:
            self.on_ble_activity()
        # If we're hidden, nudge the user once a minute at most: someone
        # nearby is actively using Quick Share and can't see us.
        if not self.visible and now - self._ble_notified_at > 60:
            self._ble_notified_at = now
            try:
                subprocess.Popen(
                    ["notify-send", "-a", "NearShare",
                     "Quick Share activity nearby",
                     "A nearby device is using Quick Share. "
                     "Turn on visibility to receive."],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    async def _set_ble(self, on: bool) -> None:
        """Start/stop the BLE trigger beacon; never fails visibility."""
        try:
            if on and not self._ble.active:
                await asyncio.to_thread(self._ble.start)
                self.ble_status = "on"
            elif not on and self._ble.active:
                await asyncio.to_thread(self._ble.stop)
                self.ble_status = "off"
        except BleUnavailable as exc:
            self.ble_status = f"unavailable: {exc}"
            log.warning("BLE advertising unavailable (%s); "
                        "mDNS-only mode - the phone may need its Quick "
                        "Share screen open to see this device", exc)

    async def toggle(self) -> bool:
        return await self.set_visible(not self.visible)

    # ------------------------------------------------------------ transfers

    async def _handle_inbound(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        # Cap concurrent inbound connections. Legitimate use is one
        # sender at a time (occasionally two); without a cap anyone on
        # the LAN can open sockets until the process runs out of file
        # descriptors, and each one costs a task, buffers and a
        # keep-alive timer. Refuse politely rather than queueing, so a
        # flood cannot delay a real transfer either.
        if self._inbound_count >= MAX_CONCURRENT_INBOUND:
            peer = writer.get_extra_info("peername")
            log.warning("refusing connection from %s: %d already in "
                        "progress", peer, self._inbound_count)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return
        self._inbound_count += 1
        try:
            conn = InboundConnection(reader, writer, self.events,
                                     self.device_name, self.download_dir)
            await self._run_tracked(conn, "receive")
        finally:
            self._inbound_count -= 1

    async def send_files(self, peer: Peer, files: list[Path]) -> None:
        """Send files to a discovered peer. Raises on failure."""
        conn = OutboundConnection(peer.host, peer.port, self.events,
                                  self.device_name, files,
                                  peer_name=peer.device_name)
        await self._run_tracked(conn, "send", peer.device_name)

    async def _run_tracked(self, conn, direction: str,
                           name: str | None = None) -> None:
        """Run a connection as a task cancel_transfer() can find.

        An inbound connection only learns the peer's name during its
        handshake, so it is looked up by `conn.peer_name` at cancel time
        rather than registered under a name here.
        """
        task = asyncio.ensure_future(conn.run())
        key = (name or id(conn), direction)
        self._transfers[key] = task
        task.nearshare_conn = conn  # type: ignore[attr-defined]
        try:
            await task
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            raise
        finally:
            self._transfers.pop(key, None)

    def cancel_transfer(self, device: str, direction: str) -> bool:
        """Stop the running transfer with `device`, if there is one.

        Returns whether a transfer was found. Partial received files are
        deleted by the connection as it unwinds.
        """
        for (name, kind), task in list(self._transfers.items()):
            if kind != direction or task.done():
                continue
            conn = getattr(task, "nearshare_conn", None)
            if name == device or getattr(conn, "peer_name", None) == device:
                task.cancel()
                return True
        return False

    async def set_device_name(self, name: str) -> None:
        """Change the name nearby devices see, re-advertising if visible."""
        name = name.strip()
        if not name or name == self.device_name:
            return
        self.device_name = name
        if self._advertiser is None:
            return
        was_visible = self._advertiser.active
        if was_visible:
            await self._advertiser.stop()
        self._advertiser = Advertiser(name, self.endpoint_id,
                                      self._advertiser.port)
        if was_visible:
            await self._advertiser.start()

    def _peers_changed(self, peers: dict[str, Peer]) -> None:
        if self.on_peers_changed:
            self.on_peers_changed(peers)

    # ------------------------------------------------------------ control API

    async def _start_control_socket(self) -> None:
        path = control_socket_path()
        if path.exists():
            if await _instance_is_alive(path):
                raise AlreadyRunning(
                    "another NearShare instance is already running")
            # Nothing listening: a previous run was killed without
            # cleaning up. Safe to take the stale socket over.
            path.unlink()
        # bind() creates the socket file with mode 0777 & ~umask, so with
        # a typical 022 umask it's briefly group/world-accessible before
        # the chmod below runs. The control socket accepts a "send"
        # command that can push any file the daemon's user can read to
        # any LAN peer, so close that window instead of just narrowing it
        # after the fact.
        old_umask = os.umask(0o177)
        try:
            self._control = await asyncio.start_unix_server(
                self._handle_control, path=str(path))
        finally:
            os.umask(old_umask)
        path.chmod(0o600)

    async def _handle_control(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await reader.readline()
            except ValueError:
                # No newline within the StreamReader's buffer limit
                # (64 KiB by default): readline() raises rather than
                # buffering forever, but that exception would otherwise
                # escape this connection's task uncaught.
                writer.write(b'{"error": "line too long"}\n')
                return
            if not line:
                return
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                writer.write(b'{"error": "bad json"}\n')
                return
            if not isinstance(req, dict):
                # Valid JSON (e.g. a bare number, string, or list) but not
                # an object: req.get("cmd") below would otherwise raise
                # AttributeError and crash this connection's task.
                writer.write(b'{"error": "request must be a JSON object"}\n')
                return
            resp = await self._dispatch_control(req)
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _dispatch_control(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "status":
            import time
            recent = (self.last_ble_activity > 0 and
                      time.monotonic() - self.last_ble_activity < 30)
            return {"visible": self.visible, "device_name": self.device_name,
                    "port": self.port, "peers": len(self.browser.peers),
                    "ble": self.ble_status,
                    "ble_scan": self.ble_scan_status,
                    "ble_activity_nearby": recent}
        if cmd in ("on", "off", "toggle"):
            if cmd == "toggle":
                visible = await self.toggle()
            else:
                visible = await self.set_visible(cmd == "on")
            return {"visible": visible}
        if cmd == "peers":
            return {"peers": [
                {"instance": name, "name": p.device_name, "host": p.host,
                 "port": p.port, "type": p.device_type}
                for name, p in self.browser.peers.items()]}
        if cmd == "send":
            files = [Path(f) for f in req.get("files", [])]
            missing = [str(f) for f in files if not f.is_file()]
            if missing:
                return {"error": f"no such file: {', '.join(missing)}"}
            peer = self._match_peer(req.get("peer", ""))
            if peer is None:
                return {"error": "peer not found; is its Quick Share screen open?"}
            try:
                await self.send_files(peer, files)
                return {"ok": True, "sent_to": peer.device_name}
            except Exception as exc:  # surfaced to CLI user
                return {"error": str(exc)}
        return {"error": f"unknown command {cmd!r}"}

    def _match_peer(self, needle: str) -> Peer | None:
        needle = needle.lower()
        for name, peer in self.browser.peers.items():
            if needle in (name.lower(), peer.instance.lower(),
                          peer.endpoint_id.lower(),
                          peer.device_name.lower()):
                return peer
        # Fall back to substring match on the human name.
        for peer in self.browser.peers.values():
            if needle and needle in peer.device_name.lower():
                return peer
        return None
