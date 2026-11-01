#!/usr/bin/env bash
# Smoke test for an installed NearShare package, run in the Fedora, openSUSE
# and Arch containers by verify.sh and after the AUR build by aur.sh, with
# the repository mounted at /src. The same checks as CI's deb job, against
# this distribution's Python, PyGObject, GTK4, libadwaita, zeroconf and
# cryptography:
#
#   1. nearshare --help
#   2. the GTK4/libadwaita bindings, the installed app and its protobuf
#      bindings import
#   3. the protocol tests (loopback transfer, payload routing, security,
#      single instance) against the installed copy
#   4. tools/gui-smoke.py: the GTK app starts under Xvfb, opens its window,
#      starts the receive service and quits cleanly
#
# EXPECT_PROTOBUF: where google.protobuf must come from (the copy bundled in
# /usr/share/nearshare; the AUR package uses the distribution's instead).
set -euo pipefail
export EXPECT_PROTOBUF="${EXPECT_PROTOBUF:-/usr/share/nearshare/google/protobuf/}"

# readlink: on Fedora and Arch /usr/sbin is a symlink to /usr/bin and comes
# first on root's PATH.
test "$(readlink -f "$(command -v nearshare)")" = /usr/bin/nearshare
nearshare --help > /dev/null

work="$(mktemp -d)"
cp -r /src/tests "$work/tests"
cp /src/tools/gui-smoke.py "$work/gui-smoke.py"
cd "$work"
export PYTHONPATH=/usr/share/nearshare

python3 - <<'PY'
import os, platform
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
import cryptography, zeroconf
import google.protobuf
from google.protobuf.internal import api_implementation
import nearshare
from nearshare.proto import offline_wire_formats_pb2, wire_format_pb2  # noqa: F401
print("machine  :", platform.machine(), "python", platform.python_version())
print("package  :", nearshare.__file__)
print("GTK      : %d.%d.%d" % (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()))
print("libadw   : %d.%d.%d" % (Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version()))
print("protobuf :", google.protobuf.__version__, api_implementation.Type(), google.protobuf.__file__)
print("zeroconf :", zeroconf.__version__, "cryptography", cryptography.__version__)
assert nearshare.__file__.startswith("/usr/share/nearshare/"), nearshare.__file__
assert google.protobuf.__file__.startswith(os.environ["EXPECT_PROTOBUF"]), google.protobuf.__file__
PY

# The service binds its control socket in $XDG_RUNTIME_DIR (/run/user/<uid>
# by default), which only a login session has; the repo's test job does the same.
XDG_RUNTIME_DIR="$(mktemp -d)"
export XDG_RUNTIME_DIR
for m in test_loopback test_payload_routing test_security test_single_instance; do
  echo "== tests.$m"
  python3 -m "tests.$m"
done

echo "== gui-smoke.py"
XDG_RUNTIME_DIR="$(mktemp -d)"
export XDG_RUNTIME_DIR GSK_RENDERER=cairo GDK_BACKEND=x11
xvfb-run -a -s '-screen 0 1280x1024x24 -ac' dbus-run-session -- python3 gui-smoke.py
echo "nearshare smoke test passed"
