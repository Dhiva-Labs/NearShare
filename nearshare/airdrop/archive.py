"""AirDrop's transfer container: a cpio archive, compressed for the wire.

An /Upload body is one cpio archive holding every file of the transfer.
Apple writes the portable ASCII ("odc", magic ``070707``) variant and, on
iOS 26 and macOS 15, wraps it in DVZip (``application/x-dvzip``): a run of
blocks, each a 4-byte big-endian header followed by its payload. The low
31 bits of the header are the payload length; the high bit set means the
payload is stored as-is, clear means it is one complete zlib stream.
Apple's blocks each inflate to 128 KiB, except the last. Older senders
(OpenDrop) post ``application/x-cpio`` instead, gzip-compressed or raw.

Everything here streams. The sender produces the archive file by file,
so a 4 GB video is never held in memory, and the receiver decodes the
body as it arrives and hands file data to the caller block by block.
The receiving side is fed entirely by a network peer, so every size it
parses is bounded before anything is allocated on the strength of it.
"""

from __future__ import annotations

import os
import stat
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

ODC_MAGIC = b"070707"
NEWC_MAGIC = b"070701"
CRC_MAGIC = b"070702"
TRAILER = "TRAILER!!!"

_ODC_HEADER = 76            # magic + 8 x 6 octal + 11 octal + 6 octal + 11 octal
_NEWC_HEADER = 110          # magic + 13 x 8 hex
ODC_MAX_SIZE = 0o77777777777  # 11 octal digits: 8 GiB - 1

DVZIP_BLOCK = 128 * 1024    # what Apple's blocks inflate to
_STORED = 0x80000000

# Ceilings on what a peer may make us parse. A name longer than this is
# not a file name; a compressed block bigger than this is not one of
# Apple's 128 KiB blocks.
MAX_NAME_BYTES = 4096
MAX_DVZIP_BLOCK = 16 * 1024 * 1024


class ArchiveError(ValueError):
    """The body is not a well-formed, bounded AirDrop archive."""


class ArchiveTooLarge(ArchiveError):
    """The body inflates past the limit the receiver allowed."""


# ------------------------------------------------------------------ writing

def odc_header(name: str, size: int, mode: int, mtime: int = 0,
               ino: int = 0) -> bytes:
    raw_name = name.encode("utf-8") + b"\0"
    if size > ODC_MAX_SIZE:
        raise ArchiveError(f"{name}: {size} bytes is too large for cpio")
    return b"".join((
        ODC_MAGIC,
        b"%06o" % 0,                 # dev
        b"%06o" % (ino & 0o777777),
        b"%06o" % (mode & 0o777777),
        b"%06o" % 0,                 # uid
        b"%06o" % 0,                 # gid
        b"%06o" % 1,                 # nlink
        b"%06o" % 0,                 # rdev
        b"%011o" % max(0, mtime),
        b"%06o" % len(raw_name),
        b"%011o" % size,
        raw_name,
    ))


def cpio_stream(files: Iterable[tuple[str, Path]],
                chunk: int = 64 * 1024) -> Iterator[bytes]:
    """Yield an odc cpio archive of (archive name, path) pairs.

    Each file is read exactly to the size it had when its header was
    written: a file that shrinks underneath us would otherwise leave the
    archive misaligned, so that is an error rather than silent garbage.
    """
    for ino, (arcname, path) in enumerate(files, start=1):
        st = path.stat()
        size = st.st_size
        yield odc_header(arcname, size, stat.S_IFREG | 0o644,
                         int(st.st_mtime), ino)
        left = size
        with open(path, "rb") as fh:
            while left:
                data = fh.read(min(chunk, left))
                if not data:
                    raise ArchiveError(f"{path} shrank while being sent")
                left -= len(data)
                yield data
    yield odc_header(TRAILER, 0, 0)


