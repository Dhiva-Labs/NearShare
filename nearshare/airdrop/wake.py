"""The Bluetooth LE advert that wakes a nearby Apple device's AirDrop.

Apple devices keep their AirDrop server closed until a sender's BLE
"Continuity" AirDrop advertisement (Apple manufacturer data, TLV type
0x05) is on the air; a Mac doesn't even publish its `_airdrop._tcp`
instance before it hears one. So while NearShare is looking for, or
sending to, Apple devices it broadcasts one.

The advert carries four 2-byte truncated hashes of the sender's contact
identifiers, which is how a Contacts Only receiver decides to wake.
Without an Apple ID we send zeros, "no contact match", which wakes a
receiver set to Everyone and nothing else. The body below is the shape
current (OS 26) Apple devices send.
"""

from __future__ import annotations

import logging

from ..core.ble import BleAdvertiser, BleUnavailable

log = logging.getLogger("nearshare.airdrop.wake")

APPLE_COMPANY_ID = 0x004C
AIRDROP_TLV = 0x05


def advert_value(contact_hashes: bytes = bytes(8)) -> bytes:
    if len(contact_hashes) != 8:
        raise ValueError("four 2-byte hashes")
    body = (bytes([0x40]) + bytes(3)        # version, 3 per-device tag bytes
            + bytes(4)                       # zero padding
            + bytes([0x03])                  # hash format
            + contact_hashes
            + bytes(1))
    return bytes([AIRDROP_TLV, len(body)]) + body


class AirDropWake:
    """Start/stop the wake advert; never raises, reports why it can't."""

    def __init__(self) -> None:
        self._adv = BleAdvertiser(
            manufacturer_data={APPLE_COMPANY_ID: advert_value()},
            name="airdrop0", label="AirDrop wake advertising",
            connectable=True, interval_ms=100)
        self.status = "off"

    @property
    def active(self) -> bool:
        return self._adv.active

    def start(self) -> bool:
        try:
            self._adv.start()
            self.status = "on"
            return True
        except BleUnavailable as exc:
            self.status = f"unavailable: {exc}"
            log.info("AirDrop wake advert unavailable: %s", exc)
            return False

    def stop(self) -> None:
        self._adv.stop()
        self.status = "off"
