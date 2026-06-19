from __future__ import annotations

import logging
from typing import Any

from tunarr_autoscheduler.core.config import ConfigManager
from tunarr_autoscheduler.integrations.tunarr.client import TunarrClient
from tunarr_autoscheduler.models.config import (
    ChannelConfig,
    DayOfWeek,
    DaypartTemplate,
    RotationConfig,
)

logger = logging.getLogger(__name__)

DEFAULT_DAYPARTS = [
    DaypartTemplate(
        name="morning",
        days=[DayOfWeek.MON, DayOfWeek.TUE, DayOfWeek.WED, DayOfWeek.THU, DayOfWeek.FRI],
        start_time="06:00", end_time="12:00",
        rotation="default", slot_duration_minutes=30,
        allow_movies=False, ad_density=0.08, continuity_frequency=4,
    ),
    DaypartTemplate(
        name="afternoon",
        days=[DayOfWeek.MON, DayOfWeek.TUE, DayOfWeek.WED, DayOfWeek.THU, DayOfWeek.FRI],
        start_time="12:00", end_time="18:00",
        rotation="default", slot_duration_minutes=30,
        allow_movies=False, ad_density=0.08, continuity_frequency=4,
    ),
    DaypartTemplate(
        name="primetime",
        days=[DayOfWeek.MON, DayOfWeek.TUE, DayOfWeek.WED, DayOfWeek.THU, DayOfWeek.FRI],
        start_time="18:00", end_time="23:00",
        rotation="default", slot_duration_minutes=60,
        allow_movies=True, variable_movie_duration=True, movie_slot_count=1,
        ad_density=0.12, continuity_frequency=4,
    ),
    DaypartTemplate(
        name="overnight",
        days=[DayOfWeek.MON, DayOfWeek.TUE, DayOfWeek.WED, DayOfWeek.THU, DayOfWeek.FRI,
              DayOfWeek.SAT, DayOfWeek.SUN],
        start_time="23:00", end_time="06:00",
        rotation="default", slot_duration_minutes=60,
        allow_movies=True, end_tolerance_minutes=15,
        ad_density=0.04, continuity_frequency=8, off_air=True,
    ),
    DaypartTemplate(
        name="weekend_morning",
        days=[DayOfWeek.SAT, DayOfWeek.SUN],
        start_time="06:00", end_time="12:00",
        rotation="default", slot_duration_minutes=30,
        allow_movies=False, ad_density=0.08, continuity_frequency=4,
    ),
    DaypartTemplate(
        name="weekend_afternoon",
        days=[DayOfWeek.SAT, DayOfWeek.SUN],
        start_time="12:00", end_time="20:00",
        rotation="default", slot_duration_minutes=45,
        allow_movies=True, variable_movie_duration=True, movie_slot_count=2,
        ad_density=0.10, continuity_frequency=4,
    ),
    DaypartTemplate(
        name="weekend_primetime",
        days=[DayOfWeek.SAT, DayOfWeek.SUN],
        start_time="20:00", end_time="23:00",
        rotation="default", slot_duration_minutes=60,
        allow_movies=True, variable_movie_duration=True, movie_slot_count=1,
        ad_density=0.12, continuity_frequency=4,
    ),
]


class ChannelSyncEngine:
    def __init__(self, tunarr_client: TunarrClient, config_manager: ConfigManager):
        self._client = tunarr_client
        self._config_manager = config_manager

    async def sync(self) -> dict[str, Any]:
        config = self._config_manager.config()
        remote_channels = await self._client.get_channels()

        existing_map: dict[str, ChannelConfig] = {
            c.id: c for c in config.channels
        }

        added: list[str] = []
        preserved: list[str] = []
        warned: list[str] = []

        new_channels: list[ChannelConfig] = []

        for remote in remote_channels:
            remote_id = remote.get("id", str(remote.get("uuid", "")))
            remote_name = remote.get("name", "Unknown")

            if remote_id in existing_map:
                existing = existing_map[remote_id]
                existing.name = remote_name
                new_channels.append(existing)
                preserved.append(remote_name)
                continue

            show_ids = await self._extract_show_ids(remote)

            channel = ChannelConfig(
                id=remote_id,
                name=remote_name,
                scheduling_enabled=False,
                dayparts=list(DEFAULT_DAYPARTS),
                rotations=[RotationConfig(
                    name="default",
                    show_ids=show_ids,
                )] if show_ids else [],
            )
            new_channels.append(channel)
            added.append(remote_name)

        remote_ids = {c.get("id", str(c.get("uuid", ""))) for c in remote_channels}
        for configured in config.channels:
            if configured.id not in remote_ids:
                new_channels.append(configured)
                warned.append(configured.name)

        config.channels = new_channels
        self._config_manager.save(config)

        result: dict[str, Any] = {
            "added": added,
            "preserved": preserved,
            "warned": warned,
            "total": len(new_channels),
        }
        logger.info("Channel sync complete: %s", result)
        return result

    async def _extract_show_ids(self, channel_data: dict[str, Any]) -> list[str]:
        shows = channel_data.get("shows", []) or channel_data.get("programs", [])
        if not shows:
            programs = channel_data.get("programs", []) or channel_data.get("items", [])
            return [str(p.get("uuid", p.get("id", ""))) for p in programs if isinstance(p, dict)]
        return [str(s.get("uuid", s.get("id", ""))) for s in shows if isinstance(s, dict)]
