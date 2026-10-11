"""AirDrop loopback and hardening tests: NearShare's sender against its
own receiver over ::1, plus hand-built hostile requests.

No radio is involved: the receiver binds ::1 instead of awdl0, which is
everything AirDrop does above the link layer (TLS, /Discover, /Ask,
/Upload, cpio, DVZip). What it cannot show is interop with a real Apple
device; that needs an iPhone or Mac and an AWDL radio backend.

Run:  .venv/bin/python -m tests.test_airdrop
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import os
import random
import sys
import tempfile
import zlib
from pathlib import Path

from nearshare.airdrop import protocol
from nearshare.airdrop.archive import (ArchiveError, ArchiveTooLarge,
                                       BodyDecoder, CpioReader, cpio_stream,
                                       dvzip_stream, odc_header)
from nearshare.airdrop.http import ClientConnection
from nearshare.airdrop.identity import load_or_create
from nearshare.airdrop.receiver import AirDropReceiver, safe_name
from nearshare.airdrop.sender import AirDropPeer, AirDropSender, Declined
from nearshare.core.connection import Events, TransferRequest


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unpack(body: bytes, feed: int) -> dict[str, bytes]:
    """Decode a body through BodyDecoder + CpioReader, fed in pieces."""
    decoder, reader = BodyDecoder(1 << 30), CpioReader()
    files: dict[str, bytearray] = {}
    current = None
    chunks = []
    for i in range(0, len(body), feed):
        chunks += decoder.feed(body[i:i + feed])
    chunks += decoder.finish()
    for chunk in chunks:
        for kind, value in reader.feed(chunk):
            if kind == "entry":
                current = files.setdefault(value.name, bytearray())
            elif kind == "data":
                current += value
    reader.finish()
    return {k: bytes(v) for k, v in files.items()}


def newc_member(name: str, data: bytes, mode: int = 0o100644) -> bytes:
    raw = name.encode() + b"\0"
    fields = [1, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(raw), 0]
    head = b"070701" + b"".join(b"%08x" % f for f in fields)
    out = head + raw + b"\0" * ((-(len(head) + len(raw))) % 4)
    return out + data + b"\0" * ((-len(data)) % 4)


def test_archive(tmp: Path) -> None:
    a = tmp / "a.bin"
    a.write_bytes(os.urandom(300_000))           # incompressible: stored blocks
    b = tmp / "b.txt"
    b.write_text("airdrop " * 50_000)            # compressible: zlib blocks
    e = tmp / "empty"
    e.write_bytes(b"")
    want = {"a.bin": a.read_bytes(), "b.txt": b.read_bytes(), "empty": b""}
    cpio = b"".join(cpio_stream([(n, tmp / n) for n in want]))
    assert cpio.startswith(b"070707")
    dvzip = b"".join(dvzip_stream([cpio[i:i + 9999]
                                   for i in range(0, len(cpio), 9999)]))
    stored = compressed = 0
    pos = 0
    while pos < len(dvzip):
        header = int.from_bytes(dvzip[pos:pos + 4], "big")
        stored += bool(header & 0x80000000)
        compressed += not header & 0x80000000
        pos += 4 + (header & 0x7FFFFFFF)
    assert stored and compressed, (stored, compressed)
    for feed in (1, 7, 4096, len(dvzip)):
        assert unpack(dvzip, feed) == want, f"dvzip feed={feed}"
    assert unpack(gzip.compress(cpio), 333) == want, "gzip"
    assert unpack(cpio, 1000) == want, "raw cpio"
    newc = (newc_member("x/y.txt", b"hello") + newc_member("z", b"abc")
            + newc_member("TRAILER!!!", b"") + b"\0" * 300)
    assert unpack(newc, 3) == {"x/y.txt": b"hello", "z": b"abc"}, "newc"
    print(f"archive: odc/newc cpio, DVZip ({stored} stored + {compressed} "
          f"zlib blocks), gzip and raw all round-trip at any feed size")

    # Hostile containers.
    bomb = zlib.compress(b"\0" * (8 << 20), 9)
    body = len(bomb).to_bytes(4, "big") + bomb
    try:
        BodyDecoder(1 << 20).feed(body)
        raise AssertionError("bomb not stopped")
    except ArchiveTooLarge:
        pass
    for bad, why in (
            ((0x7FFFFFFF).to_bytes(4, "big"), "huge block"),
            (b"\x00\x00\x00\x05abcde", "garbage zlib"),
    ):
        try:
            d = BodyDecoder(1 << 20)
            d.feed(bad)
            d.finish()
            raise AssertionError(f"accepted {why}")
        except (ArchiveError, zlib.error):
            pass
    try:
        CpioReader().feed(odc_header("x", 0, 0o100644)[:-2] + b"\xff" * 9000)
        raise AssertionError("accepted a huge name")
    except ArchiveError:
        pass
    truncated = CpioReader()
    truncated.feed(cpio[:1000])
    try:
        truncated.finish()
        raise AssertionError("accepted a truncated archive")
    except ArchiveError:
        pass
    print("archive: zip bomb, oversized block, bad zlib, huge name and "
          "truncation all refused")


async def raw_post(port: int, ctx, path: str, headers: list, body) -> int:
    conn = await ClientConnection.open("::1", port, ctx, timeout=5)
    try:
        resp = await conn.request("POST", path, headers, body, timeout=10)
        return resp.status
    finally:
        await conn.close()


async def test_transfers(tmp: Path) -> None:
    downloads = tmp / "downloads"
    identity = load_or_create(tmp / "identity")
    assert (tmp / "identity" / "key.pem").stat().st_mode & 0o777 == 0o600

    answers: list[bool] = []
    asked: list[TransferRequest] = []
    received: list[Path] = []
    texts: list[tuple[str, str]] = []
    progress: list[tuple[int, int]] = []

    async def on_request(req: TransferRequest) -> bool:
        asked.append(req)
        return answers.pop(0) if answers else True

    receiver = AirDropReceiver(
        "Test Mac", downloads,
        Events(on_transfer_request=on_request,
               on_complete=lambda dev, paths: received.extend(paths),
               on_text=lambda dev, text: texts.append((dev, text)),
               on_progress=lambda dev, d, t: progress.append((d, t))),
        identity, host="::1", port=0)
    await receiver.start()
    port = receiver.port
    sent_progress: list[int] = []
    sender = AirDropSender(identity, "Loopback Sender", Events(
        on_progress=lambda dev, d, t: sent_progress.append(d)))
    peer = AirDropPeer(address="::1", port=port, iface=None)

    try:
        # 1. /Discover returns our name.
        assert await sender.discover(peer)
        assert peer.name == "Test Mac", peer
        assert peer.model == protocol.DEFAULT_MODEL
        print(f"discover: peer answered as {peer.name!r} ({peer.model})")

        # 2. A multi-file transfer, byte-identical.
        photo = tmp / "IMG_0001.JPG"
        photo.write_bytes(os.urandom(2_500_000))
        notes = tmp / "notes.txt"
        notes.write_text("line\n" * 100_000)
        empty = tmp / "empty.dat"
        empty.write_bytes(b"")
        await asyncio.wait_for(sender.send_files(peer, [photo, notes, empty]),
                               60)
        await asyncio.sleep(0.1)
        got = {p.name: p for p in received}
        assert set(got) == {"IMG_0001.JPG", "notes.txt", "empty.dat"}, got
        for src in (photo, notes, empty):
            assert sha256(got[src.name]) == sha256(src), src.name
            assert got[src.name].parent == downloads.resolve()
        req = asked[-1]
        assert req.device_name == "Loopback Sender" and req.pin == ""
        assert sorted(f.name for f in req.files) == sorted(got)
        assert req.total_size == sum(p.stat().st_size for p in (photo, notes, empty))
        assert progress and sent_progress, "no progress reported"
        print(f"send: 3 files ({req.total_size} bytes) arrived byte-identical, "
              f"{len(progress)} receive / {len(sent_progress)} send progress events")

        # 3. Sending the same names again never overwrites.
        received.clear()
        await asyncio.wait_for(sender.send_files(peer, [photo]), 60)
        assert [p.name for p in received] == ["IMG_0001 (1).JPG"], received
        print("send: a repeated name is saved as 'IMG_0001 (1).JPG'")

        # 4. Declining is reported to the sender, and nothing is written.
        before = sorted(downloads.iterdir())
        answers.append(False)
        try:
            await sender.send_files(peer, [notes])
            raise AssertionError("decline not reported")
        except Declined:
            pass
        assert sorted(downloads.iterdir()) == before
        print("decline: sender told 'declined', nothing written")

        # 5. A link arrives as text, not a file.
        await sender.send_link(peer, "https://example.com/page")
        assert texts[-1] == ("Loopback Sender", "https://example.com/page")
        print("link: delivered through on_text")

        # 6. An /Upload no one accepted is refused before its body is read.
        ctx = identity.client_context()
        body = b"".join(dvzip_stream(cpio_stream([("x.txt", notes)])))
        status = await raw_post(port, ctx, "/Upload", [
            ("Content-Type", "application/x-dvzip"),
            ("TransferID", "NEVER-ASKED")], [body])
        assert status == 403, status
        assert sorted(downloads.iterdir()) == before
        print("upload without an accepted Ask: 403")

        # 7. A hostile archive, through an accepted Ask with no TransferID
        #    (OpenDrop's shape: x-cpio, gzip, matched by address).
        conn = await ClientConnection.open("::1", port, ctx, timeout=5)
        ask = protocol.dumps({"SenderComputerName": "../../evil\u202egpj.exe",
                              "Files": [{"FileName": "a", "FileSize": 10}]})
        resp = await conn.request("POST", "/Ask", [], ask, timeout=10)
        assert resp.status == 200, resp.status
        assert "/" not in asked[-1].device_name
        assert "\u202e" not in asked[-1].device_name
        evil = b"".join([
            odc_header("../../../escape.txt", 4, 0o100644) + b"ESC1",
            odc_header("/tmp/absolute.txt", 4, 0o100644) + b"ESC2",
            odc_header("NSIRD_AirDrop_x/._IMG.JPG", 3, 0o100644) + b"meh",
            odc_header("NSIRD_AirDrop_x/IMG.JPG", 5, 0o100644) + b"photo",
            odc_header("NSIRD_AirDrop_x", 0, 0o040755),
            odc_header("link", 11, 0o120777) + b"/etc/passwd",
            odc_header("..", 2, 0o100644) + b"dd",
            odc_header("TRAILER!!!", 0, 0),
        ])
        received.clear()
        resp = await conn.request("POST", "/Upload", [
            ("Content-Type", "application/x-cpio")], gzip.compress(evil),
            timeout=10)
        await conn.close()
        assert resp.status == 200, resp.status
        names = sorted(p.name for p in received)
        assert names == ["IMG.JPG", "absolute.txt", "airdrop-file",
                         "escape.txt"], names
        for p in received:
            assert p.parent == downloads.resolve(), p
        assert not (tmp / "escape.txt").exists()
        assert not Path("/tmp/absolute.txt").exists() or \
            Path("/tmp/absolute.txt").read_bytes() != b"ESC2"
        assert not any(p.is_symlink() for p in downloads.iterdir())
        print(f"hostile archive: flattened to {names}, AppleDouble, "
              "directory and symlink members dropped, nothing escaped")

        # 8. An upload that inflates past its Ask's allowance is refused
        #    and leaves nothing behind.
        conn = await ClientConnection.open("::1", port, ctx, timeout=5)
        tid = protocol.new_transfer_id()
        ask = protocol.ask_files("bomber", "x", "aa", tid,
                                 [("big.bin", 1, "public.data")])
        resp = await conn.request("POST", "/Ask", [], ask, timeout=10)
        assert resp.status == 200
        # The member claims 300 MiB against an Ask that declared 1 byte;
        # it is refused at its header, before any of it is written.
        member = odc_header("big.bin", 300 << 20, 0o100644)
        first = zlib.compress(member + bytes(128 * 1024 - len(member)), 9)
        zeros = zlib.compress(bytes(128 * 1024), 9)
        blocks = [len(first).to_bytes(4, "big") + first]
        blocks += [len(zeros).to_bytes(4, "big") + zeros] * 8
        before = sorted(downloads.iterdir())
        resp = await conn.request("POST", "/Upload", [
            ("Content-Type", "application/x-dvzip"), ("TransferID", tid)],
            iter(blocks), timeout=30)
        await conn.close()
        assert resp.status == 413, resp.status
        assert sorted(downloads.iterdir()) == before
        print("oversized member: 413 at its header, nothing written")

        # 9. Junk instead of HTTP doesn't take the receiver down.
        reader, writer = await asyncio.open_connection("::1", port, ssl=ctx,
                                                       server_hostname="")
        writer.write(b"\x16\x03 garbage\r\n" * 100 + b"X" * 70000 + b"\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.read(), 10)
        writer.close()
        assert await sender.discover(peer), "receiver died after junk"
        print("junk request: refused, receiver still serving")
    finally:
        await receiver.stop()


def test_names() -> None:
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("..") == "airdrop-file"
    assert safe_name("a\u202egpj.exe") == "agpj.exe"
    assert safe_name("C:\\Users\\x\\photo.jpg") == "photo.jpg"
    assert safe_name("x" * 300 + ".jpg").endswith(".jpg")
    assert len(safe_name("é" * 300).encode()) <= 200
    assert protocol.eui64_link_local("02:11:22:33:44:55") == "fe80::11:22ff:fe33:4455"
    assert protocol.scoped("fe80::1", "awdl0") == "fe80::1%awdl0"
    assert protocol.scoped("::1", "awdl0") == "::1"
    print("names: path, bidi and length sanitising; EUI-64 link-local OK")


def main() -> int:
    random.seed(1)
    tmp = Path(tempfile.mkdtemp(prefix="airdrop-test-"))
    test_names()
    test_archive(tmp)
    asyncio.run(test_transfers(tmp))
    print("\nAIRDROP TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
