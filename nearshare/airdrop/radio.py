"""The AWDL radio, through an omdrop radio backend.

AirDrop runs over AWDL, Apple's Wi-Fi peer-to-peer link, which needs
either Apple's Broadcom firmware or a Wi-Fi card driven from userspace
in monitor mode. NearShare does neither itself. It drives a radio
backend that already does, through the helper contract omdrop defines
(docs/radio-backend.md in omdrop):

  omdrop-discoverable start <seconds>   root, via pkexec: bring up awdl0
                                        and announce us on it
  omdrop-discoverable stop              root: tear it down
  omdrop-discoverable status --json     {"visible", "remaining", "reason", ...}
  omdrop-discoverable peers             "<mac> rssi=<n>" per AWDL peer heard
  omdrop-discoverable probe --json      what blocks this machine, if anything

Backends that implement it:
  omdrop-owl   userspace AWDL through OWL; MediaTek and Intel Wi-Fi
  omdrop-awdl  Broadcom firmware AWDL on Apple Silicon Macs (Asahi)

Keeping the radio in a separately installed package keeps NearShare
free of root helpers of its own and of the GPL radio code, and lets one
radio serve both NearShare and omdrop.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .protocol import AWDL_IFACE

log = logging.getLogger("nearshare.airdrop.radio")

HELPER_NAME = "omdrop-discoverable"
HELPER_DIRS = ("/usr/lib/omdrop", "/usr/local/lib/omdrop")
PROBE_TIMEOUT = 5.0
START_TIMEOUT = 90.0

INSTALL_HINT = ("AirDrop needs an AWDL radio backend: install omdrop-owl "
                "(MediaTek or Intel Wi-Fi) or omdrop-awdl (Apple Silicon "
                "Macs).")


class RadioError(Exception):
    """The radio could not be brought up; the message is for people."""


@dataclass
class Probe:
    available: bool                 # a helper is installed at all
    backend: str = ""
    version: str = ""
    hardware: bool = False
    missing: list[dict] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.available and self.hardware and not self.missing

    def explain(self) -> str:
        if not self.available:
            if os.environ.get("SNAP"):
                # Confinement can't reach a root helper outside the snap.
                return ("AirDrop isn't available in the snap: its radio "
                        "backend runs as root outside confinement. Use the "
                        ".deb from ppa:dhiva-labs/apps instead.")
            return INSTALL_HINT
        if self.missing:
            return " ".join(m.get("say", "") for m in self.missing).strip()
        if not self.hardware:
            return "This computer's Wi-Fi can't run AirDrop."
        return "Ready."


def find_helper() -> str | None:
    for var in ("NEARSHARE_AIRDROP_HELPER", "OMDROP_DISCOVERABLE"):
        path = os.environ.get(var)
        if path and os.access(path, os.X_OK):
            return path
    for directory in HELPER_DIRS:
        path = os.path.join(directory, HELPER_NAME)
        if os.access(path, os.X_OK):
            return path
    return shutil.which(HELPER_NAME)


def interface_ipv6(iface: str) -> str | None:
    """awdl0's link-local address, once it is usable (not tentative)."""
    try:
        rows = Path("/proc/net/if_inet6").read_text().splitlines()
    except OSError:
        return None
    for row in rows:
        cols = row.split()
        # address, ifindex, prefix len, scope, flags, name; 0x40 = tentative
        if len(cols) == 6 and cols[5] == iface and not int(cols[4], 16) & 0x40:
            address = ipaddress.IPv6Address(bytes.fromhex(cols[0]))
            if address.is_link_local:
                return str(address)
    return None


def interface_mac(iface: str) -> str | None:
    try:
        return Path(f"/sys/class/net/{iface}/address").read_text().strip()
    except OSError:
        return None


async def _run(*argv: str, timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="replace"), \
        err.decode(errors="replace")


