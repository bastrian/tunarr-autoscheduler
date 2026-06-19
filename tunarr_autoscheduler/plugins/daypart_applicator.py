from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.core.timezones import now_in_timezone
from tunarr_autoscheduler.models.blocks import SlotBlock
from tunarr_autoscheduler.models.config import DayOfWeek, DaypartTemplate

logger = logging.getLogger(__name__)

WEEKDAY_MAP = {
    0: DayOfWeek.MON, 1: DayOfWeek.TUE, 2: DayOfWeek.WED,
    3: DayOfWeek.THU, 4: DayOfWeek.FRI, 5: DayOfWeek.SAT, 6: DayOfWeek.SUN,
}

DEFAULT_SEGMENT_HOURS = 6


class DaypartApplicator(Plugin):
    name = "daypart_applicator"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        channel_config = context.channel_config
        dayparts = channel_config.dayparts

        if not dayparts:
            return timeline

        validation_errors = self._validate_dayparts(dayparts)
        for err in validation_errors:
            logger.warning("Daypart config: %s", err)

        horizon = timeline.total_duration()
        if horizon.total_seconds() == 0:
            horizon = timedelta(days=channel_config.schedule_horizon_days)

        timezone_name = getattr(channel_config, "timezone", None)
        app_config = getattr(context, "app_config", None)
        if app_config is not None:
            timezone_name = getattr(app_config, "timezone", timezone_name)
        timezone = ZoneInfo(str(timezone_name or "UTC"))
        start = context.schedule_start or now_in_timezone(str(timezone_name or "UTC"))
        start = start.astimezone(timezone)
        end = start + horizon
        result = Timeline()
        result.metadata.update({
            "generation_mode": context.generation_mode,
            "parent_version": context.parent_version,
            "planned_start": start.isoformat(),
            "planned_end": end.isoformat(),
        })

        current = start
        while current < end:
            daypart = self._find_daypart(current, dayparts)
            slot_minutes = daypart.slot_duration_minutes if daypart else 60
            standby_custom_show_id = channel_config.standby_custom_show_id
            daypart_boundary = self._next_daypart_boundary(current, dayparts, daypart)
            segment_end = min(
                current + timedelta(minutes=max(5, slot_minutes)),
                daypart_boundary,
                end,
            )

            metadata = {
                "daypart": daypart.name if daypart else "standby",
                "content_mode": daypart.content_mode if daypart else "off_air",
                "slot_duration_minutes": daypart.slot_duration_minutes if daypart else 60,
                "allow_movies": daypart.allow_movies if daypart else False,
                "variable_movie_duration": (
                    daypart.variable_movie_duration if daypart else False
                ),
                "movie_selection": daypart.movie_selection if daypart else "best_fit",
                "end_tolerance_minutes": daypart.end_tolerance_minutes if daypart else 0,
                "ad_density": daypart.ad_density if daypart else 0.0,
                "daypart_start": daypart.start_time if daypart else "00:00",
                "daypart_end": daypart.end_time if daypart else "23:59",
                "daypart_boundary": min(daypart_boundary, end).isoformat(),
                "rotation": daypart.rotation if daypart else "default",
                "custom_show_list_ids": (
                    daypart.custom_show_list_ids
                    if daypart else (
                        [standby_custom_show_id] if standby_custom_show_id else []
                    )
                ),
                "playlist_ids": daypart.playlist_ids if daypart else [],
                "continuity_frequency": daypart.continuity_frequency if daypart else 0,
                "off_air": daypart.off_air if daypart else True,
                "custom_show_loop": (
                    bool(daypart.custom_show_list_ids) and daypart.off_air
                    if daypart else bool(standby_custom_show_id)
                ),
            }

            block = SlotBlock(
                start_time=current,
                end_time=segment_end,
                duration=segment_end - current,
                metadata=metadata,
            )
            result.insert(block)
            current = segment_end

        return result

    def _next_daypart_boundary(
        self,
        dt: datetime,
        dayparts: list[DaypartTemplate],
        active_daypart: DaypartTemplate | None = None,
    ) -> datetime:
        if active_daypart is not None:
            return self._next_daypart_end(dt, active_daypart)

        candidates: list[datetime] = []
        for day_offset in range(3):
            day = (dt + timedelta(days=day_offset)).date()
            for dp in dayparts:
                hour, minute = self._parse_time(dp.start_time)
                boundary = datetime.combine(
                    day,
                    datetime.min.time(),
                    tzinfo=dt.tzinfo,
                ).replace(hour=hour, minute=minute)
                if boundary > dt:
                    candidates.append(boundary)
        return min(candidates) if candidates else dt + timedelta(hours=DEFAULT_SEGMENT_HOURS)

    def _next_daypart_end(self, dt: datetime, daypart: DaypartTemplate) -> datetime:
        start_hour, start_minute = self._parse_time(daypart.start_time)
        end_hour, end_minute = self._parse_time(daypart.end_time)
        end_day = dt.date()
        if daypart.end_time <= daypart.start_time:
            current_time = dt.strftime("%H:%M")
            if current_time >= daypart.start_time:
                end_day = (dt + timedelta(days=1)).date()
        boundary = datetime.combine(
            end_day,
            datetime.min.time(),
            tzinfo=dt.tzinfo,
        ).replace(hour=end_hour, minute=end_minute)
        if boundary <= dt:
            boundary += timedelta(days=1)
        if daypart.end_time == daypart.start_time:
            boundary = datetime.combine(
                dt.date(),
                datetime.min.time(),
                tzinfo=dt.tzinfo,
            ).replace(hour=start_hour, minute=start_minute) + timedelta(days=1)
        return boundary

    def _find_daypart(
        self, dt: datetime, dayparts: list[DaypartTemplate],
    ) -> DaypartTemplate | None:
        if not dayparts:
            return None

        weekday = WEEKDAY_MAP[dt.weekday()]
        current_time = dt.strftime("%H:%M")

        for dp in dayparts:
            if weekday not in dp.days:
                continue
            if self._time_in_range(dp.start_time, dp.end_time, current_time):
                return dp

        return None

    def _time_in_range(self, start: str, end: str, current: str) -> bool:
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    def _validate_dayparts(self, dayparts: list[DaypartTemplate]) -> list[str]:
        errors: list[str] = []
        names: set[str] = set()
        for dp in dayparts:
            if dp.name in names:
                errors.append(f"Duplicate daypart name: '{dp.name}'")
            names.add(dp.name)

            try:
                self._parse_time(dp.start_time)
                self._parse_time(dp.end_time)
            except ValueError as e:
                errors.append(f"Daypart '{dp.name}': {e}")

            if dp.slot_duration_minutes < 5:
                errors.append(
                    f"Daypart '{dp.name}': "
                    f"slot_duration_minutes is too low ({dp.slot_duration_minutes})",
                )
            if dp.end_tolerance_minutes < 0:
                errors.append(f"Daypart '{dp.name}': end_tolerance_minutes cannot be negative")
            if dp.ad_density < 0 or dp.ad_density > 0.5:
                errors.append(f"Daypart '{dp.name}': ad_density must be between 0 and 0.5")

        return errors

    def _parse_time(self, t: str) -> tuple[int, int]:
        parts = t.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid time format: '{t}' (expected HH:MM)")
        h, m = int(parts[0]), int(parts[1])
        if h < 0 or h > 23 or m < 0 or m > 59:
            raise ValueError(f"Invalid time: '{t}'")
        return h, m
