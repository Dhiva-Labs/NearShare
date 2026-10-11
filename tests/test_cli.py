"""Tests for nearshare.cli against a live, in-process NearShareService.

Monkeypatches XDG_RUNTIME_DIR to an isolated temp directory so the
control socket doesn't collide with a real running app, starts a
NearShareService, then calls the cli command functions directly
(building argparse.Namespace objects by hand rather than shelling out)
and asserts on their return codes and printed output.

Run:  .venv/bin/python -m tests.test_cli
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from nearshare import cli
from nearshare.core.connection import Events
from nearshare.core.paths import IS_WINDOWS
from nearshare.core.service import NearShareService, control_socket_path


def _ns(**kwargs) -> argparse.Namespace:
    kwargs.setdefault("json", False)
    return argparse.Namespace(**kwargs)


async def _run(func, *args):
    """cli.cmd_* functions use blocking sockets; run them off-thread so
    the service's asyncio event loop (in this same process, for the
    test) keeps servicing the control socket concurrently instead of
    deadlocking against itself."""
    return await asyncio.to_thread(func, *args)


@contextlib.contextmanager
def _captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qs-cli-test-"))
    runtime_dir = tmp / "run"
    runtime_dir.mkdir()
    os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
    if not IS_WINDOWS:
        assert control_socket_path() == runtime_dir / "nearshare.sock"

    service = NearShareService(device_name="cli-test-device",
                                download_dir=tmp / "downloads",
                                events=Events())
    await service.start(visible=False)
    try:
        # --- status: hidden ------------------------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_status, _ns())
        assert rc == 0, err.getvalue()
        text = out.getvalue()
        assert "hidden" in text, text
        assert "cli-test-device" in text, text
        print("status (hidden): OK")

        # --- status --json ---------------------------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_status, _ns(json=True))
        assert rc == 0
        assert '"visible": false' in out.getvalue(), out.getvalue()
        print("status --json: OK")

        # --- on -----------------------------------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_on, _ns())
        assert rc == 0, err.getvalue()
        assert "Visible to nearby devices" in out.getvalue()
        assert service.visible is True
        print("on: OK")

        # --- status: visible now ----------------------------------------
        with _captured() as (out, err):
            await _run(cli.cmd_status, _ns())
        assert "NearShare is visible" in out.getvalue()
        print("status (visible): OK")

        # --- off ----------------------------------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_off, _ns())
        assert rc == 0
        assert "Hidden from nearby devices" in out.getvalue()
        assert service.visible is False
        print("off: OK")

        # --- toggle (hidden -> visible) ----------------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_toggle, _ns())
        assert rc == 0
        assert "Visible to nearby devices" in out.getvalue()
        assert service.visible is True
        print("toggle (-> visible): OK")

        # --- toggle again (visible -> hidden) ------------------------
        with _captured() as (out, err):
            rc = await _run(cli.cmd_toggle, _ns())
        assert rc == 0
        assert "Hidden from nearby devices" in out.getvalue()
        assert service.visible is False
        print("toggle (-> hidden): OK")

        # --- peers: the browser is live, so real LAN devices may appear;
        # assert the command works and both formats agree, not emptiness.
        import json as _json
        with _captured() as (out, err):
            rc = await _run(cli.cmd_peers, _ns(json=True))
        assert rc == 0
        listed = _json.loads(out.getvalue())["peers"]
        with _captured() as (out, err):
            rc = await _run(cli.cmd_peers, _ns())
        assert rc == 0
        if listed:
            for peer in listed:
                assert peer["name"] in out.getvalue()
        else:
            assert "No nearby devices found" in out.getvalue()
        print(f"peers (live browser, {len(listed)} seen): OK")

    finally:
        await service.stop()

    # --- not-running behaviour: no service listening on this socket -----
    with _captured() as (out, err):
        rc = cli.cmd_status(_ns())
    assert rc == 1
    assert cli.NOT_RUNNING_MSG in err.getvalue()
    print("status (not running): OK")

    with _captured() as (out, err):
        rc = cli.cmd_off(_ns())
    assert rc == 1
    assert cli.NOT_RUNNING_MSG in err.getvalue()
    print("off (not running): OK")

    with _captured() as (out, err):
        rc = cli.cmd_peers(_ns())
    assert rc == 1
    assert cli.NOT_RUNNING_MSG in err.getvalue()
    print("peers (not running): OK")

    # on/toggle fall back to launching the GUI instead of failing. We
    # can't actually exercise `python -m nearshare` (no __main__ yet,
    # owned by another agent), so just check the fallback is taken
    # (exit 0, message printed) rather than a hard failure, and that it
    # tried to launch something rather than silently no-op'ing.
    launched: list[list[str]] = []
    real_popen = cli.subprocess.Popen

    def fake_popen(args, **kwargs):
        launched.append(args)

        class _FakeProc:
            pass
        return _FakeProc()

    cli.subprocess.Popen = fake_popen
    try:
        with _captured() as (out, err):
            rc = cli.cmd_on(_ns())
        assert rc == 0, err.getvalue()
        assert "starting it now" in out.getvalue()
        assert launched and launched[0][1:] == ["-m", "nearshare"]
        print("on (not running, launches GUI): OK")

        launched.clear()
        with _captured() as (out, err):
            rc = cli.cmd_toggle(_ns())
        assert rc == 0
        assert launched
        print("toggle (not running, launches GUI): OK")
    finally:
        cli.subprocess.Popen = real_popen

    # --- argparse wiring smoke test ----------------------------------
    parser = cli.build_parser()
    args = parser.parse_args(["status", "--json"])
    assert args.func is cli.cmd_status and args.json is True
    args = parser.parse_args(["send", "a.jpg", "b.pdf", "--to", "Phone"])
    assert args.func is cli.cmd_send
    assert args.files == ["a.jpg", "b.pdf"] and args.to == "Phone"
    args = parser.parse_args(["send-picker", "a.jpg"])
    assert args.func is cli.cmd_send_picker and args.files == ["a.jpg"]
    args = parser.parse_args(["install"])
    assert args.func is cli.cmd_install and args.shortcut is True
    assert args.key is None
    args = parser.parse_args(["install", "--no-shortcut"])
    assert args.shortcut is False
    args = parser.parse_args(["install", "--key", "<Control><Alt>s"])
    assert args.key == "<Control><Alt>s"
    args = parser.parse_args(["uninstall"])
    assert args.func is cli.cmd_uninstall
    print("argparse wiring: OK")

    test_install_uninstall()
    test_snap_install()
    test_shortcut_bind_unbind()
    test_accelerator_helpers()

    print("\nCLI TEST PASSED")
    return 0


# ----------------------------------------------------- fake gsettings CLI

class _FakeGsettings:
    """In-memory stand-in for the real gsettings CLI (monkeypatched onto
    cli._run_gsettings), so shortcut bind/unbind tests never touch the
    developer's actual GNOME/dconf settings. Backs just enough of
    `gsettings get/set/reset` for cli.py's keybinding helpers, keyed the
    same way the real gsettings CLI is: schema[:path] + key.

    available=False simulates the media-keys schema being invisible to
    the process (e.g. running under strict snap confinement, see
    cli.py's module comment on _gsettings_available) -- list-schemas
    then simply omits it, same as the real thing under snap."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.store: dict[tuple[str, str], str] = {
            (cli._MEDIA_KEYS_SCHEMA, "custom-keybindings"): "@as []",
        }
        self.calls: list[list[str]] = []

    @staticmethod
    def _split(spec: str) -> tuple[str, str]:
        schema, _, path = spec.partition(":")
        return schema, path

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(args)
        cmd = args[0]
        if cmd == "list-schemas":
            schemas = [cli._MEDIA_KEYS_SCHEMA] if self.available else []
            return subprocess.CompletedProcess(
                args, 0, "\n".join(schemas) + "\n", "")
        schema, path = self._split(args[1])
        key = args[2]
        if cmd == "get":
            value = self.store.get((path or schema, key))
            if value is None:
                return subprocess.CompletedProcess(args, 1, "", "No such key")
            return subprocess.CompletedProcess(args, 0, value + "\n", "")
        if cmd == "set":
            self.store[(path or schema, key)] = args[3]
            return subprocess.CompletedProcess(args, 0, "", "")
        if cmd == "reset":
            self.store.pop((path or schema, key), None)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", f"unhandled: {args}")


