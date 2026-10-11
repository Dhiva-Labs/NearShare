"""AirDrop sender: /Discover, /Ask and /Upload against an Apple device.

An iPhone in "Everyone" mode only listens in short bursts, and only after
it has heard a sender's Bluetooth LE AirDrop advertisement (see
wake.py), so connecting is retried until a deadline rather than tried
once. The Ask and the Upload go over the same TLS connection, and the
Upload's TransferID header repeats the id the Ask announced: iOS 26
refuses an upload it cannot bind to an accepted Ask.

The upload streams: files are read, archived (cpio), and compressed
(DVZip) block by block while the socket drains, so memory use does not
grow with the size of what is sent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from ..core.connection import Events
from . import protocol
from .archive import ArchiveError, cpio_stream, dvzip_stream
from .http import ClientConnection, HttpError
from .identity import Identity

log = logging.getLogger("nearshare.airdrop.sender")

CONNECT_TIMEOUT = 5.0
OP_TIMEOUT = 20.0
ASK_TIMEOUT = 120.0          # the recipient has to notice and tap Accept
REACH_DEADLINE = 30.0        # keep dialling a phone that listens in bursts

# Headers a Discover/Ask carries. Deliberately no Host and no
# Accept-Encoding, as sharingd sends.
_BASE_HEADERS = [
    ("Content-Type", "application/octet-stream"),
    ("Connection", "keep-alive"),
    ("Accept", "*/*"),
    ("User-Agent", protocol.USER_AGENT),
    ("Accept-Language", "en-us"),
]


class SendError(Exception):
    """A send that did not complete; the message is for people."""


class Declined(SendError):
    pass


@dataclass
class AirDropPeer:
    """An AirDrop endpoint: where to dial, and what it calls itself."""
    address: str                    # IPv6 literal, without zone
    port: int = protocol.PEER_PORT
    iface: str | None = protocol.AWDL_IFACE
    name: str | None = None         # None until it answers /Discover
    model: str | None = None
    mac: str | None = None
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def key(self) -> str:
        return self.mac or self.address

    @property
    def display_name(self) -> str:
        return self.name or "Apple device"

    @property
    def host(self) -> str:
        return protocol.scoped(self.address, self.iface)


class AirDropSender:
    def __init__(self, identity: Identity, device_name: str, events: Events,
                 *, model: str = protocol.DEFAULT_MODEL,
                 sender_id: str | None = None) -> None:
        self.identity = identity
        self.device_name = device_name
        self.events = events
        self.model = model
        self.sender_id = sender_id or protocol.random_sender_id()

    async def _connect(self, peer: AirDropPeer) -> ClientConnection:
        return await ClientConnection.open(
            peer.host, peer.port, self.identity.client_context(),
            timeout=CONNECT_TIMEOUT)

    async def _discover_on(self, conn: ClientConnection,
                           peer: AirDropPeer) -> None:
        resp = await conn.request("POST", "/Discover", _BASE_HEADERS,
                                  protocol.discover_request(),
                                  timeout=OP_TIMEOUT)
        if resp.status != 200:
            raise SendError(f"{peer.display_name} would not identify itself "
                            f"(HTTP {resp.status})")
        answer = protocol.loads(resp.body)
        name = answer.get("ReceiverComputerName")
        model = answer.get("ReceiverModelName")
        if isinstance(name, str) and name.strip():
            peer.name = name.strip()[:64]
        if isinstance(model, str):
            peer.model = model[:64]
        peer.last_seen = time.monotonic()

    async def discover(self, peer: AirDropPeer) -> bool:
        """Ask a peer for its name. False if it isn't answering right now."""
        try:
            conn = await self._connect(peer)
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            await self._discover_on(conn, peer)
            return True
        except (OSError, asyncio.TimeoutError, ConnectionError, HttpError,
                SendError):
            return False
        finally:
            await conn.close()

    async def _reach(self, peer: AirDropPeer, deadline: float
                     ) -> ClientConnection:
        """Dial and /Discover until the peer answers or time runs out."""
        last: Exception | None = None
        while True:
            try:
                conn = await self._connect(peer)
                try:
                    await self._discover_on(conn, peer)
                    return conn
                except BaseException:
                    await conn.close()
                    raise
            except SendError:
                raise
            except (OSError, asyncio.TimeoutError, ConnectionError,
                    HttpError) as exc:
                last = exc
            if time.monotonic() >= deadline:
                raise SendError(
                    f"{peer.display_name} is not answering. Unlock it and "
                    f"make sure AirDrop is set to Everyone. ({last})")
            await asyncio.sleep(1.0)

    async def send_files(self, peer: AirDropPeer, files: list[Path], *,
                         reach_deadline: float = REACH_DEADLINE,
                         cancelled: Callable[[], bool] | None = None) -> None:
        """Send files as one transfer the recipient accepts once."""
        files = [Path(f) for f in files]
        names = [f.name for f in files]
        if len(set(names)) != len(names):
            raise SendError("Two of the files have the same name; the "
                            "recipient would keep only one.")
        sizes = [f.stat().st_size for f in files]
        total = sum(sizes)
        transfer_id = protocol.new_transfer_id()
        ask = protocol.ask_files(
            self.device_name, self.model, self.sender_id, transfer_id,
            [(n, s, protocol.uti_for(n)) for n, s in zip(names, sizes)],
            icon=_thumbnail(files[0]))

        conn = await self._reach(peer, time.monotonic() + reach_deadline)
        try:
            resp = await conn.request("POST", "/Ask", _BASE_HEADERS, ask,
                                      timeout=ASK_TIMEOUT)
            if resp.status in (401, 403):
                raise Declined(f"{peer.display_name} declined the transfer.")
            if resp.status != 200:
                raise SendError(f"{peer.display_name} refused the transfer "
                                f"(HTTP {resp.status}).")

            sent = 0
            last_report = 0.0

            def counted() -> Iterator[bytes]:
                nonlocal sent, last_report
                for piece in cpio_stream(zip(names, files)):
                    if cancelled and cancelled():
                        raise SendError("Cancelled.")
                    sent += len(piece)
                    now = time.monotonic()
                    if self.events.on_progress and now - last_report > 0.25:
                        last_report = now
                        self.events.on_progress(peer.display_name,
                                                min(sent, total), total)
                    yield piece

            # sharingd's exact header set and order for an upload.
            headers = [
                ("User-Agent", protocol.USER_AGENT),
                ("TotalBytes", str(total)),
                ("Content-Type", "application/x-dvzip"),
                ("TransferID", transfer_id),
                ("Connection", "keep-alive"),
            ]
            try:
                resp = await conn.request("POST", "/Upload", headers,
                                          dvzip_stream(counted()),
                                          timeout=OP_TIMEOUT * 3)
            except ArchiveError as exc:
                raise SendError(str(exc)) from None
            if resp.status != 200:
                raise SendError(f"{peer.display_name} rejected the upload "
                                f"(HTTP {resp.status}).")
        except (OSError, asyncio.TimeoutError, ConnectionError,
                HttpError) as exc:
            raise SendError(f"The connection to {peer.display_name} was "
                            f"lost: {exc}") from None
        finally:
            await conn.close()
        if self.events.on_progress:
            self.events.on_progress(peer.display_name, total, total)
        if self.events.on_complete:
            self.events.on_complete(peer.display_name, files)

    async def send_link(self, peer: AirDropPeer, url: str, *,
                        reach_deadline: float = REACH_DEADLINE) -> None:
        if not protocol.is_web_link(url):
            raise SendError("Only http and https links can be sent.")
        conn = await self._reach(peer, time.monotonic() + reach_deadline)
        try:
            resp = await conn.request(
                "POST", "/Ask", _BASE_HEADERS,
                protocol.ask_links(self.device_name, self.model,
                                   self.sender_id, protocol.new_transfer_id(),
                                   [url]),
                timeout=ASK_TIMEOUT)
        except (OSError, asyncio.TimeoutError, ConnectionError,
                HttpError) as exc:
            raise SendError(f"The connection to {peer.display_name} was "
                            f"lost: {exc}") from None
        finally:
            await conn.close()
        if resp.status in (401, 403):
            raise Declined(f"{peer.display_name} declined the link.")
        if resp.status != 200:
            raise SendError(f"{peer.display_name} refused the link "
                            f"(HTTP {resp.status}).")


def _thumbnail(path: Path) -> bytes | None:
    """A small JPEG preview for the recipient's prompt, when one is cheap
    to make. Optional: senders without one are accepted all the same."""
    if protocol.uti_for(path) not in ("public.jpeg", "public.png",
                                      "public.heic", "public.heif"):
        return None
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 240, 240,
                                                        True)
        ok, data = pixbuf.save_to_bufferv("jpeg", ["quality"], ["80"])
        return bytes(data) if ok else None
    except Exception:
        return None
