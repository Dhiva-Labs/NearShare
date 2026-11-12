"""Tests for nearshare.core.ble: the beacon payload, the exported
LEAdvertisement1 object, adapter selection and the scanner's decisions.

No Bluetooth hardware, D-Bus or PyGObject is needed. The `gi.repository`
boundary (Gio, GLib) is replaced with in-memory fakes registered in
sys.modules for the duration of each test; the BleAdvertiser / BleScanner
code under test, including their private loop threads, runs for real
against those fakes.

Run:  .venv/bin/python -m tests.test_ble
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import types
import xml.etree.ElementTree as ET

from nearshare.core import ble

OBJ_MGR = "org.freedesktop.DBus.ObjectManager"
ADV_MGR = "org.bluez.LEAdvertisingManager1"


# ------------------------------------------------------------------ fakes

class Variant:
    def __init__(self, sig, value) -> None:
        self.sig, self.value = sig, value

    def unpack(self):
        return self.value

    def __eq__(self, other) -> bool:
        return isinstance(other, Variant) and \
            (self.sig, self.value) == (other.sig, other.value)


class Context:
    def __init__(self) -> None:
        self.pending: list = []
        self.pushed = 0

    def push_thread_default(self) -> None:
        self.pushed += 1

    def pop_thread_default(self) -> None:
        self.pushed -= 1


class Loop:
    def __init__(self, context) -> None:
        self.context, self._quit = context, threading.Event()

    def run(self) -> None:
        while not self._quit.is_set():
            while self.context.pending:
                self.context.pending.pop(0)()
            time.sleep(0.005)

    def quit(self) -> None:
        self._quit.set()


class Idle:
    def set_callback(self, cb) -> None:
        self.cb = cb

    def attach(self, context) -> None:
        context.pending.append(lambda: self.cb())


class FakeBus:
    """Records D-Bus traffic; `managed` is what GetManagedObjects returns."""
    created: list["FakeBus"] = []
    managed: dict = {}
    fail_methods: set = set()

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.objects: dict[int, tuple] = {}
        self.unregistered: list[int] = []
        self.subs: dict[int, tuple] = {}
        self.unsubscribed: list[int] = []
        self._next = 1
        FakeBus.created.append(self)

    def call_sync(self, dest, path, iface, method, params, reply, flags,
                  timeout, cancellable):
        self.calls.append((dest, path, iface, method, params))
        if method in FakeBus.fail_methods:
            raise RuntimeError(f"{method} failed")
        if method == "GetManagedObjects":
            return Variant("(a{oa{sa{sv}}})", (FakeBus.managed,))
        return None

    def register_object(self, path, iface, method_call, get_property, _set):
        rid = self._next
        self._next += 1
        self.objects[rid] = (path, iface, method_call, get_property)
        return rid

    def unregister_object(self, rid) -> None:
        self.unregistered.append(rid)

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, cb):
        sid = self._next
        self._next += 1
        self.subs[sid] = (iface, member, cb)
        return sid

    def signal_unsubscribe(self, sid) -> None:
        self.unsubscribed.append(sid)

    def methods(self) -> list[str]:
        return [c[3] for c in self.calls]

    def emit(self, member: str, params) -> None:
        for iface, m, cb in self.subs.values():
            if m == member:
                cb(self, ":1.1", "/", iface, member, Variant("v", params))


def build_gi(bus_connect_error: Exception | None = None):
    glib = types.SimpleNamespace(
        Variant=Variant,
        VariantType=lambda sig: ("vtype", sig),
        MainContext=types.SimpleNamespace(new=Context),
        MainLoop=types.SimpleNamespace(new=lambda ctx, _run: Loop(ctx)),
        Idle=Idle,
    )

    def new_for_address(addr, flags, _a, _b):
        if bus_connect_error is not None:
            raise bus_connect_error
        return FakeBus()

    gio = types.SimpleNamespace(
        BusType=types.SimpleNamespace(SYSTEM=1),
        DBusConnectionFlags=types.SimpleNamespace(
            AUTHENTICATION_CLIENT=1, MESSAGE_BUS_CONNECTION=2),
        DBusCallFlags=types.SimpleNamespace(NONE=0),
        DBusSignalFlags=types.SimpleNamespace(NONE=0),
        dbus_address_get_for_bus_sync=lambda _t: "unix:fake",
        DBusConnection=types.SimpleNamespace(
            new_for_address_sync=new_for_address),
        DBusNodeInfo=types.SimpleNamespace(new_for_xml=lambda xml:
                                           types.SimpleNamespace(
                                               interfaces=["iface"])),
    )
    repo = types.ModuleType("gi.repository")
    repo.Gio, repo.GLib = gio, glib
    pkg = types.ModuleType("gi")
    pkg.repository = repo
    return {"gi": pkg, "gi.repository": repo}


class fake_gi:
    """Install the fake gi (or, with missing=True, make `import gi` fail)."""

    def __init__(self, missing: bool = False, managed=None, fail=(),
                 connect_error=None) -> None:
        self.missing, self.managed = missing, managed
        self.fail, self.connect_error = set(fail), connect_error

    def __enter__(self):
        self.saved = {k: sys.modules.get(k) for k in ("gi", "gi.repository")}
        if self.missing:
            sys.modules["gi"] = None
            sys.modules["gi.repository"] = None
        else:
            sys.modules.update(build_gi(self.connect_error))
        FakeBus.created = []
        FakeBus.managed = {} if self.managed is None else self.managed
        FakeBus.fail_methods = self.fail
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


ADAPTER = "/org/bluez/hci0"
GOOD_ADAPTER = {ADAPTER: {ADV_MGR: {}, "org.bluez.Adapter1": {}}}


def wait_for(cond, timeout=3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# ------------------------------------------------------- static payloads

def test_beacon_constants() -> None:
    assert ble.SERVICE_UUID == "0000fe2c-0000-1000-8000-00805f9b34fb"
    # 16-bit UUID 0xFE2C expanded onto the Bluetooth base UUID
    assert ble.SERVICE_UUID[4:8] == "fe2c"
    assert ble.SERVICE_UUID.endswith("-0000-1000-8000-00805f9b34fb")
    assert isinstance(ble.SERVICE_DATA, bytes) and len(ble.SERVICE_DATA) == 24
    assert ble.SERVICE_DATA[0] == 0xFC
    # Legacy BLE advertising allows 31 bytes: flags(3) + service data AD
    # header(2) + UUID(2) + payload must fit.
    assert 3 + 2 + 2 + len(ble.SERVICE_DATA) <= 31
    print("beacon: 0xFE2C UUID form and a 24-byte payload that fits legacy BLE")


def test_advertisement_xml_describes_the_exported_interface() -> None:
    root = ET.fromstring(ble._ADV_XML)
    ifaces = root.findall("interface")
    assert [i.get("name") for i in ifaces] == ["org.bluez.LEAdvertisement1"]
    props = {p.get("name"): (p.get("type"), p.get("access"))
             for p in ifaces[0].findall("property")}
    assert props == {"Type": ("s", "read"), "ServiceData": ("a{sv}", "read"),
                     "Includes": ("as", "read")}, props
    assert [m.get("name") for m in ifaces[0].findall("method")] == ["Release"]
    print("advertisement XML: Type/ServiceData/Includes + Release")


# ------------------------------------------------ availability handling

def test_start_without_pygobject_is_unavailable() -> None:
    with fake_gi(missing=True):
        for obj in (ble.BleAdvertiser(), ble.BleScanner(lambda: None)):
            try:
                obj.start()
            except ble.BleUnavailable as exc:
                assert "PyGObject" in str(exc), exc
            else:
                raise AssertionError("start() succeeded without gi")
            assert not obj.active and obj._thread is None
            obj.stop()  # stopping something never started is a no-op
    print("start without gi: BleUnavailable, stays inactive, stop is a no-op")


def test_start_without_adapter_is_unavailable() -> None:
    for cls, args in ((ble.BleAdvertiser, ()), (ble.BleScanner, (lambda: None,))):
        with fake_gi(managed={"/org/bluez": {"org.bluez.AgentManager1": {}}}):
            obj = cls(*args)
            try:
                obj.start()
            except ble.BleUnavailable as exc:
                assert "adapter" in str(exc).lower(), exc
            else:
                raise AssertionError(f"{cls.__name__} started with no adapter")
            assert not obj.active
            bus = FakeBus.created[0]
            assert "RegisterAdvertisement" not in bus.methods()
            assert "StartDiscovery" not in bus.methods()
    print("start with no BlueZ adapter: BleUnavailable and nothing registered")


def test_start_when_system_bus_unreachable() -> None:
    with fake_gi(connect_error=OSError("no system bus")):
        for obj in (ble.BleAdvertiser(), ble.BleScanner(lambda: None)):
            try:
                obj.start()
            except ble.BleUnavailable as exc:
                assert "no system bus" in str(exc), exc
            else:
                raise AssertionError("start() succeeded without a bus")
            assert not obj.active
    print("start with no system bus: BleUnavailable carries the cause")


def test_find_adapter() -> None:
    bus = FakeBus()
    with fake_gi():
        FakeBus.managed = {
            "/org/bluez": {"org.bluez.AgentManager1": {}},
            "/org/bluez/hci0": {"org.bluez.Adapter1": {}},          # no LE adv
            "/org/bluez/hci1": {ADV_MGR: {}},
            "/org/bluez/hci2": {ADV_MGR: {}},
        }
        assert ble._find_adapter(bus) == "/org/bluez/hci1"
        FakeBus.managed = {}
        assert ble._find_adapter(bus) is None
        FakeBus.fail_methods = {"GetManagedObjects"}
        assert ble._find_adapter(bus) is None  # bluez not running
    print("_find_adapter: first LE-advertising adapter, None when absent/erroring")


# ---------------------------------------------------------- advertiser

def test_advertiser_registers_and_unregisters() -> None:
    with fake_gi(managed=GOOD_ADAPTER):
        adv = ble.BleAdvertiser()
        assert not adv.active
        adv.start()
        assert adv.active
        bus = FakeBus.created[0]
        reg = [c for c in bus.calls if c[3] == "RegisterAdvertisement"]
        assert len(reg) == 1
        dest, path, iface, _m, params = reg[0]
        assert (dest, path, iface) == ("org.bluez", ADAPTER, ADV_MGR)
        assert params == Variant("(oa{sv})", (ble._ADV_PATH, {}))
        (obj_path, _i, _mc, _gp), = bus.objects.values()
        assert obj_path == ble._ADV_PATH
        adv.start()                       # idempotent
        assert len(FakeBus.created) == 1
        adv.stop()
        assert not adv.active and adv._thread is None and adv._stop_cb is None
        assert bus.methods()[-1] == "UnregisterAdvertisement"
        assert bus.unregistered == [1]
        adv.stop()                        # second stop is harmless
    print("advertiser: registers once on the adapter, unregisters on stop")


def test_advertiser_exported_properties_and_release() -> None:
    with fake_gi(managed=GOOD_ADAPTER):
        adv = ble.BleAdvertiser()
        adv.start()
        try:
            (_p, _i, method_call, get_property), = FakeBus.created[0].objects.values()
            assert get_property(None, None, None, None, "Type") == \
                Variant("s", "broadcast")
            sd = get_property(None, None, None, None, "ServiceData")
            assert sd == Variant("a{sv}", {
                ble.SERVICE_UUID: Variant("ay", ble.SERVICE_DATA)})
            assert get_property(None, None, None, None, "Includes") == \
                Variant("as", [])
            assert get_property(None, None, None, None, "Bogus") is None

            returned: list = []
            inv = types.SimpleNamespace(return_value=returned.append)
            method_call(None, None, None, None, "Release", None, inv)
            assert returned == [None]
            method_call(None, None, None, None, "Unknown", None, inv)
            assert returned == [None]     # unknown methods are not answered
        finally:
            adv.stop()
    print("advertiser: BlueZ sees broadcast + 0xFE2C data; Release is answered")


def test_advertiser_registration_failure_cleans_up() -> None:
    with fake_gi(managed=GOOD_ADAPTER, fail={"RegisterAdvertisement"}):
        adv = ble.BleAdvertiser()
        try:
            adv.start()
        except ble.BleUnavailable as exc:
            assert "RegisterAdvertisement" in str(exc), exc
        else:
            raise AssertionError("start() hid a RegisterAdvertisement failure")
        bus = FakeBus.created[0]
        assert bus.unregistered == [1], "exported object was left registered"
        assert not adv.active
        # a later attempt is allowed once the cause is gone
        FakeBus.fail_methods = set()
        adv.start()
        assert adv.active
        adv.stop()
    print("advertiser: a failed registration unexports the object and can retry")


def test_advertiser_stop_survives_bluez_errors() -> None:
    with fake_gi(managed=GOOD_ADAPTER):
        adv = ble.BleAdvertiser()
        adv.start()
        FakeBus.fail_methods = {"UnregisterAdvertisement"}  # adapter vanished
        adv.stop()
        assert not adv.active
        assert FakeBus.created[0].unregistered == [1]
    print("advertiser: stop completes even when UnregisterAdvertisement fails")


# ------------------------------------------------------------- scanner

BEACON = {ble.SERVICE_UUID: b"\xfc\x12"}
OTHER = {"0000180f-0000-1000-8000-00805f9b34fb": b"\x50"}


def started_scanner():
    hits: list[int] = []
    sc = ble.BleScanner(lambda: hits.append(1))
    sc.start()
    return sc, hits, FakeBus.created[0]


def test_scanner_discovery_setup_and_teardown() -> None:
    with fake_gi(managed=GOOD_ADAPTER):
        sc, _hits, bus = started_scanner()
        assert sc.active
        names = bus.methods()
        assert names[-2:] == ["SetDiscoveryFilter", "StartDiscovery"], names
        flt = [c for c in bus.calls if c[3] == "SetDiscoveryFilter"][0]
        assert flt[1] == ADAPTER and flt[2] == "org.bluez.Adapter1"
        opts, = flt[4].value
        assert opts["UUIDs"] == Variant("as", [ble.SERVICE_UUID])
        assert opts["Transport"] == Variant("s", "le")
        assert opts["DuplicateData"] == Variant("b", True)
        members = sorted(m for _i, m, _c in bus.subs.values())
        assert members == ["InterfacesAdded", "PropertiesChanged"], members
        sc.start()                              # idempotent
        assert len(FakeBus.created) == 1
        sc.stop()
        assert not sc.active
        assert bus.methods()[-1] == "StopDiscovery"
        assert sorted(bus.unsubscribed) == sorted(bus.subs)
        sc.stop()
    print("scanner: LE+UUID filter, both signals subscribed, clean teardown")


def test_scanner_reports_only_quick_share_beacons() -> None:
    with fake_gi(managed=GOOD_ADAPTER):
        sc, hits, bus = started_scanner()
        try:
            dev = "org.bluez.Device1"
            # InterfacesAdded: (object path, {interface: properties})
            bus.emit("InterfacesAdded", ("/dev/1", {dev: {"ServiceData": BEACON}}))
            assert len(hits) == 1
            bus.emit("InterfacesAdded", ("/dev/2", {dev: {"ServiceData": OTHER}}))
            bus.emit("InterfacesAdded", ("/dev/3", {dev: {}}))
            bus.emit("InterfacesAdded", ("/dev/4", {"org.bluez.GattService1": {}}))
            assert len(hits) == 1
            # PropertiesChanged: (interface, changed, invalidated)
            bus.emit("PropertiesChanged", (dev, {"ServiceData": BEACON}, []))
            assert len(hits) == 2
            bus.emit("PropertiesChanged", (dev, {"RSSI": -60}, []))
            bus.emit("PropertiesChanged", (dev, {"ServiceData": OTHER}, []))
            bus.emit("PropertiesChanged",
                     ("org.bluez.Adapter1", {"ServiceData": BEACON}, []))
            assert len(hits) == 2
            # every sighting counts: the callback is deliberately not rate-limited
            for _ in range(3):
                bus.emit("PropertiesChanged", (dev, {"ServiceData": BEACON}, []))
            assert len(hits) == 5
        finally:
            sc.stop()
    print("scanner: callback fires only for 0xFE2C data on Device1, unthrottled")


def test_scanner_survives_a_failing_callback() -> None:
    with fake_gi(managed=GOOD_ADAPTER):
        calls: list[int] = []

        def bad() -> None:
            calls.append(1)
            raise ValueError("boom")

        sc = ble.BleScanner(bad)
        logging.disable(logging.CRITICAL)  # the contained error is logged
        sc.start()
        bus = FakeBus.created[0]
        try:
            dev = "org.bluez.Device1"
            bus.emit("PropertiesChanged", (dev, {"ServiceData": BEACON}, []))
            bus.emit("PropertiesChanged", (dev, {"ServiceData": BEACON}, []))
            assert len(calls) == 2, "an exception stopped later notifications"
        finally:
            logging.disable(logging.NOTSET)
            sc.stop()
    print("scanner: an exception in the activity callback is contained")


def test_scanner_start_failure_cleans_up() -> None:
    with fake_gi(managed=GOOD_ADAPTER, fail={"StartDiscovery"}):
        sc = ble.BleScanner(lambda: None)
        try:
            sc.start()
        except ble.BleUnavailable as exc:
            assert "StartDiscovery" in str(exc), exc
        else:
            raise AssertionError("start() hid a StartDiscovery failure")
        bus = FakeBus.created[0]
        assert sorted(bus.unsubscribed) == sorted(bus.subs) and bus.subs
        assert not sc.active
    print("scanner: failed StartDiscovery unsubscribes both signals")


def test_scanner_stop_survives_bluez_errors() -> None:
    with fake_gi(managed=GOOD_ADAPTER):
        sc, _hits, bus = started_scanner()
        FakeBus.fail_methods = {"StopDiscovery"}
        sc.stop()
        assert not sc.active
        assert sorted(bus.unsubscribed) == sorted(bus.subs)
    print("scanner: stop completes even when StopDiscovery fails")


def test_scanner_ignores_malformed_signal_bodies() -> None:
    # A garbled signal from the bus must not take down discovery; GLib logs
    # an exception from a signal callback and keeps dispatching, which the
    # fake mirrors by letting us call the handler directly and carry on.
    with fake_gi(managed=GOOD_ADAPTER):
        sc, hits, bus = started_scanner()
        try:
            for member, body in (("InterfacesAdded", ("/x", {})),
                                 ("PropertiesChanged", ("other.iface", {}, []))):
                bus.emit(member, body)
            assert hits == []
            bus.emit("PropertiesChanged",
                     ("org.bluez.Device1", {"ServiceData": BEACON}, []))
            assert hits == [1]
        finally:
            sc.stop()
    print("scanner: empty and foreign-interface signals are ignored")


# ----------------------------------------------------------------- main

def main() -> int:
    test_beacon_constants()
    test_advertisement_xml_describes_the_exported_interface()
    test_start_without_pygobject_is_unavailable()
    test_start_without_adapter_is_unavailable()
    test_start_when_system_bus_unreachable()
    test_find_adapter()
    test_advertiser_registers_and_unregisters()
    test_advertiser_exported_properties_and_release()
    test_advertiser_registration_failure_cleans_up()
    test_advertiser_stop_survives_bluez_errors()
    test_scanner_discovery_setup_and_teardown()
    test_scanner_reports_only_quick_share_beacons()
    test_scanner_survives_a_failing_callback()
    test_scanner_start_failure_cleans_up()
    test_scanner_stop_survives_bluez_errors()
    test_scanner_ignores_malformed_signal_bodies()
    print("\nBLE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
