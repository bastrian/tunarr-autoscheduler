from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def app_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo((name or "UTC").strip() or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def now_in_timezone(name: str | None) -> datetime:
    return datetime.now(tz=app_timezone(name))


def to_timezone(value: datetime | str | None, name: str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        if not value:
            return None
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(app_timezone(name))


def format_datetime(value: datetime | str | None, name: str | None) -> str:
    localized = to_timezone(value, name)
    if localized is None:
        return "-"
    return localized.strftime("%Y-%m-%d %H:%M:%S %Z")
