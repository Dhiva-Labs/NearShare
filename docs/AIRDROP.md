# AirDrop

NearShare can send to and receive from iPhones, iPads and Macs over
AirDrop, alongside Quick Share. Apple devices see this machine in their
share sheet like any other AirDrop target, and appear in NearShare's
device list (and `nearshare peers`) while AirDrop is on.

> **Status: built and tested against itself, not yet against a real
> Apple device.** The protocol layer (TLS, `/Discover`, `/Ask`,
> `/Upload`, cpio and DVZip) passes loopback and hostile-input tests
> (`tests/test_airdrop.py`, `tests/test_airdrop_service.py`), and
> follows what iOS 26 is recorded doing on the wire. Interop with an
> iPhone or Mac still has to be confirmed on hardware.

## What you need

AirDrop doesn't run over your Wi-Fi network. It runs over **AWDL**,
Apple's own Wi-Fi peer-to-peer link, which a normal Linux Wi-Fi driver
doesn't speak. NearShare doesn't bring up AWDL itself. It uses an
installed **radio backend**, the same one [omdrop] uses:

| Backend | Wi-Fi | Notes |
|---|---|---|
| [omdrop-owl] | MediaTek MT7925 (tested), Intel AX201/AX211/BE (tested), others untested | AWDL in userspace through [OWL] |
| [omdrop-awdl] | Broadcom in M1/M2 Macs running Asahi Linux | AWDL in the Wi-Fi firmware |

Then, on the Apple device, set AirDrop to **Everyone for 10 Minutes**.
"Contacts Only" needs an Apple-signed identity that can't be made off
Apple hardware, so NearShare is never visible to it.

### Installing omdrop-owl

On Debian or Ubuntu (see its README for Arch):

```bash
sudo apt install build-essential cmake git pkg-config dpkg-dev \
  libev-dev libpcap-dev libnl-3-dev libnl-genl-3-dev
git clone https://github.com/t4t5/omdrop-owl.git && cd omdrop-owl
tools/build-deb.sh
sudo apt install ./omdrop-owl_*.deb
```

It installs `/usr/lib/omdrop/omdrop-discoverable`, which NearShare
finds on its own, plus a polkit rule that lets the person at the
computer turn AirDrop on without a password.

Check what stands in the way:

```bash
/usr/lib/omdrop/omdrop-discoverable probe
nearshare airdrop status
```

### Router channel (shared mode)

On cards that can keep Wi-Fi connected while AirDrop is on (MediaTek),
AWDL shares the access point's channel, so **the router has to be on
channel 6, 44 or 149**, the channels Apple devices use for AWDL. If the
5 GHz network is on another channel (for example 153), move it to 149
in the router's settings. Intel cards don't have this constraint, but
they drop Wi-Fi for as long as AirDrop is visible.

### MediaTek MT7921 / MT7922

These cards use the `mt7921e` driver, which omdrop-owl hasn't tested yet
(the [airdrop-mt7921] project shows the chip can do AirDrop). To try it,
opt in and give the driver a profile line. The line copies the MT7925's
settings, including turning off mt76 runtime power management, which
otherwise stops monitor-mode receive on this chip:

```bash
echo 1 | sudo tee /etc/omdrop/allow-untested
echo 'mt7921e  untested  shared  station  yes  yes  MediaTek MT7921/MT7922' \
  | sudo tee /etc/omdrop/owl-profiles
```

If it works, please report it to omdrop-owl ("Adding your card" in its
README) so the line can ship for everyone.

### Firewall

The receiver listens on TCP 8771 on the AWDL interface only. With `ufw`
active, allow it from link-local addresses:

```bash
sudo ufw allow in on awdl0 from fe80::/10 to any port 8771 proto tcp
```

### Windows

