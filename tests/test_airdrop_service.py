"""AirDrop through the whole service: radio contract, control socket,
discovery, both send directions, and the window ending on its own.

The radio is faked (no AWDL here): one fake radio-backend helper script
exercises the omdrop helper contract that nearshare.airdrop.radio
speaks, and an in-process fake radio stands in for awdl0 so a real
NearShareService and a second AirDrop instance (playing the iPhone) can
find each other and transfer over ::1.

Run:  .venv/bin/python -m tests.test_airdrop_service
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="airdrop-svc-"))
os.environ["XDG_RUNTIME_DIR"] = str(TMP / "run")
os.environ["XDG_DATA_HOME"] = str(TMP / "data")
(TMP / "run").mkdir()

import nearshare.airdrop.manager as manager_mod  # noqa: E402
from nearshare.airdrop.manager import AirDrop  # noqa: E402
from nearshare.airdrop.radio import Probe, Radio, RadioError  # noqa: E402
from nearshare.airdrop.sender import AirDropPeer, AirDropSender  # noqa: E402
from nearshare.core.connection import Events, TransferRequest  # noqa: E402
from nearshare.core.service import NearShareService  # noqa: E402
from tests import _control  # noqa: E402

FAKE_HELPER = r"""#!/usr/bin/env bash
dir=$(dirname "$0")
case "$1" in
  probe)  echo '{"backend":"owl","version":"0.1.0","contract":"0.8.1","hardware":true,"missing":[{"id":"radio","say":"Your Wi-Fi network is on 5765 MHz, but AirDrop can only share channel 6, 44 or 149 with it."}]}' ;;
  start)  [ -e "$dir/refuse" ] && { echo "omdrop-discoverable: Wi-Fi isn't connected" >&2; exit 3; }
          echo "$2" > "$dir/up" ;;
  stop)   rm -f "$dir/up" ;;
  status) if [ -e "$dir/up" ]; then echo '{"visible":true,"remaining":42,"reason":"","rx_proven":false,"peer_op":""}';
          else echo '{"visible":false,"remaining":null,"reason":"stopped","rx_proven":false,"peer_op":""}'; fi ;;
  peers)  echo "2:aa:bb:cc:dd:e rssi=?"; echo "02:aa:bb:cc:dd:ee rssi=-40"; echo "not a peer" ;;
  *)      echo "usage: omdrop-discoverable start|stop|status|peers" >&2; exit 2 ;;
