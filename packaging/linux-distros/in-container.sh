#!/usr/bin/env bash
# Runs inside a distribution container started by verify.sh.
# MODE: rpm (Fedora) | rpm-suse (openSUSE) | pacman (Arch) | appimage (Fedora)
# FILE: the package to install, under /pkgs.
set -euo pipefail
# shellcheck source=/dev/null
. /linux-distros/distro.conf

# Base tools, plus the display and GL stack when there is a GUI smoke test
# (GUI_CMD, or NEEDS_DISPLAY=1 for a smoke test that opens a window itself).
case "$MODE" in
  rpm|appimage)
    tools=(file findutils which procps-ng)
    if [ -n "${GUI_CMD:-}" ] || [ "${NEEDS_DISPLAY:-0}" = 1 ]; then
      tools+=(xorg-x11-server-Xvfb xorg-x11-xauth mesa-dri-drivers dbus-daemon)
    fi
    dnf -y -q install "${tools[@]}"
    ;;
  rpm-suse)
    zypper -n -q --gpg-auto-import-keys refresh
    tools=(file findutils which procps)
    # xvfb-run needs awk, and dbus-run-session is not in dbus-1 there.
    if [ -n "${GUI_CMD:-}" ] || [ "${NEEDS_DISPLAY:-0}" = 1 ]; then
      tools+=(xvfb-run gawk Mesa-dri dbus-1 /usr/bin/dbus-run-session)
    fi
    zypper -n -q install "${tools[@]}"
    ;;
  pacman)
    tools=(file findutils which procps-ng)
    if [ -n "${GUI_CMD:-}" ] || [ "${NEEDS_DISPLAY:-0}" = 1 ]; then
      tools+=(xorg-server-xvfb xorg-xauth mesa dbus)
    fi
    pacman -Syu --noconfirm --needed -q "${tools[@]}"
    ;;
esac

case "$MODE" in
  rpm)      dnf -y install "$FILE"; files() { rpm -ql "$NAME"; } ;;
  rpm-suse) zypper -n install --allow-unsigned-rpm "$FILE"; files() { rpm -ql "$NAME"; } ;;
  pacman)   pacman -U --noconfirm "$FILE"; files() { pacman -Qlq "$NAME"; } ;;
  appimage)
    # The libraries an AppImage expects from the host: GTK 3 and friends,
    # and the EGL/GLES libraries Flutter's engine dlopens (every desktop has
    # them; a minimal container does not).
    dnf -y -q install gtk3 libglvnd-egl libglvnd-gles
    cp "$FILE" /tmp/app.AppImage
    chmod +x /tmp/app.AppImage
    (cd /tmp && /tmp/app.AppImage --appimage-extract > /dev/null)
    files() { find /tmp/squashfs-root -type f; }
    ;;
  *) echo "unknown MODE $MODE" >&2; exit 2 ;;
esac
# What the package manager resolved, for the log.
case "$MODE" in
  rpm|rpm-suse) rpm -qR "$NAME" ;;
  pacman) pacman -Qi "$NAME" ;;
esac

# Every ELF file the package installed must find all of its libraries. The
# package's own library directories go on LD_LIBRARY_PATH, as the app's
# RPATH/$ORIGIN would supply them at run time, so only libraries missing
# from the distribution are reported.
mapfile -t elfs < <(files | while IFS= read -r f; do
  [ -f "$f" ] && [ ! -L "$f" ] || continue
  [ "$(head -c 4 "$f" | od -An -tx1 | tr -d ' \n')" = 7f454c46 ] && echo "$f"
done)
libdirs="$(for f in "${elfs[@]}"; do case "$f" in *.so|*.so.*) dirname "$f" ;; esac; done | sort -u | paste -sd:)"
missing=0
for f in "${elfs[@]}"; do
  # grep to /dev/null, not grep -q: under pipefail an early grep exit can
  # kill ldd with SIGPIPE and turn a match into a false negative.
  if LD_LIBRARY_PATH="$libdirs" ldd "$f" 2>&1 | grep 'not found' > /dev/null; then
    echo "missing libraries for $f:"
    LD_LIBRARY_PATH="$libdirs" ldd "$f" | grep 'not found'
    missing=1
  fi
done
echo "${#elfs[@]} ELF files checked with ldd"
[ "$missing" = 0 ] || exit 1

# Smoke test. SMOKE_CMD must exit 0. GUI apps: GUI_CMD must still be running
# after SMOKE_SECONDS under Xvfb, with no loader errors. Both are shell
# commands naming the installed command; for the AppImage a shim of that
# name on PATH runs the AppImage instead, so arguments pass through.
if [ "$MODE" = appimage ]; then
  export APPIMAGE_EXTRACT_AND_RUN=1
  read -r -a words <<< "$GUI_CMD"
  printf '#!/bin/sh\nexec /tmp/app.AppImage "$@"\n' > "/usr/local/bin/${words[0]}"
  chmod 0755 "/usr/local/bin/${words[0]}"
fi
export HOME=/tmp/smoke-home SMOKE_SECONDS
mkdir -p "$HOME"
if [ -n "${SMOKE_CMD:-}" ]; then
  bash -c "$SMOKE_CMD"
fi
if [ -n "${GUI_CMD:-}" ]; then
  read -r -a gui <<< "$GUI_CMD"
  cat > /tmp/smoke.sh <<'EOS'
log=/tmp/smoke.log
"$@" > "$log" 2>&1 &
pid=$!
secs="${SMOKE_SECONDS:-20}"
for _ in $(seq 1 "$secs"); do
  kill -0 "$pid" 2> /dev/null || break
  sleep 1
done
if kill -0 "$pid" 2> /dev/null; then
  echo "still running after ${secs}s"
  kill "$pid"
  ok=1
else
  wait "$pid" && rc=0 || rc=$?
  echo "exited early with status $rc"
  ok=0
fi
cat "$log"
if grep -E 'error while loading shared libraries|cannot open shared object|undefined symbol' "$log"; then ok=0; fi
[ "$ok" = 1 ]
EOS
  xvfb-run -a -s '-screen 0 1280x800x24' dbus-run-session -- bash /tmp/smoke.sh "${gui[@]}"
fi
echo "$MODE: OK"
