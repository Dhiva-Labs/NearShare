"""Security regression tests for peer-controlled input.

Everything a sender puts in an IntroductionFrame is attacker-controlled.
The filename in particular was an arbitrary-file-write primitive:
`download_dir / name` with an absolute name discards the download
directory entirely, and "../" walks out of it.

Run:  .venv/bin/python -m tests.test_security
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from nearshare.core.connection import (Events, FileOffer, InboundConnection,
                                        enough_free_space, safe_filename)
import nearshare.core.connection as connection_mod
from nearshare.core.crypto import HandshakeError
from nearshare.core.service import NearShareService, control_socket_path
from nearshare.proto import offline_wire_formats_pb2 as ow
from nearshare.proto import wire_format_pb2 as wf
from tests.test_payload_routing import (FakeSender, connect_sender, finish,
                                        rand_payload_id, start_receiver)


ATTACKS = [
    "/etc/cron.d/evil",
    "../../../.bashrc",
    "../.ssh/authorized_keys",
    "..",
    ".",
    "....//....//etc/passwd",
    "sub/dir/photo.jpg",
    "back\\slash\\payload.sh",
    "with\x00null.txt",
    "carriage\r\nreturn.txt",
    ".hidden",
    "",
    "   ",
    "A" * 5000,
]


def test_safe_filename() -> None:
    for raw in ATTACKS:
        out = safe_filename(raw)
        assert "/" not in out, (raw, out)
        assert "\\" not in out, (raw, out)
        assert not out.startswith("."), (raw, out)
        assert out not in ("", ".", ".."), (raw, out)
        assert len(out) <= 200, (raw, out)
        assert not any(ord(c) < 32 or ord(c) == 127 for c in out), (raw, out)
    # Ordinary names must survive untouched.
    for good in ("photo.jpg", "My Report (final).pdf", "vidéo.mp4"):
        assert safe_filename(good) == good, good
    print(f"safe_filename: {len(ATTACKS)} hostile names neutralised, "
          "ordinary names preserved")


def test_open_dest_stays_inside_download_dir() -> None:
    root = Path(tempfile.mkdtemp(prefix="ns-sec-"))
    downloads = root / "Downloads"
    downloads.mkdir()
    outside = root / "SHOULD_NOT_EXIST.txt"

    conn = InboundConnection.__new__(InboundConnection)
    conn.download_dir = downloads

    for raw in ATTACKS + [str(outside)]:
        offer = FileOffer(payload_id=1, name=safe_filename(raw), size=0,
                          mime_type="application/octet-stream")
        conn._open_dest(offer)
        offer.handle.close()
        resolved = offer.dest_path.resolve()
        assert resolved.parent == downloads.resolve(), (raw, resolved)
    assert not outside.exists(), "wrote outside the download directory!"
    # Nothing escaped anywhere else in the sandbox either.
    strays = [p for p in root.iterdir() if p != downloads]
    assert not strays, strays
    print(f"_open_dest: {len(ATTACKS) + 1} attempts all confined to "
          f"{downloads.name}/")


def test_symlink_in_download_dir_not_followed() -> None:
    root = Path(tempfile.mkdtemp(prefix="ns-sec-link-"))
    downloads = root / "Downloads"
    downloads.mkdir()
    target = root / "victim.txt"
    target.write_text("original")
    # A dangling-or-live symlink planted where the next file will land.
    (downloads / "photo.jpg").symlink_to(target)

    conn = InboundConnection.__new__(InboundConnection)
    conn.download_dir = downloads
    offer = FileOffer(payload_id=1, name="photo.jpg", size=0,
                      mime_type="image/jpeg")
    conn._open_dest(offer)
    offer.handle.write(b"attacker content")
    offer.handle.close()

    assert target.read_text() == "original", "symlink was followed!"
    print("symlink: pre-planted link in the download dir was not followed")


async def _accept(*_a, **_kw) -> bool:
    return True


# ---------------------------------------------------------- disk exhaustion

def test_enough_free_space_rejects_absurd_size() -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="ns-sec-disk-"))
    assert enough_free_space(tmp_dir, 100), "a tiny transfer should always fit"
    assert enough_free_space(tmp_dir, 0)
    assert enough_free_space(tmp_dir, -5), "a nonsense negative size must not block"
    assert not enough_free_space(tmp_dir, 10 ** 18), \
        "an exabyte transfer must be rejected, not attempted"
    print("enough_free_space: absurd sizes rejected, ordinary sizes accepted")


def test_recv_sharing_frame_size_is_bounded() -> None:
    """A peer that never sets LAST_CHUNK on a sharing-layer BYTES payload
    (Introduction / PairedKey* during the handshake) must not be able to
    grow _recv_sharing_frame's accumulation buffer without bound."""
    conn = InboundConnection.__new__(InboundConnection)
    chunk_body = os.urandom(4096)
    payload_id = 1
    calls = {"n": 0}

    async def fake_recv_encrypted():
        calls["n"] += 1
        frame = ow.OfflineFrame()
        frame.version = ow.OfflineFrame.V1
        frame.v1.type = ow.V1Frame.PAYLOAD_TRANSFER
        pt = frame.v1.payload_transfer
        pt.packet_type = ow.PayloadTransferFrame.DATA
        pt.payload_header.id = payload_id
        pt.payload_header.type = ow.PayloadTransferFrame.PayloadHeader.BYTES
        pt.payload_chunk.offset = calls["n"] * len(chunk_body)
        pt.payload_chunk.body = chunk_body
        return frame

    conn._recv_encrypted = fake_recv_encrypted

    async def run() -> None:
        try:
            await conn._recv_sharing_frame()
            raise AssertionError("expected HandshakeError for an unbounded "
                                 "sharing-layer payload")
        except HandshakeError:
            pass

    asyncio.run(run())
    sent = calls["n"] * len(chunk_body)
    assert sent <= connection_mod.MAX_SHARING_FRAME_SIZE + len(chunk_body), (
        f"buffer grew to {sent} bytes before the cap kicked in")
    print(f"_recv_sharing_frame: aborted after {sent} bytes instead of "
          f"buffering an unbounded stream of chunks "
          f"(cap {connection_mod.MAX_SHARING_FRAME_SIZE})")


