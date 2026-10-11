"""BLE trigger advertising so Android phones discover us instantly.

Android's Quick Share sheet listens for a BLE service-data advertisement
on 16-bit UUID 0xFE2C before it aggressively browses mDNS. Without this
"trigger" beacon, the phone may never (or only slowly) list an
mDNS-only receiver. The payload below is the same static trigger
rnearshare broadcasts; it carries no identity — actual discovery and
transfer still happen over mDNS + TCP.

Implemented against BlueZ's org.bluez.LEAdvertisingManager1 D-Bus API
via Gio. BlueZ calls back into our exported LEAdvertisement1 object
(GetAll properties, Release), so those callbacks need a running GLib
main loop; we spin a private one on a dedicated thread with its own
MainContext, which keeps this module independent of whether a GTK app
is running (GUI and headless daemon both work).

Everything degrades gracefully: no adapter, no BlueZ, no gi — BLE is
simply reported unavailable and mDNS-only operation continues.
"""

from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger("nearshare.ble")

SERVICE_UUID = "0000fe2c-0000-1000-8000-00805f9b34fb"
# Static Quick Share trigger payload (matches rnearshare's beacon).
SERVICE_DATA = bytes([
    0xFC, 0x12, 0x8E, 0x01, 0x42, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0xBF, 0x2D, 0x5B, 0xA0, 0xE1, 0xD8, 0x75, 0x24,
    0xCA, 0x00,
])

_ADV_PATH_BASE = "/dev/dhivalabs/nearshare/"

_ADV_XML = """
<node>
  <interface name="org.bluez.LEAdvertisement1">
    <method name="Release"/>
    <property name="Type" type="s" access="read"/>
    <property name="ServiceData" type="a{sv}" access="read"/>
    <property name="ManufacturerData" type="a{qv}" access="read"/>
    <property name="Includes" type="as" access="read"/>
    <property name="MinInterval" type="u" access="read"/>
    <property name="MaxInterval" type="u" access="read"/>
  </interface>
</node>
"""


class BleUnavailable(Exception):
    """Bluetooth advertising can't be provided in this environment."""


