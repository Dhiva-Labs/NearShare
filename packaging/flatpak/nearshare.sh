#!/bin/sh
# NearShare launcher inside the Flatpak: the debian/nearshare.wrapper of the
# .deb with /app paths. The app tree (with its own protobuf runtime) is
# /app/share/nearshare; Python, PyGObject, GTK4 and libadwaita come from the
# GNOME runtime, zeroconf and cryptography from /app. With no arguments,
# which is what `flatpak run` passes, it opens the window.
export PYTHONPATH="/app/share/nearshare${PYTHONPATH:+:$PYTHONPATH}"
[ "$#" -gt 0 ] || set -- gui
exec python3 -m nearshare.cli "$@"
