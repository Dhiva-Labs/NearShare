# Changelog

Versions up to 1.0.8 are described in `debian/changelog`. The snap and the application have
shared one version number since then; the Debian package keeps its own `~noble1` revision.

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
