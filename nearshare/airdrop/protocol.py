"""AirDrop wire constants and message bodies.

The exchange, as iOS 26 and macOS 15 speak it, is three HTTPS POSTs:

  /Discover  "who are you?"  Both sides send binary plists; the answer
             carries ReceiverComputerName, the name shown under the tile.
  /Ask       "may I send these?"  Lists Files (or Items, for links) and
             blocks until the receiver's user accepts (200) or declines
             (403). Carries TransferID = {"id": <UUID>}.
  /Upload    the files, as one cpio archive in DVZip (see archive.py),
             chunked, with a TransferID header repeating the Ask's. A
             receiver binds the upload to the Ask it accepted by that id;
             an unannounced id is refused before the body is read.

A link is an /Ask with TransferType "links" and the URLs in Items, and
no /Upload follows: accepting the Ask is the whole transfer.

Apple devices listen on port 8770. Our receiver serves 8771, the port
the radio backend's announcement names (omdrop's convention), so it
never collides with anything else bound on the AWDL interface.

Sources: the published research (Stute et al., "A Billion Open
Interfaces for Eve and Mallory", USENIX Security 2019) and wire
observations of iOS 26 recorded by the omdrop and airdrop-mt7921
projects.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import plistlib
import secrets
import uuid
from pathlib import Path

SERVICE_TYPE = "_airdrop._tcp.local."
PEER_PORT = 8770
RECEIVER_PORT = 8771
AWDL_IFACE = "awdl0"

# The device glyph a sender draws for us comes from this. Apple model
# identifiers only; this one is what omdrop has been tested with.
DEFAULT_MODEL = "MacBookPro18,3"

# Version 1 with no codecs listed: senders convert to legacy formats
# (JPEG rather than HEIF) before sending, so whatever arrives opens on
# Linux without extra codecs. The key's presence is what matters.
MEDIA_CAPABILITIES = json.dumps({"Version": 1}).encode()

# What a Mac's /Discover request carries.
DEVICE_SUPPORT_FLAGS = 111611

USER_AGENT = "AirDrop/1.0"

# UTIs for the file types people commonly send. Anything else is
# public.data, which every receiver accepts as an opaque file.
_UTI_BY_EXT = {
    ".jpg": "public.jpeg", ".jpeg": "public.jpeg", ".png": "public.png",
    ".gif": "com.compuserve.gif", ".heic": "public.heic",
    ".heif": "public.heif", ".webp": "org.webmproject.webp",
    ".tif": "public.tiff", ".tiff": "public.tiff", ".bmp": "com.microsoft.bmp",
    ".mov": "com.apple.quicktime-movie", ".mp4": "public.mpeg-4",
    ".m4v": "com.apple.m4v-video", ".mp3": "public.mp3",
    ".m4a": "com.apple.m4a-audio", ".wav": "com.microsoft.waveform-audio",
    ".pdf": "com.adobe.pdf", ".txt": "public.plain-text",
    ".md": "net.daringfireball.markdown", ".csv": "public.comma-separated-values-text",
    ".html": "public.html", ".json": "public.json", ".zip": "public.zip-archive",
    ".vcf": "public.vcard", ".ics": "com.apple.ical.ics",
    ".docx": "org.openxmlformats.wordprocessingml.document",
    ".xlsx": "org.openxmlformats.spreadsheetml.sheet",
    ".pptx": "org.openxmlformats.presentationml.presentation",
    ".pages": "com.apple.iwork.pages.sffpages",
}


def uti_for(path: Path | str) -> str:
    return _UTI_BY_EXT.get(os.path.splitext(str(path))[1].lower(), "public.data")


def mime_for(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def new_transfer_id() -> str:
    return str(uuid.uuid4()).upper()


def random_sender_id() -> str:
    """12 hex digits, the shape of a MAC: what a sender calls itself when
    no AWDL interface address is available to use instead."""
    return secrets.token_hex(6)


def dumps(obj: dict) -> bytes:
    return plistlib.dumps(obj, fmt=plistlib.FMT_BINARY)


def loads(data: bytes) -> dict:
    """Parse a peer's plist; anything that isn't a dictionary is empty."""
    try:
        value = plistlib.loads(data)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


# ------------------------------------------------------------------ bodies

def discover_request() -> bytes:
    return dumps({"DeviceSupportFlags": DEVICE_SUPPORT_FLAGS})


def receiver_answer(name: str, model: str) -> bytes:
    """Our answer to /Discover and to an accepted /Ask."""
    return dumps({
        "ReceiverMediaCapabilities": MEDIA_CAPABILITIES,
        "ReceiverComputerName": name,
        "ReceiverModelName": model,
    })


def ask_files(sender_name: str, model: str, sender_id: str,
              transfer_id: str, files: list[tuple[str, int, str]],
              icon: bytes | None = None) -> bytes:
    """/Ask for a files transfer. ``files`` is (archive name, size, UTI)."""
    body = {
        "TransferID": {"id": transfer_id},
        "TransferType": {"files": {}},
        "SenderID": sender_id,
        "BundleID": "com.apple.finder",
        "SenderComputerName": sender_name,
        "SenderModelName": model,
        "Items": [],
        "Files": [{
            "FileName": name,
            "FileType": uti,
            "FileSize": size,
            "FileBomPath": f"./{name}",
            "FileIsDirectory": False,
            "ConvertMediaFormats": False,
        } for name, size, uti in files],
        "ConvertMediaFormats": False,
    }
    if icon:
        body["FileIcon"] = icon
    return dumps(body)


def ask_links(sender_name: str, model: str, sender_id: str,
              transfer_id: str, urls: list[str]) -> bytes:
    return dumps({
        "TransferID": {"id": transfer_id},
        "TransferType": {"links": {}},
        "SenderID": sender_id,
        "BundleID": "com.apple.Safari",
        "SenderComputerName": sender_name,
        "SenderModelName": model,
        "Items": list(urls),
        "ConvertMediaFormats": False,
    })


def is_web_link(item: object) -> bool:
    return (isinstance(item, str) and len(item) < 8192
            and item.lower().startswith(("http://", "https://"))
            and not any(c.isspace() for c in item))


# ------------------------------------------------------------------ addresses

def eui64_link_local(mac: str) -> str:
    """fe80:: address a peer derives from its AWDL MAC (RFC 4291 EUI-64).

    Apple devices configure exactly this on awdl0, so a MAC heard on the
    link is enough to dial the peer without waiting for an mDNS answer.
    """
    octets = bytes(int(p, 16) for p in mac.split(":"))
    if len(octets) != 6:
        raise ValueError(f"not a MAC address: {mac!r}")
    eui = bytes([octets[0] ^ 0x02]) + octets[1:3] + b"\xff\xfe" + octets[3:]
    return str(ipaddress.IPv6Address(b"\xfe\x80" + bytes(6) + eui))


def scoped(address: str, iface: str | None) -> str:
    """Attach the zone a link-local address needs to be routable."""
    if iface and "%" not in address:
        try:
            if ipaddress.IPv6Address(address).is_link_local:
                return f"{address}%{iface}"
        except ValueError:
            pass
    return address