esac
"""

LEGACY_HELPER = r"""#!/usr/bin/env bash
echo "usage: omdrop-discoverable start <seconds> | stop | status | peers" >&2
exit 2
"""


class FakeRadio(Radio):
    """awdl0 stand-in: "up" while started, peers from a fixed table."""

    def __init__(self, mac: str, neighbours: dict[str, str] | None = None,
                 refuse: str | None = None) -> None:
        super().__init__(helper="/bin/true", elevate=False)
        self._mac = mac
        self._neighbours = neighbours or {}
        self._refuse = refuse
        self.up = False

    async def probe(self) -> Probe:
        return Probe(available=True, backend="fake", hardware=True)

    async def start(self, seconds: int) -> None:
        if self._refuse:
            raise RadioError(self._refuse)
        self.up = True

    async def stop(self) -> None:
        self.up = False

    async def status(self) -> dict:
        return {"visible": self.up, "remaining": 300 if self.up else None}

    async def peers(self) -> list[str]:
        return []

    async def neighbours(self) -> dict[str, str]:
        return dict(self._neighbours) if self.up else {}

    def address(self) -> str | None:
        return "::1" if self.up else None

    def mac(self) -> str | None:
        return self._mac


class FakeWake:
    status = "off"
    active = False

    def start(self) -> bool:
        self.active, self.status = True, "on"
        return True

    def stop(self) -> None:
        self.active, self.status = False, "off"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def control(req: dict) -> dict:
    return await _control.request(req)


async def test_helper_contract() -> None:
    helper = TMP / "helper" / "omdrop-discoverable"
    helper.parent.mkdir()
    helper.write_text(FAKE_HELPER)
    helper.chmod(0o755)
    radio = Radio(helper=str(helper), elevate=False)
    probe = await radio.probe()
    assert probe.available and probe.backend == "owl" and probe.hardware
    assert not probe.ready and "149" in probe.explain(), probe
    await radio.start(600)
    assert (helper.parent / "up").read_text().strip() == "600"
    status = await radio.status()
    assert status["visible"] is True and status["remaining"] == 42
    assert await radio.peers() == ["02:aa:bb:cc:dd:ee"]
    await radio.stop()
    assert (await radio.status())["visible"] is False
    (helper.parent / "refuse").touch()
    try:
        await radio.start(60)
        raise AssertionError("exit 3 not reported")
    except RadioError as exc:
        assert str(exc) == "Wi-Fi isn't connected", exc

    legacy = TMP / "legacy" / "omdrop-discoverable"
    legacy.parent.mkdir()
    legacy.write_text(LEGACY_HELPER)
    legacy.chmod(0o755)
    probe = await Radio(helper=str(legacy), elevate=False).probe()
    assert probe.ready and probe.backend == "awdl", probe

    assert "omdrop-owl" in Probe(available=False).explain()
    print("radio contract: probe (owl + legacy awdl), start/stop/status, "
          "peers parsing, exit 3 message")


async def test_service() -> None:
    manager_mod.STATUS_POLL = 0.2
    manager_mod.DISCOVER_POLL = 0.2

    # The "iPhone": a bare AirDrop instance with its own receiver.
    phone_downloads = TMP / "phone"
    phone_got: list[Path] = []
    phone_asked: list[TransferRequest] = []

    async def phone_accepts(req: TransferRequest) -> bool:
        phone_asked.append(req)
        return True

    phone = AirDrop("Dhiva's iPhone", phone_downloads, Events(
        on_transfer_request=phone_accepts,
        on_complete=lambda dev, paths: phone_got.extend(paths)),
        radio=FakeRadio("02:00:00:00:00:0b"), model="iPhone17,1")
    phone.wake = FakeWake()
    phone.receiver_port = 0
    await phone.set_visible(True, 10)
    phone_port = phone.receiver.port

    # The Linux box: a real NearShareService with a fake radio.
    box_downloads = TMP / "box"
    box_got: list[Path] = []

    async def box_accepts(req: TransferRequest) -> bool:
        assert req.pin == "" and req.device_name == "Dhiva's iPhone", req
        return True

    service = NearShareService(device_name="Linux Box",
                               download_dir=box_downloads, events=Events(
                                   on_transfer_request=box_accepts,
                                   on_complete=lambda dev, paths:
                                       box_got.extend(paths)))
    service.airdrop = AirDrop(
        service.device_name, box_downloads, service.events,
        radio=FakeRadio("02:00:00:00:00:0a",
                        neighbours={"::1": "02:00:00:00:00:0b"}))
    service.airdrop.wake = FakeWake()
    service.airdrop.receiver_port = 0
    service.airdrop.peer_port = phone_port
    await service.start(visible=False)
    try:
        status = await control({"cmd": "status"})
        assert status["airdrop"]["state"] == "off", status
        resp = await control({"cmd": "airdrop", "action": "discover"})
        assert "error" in resp and "off" in resp["error"], resp

        resp = await control({"cmd": "airdrop", "action": "on", "minutes": 5})
        assert resp["airdrop"]["visible"], resp
        assert 0 < resp["airdrop"]["remaining"] <= 300, resp
        resp = await control({"cmd": "airdrop", "action": "discover",
                              "seconds": 20})
        assert "error" not in resp, resp
        for _ in range(100):
            peers = (await control({"cmd": "peers"}))["peers"]
            airdrop_peers = [p for p in peers if p["protocol"] == "airdrop"
                             and p["named"]]
            if airdrop_peers:
                break
            await asyncio.sleep(0.1)
        assert airdrop_peers, peers
        assert airdrop_peers[0]["name"] == "Dhiva's iPhone"
        assert airdrop_peers[0]["model"] == "iPhone17,1"
        assert service.airdrop.wake.active, "no wake advert while looking"
        print(f"discovery: found {airdrop_peers[0]['name']!r} through the "
              "neighbour table, named via /Discover")

        # Linux -> iPhone, through the control socket like `nearshare send`.
        photo = TMP / "holiday.jpg"
        photo.write_bytes(os.urandom(700_000))
        resp = await control({"cmd": "send", "peer": "iphone",
                              "protocol": "airdrop", "files": [str(photo)]})
        assert resp == {"ok": True, "sent_to": "Dhiva's iPhone",
                        "protocol": "airdrop"}, resp
        assert [p.name for p in phone_got] == ["holiday.jpg"]
        assert sha256(phone_got[0]) == sha256(photo)
        assert phone_asked[-1].device_name == "Linux Box"
        print("send to Apple device: control-socket send delivered "
              "holiday.jpg intact")

        # Quick Share-only sends must not fall through to AirDrop.
        resp = await control({"cmd": "send", "peer": "iphone",
                              "protocol": "quickshare", "files": [str(photo)]})
        assert "error" in resp, resp

        # The send above reported completion through the same Events the
        # UI uses for both directions.
        assert [p.name for p in box_got] == ["holiday.jpg"], box_got
        box_got.clear()

        # iPhone -> Linux, into the service's AirDrop receiver.
        note = TMP / "from-phone.txt"
        note.write_text("sent from an iPhone, allegedly\n")
        box_port = service.airdrop.receiver.port
        sender = AirDropSender(phone.identity, "Dhiva's iPhone", Events(),
                               model="iPhone17,1")
        await sender.send_files(AirDropPeer(address="::1", port=box_port,
                                            iface=None), [note])
        assert [p.name for p in box_got] == ["from-phone.txt"], box_got
        assert box_got[0].read_text() == note.read_text()
        print("receive from Apple device: service's receiver saved "
              "from-phone.txt")

        # The backend ends the window on its own (its timer, or omdrop's
        # panel): the service notices and tears its side down.
        service.airdrop.radio.up = False
        for _ in range(50):
            if service.airdrop.state == "off":
                break
            await asyncio.sleep(0.1)
        assert service.airdrop.state == "off"
        assert service.airdrop.receiver is None
        assert not service.airdrop.peers
        print("window end: noticed the backend stopping, receiver closed")

        # A radio that refuses reports why, and leaves AirDrop off.
        service.airdrop.radio._refuse = "Your Wi-Fi network is on 5765 MHz"
        resp = await control({"cmd": "airdrop", "action": "on"})
        assert resp["error"] == "Your Wi-Fi network is on 5765 MHz", resp
        assert resp["airdrop"]["state"] == "off", resp
        print("refused start: error passed through, state off")
    finally:
        await service.stop()
        await phone.shutdown()


async def main() -> int:
    if os.name == "posix":      # the fake helper is a bash script
        await test_helper_contract()
    await test_service()
    print("\nAIRDROP SERVICE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
