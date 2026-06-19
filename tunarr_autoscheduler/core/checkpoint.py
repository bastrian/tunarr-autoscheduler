from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tunarr_autoscheduler.core.timeline import Timeline


class CheckpointManager:
    def __init__(self, base_dir: str | None = None):
        self._base_dir = base_dir or os.path.expanduser("~/.tunarr/checkpoints")

    def save(self, channel_id: str, generation_id: str, stage_name: str, timeline: Timeline) -> str:
        path = Path(self._base_dir) / channel_id / generation_id / f"{stage_name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "channel_id": channel_id,
            "generation_id": generation_id,
            "stage_name": stage_name,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "timeline": timeline.snapshot(),
        }
        path.write_text(json.dumps(data, default=str, indent=2))
        return str(path)

    def load(self, channel_id: str, generation_id: str, stage_name: str) -> dict[str, Any] | None:
        path = Path(self._base_dir) / channel_id / generation_id / f"{stage_name}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return data.get("timeline")  # type: ignore[no-any-return]

    def get_last_stage(
        self,
        channel_id: str,
        generation_id: str,
        pipeline_order: list[str] | None = None,
    ) -> str | None:
        path = Path(self._base_dir) / channel_id / generation_id
        if not path.exists():
            return None
        stages = [p for p in path.iterdir() if p.suffix == ".json"]
        if not stages:
            return None
        if pipeline_order:
            completed = {p.stem for p in stages}
            for stage_name in reversed(pipeline_order):
                if stage_name in completed:
                    return stage_name
            return None
        stages = sorted(stages, key=self._checkpoint_timestamp)
        return stages[-1].stem

    def get_latest_generation(self, channel_id: str) -> str | None:
        path = Path(self._base_dir) / channel_id
        if not path.exists():
            return None
        gens = sorted(path.iterdir())
        if not gens:
            return None
        return gens[-1].name

    def clear(self, channel_id: str, generation_id: str) -> None:
        path = Path(self._base_dir) / channel_id / generation_id
        if path.exists():
            import shutil
            shutil.rmtree(path)

    def clear_all(self, channel_id: str) -> None:
        path = Path(self._base_dir) / channel_id
        if path.exists():
            import shutil
            shutil.rmtree(path)

    def _checkpoint_timestamp(self, path: Path) -> str:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return ""
        return str(data.get("timestamp", ""))
