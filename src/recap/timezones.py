"""Resolve IANA timezones, including the local machine's zone."""

from __future__ import annotations

import os
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_timezone_name(name: str) -> None:
    if not name or name.lower() == "local":
        return
    if name != "UTC" and "/" not in name:
        raise ValueError(f"Use an IANA timezone such as Asia/Kolkata, not {name!r}")
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {name}") from exc


def _candidate_zone(name: str) -> tuple[tzinfo, str] | None:
    try:
        validate_timezone_name(name)
        if name and name.lower() != "local":
            return ZoneInfo(name), name
    except ValueError:
        pass
    return None


def resolve_timezone(name: str = "local") -> tuple[tzinfo, str]:
    """Return a DST-aware local zone when its IANA identity is available."""
    validate_timezone_name(name)
    if name and name.lower() != "local":
        return ZoneInfo(name), name

    env_name = os.environ.get("TZ", "").lstrip(":")
    candidate = _candidate_zone(env_name)
    if candidate:
        return candidate

    try:
        from tzlocal import get_localzone_name

        candidate = _candidate_zone(get_localzone_name())
        if candidate:
            return candidate
    except (ImportError, OSError, ValueError, ZoneInfoNotFoundError):
        pass

    timezone_file = Path("/etc/timezone")
    if timezone_file.is_file():
        try:
            candidate = _candidate_zone(
                timezone_file.read_text(encoding="utf-8").strip()
            )
            if candidate:
                return candidate
        except OSError:
            pass

    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        try:
            target = str(localtime.resolve())
            if "/zoneinfo/" in target:
                candidate = _candidate_zone(target.split("/zoneinfo/", 1)[1])
                if candidate:
                    return candidate
        except OSError:
            pass

    # Last resort: current OS offset. The package normally supplies tzlocal.
    local = datetime.now(UTC).astimezone()
    offset_value = local.utcoffset()
    offset = int(offset_value.total_seconds() // 60) if offset_value is not None else 0
    sign = "+" if offset >= 0 else "-"
    hours, minutes = divmod(abs(offset), 60)
    return local.tzinfo or UTC, f"local UTC{sign}{hours:02d}:{minutes:02d}"
