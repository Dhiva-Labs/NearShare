"""Tests for nearshare.core.mdns: service records, instance names and the
Browser's peer-resolution decisions.

zeroconf is faked only at its boundary (AsyncZeroconf, AsyncServiceBrowser,
AsyncServiceInfo, ServiceInfo are swapped for in-memory fakes on the mdns
module); no sockets are opened and nothing touches the network. The
parsing, naming and state logic under test is the real code.

A test marked XFAIL documents a real bug: it asserts the correct behaviour
and is expected to fail until the code is fixed (an unexpected pass is
reported as an error so the marker gets removed).

Run:  .venv/bin/python -m tests.test_mdns
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

from nearshare.core import mdns, names
from nearshare.core.connection import build_endpoint_info, parse_endpoint_info

PEER_IP = "192.168.7.20"


# ------------------------------------------------------------------ fakes

class FakeInfo:
    """Stands in for AsyncServiceInfo: serves canned data per service name."""
    records: dict[str, dict] = {}

    def __init__(self, type_: str, name: str) -> None:
        self.type, self.name = type_, name
        rec = FakeInfo.records.get(name, {})
        self._found = rec.get("found", True)
        self._addresses = rec.get("addresses", [PEER_IP])
        self.properties = rec.get("properties", {})
        self.port = rec.get("port", 40000)

    async def async_request(self, zc, timeout) -> bool:
        return self._found

    def parsed_addresses(self, version=None) -> list[str]:
        return list(self._addresses)


class FakeAsyncZeroconf:
    instances: list["FakeAsyncZeroconf"] = []
    fail_unregister = False

    def __init__(self, ip_version=None) -> None:
        self.zeroconf = object()
        self.registered: list = []
        self.closed = False
        FakeAsyncZeroconf.instances.append(self)

    async def async_register_service(self, info) -> None:
        self.registered.append(info)

    async def async_unregister_service(self, info) -> None:
        if FakeAsyncZeroconf.fail_unregister:
            raise RuntimeError("unregister failed")
        self.registered.remove(info)

    async def async_close(self) -> None:
        self.closed = True


class FakeServiceInfo:
    def __init__(self, type_, name, port=0, properties=None, server=None,
                 addresses=None) -> None:
        self.type, self.name, self.port = type_, name, port
        self.properties, self.server, self.addresses = (
            properties, server, addresses)


class FakeBrowser:
    last: "FakeBrowser | None" = None

    def __init__(self, zc, type_, listener=None) -> None:
        self.type, self.listener, self.cancelled = type_, listener, False
        FakeBrowser.last = self

    async def async_cancel(self) -> None:
        self.cancelled = True


class patched:
    """Swap the zeroconf names on the mdns module for the fakes."""

    def __enter__(self):
        self.saved = {n: getattr(mdns, n) for n in (
            "AsyncZeroconf", "AsyncServiceBrowser", "AsyncServiceInfo",
            "ServiceInfo", "_local_ipv4s", "_local_addresses")}
        mdns.AsyncZeroconf = FakeAsyncZeroconf
        mdns.AsyncServiceBrowser = FakeBrowser
        mdns.AsyncServiceInfo = FakeInfo
        mdns.ServiceInfo = FakeServiceInfo
        mdns._local_ipv4s = lambda: {"127.0.0.1", "192.168.7.5"}
        mdns._local_addresses = lambda: [b"\xc0\xa8\x07\x05"]
        FakeInfo.records = {}
        FakeAsyncZeroconf.instances = []
        FakeAsyncZeroconf.fail_unregister = False
        FakeBrowser.last = None
        return self

    def __exit__(self, *exc):
        for n, v in self.saved.items():
            setattr(mdns, n, v)


def full_name(endpoint_id: str) -> str:
    return f"{mdns.encode_instance_name(endpoint_id)}.{mdns.SERVICE_TYPE}"


def txt_for(name: str, device_type: int = 3) -> dict:
    blob = build_endpoint_info(name, device_type)
    return {b"n": mdns._b64(blob).encode()}


def fresh_browser(**kw):
    changes: list[dict] = []
    b = mdns.Browser(lambda peers: changes.append(peers), **kw)
    b._aiozc = FakeAsyncZeroconf()
    return b, changes


def isolate_names_cache() -> str:
    """Point the name cache at an empty temp dir and drop the in-memory copy."""
    d = tempfile.mkdtemp(prefix="ns-mdns-")
    os.environ["XDG_CONFIG_HOME"] = d
    names._cache = None
    return d


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ encoding

def test_b64_roundtrip_without_padding() -> None:
    for n in range(0, 40):
        raw = bytes(range(n))
        text = mdns._b64(raw)
        assert "=" not in text and "+" not in text and "/" not in text, text
        assert mdns._unb64(text) == raw, n
    # url-safe alphabet is used, not the standard one
    assert mdns._b64(b"\xfb\xff\xfe") == "-__-"
    assert mdns._unb64("-__-") == b"\xfb\xff\xfe"
    print("base64url helpers: unpadded round trip, url-safe alphabet")


def test_unb64_rejects_garbage() -> None:
    for bad in ("a", "abcde", "é" * 4):
        try:
            mdns._unb64(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} decoded without error")
    print("base64url helpers: bad length / non-ASCII text raises ValueError")


def test_instance_name_layout() -> None:
    name = mdns.encode_instance_name("AB12")
    assert len(name) == 14 and "=" not in name, name
    raw = mdns._unb64(name)
    assert len(raw) == 10
    assert raw[0] == mdns.PCP_P2P_CLUSTER == 0x23
    assert raw[1:5] == b"AB12"
    assert raw[5:8] == mdns.SERVICE_ID_HASH == bytes([0xFC, 0x9F, 0x5E])
    assert raw[8:10] == b"\x00\x00"
    assert mdns.encode_instance_name("AB12") == name  # deterministic
    assert mdns.encode_instance_name("AB13") != name
    assert mdns.SERVICE_TYPE == "_FC9F5ED42C8A._tcp.local."
    print("instance name: 10-byte PCP/id/hash layout, 14 chars, deterministic")


def test_instance_name_rejects_non_ascii_id() -> None:
    try:
        mdns.encode_instance_name("Aé12")
    except UnicodeEncodeError:
        print("instance name: non-ASCII endpoint id is refused")
        return
    raise AssertionError("non-ASCII endpoint id was accepted")


def test_endpoint_info_in_txt_roundtrips() -> None:
    for dtype in (1, 2, 3):
        for name in ("Laptop", "Dhiva's PC", "Ünïcode ✓"):
            blob = build_endpoint_info(name, dtype)
            got = parse_endpoint_info(mdns._unb64(mdns._b64(blob)))
            assert got == (name, dtype), (name, dtype, got)
    print("TXT 'n' value: endpoint info survives the base64url round trip")


# ----------------------------------------------------------- advertiser

def test_advertiser_registers_service_record() -> None:
    async def go():
        with patched():
            adv = mdns.Advertiser("Test Box", "QWER", 41234)
            assert not adv.active
            await adv.start()
            assert adv.active
            zc = FakeAsyncZeroconf.instances[0]
            assert len(zc.registered) == 1
            info = zc.registered[0]
            assert info.type == mdns.SERVICE_TYPE
            assert info.name == full_name("QWER")
            assert info.port == 41234
            assert info.addresses == [b"\xc0\xa8\x07\x05"]
            assert info.server.endswith("-nearshare.local.")
            name, dtype = parse_endpoint_info(mdns._unb64(info.properties["n"]))
            assert (name, dtype) == ("Test Box", 3)
            # a second start() is a no-op: no second registration
            await adv.start()
            assert len(FakeAsyncZeroconf.instances) == 1
            await adv.stop()
            assert not adv.active and zc.registered == [] and zc.closed
            await adv.stop()  # stopping twice is harmless
    run(go())
    print("advertiser: service name, port, TXT payload, idempotent start/stop")


def test_advertiser_stop_state_reset_on_unregister_error() -> None:
    async def go():
        with patched():
            adv = mdns.Advertiser("Box", "ZZZZ", 1)
            await adv.start()
            FakeAsyncZeroconf.fail_unregister = True
            try:
                await adv.stop()
            except RuntimeError:
                pass
            assert not adv.active and adv._info is None
    run(go())
    print("advertiser: a failed unregister still leaves it stopped")


def test_advertiser_stop_closes_zeroconf_even_if_unregister_fails() -> None:
    async def go():
        with patched():
            adv = mdns.Advertiser("Box", "ZZZZ", 1)
            await adv.start()
            zc = FakeAsyncZeroconf.instances[0]
            FakeAsyncZeroconf.fail_unregister = True
            try:
                await adv.stop()
            except RuntimeError:
                pass
            assert zc.closed, "Zeroconf instance leaked (never closed)"
    run(go())


def test_advertiser_long_multibyte_name_roundtrips() -> None:
    # build_endpoint_info cuts the UTF-8 name at 255 bytes; for a name made of
    # 2-byte characters that lands mid-character and the peer then sees
    # "Unknown device" instead of a truncated name.
    blob = build_endpoint_info("é" * 128)
    name, _ = parse_endpoint_info(blob)
    assert name != "Unknown device", name


# -------------------------------------------------------- local address

def test_local_ipv4s_parses_ip_json() -> None:
    real_run = subprocess.run
    payload = (b'[{"addr_info":[{"local":"10.0.0.4"},{"local":"192.168.1.9"},'
               b'{"peer":"x"}]},{"ifname":"lo"},{"addr_info":[]}]')

    def fake_run(cmd, **kw):
        assert cmd[:2] == ["ip", "-j"]
        return subprocess.CompletedProcess(cmd, 0, stdout=payload)

    subprocess.run = fake_run
    try:
        got = mdns._local_ipv4s()
    finally:
        subprocess.run = real_run
    assert got == {"127.0.0.1", "10.0.0.4", "192.168.1.9"}, got
    print("_local_ipv4s: reads `ip -j addr`, always includes loopback")


def test_local_ipv4s_falls_back_on_bad_output() -> None:
    real_run, real_socket = subprocess.run, mdns.socket.socket

    class FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def connect(self, addr): pass
        def getsockname(self): return ("172.16.0.3", 5555)

    for broken in (lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=b"not json"),
                   lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("ip"))):
        subprocess.run = broken
        mdns.socket.socket = lambda *a, **k: FakeSock()
        try:
            got = mdns._local_ipv4s()
        finally:
            subprocess.run, mdns.socket.socket = real_run, real_socket
        assert got == {"127.0.0.1", "172.16.0.3"}, got
    # nothing works at all: still returns loopback instead of raising
    subprocess.run = lambda cmd, **kw: (_ for _ in ()).throw(OSError("x"))

    def boom(*a, **k):
        raise OSError("no network")
    mdns.socket.socket = boom
    try:
        assert mdns._local_ipv4s() == {"127.0.0.1"}
    finally:
        subprocess.run, mdns.socket.socket = real_run, real_socket
    print("_local_ipv4s: falls back to the UDP trick, then to loopback only")


def test_local_addresses_fallback_to_loopback() -> None:
    real_socket = mdns.socket.socket

    def boom(*a, **k):
        raise OSError("offline")
    mdns.socket.socket = boom
    try:
        assert mdns._local_addresses() == [b"\x7f\x00\x00\x01"]
    finally:
        mdns.socket.socket = real_socket
    print("_local_addresses: offline machine advertises 127.0.0.1")


# -------------------------------------------------------------- browser

def test_browser_resolves_a_peer() -> None:
    async def go():
        with patched():
            isolate_names_cache()
            b, changes = fresh_browser()
            name = full_name("PE3R")
            FakeInfo.records[name] = {"properties": txt_for("Pixel 9", 1),
                                      "port": 51000}
            await b._resolve(name)
            assert list(b.peers) == [name]
            p = b.peers[name]
            assert p == mdns.Peer(instance=mdns.encode_instance_name("PE3R"),
                                  endpoint_id="PE3R", device_name="Pixel 9",
                                  device_type=1, host=PEER_IP, port=51000)
            assert len(changes) == 1 and changes[0] == b.peers
            assert changes[0] is not b.peers  # callback gets a copy
    run(go())
    print("browser: resolved record becomes a Peer and fires on_change once")


def test_browser_skips_unresolvable_and_addressless() -> None:
    async def go():
        with patched():
            isolate_names_cache()
            b, changes = fresh_browser()
            n1, n2 = full_name("AAAA"), full_name("BBBB")
            FakeInfo.records[n1] = {"found": False}
            FakeInfo.records[n2] = {"addresses": []}
            await b._resolve(n1)
            await b._resolve(n2)
            assert b.peers == {} and changes == []
    run(go())
    print("browser: failed lookups and records without IPv4 are dropped")


def test_browser_self_filtering() -> None:
    async def go():
        with patched():
            isolate_names_cache()
            # 1. our own endpoint id is ignored before any lookup happens
            b, changes = fresh_browser()
            b.ignore_instance("MINE")
            await b._resolve(full_name("MINE"))
            assert b.peers == {} and changes == []
            # 2. a sibling advertisement from this machine's own address
            sibling = full_name("SIBL")
            FakeInfo.records[sibling] = {"addresses": ["192.168.7.5"]}
            await b._resolve(sibling)
            assert b.peers == {}
            await b._resolve(full_name("OTHR"))
            assert len(b.peers) == 1
            # 3. include_self keeps the sibling
            b2, _ = fresh_browser(include_self=True)
            await b2._resolve(sibling)
            assert list(b2.peers) == [sibling]
    run(go())
    print("browser: own id and own-address advertisements are filtered")


def test_browser_malformed_instance_names() -> None:
    async def go():
        with patched():
            isolate_names_cache()
            b, _ = fresh_browser()
            cases = {
                "!!!notbase64!!!": "????",           # not decodable
                "é-é-é-é-": "????",                   # non-ASCII text
                mdns._b64(b"\x23AB"): "????",         # truncated (< 5 bytes)
                "": "????",                           # empty instance
                mdns._b64(b"\x23\xff\xfe\xfd\xfc" + b"\x00" * 5): "�" * 4,
            }
            for instance, want in cases.items():
                name = f"{instance}.{mdns.SERVICE_TYPE}"
                FakeInfo.records[name] = {}
                b.peers.clear()
                await b._resolve(name)
                assert list(b.peers) == [name], instance
                assert b.peers[name].endpoint_id == want, (instance, want)
            # the instance is the part of the name before the service type
            assert b.peers[name].instance == instance
    run(go())
    print("browser: undecodable/truncated instance names degrade to '????'")


def test_browser_malformed_txt_records() -> None:
    async def go():
        with patched():
            isolate_names_cache()
            b, _ = fresh_browser()
            name = full_name("ABCD")
            good = build_endpoint_info("Named", 2)
            bad_props = [
                {},                                         # no n key
                {b"n": b""},                                # empty value
                {b"n": None},                               # key without value
                {b"n": b"$$$$"},                            # not base64
                {b"n": b"\xff\xfe"},                        # not UTF-8
                {b"n": mdns._b64(b"").encode()},            # empty blob
                {b"n": mdns._b64(good[:10]).encode()},      # truncated blob
                {b"n": mdns._b64(good[:18]).encode()},      # name length cut off
            ]
            for props in bad_props:
                FakeInfo.records[name] = {"properties": props}
                b.peers.clear()
                await b._resolve(name)
                p = b.peers[name]  # must still produce a peer, never raise
                assert p.device_name.endswith("(ABCD)"), (props, p.device_name)
            # a truncated name body keeps the part that arrived
            FakeInfo.records[name] = {"properties": {
                b"n": mdns._b64(good[:-2]).encode()}}
            b.peers.clear()
            await b._resolve(name)
            assert b.peers[name].device_name == "Nam"
    run(go())
    print("browser: malformed or truncated TXT blobs never raise")


def test_browser_placeholder_names_by_device_type() -> None:
    async def go():
        with patched():
            isolate_names_cache()
            b, _ = fresh_browser()
            name = full_name("WXYZ")
            want = {1: "Phone (WXYZ)", 2: "Tablet (WXYZ)",
                    3: "Computer (WXYZ)", 0: "Device (WXYZ)",
                    5: "Device (WXYZ)"}
            for dtype, label in want.items():
                # flags byte with the type bits but an empty name
                blob = bytes([dtype << 1]) + b"\x00" * 16 + b"\x00"
                FakeInfo.records[name] = {"properties": {
                    b"n": mdns._b64(blob).encode()}}
                await b._resolve(name)
                p = b.peers[name]
                assert p.device_name == label, (dtype, p.device_name)
                assert p.device_type == dtype
    run(go())
    print("browser: nameless phones get a type label plus endpoint id")


def test_browser_uses_cached_name_for_nameless_peer() -> None:
    async def go():
        with patched():
            isolate_names_cache()
            names.remember(PEER_IP, "Mum's phone")
            b, _ = fresh_browser()
            name = full_name("CACH")
            blob = bytes([1 << 1]) + b"\x00" * 16 + b"\x00"
            FakeInfo.records[name] = {"properties": {
                b"n": mdns._b64(blob).encode()}}
            await b._resolve(name)
            assert b.peers[name].device_name == "Mum's phone"
            # an advertised name wins over the cache
            FakeInfo.records[name] = {"properties": txt_for("Live Name", 1)}
            await b._resolve(name)
            assert b.peers[name].device_name == "Live Name"
    run(go())
    print("browser: learned name fills in for nameless peers only")


def test_browser_port_defaults_to_zero() -> None:
    async def go():
        with patched():
            isolate_names_cache()
            b, _ = fresh_browser()
            name = full_name("PORT")
            FakeInfo.records[name] = {"port": None}
            await b._resolve(name)
            assert b.peers[name].port == 0
    run(go())
    print("browser: a missing port is reported as 0")


def test_browser_remove_and_reresolve() -> None:
    async def go():
        with patched():
            isolate_names_cache()
            b, changes = fresh_browser()
            a, c = full_name("AAAA"), full_name("CCCC")
            await b._resolve(a)
            await b._resolve(c)
            assert len(changes) == 2 and len(changes[1]) == 2
            b._remove(a)
            assert list(b.peers) == [c] and len(changes) == 3
            b._remove(a)                   # already gone: no callback
            b._remove("never-seen." + mdns.SERVICE_TYPE)
            assert len(changes) == 3
            # an update for a known service replaces it rather than duplicating
            FakeInfo.records[c] = {"port": 9999}
            await b._resolve(c)
            assert len(b.peers) == 1 and b.peers[c].port == 9999
    run(go())
    print("browser: remove notifies once, re-resolve updates in place")


def test_browser_start_stop_lifecycle() -> None:
    async def go():
        with patched():
            b = mdns.Browser(lambda peers: None)
            await b.start()
            zc = FakeAsyncZeroconf.instances[0]
            assert FakeBrowser.last.type == mdns.SERVICE_TYPE
            await b.start()                      # idempotent
            assert len(FakeAsyncZeroconf.instances) == 1
            b.peers["x"] = object()
            await b.stop()
            assert b.peers == {} and b._aiozc is None and b._browser is None
            assert zc.closed and FakeBrowser.last.cancelled
            await b.stop()                       # harmless when stopped
    run(go())
    print("browser: start is idempotent, stop cancels, closes and clears")


def test_browser_listener_dispatches_to_loop() -> None:
    async def go():
        with patched():
            isolate_names_cache()
            changes = []
            b = mdns.Browser(lambda peers: changes.append(dict(peers)))
            await b.start()
            listener = FakeBrowser.last.listener
            name = full_name("LSTN")
            FakeInfo.records[name] = {}
            # zeroconf calls these from its own thread
            await asyncio.to_thread(listener.add_service, None, mdns.SERVICE_TYPE, name)
            for _ in range(50):
                if name in b.peers:
                    break
                await asyncio.sleep(0.02)
            assert name in b.peers, "add_service did not resolve the peer"
            await asyncio.to_thread(listener.remove_service, None, mdns.SERVICE_TYPE, name)
            for _ in range(50):
                if name not in b.peers:
                    break
                await asyncio.sleep(0.02)
            assert name not in b.peers, "remove_service did not drop the peer"
            await b.stop()
    run(go())
    print("browser: listener callbacks from a foreign thread reach the loop")


def test_resolve_after_stop_does_not_raise() -> None:
    # A listener callback can queue _resolve just before stop(); once the
    # browser is stopped _aiozc is None and _resolve dereferences it.
    async def go():
        with patched():
            b = mdns.Browser(lambda peers: None)
            await b.start()
            await b.stop()
            await b._resolve(full_name("LATE"))
            assert b.peers == {}
    run(go())


# ----------------------------------------------------------------- main

def _xfail(fn, reason: str) -> bool:
    """Run fn, which must fail; returns True when it failed as expected."""
    try:
        fn()
    except Exception as exc:
        print(f"XFAIL {fn.__name__}: {reason} ({type(exc).__name__}: {exc})")
        return True
    raise AssertionError(f"{fn.__name__} unexpectedly passed; remove its XFAIL")


def main() -> int:
    test_b64_roundtrip_without_padding()
    test_unb64_rejects_garbage()
    test_instance_name_layout()
    test_instance_name_rejects_non_ascii_id()
    test_endpoint_info_in_txt_roundtrips()
    test_advertiser_registers_service_record()
    test_advertiser_stop_state_reset_on_unregister_error()
    test_local_ipv4s_parses_ip_json()
    test_local_ipv4s_falls_back_on_bad_output()
    test_local_addresses_fallback_to_loopback()
    test_browser_resolves_a_peer()
    test_browser_skips_unresolvable_and_addressless()
    test_browser_self_filtering()
    test_browser_malformed_instance_names()
    test_browser_malformed_txt_records()
    test_browser_placeholder_names_by_device_type()
    test_browser_uses_cached_name_for_nameless_peer()
    test_browser_port_defaults_to_zero()
    test_browser_remove_and_reresolve()
    test_browser_start_stop_lifecycle()
    test_browser_listener_dispatches_to_loop()
    xfails = [
        (test_advertiser_stop_closes_zeroconf_even_if_unregister_fails,
         "Advertiser.stop skips async_close() when unregister raises"),
        (test_advertiser_long_multibyte_name_roundtrips,
         "build_endpoint_info cuts the name mid-character at 255 bytes"),
        (test_resolve_after_stop_does_not_raise,
         "Browser._resolve dereferences _aiozc after stop()"),
    ]
    for fn, reason in xfails:
        _xfail(fn, reason)
    print("\nMDNS TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
