# Publishing `nearshare` to a Launchpad PPA

This assumes the same house PPA other Dhiva Labs Linux packages use,
`ppa:dhiva-labs/apps` (see BranchPilot's `packaging/PPA.md`) — **confirm this
is where NearShare should actually land** before the first upload; nothing
below is NearShare-specific to that PPA name.

## Version convention

`debian/changelog`'s in-tree version, `1.0.2-1`, is the plain Debian-style
version for a standalone `.deb` (see `packaging/DEB.md`). Launchpad PPA
uploads use a distinct, per-series suffix instead, so the same upstream
version can be built once per targeted Ubuntu release without version
collisions:

```
1.0.2~noble1     # noble (24.04) — first PPA upload of 1.0.2
1.0.2~jammy1     # jammy (22.04) — same source, rebuilt for jammy
```

The `~` sorts *before* nothing in Debian version comparison, so
`1.0.2~noble1 < 1.0.2`, keeping PPA builds ordered before a hypothetical
plain `1.0.2-1` release of the same upstream version.

Before building the source package for upload, add a new changelog entry
(don't reuse `1.0.2-1`'s entry — Launchpad rejects re-uploads of a version
it's already seen, successful or not):

```bash
dch --newversion 1.0.2~noble1 --distribution noble \
    "PPA build for noble."
```

## Why this package is straightforward for the PPA (unlike a Flutter build)

`nearshare` has no vendored SDK problem: Launchpad's build farm has no
outbound network, and this package's only build step
(`debian/rules override_dh_auto_build`) needs `python3-grpc-tools` to
regenerate the protobuf bindings — declared as a `Build-Depends`, so
Launchpad installs it from the archive itself before building, same as any
other build-dependency. There is nothing to vendor.

**Do verify `python3-grpc-tools` exists in every series you target.** It
exists in noble (`universe`, `1.14.1-6build2` at the time this was written).
Check an older series before assuming:

```bash
rmadison python3-grpc-tools
# or:
curl -s "https://api.launchpad.net/1.0/ubuntu/+archive/primary?ws.op=getPublishedBinaries&binary_name=python3-grpc-tools&exact_match=true" \
  | python3 -m json.tool
```

If it's missing on an older series, that series can't build this package
as-is (the `debian/rules` version-mismatch guard, see `packaging/DEB.md`,
means shipping pre-generated bindings instead is a deliberate design
change, not a quick patch — flag it rather than silently committing
generated `_pb2.py` files against the gitignore's intent).

## Making the PPA available on arm64

`nearshare` is `Architecture: all`, so the amd64 build is the only one
Launchpad needs, but an arm64 machine can only install it if the PPA
publishes for arm64 at all. That is a setting on the PPA, not something in
this repository: open <https://launchpad.net/~dhiva-labs/+archive/ubuntu/apps/+edit>
and tick the ARM ARMv8 (arm64) processor under *Processors*. Until it is
enabled, `apt update` on an arm64 machine skips the PPA with "repository
doesn't support architecture 'arm64'".

Launchpad publishes an `Architecture: all` binary into the architectures
that are enabled when it is published, so a version uploaded before arm64
was enabled may need to be re-published for arm64 (upload a new version, or
copy the existing publication onto itself). Check with
`apt-cache policy nearshare` on an arm64 machine.

CI proves the package itself works on arm64 (see `packaging/DEB.md`); it does
not exercise Launchpad.

## Rehearse the source build before every upload

```bash
dpkg-buildpackage -S -us -uc
```

Confirms `debian/source/options`' tar-ignore rules are still doing their
job (no `.git/`, `.venv/`, `__pycache__/`, or stale `_pb2.py` in the
tarball — check with `tar tJf ../nearshare_<version>.tar.xz | grep -E
'\.venv|__pycache__|_pb2\.py|^\S*\.git'`, which should print nothing) and
that `debian/rules` regenerates the protobuf bindings cleanly.

## Sign and upload

Use whichever GPG key is actually registered against the Launchpad account
that owns `ppa:dhiva-labs/apps` — **do not assume** it's the same key used
for a different Dhiva Labs package without checking:

```bash
curl -s https://api.launchpad.net/1.0/~<launchpad-user>/gpg_keys | grep keyid
```

(BranchPilot's `packaging/PPA.md` documents a case where signing with an
*unregistered* key produced a valid signature, `dput` reported success, and
Launchpad silently discarded the upload — the signing key's email must also
match `debian/changelog`'s Maintainer address, `dhivakar1010@gmail.com`.)

```bash
debuild -S -sa -k<KEYID>
dput dhiva-apps ../nearshare_1.0.2~noble1_source.changes
```

(`dhiva-apps` here is whatever `dput` host alias/section this house's
`~/.dput.cf` already defines for `ppa:dhiva-labs/apps` — reuse it rather
than reconfiguring dput.)

### Two silent failures to guard against

Both report success and upload nothing:

**1. `dput` skips a version it already sent.** After a successful transfer
it writes `nearshare_<version>_source.<host>.upload` next to the package;
a second `dput` of the *same version* is then a silent no-op — this bit a
prior Dhiva Labs upload (see BranchPilot's `packaging/PPA.md`). If Launchpad
rejected an upload (wrong/unregistered key, bad signature) and you re-sign,
force the re-upload rather than bumping the version needlessly:

```bash
rm -f ../nearshare_*_source.dhiva-apps.upload
dput -f dhiva-apps ../nearshare_1.0.2~noble1_source.changes
```

**2. Launchpad discards uploads signed by an unregistered key.** `dput`
still prints "Successfully uploaded packages" — that only confirms the FTP
transfer, not that Launchpad accepted it.

### Confirm it actually landed

```bash
curl -s "https://api.launchpad.net/1.0/~dhiva-labs/+archive/ubuntu/apps?ws.op=getPublishedSources&source_name=nearshare" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["total_size"], "published")'
```

Or watch <https://launchpad.net/~dhiva-labs/+archive/ubuntu/apps/+packages>.

## Rebuilding for another series

1. Confirm `python3-grpc-tools` (and the runtime Depends — `python3-gi`,
   `gir1.2-gtk-4.0`, `gir1.2-adw-1`, `python3-zeroconf`, `python3-protobuf`,
   `python3-cryptography`) all exist in that series.
2. `dch --newversion 1.0.2~<series>1 --distribution <series> "PPA build for <series>."`
3. Rehearse the source build, sign, and upload exactly as above, targeting
   that series' distribution name in both the changelog and the `dput`
   call.

## Version bumps for a new upstream release

Bump `debian/changelog`'s top entry to the new upstream version (e.g.
`1.0.1-1` for the plain `.deb`), then repeat the `~<series>N` dance above
per targeted series for the PPA upload.
