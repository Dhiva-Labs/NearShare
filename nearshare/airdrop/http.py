"""Just enough HTTP/1.1 for AirDrop, on asyncio streams, both directions.

AirDrop is three POSTs over TLS, but the details are strict enough that
a general-purpose client gets them wrong: iOS 26 refuses an /Upload
whose headers differ from what sharingd sends (no Host, no
Accept-Encoding, chunked body), and on the receiving side iOS sends
/Discover and /Ask chunked with no Content-Length. Owning the framing
keeps every header and every byte count under our control.

The server half reads input from an unauthenticated network peer, so
every line, header block and chunk size is bounded before it is used,
and every read has an idle timeout.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Iterable

MAX_HEAD_BYTES = 16 * 1024
MAX_CHUNK_LINE = 128
MAX_TRAILER_BYTES = 8 * 1024
READ_PIECE = 64 * 1024

_REASONS = {
    100: "Continue", 200: "OK", 400: "Bad Request", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
    411: "Length Required", 413: "Payload Too Large",
    415: "Unsupported Media Type", 422: "Unprocessable Entity",
    500: "Internal Server Error", 503: "Service Unavailable",
    507: "Insufficient Storage",
}


class HttpError(Exception):
    """A request we refuse; ``status`` is what the peer is told."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Headers:
    """Case-insensitive header lookup that remembers the original order."""

    def __init__(self, items: Iterable[tuple[str, str]] = ()) -> None:
        self.items: list[tuple[str, str]] = list(items)

    def get(self, name: str, default: str | None = None) -> str | None:
        name = name.lower()
        for key, value in self.items:
            if key.lower() == name:
                return value
        return default

    def __repr__(self) -> str:
        return f"Headers({self.items!r})"


async def _readline(reader: asyncio.StreamReader, limit: int,
                    timeout: float) -> bytes:
    try:
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout)
    except asyncio.LimitOverrunError:
        raise HttpError(400, "line too long") from None
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            raise EOFError from None
        raise HttpError(400, "connection closed mid-line") from None
    if len(line) > limit:
        raise HttpError(400, "line too long")
    return line


async def _read_head(reader: asyncio.StreamReader, timeout: float
                     ) -> tuple[str, Headers]:
    """First line plus headers. Raises EOFError on a clean close before
    the first byte, which is how a keep-alive connection normally ends."""
    first = await _readline(reader, MAX_HEAD_BYTES, timeout)
    used = len(first)
    items: list[tuple[str, str]] = []
    while True:
        try:
            line = await _readline(reader, MAX_HEAD_BYTES, timeout)
        except EOFError:
            raise HttpError(400, "connection closed in headers") from None
        used += len(line)
        if used > MAX_HEAD_BYTES:
            raise HttpError(400, "header block too large")
        line = line.rstrip(b"\r\n")
        if not line:
            break
        name, sep, value = line.partition(b":")
        if not sep or not name.strip():
            raise HttpError(400, "malformed header line")
        items.append((name.strip().decode("latin-1"),
                      value.strip().decode("latin-1")))
    return first.rstrip(b"\r\n").decode("latin-1"), Headers(items)


def body_framing(headers: Headers) -> tuple[str, int | None]:
    """("chunked", None), ("length", n) or ("none", None)."""
    encoding = headers.get("Transfer-Encoding")
    if encoding:
        if [p.strip().lower() for p in encoding.split(",")] != ["chunked"]:
            raise HttpError(400, f"unsupported Transfer-Encoding {encoding!r}")
        return "chunked", None
    length = headers.get("Content-Length")
    if length is None:
        return "none", None
    if not length.isdigit():
        raise HttpError(400, f"invalid Content-Length {length!r}")
    return "length", int(length)


async def iter_body(reader: asyncio.StreamReader, headers: Headers, *,
                    timeout: float, limit: int) -> AsyncIterator[bytes]:
    """Yield a message body as it arrives, never more than ``limit``
    bytes in total (HttpError 413 beyond that)."""
    framing, length = body_framing(headers)
    received = 0

    async def exactly(n: int) -> AsyncIterator[bytes]:
        nonlocal received
        while n:
            try:
                piece = await asyncio.wait_for(
                    reader.read(min(n, READ_PIECE)), timeout)
            except asyncio.TimeoutError:
                raise HttpError(408, "body stalled") from None
            if not piece:
                raise HttpError(400, "connection closed mid-body")
            n -= len(piece)
            received += len(piece)
            if received > limit:
                raise HttpError(413, f"body exceeds {limit} bytes")
            yield piece

    if framing == "length":
        if length > limit:
            raise HttpError(413, f"body exceeds {limit} bytes")
        async for piece in exactly(length):
            yield piece
        return
    if framing == "none":
        return
    while True:
        try:
            line = await _readline(reader, MAX_CHUNK_LINE, timeout)
        except (EOFError, asyncio.TimeoutError):
            raise HttpError(400, "connection closed in chunk header") from None
        token = line.split(b";", 1)[0].strip()
        try:
            size = int(token, 16)
        except ValueError:
            raise HttpError(400, "invalid chunk size") from None
        if size < 0:
            raise HttpError(400, "invalid chunk size")
        if size == 0:
            trailer = 0
            while True:
                try:
                    tline = await _readline(reader, MAX_CHUNK_LINE * 8, timeout)
                except (EOFError, asyncio.TimeoutError):
                    raise HttpError(400, "connection closed in trailer") from None
                trailer += len(tline)
                if trailer > MAX_TRAILER_BYTES:
                    raise HttpError(400, "trailer too large")
                if not tline.strip():
                    return
        async for piece in exactly(size):
            yield piece
        try:
            crlf = await asyncio.wait_for(reader.readexactly(2), timeout)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            raise HttpError(400, "connection closed after chunk") from None
        if crlf != b"\r\n":
            raise HttpError(400, "chunk not followed by CRLF")