class Radio:
    """One radio backend's helper, called the way the contract says."""

    def __init__(self, helper: str | None = None,
                 iface: str = AWDL_IFACE, elevate: bool = True) -> None:
        self.helper = helper or find_helper()
        self.iface = iface
        # Root via pkexec for start/stop; off for tests and when already root.
        self.elevate = elevate and os.geteuid() != 0

    @property
    def installed(self) -> bool:
        return self.helper is not None

    async def probe(self) -> Probe:
        if not self.helper:
            return Probe(available=False)
        try:
            code, out, _ = await _run(self.helper, "probe", "--json",
                                      timeout=PROBE_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return Probe(available=True, backend="unknown", missing=[{
                "id": "backend",
                "say": "The AirDrop radio backend did not answer."}])
        try:
            data = json.loads(out) if code == 0 else None
        except ValueError:
            data = None
        if not isinstance(data, dict) or "backend" not in data:
            # omdrop-awdl's helper predates probe and answers with usage;
            # it only installs on hardware it supports.
            return Probe(available=True, backend="awdl", hardware=True)
        missing = [m for m in data.get("missing") or []
                   if isinstance(m, dict) and isinstance(m.get("say"), str)]
        return Probe(available=True, backend=str(data.get("backend")),
                     version=str(data.get("version") or ""),
                     hardware=bool(data.get("hardware")), missing=missing)

    async def start(self, seconds: int) -> None:
        """Become discoverable for ``seconds`` (0: until stopped).

        Returns once awdl0 has a usable address. Root comes from pkexec;
        the backend's polkit policy lets the person at the seat do this
        without a password, and asks everyone else for one.
        """
        if not self.helper:
            raise RadioError(INSTALL_HINT)
        argv = [self.helper, "start", str(max(0, int(seconds)))]
        if self.elevate:
            argv.insert(0, "pkexec")
        try:
            code, out, err = await _run(*argv, timeout=START_TIMEOUT)
        except asyncio.TimeoutError:
            raise RadioError("The AirDrop radio took too long to start.") from None
        except FileNotFoundError:
            raise RadioError("pkexec is missing; install polkit.") from None
        why = (err.strip() or out.strip()).splitlines()
        why = why[-1].removeprefix(f"{HELPER_NAME}: ") if why else ""
        if code in (0, 5):          # 5: already up, window extended
            return
        if code == 3:
            raise RadioError(why or "AirDrop can't start right now; try again.")
        if code == 4:
            raise RadioError(why or "No AirDrop-capable Wi-Fi radio was found.")
        if code in (126, 127):      # pkexec: dismissed / not authorised
            raise RadioError("Permission to turn on AirDrop was not given.")
        raise RadioError(why or f"The AirDrop radio failed (exit {code}).")

    async def stop(self) -> None:
        if not self.helper:
            return
        argv = [self.helper, "stop"]
        if self.elevate:
            argv.insert(0, "pkexec")
        try:
            await _run(*argv, timeout=START_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("stopping the AirDrop radio failed: %s", exc)

    async def status(self) -> dict:
        if not self.helper:
            return {"visible": False}
        try:
            code, out, _ = await _run(self.helper, "status", "--json",
                                      timeout=PROBE_TIMEOUT)
            data = json.loads(out)
            return data if isinstance(data, dict) else {"visible": False}
        except (OSError, ValueError, asyncio.TimeoutError):
            return {"visible": False}

    async def peers(self) -> list[str]:
        """MACs of AWDL peers the radio has heard; empty when it is down.

        omdrop-owl answers this unprivileged; omdrop-awdl needs root, so
        a refusal is retried through pkexec.
        """
        if not self.helper:
            return []
        out = ""
        for argv in ([self.helper, "peers"], ["pkexec", self.helper, "peers"]):
            if argv[0] == "pkexec" and not self.elevate:
                break
            try:
                code, out, _ = await _run(*argv, timeout=PROBE_TIMEOUT)
            except (OSError, asyncio.TimeoutError):
                return []
            if code == 0:
                break
            if code != 2:           # 2 is "must run as root"
                return []
        macs = []
        for line in out.splitlines():
            mac = line.split(" ", 1)[0].strip().lower()
            if len(mac) == 17 and mac.count(":") == 5:
                macs.append(mac)
        return macs

    async def neighbours(self) -> dict[str, str]:
        """{IPv6 address: MAC} of peers in awdl0's neighbour table.

        OWL adds each peer it syncs with as a permanent neighbour, so this
        finds peers without root and without waiting for mDNS.
        """
        try:
            code, out, _ = await _run("ip", "-6", "neigh", "show", "dev",
                                      self.iface, timeout=PROBE_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return {}
        found = {}
        for line in out.splitlines() if code == 0 else []:
            cols = line.split()
            if len(cols) >= 3 and cols[1] == "lladdr" and \
                    cols[0].startswith("fe80:"):
                found[cols[0]] = cols[2].lower()
        return found

    def address(self) -> str | None:
        return interface_ipv6(self.iface)

    def mac(self) -> str | None:
        return interface_mac(self.iface)
