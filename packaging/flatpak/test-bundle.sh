#!/usr/bin/env bash
# Installs the Flatpak bundle on the CI host and runs it there, the way a
# user would get it: the GNOME runtime comes from Flathub.
#
# usage: sudo test-bundle.sh <bundle.flatpak>
#
# The same checks as packaging/linux-distros/smoke.sh, inside the sandbox:
#   1. every ELF file under /app finds all of its libraries (ldd)
#   2. nearshare --help; the GTK4/libadwaita bindings, the app and its
#      protobuf bindings import (with the GNOME runtime's Python)
#   3. the protocol tests against /app/share/nearshare
#   4. tools/gui-smoke.py: the window opens, the receive service listens,
#      the app quits cleanly (Xvfb)
#
# Runs as root: Ubuntu 24.04 restricts unprivileged user namespaces, which
# bubblewrap needs as a normal user.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
[ $# -eq 1 ] || { echo "usage: $0 <bundle.flatpak>" >&2; exit 2; }
bundle="$(readlink -f "$1")"
app=dev.dhivalabs.nearshare
export XDG_RUNTIME_DIR=/run/user/0
mkdir -p -m 0700 "$XDG_RUNTIME_DIR"

flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install -y --noninteractive "$bundle"
flatpak info "$app"

echo "::group::ldd inside the sandbox"
flatpak run --command=bash "$app" -euo pipefail -c '
  mapfile -t elfs < <(find /app -type f | while IFS= read -r f; do
    [ "$(head -c 4 "$f" | od -An -tx1 | tr -d " \n")" = 7f454c46 ] && echo "$f"
  done)
  libdirs="$(for f in "${elfs[@]}"; do case "$f" in *.so|*.so.*) dirname "$f" ;; esac; done | sort -u | paste -sd:)"
  missing=0
  for f in "${elfs[@]}"; do
    if LD_LIBRARY_PATH="$libdirs" ldd "$f" 2>&1 | grep "not found" > /dev/null; then
      echo "missing libraries for $f:"
      LD_LIBRARY_PATH="$libdirs" ldd "$f" | grep "not found"
      missing=1
    fi
  done
  echo "${#elfs[@]} ELF files checked with ldd"
  [ "${#elfs[@]}" -gt 0 ] && [ "$missing" = 0 ]
'
echo "::endgroup::"

# The tests and gui-smoke.py from a directory of their own, so `nearshare`
# resolves to /app/share/nearshare, not to the checkout. Next to the
# checkout rather than in /tmp, which the sandbox replaces with its own.
work="$repo/../nearshare-flatpak-tests"
rm -rf "$work"
mkdir -p "$work"
work="$(cd "$work" && pwd)"
cp -r "$repo/tests" "$work/tests"
cp "$repo/tools/gui-smoke.py" "$work/gui-smoke.py"
run=(flatpak run --filesystem="$work" --env=PYTHONPATH=/app/share/nearshare)

echo "::group::imports"
flatpak run "$app" --help > /dev/null
"${run[@]}" --command=python3 "$app" - <<'PY'
import platform
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
import cryptography, zeroconf
import google.protobuf
import nearshare
from nearshare.proto import offline_wire_formats_pb2, wire_format_pb2  # noqa: F401
from cryptography.hazmat.bindings import _rust
print("machine  :", platform.machine(), "python", platform.python_version())
print("GTK      : %d.%d.%d" % (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()))
print("libadw   : %d.%d.%d" % (Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version()))
print("protobuf :", google.protobuf.__version__, google.protobuf.__file__)
print("zeroconf :", zeroconf.__version__, "cryptography", cryptography.__version__, _rust.__file__)
assert nearshare.__file__.startswith("/app/share/nearshare/"), nearshare.__file__
assert google.protobuf.__file__.startswith("/app/share/nearshare/google/protobuf/"), google.protobuf.__file__
PY
echo "::endgroup::"

for m in test_loopback test_payload_routing test_security test_single_instance; do
  echo "::group::tests.$m"
  "${run[@]}" --command=sh "$app" -c "cd '$work' && exec python3 -m tests.$m"
  echo "::endgroup::"
done

echo "::group::gui-smoke.py"
xvfb-run -a -s '-screen 0 1280x1024x24 -ac' dbus-run-session -- \
  "${run[@]}" --env=GSK_RENDERER=cairo --env=GDK_BACKEND=x11 --command=python3 "$app" "$work/gui-smoke.py"
echo "::endgroup::"
echo "Flatpak bundle OK"
