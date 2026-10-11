"""Nearby Connections state machine: inbound (receive) and outbound (send).

Layering, bottom to top:

1. TCP with 4-byte big-endian length-prefixed frames.
2. Plaintext phase: ConnectionRequest OfflineFrame, then raw Ukey2Message
   frames (see crypto.py), then ConnectionResponse OfflineFrames both ways.
3. Encrypted phase: every frame is a SecureMessage wrapping one
   OfflineFrame (KEEP_ALIVE / PAYLOAD_TRANSFER / DISCONNECTION).
4. Sharing layer: sharing.nearby Frames (PairedKeyEncryption,
   PairedKeyResult, Introduction, Response, Cancel) travel as BYTES
   payloads inside PAYLOAD_TRANSFER frames; actual file bytes travel as
   FILE payloads, chunked.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import random
import re
import shutil
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from ..proto import offline_wire_formats_pb2 as ow
from ..proto import wire_format_pb2 as wf
from .crypto import D2DCipher, HandshakeError, Ukey2Client, Ukey2Server

log = logging.getLogger("nearshare.connection")

CHUNK_SIZE = 512 * 1024
KEEP_ALIVE_INTERVAL = 10.0
KEEP_ALIVE_TIMEOUT = 30.0

# Sharing-layer control frames (Introduction, PairedKey*, text shares,
# progress/cancel control) are all small by construction; the only ones
# that legitimately run to any size are text shares, still nowhere near
# this. Without a cap a peer that never sets LAST_CHUNK turns
# _recv_sharing_frame's / _receive_payloads' accumulation buffers into
# an unbounded memory-exhaustion primitive (each individual frame is
# already capped at 100 MiB by read_frame, but nothing stopped an
# unlimited *number* of them piling into the same buffer).
MAX_SHARING_FRAME_SIZE = 64 * 1024 * 1024


# ---------------------------------------------------------------- framing

_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")


def safe_filename(name: str, fallback: str = "received-file") -> str:
    """Reduce a peer-supplied filename to a bare, safe basename.

    The name in an IntroductionFrame is entirely attacker-controlled. Used
    as-is it is an arbitrary-file-write primitive: Path.__truediv__ with an
    absolute path ("/etc/cron.d/evil") discards the download directory
    completely, and "../" segments walk out of it. Take the final path
    component only, drop separators and control characters, and refuse the
    specials "." and "..".
    """
    # Cut on both separators: a Windows or Android peer may send either.
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE_FILENAME_CHARS.sub("_", base).strip()
    while base.startswith("."):
        base = base[1:]          # no hidden files, no "." / ".."
    base = base.strip()
    if not base:
        return fallback
    return base[:200]            # keep well under NAME_MAX


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(4)
    length = int.from_bytes(header, "big")
    if length > 100 * 1024 * 1024:
        raise HandshakeError(f"absurd frame length {length}")
    return await reader.readexactly(length)


def write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(len(data).to_bytes(4, "big") + data)


# Flags for creating a received file: ours alone (O_EXCL), never through
# a symlink planted in the download directory (O_NOFOLLOW, POSIX only),
# and byte-for-byte (O_BINARY: without it Windows opens the descriptor in
# text mode and every "\n" written becomes "\r\n").
NEW_FILE_FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                  getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))


def random_endpoint_id() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=4))


def enough_free_space(download_dir: Path, total_size: int) -> bool:
    """False if total_size (sum of peer-declared file sizes, entirely
    attacker-controlled) would not fit in download_dir's free space.

    Best-effort: if we can't even stat the filesystem, don't block what
    might be a perfectly legitimate transfer over it.
    """
    if total_size <= 0:
        return True
    try:
        download_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(download_dir).free
    except OSError:
        return True
    return total_size <= free


def build_endpoint_info(device_name: str, device_type: int = 3) -> bytes:
    """Endpoint info blob: flags byte (device type in bits 1-3), 16 random
    bytes, then length-prefixed UTF-8 device name. device_type 3 = laptop."""
    name = device_name.encode("utf-8")[:255]
    return bytes([device_type << 1]) + os.urandom(16) + bytes([len(name)]) + name


def parse_endpoint_info(info: bytes) -> tuple[str, int]:
    """Return (device_name, device_type) from an endpoint info blob."""
    device_type = (info[0] >> 1) & 0x07 if info else 0
    name = "Unknown device"
    if len(info) > 18:
        n = info[17]
        raw = info[18:18 + n]
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = None
        if decoded:
            # This name is attacker-controlled and flows straight into
            # terminal output (cli.py) and the names.py cache with no
            # further sanitisation downstream. Strip control characters
            # here at the source so a peer can't smuggle ANSI/terminal
            # escape sequences or embedded newlines into either.
            cleaned = _UNSAFE_FILENAME_CHARS.sub("", decoded).strip()
            if cleaned:
                name = cleaned
    return name, device_type


# ---------------------------------------------------------------- model

@dataclass
class FileOffer:
    payload_id: int
    name: str
    size: int
    mime_type: str
    dest_path: Path | None = None
    received: int = 0
    handle: object = None  # open file object while receiving


@dataclass
class TransferRequest:
    """What the UI shows when a peer wants to send us something."""
    device_name: str
    pin: str
    files: list[FileOffer]
    text_preview: str | None = None

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class Events:
    """Callbacks the daemon/UI layer plugs in. All optional but
    on_transfer_request must be set for inbound connections."""
    # Return True to accept, False to decline.
    on_transfer_request: Callable[[TransferRequest], Awaitable[bool]] | None = None
    on_progress: Callable[[str, int, int], None] | None = None  # device, done, total
    on_complete: Callable[[str, list[Path]], None] | None = None
    on_error: Callable[[str, str], None] | None = None
    on_text: Callable[[str, str], None] | None = None  # device, text


class _ConnectionBase:
    """Shared plumbing for both directions once encryption is up."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, events: Events) -> None:
        self.reader = reader
        self.writer = writer
        self.events = events
        self.cipher: D2DCipher | None = None
        self.peer_name = "Unknown device"
        self._closed = False

    async def _send_plain(self, frame: ow.OfflineFrame) -> None:
        write_frame(self.writer, frame.SerializeToString())
        await self.writer.drain()

    async def _send_encrypted(self, frame: ow.OfflineFrame) -> None:
        assert self.cipher is not None
        write_frame(self.writer, self.cipher.encrypt(frame.SerializeToString()))
        await self.writer.drain()

    async def _recv_encrypted(self) -> ow.OfflineFrame:
        """Read next encrypted OfflineFrame, transparently answering
        keep-alives."""
        assert self.cipher is not None
        while True:
            raw = await asyncio.wait_for(read_frame(self.reader),
                                         KEEP_ALIVE_TIMEOUT)
            frame = ow.OfflineFrame()
            frame.ParseFromString(self.cipher.decrypt(raw))
            ftype = frame.v1.type
            if ftype == ow.V1Frame.KEEP_ALIVE:
                if not frame.v1.keep_alive.ack:
                    ka = ow.OfflineFrame()
                    ka.version = ow.OfflineFrame.V1
                    ka.v1.type = ow.V1Frame.KEEP_ALIVE
                    ka.v1.keep_alive.ack = True
                    await self._send_encrypted(ka)
                continue
            if ftype == ow.V1Frame.DISCONNECTION:
                raise ConnectionResetError("peer disconnected")
            return frame

    async def _send_sharing_frame(self, sharing: wf.Frame) -> None:
        """Sharing-layer frames ride as a BYTES payload: one data chunk
        plus an empty LAST_CHUNK marker."""
        body = sharing.SerializeToString()
        payload_id = random.randint(1, 2**62)  # positive: Android rejects negative ids

        first = ow.OfflineFrame()
        first.version = ow.OfflineFrame.V1
        first.v1.type = ow.V1Frame.PAYLOAD_TRANSFER
        pt = first.v1.payload_transfer
        pt.packet_type = ow.PayloadTransferFrame.DATA
        pt.payload_header.id = payload_id
        pt.payload_header.type = ow.PayloadTransferFrame.PayloadHeader.BYTES
        pt.payload_header.total_size = len(body)
        pt.payload_chunk.offset = 0
        pt.payload_chunk.flags = 0
        pt.payload_chunk.body = body
        await self._send_encrypted(first)

        last = ow.OfflineFrame()
        last.CopyFrom(first)
        lpt = last.v1.payload_transfer
        lpt.payload_chunk.offset = len(body)
        lpt.payload_chunk.flags = ow.PayloadTransferFrame.PayloadChunk.LAST_CHUNK
        lpt.payload_chunk.ClearField("body")
        await self._send_encrypted(last)

    async def _send_keep_alives(self) -> None:
        """Background task: ping every KEEP_ALIVE_INTERVAL seconds."""
        try:
            while not self._closed:
                await asyncio.sleep(KEEP_ALIVE_INTERVAL)
                ka = ow.OfflineFrame()
                ka.version = ow.OfflineFrame.V1
                ka.v1.type = ow.V1Frame.KEEP_ALIVE
                ka.v1.keep_alive.ack = False
                await self._send_encrypted(ka)
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def _recv_sharing_frame(self) -> wf.Frame:
        """Accumulate BYTES payload chunks until a complete sharing Frame."""
        buffers: dict[int, bytearray] = {}
        while True:
            frame = await self._recv_encrypted()
            if frame.v1.type != ow.V1Frame.PAYLOAD_TRANSFER:
                continue
            pt = frame.v1.payload_transfer
            if pt.payload_header.type != ow.PayloadTransferFrame.PayloadHeader.BYTES:
                continue
            buf = buffers.setdefault(pt.payload_header.id, bytearray())
            if len(buf) + len(pt.payload_chunk.body) > MAX_SHARING_FRAME_SIZE:
                # A well-behaved peer never sends a control/introduction
                # frame anywhere near this size; a peer that just keeps
                # sending DATA chunks without LAST_CHUNK is trying to
                # grow this buffer without bound.
                raise HandshakeError("sharing-layer payload too large")
            buf.extend(pt.payload_chunk.body)
            if pt.payload_chunk.flags & ow.PayloadTransferFrame.PayloadChunk.LAST_CHUNK:
                sharing = wf.Frame()
                sharing.ParseFromString(bytes(buffers.pop(pt.payload_header.id)))
                return sharing

    async def close(self, send_disconnect: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if send_disconnect and self.cipher is not None:
                frame = ow.OfflineFrame()
                frame.version = ow.OfflineFrame.V1
                frame.v1.type = ow.V1Frame.DISCONNECTION
                await self._send_encrypted(frame)
        except ConnectionError:
            pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


# ---------------------------------------------------------------- inbound

class InboundConnection(_ConnectionBase):
    """Handles one incoming sender (an Android phone sharing to us)."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, events: Events,
                 device_name: str, download_dir: Path) -> None:
        super().__init__(reader, writer, events)
        self.device_name = device_name
        self.download_dir = download_dir

    async def run(self) -> None:
        keep_alive_task: asyncio.Task | None = None
        try:
            await self._handshake()
            keep_alive_task = asyncio.create_task(self._send_keep_alives())
            await self._sharing_phase()
        except (ConnectionError, asyncio.IncompleteReadError,
                asyncio.TimeoutError, HandshakeError) as exc:
            log.info("inbound connection ended: %s", exc)
            if self.events.on_error:
                self.events.on_error(self.peer_name, str(exc))
        finally:
            if keep_alive_task:
                keep_alive_task.cancel()
            await self.close(send_disconnect=False)

    async def _handshake(self) -> None:
        # 1. Plaintext connection request tells us who is knocking.
        raw = await read_frame(self.reader)
        frame = ow.OfflineFrame()
        frame.ParseFromString(raw)
        if frame.v1.type != ow.V1Frame.CONNECTION_REQUEST:
            raise HandshakeError("expected CONNECTION_REQUEST")
        self.peer_name, _ = parse_endpoint_info(
            frame.v1.connection_request.endpoint_info)
        # The sender just told us its real user-set name; remember it so
        # future discovery can label this device properly even when its
        # advertisements hide the name (contact-mode visibility).
        peer_addr = self.writer.get_extra_info("peername")
        if peer_addr:
            from . import names
            names.remember(peer_addr[0], self.peer_name)

        # 2. UKEY2: client init -> server init -> client finish.
        server = Ukey2Server()
        m1 = await read_frame(self.reader)
        m2 = server.handle_client_init(m1)
        write_frame(self.writer, m2)
        await self.writer.drain()
        m3 = await read_frame(self.reader)
        keys = server.handle_client_finish(m3)
        self.cipher = D2DCipher(keys)

        # 3. Plaintext connection responses cross both ways.
        resp = ow.OfflineFrame()
        resp.version = ow.OfflineFrame.V1
        resp.v1.type = ow.V1Frame.CONNECTION_RESPONSE
        resp.v1.connection_response.status = 0
        resp.v1.connection_response.response = ow.ConnectionResponseFrame.ACCEPT
        resp.v1.connection_response.os_info.type = ow.OsInfo.LINUX
        await self._send_plain(resp)
        peer_resp = ow.OfflineFrame()
        peer_resp.ParseFromString(await read_frame(self.reader))
        if (peer_resp.v1.type != ow.V1Frame.CONNECTION_RESPONSE or
                peer_resp.v1.connection_response.response !=
                ow.ConnectionResponseFrame.ACCEPT):
            raise HandshakeError("peer rejected connection")

        # 4. Paired-key dance. We hold no Google account certificates, so
        # send random signed data and report UNABLE, like NearDrop does.
        # The phone then falls back to PIN-based verification.
        theirs = await self._recv_sharing_frame()
        if theirs.v1.type != wf.V1Frame.PAIRED_KEY_ENCRYPTION:
            raise HandshakeError("expected PAIRED_KEY_ENCRYPTION")
        pke = wf.Frame()
        pke.version = wf.Frame.V1
        pke.v1.type = wf.V1Frame.PAIRED_KEY_ENCRYPTION
        pke.v1.paired_key_encryption.signed_data = os.urandom(72)
        pke.v1.paired_key_encryption.secret_id_hash = os.urandom(6)
        await self._send_sharing_frame(pke)

        their_result = await self._recv_sharing_frame()
        if their_result.v1.type != wf.V1Frame.PAIRED_KEY_RESULT:
            raise HandshakeError("expected PAIRED_KEY_RESULT")
        pkr = wf.Frame()
        pkr.version = wf.Frame.V1
        pkr.v1.type = wf.V1Frame.PAIRED_KEY_RESULT
        pkr.v1.paired_key_result.status = wf.PairedKeyResultFrame.UNABLE
        await self._send_sharing_frame(pkr)

    async def _sharing_phase(self) -> None:
        intro = await self._recv_sharing_frame()
        if intro.v1.type != wf.V1Frame.INTRODUCTION:
            raise HandshakeError("expected INTRODUCTION")

        # Sanitise at the edge: the name is shown in the accept dialog and
        # used for the destination, so the user must be shown exactly the
        # name that will be written, never a crafted path. Sizes are
        # peer-declared too; clamp negatives to 0 so a crafted negative
        # size can't be used to offset a huge one elsewhere in the same
        # Introduction and slip an oversized transfer past the disk-space
        # check below (each file's writes are separately capped at its
        # own clamped size regardless, but the aggregate check should not
        # be foolable either).
        offers = [FileOffer(payload_id=fm.payload_id,
                            name=safe_filename(fm.name),
                            size=max(fm.size, 0), mime_type=fm.mime_type)
                  for fm in intro.v1.introduction.file_metadata]
        texts = list(intro.v1.introduction.text_metadata)
        # Only payload ids declared as text in the introduction are text
        # shares; any other BYTES payload during transfer is a sharing-layer
        # control frame (progress, cancel) and must NOT reach the user.
        text_ids = {tm.payload_id for tm in texts}
        request = TransferRequest(
            device_name=self.peer_name,
            pin=self.cipher.pin,
            files=offers,
            text_preview=texts[0].text_title if texts else None,
        )

        # A hostile peer can declare any total size it likes (there is no
        # upper bound in the protocol); a petabyte claim would otherwise
        # only be caught once the disk actually filled up mid-transfer.
        # Refuse before ever bothering the human with an accept dialog.
        if not enough_free_space(self.download_dir, request.total_size):
            log.warning("declined transfer from %s: declared size %d "
                       "exceeds free space", self.peer_name,
                       request.total_size)
            accepted = False
        else:
            # The sender's phone shows a spinner while we decide; give the
            # human 60s before declining, so the peer isn't left hanging
            # forever if the dialog goes unnoticed.
            accepted = False
            if self.events.on_transfer_request:
                try:
                    accepted = await asyncio.wait_for(
                        self.events.on_transfer_request(request), timeout=60)
                except asyncio.TimeoutError:
                    accepted = False

        resp = wf.Frame()
        resp.version = wf.Frame.V1
        resp.v1.type = wf.V1Frame.RESPONSE
        resp.v1.connection_response.status = (
            wf.ConnectionResponseFrame.ACCEPT if accepted
            else wf.ConnectionResponseFrame.REJECT)
        await self._send_sharing_frame(resp)
        if not accepted:
            return

        await self._receive_payloads(request, text_ids=text_ids)

    def _open_dest(self, offer: FileOffer) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        download_dir = self.download_dir.resolve()
        dest = download_dir / safe_filename(offer.name)
        stem, suffix = dest.stem, dest.suffix
        counter = 1
        while dest.exists():
            dest = download_dir / f"{stem} ({counter}){suffix}"
            counter += 1
        # Defence in depth: even with a sanitised name, never open a path
        # that escaped the download directory.
        if dest.parent.resolve() != download_dir:
            raise ValueError(f"refusing to write outside {download_dir}")
        offer.dest_path = dest
        # O_EXCL|O_NOFOLLOW: create it ourselves and never follow a
        # symlink planted in the download directory (a dangling symlink
        # reports exists() == False, so the loop above would not catch
        # it and a plain open() would write through it).
        fd = os.open(dest, NEW_FILE_FLAGS, 0o600)
        offer.handle = os.fdopen(fd, "wb")

    async def _receive_payloads(self, request: TransferRequest,
                                text_ids: set[int]) -> None:
        by_payload = {f.payload_id: f for f in request.files}
        byte_buffers: dict[int, bytearray] = {}
        total = request.total_size
        done = 0
        pending_files = set(by_payload)
        pending_texts = set(text_ids)

        try:
            while pending_files or pending_texts:
                frame = await self._recv_encrypted()
                if frame.v1.type != ow.V1Frame.PAYLOAD_TRANSFER:
                    continue
                pt = frame.v1.payload_transfer
                header, chunk = pt.payload_header, pt.payload_chunk
                is_last = bool(chunk.flags &
                               ow.PayloadTransferFrame.PayloadChunk.LAST_CHUNK)

                if header.type == ow.PayloadTransferFrame.PayloadHeader.FILE:
                    offer = by_payload.get(header.id)
                    if offer is None or header.id not in pending_files:
                        continue  # unknown or already-completed payload
                    if offer.handle is None:
                        self._open_dest(offer)
                    if chunk.body:
                        # offset and declared size are both peer-controlled.
                        # A huge offset with a tiny body is a sparse-file
                        # attack (a few bytes on the wire, an
                        # apparently-huge file on disk); writing past the
                        # size the human was actually shown in the accept
                        # dialog is the same disk-exhaustion primitive the
                        # up-front free-space check exists to stop, just
                        # arriving one payload at a time instead of via an
                        # inflated total. Refuse either.
                        end = chunk.offset + len(chunk.body)
                        if chunk.offset < 0 or end > offer.size:
                            raise HandshakeError(
                                f"payload {header.id}: chunk [{chunk.offset}:"
                                f"{end}) exceeds declared size {offer.size}")
                        # Chunks arrive in order per payload, but guard
                        # against retransmits by writing at the offset.
                        if chunk.offset != offer.received:
                            offer.handle.seek(chunk.offset)
                            offer.received = chunk.offset
                        offer.handle.write(chunk.body)
                        offer.received += len(chunk.body)
                        done = sum(f.received for f in request.files)
                        if self.events.on_progress:
                            self.events.on_progress(self.peer_name, done,
                                                    total)
                    if is_last:
                        offer.handle.close()
                        offer.handle = None
                        pending_files.discard(header.id)
                elif header.type == ow.PayloadTransferFrame.PayloadHeader.BYTES:
                    buf = byte_buffers.setdefault(header.id, bytearray())
                    if len(buf) + len(chunk.body) > MAX_SHARING_FRAME_SIZE:
                        raise HandshakeError(
                            f"payload {header.id}: BYTES payload too large")
                    buf.extend(chunk.body)
                    if not is_last:
                        continue
                    data = bytes(byte_buffers.pop(header.id))
                    if header.id in text_ids:
                        self._deliver_text(data)
                        pending_texts.discard(header.id)
                    elif self._is_cancel_frame(data):
                        log.info("sender cancelled the transfer")
                        self._discard_partials(request)
                        return
                    # Any other BYTES payload is a control frame
                    # (progress updates etc.) — consumed silently.
        except BaseException:
            self._discard_partials(request)
            raise

        if self.events.on_complete:
            self.events.on_complete(
                self.peer_name,
                [f.dest_path for f in request.files if f.dest_path])

    @staticmethod
    def _is_cancel_frame(data: bytes) -> bool:
        try:
            sharing = wf.Frame()
            sharing.ParseFromString(data)
            return sharing.v1.type == wf.V1Frame.CANCEL
        except Exception:
            return False

    def _discard_partials(self, request: TransferRequest) -> None:
        """Close and delete files whose payloads never completed."""
        for offer in request.files:
            if offer.handle is not None:
                offer.handle.close()
                offer.handle = None
                if offer.dest_path is not None:
                    offer.dest_path.unlink(missing_ok=True)
                    offer.dest_path = None

    def _deliver_text(self, data: bytes) -> None:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return
        if self.events.on_text:
            self.events.on_text(self.peer_name, text)


# ---------------------------------------------------------------- outbound

class OutboundConnection(_ConnectionBase):
    """Sends files to a discovered peer (Android phone, or another
    machine running a Nearby Share receiver)."""

    def __init__(self, host: str, port: int, events: Events,
                 device_name: str, files: list[Path],
                 endpoint_id: str | None = None,
                 peer_name: str | None = None) -> None:
        # reader/writer attached in run() after connecting
        self.host = host
        self.port = port
        self.device_name = device_name
        self.files = [Path(f) for f in files]
        self.endpoint_id = endpoint_id or random_endpoint_id()
        self._events = events
        self._peer_name = peer_name  # from mDNS discovery, if known
        self.pin: str | None = None

    async def run(self) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        super().__init__(reader, writer, self._events)
        if self._peer_name:
            self.peer_name = self._peer_name
        keep_alive_task: asyncio.Task | None = None
        try:
            await self._handshake()
            keep_alive_task = asyncio.create_task(self._send_keep_alives())
            await self._share()
            if self.events.on_complete:
                self.events.on_complete(self.peer_name, self.files)
        except (ConnectionError, asyncio.IncompleteReadError,
                asyncio.TimeoutError, HandshakeError) as exc:
            log.info("outbound connection failed: %s", exc)
            if self.events.on_error:
                self.events.on_error(self.peer_name, str(exc))
            raise
        finally:
            if keep_alive_task:
                keep_alive_task.cancel()
            await self.close()

    async def _handshake(self) -> None:
        req = ow.OfflineFrame()
        req.version = ow.OfflineFrame.V1
        req.v1.type = ow.V1Frame.CONNECTION_REQUEST
        cr = req.v1.connection_request
        cr.endpoint_id = self.endpoint_id
        cr.endpoint_name = self.device_name
        cr.endpoint_info = build_endpoint_info(self.device_name)
        cr.mediums.append(ow.ConnectionRequestFrame.WIFI_LAN)
        await self._send_plain(req)

        client = Ukey2Client()
        write_frame(self.writer, client.client_init())
        await self.writer.drain()
        m2 = await read_frame(self.reader)
        m3, keys = client.handle_server_init(m2)
        write_frame(self.writer, m3)
        await self.writer.drain()
        self.cipher = D2DCipher(keys)
        self.pin = keys.pin

        resp = ow.OfflineFrame()
        resp.version = ow.OfflineFrame.V1
        resp.v1.type = ow.V1Frame.CONNECTION_RESPONSE
        resp.v1.connection_response.status = 0
        resp.v1.connection_response.response = ow.ConnectionResponseFrame.ACCEPT
        resp.v1.connection_response.os_info.type = ow.OsInfo.LINUX
        await self._send_plain(resp)
        peer_resp = ow.OfflineFrame()
        peer_resp.ParseFromString(await read_frame(self.reader))
        if (peer_resp.v1.type != ow.V1Frame.CONNECTION_RESPONSE or
                peer_resp.v1.connection_response.response !=
                ow.ConnectionResponseFrame.ACCEPT):
            raise HandshakeError("peer rejected connection")

        pke = wf.Frame()
        pke.version = wf.Frame.V1
        pke.v1.type = wf.V1Frame.PAIRED_KEY_ENCRYPTION
        pke.v1.paired_key_encryption.signed_data = os.urandom(72)
        pke.v1.paired_key_encryption.secret_id_hash = os.urandom(6)
        await self._send_sharing_frame(pke)
        theirs = await self._recv_sharing_frame()
        if theirs.v1.type != wf.V1Frame.PAIRED_KEY_ENCRYPTION:
            raise HandshakeError("expected PAIRED_KEY_ENCRYPTION")

        pkr = wf.Frame()
        pkr.version = wf.Frame.V1
        pkr.v1.type = wf.V1Frame.PAIRED_KEY_RESULT
        pkr.v1.paired_key_result.status = wf.PairedKeyResultFrame.UNABLE
        await self._send_sharing_frame(pkr)
        their_result = await self._recv_sharing_frame()
        if their_result.v1.type != wf.V1Frame.PAIRED_KEY_RESULT:
            raise HandshakeError("expected PAIRED_KEY_RESULT")

    async def _share(self) -> None:
        intro = wf.Frame()
        intro.version = wf.Frame.V1
        intro.v1.type = wf.V1Frame.INTRODUCTION
        payload_ids: list[tuple[Path, int]] = []
        for path in self.files:
            payload_id = random.randint(1, 2**62)  # positive: Android rejects negative ids
            payload_ids.append((path, payload_id))
            fm = intro.v1.introduction.file_metadata.add()
            fm.name = path.name
            fm.payload_id = payload_id
            fm.size = path.stat().st_size
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            fm.mime_type = mime
            fm.type = self._file_type(mime)
        await self._send_sharing_frame(intro)

        # Receiver's human taps accept (they verify our self.pin on screen).
        resp = await self._recv_sharing_frame()
        if resp.v1.type != wf.V1Frame.RESPONSE:
            raise HandshakeError("expected RESPONSE")
        if (resp.v1.connection_response.status !=
                wf.ConnectionResponseFrame.ACCEPT):
            raise HandshakeError("receiver declined the transfer")

        total = sum(p.stat().st_size for p, _ in payload_ids)
        done = 0
        for path, payload_id in payload_ids:
            done = await self._send_file(path, payload_id, done, total)
        await self._await_receiver_wrapup()

    async def _await_receiver_wrapup(self) -> None:
        """Linger after the last chunk instead of hanging up immediately.

        Some Android builds mark an incoming transfer FAILED if the
        sender disconnects the instant the final chunk is written —
        the receiver is still flushing and verifying. Stay on the line
        until the peer disconnects (normal) or a quiet timeout passes,
        answering keep-alives the whole time via _recv_encrypted."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while (remaining := deadline - loop.time()) > 0:
            try:
                await asyncio.wait_for(self._recv_encrypted(),
                                       timeout=remaining)
            except (asyncio.TimeoutError, ConnectionResetError,
                    asyncio.IncompleteReadError):
                return  # peer hung up or has gone quiet: we're done

    @staticmethod
    def _file_type(mime: str) -> int:
        if mime.startswith("image/"):
            return wf.FileMetadata.IMAGE
        if mime.startswith("video/"):
            return wf.FileMetadata.VIDEO
        if mime.startswith("audio/"):
            return wf.FileMetadata.AUDIO
        if mime == "application/vnd.android.package-archive":
            return wf.FileMetadata.ANDROID_APP
        return wf.FileMetadata.UNKNOWN

    async def _send_file(self, path: Path, payload_id: int,
                         done: int, total: int) -> int:
        size = path.stat().st_size
        offset = 0
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                frame = self._file_chunk_frame(payload_id, size, offset,
                                               chunk, path.name, last=False)
                await self._send_encrypted(frame)
                offset += len(chunk)
                done += len(chunk)
                if self.events.on_progress:
                    self.events.on_progress(self.peer_name, done, total)
        last = self._file_chunk_frame(payload_id, size, offset, b"",
                                      path.name, last=True)
        await self._send_encrypted(last)
        return done

    @staticmethod
    def _file_chunk_frame(payload_id: int, size: int, offset: int,
                          body: bytes, name: str, last: bool) -> ow.OfflineFrame:
        frame = ow.OfflineFrame()
        frame.version = ow.OfflineFrame.V1
        frame.v1.type = ow.V1Frame.PAYLOAD_TRANSFER
        pt = frame.v1.payload_transfer
        pt.packet_type = ow.PayloadTransferFrame.DATA
        pt.payload_header.id = payload_id
        pt.payload_header.type = ow.PayloadTransferFrame.PayloadHeader.FILE
        pt.payload_header.total_size = size
        pt.payload_header.file_name = name
        pt.payload_chunk.offset = offset
        pt.payload_chunk.flags = (
            ow.PayloadTransferFrame.PayloadChunk.LAST_CHUNK if last else 0)
        if body:
            pt.payload_chunk.body = body
        return frame