async def test_chunk_exceeding_declared_size_aborts_transfer(tmp: Path) -> None:
    """A file's declared size (shown to the human in the accept dialog) is
    peer-controlled; the actual bytes written must never exceed it. A
    single chunk whose body alone blows past the declared size must abort
    the transfer rather than silently writing more than was disclosed."""
    downloads = tmp / "sec-oversized-chunk"
    completed: list[list[Path]] = []
    errors: list[str] = []
    events = Events(
        on_transfer_request=lambda req: _accept(),
        on_complete=lambda dev, paths: completed.append(paths),
        on_error=lambda dev, msg: errors.append(msg),
    )
    server, port = await start_receiver(downloads, events)
    sender = await connect_sender(port)

    payload_id = rand_payload_id()
    declared_size = 10
    await sender.send_introduction(files=[
        {"name": "tiny.bin", "payload_id": payload_id, "size": declared_size}])
    await sender.send_file_chunk(payload_id, declared_size, 0, b"X" * 4096,
                                 "tiny.bin", last=True)

    await asyncio.sleep(0.3)
    await finish(sender, server)

    assert not completed, "transfer must not complete after an oversized chunk"
    assert errors, "receiver should have reported an error"
    leftover = [p for p in downloads.glob("**/*") if p.is_file()]
    assert not leftover, f"oversized chunk was written to disk: {leftover}"
    print("chunk exceeding declared size: transfer aborted, nothing written")


