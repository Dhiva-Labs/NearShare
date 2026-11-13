# Changelog

Versions up to 1.0.8 are described in `debian/changelog`. The snap and the application have
shared one version number since then; the Debian package keeps its own `~noble1` revision.

## Unreleased

## 1.1.0 - 2026-11-14

### Added
- Drag and drop: drop files on a device to send them, or anywhere on the window to pick the device
  afterwards. A Send Files button in the header (Ctrl+O) does the same.
- The PIN is shown while sending from the main window, so it can be compared with the receiver's.
  Before, only the right-click send dialog showed it.
- Transfers can be cancelled. The button on a running transfer used to remove only the row and
  leave the transfer running.
- Incoming transfers can be accepted or declined from the desktop notification, and a notification
  with a Show in Files button says when files have arrived while the window is hidden.
- Preferences (Ctrl+,): the device name others see, the folder received files are saved to, and
  Start at login, which starts NearShare hidden. In the snap this uses the snap's own autostart.
- About dialog with the version and troubleshooting details. `nearshare --version`.
- The snap shows the command for each interface that is not connected yet (`bluez`,
  `network-manager`), with a copy button.
- Releases are published by CI from a `v*` tag: a GitHub release with the `.deb`, both snaps and the
  other packages plus checksums, the Snap Store upload for amd64 and arm64, and the PPA upload for
  every supported Ubuntu series. See `packaging/RELEASING.md`.
- Packages for other distributions, built from the `.deb` and tested in CI on each: a noarch
  `.rpm` (Fedora, RHEL, openSUSE), an Arch Linux package, an AUR PKGBUILD (built from source
  against Arch's protobuf) and a Flatpak bundle (`dev.dhivalabs.nearshare`, GNOME 51 runtime) for
  x86-64 and arm64. The `.rpm`, Arch package and Flatpak carry a pure-Python protobuf runtime
  with bindings generated for it. A published GitHub release gets them attached.

### Changed
- Device rows show an icon for the kind of device and are clickable as a whole. Transfer rows have a
  full-width progress bar, and the speed no longer counts the time spent waiting for the other
  person to accept.
- Setup tips are a single collapsed row that can be dismissed, instead of a block above the device
  list.
- The send dialog opened from Files shows the files and their size, the PIN, speed and time left, has
  a Cancel button, and offers another device after a failure instead of only Close.
- PPA packages are built for Ubuntu 24.04, 26.04 and 26.10.

### Fixed
- The receiver now notices when the sender gives up before an answer: the accept dialog closes
  instead of staying up until its 60 second timeout, and it also closes when that timeout passes.
- Device names containing `&` or `<` no longer break the device list.
- A text-only transfer finishing could mark an unrelated send as complete.

### Performance
- Progress updates reach the window at most ten times a second instead of once per 512 KB chunk.
- The desktop-integration checks (several `gsettings` calls) run off the main thread, so they no
  longer delay the window's first frame.
- The snap and the `.deb` ship compiled bytecode. Both install to read-only locations, so Python
  recompiled every module on every launch.
- The snap no longer carries the NetworkManager daemon and the libraries only it uses, or the
  build's `pip`: only `nmcli` and `libnm` are needed.

## 1.0.10 - 2026-10-16

### Added
- Linux arm64: the snap is built for `amd64` and `arm64` (core24 `platforms`). CI builds each on
  a native runner, checks that every ELF file in it is for that architecture, installs it confined
  and starts the GTK app headlessly inside the snap.
- CI builds and tests on x64 and arm64: the test suite, the `.deb` (`Architecture: all`, checked to
  hold identical files whichever architecture builds it) and both snaps.
- A UI screenshot and an animated transfer GIF in `docs/screenshots/`, and the tools that render them.
- Drafts in `packaging/` for the bluez auto-connect request and the announcement.

### Changed
- README and `packaging/SNAP.md` describe the arm64 snap and the PPA's arm64 requirement.
- No application code changed since 1.0.9.

## 1.0.9 - 2026-08-06

### Fixed
- The send picker never opened from the file manager.