class BleAdvertiser:
    """Broadcasts a BLE advertisement while started: by default the Quick
    Share trigger beacon, or any service/manufacturer data given (the
    AirDrop wake advert in nearshare.airdrop.wake uses this).

    Thread-safe start()/stop(); both are blocking but fast. All D-Bus
    traffic happens on a private GLib main-loop thread. Each instance
    exports its own object path, so several can advertise at once.
    """

    def __init__(self, service_data: dict[str, bytes] | None = None,
                 manufacturer_data: dict[int, bytes] | None = None,
                 name: str = "advertisement0",
                 label: str = "BLE trigger advertising",
                 connectable: bool = False,
                 interval_ms: int | None = None) -> None:
        if service_data is None and manufacturer_data is None:
            service_data = {SERVICE_UUID: SERVICE_DATA}
        self._service_data = service_data or {}
        self._manufacturer_data = manufacturer_data or {}
        self._path = _ADV_PATH_BASE + name
        self._label = label
        self._connectable = connectable
        self._interval_ms = interval_ms
        self._thread: threading.Thread | None = None
        self._stop_cb = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            try:
                from gi.repository import Gio, GLib  # noqa: F401
            except ImportError as exc:
                raise BleUnavailable(f"PyGObject not importable: {exc}")
            ready = threading.Event()
            error: list[Exception] = []
            self._thread = threading.Thread(
                target=self._run, args=(ready, error),
                name="nearshare-ble", daemon=True)
            self._thread.start()
            ready.wait(timeout=10)
            if error:
                self._thread = None
                raise BleUnavailable(str(error[0]))
            log.info("%s started", self._label)

    def stop(self) -> None:
        with self._lock:
            if self._thread is None:
                return
            if self._stop_cb is not None:
                self._stop_cb()
            self._thread.join(timeout=5)
            self._thread = None
            self._stop_cb = None
            log.info("%s stopped", self._label)

    # ------------------------------------------------------------------

    def _run(self, ready: threading.Event, error: list[Exception]) -> None:
        from gi.repository import Gio, GLib

        context = GLib.MainContext.new()
        context.push_thread_default()
        loop = GLib.MainLoop.new(context, False)
        bus = None
        reg_id = 0
        adapter = None
        try:
            # Dedicated connection so exported-object callbacks dispatch
            # to THIS thread's context, not the (maybe absent) GTK loop.
            addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM)
            bus = Gio.DBusConnection.new_for_address_sync(
                addr,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
                Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)

            adapter = _find_adapter(bus)
            if adapter is None:
                raise BleUnavailable("no Bluetooth adapter with LE "
                                     "advertising support found")

            node = Gio.DBusNodeInfo.new_for_xml(_ADV_XML)

            def get_property(_conn, _sender, _path, _iface, name):
                if name == "Type":
                    return GLib.Variant(
                        "s", "peripheral" if self._connectable else "broadcast")
                if name in ("MinInterval", "MaxInterval"):
                    # Left out (None) unless asked for: BlueZ rejects the
                    # whole advertisement over an out-of-range interval,
                    # and its own default suits the Quick Share beacon.
                    if self._interval_ms is None:
                        return None
                    return GLib.Variant("u", self._interval_ms)
                if name == "ServiceData":
                    return GLib.Variant("a{sv}", {
                        uuid: GLib.Variant("ay", data)
                        for uuid, data in self._service_data.items()})
                if name == "ManufacturerData":
                    return GLib.Variant("a{qv}", {
                        company: GLib.Variant("ay", data)
                        for company, data in self._manufacturer_data.items()})
                if name == "Includes":
                    return GLib.Variant("as", [])
                return None

            def method_call(_conn, _sender, _path, _iface, method, _params,
                            invocation):
                # BlueZ calls Release when it tears the advertisement down.
                if method == "Release":
                    invocation.return_value(None)

            reg_id = bus.register_object(
                self._path, node.interfaces[0], method_call, get_property,
                None)

            bus.call_sync(
                "org.bluez", adapter, "org.bluez.LEAdvertisingManager1",
                "RegisterAdvertisement",
                GLib.Variant("(oa{sv})", (self._path, {})),
                None, Gio.DBusCallFlags.NONE, 10_000, None)
        except Exception as exc:  # surfaced via start()
            error.append(exc)
            if reg_id and bus is not None:
                bus.unregister_object(reg_id)
            context.pop_thread_default()
            ready.set()
            return

        def request_stop() -> None:
            def _do_stop(*_args) -> bool:
                try:
                    bus.call_sync(
                        "org.bluez", adapter,
                        "org.bluez.LEAdvertisingManager1",
                        "UnregisterAdvertisement",
                        GLib.Variant("(o)", (self._path,)),
                        None, Gio.DBusCallFlags.NONE, 5_000, None)
                except Exception:
                    pass  # adapter gone, bluez restarted, etc.
                bus.unregister_object(reg_id)
                loop.quit()
                return False
            # GLib.idle_add targets the *default* context; build a source
            # attached to our private context instead.
            source = GLib.Idle()
            source.set_callback(_do_stop)
            source.attach(context)

        self._stop_cb = request_stop
        ready.set()
        loop.run()
        context.pop_thread_default()