Windows Wi-Fi drivers can't do monitor mode or frame injection, so AWDL
needs a separate radio there: an ESP32 on USB, driven by
[omdrop-owl-windows], which runs OWL on Windows against it. NearShare
talks to it through the same helper contract as on Linux, through
`omdrop-discoverable.exe`, which that project's planned Windows service
provides in place of pkexec. NearShare looks for it in
`%ProgramFiles%\omdrop-owl\`, `%ProgramFiles%\OmdropOwl\` and on
`PATH`. Point `NEARSHARE_AIRDROP_HELPER` at it if it's elsewhere.

As of 2026-10 that project has OWL working on Windows (an IPv6 ping to
an iPhone over AWDL through an ESP32-S3), but not the helper service
yet. Until it ships, the AirDrop switch on Windows says what's missing
and Quick Share works as normal. The Bluetooth wake advert does work on
Windows: the WinRT publisher accepts Apple's manufacturer data, and it
has been seen on the air.

### Not in the snap

The radio backend runs as root outside NearShare, which strict snap
confinement can't reach. Use the `.deb` (`ppa:dhiva-labs/apps`) or a
source install for AirDrop.

## Using it

In the window, turn on **Visible to Apple devices**. It stays on for 10
minutes, as Apple's own "Everyone for 10 Minutes" does. While it's on:

- Apple devices that open the share sheet see this machine. When one
  sends, NearShare asks you to accept or decline, as it does for Quick
  Share, but without a PIN (AirDrop has none). Files land in
  `~/Downloads`. A shared link opens a dialog with **Open Link** and
  **Copy**, and nothing is opened unless you click.
- Nearby Apple devices appear in **Nearby devices** with an "AirDrop"
  subtitle. To find them, NearShare broadcasts Apple's Bluetooth
  AirDrop advert, which wakes their AirDrop receivers. **Send files…**
  sends, and the recipient taps Accept.

From a terminal:

```bash
nearshare airdrop on          # visible for 10 minutes (on 0: until off)
nearshare airdrop peers       # look for Apple devices for 30 s
nearshare send photo.jpg --to "iPhone" --via airdrop
nearshare airdrop off
nearshare airdrop status      # including what the radio backend reports
```

The right-click **Send with NearShare** picker lists Apple devices too
while AirDrop is on in the running app.

## How it works

```
NearShare (MIT)                              radio backend (separate package)
-------------------------------------------  --------------------------------
nearshare/airdrop/manager.py  window, peers  omdrop-discoverable start/stop/
nearshare/airdrop/radio.py  ── pkexec ─────▶   status/peers/probe
nearshare/airdrop/receiver.py  [awdl0]:8771  OWL or Broadcom firmware: awdl0
nearshare/airdrop/sender.py  ──▶ peer:8770   _airdrop._tcp announcements
nearshare/airdrop/wake.py  BLE Continuity advert (BlueZ)
nearshare/airdrop/archive.py  cpio + DVZip
nearshare/airdrop/http.py  HTTP/1.1 over TLS
```

- **Window.** `start <seconds>` (through `pkexec`) brings `awdl0` up and
  puts the `_airdrop._tcp` announcement on the air, naming port 8771.
  NearShare then binds its receiver to `awdl0`'s link-local address,
  and polls `status` so the window ends cleanly when the backend's
  timer runs out, or when something else stops it.
- **Receiving.** `/Discover` answers with our name. `/Ask` lists the
  files and waits for your answer: 200 to accept, 403 to decline. An
  `/Upload` is accepted only if its `TransferID` header names an `/Ask`
  you accepted (older senders without IDs are matched by address). The
  body is DVZip (4-byte block headers, each block stored or zlib) or
  gzip around an odc cpio archive. It is decoded as it arrives and
  written straight to disk. Names are reduced to safe base names, iOS's
  `NSIRD_*` wrapper directory and `._` AppleDouble files are dropped,
  nothing is overwritten, and every size is bounded.
- **Sending.** Peers come from `awdl0`'s neighbour table (OWL adds each
  AWDL peer it syncs with) and the backend's `peers` list. An address
  is the EUI-64 link-local form of the peer's MAC. Each is asked for
  its name with `/Discover` on port 8770, with the Bluetooth wake
  advert on. A send is `/Discover`, `/Ask` (TransferID announced), then
  a chunked `/Upload` of DVZip cpio on the same TLS connection, with
  sharingd's header set: no `Host`, no `Accept-Encoding`.
- **Identity.** One self-signed RSA certificate, made on first use, at
  `~/.local/share/nearshare/airdrop/`. In Everyone mode neither side
  verifies the other.

## Limitations

- Everyone mode only (see above). There is no "Contacts Only".
- Speed is the radio backend's. Reported figures: about 1.3 MB/s
  receiving and 0.5 MB/s sending on an MT7925, and seconds per photo
  on Intel cards.
- An iPhone is only reachable while unlocked, and its AirDrop server
  only listens in bursts. A send keeps dialling for 30 s with the wake
  advert on.
- MediaTek MT7921/7922 cards share one chip between Wi-Fi and
  Bluetooth. Reconfiguring the Wi-Fi side can reset an advert that is
  already running, so NearShare starts the wake advert only after the
  window is up.

## Credits

AirDrop is a trademark of Apple Inc. NearShare is not affiliated with
Apple; the name is used only to describe the protocol it interoperates
with.

- **[omdrop]** by Brent Kearney: the radio-backend contract NearShare
  speaks, and the iOS 26 wire findings in its receiver.
- **[omdrop-owl]** by t4t5: the userspace AWDL backend for non-Apple
  Wi-Fi cards.
- **[OWL] and [OpenDrop]** by the Secure Mobile Networking Lab
  (SEEMOO), TU Darmstadt: the open AWDL implementation and the original
  AirDrop research (Stute et al., USENIX Security 2019).
- **[airdrop-mt7921]** by jedbillyb: the measurements behind the
  `/Upload` header set, the TransferID binding, and the MT7921 notes.

NearShare's AirDrop code is its own (MIT). No code from these projects
is included; omdrop-owl and OWL run as a separately installed package.

[omdrop]: https://github.com/brentkearney/omdrop-plugin
[omdrop-owl]: https://github.com/t4t5/omdrop-owl
[omdrop-awdl]: https://github.com/brentkearney/omdrop-awdl
[OWL]: https://github.com/seemoo-lab/owl
[OpenDrop]: https://github.com/seemoo-lab/opendrop
[airdrop-mt7921]: https://github.com/jedbillyb/airdrop-mt7921
[omdrop-owl-windows]: https://github.com/dantdmdarealguy/omdrop-owl-windows
