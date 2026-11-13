"""Text formatting and icon choices shared by the main window and the
send picker. No GTK imports, so it can be tested without a display."""

from __future__ import annotations

from pathlib import Path

# wire_format.proto's device types as they arrive in the mDNS record.
DEVICE_TYPE_NAMES = {0: "Device", 1: "Phone", 2: "Tablet", 3: "Computer"}
DEVICE_TYPE_ICONS = {0: "network-wireless-symbolic", 1: "phone-symbolic",
                     2: "tablet-symbolic", 3: "computer-symbolic"}


def device_type_name(device_type: int) -> str:
    return DEVICE_TYPE_NAMES.get(device_type, "Device")


def device_icon(device_type: int) -> str:
    return DEVICE_TYPE_ICONS.get(device_type, "network-wireless-symbolic")


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_speed(bytes_per_second: float) -> str:
    return f"{human_size(int(bytes_per_second))}/s"


def human_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def count_files(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


def describe_files(paths: list[Path]) -> str:
    """"report.pdf", "report.pdf and photo.jpg", "report.pdf and 4 more"."""
    if not paths:
        return "nothing"
    first = paths[0].name
    if len(paths) == 1:
        return first
    if len(paths) == 2:
        return f"{first} and {paths[1].name}"
    return f"{first} and {len(paths) - 1} more"


def split_sendable(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Separate regular files from what cannot be sent (folders, missing
    files): the protocol transfers a flat list of files."""
    files = [p for p in paths if p.is_file()]
    rejected = [p for p in paths if not p.is_file()]
    return files, rejected


def total_size(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total