class BleScanner:
    """Listens for Quick Share BLE beacons from nearby devices.

    Android broadcasts service data on 0xFE2C while its share sheet (and,
    on many models, the receive screen) is open. Seeing that beacon tells
    us a Quick Share peer is physically nearby *right now* — we surface it
    via the callback so the app can react (e.g. prompt to go visible, or
    nudge the user that a sender is looking for targets). The beacon
    carries no address/identity; actual connection still runs over mDNS.

    Same private GLib-loop-thread pattern as BleAdvertiser.
    """

    def __init__(self, on_activity) -> None:
        """on_activity: callable() -> None, invoked (rate-unlimited, from
        the BLE thread) each time a Quick Share beacon is seen."""
        self._on_activity = on_activity
        self._thread: threading.Thread | None = None
        self._stop_cb = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            try:
                from gi.repository import Gio, GLib  # noqa: F401
            except ImportError as exc:
                raise BleUnavailable(f"PyGObject not importable: {exc}")
            ready = threading.Event()
            error: list[Exception] = []
            self._thread = threading.Thread(
                target=self._run, args=(ready, error),
                name="nearshare-ble-scan", daemon=True)
            self._thread.start()
            ready.wait(timeout=15)
            if error:
                self._thread = None
                raise BleUnavailable(str(error[0]))
            log.info("BLE scanning for Quick Share beacons started")

    def stop(self) -> None:
        with self._lock:
            if self._thread is None:
                return
            if self._stop_cb is not None:
                self._stop_cb()
            self._thread.join(timeout=5)
            self._thread = None
            self._stop_cb = None
            log.info("BLE scanning stopped")

    def _run(self, ready: threading.Event, error: list[Exception]) -> None:
        from gi.repository import Gio, GLib

        context = GLib.MainContext.new()
        context.push_thread_default()
        loop = GLib.MainLoop.new(context, False)
        bus = None
        sub_ids: list[int] = []
        adapter = None
        try:
            addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM)
            bus = Gio.DBusConnection.new_for_address_sync(
                addr,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
                Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)
            adapter = _find_adapter(bus)
            if adapter is None:
                raise BleUnavailable("no Bluetooth adapter found")

            def saw_service_data(service_data: dict) -> None:
                if SERVICE_UUID in service_data:
                    try:
                        self._on_activity()
                    except Exception:
                        log.exception("BLE activity callback failed")

            def on_signal(_bus, _sender, _path, _iface, signal, params):
                body = params.unpack()
                if signal == "InterfacesAdded":
                    _, interfaces = body
                    dev = interfaces.get("org.bluez.Device1", {})
                    saw_service_data(dev.get("ServiceData", {}))
                elif signal == "PropertiesChanged":
                    iface, changed, _ = body
                    if iface == "org.bluez.Device1":
                        saw_service_data(changed.get("ServiceData", {}))

            sub_ids.append(bus.signal_subscribe(
                "org.bluez", "org.freedesktop.DBus.ObjectManager",
                "InterfacesAdded", None, None,
                Gio.DBusSignalFlags.NONE, on_signal))
            sub_ids.append(bus.signal_subscribe(
                "org.bluez", "org.freedesktop.DBus.Properties",
                "PropertiesChanged", None, None,
                Gio.DBusSignalFlags.NONE, on_signal))

            # Filter discovery to LE + our UUID so BlueZ doesn't flood us
            # (and the radio) with unrelated devices.
            bus.call_sync(
                "org.bluez", adapter, "org.bluez.Adapter1",
                "SetDiscoveryFilter",
                GLib.Variant("(a{sv})", ({
                    "UUIDs": GLib.Variant("as", [SERVICE_UUID]),
                    "Transport": GLib.Variant("s", "le"),
                    "DuplicateData": GLib.Variant("b", True),
                },)),
                None, Gio.DBusCallFlags.NONE, 10_000, None)
            bus.call_sync(
                "org.bluez", adapter, "org.bluez.Adapter1",
                "StartDiscovery", None,
                None, Gio.DBusCallFlags.NONE, 10_000, None)
        except Exception as exc:
            error.append(exc)
            for sid in sub_ids:
                bus.signal_unsubscribe(sid)
            context.pop_thread_default()
            ready.set()
            return

        def request_stop() -> None:
            def _do_stop(*_args) -> bool:
                try:
                    bus.call_sync(
                        "org.bluez", adapter, "org.bluez.Adapter1",
                        "StopDiscovery", None,
                        None, Gio.DBusCallFlags.NONE, 5_000, None)
                except Exception:
                    pass
                for sid in sub_ids:
                    bus.signal_unsubscribe(sid)
                loop.quit()
                return False
            source = GLib.Idle()
            source.set_callback(_do_stop)
            source.attach(context)

        self._stop_cb = request_stop
        ready.set()
        loop.run()
        context.pop_thread_default()


def _find_adapter(bus) -> str | None:
    """First BlueZ adapter object exposing LEAdvertisingManager1."""
    from gi.repository import GLib
    try:
        result = bus.call_sync(
            "org.bluez", "/", "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects", None,
            GLib.VariantType("(a{oa{sa{sv}}})"),
            0, 10_000, None)
    except Exception:
        return None
    for path, interfaces in result.unpack()[0].items():
        if "org.bluez.LEAdvertisingManager1" in interfaces:
            return path
    return None


if sys.platform == "win32":
    # BlueZ is Linux-only. On Windows the same three names drive WinRT's
    # advertisement publisher and watcher through a small helper process.
    from .ble_windows import (BleAdvertiser, BleScanner,  # noqa: E402,F811
                              BleUnavailable)