def dvzip_stream(source: Iterable[bytes], block: int = DVZIP_BLOCK,
                 level: int = 6) -> Iterator[bytes]:
    """Re-block a byte stream into DVZip frames.

    A block zlib does not shrink (JPEG, HEIC, video: most of what people
    AirDrop) goes stored, as sharingd does, so the receiver spends no time
    inflating it.
    """
    pending = bytearray()

    def frame(raw: bytes) -> bytes:
        packed = zlib.compress(raw, level)
        if len(packed) < len(raw):
            return len(packed).to_bytes(4, "big") + packed
        return (_STORED | len(raw)).to_bytes(4, "big") + raw

    for data in source:
        pending += data
        while len(pending) >= block:
            yield frame(bytes(pending[:block]))
            del pending[:block]
    if pending:
        yield frame(bytes(pending))


# ------------------------------------------------------------------ reading

class BodyDecoder:
    """Turn /Upload body bytes into cpio bytes, incrementally.

    The container is recognised from the first bytes rather than trusted
    from Content-Type: gzip (``1f 8b``), raw cpio (``0707``), or DVZip
    blocks. ``limit`` caps the inflated size; exceeding it raises
    ArchiveTooLarge before the excess is ever produced in memory.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.produced = 0
        self._mode: str | None = None
        self._head = bytearray()           # bytes seen before the mode is known
        self._gzip: "zlib._Decompress | None" = None
        # DVZip state: the current block's header value and bytes left.
        self._hdr = bytearray()
        self._left = 0
        self._stored = False
        self._block: "zlib._Decompress | None" = None

    def _emit(self, out: list[bytes], data: bytes) -> None:
        if not data:
            return
        self.produced += len(data)
        if self.produced > self.limit:
            raise ArchiveTooLarge(
                f"archive inflates past the {self.limit}-byte limit")
        out.append(data)

    def _inflate(self, decoder, data: bytes, out: list[bytes]) -> None:
        # Bounded output per call: a few compressed bytes can expand to a
        # great deal, so never ask zlib for more than we may still accept.
        while data:
            room = self.limit - self.produced + 1
            chunk = decoder.decompress(data, min(room, 1 << 20))
            self._emit(out, chunk)
            data = decoder.unconsumed_tail

    def feed(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        if self._mode is None:
            self._head += data
            if len(self._head) < 4:
                return out
            head = bytes(self._head)
            self._head.clear()
            if head[:2] == b"\x1f\x8b":
                self._mode = "gzip"
                self._gzip = zlib.decompressobj(31)
            elif head[:4] == b"0707":
                self._mode = "raw"
            else:
                self._mode = "dvzip"
            data = head
        if self._mode == "raw":
            self._emit(out, data)
        elif self._mode == "gzip":
            if self._gzip.eof:
                if data:
                    raise ArchiveError("gzip body has trailing data")
                return out
            self._inflate(self._gzip, data, out)
            if self._gzip.unused_data:
                raise ArchiveError("gzip body has trailing data")
        else:
            self._feed_dvzip(memoryview(data), out)
        return out

    def _feed_dvzip(self, data: memoryview, out: list[bytes]) -> None:
        while data:
            if self._left == 0 and self._block is None:
                need = 4 - len(self._hdr)
                self._hdr += data[:need]
                data = data[need:]
                if len(self._hdr) < 4:
                    return
                value = int.from_bytes(self._hdr, "big")
                self._hdr.clear()
                self._stored = bool(value & _STORED)
                self._left = value & ~_STORED
                if self._left == 0 or self._left > MAX_DVZIP_BLOCK:
                    raise ArchiveError(
                        f"implausible DVZip block of {self._left} bytes")
                if not self._stored:
                    self._block = zlib.decompressobj()
                continue
            take = bytes(data[:self._left])
            data = data[len(take):]
            self._left -= len(take)
            if self._stored:
                self._emit(out, take)
            else:
                self._inflate(self._block, take, out)
                if self._block.unused_data:
                    raise ArchiveError("DVZip block has trailing data")
                if self._left == 0:
                    if not self._block.eof:
                        raise ArchiveError("truncated DVZip block")
                    self._block = None

    def finish(self) -> list[bytes]:
        """Validate that the body ended on a container boundary."""
        out: list[bytes] = []
        if self._mode is None:
            if self._head:
                raise ArchiveError("body too short to be an archive")
            return out
        if self._mode == "gzip":
            self._emit(out, self._gzip.flush())
            if not self._gzip.eof:
                raise ArchiveError("truncated gzip body")
        elif self._mode == "dvzip":
            if self._hdr or self._left or self._block is not None:
                raise ArchiveError("truncated DVZip body")
        return out


@dataclass
class CpioEntry:
    name: str
    size: int
    mode: int

    @property
    def is_file(self) -> bool:
        return stat.S_ISREG(self.mode)


class CpioReader:
    """Incremental cpio parser (odc, newc and crc variants).

    ``feed()`` returns events in order:
        ("entry", CpioEntry)  a member begins
        ("data", bytes)       some of its contents
        ("end", entry)        the member is complete
    and ``done`` turns True at the trailer. Bytes after the trailer are
    the archiver's block padding and are ignored.
    """

    def __init__(self, max_members: int = 4096) -> None:
        self.max_members = max_members
        self.members = 0
        self.done = False
        self._buf = bytearray()
        self._entry: CpioEntry | None = None
        self._left = 0          # data bytes left in the current entry
        self._pad = 0           # alignment bytes to skip after the data
        self._newc = False

    def feed(self, data: bytes) -> list[tuple[str, object]]:
        events: list[tuple[str, object]] = []
        if self.done:
            return events
        self._buf += data
        while not self.done:
            if self._entry is not None:
                if self._left:
                    if not self._buf:
                        break
                    take = bytes(self._buf[:self._left])
                    del self._buf[:len(take)]
                    self._left -= len(take)
                    events.append(("data", take))
                    continue
                if self._pad:
                    skip = min(self._pad, len(self._buf))
                    del self._buf[:skip]
                    self._pad -= skip
                    if self._pad:
                        break
                events.append(("end", self._entry))
                self._entry = None
                continue
            if not self._parse_header(events):
                break
        return events

    def _parse_header(self, events: list) -> bool:
        if len(self._buf) < 6:
            return False
        magic = bytes(self._buf[:6])
        if magic == ODC_MAGIC:
            hlen = _ODC_HEADER
            if len(self._buf) < hlen:
                return False
            h = bytes(self._buf[:hlen])
            try:
                mode = int(h[18:24], 8)
                namesize = int(h[59:65], 8)
                size = int(h[65:76], 8)
            except ValueError:
                raise ArchiveError("malformed odc cpio header") from None
            name_pad = data_pad = 0
            self._newc = False
        elif magic in (NEWC_MAGIC, CRC_MAGIC):
            hlen = _NEWC_HEADER
            if len(self._buf) < hlen:
                return False
            h = bytes(self._buf[:hlen])
            try:
                fields = [int(h[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
            except ValueError:
                raise ArchiveError("malformed newc cpio header") from None
            mode, size, namesize = fields[1], fields[6], fields[11]
            name_pad = (-(hlen + namesize)) % 4
            data_pad = (-size) % 4
            self._newc = True
        else:
            raise ArchiveError(f"not a cpio archive (magic {magic!r})")
        if namesize == 0 or namesize > MAX_NAME_BYTES:
            raise ArchiveError(f"implausible cpio name length {namesize}")
        if len(self._buf) < hlen + namesize + name_pad:
            return False
        raw_name = bytes(self._buf[hlen:hlen + namesize]).rstrip(b"\0")
        del self._buf[:hlen + namesize + name_pad]
        name = raw_name.decode("utf-8", "replace")
        if name == TRAILER:
            self.done = True
            self._buf.clear()
            return True
        self.members += 1
        if self.members > self.max_members:
            raise ArchiveError(
                f"archive holds more than {self.max_members} members")
        self._entry = CpioEntry(name=name, size=size, mode=mode)
        self._left = size
        self._pad = data_pad
        events.append(("entry", self._entry))
        return True

    def finish(self) -> None:
        if not self.done:
            raise ArchiveError("archive ended before its trailer")


def total_size(paths: Iterable[Path]) -> int:
    return sum(os.path.getsize(p) for p in paths)
