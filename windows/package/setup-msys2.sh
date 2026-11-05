#!/usr/bin/env bash
# Install what NearShare needs to run, test and be bundled on Windows,
# into an MSYS2 install. Run from an MSYS2 UCRT64 shell. Idempotent.
set -euo pipefail
pacman -S --needed --noconfirm --noprogressbar \
  mingw-w64-ucrt-x86_64-python \
  mingw-w64-ucrt-x86_64-python-gobject \
  mingw-w64-ucrt-x86_64-gtk4 \
  mingw-w64-ucrt-x86_64-libadwaita \
  mingw-w64-ucrt-x86_64-adwaita-icon-theme \
  mingw-w64-ucrt-x86_64-librsvg \
  mingw-w64-ucrt-x86_64-python-cryptography \
  mingw-w64-ucrt-x86_64-python-protobuf \
  mingw-w64-ucrt-x86_64-python-zeroconf \
  mingw-w64-ucrt-x86_64-python-ifaddr \
  mingw-w64-ucrt-x86_64-gcc \
  pacman-contrib
