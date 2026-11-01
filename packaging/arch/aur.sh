#!/usr/bin/env bash
# Renders the AUR PKGBUILD for one release and proves it builds.
#
# usage: aur.sh <source.tar.gz> <outdir>
#
#   <source.tar.gz>  the release's source archive, the file the PKGBUILD's
#                    source= line downloads (GitHub's archive of the tag).
#
# Fills PKGBUILD.in with the version and the archive's sha256, then, in an
# archlinux:latest container, generates .SRCINFO, installs the build and
# check dependencies it lists, runs makepkg (build: the protobuf bindings;
# check: the protocol tests; package; the archive is placed next to the
# PKGBUILD, so makepkg verifies the checksum without downloading), runs
# namcap, installs the result and runs the same smoke test as verify.sh,
# against Arch's own python-protobuf. Writes <name>-<version>-aur.tar.gz
# holding PKGBUILD and .SRCINFO, ready to push to the AUR.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
[ $# -eq 2 ] || { echo "usage: $0 <source.tar.gz> <outdir>" >&2; exit 2; }
src="$(readlink -f "$1")"
mkdir -p "$2"; out="$(cd "$2" && pwd)"
# shellcheck source=/dev/null
. "$here/../linux-distros/distro.conf"
: "${VERSION:?set VERSION}"

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
deps=""; for d in "${ARCH_DEPENDS[@]}"; do deps+="'$d' "; done
optdeps=""; for d in "${ARCH_OPTDEPENDS[@]}"; do optdeps+="'$d' "; done
sed -e "s|@NAME@|$NAME|g" -e "s|@VERSION@|$VERSION|g" \
    -e "s|@SUMMARY@|${SUMMARY//\'/}|g" -e "s|@HOMEPAGE@|$HOMEPAGE|g" \
    -e "s|@LICENSE@|$LICENSE|g" -e "s|@ARCH_DEPENDS@|${deps% }|g" \
    -e "s|@ARCH_OPTDEPENDS@|${optdeps% }|g" \
    -e "s|@SHA256@|$(sha256sum "$src" | cut -d' ' -f1)|g" \
    "$here/PKGBUILD.in" > "$work/PKGBUILD"
cp "$src" "$work/$NAME-$VERSION.tar.gz"
if grep -n '@[A-Z0-9_]*@' "$work/PKGBUILD"; then echo "unfilled placeholder" >&2; exit 1; fi
cat "$work/PKGBUILD"

docker run --rm -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" -v "$work:/build" -v "$here/../linux-distros:/linux-distros:ro" \
  -v "$repo:/src:ro" -e SMOKE_CMD="$SMOKE_CMD" -e EXPECT_PROTOBUF=/usr/lib/python3 \
  archlinux:latest bash -euo pipefail -c '
  # makepkg runs as "builder", so /build is handed to it; give the files back
  # to the runner user afterwards, or this script cannot read or delete them.
  trap '"'"'chown -R "$HOST_UID:$HOST_GID" /build'"'"' EXIT
  pacman -Syu --noconfirm --needed -q base-devel namcap xorg-server-xvfb xorg-xauth mesa dbus
  useradd -m builder
  chown -R builder /build
  cd /build
  su builder -c "makepkg --printsrcinfo > .SRCINFO"
  mapfile -t deps < <(sed -nE "s/^\t(make|check)?depends = //p" .SRCINFO)
  pacman -S --noconfirm --needed -q "${deps[@]}"
  su builder -c "makepkg --noconfirm -f"
  namcap PKGBUILD ./*-any.pkg.tar.zst
  pacman -U --noconfirm ./*-any.pkg.tar.zst
  bash -c "$SMOKE_CMD"
  cat .SRCINFO
'
tar -C "$work" -czf "$out/$NAME-$VERSION-aur.tar.gz" PKGBUILD .SRCINFO
ls -l "$out/$NAME-$VERSION-aur.tar.gz"
