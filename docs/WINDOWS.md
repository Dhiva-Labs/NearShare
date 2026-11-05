# NearShare on Windows

The same app as on Linux, built from the same source: the GTK 4 /
libadwaita window, the `nearshare` command line, Quick Share with
Android phones, other PCs and Macs (NearDrop), and AirDrop once an ESP32
AWDL radio is available (see [AIRDROP.md](AIRDROP.md#windows)).

## What differs from Linux

| | Linux | Windows |
|---|---|---|
| Runtime | system Python + distro GTK | MSYS2 UCRT64 Python, GTK 4, libadwaita, bundled with the app |
| Bluetooth LE | BlueZ over D-Bus (`core/ble.py`) | WinRT publisher/watcher in `nearshare-ble.exe`, driven by `core/ble_windows.py` |
| CLI ↔ app | Unix socket, mode 0600 | loopback TCP port plus a per-run token in `%LOCALAPPDATA%\NearShare\control.json` |
| Settings | `~/.config/nearshare` | `%APPDATA%\NearShare` |
| AirDrop identity | `~/.local/share/nearshare/airdrop` | `%LOCALAPPDATA%\NearShare\airdrop` |
| File manager | Nautilus right-click | Explorer **Send to ▸ NearShare** |
| Direct mode (hotspot) | NetworkManager | not available |
| AirDrop radio | omdrop-owl / omdrop-awdl via pkexec | omdrop-owl-windows (ESP32) via its service |

Windows' Bluetooth LE publisher always sends non-connectable adverts at
a fixed interval, so on Windows the AirDrop wake advert's connectable
flag and 100 ms interval are ignored. Windows does accept Apple's
manufacturer data, and the advert has been received over the air.

## Smart App Control

Windows 11's Smart App Control blocks unsigned programs and libraries
that Microsoft has no reputation for. The GTK and Python runtime
NearShare bundles is unsigned, so **with Smart App Control on, the
bundle below won't start.** A Microsoft Store (MSIX) build, which the
Store signs, is the way to reach those PCs. Developers testing the
bundle need Smart App Control off.

## Building

On a Windows PC with [MSYS2](https://www.msys2.org/) in `C:\msys64` and
Visual Studio 2022 Build Tools (C++ workload and Windows SDK):

```bat
windows\ble\build.cmd
```

Then, from an MSYS2 **UCRT64** shell in the repository:

```bash
windows/package/setup-msys2.sh     # GTK 4, libadwaita, PyGObject, Python libraries, gcc
python3 -m tests.test_loopback     # the usual test suites run here too
windows/package/build-bundle.sh    # dist/NearShare/ and dist/NearShare-<version>-windows-x64.zip
```

To run from the source tree without bundling:

```bash
python3 -m nearshare               # the window
python3 -m nearshare.cli status    # the command line
```

## Installing the bundle

Unzip it anywhere and run, from PowerShell in that folder:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

It copies NearShare to `%LOCALAPPDATA%\Programs\NearShare` and adds a
Start menu entry and **Send to ▸ NearShare** in Explorer. From an
elevated PowerShell it also allows NearShare through Windows Firewall
on private networks. Otherwise Windows asks the first time NearShare
listens for transfers. `install.ps1 -Uninstall` removes all of it.

The command line is `nearshare.cmd` in the install folder:

```bat
"%LOCALAPPDATA%\Programs\NearShare\nearshare.cmd" status
```
