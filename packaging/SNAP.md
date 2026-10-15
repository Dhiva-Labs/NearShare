# Packaging NearShare as a snap

This covers the strictly-confined snap defined in `snap/snapcraft.yaml`
(base `core24`, using the `gnome` extension for GTK4/libadwaita/PyGObject).
For the `.deb`, see `debian/`; this file is only about the snap.

## Contents

- [Build](#build)
- [Local install](#local-install)
- [Interfaces](#interfaces) -- what's plugged, what auto-connects, and why
- [Investigated confinement questions](#investigated-confinement-questions)
- [Known limitations of the snap specifically](#known-limitations-of-the-snap-specifically)
- [Publishing to the Snap Store](#publishing-to-the-snap-store)

## Build

```bash
snapcraft
```

Requires `snapcraft` (`sudo snap install snapcraft --classic`) and a build
backend -- LXD (`sudo snap install lxd && lxd init --auto`) or Multipass.
It builds in a clean `core24` instance, so nothing on your host machine
(your `.venv`, system Python, etc.) leaks into the result. Expect several
minutes: it fetches `network-manager` and its dependencies as a
stage-package, plus the build-only `grpcio-tools` needed to run
`tools/genproto.sh`.

Output: `nearshare_<version>_amd64.snap` on an amd64 machine and
`nearshare_<version>_arm64.snap` on an arm64 one -- `platforms:` in
`snap/snapcraft.yaml` lists both, and a native `snapcraft` run builds the one
that matches the host. See [Building for arm64](#building-for-arm64).

## Building for arm64

Nothing in the snap is architecture-specific: the python plugin pulls
manylinux `aarch64` wheels for `zeroconf`, `protobuf`, `cryptography` and
the build-only `grpcio-tools`, `network-manager` is staged from the Ubuntu
`arm64` archive, and no path in `snapcraft.yaml` names a multiarch triplet.

The arm64 snap is built natively -- on an arm64 machine, or in CI on the
`ubuntu-24.04-arm` runner. `.github/workflows/ci.yml` does that for every
push and pull request, next to the amd64 build on `ubuntu-24.04`, and for
each of them:

- checks that `meta/snap.yaml` declares the right architecture and that
  every ELF file in the snap (the staged `nmcli` and its libraries, the
  compiled wheels) is built for it;
- installs the snap and runs `nearshare --help`;
- starts the GTK application under `xvfb` inside the snap's confinement and
  checks that its window opens and its receive service listens
  (`tools/gui-smoke.py`);
- uploads the `.snap` as the `nearshare-snap-amd64` / `nearshare-snap-arm64`
  workflow artifact.

CI never talks to the Snap Store. To put the arm64 build on the Store, take
the artifact from a green run on the commit you are releasing (or build it
on an arm64 machine), check that its version matches the amd64 revision,
then upload it to the same channel:

```bash
snapcraft upload --release=stable ./nearshare_<version>_arm64.snap
```

## Local install

```bash
sudo snap install --dangerous ./nearshare_1.0.0_amd64.snap
```

`--dangerous` is required for a locally-built, unsigned snap. Then wire up
the manual-connect interfaces (see below) -- none of these are needed just
to launch the app, only for the features they gate:

```bash
sudo snap connect nearshare:network-manager
sudo snap connect nearshare:bluez
sudo snap connect nearshare:removable-media
sudo snap connect nearshare.gui:network-manager
sudo snap connect nearshare.gui:bluez
sudo snap connect nearshare.gui:removable-media
```

(Both apps declare the same plugs; connect both, or just the one you
actually use -- the CLI's `send`/`send-picker` path and the GUI's Direct
mode / BLE features are independent.)

Then:

```bash
nearshare status          # CLI, via the `nearshare` alias
nearshare.gui              # GUI
```

## Interfaces

| Interface          | Connect     | Why |
|---------------------|-------------|-----|
| `network`           | auto        | Outbound TCP to a peer (`connection.py`), and the UDP socket python-zeroconf uses for mDNS. |
| `network-bind`      | auto        | The inbound TCP receive server (`service.py`'s `asyncio.start_server`), the JSON control Unix socket, and zeroconf's own bound multicast socket. |
| `home`              | auto        | Reading files to send, and writing received files to `~/Downloads` (`service.py`'s default `download_dir`). See the `HOME` override below -- without it this would silently land in the per-revision snap sandbox instead of the real `~/Downloads`. |
| `network-manager`   | **manual**  | Direct-mode hotspot (`hotspot.py`) shells out to `nmcli`, which needs D-Bus access to the host's NetworkManager to actually create/tear down the AP. |
| `bluez`             | **manual**  | BLE trigger advertising and scanning (`ble.py`) talk to `org.bluez` over the system D-Bus directly (BlueZ's `LEAdvertisingManager1`/`Adapter1`/`Device1`), not through any CLI tool. |
| `removable-media`   | **manual**  | `send`/`send-picker` accept arbitrary file paths; `home` alone doesn't cover files under `/media/**` or `/mnt/**` (e.g. sending something off a USB stick). |
| `desktop`, `desktop-legacy`, `wayland`, `x11`, `opengl`, `gsettings`, etc. | auto (via extension) | Provided by the `gnome` extension (`gnome-46` content snap for core24), which also supplies the GTK4/libadwaita/PyGObject runtime itself -- see [Build](#build). Not hand-listed in `snap/snapcraft.yaml` to avoid fighting the extension's own plug declarations. |

`network` and `network-bind` auto-connect on install; `home` auto-connects
too (subject to the caveat below). `network-manager`, `bluez`, and
`removable-media` are all "manual connect" interfaces by snapd policy --
strict confinement passing automated review does **not** exempt them from
requiring an explicit `snap connect` (see [Local install](#local-install)).

### Interfaces deliberately *not* plugged

- **`network-observe`** -- not needed. See [self-IP detection](#does-self-ip-detection-work-under-confinement) below: the code's own fallback covers it.
- **`avahi-observe`/`avahi-control`** -- not needed. See [mDNS via zeroconf](#does-python-zeroconf-work-in-strict-confinement) below: python-zeroconf never talks to Avahi in the first place.

## Investigated confinement questions

### Does self-IP detection work under confinement?

`nearshare/core/mdns.py`'s `_local_ipv4s()` (used to filter this
machine's own advertisement out of the peer list) tries `ip -j -4 addr`
first and falls back to a UDP-connect trick (`connect()` a UDP socket to
`8.8.8.8:80`, no packet actually sent, then read back the picked source
address) if that fails.

The snap does **not** stage `iproute2`, so `ip` is not on `$PATH` inside
the confined app; `subprocess.run(["ip", ...])` raises `FileNotFoundError`,
which is a subclass of `OSError` and is already caught by the `except
(OSError, subprocess.SubprocessError, ValueError)` in that function --
confirmed by reading the code, not just assumed. It falls straight
through to the UDP-connect branch.

That fallback suffices here: it's the *only* mechanism `_local_addresses()`
(the function that decides what address to actually advertise over mDNS)
ever uses -- it never calls `ip` at all, in or out of a snap. So the
address `_local_ipv4s()` recovers via the same trick is the same address
this machine is advertising, and self-filtering keeps working correctly.
`network-observe` (which would be needed for raw netlink/`ip addr`
access) was left unplugged deliberately -- it would only ever change
behavior on a multi-homed machine wanting every local IP filtered, not
just the default-route one, which isn't a scenario this app's self-filter
relies on.

Caveat: this reasoning wasn't validated against a live multi-interface
machine inside the built snap (no such test rig here) -- it's confirmed
by reading `mdns.py`'s actual exception handling and call graph, not by
observing the fallback fire at runtime.

### Does python-zeroconf work in strict confinement?

Yes, and not coincidentally: `mdns.py` uses `zeroconf`/`zeroconf.asyncio`
directly, which implements the mDNS protocol itself by joining the
`224.0.0.251:5353` multicast group on its own UDP socket -- it does not
shell out to, or D-Bus into, the system's Avahi daemon at all. That means
the only interfaces this needs are the ordinary `network`/`network-bind`
pair (both auto-connected), not `avahi-observe`/`avahi-control` (which
exist specifically for snaps that talk to Avahi over D-Bus, a different
approach this codebase doesn't take). No `avahi-*` interface is plugged.

Caveat: not verified against a live second mDNS peer (a phone or another
machine) from inside the built, installed snap in this environment --
the reasoning above is architectural (based on reading `mdns.py` and
knowing how python-zeroconf is implemented), not an observed multicast
exchange.

### HOME redirection (why `environment: HOME: $SNAP_REAL_HOME` is set)

By default snapd points a confined app's `$HOME` at
`~/snap/nearshare/<revision>` (`$SNAP_USER_DATA`), not the user's actual
home directory. `nearshare/core/service.py`'s default `download_dir` is
literally `Path.home() / "Downloads"`, which reads `$HOME` -- left alone,
received files would land in `~/snap/nearshare/current/Downloads`
instead of the `~/Downloads` that Nautilus and every other app shows,
which would defeat the point of a file-sharing app. `snap/snapcraft.yaml`
overrides `HOME` to `$SNAP_REAL_HOME` for both apps to fix this.

This is deliberately *not* a blanket fix-everything change: `$XDG_CONFIG_HOME`
etc. are left at whatever snapd already points them to (the per-revision
sandbox), which is correct for `nearshare/core/names.py`'s device-name
cache -- that's app-private state, not something the user needs to find
in Nautilus. GTK/GLib/dconf paths are unaffected by this too, since they
go through their own dedicated interfaces (`gsettings`, `desktop-legacy`,
etc., supplied by the `gnome` extension) which are written against the
*real* home directory's AppArmor path glob regardless of the app's own
`$HOME` env var.

### `nearshare install`/`uninstall` mostly don't work from the snap

`bin/nearshare install` (and the `cli.py install`/`uninstall`
subcommands it's a thin wrapper for) write to `~/.local/bin`,
`~/.local/share/applications`, `~/.local/share/nautilus/scripts`, and
`~/.local/share/nautilus-python/extensions`. All four are under the
dot-directory `~/.local`, and the `home` interface's standard AppArmor
rule explicitly excludes dotfiles/dot-directories under `$HOME` -- it
only grants access to non-hidden paths. This holds regardless of the
`HOME` override above (that changes what path the app *computes*, not
what the `home` interface's fixed, real-home-directory AppArmor rule
permits).

Net effect: running `nearshare install`/`uninstall` from the snap will
fail (or silently no-op) on most of its steps. This isn't a snap
packaging bug to fix -- it's inherent to what `home` grants under strict
confinement. If you want the PATH symlink, desktop launchers, or Nautilus
integration, use the `.deb` or a from-source install instead (see
`README.md`).

## Known limitations of the snap specifically

- **No Nautilus right-click integration.** `bin/nearshare install`'s
  Nautilus script and the top-level context-menu extension both need to
  write into `~/.local/share/nautilus*`, which a strictly-confined snap
  can't do (see above) -- and even if it could, Nautilus (unless it's
  the very same snap) has no straightforward way to exec into another
  confined snap's binary for a script it's running. This is a hard
  limitation of strict confinement, not a bug: **the `.deb`/source
  install covers Nautilus integration; the snap does not, and can't.**
- **`nearshare install`/`uninstall` mostly no-op**, per the dedicated
  section above.
- **The GNOME custom-keybinding step is unaffected** -- `cli.py`'s
  `_print_shortcut_commands` only *prints* a `gsettings` one-liner, it
  never writes anything itself. If you bind the shortcut yourself, point
  it at `nearshare.toggle`'s real path, e.g.
  `/snap/bin/nearshare toggle` (not `/snap/bin/nearshare.gui`).
- **Direct-mode hotspot (`nmcli`) and BLE need a manual `snap connect`**
  the first time -- see [Local install](#local-install). Without them,
  `hotspot.py`/`ble.py`'s own error handling degrades gracefully (BLE
  falls back to mDNS-only automatically per `README.md`'s own design;
  `nmcli` failures surface as a readable `HotspotError` in the UI) rather
  than crashing, so this is a "feature missing" case, not a broken one.

## Publishing to the Snap Store

```bash
snapcraft login
snapcraft register nearshare
snapcraft upload --release=stable ./nearshare_1.0.0_amd64.snap
```

(`upload` used to be `push`; if your snapcraft is old enough to only know
`push`, use `snapcraft push --release=stable ./nearshare_1.0.0_amd64.snap`
instead. This project builds with snapcraft 9.0.1, which uses `upload`.)

**Strict confinement, as configured here, passes automated review and can
go straight to the `stable` channel with no human review wait** -- that's
the entire reason this snap is built as `confinement: strict` rather than
`classic`. Prior experience in this house: a `classic`-confinement snap
is *always* held for manual review by a human at Canonical before it can
release to `stable` (classic confinement bypasses the sandbox entirely,
so it's treated as inherently higher-risk, regardless of what the snap
actually does) -- that queue can take anywhere from hours to weeks with
no SLA. Strict confinement has no such queue: automated `review-tools`
checks run in seconds, and `--release=stable` takes effect immediately
once they pass. Don't downgrade to `classic` confinement to work around a
plug/interface problem without knowing this tradeoff -- it trades a
packaging fix now for an indefinite wait later.

If `snapcraft upload` reports the automated review rejected the snap
(rather than just queuing it), the failure reason is almost always an
interface/metadata problem (e.g. a plug snapd doesn't recognize, or a
`command:` pointing at a file that isn't actually there) -- rerun
`snapcraft try` or inspect the built snap's contents (`unsquashfs -l
nearshare_1.0.0_amd64.snap`) before assuming it's a review-policy issue.

### Icon note

`snap/snapcraft.yaml`'s top-level `icon:` points at
`data/icons/dev.dhivalabs.nearshare.svg` directly. If the Snap Store's
listing validation rejects SVG (some older tooling wants PNG), regenerate
it as a PNG and put it under `snap/gui/icon.png` instead (that path is
snapcraft-recognized and doesn't require touching `data/`):

```bash
rsvg-convert -w 256 -h 256 data/icons/dev.dhivalabs.nearshare.svg -o snap/gui/icon.png
```

This wasn't done proactively since the current `icon:` value builds fine
as-is (see [Build](#build)) and no SVG rejection was actually observed.