async def read_body(reader: asyncio.StreamReader, headers: Headers, *,
                    timeout: float, limit: int) -> bytes:
    parts = []
    async for piece in iter_body(reader, headers, timeout=timeout, limit=limit):
        parts.append(piece)
    return b"".join(parts)


# ------------------------------------------------------------------ server

class Request:
    def __init__(self, method: str, path: str, headers: Headers) -> None:
        self.method = method
        self.path = path
        self.headers = headers


async def read_request(reader: asyncio.StreamReader, timeout: float
                       ) -> Request | None:
    """The next request's head, or None when the peer closed cleanly."""
    try:
        first, headers = await _read_head(reader, timeout)
    except EOFError:
        return None
    except asyncio.TimeoutError:
        return None          # an idle keep-alive connection: just close it
    parts = first.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise HttpError(400, "malformed request line")
    return Request(parts[0].upper(), parts[1], headers)


def response_bytes(status: int, body: bytes = b"", *,
                   headers: Iterable[tuple[str, str]] = (),
                   close: bool = False) -> bytes:
    lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Status')}"]
    lines += [f"{k}: {v}" for k, v in headers]
    lines.append(f"Content-Length: {len(body)}")
    if close:
        lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def continue_bytes() -> bytes:
    return b"HTTP/1.1 100 Continue\r\n\r\n"


# ------------------------------------------------------------------ client

class Response:
    def __init__(self, status: int, headers: Headers, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body


class ClientConnection:
    """One keep-alive TLS connection to a peer's AirDrop server."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def open(cls, host: str, port: int, ssl_context, *,
                   timeout: float) -> "ClientConnection":
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context,
                                    server_hostname="" if ssl_context else None,
                                    limit=MAX_HEAD_BYTES * 4),
            timeout)
        return cls(reader, writer)

    async def request(self, method: str, path: str,
                      headers: Iterable[tuple[str, str]],
                      body: bytes | Iterable[bytes] | None = None, *,
                      timeout: float, response_limit: int = 4 << 20,
                      on_sent=None) -> Response:
        """Send one request and read its response.

        ``body`` as bytes goes with a Content-Length; an iterable goes
        chunked, one chunk per item, draining after each so a slow link
        applies backpressure instead of buffering the whole upload.
        ``headers`` are written exactly as given, in order, and nothing
        is added except the framing header.
        """
        lines = [f"{method} {path} HTTP/1.1"]
        lines += [f"{k}: {v}" for k, v in headers]
        chunked = body is not None and not isinstance(body, (bytes, bytearray))
        if body is None:
            pass
        elif chunked:
            lines.append("Transfer-Encoding: chunked")
        else:
            lines.append(f"Content-Length: {len(body)}")
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        try:
            if chunked:
                for piece in body:
                    if not piece:
                        continue
                    self.writer.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                    await asyncio.wait_for(self.writer.drain(), timeout)
                    if on_sent:
                        on_sent(len(piece))
                self.writer.write(b"0\r\n\r\n")
            elif body:
                self.writer.write(body)
            await asyncio.wait_for(self.writer.drain(), timeout)
        except (ConnectionError, OSError):
            # The peer may have answered early and closed (a refusal sent
            # before reading the body). Its answer is the useful part.
            pass
        return await self._read_response(timeout, response_limit)

    async def _read_response(self, timeout: float, limit: int) -> Response:
        while True:
            try:
                first, headers = await _read_head(self.reader, timeout)
            except EOFError:
                raise ConnectionError("peer closed without answering") from None
            parts = first.split(" ", 2)
            if len(parts) < 2 or not parts[0].startswith("HTTP/1."):
                raise ConnectionError(f"malformed status line {first!r}")
            try:
                status = int(parts[1])
            except ValueError:
                raise ConnectionError(f"malformed status line {first!r}") from None
            if status == 100:
                continue
            framing, _ = body_framing(headers)
            if framing == "none" and (headers.get("Connection") or "").lower() == "close":
                body = await asyncio.wait_for(self.reader.read(limit), timeout)
            else:
                body = await read_body(self.reader, headers, timeout=timeout,
                                       limit=limit)
            return Response(status, headers, body)

    async def close(self) -> None:
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 2)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            pass
