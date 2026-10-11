"""AirDrop for NearShareService: radio window, receiver, peers, sending.

Visibility to Apple devices is a separate, timed window, unlike Quick
Share's: the radio backend has to bring an AWDL link up (and, on some
Wi-Fi cards, take Wi-Fi down while it is up), so it is opened for a set
number of minutes as Apple's own "Everyone for 10 Minutes" is, and the
receiver lives exactly as long as that window.

Finding peers is on demand. An Apple device answers /Discover only
while woken by our Bluetooth advert, so peers are looked for only while
someone is choosing where to send (the send picker or `nearshare
peers`), with the wake advert on for that long.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import re
import time
from pathlib import Path
from typing import Callable

from ..core.connection import Events
from . import protocol
from .identity import Identity, load_or_create
from .radio import Probe, Radio, RadioError
from .receiver import AirDropReceiver
from .sender import AirDropPeer, AirDropSender, SendError
from .wake import AirDropWake

log = logging.getLogger("nearshare.airdrop")

DEFAULT_MINUTES = 10
ADDRESS_WAIT = 15.0
STATUS_POLL = 5.0
DISCOVER_POLL = 3.0
PEER_TTL = 90.0
DISCOVER_CONCURRENCY = 4


def receiver_name(name: str) -> str:
    """What an Apple device draws under our tile: bounded, and free of
    characters a share sheet would render misleadingly."""
    name = re.sub(r"[\x00-\x1f\x7f/\\​-‏‪-‮⁦-⁩]",
                  "", name).strip()
    return (name or "NearShare")[:63]


class AirDrop:
    def __init__(self, device_name: str, download_dir: Path, events: Events,
                 *, radio: Radio | None = None,
                 identity: Identity | None = None,
                 model: str = protocol.DEFAULT_MODEL) -> None:
        self.device_name = receiver_name(device_name)
        self.download_dir = download_dir
        self.events = events
        self.radio = radio or Radio()
        self.model = model
        self.receiver_port = protocol.RECEIVER_PORT
        self.peer_port = protocol.PEER_PORT
        self._identity = identity
        self.receiver: AirDropReceiver | None = None
        self.wake = AirDropWake()
        self.peers: dict[str, AirDropPeer] = {}
        self.state = "off"                  # off | starting | on | stopping
        self.visible_until: float | None = None
        self.last_error: str | None = None
        self.probe_result: Probe | None = None
        self.on_state_changed: Callable[[str], None] | None = None
        self.on_peers_changed: Callable[[dict[str, AirDropPeer]], None] | None = None
        self._lock = asyncio.Lock()
        self._watch: asyncio.Task | None = None
        self._discover: asyncio.Task | None = None
        self._discover_until = 0.0
        self._sending = 0

    # ------------------------------------------------------------ status

    @property
    def identity(self) -> Identity:
        if self._identity is None:
            self._identity = load_or_create()
        return self._identity

    @property
    def visible(self) -> bool:
        return self.state == "on"

    def status(self) -> dict:
        remaining = None
        if self.visible and self.visible_until is not None:
            remaining = max(0, int(self.visible_until - time.monotonic()))
        probe = self.probe_result
        return {
            "state": self.state,
            "visible": self.visible,
            "remaining": remaining,
            "backend": probe.backend if probe else None,
            "ready": probe.ready if probe else None,
            "explain": probe.explain() if probe else None,
            "error": self.last_error,
            "wake": self.wake.status,
            "peers": len(self.peers),
        }

    def _set_state(self, state: str) -> None:
        self.state = state
        if self.on_state_changed:
            self.on_state_changed(state)

    async def probe(self) -> Probe:
        self.probe_result = await self.radio.probe()
        return self.probe_result

    # ------------------------------------------------------------ window

    async def set_visible(self, on: bool, minutes: int = DEFAULT_MINUTES
                          ) -> bool:
        """Open or close the window. Raises RadioError with a sentence
        for people when the radio can't be brought up."""
        async with self._lock:
            if on:
                await self._open(minutes)
            else:
                await self._close(stop_radio=True)
            return self.visible

    async def _open(self, minutes: int) -> None:
        seconds = max(0, int(minutes * 60))
        self.last_error = None
        if not self.radio.installed:
            self.last_error = Probe(available=False).explain()
            raise RadioError(self.last_error)
        self._set_state("starting")
        try:
            await self.radio.start(seconds)
            address = await self._wait_for_address()
            if self.receiver is None:
                self.receiver = AirDropReceiver(
                    self.device_name, self.download_dir, self.events,
                    self.identity, model=self.model,
                    host=protocol.scoped(address, self.radio.iface),
                    port=self.receiver_port)
                try:
                    await self.receiver.start()
                except OSError as exc:
                    self.receiver = None
                    if exc.errno == errno.EADDRINUSE:
                        raise RadioError(
                            "Another AirDrop receiver (omdrop?) is already "
                            "serving this machine; turn it off first.") from None
                    raise RadioError(f"The AirDrop receiver could not "
                                     f"start: {exc}") from None
        except RadioError as exc:
            self.last_error = str(exc)
            await self._close(stop_radio=True)
            raise
        self.visible_until = (time.monotonic() + seconds) if seconds else None
        self._set_state("on")
        if self._watch is None or self._watch.done():
            self._watch = asyncio.create_task(self._watch_window())

    async def _wait_for_address(self) -> str:
        deadline = time.monotonic() + ADDRESS_WAIT
        while True:
            address = self.radio.address()
            if address:
                return address
            if time.monotonic() >= deadline:
                raise RadioError("The AirDrop link (awdl0) never came up.")
            await asyncio.sleep(0.25)

    async def _close(self, stop_radio: bool) -> None:
        if self.state == "off" and self.receiver is None:
            return
        self._set_state("stopping")
        if self._watch is not None and self._watch is not asyncio.current_task():
            self._watch.cancel()
        self._watch = None
        if self.receiver is not None:
            await self.receiver.stop()
            self.receiver = None
        if stop_radio:
            await self.radio.stop()
        self.visible_until = None
        self.peers.clear()
        self._peers_changed()
        self._set_state("off")

    async def _watch_window(self) -> None:
        """Follow the backend: its own timer ends the window, and anything
        else may stop it too (omdrop's panel, the backend's `stop`)."""
        try:
            misses = 0
            while True:
                await asyncio.sleep(STATUS_POLL)
                status = await self.radio.status()
                if status.get("visible"):
                    misses = 0
                    remaining = status.get("remaining")
                    if isinstance(remaining, int):
                        self.visible_until = time.monotonic() + remaining
                    continue
                misses += 1
                if misses >= 2:           # one miss may be a status race
                    log.info("AirDrop window ended (%s)",
                             status.get("reason") or "expired")
                    async with self._lock:
                        await self._close(stop_radio=False)
                    return
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------ peers

    def _peers_changed(self) -> None:
        if self.on_peers_changed:
            self.on_peers_changed(dict(self.peers))

    def _sender(self) -> AirDropSender:
        mac = self.radio.mac()
        return AirDropSender(
            self.identity, self.device_name, self.events, model=self.model,
            sender_id=mac.replace(":", "") if mac else None)

    async def discover(self, seconds: float = 60.0) -> None:
        """Look for nearby Apple devices for a while (extends a running
        search). The window must be open: it is what hears them."""
        self._discover_until = max(self._discover_until,
                                   time.monotonic() + seconds)
        if self._discover is None or self._discover.done():
            self._discover = asyncio.create_task(self._discover_loop())

    def stop_discovery(self) -> None:
        self._discover_until = 0.0

    async def _discover_loop(self) -> None:
        sender = self._sender()
        woke = await asyncio.to_thread(self.wake.start)
        if not woke:
            log.info("discovering without a wake advert: only devices "
                     "already listening will answer")
        limit = asyncio.Semaphore(DISCOVER_CONCURRENCY)

        async def ask(peer: AirDropPeer) -> None:
            async with limit:
                if await sender.discover(peer):
                    self._peers_changed()

        try:
            while self.visible and time.monotonic() < self._discover_until:
                found = await self._candidates()
                now = time.monotonic()
                changed = False
                for key, peer in found.items():
                    known = self.peers.get(key)
                    if known is None:
                        self.peers[key] = peer
                        changed = True
                    else:
                        known.last_seen = now
                for key in [k for k, p in self.peers.items()
                            if now - p.last_seen > PEER_TTL]:
                    del self.peers[key]
                    changed = True
                if changed:
                    self._peers_changed()
                unnamed = [p for p in self.peers.values() if p.name is None]
                if unnamed:
                    await asyncio.gather(*(ask(p) for p in unnamed))
                await asyncio.sleep(DISCOVER_POLL)
        finally:
            if not self._sending:
                await asyncio.to_thread(self.wake.stop)

    async def _candidates(self) -> dict[str, AirDropPeer]:
        """Peers the radio has heard, keyed by MAC: the backend's list and
        awdl0's neighbour table, each address derived or read directly."""
        found: dict[str, AirDropPeer] = {}
        own = (self.radio.mac() or "").lower()
        for address, mac in (await self.radio.neighbours()).items():
            if mac != own:
                found[mac] = AirDropPeer(address=address, mac=mac,
                                         port=self.peer_port,
                                         iface=self.radio.iface)
        for mac in await self.radio.peers():
            if mac != own and mac not in found:
                found[mac] = AirDropPeer(
                    address=protocol.eui64_link_local(mac), mac=mac,
                    port=self.peer_port, iface=self.radio.iface)
        return found

    def match_peer(self, needle: str) -> AirDropPeer | None:
        needle = needle.lower()
        exact = [p for p in self.peers.values()
                 if needle in (p.key.lower(), (p.name or "").lower(),
                               p.address.lower())]
        if exact:
            return exact[0]
        fuzzy = [p for p in self.peers.values()
                 if p.name and needle and needle in p.name.lower()]
        return fuzzy[0] if len(fuzzy) == 1 else None

    # ------------------------------------------------------------ sending

    async def send_files(self, peer: AirDropPeer, files: list[Path]) -> None:
        if not self.visible:
            raise SendError("Turn on AirDrop visibility first: the AirDrop "
                            "link is only up while it is on.")
        self._sending += 1
        try:
            await asyncio.to_thread(self.wake.start)
            await self._sender().send_files(peer, files)
        finally:
            self._sending -= 1
            if not self._sending and (self._discover is None
                                      or self._discover.done()):
                await asyncio.to_thread(self.wake.stop)

    async def shutdown(self) -> None:
        self.stop_discovery()
        if self._discover is not None:
            self._discover.cancel()
        async with self._lock:
            await self._close(stop_radio=True)
        if self.wake.active:
            await asyncio.to_thread(self.wake.stop)