async def test_hostile_offset_does_not_create_sparse_file(tmp: Path) -> None:
    """A tiny body at a gigantic offset would otherwise create a sparse
    file that claims to be gigabytes for a few bytes on the wire."""
    downloads = tmp / "sec-hostile-offset"
    completed: list[list[Path]] = []
    errors: list[str] = []
    events = Events(
        on_transfer_request=lambda req: _accept(),
        on_complete=lambda dev, paths: completed.append(paths),
        on_error=lambda dev, msg: errors.append(msg),
    )
    server, port = await start_receiver(downloads, events)
    sender = await connect_sender(port)

    payload_id = rand_payload_id()
    declared_size = 100
    await sender.send_introduction(files=[
        {"name": "small.bin", "payload_id": payload_id, "size": declared_size}])
    await sender.send_file_chunk(payload_id, declared_size, 10 ** 9, b"hi",
                                 "small.bin", last=False)

    await asyncio.sleep(0.3)
    await finish(sender, server)

    assert not completed, "transfer must not complete after a hostile offset"
    assert errors, "receiver should have reported an error"
    leftover = [p for p in downloads.glob("**/*") if p.is_file()]
    assert not leftover, f"sparse file left behind: {leftover}"
    print("hostile chunk.offset: rejected, no sparse file created")


async def test_huge_declared_size_rejected_before_dialog(tmp: Path) -> None:
    """A peer declaring an impossible total size (more than any real disk
    has free) must be rejected automatically, before the human is ever
    asked to accept or decline."""
    downloads = tmp / "sec-huge-total"
    dialog_calls: list[object] = []
    events = Events(
        on_transfer_request=lambda req: dialog_calls.append(req) or _accept(),
    )
    server, port = await start_receiver(downloads, events)
    sender = await connect_sender(port)

    payload_id = rand_payload_id()
    intro = wf.Frame()
    intro.version = wf.Frame.V1
    intro.v1.type = wf.V1Frame.INTRODUCTION
    fm = intro.v1.introduction.file_metadata.add()
    fm.name = "huge.bin"
    fm.payload_id = payload_id
    fm.size = 10 ** 18  # an exabyte
    fm.mime_type = "application/octet-stream"
    await sender.send_sharing_frame(intro)
    resp = await sender.recv_sharing_frame()

    assert resp.v1.type == wf.V1Frame.RESPONSE, resp
    assert (resp.v1.connection_response.status ==
            wf.ConnectionResponseFrame.REJECT), \
        "an undeliverable size must be rejected, not accepted"
    assert not dialog_calls, "must not bother the human with an impossible transfer"
    await finish(sender, server)
    print("huge declared total size: rejected before the accept dialog fired")


