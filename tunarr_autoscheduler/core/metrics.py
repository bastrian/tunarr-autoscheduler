from __future__ import annotations

from collections import defaultdict


class MetricsCollector:
    def __init__(self) -> None:
        self._pipeline_stage_durations: dict[str, list[float]] = defaultdict(list)
        self._pipeline_stage_counts: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int),
        )
        self._generation_counts: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int),
        )
        self._media_sync_durations: list[float] = []
        self._media_sync_items: dict[str, int] = defaultdict(int)
        self._upload_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._validation_errors: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._active_generations: int = 0

    def record_pipeline_stage(
        self, channel_id: str, stage: str, duration_ms: float, status: str,
    ) -> None:
        key = f"{channel_id}:{stage}"
        self._pipeline_stage_durations[key].append(duration_ms)
        self._pipeline_stage_counts[key][status] += 1

    def record_generation(self, channel_id: str, status: str) -> None:
        self._generation_counts[channel_id][status] += 1

    def record_media_sync(self, duration_ms: float, new_items: int, removed_items: int) -> None:
        self._media_sync_durations.append(duration_ms)
        self._media_sync_items["new"] += new_items
        self._media_sync_items["removed"] += removed_items

    def record_upload(self, channel_id: str, status: str) -> None:
        self._upload_counts[channel_id][status] += 1

    def record_validation_error(self, channel_id: str, error_type: str) -> None:
        self._validation_errors[channel_id][error_type] += 1

    def set_active_generations(self, count: int) -> None:
        self._active_generations = count

    def get_metrics(self) -> dict[str, object]:
        return {
            "pipeline_stage_durations": dict(self._pipeline_stage_durations),
            "pipeline_stage_counts": dict(self._pipeline_stage_counts),
            "generation_counts": dict(self._generation_counts),
            "media_sync_items": dict(self._media_sync_items),
            "upload_counts": dict(self._upload_counts),
            "validation_errors": dict(self._validation_errors),
            "active_generations": self._active_generations,
        }

    def render_prometheus(self) -> str:
        lines = [
            "# HELP tunarr_active_generations Active schedule generation jobs.",
            "# TYPE tunarr_active_generations gauge",
            f"tunarr_active_generations {self._active_generations}",
            "# HELP tunarr_generation_total Schedule generations by channel and status.",
            "# TYPE tunarr_generation_total counter",
        ]
        for channel_id, statuses in self._generation_counts.items():
            for status, count in statuses.items():
                lines.append(
                    'tunarr_generation_total'
                    f'{{channel_id="{channel_id}",status="{status}"}} {count}'
                )

        lines.extend([
            "# HELP tunarr_pipeline_stage_total Pipeline stages by channel, stage, and status.",
            "# TYPE tunarr_pipeline_stage_total counter",
        ])
        for key, statuses in self._pipeline_stage_counts.items():
            channel_id, stage = key.split(":", 1)
            for status, count in statuses.items():
                lines.append(
                    'tunarr_pipeline_stage_total'
                    f'{{channel_id="{channel_id}",stage="{stage}",status="{status}"}} {count}'
                )
        lines.extend([
            "# HELP tunarr_pipeline_stage_duration_ms_sum "
            "Total pipeline stage duration in milliseconds.",
            "# TYPE tunarr_pipeline_stage_duration_ms_sum counter",
        ])
        for key, durations in self._pipeline_stage_durations.items():
            channel_id, stage = key.split(":", 1)
            lines.append(
                'tunarr_pipeline_stage_duration_ms_sum'
                f'{{channel_id="{channel_id}",stage="{stage}"}} {sum(durations):.3f}'
            )
        lines.extend([
            "# HELP tunarr_pipeline_stage_duration_ms_avg "
            "Average pipeline stage duration in milliseconds.",
            "# TYPE tunarr_pipeline_stage_duration_ms_avg gauge",
        ])
        for key, durations in self._pipeline_stage_durations.items():
            if not durations:
                continue
            channel_id, stage = key.split(":", 1)
            lines.append(
                'tunarr_pipeline_stage_duration_ms_avg'
                f'{{channel_id="{channel_id}",stage="{stage}"}} '
                f'{(sum(durations) / len(durations)):.3f}'
            )

        lines.extend([
            "# HELP tunarr_media_sync_items_total Media sync item counts by result type.",
            "# TYPE tunarr_media_sync_items_total counter",
        ])
        for item_type, count in self._media_sync_items.items():
            lines.append(f'tunarr_media_sync_items_total{{type="{item_type}"}} {count}')

        lines.extend([
            "# HELP tunarr_upload_total Tunarr uploads by channel and status.",
            "# TYPE tunarr_upload_total counter",
        ])
        for channel_id, statuses in self._upload_counts.items():
            for status, count in statuses.items():
                lines.append(
                    'tunarr_upload_total'
                    f'{{channel_id="{channel_id}",status="{status}"}} {count}'
                )

        lines.extend([
            "# HELP tunarr_validation_errors_total Validation errors by channel and type.",
            "# TYPE tunarr_validation_errors_total counter",
        ])
        for channel_id, error_types in self._validation_errors.items():
            for error_type, count in error_types.items():
                lines.append(
                    'tunarr_validation_errors_total'
                    f'{{channel_id="{channel_id}",error_type="{error_type}"}} {count}'
                )

        return "\n".join(lines) + "\n"
