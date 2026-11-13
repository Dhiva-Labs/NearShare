"""Transfer control over loopback: the PIN event, cancelling in either
direction, and renaming this device.

Run:  .venv/bin/python -m tests.test_transfer_control
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from nearshare.core.connection import Events, TransferRequest
from nearshare.core.mdns import Peer
from nearshare.core.service import NearShareService


def _peer(service: NearShareService) -> Peer:
    return Peer(instance="x", endpoint_id="", device_name=service.device_name,
                device_type=3, host="127.0.0.1", port=service.port)


async def test_pin_and_cancel_send(tmp: Path) -> None:
    """The sender learns the PIN before the receiver answers, and a send
    cancelled while waiting stops cleanly on both sides."""
    asked = asyncio.Event()
    receiver_pins: list[str] = []
    receiver_errors: list[str] = []

    async def never_answers(req: TransferRequest) -> bool:
        receiver_pins.append(req.pin)
        asked.set()
        await asyncio.Event().wait()  # the human never taps anything
        return False

    receiver = NearShareService(
        device_name="ctl-receiver", download_dir=tmp / "in",
        events=Events(on_transfer_request=never_answers,
                      on_error=lambda dev, err: receiver_errors.append(err)))
    await receiver.start(visible=False)

    sender_pins: list[tuple[str, str]] = []
    # The sender only needs send_files(); it is never start()ed.
    sender = NearShareService(
        device_name="ctl-sender",
        events=Events(on_pin=lambda dev, pin: sender_pins.append((dev, pin))))

    payload = tmp / "file.bin"
    payload.write_bytes(os.urandom(64 * 1024))
    send = asyncio.ensure_future(sender.send_files(_peer(receiver), [payload]))
    try:
        await asyncio.wait_for(asked.wait(), timeout=20)
        assert sender_pins == [("ctl-receiver", receiver_pins[0])], \
            f"sender {sender_pins} vs receiver {receiver_pins}"
        print(f"on_pin fired before accept, PIN {receiver_pins[0]} on both sides")

        assert not sender.cancel_transfer("someone-else", "send")
        assert not sender.cancel_transfer("ctl-receiver", "receive")
        assert sender.cancel_transfer("ctl-receiver", "send")
        try:
            await asyncio.wait_for(send, timeout=10)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled send returned normally")
        assert not sender._transfers, sender._transfers
        # The receiver is told (a DISCONNECTION frame), not left hanging.
        for _ in range(50):
            if not receiver._transfers:
                break
            await asyncio.sleep(0.1)
        assert not receiver._transfers, "receiver still thinks it is busy"
        print("cancelled send unwound on both sides")
    finally:
        await receiver.stop()


async def test_cancel_receive(tmp: Path) -> None:
    """Cancelling a receive part-way deletes the partial file."""
    downloads = tmp / "in2"
    started = asyncio.Event()

    async def accept(req: TransferRequest) -> bool:
        return True

    receiver = NearShareService(
        device_name="ctl-receiver", download_dir=downloads,
        events=Events(on_transfer_request=accept,
                      on_progress=lambda dev, done, total: started.set()))
    await receiver.start(visible=False)
    sender = NearShareService(device_name="ctl-sender", events=Events())

    payload = tmp / "big.bin"
    with open(payload, "wb") as fh:  # sparse: 256 MB without writing it
        fh.truncate(256 * 1024 * 1024)
    send = asyncio.ensure_future(sender.send_files(_peer(receiver), [payload]))
    try:
        await asyncio.wait_for(started.wait(), timeout=30)
        assert (downloads / "big.bin").exists(), "no partial file yet?"
        assert receiver.cancel_transfer("ctl-sender", "receive")
        for _ in range(100):
            if not receiver._transfers:
                break
            await asyncio.sleep(0.1)
        assert not receiver._transfers
        assert not list(downloads.iterdir()), \
            f"partial file left behind: {list(downloads.iterdir())}"
        print("cancelled receive deleted its partial file")
    finally:
        send.cancel()
        try:
            await send
        except (asyncio.CancelledError, Exception):
            pass
        await receiver.stop()


async def test_rename() -> None:
    service = NearShareService(device_name="before")
    await service.start(visible=False)
    try:
        port = service.port
        await service.set_device_name("  after  ")
        assert service.device_name == "after"
        assert service._advertiser.device_name == "after"
        assert service._advertiser.port == port
        assert not service.visible, "renaming must not make a hidden device visible"
        await service.set_device_name("")
        assert service.device_name == "after", "an empty name is ignored"
        print("rename re-creates the advertiser and keeps visibility")
    finally:
        await service.stop()


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ns-ctl-"))
    await test_pin_and_cancel_send(tmp)
    await test_cancel_receive(tmp)
    await test_rename()
    print("\nTRANSFER CONTROL TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
