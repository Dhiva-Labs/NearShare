"""The UI's display-free logic: formatting, saved settings, start at
login, and that every file carrying the version agrees.

Run:  .venv/bin/python -m tests.test_ui_logic
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

import nearshare
from nearshare import autostart, cli
from nearshare.ui import fmt, prefs

ROOT = Path(__file__).resolve().parent.parent


def test_fmt(tmp: Path) -> None:
    assert fmt.human_size(0) == "0 B"
    assert fmt.human_size(1536) == "1.5 KB"
    assert fmt.human_size(5 * 1024 ** 3) == "5.0 GB"
    assert fmt.human_speed(2 * 1024 ** 2) == "2.0 MB/s"
    assert fmt.human_duration(59) == "59s"
    assert fmt.human_duration(125) == "2m 05s"
    assert fmt.human_duration(3700) == "1h 01m"
    assert fmt.count_files(1) == "1 file" and fmt.count_files(3) == "3 files"

    a, b, c = Path("a.pdf"), Path("b.jpg"), Path("c.mp4")
    assert fmt.describe_files([]) == "nothing"
    assert fmt.describe_files([a]) == "a.pdf"
    assert fmt.describe_files([a, b]) == "a.pdf and b.jpg"
    assert fmt.describe_files([a, b, c]) == "a.pdf and 2 more"

    real = tmp / "real.txt"
    real.write_bytes(b"x" * 10)
    folder = tmp / "folder"
    folder.mkdir()
    files, rejected = fmt.split_sendable([real, folder, tmp / "missing"])
    assert files == [real] and rejected == [folder, tmp / "missing"]
    assert fmt.total_size([real, tmp / "missing"]) == 10
    assert fmt.device_icon(1) == "phone-symbolic"
    assert fmt.device_icon(99) == fmt.device_icon(0)
    print("fmt: OK")


def test_prefs(tmp: Path) -> None:
    with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp / "cfg")}):
        assert prefs.device_name() is None
        assert prefs.download_dir() is None
        assert not prefs.has_seen_background_explainer()
        assert not prefs.setup_dismissed()

        prefs.set_device_name("  Study laptop ")
        prefs.mark_background_explainer_seen()
        prefs.set_setup_dismissed(True)
        assert prefs.device_name() == "Study laptop"
        assert prefs.has_seen_background_explainer(), "one setting must not erase another"
        assert prefs.setup_dismissed()

        prefs.set_device_name("   ")
        assert prefs.device_name() is None
        prefs.set_setup_dismissed(False)
        assert not prefs.setup_dismissed()

        target = tmp / "inbox"
        target.mkdir()
        prefs.set_download_dir(target)
        assert prefs.download_dir() == target
        target.rmdir()
        assert prefs.download_dir() is None, "a vanished folder falls back to the default"

        # A corrupt file resets to defaults rather than crashing the app.
        state = tmp / "cfg" / "nearshare" / "ui-state.json"
        state.write_text("{not json")
        assert prefs.device_name() is None
        state.write_text("[1, 2]")
        assert prefs.device_name() is None
        prefs.set_device_name("ok")
        assert prefs.device_name() == "ok"
        assert not state.with_name(state.name + ".tmp").exists()
    print("prefs: OK")


def test_autostart(tmp: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp / "xdg")}
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(autostart, "_in_snap", return_value=False), \
            mock.patch.object(autostart, "_in_flatpak", return_value=False):
        path = autostart.entry_path()
        assert path == tmp / "xdg" / "autostart" / "dev.dhivalabs.nearshare.desktop"
        assert autostart.supported() and not autostart.enabled()
        autostart.set_enabled(True)
        text = path.read_text()
        assert autostart.enabled()
        assert re.search(r'^Exec=".+nearshare" gui --background$', text, re.M), text
        autostart.set_enabled(False)
        autostart.set_enabled(False)  # removing twice is fine
        assert not autostart.enabled()

    snap_env = {"SNAP": "/snap/nearshare/1", "SNAP_USER_DATA": str(tmp / "snapdata")}
    with mock.patch.dict(os.environ, snap_env), \
            mock.patch.object(autostart, "_in_flatpak", return_value=False):
        path = autostart.entry_path()
        # The name must match `autostart:` in snap/snapcraft.yaml, or
        # snapd never runs it.
        assert path == (tmp / "snapdata" / ".config" / "autostart" /
                        "nearshare-autostart.desktop")
        snapcraft = (ROOT / "snap" / "snapcraft.yaml").read_text()
        assert f"autostart: {path.name}" in snapcraft
        autostart.set_enabled(True)
        assert "Exec=nearshare.gui --background\n" in path.read_text()

    with mock.patch.object(autostart, "_in_flatpak", return_value=True):
        assert not autostart.supported()
    print("autostart: OK")


def test_cli_flags() -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
        cli.main(["--version"])
    assert out.getvalue().strip() == f"NearShare {nearshare.__version__}"

    with mock.patch.object(os, "execv") as execv:
        cli.main(["gui"])
        assert execv.call_args[0][1][1:] == ["-m", "nearshare"]
        cli.main(["gui", "--background"])
        assert execv.call_args[0][1][1:] == ["-m", "nearshare", "--background"]

    assert cli.snap_interface_connected("bluez") is None or os.environ.get("SNAP")
    with mock.patch.dict(os.environ, {"SNAP": "/snap/nearshare/1"}):
        for code, expected in ((0, True), (1, False), (2, None)):
            with mock.patch.object(cli.subprocess, "run",
                                   return_value=mock.Mock(returncode=code)):
                assert cli.snap_interface_connected("bluez") is expected
        with mock.patch.object(cli.subprocess, "run", side_effect=OSError):
            assert cli.snap_interface_connected("bluez") is None
    print("cli flags: OK")


def test_versions_agree() -> None:
    """One release, one number. A tag is refused by CI if these differ,
    so catch it here first."""
    version = nearshare.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version

    snapcraft = (ROOT / "snap" / "snapcraft.yaml").read_text()
    assert re.search(rf"^version: '{re.escape(version)}'$", snapcraft, re.M), \
        "snap/snapcraft.yaml version differs"

    first = (ROOT / "debian" / "changelog").read_text().splitlines()[0]
    match = re.match(r"nearshare \(([^)~]+)~", first)
    assert match and match.group(1) == version, \
        f"debian/changelog top entry is {first!r}"

    metainfo = (ROOT / "packaging" / "flatpak" /
                "dev.dhivalabs.nearshare.metainfo.xml").read_text()
    newest = re.search(r'<release version="([^"]+)"', metainfo)
    assert newest and newest.group(1) == version, "metainfo newest release differs"

    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert re.search(rf"^## {re.escape(version)} - \d{{4}}-\d\d-\d\d$",
                     changelog, re.M), "CHANGELOG.md has no dated entry"
    print(f"versions agree: {version}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ns-ui-"))
    test_fmt(tmp)
    test_prefs(tmp)
    test_autostart(tmp)
    test_cli_flags()
    test_versions_agree()
    print("\nUI LOGIC TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