async def test_bytes_payload_size_is_bounded(tmp: Path) -> None:
    """Same accumulation-without-bound risk as _recv_sharing_frame, but on
    the post-accept BYTES-payload path in _receive_payloads (text shares /
    control frames). Uses a small patched cap so the test stays fast."""
    downloads = tmp / "sec-bytes-cap"
    completed: list[list[Path]] = []
    errors: list[str] = []
    events = Events(
        on_transfer_request=lambda req: _accept(),
        on_complete=lambda dev, paths: completed.append(paths),
        on_error=lambda dev, msg: errors.append(msg),
    )
    server, port = await start_receiver(downloads, events)
    sender = await connect_sender(port)

    small_cap = 64 * 1024
    original_cap = connection_mod.MAX_SHARING_FRAME_SIZE
    connection_mod.MAX_SHARING_FRAME_SIZE = small_cap
    sent = 0
    try:
        text_id = rand_payload_id()
        await sender.send_introduction(texts=[
            {"title": "note", "payload_id": text_id, "size": small_cap * 10}])

        chunk = b"A" * 16384
        try:
            # A well-behaved connection would never see this many chunks
            # for one payload without LAST_CHUNK; if the cap didn't exist,
            # this loop alone would buffer 10x the cap. Yield after each
            # send (loopback write buffers are generous enough that a
            # tight send loop can otherwise outrun the receiver's task
            # entirely) so the receiver gets a real chance to detect the
            # violation and close the connection out from under us.
            for _ in range(10 * small_cap // len(chunk)):
                frame = ow.OfflineFrame()
                frame.version = ow.OfflineFrame.V1
                frame.v1.type = ow.V1Frame.PAYLOAD_TRANSFER
                pt = frame.v1.payload_transfer
                pt.packet_type = ow.PayloadTransferFrame.DATA
                pt.payload_header.id = text_id
                pt.payload_header.type = ow.PayloadTransferFrame.PayloadHeader.BYTES
                pt.payload_chunk.offset = sent
                pt.payload_chunk.body = chunk
                await sender.send_encrypted(frame)
                sent += len(chunk)
                await asyncio.sleep(0.005)
        except (ConnectionError, OSError):
            pass  # receiver already hung up on us -- exactly what we want

        await asyncio.sleep(0.3)
        await finish(sender, server)
    finally:
        connection_mod.MAX_SHARING_FRAME_SIZE = original_cap

    assert not completed, "transfer must not complete"
    assert errors, "receiver should have reported an error"
    assert any("too large" in e for e in errors), errors
    assert sent < 10 * small_cap, (
        f"receiver kept accepting bytes well past the cap: sent {sent}, "
        f"cap {small_cap}")
    print(f"BYTES payload: aborted after {sent} bytes instead of buffering "
          f"all {10 * small_cap} (cap was {small_cap})")


# --------------------------------------------------------- control socket

async def test_control_socket_private_perms_despite_permissive_umask() -> None:
    """The control socket accepts a 'send' command that can push any file
    the daemon's user can read to a LAN peer, so it must never be briefly
    group/world-accessible between bind() and chmod()."""
    os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="ns-run-perm-")
    downloads = Path(tempfile.mkdtemp(prefix="ns-dl-perm-"))
    old_umask = os.umask(0o000)  # worst case: fully permissive ambient umask
    umask_calls: list[int] = []
    real_umask = os.umask

    def spy_umask(mask: int) -> int:
        umask_calls.append(mask)
        return real_umask(mask)

    service = NearShareService(device_name="permtest", download_dir=downloads)
    import nearshare.core.service as service_mod
    service_mod.os.umask = spy_umask
    try:
        await service.start(visible=False)
        try:
            mode = control_socket_path().stat().st_mode & 0o777
            assert mode == 0o600, f"control socket perms are {oct(mode)}, want 0600"
            assert 0o177 in umask_calls, (
                "expected the bind to tighten the process umask before "
                f"creating the socket, calls were {umask_calls}")
        finally:
            await service.stop()
    finally:
        service_mod.os.umask = real_umask
        os.umask(old_umask)
    print("control socket: private (0600) perms held even under a "
          "permissive ambient umask")


