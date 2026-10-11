"""Persistent cache of peer device names learned from real transfers.

Phones hide their user-set name in mDNS advertisements unless visibility
is "Everyone" (Android privacy behaviour), so discovery often only knows
"Phone (A1B2)". But every inbound transfer hands us the sender's real
name in its ConnectionRequest endpoint info. Remember it, keyed by IP —
the only stable-ish identifier across sessions on a home LAN (endpoint
ids are re-randomised constantly, by design).

Stored as JSON at ~/.config/nearshare/known_devices.json.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from .paths import config_dir

log = logging.getLogger("nearshare.names")

_lock = threading.Lock()
_cache: dict[str, str] | None = None


def _path() -> Path:
    return config_dir() / "known_devices.json"


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_path().read_text())
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _entry_name(entry) -> str:
    """Entries are either a bare string (learned) or {name, manual}."""
    return entry["name"] if isinstance(entry, dict) else entry


def lookup(ip: str) -> str | None:
    with _lock:
        entry = _load().get(ip)
        return _entry_name(entry) if entry else None


def all_names() -> dict[str, str]:
    with _lock:
        return {ip: _entry_name(e) for ip, e in _load().items()}


def _save(cache: dict) -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=1))
    except OSError as exc:
        log.warning("could not persist device names: %s", exc)


def remember(ip: str, name: str, manual: bool = False) -> None:
    """Record a device name.

    Learned names (manual=False) come from a device introducing itself
    during a transfer. A name the user set by hand always wins — a later
    transfer must not silently undo `nearshare rename`.
    """
    if not name or name == "Unknown device" or ip.startswith("127."):
        return
    with _lock:
        cache = _load()
        existing = cache.get(ip)
        if isinstance(existing, dict) and existing.get("manual") \
                and not manual:
            return  # user-set name wins over a learned one
        if _entry_name(existing) == name and \
                bool(isinstance(existing, dict) and
                     existing.get("manual")) == manual:
            return
        cache[ip] = {"name": name, "manual": True} if manual else name
        _save(cache)
        log.info("%s device name %r for %s",
                 "set" if manual else "learned", name, ip)


def forget(ip: str) -> bool:
    with _lock:
        cache = _load()
        if cache.pop(ip, None) is None:
            return False
        _save(cache)
        return True
