# Building the `nearshare` .deb

Target: Ubuntu 24.04 (noble), `Architecture: all` (pure Python 3 + GTK4/
libadwaita via PyGObject — nothing to compile).

## One package for every architecture

The package is `Architecture: all`: nothing is compiled, and the generated
protobuf bindings are plain Python. The same `.deb` therefore installs on
amd64 and arm64. `.github/workflows/ci.yml` builds it on both
`ubuntu-24.04` and `ubuntu-24.04-arm`, installs it on the machine that built
it, runs `nearshare --help`, runs `tests/` against the installed copy and the
distribution's own `python3-protobuf`/`python3-zeroconf`/`python3-cryptography`,
starts the GTK app headlessly (`tools/gui-smoke.py`), and finally checks that
the two builds contain identical file trees. The `.deb` from each run is kept
as the `nearshare-deb-built-on-x64` / `nearshare-deb-built-on-arm64`
workflow artifact.

## Why this layout, not a Python dist-packages install

Upstream ships no `setup.py` or `pyproject.toml` — `nearshare/cli.py`'s
`install`/`uninstall` subcommands are a *from-source* dev-desktop-integration
tool (symlinks `bin/nearshare` into `~/.local/bin`, writes `.desktop` files
into `~/.local/share/applications`, etc.), not a packaging mechanism, and
adding a build-only `pyproject.toml` at the project root was out of scope for
this packaging pass (see the task this package was built under). So instead
of pybuild/dh-python installing into `/usr/lib/python3/dist-packages`, this
package uses the same shape as the BranchPilot `.deb` in this house:

- The whole `nearshare/` Python package tree is installed verbatim under
  `/usr/share/nearshare/nearshare/`.
- `/usr/bin/nearshare` is a thin wrapper (`debian/nearshare.wrapper`) that
  exports `PYTHONPATH=/usr/share/nearshare` and execs
  `/usr/bin/python3 -m nearshare.cli "$@"`.

Because `PYTHONPATH` is an exported environment variable, every place the
app re-launches itself — `nearshare gui` (`os.execv` in `cli.cmd_gui`),
`nearshare on`/`toggle` auto-starting the GUI when nothing is running
(`cli._launch_gui`, a `subprocess.Popen`), and the Nautilus script/extension
calling back into `nearshare send-picker` — inherits it too, so
`python3 -m nearshare...` keeps resolving no matter how deep the call
chain goes. `nearshare/__main__.py`'s own dist-packages fallback for `gi`
becomes dead code on a system python3 (which already sees `python3-gi`
directly) but is harmless.

## Generated protobuf bindings

`nearshare/proto/*_pb2.py` are gitignored upstream — they're tied to the
exact protobuf runtime version that generated them. `debian/rules`
regenerates them at build time via `tools/genproto.sh`, using whatever
`python3-grpc-tools` (the actual Debian/Ubuntu package name — **not**
`python3-grpcio-tools`, which does not exist in noble) this build
Build-Depends on, so the generated code is always compiled against the same
protobuf runtime this package Depends on (`python3-protobuf`). This was
verified end to end: bindings regenerated with noble's `python3-grpc-tools`
1.14.1 import cleanly against noble's `python3-protobuf` 3.21.12 (old-style,
non-versioned codegen — no `ValidateProtobufRuntimeVersion` guard to trip).

If `python3-grpc-tools` isn't installed, `debian/rules override_dh_auto_build`
fails immediately with a clear message telling you which package to install,
rather than a confusing traceback deep inside `tools/genproto.sh`.

## Build dependencies

```bash
sudo apt install debhelper python3 python3-grpc-tools
# or, from a clean checkout:
sudo apt build-dep .
```

Everything else needed to run the app (`python3-gi`, `gir1.2-gtk-4.0`,
`gir1.2-adw-1`, `python3-zeroconf`, `python3-protobuf`,
`python3-cryptography`) is a runtime Depends, not a build dependency — the
build itself never imports them.

## Build

From a clean checkout of this repository:

```bash
dpkg-buildpackage -us -uc -b
```

This produces `../nearshare_1.0.2-1_all.deb` (plus a `.changes` and
`.buildinfo` next to it, one directory up from the checkout).

