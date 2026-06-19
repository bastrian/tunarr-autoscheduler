from __future__ import annotations

from typing import Any


def unwrap_program(program: dict[str, Any]) -> dict[str, Any]:
    current = program
    while isinstance(current.get("program"), dict):
        current = current["program"]
    return current


def program_subtype(program: dict[str, Any]) -> str:
    source = unwrap_program(program)
    return str(source.get("subtype") or source.get("type") or "").lower()


def program_ids(program: dict[str, Any]) -> list[str]:
    source = unwrap_program(program)
    ids: list[str] = []
    for key in ("id", "uuid", "externalSourceId", "externalKey", "uniqueId", "serverFileKey"):
        value = source.get(key)
        if value:
            ids.append(str(value))

    external_ids = source.get("externalIds")
    if isinstance(external_ids, list):
        for external_id in external_ids:
            if isinstance(external_id, dict) and external_id.get("id"):
                ids.append(str(external_id["id"]))

    seen: set[str] = set()
    unique_ids: list[str] = []
    for item_id in ids:
        if item_id and item_id not in seen:
            unique_ids.append(item_id)
            seen.add(item_id)
    return unique_ids


def program_show_id(program: dict[str, Any]) -> str:
    source = unwrap_program(program)
    for key in ("showId", "seriesId", "parentId"):
        value = source.get(key)
        if value:
            return str(value)
    show = source.get("show")
    if isinstance(show, dict) and show.get("uuid"):
        return str(show["uuid"])
    parent = source.get("parent")
    if isinstance(parent, dict) and parent.get("id"):
        return str(parent["id"])
    return ""


def program_duration_seconds(program: dict[str, Any], default: int) -> int:
    duration = unwrap_program(program).get("duration")
    try:
        value = int(float(str(duration)))
    except (TypeError, ValueError):
        return default
    return int(value / 1000) if value > 100_000 else value
