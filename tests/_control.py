"""Raw access to the running service's control channel for tests, on
either platform: the Unix socket on Linux, the token-guarded loopback
port on Windows (see nearshare/core/control.py)."""

from __future__ import annotations

import asyncio
import json

from nearshare.core import control
from nearshare.core.paths import IS_WINDOWS


async def open_raw() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if IS_WINDOWS:
        port, _token = control._read_info()
        return await asyncio.open_connection("127.0.0.1", port)
    return await asyncio.open_unix_connection(str(control.control_socket_path()))


def encode(req: dict) -> bytes:
    """One request line, carrying the token where the platform needs it."""
    if IS_WINDOWS:
        req = {**req, "token": control._read_info()[1]}
    return json.dumps(req).encode() + b"\n"


async def request(req: dict, timeout: float = 60) -> dict:
    reader, writer = await open_raw()
    try:
        writer.write(encode(req))
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), timeout))
    finally:
        writer.close()
