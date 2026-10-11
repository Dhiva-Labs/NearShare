"""The local control channel between the running app and its clients
(the `nearshare` CLI, the send picker, a second launch of the app).

One JSON request per connection, one JSON line back. The channel accepts
"send", which pushes any file the app's user can read to any peer, so
only that user may reach it:

  Linux    a Unix socket, $XDG_RUNTIME_DIR/nearshare.sock, mode 0600.
  Windows  asyncio has no Unix-socket server there, so a TCP socket on
           127.0.0.1 with a random port, and a random token published
           beside it in %LOCALAPPDATA%\\NearShare\\control.json (inside
           the user's profile, so readable only by them). Every request
           carries the token; one without it is refused.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import socket
from pathlib import Path
from typing import Any, Awaitable, Callable

from .paths import IS_WINDOWS, runtime_dir

PROBE_TIMEOUT = 2.0


class AlreadyRunning(RuntimeError):
    """Another instance already owns the control channel.

    Without this check every launch would happily start a second full
    service: a second mDNS advertisement, a second TCP listener and a
    second BLE beacon, so phones list this machine once per running
    instance. Raised by start() before anything is advertised.
    """


def control_socket_path() -> Path:
    return runtime_dir() / "nearshare.sock"


def _info_path() -> Path:
    return runtime_dir() / "control.json"


def _read_info() -> tuple[int, str] | None:
    try:
        info = json.loads(_info_path().read_text())
        return int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ------------------------------------------------------------------ client

def request(req: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    """Send one request to the running app; its answer, or None when no
    app is listening."""
    try:
        if IS_WINDOWS:
            info = _read_info()
            if info is None:
                return None
            port, token = info
            req = {**req, "token": token}
            sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(str(control_socket_path()))
    except OSError:
        return None
    try:
        sock.settimeout(timeout)
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
    except ValueError:
        return {"error": "malformed response"}
    finally:
        sock.close()


async def instance_alive() -> bool:
    """True if something is actually serving the control channel.

    A socket file (or control.json) left behind by a killed process
    still exists, so liveness has to be probed by connecting.
    """
    try:
        if IS_WINDOWS:
            info = _read_info()
            if info is None:
                return False
            port, token = info
            opened = asyncio.open_connection("127.0.0.1", port)
            probe = {"cmd": "status", "token": token}
        else:
            opened = asyncio.open_unix_connection(str(control_socket_path()))
            probe = {"cmd": "status"}
        reader, writer = await asyncio.wait_for(opened, PROBE_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.write(json.dumps(probe).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), PROBE_TIMEOUT)
        return bool(line.strip())
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass


# ------------------------------------------------------------------ server

class ControlServer:
    def __init__(self, on_client: Callable[[asyncio.StreamReader,
                                            asyncio.StreamWriter],
                                           Awaitable[None]]) -> None:
        self._on_client = on_client
        self._server: asyncio.Server | None = None
        self._token = secrets.token_hex(32) if IS_WINDOWS else None

    def authorised(self, req: dict) -> bool:
        """Strip and check the request's token (Windows); always true on
        Linux, where the socket's file mode already did the checking."""
        token = req.pop("token", None)
        if self._token is None:
            return True
        return isinstance(token, str) and hmac.compare_digest(token, self._token)

    async def start(self) -> None:
        if await instance_alive():
            raise AlreadyRunning("another NearShare instance is already running")
        if IS_WINDOWS:
            await self._start_tcp()
        else:
            await self._start_unix()

    async def _start_unix(self) -> None:
        path = control_socket_path()
        # Nothing answered above, so a file still here is a stale socket
        # from a run that was killed. Safe to take over.
        if path.exists():
            path.unlink()
        # bind() creates the socket file with mode 0777 & ~umask, so with
        # a typical 022 umask it's briefly group/world-accessible before
        # the chmod below runs. Close that window instead of narrowing it
        # after the fact.
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._on_client, path=str(path))
        finally:
            os.umask(old_umask)
        path.chmod(0o600)

    async def _start_tcp(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client, host="127.0.0.1", port=0)
        port = self._server.sockets[0].getsockname()[1]
        path = _info_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"port": port, "token": self._token, "pid": os.getpid()}, fh)
        os.replace(tmp, path)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        path = _info_path() if IS_WINDOWS else control_socket_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
