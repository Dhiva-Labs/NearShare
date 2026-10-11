"""AirDrop receiver: the HTTPS server an iPhone or Mac sends files to.

Serves /Discover, /Ask and /Upload on the AWDL interface while the radio
backend keeps this machine discoverable. The radio backend, not this
module, puts the `_airdrop._tcp` announcement on the air; it names port
8771 and an instance derived from awdl0's MAC, so all this has to do is
answer on that port.

Every /Ask is put to the user through the same Events.on_transfer_request
callback Quick Share transfers use (with an empty PIN: AirDrop has none),
and nothing is written to disk unless they accept. An /Upload is only
taken when it belongs to an accepted /Ask: matched by TransferID, or for
older senders that send none, by the address that was accepted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..core.connection import Events, FileOffer, TransferRequest
from . import protocol
from .archive import ArchiveError, ArchiveTooLarge, BodyDecoder, CpioReader
from .http import (HttpError, Request, continue_bytes, iter_body, read_body,
                   read_request, response_bytes)
from .identity import Identity

log = logging.getLogger("nearshare.airdrop.receiver")

IDLE_TIMEOUT = 30.0
MAX_METADATA = 1 << 20          # /Discover and /Ask bodies (an Ask's icon is ~25 KB)
MAX_FILES = 1024
MAX_CONNECTIONS = 16
MAX_UPLOADS = 2
ACCEPT_TTL = 600.0              # an accepted Ask must be followed by its Upload
ASK_TIMEOUT = 60.0              # how long the user has to answer the prompt
# Room to leave on the disk, and the most an upload may inflate beyond
# what its Ask declared (senders convert HEIF to JPEG after asking).
DISK_RESERVE = 512 * 1024 * 1024
CONVERSION_SLACK = 4

# Path separators, control characters, and the bidi/zero-width format
# characters that let "gpj.exe" render as "exe.jpg" in a file manager.
_UNSAFE = re.compile(
    r"[/\\\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")


def safe_name(name: str, fallback: str = "airdrop-file") -> str:
    base = unicodedata.normalize("NFC", name.replace("\\", "/").rsplit("/", 1)[-1])
    base = _UNSAFE.sub("", base).strip().lstrip(".").strip()
    if not base:
        return fallback
    stem, ext = os.path.splitext(base)
    while len((stem + ext).encode("utf-8")) > 200 and stem:
        stem = stem[:-1]
    return (stem + ext) or fallback


def open_unique(directory: Path, name: str):
    """Create directory/name, or "name (n)", without following symlinks
    or replacing anything already there. Returns (path, file)."""
    stem, ext = os.path.splitext(name)
    for n in range(10000):
        candidate = directory / (name if n == 0 else f"{stem} ({n}){ext}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        return candidate, os.fdopen(fd, "wb")
    raise FileExistsError(name)


@dataclass
class _Accepted:
    """An /Ask the user accepted, waiting for its /Upload."""
    sender: str
    peer_ip: str
    transfer_id: str | None
    declared: int
    files: list[FileOffer]
    at: float = field(default_factory=time.monotonic)


class AirDropReceiver:
    def __init__(self, device_name: str, download_dir: Path, events: Events,
                 identity: Identity, *, model: str = protocol.DEFAULT_MODEL,
                 host: str = "::", port: int = protocol.RECEIVER_PORT,
                 ask_timeout: float = ASK_TIMEOUT) -> None:
        self.device_name = device_name
        self.download_dir = download_dir
        self.events = events
        self.identity = identity
        self.model = model
        self.host = host
        self.port = port
        self.ask_timeout = ask_timeout
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.StreamWriter] = set()
        self._accepted: list[_Accepted] = []
        self._uploads = 0
        self._prompt_lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._serve, host=self.host, port=self.port,
            family=socket.AF_INET6, ssl=self.identity.server_context(),
            ssl_handshake_timeout=IDLE_TIMEOUT, reuse_address=True)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("AirDrop receiver on [%s]:%d", self.host, self.port)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        # A sender keeps its session open after a transfer; an iPhone that
        # has already left the link never answers our FIN, which would hold
        # the port for minutes. Abort instead: the transfer is over.
        for writer in list(self._connections):
            transport = writer.transport
            if transport is not None:
                transport.abort()
        try:
            await asyncio.wait_for(server.wait_closed(), 5)
        except asyncio.TimeoutError:
            pass
        self._accepted.clear()

    # ------------------------------------------------------------ serving

    async def _serve(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        if len(self._connections) >= MAX_CONNECTIONS:
            writer.transport.abort()
            return
        self._connections.add(writer)
        peer = writer.get_extra_info("peername") or ("?",)
        peer_ip = str(peer[0]).split("%")[0]
        try:
            while True:
                try:
                    request = await read_request(reader, IDLE_TIMEOUT)
                except HttpError as exc:
                    await self._send(writer, response_bytes(exc.status, close=True))
                    return
                if request is None:
                    return
                keep = await self._dispatch(request, reader, writer, peer_ip)
                if not keep:
                    return
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("AirDrop connection from %s failed", peer_ip)
        finally:
            self._connections.discard(writer)
            writer.close()

    async def _send(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        writer.write(data)
        await asyncio.wait_for(writer.drain(), IDLE_TIMEOUT)

    async def _refuse(self, writer, status: int, why: str) -> bool:
        log.warning("AirDrop request refused (%d): %s", status, why)
        await self._send(writer, response_bytes(status, close=True))
        return False

    async def _dispatch(self, request: Request, reader, writer,
                        peer_ip: str) -> bool:
        """Handle one request; False closes the connection."""
        if request.method in ("GET", "HEAD"):
            await self._send(writer, response_bytes(200))
            return True
        if request.method != "POST":
            return await self._refuse(writer, 405, request.method)
        try:
            if request.path == "/Upload":
                return await self._upload(request, reader, writer, peer_ip)
            body = await read_body(reader, request.headers,
                                   timeout=IDLE_TIMEOUT, limit=MAX_METADATA)
        except HttpError as exc:
            return await self._refuse(writer, exc.status, str(exc))
        if request.path == "/Discover":
            await self._send(writer, response_bytes(
                200, protocol.receiver_answer(self.device_name, self.model)))
            return True
        if request.path == "/Ask":
            return await self._ask(body, writer, peer_ip)
        return await self._refuse(writer, 404, request.path)

    # ------------------------------------------------------------ /Ask

    async def _ask(self, body: bytes, writer, peer_ip: str) -> bool:
        ask = protocol.loads(body)
        sender = ask.get("SenderComputerName")
        sender = safe_name(sender, "Apple device") if isinstance(sender, str) \
            else "Apple device"
        transfer_type = ask.get("TransferType") or {}
        transfer_id = None
        tid = ask.get("TransferID")
        if isinstance(tid, dict) and isinstance(tid.get("id"), str):
            transfer_id = tid["id"][:64]

        links: list[str] = []
        offers: list[FileOffer] = []
        if isinstance(transfer_type, dict) and "links" in transfer_type:
            items = ask.get("Items") or []
            if not isinstance(items, list) or len(items) > MAX_FILES:
                return await self._refuse(writer, 413, "too many items")
            links = [i for i in items if protocol.is_web_link(i)]
            if not links:
                return await self._refuse(writer, 400, "links Ask with no web link")
        else:
            files = ask.get("Files") or []
            if not isinstance(files, list) or len(files) > MAX_FILES:
                return await self._refuse(writer, 413, "too many files")
            for n, entry in enumerate(files):
                if not isinstance(entry, dict):
                    continue
                name = entry.get("FileName")
                name = safe_name(name) if isinstance(name, str) else "airdrop-file"
                size = entry.get("FileSize")
                size = size if isinstance(size, int) and size >= 0 else 0
                offers.append(FileOffer(payload_id=n, name=name, size=size,
                                        mime_type=protocol.mime_for(name)))

        request = TransferRequest(
            device_name=sender, pin="", files=offers,
            text_preview="\n".join(links) if links else None)
        declared = request.total_size
        if offers and not self._space_for(declared):
            return await self._refuse(writer, 507, "not enough free space")

        accepted = await self._prompt(request)
        if not accepted:
            log.info("AirDrop transfer from %s declined", sender)
            # 403 is what makes the sender report "Declined".
            await self._send(writer, response_bytes(403))
            return True

        await self._send(writer, response_bytes(
            200, protocol.receiver_answer(self.device_name, self.model)))
        if links:
            for url in links:
                if self.events.on_text:
                    self.events.on_text(sender, url)
            return True
        self._expire()
        self._accepted.append(_Accepted(sender, peer_ip, transfer_id,
                                        declared, offers))
        return True

    async def _prompt(self, request: TransferRequest) -> bool:
        if self.events.on_transfer_request is None:
            return False
        # One prompt at a time: an iPhone retries its Ask on a second
        # connection while the first waits, and the user should be asked
        # once, not twice.
        async with self._prompt_lock:
            try:
                return bool(await asyncio.wait_for(
                    self.events.on_transfer_request(request), self.ask_timeout))
            except asyncio.TimeoutError:
                return False

    def _expire(self) -> None:
        now = time.monotonic()
        self._accepted = [a for a in self._accepted if now - a.at < ACCEPT_TTL]

    def _claim(self, transfer_id: str | None, peer_ip: str) -> _Accepted | None:
        """The accepted Ask this upload belongs to, removed from the list.

        A TransferID binds an upload to the Ask that announced it, which is
        the binding iOS itself enforces. Senders that send no id (OpenDrop)
        are matched by address, newest first. An upload whose id was never
        announced can only claim, by address, an Ask that announced no id:
        it never takes over a transfer accepted under a different id.
        """
        self._expire()
        for i, pending in enumerate(self._accepted):
            if transfer_id and pending.transfer_id == transfer_id:
                return self._accepted.pop(i)
        for i in range(len(self._accepted) - 1, -1, -1):
            pending = self._accepted[i]
            if pending.peer_ip != peer_ip:
                continue
            if pending.transfer_id is None or transfer_id is None:
                return self._accepted.pop(i)
        return None

    def _space_for(self, declared: int) -> bool:
        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.download_dir).free
        except OSError:
            return True
        return declared + DISK_RESERVE <= free

    # ------------------------------------------------------------ /Upload

    def _inflate_limit(self, declared: int) -> int:
        try:
            free = shutil.disk_usage(self.download_dir).free - DISK_RESERVE
        except OSError:
            free = 1 << 40
        allowance = max(declared * CONVERSION_SLACK, declared + (256 << 20))
        return max(0, min(free, allowance))

    async def _upload(self, request: Request, reader, writer,
                      peer_ip: str) -> bool:
        headers = request.headers
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ("application/x-dvzip", "application/x-cpio"):
            return await self._refuse(writer, 415, f"Content-Type {ctype!r}")
        pending = self._claim(headers.get("TransferID"), peer_ip)
        if pending is None:
            return await self._refuse(writer, 403, "upload with no accepted Ask")
        if self._uploads >= MAX_UPLOADS:
            return await self._refuse(writer, 503, "too many uploads")
        limit = self._inflate_limit(pending.declared)
        if limit <= 0:
            return await self._refuse(writer, 507, "not enough free space")
        if (headers.get("Expect") or "").lower() == "100-continue":
            await self._send(writer, continue_bytes())

        self._uploads += 1
        written: list[Path] = []
        try:
            written = await self._extract(reader, headers, limit, pending)
        except HttpError as exc:
            return await self._refuse(writer, exc.status, str(exc))
        except ArchiveTooLarge as exc:
            return await self._refuse(writer, 413, str(exc))
        except ArchiveError as exc:
            return await self._refuse(writer, 422, str(exc))
        except OSError as exc:
            if self.events.on_error:
                self.events.on_error(pending.sender, f"Could not save: {exc}")
            return await self._refuse(writer, 507, str(exc))
        finally:
            self._uploads -= 1
        await self._send(writer, response_bytes(200))
        log.info("AirDrop from %s: saved %s", pending.sender,
                 ", ".join(p.name for p in written))
        if self.events.on_complete:
            self.events.on_complete(pending.sender, written)
        return True

    async def _extract(self, reader, headers, limit: int,
                       pending: _Accepted) -> list[Path]:
        """Stream the body through DVZip/gzip and cpio onto disk.

        Members are flattened to their base names (iOS wraps a transfer
        in an NSIRD_* directory), AppleDouble "._" companions and
        anything that isn't a regular file are skipped, and on any error
        every file this upload created is removed again.
        """
        self.download_dir.mkdir(parents=True, exist_ok=True)
        dest_dir = self.download_dir.resolve()
        decoder = BodyDecoder(limit)
        cpio = CpioReader(max_members=MAX_FILES * 4)
        total = pending.declared or int(headers.get("TotalBytes") or 0)
        done = 0
        created: list[Path] = []
        handle = None
        last_report = 0.0

        def consume(chunks: list[bytes]) -> None:
            nonlocal handle, done
            for chunk in chunks:
                for kind, value in cpio.feed(chunk):
                    if kind == "entry":
                        if value.size > limit - done:
                            raise ArchiveTooLarge(
                                f"{value.name!r} declares {value.size} bytes, "
                                f"more than this transfer may take")
                        base = value.name.replace("\\", "/").rsplit("/", 1)[-1]
                        if value.is_file and not base.startswith("._"):
                            path, handle = open_unique(dest_dir, safe_name(base))
                            created.append(path)
                    elif kind == "data" and handle is not None:
                        handle.write(value)
                        done += len(value)
                    elif kind == "end" and handle is not None:
                        handle.close()
                        handle = None

        try:
            async for piece in iter_body(reader, headers, timeout=IDLE_TIMEOUT,
                                         limit=limit):
                consume(decoder.feed(piece))
                now = time.monotonic()
                if self.events.on_progress and now - last_report > 0.25:
                    last_report = now
                    self.events.on_progress(pending.sender, done,
                                            max(total, done))
            consume(decoder.finish())
            cpio.finish()
        except BaseException:
            if handle is not None:
                handle.close()
            for path in created:
                path.unlink(missing_ok=True)
            raise
        if self.events.on_progress:
            self.events.on_progress(pending.sender, done, max(total, done))
        return created
