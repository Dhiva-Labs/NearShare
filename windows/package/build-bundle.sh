#!/usr/bin/env bash
# Assemble a self-contained NearShare for Windows from an MSYS2 install.
#
# Run from an MSYS2 UCRT64 shell (C:\msys64\ucrt64.exe), after
# windows/package/setup-msys2.sh and windows/ble/build.cmd:
#
#   windows/package/build-bundle.sh            -> dist/NearShare/, dist/NearShare-<ver>-windows-x64.zip
#
# The bundle is MSYS2's own Python, GTK 4 and libadwaita (only the files
# of the packages NearShare needs, and of what they depend on), the
# NearShare package, the BLE helper and a launcher. Everything in it
# finds everything else relative to its own location, so the folder can
# live anywhere; windows/package/install.ps1 copies it into place.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
DIST=$REPO/dist
OUT=$DIST/NearShare
VERSION=${VERSION:-$(git -C "$REPO" describe --tags --always 2>/dev/null || echo dev)}

[ "${MSYSTEM:-}" = UCRT64 ] || { echo "run this from an MSYS2 UCRT64 shell" >&2; exit 1; }
BLE_EXE=$REPO/windows/ble/nearshare-ble.exe
[ -f "$BLE_EXE" ] || { echo "build $BLE_EXE first (windows/ble/build.cmd)" >&2; exit 1; }

# What NearShare imports, and the icons GTK draws with. Their dependency
# closure is computed below.
ROOTS=(
  mingw-w64-ucrt-x86_64-python
  mingw-w64-ucrt-x86_64-python-gobject
  mingw-w64-ucrt-x86_64-gtk4
  mingw-w64-ucrt-x86_64-libadwaita
  mingw-w64-ucrt-x86_64-python-cryptography
  mingw-w64-ucrt-x86_64-python-protobuf
  mingw-w64-ucrt-x86_64-python-zeroconf
  mingw-w64-ucrt-x86_64-python-ifaddr
  mingw-w64-ucrt-x86_64-adwaita-icon-theme
  mingw-w64-ucrt-x86_64-librsvg
)
# Pulled in by the packages above but never used at run time.
SKIP='^mingw-w64-ucrt-x86_64-(tcl|tk|gcc|binutils|crt|headers|winpthreads-git|pkgconf|python-pip|python-setuptools|vulkan-loader|gtk-update-icon-cache-ignored)$'

echo "== resolving packages"
mapfile -t PKGS < <(for p in "${ROOTS[@]}"; do pactree -lu "$p"; done \
                    | sort -u | grep -Ev "$SKIP" | grep '^mingw-w64-ucrt-x86_64-')
echo "   ${#PKGS[@]} packages"

rm -rf "$OUT"
mkdir -p "$OUT"
echo "== copying runtime files"
pacman -Qlq "${PKGS[@]}" \
  | grep '^/ucrt64/' \
  | grep -Ev '/$|/include/|/share/(doc|man|info|gtk-doc|aclocal|bash-completion|zsh|fish|vala|gir-1\.0|gettext|thumbnailers)/|\.a$|/lib/pkgconfig/|/lib/cmake/|/share/pkgconfig/|/lib/python3\.[0-9]+/(test|idlelib|tkinter|turtledemo|ensurepip|lib2to3)/|__pycache__|\.pyc$' \
  | while IFS= read -r f; do
      [ -f "$f" ] || continue
      dest=$OUT/${f#/ucrt64/}
      mkdir -p "${dest%/*}"
      cp -p "$f" "$dest"
    done

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
SITE=$OUT/lib/python$PYVER/site-packages

echo "== NearShare $VERSION"
mkdir -p "$SITE"
tar -C "$REPO" --exclude=__pycache__ -cf - nearshare | tar -C "$SITE" -xf -
cp "$BLE_EXE" "$OUT/bin/"

echo "== launcher"
windres "$REPO/windows/launcher/launcher.rc" -O coff -o "$DIST/launcher.res" \
  --include-dir "$REPO/windows/launcher"
gcc -O2 -municode -mwindows -o "$OUT/NearShare.exe" \
  "$REPO/windows/launcher/launcher.c" "$DIST/launcher.res" -lshell32
rm -f "$DIST/launcher.res"
cp "$REPO/windows/launcher/nearshare.ico" "$OUT/"
# The command line: the same `nearshare` CLI as on Linux.
printf '@"%%~dp0bin\\python.exe" -m nearshare.cli %%*\r\n' > "$OUT/nearshare.cmd"
cp "$REPO/windows/package/install.ps1" "$OUT/"
cp "$REPO/LICENSE" "$OUT/LICENSE.txt"

echo "== caches"
glib-compile-schemas "$OUT/share/glib-2.0/schemas"
# gdk-pixbuf resolves loader paths relative to its own DLL on Windows.
loaders=$(ls -d "$OUT"/lib/gdk-pixbuf-2.0/*/ | head -1)
( cd "$OUT/bin" && GDK_PIXBUF_MODULEDIR="$loaders/loaders" \
    ./gdk-pixbuf-query-loaders.exe > "$loaders/loaders.cache" ) || true
sed -i "s|$(cygpath -m "$OUT")/||; s|$OUT/||" "$loaders/loaders.cache" || true
for theme in "$OUT"/share/icons/*/; do
  [ -f "$theme/index.theme" ] && gtk4-update-icon-cache -q -f -t "$theme" || true
done
"$OUT/bin/python.exe" -m compileall -q "$OUT/lib/python$PYVER" >/dev/null || true

echo "== smoke test"
"$OUT/bin/python.exe" - <<'PY'
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: F401
import cryptography, google.protobuf, zeroconf, ifaddr  # noqa: F401
import nearshare.core.service, nearshare.airdrop.manager  # noqa: F401
print("imports OK")
PY

echo "== zip"
ZIP=$DIST/NearShare-$VERSION-windows-x64.zip
rm -f "$ZIP"
( cd "$DIST" && powershell -NoProfile -Command \
    "Compress-Archive -Path NearShare -DestinationPath '$(cygpath -w "$ZIP")'" )
du -sh "$OUT" "$ZIP"