To also build the source package (used for the PPA — see `packaging/PPA.md`):

```bash
dpkg-buildpackage -S -us -uc
```

`debian/source/format` is `3.0 (native)` (single git checkout, no separate
upstream tarball). `debian/source/options` excludes `.git`, the developer's
`.venv/`, `__pycache__/`/`*.pyc`, and the generated `nearshare/proto/*_pb2.py`
from the source tarball — without that, a native-format build happily packs
the entire `.git/` history and the dev virtualenv into the `.tar.xz`.

You'll see one harmless warning: `dpkg-source: warning: native package
version may not have a revision` — this package intentionally uses the
`1.0.2-1` debian-revision style version (per this project's packaging
convention) on top of a native source format; it still builds correctly.

## Verify the file list

```bash
dpkg -c ../nearshare_1.0.2-1_all.deb
```

Expected layout:

```
./usr/bin/nearshare                                                (wrapper, 0755)
./usr/share/applications/dev.dhivalabs.nearshare.desktop
./usr/share/applications/nearshare-send.desktop
./usr/share/applications/nearshare-toggle.desktop
./usr/share/doc/nearshare/changelog.Debian.gz
./usr/share/doc/nearshare/copyright
./usr/share/doc/nearshare/README.md.gz
./usr/share/doc/nearshare/SHORTCUT.md
./usr/share/icons/hicolor/scalable/apps/dev.dhivalabs.nearshare.svg
./usr/share/nautilus-python/extensions/nearshare_menu.py          (system-wide right-click item, see below)
./usr/share/nearshare/nearshare/...                                (the app; cli.py, core/, ui/, proto/*_pb2.py freshly generated)
```

`lintian ../nearshare_1.0.2-1_all.deb` should report only two informational
warnings: `initial-upload-closes-no-bugs` (irrelevant outside Debian's NEW
queue) and `no-manual-page` (nice-to-have, not added in this pass).

## Nautilus integration: system-wide, not per-user

As of `1.0.2-1`, `sudo apt install ./nearshare_*.deb` is a *true* one-step
install for the Nautilus right-click "Send with NearShare" item — no
separate `nearshare install` run is needed by any user, for any user, on
this machine.

**The top-level context-menu item (nautilus-python extension).**
`debian/extra/nearshare_menu.py` is installed by `debian/rules
override_dh_auto_install` straight to
`/usr/share/nautilus-python/extensions/nearshare_menu.py`. That directory
is confirmed to be nautilus-python's **system-wide, all-user** plugin
directory two ways:

```bash
dpkg -L python3-nautilus | grep nautilus-python/extensions
# -> /usr/share/nautilus-python
# -> /usr/share/nautilus-python/extensions
cat /usr/share/doc/python3-nautilus/README.Debian
# "Plugins are loaded by default from two locations:
#   /usr/share/nautilus-python/extensions - all-user plugin directory
#   ~/.local/share/nautilus-python/extensions - per-user plugin directory"
```

(nautilus-python's own upstream `README.md`, also shipped in
`/usr/share/doc/python3-nautilus/`, documents a third, less relevant path
too: `$XDG_DATA_DIRS/nautilus-python/extensions`, of which
`/usr/share/nautilus-python/extensions` is the default entry on a stock
Ubuntu `$XDG_DATA_DIRS`.)

`debian/extra/nearshare_menu.py` is a hand-kept copy of
`nearshare/cli.py`'s `_NAUTILUS_EXTENSION_TEMPLATE`, with `{launcher}`
hardcoded to `/usr/bin/nearshare` — the deb's wrapper path — instead of
being filled in per-user from `_installed_bin_path()`. It is **not**
generated from `cli.py` at build time (this package only touches
`debian/**`); if the template in `cli.py` changes, update
`debian/extra/nearshare_menu.py` to match by hand.

`python3-nautilus` is a `Recommends` (see `debian/control`), not a
`Depends`, so a plain `apt install`/`apt install ./nearshare_*.deb` pulls
it in by default and the item works immediately after a `nautilus -q` —
but the package still installs fine, and nothing else breaks, if it's
declined or later removed (Nautilus just never imports the extension
file).

**The Nautilus Scripts submenu fallback is per-user only — not shipped
system-wide.** Nautilus scripts (the "Scripts" submenu, as opposed to the
nautilus-python top-level item above) are loaded only from
`$XDG_DATA_HOME/nautilus/scripts` (normally
`~/.local/share/nautilus/scripts`). Unlike nautilus-python's extensions,
there is no `$XDG_DATA_DIRS`/system-wide equivalent search path for
scripts — confirmed by inspecting the installed `nautilus`/`nautilus-data`
packages (`dpkg -L nautilus nautilus-data | grep -i script` finds nothing)
and there being no such directory documented anywhere in Nautilus's own
packaging. So this package does **not** attempt to fake a system-wide
Nautilus script; the top-level extension above is this package's only
(and sufficient, since it covers a superset of what the script does)
system-wide integration point. Anyone who additionally wants the per-user
Scripts submenu entry can still get it by running `nearshare install`
themselves — that per-user path in `nearshare/cli.py` is unchanged.

**Cache refresh and restart.** `debian/postinst` best-effort runs
`update-desktop-database` and `gtk-update-icon-cache` on `configure` (and
`debian/postrm` runs the same on `remove`/`purge`), so the `.desktop`
entries and the app icon are picked up without a re-login in the common
case. Neither `postinst` nor `postrm` restarts Nautilus for any logged-in
user — killing another user's file-manager process from a root-run
maintainer script would be surprising on a shared/multi-user machine, and
Nautilus (like any nautilus-python extension) only loads new extension
files at startup anyway. `postinst` prints what to do instead: log out and
back in, or run `nautilus -q` once, per user, to pick up the new
right-click item.

## The deb vs. the snap for Nautilus integration

This is the full-featured install. The strictly-confined snap (see
`packaging/SNAP.md`) **cannot** provide any Nautilus integration at all —
neither the top-level extension nor the Scripts submenu — because both
require writing into `~/.local/share/nautilus*`/`/usr/share/nautilus-python`
and being loaded by a Nautilus process running outside the snap's sandbox;
strict confinement blocks exactly that. If right-click "Send with
NearShare" matters, install the `.deb`, not the snap.

## Test-install without root

To sanity-check the wrapper and Python import paths without `sudo`:

```bash
mkdir -p /tmp/qs-test && dpkg-deb -x ../nearshare_1.0.2-1_all.deb /tmp/qs-test
PYTHONPATH=/tmp/qs-test/usr/share/nearshare python3 -m nearshare.cli --help
```

This runs the exact same code the installed wrapper would run once
`/usr/share/nearshare` exists for real (the wrapper hardcodes that path, so
running it directly out of `/tmp/qs-test` requires either installing the
package or overriding `PYTHONPATH` as above and invoking the module directly
rather than `/tmp/qs-test/usr/bin/nearshare`).

## Install and remove

```bash
sudo apt install ./nearshare_1.0.2-1_all.deb   # pulls in Depends and Recommends (incl. python3-nautilus) automatically
sudo apt remove nearshare
```

After install, each logged-in user needs to log out and back in, or run
`nautilus -q` once, for Files to pick up the new right-click item (see
"Nautilus integration" above) — nothing else is required.

## Known gaps / follow-ups

- No man page for `nearshare` (lintian: `no-manual-page`) — cosmetic, not
  required for the package to work.
- `Homepage:` is left out of `debian/control` — no project URL was found in
  the repo (README/PLAN.md); add one if/when the project gets a public repo
  URL.
- `packaging/PPA.md` (not owned by this pass — `debian/**` and
  `packaging/DEB.md` only) still shows `1.0.0-1`/`1.0.0~noble1` as the
  worked example version throughout its "Version convention" and
  "Rebuilding for another series" sections. Now that `debian/changelog`'s
  plain-deb version has moved to `1.0.2-1`, that doc's example commands
  (`dch --newversion 1.0.0~noble1 ...`, `dput ... nearshare_1.0.0~noble1_source.changes`,
  etc.) should be bumped to the `1.0.2~noble1` equivalents before the next
  PPA upload, so the guidance matches the actual version being shipped.