async def test_control_socket_handles_malformed_input() -> None:
    """A malformed or oversized line on the control socket must not crash
    the connection handler or leave the service unresponsive."""
    os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="ns-run-bad-")
    downloads = Path(tempfile.mkdtemp(prefix="ns-dl-bad-"))
    service = NearShareService(device_name="badinput", download_dir=downloads)
    await service.start(visible=False)
    try:
        sock = control_socket_path()

        # Valid JSON, but not an object -- req.get("cmd") would otherwise
        # raise AttributeError inside the connection task.
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(b"[1, 2, 3]\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        assert b'"error"' in line, line
        writer.close()

        # A line with no trailing newline that blows past the
        # StreamReader's buffer limit -- readline() raises ValueError.
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(b"A" * (200 * 1024))
        await writer.drain()
        try:
            await asyncio.wait_for(reader.readline(), timeout=5)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        writer.close()

        # The service itself must still be alive and answering normally.
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(b'{"cmd": "status"}\n')
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        assert b'"device_name": "badinput"' in line, line
        writer.close()
    finally:
        await service.stop()
    print("control socket: malformed/oversized input handled, "
          "service stayed alive")


async def _run_async_tests() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ns-sec-async-"))
    await test_chunk_exceeding_declared_size_aborts_transfer(tmp)
    await test_hostile_offset_does_not_create_sparse_file(tmp)
    await test_huge_declared_size_rejected_before_dialog(tmp)
    await test_concurrent_inbound_connections_are_capped(tmp)
    await test_silent_peer_is_dropped_at_handshake_deadline(tmp)
    await test_plaintext_frame_is_capped_before_authentication(tmp)
    await test_bytes_payload_size_is_bounded(tmp)
    await test_control_socket_private_perms_despite_permissive_umask()
    await test_control_socket_handles_malformed_input()


def main() -> int:
    test_safe_filename()
    test_open_dest_stays_inside_download_dir()
    test_symlink_in_download_dir_not_followed()
    test_enough_free_space_rejects_absurd_size()
    test_recv_sharing_frame_size_is_bounded()
    asyncio.run(_run_async_tests())
    print("\nSECURITY TESTS PASSED")
    return 0


async def test_concurrent_inbound_connections_are_capped(tmp: Path) -> None:
    """Anyone on the LAN can open sockets; without a cap they exhaust
    file descriptors and starve real transfers. Connections beyond the
    cap must be refused immediately rather than queued."""
    from nearshare.core import service as service_mod

    downloads = tmp / "sec-conncap"
    downloads.mkdir(parents=True, exist_ok=True)
    svc = NearShareService(device_name="cap-test", download_dir=downloads)
    os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="ns-cap-")
    await svc.start(visible=False)
    try:
        held = []
        # Fill every slot without speaking the protocol, the cheapest
        # possible flood.
        for _ in range(service_mod.MAX_CONCURRENT_INBOUND):
            held.append(await asyncio.open_connection("127.0.0.1", svc.port))
        await asyncio.sleep(0.3)
        assert svc._inbound_count == service_mod.MAX_CONCURRENT_INBOUND, \
            svc._inbound_count

        reader, writer = await asyncio.open_connection("127.0.0.1", svc.port)
        # The server must hang up on the extra connection, not hold it.
        data = await asyncio.wait_for(reader.read(1), timeout=5)
        assert data == b"", "connection past the cap was not refused"
        writer.close()

        for _r, w in held:
            w.close()
    finally:
        await svc.stop()
    print(f"connection cap: {service_mod.MAX_CONCURRENT_INBOUND} accepted, "
          "the next refused immediately")


async def test_silent_peer_is_dropped_at_handshake_deadline(tmp: Path) -> None:
    """A peer that connects and never speaks used to hold its inbound slot
    forever: the plaintext handshake reads had no timeout, so eight idle
    sockets locked every real sender out. The handshake now has a
    deadline and the connection is closed when it passes."""
    downloads = tmp / "sec-silent"
    original = connection_mod.HANDSHAKE_TIMEOUT
    connection_mod.HANDSHAKE_TIMEOUT = 0.5
    server, port = await start_receiver(downloads, Events())
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # Say nothing. The receiver must hang up on its own.
        data = await asyncio.wait_for(reader.read(1), timeout=5)
        assert data == b"", "a silent peer was not disconnected"
        writer.close()

        # Stalling halfway (a header promising bytes that never come)
        # is the same attack one step later.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write((16).to_bytes(4, "big") + b"abc")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(1), timeout=5)
        assert data == b"", "a stalled handshake was not disconnected"
        writer.close()
    finally:
        connection_mod.HANDSHAKE_TIMEOUT = original
        server.close()
        await server.wait_closed()
    print("silent peer: dropped at the handshake deadline")


async def test_plaintext_frame_is_capped_before_authentication(tmp: Path) -> None:
    """Before the cipher exists every frame is a few hundred bytes. The
    general 100 MiB ceiling let an unauthenticated peer make the receiver
    buffer that much per connection; anything over the plaintext cap is
    now refused on the header alone, before a byte of body is read."""
    downloads = tmp / "sec-plaincap"
    server, port = await start_receiver(downloads, Events())
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        too_big = connection_mod.MAX_PLAINTEXT_FRAME + 1
        writer.write(too_big.to_bytes(4, "big"))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(1), timeout=5)
        assert data == b"", "an oversized plaintext frame was not refused"
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    print(f"plaintext frame cap: {connection_mod.MAX_PLAINTEXT_FRAME} bytes, "
          "larger refused before authentication")


if __name__ == "__main__":
    sys.exit(main())