@contextlib.contextmanager
def _fake_gsettings(available: bool = True):
    fake = _FakeGsettings(available=available)
    real = cli._run_gsettings
    cli._run_gsettings = fake
    try:
        yield fake
    finally:
        cli._run_gsettings = real


# ------------------------------------------------------- install/uninstall

def test_install_uninstall() -> None:
    """`nearshare install`/`uninstall` against a fake $HOME, so the real
    developer machine's ~/.local/{bin,share} is never touched. HOME and
    XDG_DATA_HOME are monkeypatched just for this block and restored
    afterwards (unlike XDG_RUNTIME_DIR above, HOME affects Path.home()
    calls anywhere in the process, so leaving it patched could leak into
    other tests run later in the same process)."""
    orig_home = os.environ.get("HOME")
    orig_xdg_data = os.environ.get("XDG_DATA_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="qs-install-test-"))
    fake_home = tmp / "home"
    fake_home.mkdir()
    os.environ["HOME"] = str(fake_home)
    os.environ["XDG_DATA_HOME"] = str(fake_home / ".local" / "share")

    try:
        bin_target = fake_home / ".local" / "bin" / "nearshare"
        apps_dir = fake_home / ".local" / "share" / "applications"
        nautilus_script = (fake_home / ".local" / "share" / "nautilus" /
                           "scripts" / "Send with NearShare")

        # gsettings is faked for this whole test (not just the dedicated
        # shortcut tests below) so `cmd_install`/`cmd_uninstall`'s default
        # shortcut=True path is exercised too, without ever touching the
        # real developer machine's GNOME settings.
        with _fake_gsettings() as fake:
            # --- install -------------------------------------------------
            with _captured() as (out, err):
                rc = cli.cmd_install(_ns())
            assert rc == 0, err.getvalue()

            assert bin_target.is_symlink(), "launcher symlink not created"
            assert bin_target.resolve() == cli.BIN_SCRIPT.resolve()
            print("install: symlink OK")

            for name in cli._desktop_file_names():
                dest = apps_dir / name
                assert dest.is_file(), f"{dest} missing"
                content = dest.read_text()
                assert f"Exec={bin_target}" in content, content
            print("install: desktop files (Exec rewritten) OK")

            assert nautilus_script.is_file(), "nautilus script missing"
            mode = nautilus_script.stat().st_mode
            assert mode & 0o111, "nautilus script is not executable"
            assert str(bin_target) in nautilus_script.read_text()
            print("install: nautilus script (executable) OK")

            assert cli._KEYBINDING_PATH in fake.store[
                (cli._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
            assert fake.store[(cli._KEYBINDING_PATH, "command")] == \
                f"{bin_target} toggle"
            print("install: keyboard shortcut bound OK")

            # --- install again: idempotent ----------------------------------
            with _captured() as (out, err):
                rc = cli.cmd_install(_ns())
            assert rc == 0, err.getvalue()
            assert "already in place" in out.getvalue()
            assert "already up to date" in out.getvalue()
            assert "already bound" in out.getvalue()
            assert bin_target.is_symlink()
            print("install (re-run): idempotent OK")

            # --- uninstall -----------------------------------------------
            with _captured() as (out, err):
                rc = cli.cmd_uninstall(_ns())
            assert rc == 0, err.getvalue()
            assert not bin_target.exists() and not bin_target.is_symlink()
            for name in cli._desktop_file_names():
                assert not (apps_dir / name).exists()
            assert not nautilus_script.exists()
            assert cli._KEYBINDING_PATH not in fake.store[
                (cli._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
            print("uninstall: clean removal (incl. shortcut) OK")

            # --- uninstall again: idempotent, no errors -------------------
            with _captured() as (out, err):
                rc = cli.cmd_uninstall(_ns())
            assert rc == 0, err.getvalue()
            assert "already removed" in out.getvalue()
            assert "already unbound" in out.getvalue()
            print("uninstall (re-run): idempotent OK")
    finally:
        if orig_home is not None:
            os.environ["HOME"] = orig_home
        else:
            os.environ.pop("HOME", None)
        if orig_xdg_data is not None:
            os.environ["XDG_DATA_HOME"] = orig_xdg_data
        else:
            os.environ.pop("XDG_DATA_HOME", None)


# -------------------------------------------------------------- snap path

def test_snap_install() -> None:
    """Inside the snap ($SNAP set), `nearshare install`/`uninstall` must
    never crash and must never write anything under $HOME -- strict
    confinement's `home` interface would deny it (a real PermissionError
    on a real snap), so the snap branch is expected to skip those steps
    entirely rather than attempt and catch. gsettings is faked here too:
    this test is about the snap detection/skip logic, not the shortcut
    logic (covered separately below)."""
    orig_home = os.environ.get("HOME")
    orig_xdg_data = os.environ.get("XDG_DATA_HOME")
    orig_snap = os.environ.get("SNAP")
    tmp = Path(tempfile.mkdtemp(prefix="qs-snap-test-"))
    fake_home = tmp / "home"
    fake_home.mkdir()
    os.environ["HOME"] = str(fake_home)
    os.environ["XDG_DATA_HOME"] = str(fake_home / ".local" / "share")
    os.environ["SNAP"] = "/snap/nearshare/x1"

    try:
        assert cli._in_snap() is True

        with _fake_gsettings() as fake:
            with _captured() as (out, err):
                rc = cli.cmd_install(_ns())
            assert rc == 0, err.getvalue()
            assert "skipped" in out.getvalue()
            print("snap install: no exception, rc 0 OK")

            assert not (fake_home / ".local").exists(), (
                "snap install wrote under $HOME despite strict confinement "
                "never granting that access")
            print("snap install: nothing under fake $HOME written OK")

            # The one thing that IS possible under snap (per this task's
            # verified fact that gsettings works there) should still have
            # happened, using the faked schema.
            assert cli._KEYBINDING_PATH in fake.store[
                (cli._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
            print("snap install: keyboard shortcut still bound OK")

            with _captured() as (out, err):
                rc = cli.cmd_uninstall(_ns())
            assert rc == 0, err.getvalue()
            assert not (fake_home / ".local").exists()
            assert cli._KEYBINDING_PATH not in fake.store[
                (cli._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
            print("snap uninstall: no exception, shortcut removed OK")

        # integration_status() must also survive/report sanely under snap.
        with _fake_gsettings():
            status = cli.integration_status()
            assert status["snap"] is True
        print("snap: integration_status() OK")
    finally:
        if orig_home is not None:
            os.environ["HOME"] = orig_home
        else:
            os.environ.pop("HOME", None)
        if orig_xdg_data is not None:
            os.environ["XDG_DATA_HOME"] = orig_xdg_data
        else:
            os.environ.pop("XDG_DATA_HOME", None)
        if orig_snap is not None:
            os.environ["SNAP"] = orig_snap
        else:
            os.environ.pop("SNAP", None)


# ----------------------------------------------------------- shortcut bind

def test_shortcut_bind_unbind() -> None:
    """cli._bind_shortcut/_unbind_shortcut against the fake gsettings
    backend -- covers first bind, idempotent re-bind, refusing to
    clobber an unrelated binding already on the same key combo, and
    unbind. Never touches the real developer machine's GNOME settings."""
    # --- schema unavailable (e.g. under snap without it) --------------
    with _fake_gsettings(available=False) as fake:
        with _captured() as (out, err):
            cli._bind_shortcut()
        assert "skipped" in out.getvalue()
        assert fake.store[(cli._MEDIA_KEYS_SCHEMA, "custom-keybindings")] == \
            "@as []"
        print("shortcut: skips cleanly when schema unavailable OK")

    # --- fresh bind -----------------------------------------------------
    with _fake_gsettings() as fake:
        with _captured() as (out, err):
            cli._bind_shortcut()
        assert "bound" in out.getvalue()
        keybindings = fake.store[(cli._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
        assert cli._KEYBINDING_PATH in keybindings
        assert fake.store[(cli._KEYBINDING_PATH, "binding")] == \
            cli.DEFAULT_SHORTCUT_BINDING
        assert fake.store[(cli._KEYBINDING_PATH, "command")] == \
            cli._wanted_shortcut_command()
        print("shortcut: fresh bind OK")

        # --- re-bind: idempotent, no duplicate entry ---------------------
        with _captured() as (out, err):
            cli._bind_shortcut()
        assert "already bound" in out.getvalue()
        keybindings = fake.store[(cli._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
        assert keybindings.count(cli._KEYBINDING_PATH) == 1
        print("shortcut: re-bind idempotent OK")

        # --- unbind -----------------------------------------------------
        with _captured() as (out, err):
            cli._unbind_shortcut()
        assert "removed" in out.getvalue()
        assert cli._KEYBINDING_PATH not in fake.store[
            (cli._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
        print("shortcut: unbind OK")

        # --- unbind again: idempotent ------------------------------------
        with _captured() as (out, err):
            cli._unbind_shortcut()
        assert "already unbound" in out.getvalue()
        print("shortcut: re-unbind idempotent OK")

    # --- conflict: some other binding already owns our key combo --------
    with _fake_gsettings() as fake:
        other_path = ("/org/gnome/settings-daemon/plugins/media-keys/"
                     "custom-keybindings/someone-elses-shortcut/")
        fake.store[(cli._MEDIA_KEYS_SCHEMA, "custom-keybindings")] = \
            f"['{other_path}']"
        fake.store[(other_path, "binding")] = cli.DEFAULT_SHORTCUT_BINDING
        fake.store[(other_path, "name")] = "Someone Else's Shortcut"

        with _captured() as (out, err):
            cli._bind_shortcut()
        assert "already bound to" in out.getvalue()
        assert "Someone Else's Shortcut" in out.getvalue()
        keybindings = fake.store[(cli._MEDIA_KEYS_SCHEMA, "custom-keybindings")]
        assert cli._KEYBINDING_PATH not in keybindings, (
            "must never clobber an unrelated existing binding on the "
            "same key")
        assert other_path in keybindings, "the other binding must survive"
        print("shortcut: refuses to clobber an unrelated binding on the "
             "same key OK")

    # --- custom --key: valid override is honoured -----------------------
    with _fake_gsettings() as fake:
        with _captured() as (out, err):
            cli._bind_shortcut("<Control><Alt>s")
        assert "bound" in out.getvalue(), out.getvalue()
        assert fake.store[(cli._KEYBINDING_PATH, "binding")] == "<Control><Alt>s"
        print("shortcut: custom --key override bound OK")

    # --- custom --key: invalid value rejected, nothing written -----------
    with _fake_gsettings() as fake:
        with _captured() as (out, err):
            cli._bind_shortcut("not a real accelerator!!")
        assert "invalid --key" in out.getvalue(), out.getvalue()
        assert fake.store[(cli._MEDIA_KEYS_SCHEMA, "custom-keybindings")] == \
            "@as []", "an invalid --key must never write a broken binding"
        print("shortcut: invalid --key rejected without writing anything OK")

    # cmd_install itself should refuse an invalid --key before touching
    # anything (filesystem or gsettings), rather than partially installing.
    with _fake_gsettings():
        with _captured() as (out, err):
            rc = cli.cmd_install(_ns(key="garbage!!!", shortcut=True))
        assert rc == 1
        assert "invalid --key" in err.getvalue(), err.getvalue()
        print("cmd_install: invalid --key rejected up front (rc=1) OK")


def test_accelerator_helpers() -> None:
    """validate_accelerator/accelerator_label -- the default shortcut
    changed from a Super-chord to Ctrl+Alt+N because some keyboards have
    no usable Super key; these are the helpers `nearshare install --key`
    and the GUI's human-readable display both depend on."""
    assert cli.validate_accelerator(cli.DEFAULT_SHORTCUT_BINDING) is None
    assert cli.validate_accelerator("<Control><Alt>s") is None
    assert cli.validate_accelerator("") is not None
    assert cli.validate_accelerator("not an accelerator!!") is not None
    print("validate_accelerator: accepts/rejects as expected OK")

    label = cli.accelerator_label(cli.DEFAULT_SHORTCUT_BINDING)
    assert "N" in label and ("Ctrl" in label or "Control" in label), label
    assert "<" not in label, f"label should be human-readable, got {label!r}"
    print(f"accelerator_label({cli.DEFAULT_SHORTCUT_BINDING!r}) = {label!r} OK")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
