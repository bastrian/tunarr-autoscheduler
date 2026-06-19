from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from tunarr_autoscheduler.core.auth import hash_password
from tunarr_autoscheduler.core.metrics import MetricsCollector
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.models.blocks import (
    AdBlock,
    EpisodeBlock,
    GenerationJob,
    JobStatus,
    OfflineBlock,
    StationIDBlock,
)
from tunarr_autoscheduler.models.config import (
    AdsConfig,
    AppConfig,
    AuthConfig,
    ChannelConfig,
    DayOfWeek,
    DaypartTemplate,
)
from tunarr_autoscheduler.models.playlist import Playlist, PlaylistCategory, PlaylistItem
from tunarr_autoscheduler.models.schedule import MediaCacheEntry
from tunarr_autoscheduler.recommendations.profiles import RecommendationProfile
from tunarr_autoscheduler.web.app import create_app
from tunarr_autoscheduler.web.routes import auth as auth_routes
from tunarr_autoscheduler.web.routes import public as public_routes
from tunarr_autoscheduler.web.routes import recommendations as recommendation_routes
from tunarr_autoscheduler.web.routes import settings as settings_routes
from tunarr_autoscheduler.web.routes.public import _merge_consecutive_epg_items


class ConfigManager:
    def __init__(self, channels: list[ChannelConfig]) -> None:
        self.config_path = "~/.tunarr/config.yaml"
        self._config = AppConfig(
            auth=AuthConfig(
                password_hash=hash_password("password123"),
                session_secret="test-secret",
            ),
            channels=channels,
        )
        self.saved = False

    def config(self):
        return self._config

    def auth_configured(self) -> bool:
        return bool(self._config.auth.password_hash)

    def save(self, config: AppConfig | None = None) -> None:
        if config is not None:
            self._config = config
        self.saved = True

    def load(self) -> AppConfig:
        self.saved = True
        return self._config


class JobManager:
    def __init__(self) -> None:
        self.started = False
        self.generation_mode = ""
        self.parent_version: int | None = None
        self.cancelled = False
        self.running = False

    def get_active_job(self, channel_id: str) -> None:
        if not self.running:
            return None
        return GenerationJob(
            id="active-job",
            channel_id=channel_id,
            status=JobStatus.RUNNING,
            started_at=datetime.now(tz=UTC),
            current_stage="validator",
        )

    def is_running(self, channel_id: str) -> bool:
        return self.running

    async def start_generation(
        self,
        channel: ChannelConfig,
        generation_mode: str = "fresh",
        parent_version: int | None = None,
    ) -> GenerationJob:
        if not channel.scheduling_enabled:
            raise ValueError(f"Channel {channel.id} has scheduling disabled")
        self.started = True
        self.generation_mode = generation_mode
        self.parent_version = parent_version
        return GenerationJob(
            id="job1",
            channel_id=channel.id,
            status=JobStatus.RUNNING,
            started_at=datetime.now(tz=UTC),
        )

    async def cancel_generation(self, channel_id: str) -> bool:
        if not self.running:
            return False
        self.cancelled = True
        self.running = False
        return True


class State:
    def __init__(self) -> None:
        timeline = Timeline()
        timeline.insert(EpisodeBlock(
            start_time=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
            end_time=datetime(2026, 5, 28, 12, 30, tzinfo=UTC),
            duration=timedelta(minutes=30),
            episode_id="episode-1",
            show_id="show-1",
            season_number=1,
            episode_number=1,
            runtime_seconds=1800,
            metadata={
                "title": "Pilot",
                "show_name": "Series One",
                "overview": "A first episode.",
            },
        ))
        self.versions: dict[tuple[str, int], dict[str, object]] = {
            ("ch1", 1): {
                "id": "version-1",
                "channel_id": "ch1",
                "version": 1,
                "status": "draft",
                "timeline_json": json.dumps(timeline.snapshot()),
                "created_at": "2026-05-28T12:00:00+00:00",
                "parent_version": None,
            },
            ("ch1", 2): {
                "id": "version-2",
                "channel_id": "ch1",
                "version": 2,
                "status": "approved",
                "timeline_json": json.dumps(timeline.snapshot()),
                "created_at": "2026-05-28T13:00:00+00:00",
                "parent_version": None,
            },
            ("ch1", 3): {
                "id": "version-3",
                "channel_id": "ch1",
                "version": 3,
                "status": "uploaded",
                "timeline_json": json.dumps(timeline.snapshot()),
                "created_at": "2026-05-28T14:00:00+00:00",
                "parent_version": None,
            },
        }
        self.upload_attempts: list[dict[str, object]] = []

    async def get_schedule_version(self, channel_id: str, version: int) -> str | None:
        row = self.versions.get((channel_id, version))
        return str(row["timeline_json"]) if row else None

    async def get_schedule_version_meta(
        self, channel_id: str, version: int,
    ) -> dict[str, object] | None:
        row = self.versions.get((channel_id, version))
        return dict(row) if row else None

    async def approve_version(
        self, channel_id: str, version: int, approved_by: str = "system",
    ) -> None:
        self.versions[(channel_id, version)]["status"] = "approved"

    async def set_schedule_status(self, channel_id: str, version: int, status: str) -> None:
        self.versions[(channel_id, version)]["status"] = status

    async def rollback_to_version(self, channel_id: str, target_version: int) -> int | None:
        row = self.versions.get((channel_id, target_version))
        if row is None:
            return None
        new_version = max(version for cid, version in self.versions if cid == channel_id) + 1
        self.versions[(channel_id, new_version)] = {
            **row,
            "id": f"version-{new_version}",
            "version": new_version,
            "status": "draft",
            "parent_version": target_version,
        }
        return new_version

    async def delete_schedule_version(self, channel_id: str, version: int) -> bool:
        return self.versions.pop((channel_id, version), None) is not None

    async def delete_schedule_versions(
        self,
        channel_id: str,
        versions: list[int],
        *,
        include_uploaded: bool = False,
    ) -> dict[str, int | list[int]]:
        deleted = 0
        skipped_uploaded: list[int] = []
        missing: list[int] = []
        for version in sorted(set(versions)):
            row = self.versions.get((channel_id, version))
            if row is None:
                missing.append(version)
                continue
            if row["status"] == "uploaded" and not include_uploaded:
                skipped_uploaded.append(version)
                continue
            del self.versions[(channel_id, version)]
            deleted += 1
        return {
            "deleted": deleted,
            "skipped_uploaded": skipped_uploaded,
            "missing": missing,
        }

    async def cleanup_schedule_versions(
        self,
        channel_id: str,
        *,
        keep_latest: int,
        include_uploaded: bool = False,
        statuses: list[str] | None = None,
    ) -> dict[str, int | list[int]]:
        rows = sorted(
            [
                (version, row)
                for (cid, version), row in self.versions.items()
                if cid == channel_id
            ],
            reverse=True,
        )
        keep = {version for version, _ in rows[:keep_latest]}
        allowed = set(statuses or [])
        candidates = [
            version
            for version, row in rows
            if version not in keep and (not allowed or str(row["status"]) in allowed)
        ]
        return await self.delete_schedule_versions(
            channel_id,
            candidates,
            include_uploaded=include_uploaded,
        )

    async def record_upload_attempt(
        self,
        channel_id: str,
        version: int,
        status: str,
        message: str = "",
        details: dict[str, object] | None = None,
    ) -> str:
        attempt = {
            "id": f"attempt-{len(self.upload_attempts) + 1}",
            "channel_id": channel_id,
            "schedule_version": version,
            "status": status,
            "message": message,
            "details": details or {},
            "details_json": json.dumps(details or {}),
            "created_at": "2026-05-28T15:00:00+00:00",
        }
        self.upload_attempts.insert(0, attempt)
        return str(attempt["id"])

    async def list_upload_attempts(
        self,
        channel_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        attempts = [
            attempt for attempt in self.upload_attempts
            if channel_id is None or attempt["channel_id"] == channel_id
        ]
        return [dict(attempt) for attempt in attempts[:limit]]

    async def list_versions(self, channel_id: str) -> list[dict[str, object]]:
        versions = []
        for (cid, _), row in self.versions.items():
            if cid != channel_id:
                continue
            item = dict(row)
            item.setdefault("planned_start", "2026-05-28T12:00:00+00:00")
            item.setdefault("planned_end", "2026-05-28T12:30:00+00:00")
            versions.append(item)
        return sorted(versions, key=lambda item: int(item["version"]), reverse=True)

    async def list_public_epg_versions(self) -> list[dict[str, object]]:
        return [
            dict(row)
            for row in self.versions.values()
            if row["status"] == "uploaded"
        ]

    async def list_recent_jobs(self, channel_id: str, limit: int = 10) -> list[dict[str, object]]:
        return [{
            "id": "job1",
            "channel_id": channel_id,
            "status": "completed",
            "current_stage": "completed",
            "schedule_version_id": "version-2",
            "schedule_version": 2,
            "started_at": "2026-05-28T13:10:00+00:00",
            "error_message": None,
        }]

    async def get_follow_up_context(
        self, channel_id: str, parent_version: int | None = None,
    ) -> dict[str, object] | None:
        versions = [
            row
            for (cid, _), row in self.versions.items()
            if cid == channel_id and row["status"] in {"draft", "approved", "uploaded"}
        ]
        if not versions:
            return None
        return {
            "version": 3,
            "end_time": datetime(2026, 5, 28, 12, 30, tzinfo=UTC),
            "planned_start": datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
            "planned_end": datetime(2026, 5, 28, 12, 30, tzinfo=UTC),
            "chain_versions": [1, 2, 3],
            "gaps": [],
            "episode_ids": {"episode-1"},
            "movie_ids": set(),
        }


class TunarrClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, dict]] = []
        self.fail_upload = False
        self.fail_upload_text = "Not Found"

    async def get_custom_shows(self) -> list[dict[str, object]]:
        return [
            {"id": "custom-morning", "name": "Morning Shows"},
            {"id": "station-list", "name": "Station IDs"},
        ]

    async def get_filler_lists(self) -> list[dict[str, object]]:
        return [{"id": "filler-1", "name": "Ads"}]

    async def get_filler_list_programs(self, filler_list_id: str) -> list[dict[str, object]]:
        return [
            {"id": "ad-1", "title": "Short Ad", "duration": 30_000},
            {"id": "ad-2", "title": "Long Ad", "duration": 120_000},
        ]

    async def upload_schedule(self, channel_id: str, schedule_data: dict) -> dict:
        if self.fail_upload:
            request = httpx.Request("POST", "http://tunarr.test/api/channels/ch1/schedule")
            response = httpx.Response(404, text=self.fail_upload_text, request=request)
            raise httpx.HTTPStatusError("Not Found", request=request, response=response)
        self.uploads.append((channel_id, schedule_data))
        return {"ok": True}

    async def upload_timeline(
        self,
        channel_id: str,
        timeline: Timeline,
        **kwargs,
    ) -> dict:
        if self.fail_upload:
            request = httpx.Request("POST", "http://tunarr.test/api/channels/ch1/programming")
            response = httpx.Response(404, text=self.fail_upload_text, request=request)
            raise httpx.HTTPStatusError("Not Found", request=request, response=response)
        self.uploads.append((channel_id, {"timeline": timeline, **kwargs}))
        return {
            "ok": True,
            "_upload": {
                "mode": "manual",
                "persistent_time_status": "not_attempted",
                "programming_status": 200,
                "channel_update_status": 200,
                "verification_status": 200,
                "final_status": 200,
                "duration_ms": 1_800_000,
                "lineup_items": 1,
                "content_items": 1,
                "fallback_used": False,
            },
        }

    async def check_connection(self) -> bool:
        return True


class JellyfinClient:
    async def check_connection(self) -> bool:
        return True


class Database:
    async def fetch_one(self, sql: str, *params) -> dict:
        return {"ok": 1}


class MediaSync:
    def __init__(self) -> None:
        self.synced = False
        self.targeted_item_id: str | None = None
        self.targeted_event_name: str | None = None

    async def sync_now(self) -> dict:
        self.synced = True
        return {"new_episodes": 1, "new_movies": 2, "removed_items": 0}

    async def sync_item(self, item_id: str, event_name: str | None = None) -> dict:
        self.targeted_item_id = item_id
        self.targeted_event_name = event_name
        return {
            "status": "updated",
            "item_id": item_id,
            "new_episodes": 0,
            "new_movies": 0,
            "updated_items": 1,
            "removed_items": 0,
            "ignored_items": 0,
        }


class MediaRepository:
    async def get_all_available(self) -> list[MediaCacheEntry]:
        return [
            MediaCacheEntry(
                id="episode-1",
                item_type="episode",
                source_type="jellyfin",
                source_id="episode-1",
                title="Pilot",
                duration_seconds=1800,
                metadata={
                    "series_id": "series-1",
                    "series_name": "Series One",
                    "genres": ["Sci-Fi"],
                    "tags": ["space"],
                },
            ),
            MediaCacheEntry(
                id="movie-1",
                item_type="movie",
                source_type="jellyfin",
                source_id="movie-1",
                title="Movie One",
                duration_seconds=5400,
                metadata={"year": 2025, "genres": ["Sci-Fi"], "tags": ["space"]},
            ),
        ]

    async def get_playlist_options(self) -> list[dict[str, str]]:
        return [
            {
                "key": "series:series-1",
                "media_type": "series",
                "media_id": "series-1",
                "title": "Series One",
                "details": "1 episodes",
            },
            {
                "key": "movie:movie-1",
                "media_type": "movie",
                "media_id": "movie-1",
                "title": "Movie One",
                "details": "2025",
            },
        ]


class AuditRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def record(
        self,
        action: str,
        *,
        actor: str = "admin",
        source: str = "web",
        status: str = "success",
        channel_id: str = "",
        schedule_version: int | None = None,
        target_type: str = "",
        target_id: str = "",
        message: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        self.events.insert(0, {
            "id": f"audit-{len(self.events) + 1}",
            "action": action,
            "actor": actor,
            "source": source,
            "status": status,
            "channel_id": channel_id,
            "schedule_version": schedule_version,
            "target_type": target_type,
            "target_id": target_id,
            "message": message,
            "details": details or {},
            "created_at": "2026-05-28T16:00:00+00:00",
        })

    async def list_events(
        self,
        *,
        channel_id: str = "",
        action: str = "",
        status: str = "",
        limit: int = 200,
    ) -> list[dict[str, object]]:
        events = [
            event for event in self.events
            if (not channel_id or event["channel_id"] == channel_id)
            and (not action or event["action"] == action)
            and (not status or event["status"] == status)
        ]
        return [dict(event) for event in events[:limit]]


class PlaylistRepository:
    def __init__(self) -> None:
        self.playlists: dict[str, Playlist] = {}
        self.categories: dict[str, PlaylistCategory] = {}

    async def list_all(self) -> list[Playlist]:
        return list(self.playlists.values())

    async def list_categories(self) -> list[PlaylistCategory]:
        return list(self.categories.values())

    async def list_tags(self) -> list[str]:
        return sorted({tag for playlist in self.playlists.values() for tag in playlist.tags})

    async def get_recommendation_terms_by_media_id(self) -> dict[str, list[str]]:
        terms: dict[str, list[str]] = {}
        for playlist in self.playlists.values():
            playlist_terms = [
                playlist.name,
                playlist.description,
                playlist.category_name,
                *playlist.tags,
            ]
            for item in playlist.items:
                terms.setdefault(item.media_id, []).extend(
                    term for term in playlist_terms if term
                )
        return terms

    async def get(self, playlist_id: str) -> Playlist | None:
        return self.playlists.get(playlist_id)

    async def create(
        self,
        name: str,
        description: str = "",
        items: list[PlaylistItem] | None = None,
        category_id: str = "",
        channel_scope: str = "",
        tags: list[str] | None = None,
    ) -> Playlist:
        now = datetime.now(tz=UTC)
        playlist = Playlist(
            id=f"playlist-{len(self.playlists) + 1}",
            name=name,
            description=description,
            category_id=category_id,
            category_name=self.categories[category_id].name if category_id else "",
            channel_scope=channel_scope,
            tags=_normalize_test_tags(tags or []),
            items=items or [],
            created_at=now,
            updated_at=now,
        )
        self.playlists[playlist.id] = playlist
        return playlist

    async def update(
        self,
        playlist_id: str,
        name: str,
        description: str,
        items: list[PlaylistItem],
        category_id: str = "",
        channel_scope: str = "",
        tags: list[str] | None = None,
    ) -> Playlist | None:
        existing = self.playlists.get(playlist_id)
        if existing is None:
            return None
        updated = existing.model_copy(update={
            "name": name,
            "description": description,
            "category_id": category_id,
            "category_name": self.categories[category_id].name if category_id else "",
            "channel_scope": channel_scope,
            "tags": _normalize_test_tags(tags or []),
            "items": items,
            "updated_at": datetime.now(tz=UTC),
        })
        self.playlists[playlist_id] = updated
        return updated

    async def delete(self, playlist_id: str) -> bool:
        return self.playlists.pop(playlist_id, None) is not None

    async def create_category(
        self, name: str, description: str = "",
    ) -> PlaylistCategory:
        now = datetime.now(tz=UTC)
        category = PlaylistCategory(
            id=f"category-{len(self.categories) + 1}",
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
        )
        self.categories[category.id] = category
        return category

    async def update_category(
        self, category_id: str, name: str, description: str = "",
    ) -> PlaylistCategory | None:
        existing = self.categories.get(category_id)
        if existing is None:
            return None
        updated = existing.model_copy(update={
            "name": name,
            "description": description,
            "updated_at": datetime.now(tz=UTC),
        })
        self.categories[category_id] = updated
        for playlist_id, playlist in list(self.playlists.items()):
            if playlist.category_id == category_id:
                self.playlists[playlist_id] = playlist.model_copy(
                    update={"category_name": name},
                )
        return updated

    async def delete_category(self, category_id: str) -> bool:
        if category_id not in self.categories:
            return False
        del self.categories[category_id]
        for playlist_id, playlist in list(self.playlists.items()):
            if playlist.category_id == category_id:
                self.playlists[playlist_id] = playlist.model_copy(
                    update={"category_id": "", "category_name": ""},
                )
        return True


class RecommendationProfileRepository:
    def __init__(self) -> None:
        self.profiles: dict[str, RecommendationProfile] = {}

    async def list_all(self) -> list[RecommendationProfile]:
        return list(self.profiles.values())

    async def get(self, profile_id: str) -> RecommendationProfile | None:
        return self.profiles.get(profile_id)

    async def save(self, profile: RecommendationProfile) -> RecommendationProfile:
        self.profiles[profile.id] = profile
        return profile

    async def delete(self, profile_id: str) -> bool:
        return self.profiles.pop(profile_id, None) is not None


class RecommendationRunRepository:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, object]] = {}

    async def create(
        self,
        *,
        run_type: str,
        title: str,
        request: dict[str, object],
        result: dict[str, object],
        status: str = "draft",
    ) -> dict[str, object]:
        run = {
            "id": f"run-{len(self.runs) + 1}",
            "run_type": run_type,
            "title": title,
            "status": status,
            "request": request,
            "result": result,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "applied_at": "",
        }
        self.runs[str(run["id"])] = run
        return run

    async def list_recent(self, limit: int = 50) -> list[dict[str, object]]:
        return list(self.runs.values())[:limit]

    async def get(self, run_id: str) -> dict[str, object] | None:
        return self.runs.get(run_id)

    async def mark_applied(self, run_id: str) -> None:
        self.runs[run_id]["status"] = "applied"
        self.runs[run_id]["applied_at"] = datetime.now(tz=UTC).isoformat()


def _normalize_test_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = " ".join(raw.strip().lower().split())
        if not tag or tag in seen:
            continue
        normalized.append(tag)
        seen.add(tag)
    return normalized


class Core:
    def __init__(self) -> None:
        self.config_manager = ConfigManager([
            ChannelConfig(id="ch1", name="Channel", scheduling_enabled=True),
        ])
        self.job_manager = JobManager()
        self.state = State()
        self.tunarr_client = TunarrClient()
        self.jellyfin_client = JellyfinClient()
        self.db = Database()
        self.metrics = MetricsCollector()
        self.media_sync = MediaSync()
        self.media_repo = MediaRepository()
        self.playlist_repo = PlaylistRepository()
        self.recommendation_profile_repo = RecommendationProfileRepository()
        self.recommendation_run_repo = RecommendationRunRepository()
        self.audit_repo = AuditRepository()
        self.channel_sync_engine = None


class SetupJellyfinClient:
    checked = False

    def __init__(self, base_url: str, api_key: str, user_id: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.user_id = user_id

    async def check_connection(self) -> bool:
        type(self).checked = True
        return self.api_key == "jf-key" and self.user_id == "jf-user"

    async def close(self) -> None:
        return None


class SetupTunarrClient:
    checked = False

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    async def check_connection(self) -> bool:
        type(self).checked = True
        return self.base_url == "http://tunarr.local"

    async def close(self) -> None:
        return None


def make_client(core: Core) -> TestClient:
    app = create_app(core)
    client = TestClient(app)
    response = client.post(
        "/login",
        data={"username": "admin", "password": "password123"},
    )
    assert response.status_code == 200
    return client


def test_audit_log_page_filters_events() -> None:
    core = Core()
    client = make_client(core)
    core.audit_repo.events.append({
        "id": "audit-1",
        "action": "schedule.upload",
        "actor": "admin",
        "source": "web",
        "status": "success",
        "channel_id": "ch1",
        "schedule_version": 3,
        "target_type": "",
        "target_id": "",
        "message": "Uploaded schedule version 3",
        "details": {"mode": "manual"},
        "created_at": "2026-05-28T16:00:00+00:00",
    })

    response = client.get("/audit?action=schedule.upload&status=success")

    assert response.status_code == 200
    assert "Audit Log" in response.text
    assert "schedule.upload" in response.text
    assert "Uploaded schedule version 3" in response.text
    assert "manual" in response.text


def test_settings_update_records_audit_event() -> None:
    core = Core()
    client = make_client(core)

    response = client.post(
        "/settings",
        data={
            "timezone": "Europe/Berlin",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert core.audit_repo.events[0]["action"] == "settings.update"
    assert core.audit_repo.events[0]["status"] == "success"
    assert core.audit_repo.events[0]["target_type"] == "settings"


def test_public_epg_does_not_require_login_and_has_no_app_nav() -> None:
    core = Core()
    timeline = Timeline()
    now = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    timeline.insert(EpisodeBlock(
        start_time=now - timedelta(minutes=5),
        end_time=now + timedelta(minutes=25),
        duration=timedelta(minutes=30),
        episode_id="episode-1",
        show_id="show-1",
        season_number=1,
        episode_number=1,
        runtime_seconds=1800,
        metadata={
            "title": "Pilot",
            "show_name": "Series One",
            "overview": "A first episode.",
        },
    ))
    timeline.insert(StationIDBlock(
        start_time=now + timedelta(minutes=25),
        end_time=now + timedelta(minutes=26),
        duration=timedelta(minutes=1),
        clip_id="station-1",
        metadata={"title": "Station ID"},
    ))
    timeline.insert(AdBlock(
        start_time=now + timedelta(minutes=26),
        end_time=now + timedelta(minutes=29),
        duration=timedelta(minutes=3),
        ad_count=4,
        total_duration_seconds=180,
        metadata={"title": "Ad Break"},
    ))
    timeline.insert(EpisodeBlock(
        start_time=now + timedelta(minutes=35),
        end_time=now + timedelta(minutes=65),
        duration=timedelta(minutes=30),
        episode_id="episode-2",
        show_id="show-1",
        season_number=1,
        episode_number=2,
        runtime_seconds=1800,
        metadata={
            "title": "Second",
            "show_name": "Series One",
            "overview": "The next episode.",
            "genres": ["Drama", "Sci-Fi"],
            "imdb_id": "tt1234567",
            "image_url": "https://img.example/episode.jpg",
        },
    ))
    core.state.versions[("ch1", 3)]["timeline_json"] = json.dumps(timeline.snapshot())
    core.config_manager.config().channels[0].public_epg_logo_url = "https://img.example/logo.png"
    core.config_manager.config().channels.append(ChannelConfig(
        id="ch2",
        name="No Schedule",
        public_epg_enabled=True,
        public_epg_order=0,
    ))
    client = TestClient(create_app(core))

    response = client.get("/epg?view=week")

    assert response.status_code == 200
    assert "FlixWolf EPG" in response.text
    assert "Series One" in response.text
    assert "No Schedule" in response.text
    assert "No public schedule uploaded for this channel yet." in response.text
    assert "S01E01 Pilot" in response.text
    assert "S01E02 Second" in response.text
    assert "https://img.example/logo.png" in response.text
    assert "https://img.example/episode.jpg" in response.text
    assert "https://www.imdb.com/title/tt1234567/" in response.text
    assert "Station ID" not in response.text
    assert "Ad Break" not in response.text
    assert "Version 3" not in response.text
    assert "Version 3 / uploaded" not in response.text
    assert "epg-channel-filter" in response.text
    assert "epg-timeline" in response.text
    assert "epg-now-tick" in response.text
    assert "epg-live-clock" in response.text
    assert "setInterval" in response.text
    assert "Up next" in response.text
    assert "data-view=\"week\"" in response.text
    assert "runtime_label" not in response.text
    assert "Previous week" in response.text
    assert "Day" in response.text
    assert "Week" in response.text
    assert "Dashboard" not in response.text
    assert "/channels/" not in response.text


def test_public_epg_search_filters_programs_and_preserves_exports() -> None:
    core = Core()
    timeline = Timeline()
    now = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    timeline.insert(EpisodeBlock(
        start_time=now + timedelta(minutes=5),
        end_time=now + timedelta(minutes=35),
        duration=timedelta(minutes=30),
        episode_id="episode-1",
        show_id="show-1",
        season_number=1,
        episode_number=1,
        runtime_seconds=1800,
        metadata={"title": "Pilot", "show_name": "Series One"},
    ))
    timeline.insert(EpisodeBlock(
        start_time=now + timedelta(minutes=40),
        end_time=now + timedelta(minutes=70),
        duration=timedelta(minutes=30),
        episode_id="episode-2",
        show_id="show-1",
        season_number=1,
        episode_number=2,
        runtime_seconds=1800,
        metadata={
            "title": "Second",
            "show_name": "Series One",
            "overview": "A sci-fi follow-up.",
            "genres": ["Sci-Fi"],
        },
    ))
    core.state.versions[("ch1", 3)]["timeline_json"] = json.dumps(timeline.snapshot())
    client = TestClient(create_app(core))

    response = client.get("/epg", params={"q": "sci-fi"})

    assert response.status_code == 200
    assert "Second" in response.text
    assert "Pilot" not in response.text
    assert 'name="q" value="sci-fi"' in response.text
    assert "/public/epg.json" in response.text
    assert "q=sci-fi" in response.text


def test_public_epg_compact_week_view_groups_days_and_preserves_exports() -> None:
    core = Core()
    client = TestClient(create_app(core))

    response = client.get("/epg?view=week&compact=1&date=2026-05-28")

    assert response.status_code == 200
    assert "Compact guide by channel" in response.text
    assert "Next 7 Days" in response.text
    assert "epg-compact-week" in response.text
    assert "epg-timeline" not in response.text
    assert "compact=1" in response.text
    assert "/public/epg.json?view=week&amp;period=day&amp;compact=1" in response.text


def test_public_epg_json_includes_compact_day_groups() -> None:
    core = Core()
    client = TestClient(create_app(core))

    response = client.get("/public/epg.json?view=week&compact=1&date=2026-05-28")

    assert response.status_code == 200
    payload = response.json()
    assert payload["compact"] is True
    assert payload["channels"][0]["days"]
    assert payload["channels"][0]["days"][0]["count"] >= 1
    assert payload["channels"][0]["days"][0]["prime"]["title"] == "Series One"


def test_public_epg_can_be_disabled() -> None:
    core = Core()
    core.config_manager.config().public_access.epg = "disabled"
    client = TestClient(create_app(core))

    response = client.get("/epg")

    assert response.status_code == 404
    assert "Public EPG is disabled" in response.text


def test_public_epg_can_require_jellyfin_login() -> None:
    core = Core()
    core.config_manager.config().public_access.epg = "jellyfin_login"
    client = TestClient(create_app(core))

    response = client.get("/epg", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/public/login?return_to=/epg"


def test_public_epg_json_requires_jellyfin_login_without_redirect() -> None:
    core = Core()
    core.config_manager.config().public_access.epg = "jellyfin_login"
    client = TestClient(create_app(core))

    response = client.get("/public/epg.json", follow_redirects=False)

    assert response.status_code == 401
    assert response.json()["error"] == "Jellyfin login required"


def test_public_epg_jellyfin_login_allows_public_guide(monkeypatch) -> None:
    core = Core()
    core.config_manager.config().public_access.epg = "jellyfin_login"

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"User": {"Id": "jellyfin-user"}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, **kwargs) -> FakeResponse:
            assert url == "/Users/AuthenticateByName"
            assert kwargs["json"] == {"Username": "viewer", "Pw": "secret"}
            return FakeResponse()

    monkeypatch.setattr(auth_routes.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(core))

    login = client.post(
        "/public/login",
        data={"username": "viewer", "password": "secret", "return_to": "/epg"},
        follow_redirects=False,
    )
    response = client.get("/epg")

    assert login.status_code == 303
    assert login.headers["location"] == "/epg"
    assert response.status_code == 200
    assert "FlixWolf EPG" in response.text


def test_public_epg_jellyfin_login_rejects_external_return_to(monkeypatch) -> None:
    core = Core()
    core.config_manager.config().public_access.epg = "jellyfin_login"

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"User": {"Id": "jellyfin-user"}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, **kwargs) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(auth_routes.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(core))

    login = client.post(
        "/public/login",
        data={
            "username": "viewer",
            "password": "secret",
            "return_to": "https://evil.example/steal",
        },
        follow_redirects=False,
    )

    assert login.status_code == 303
    assert login.headers["location"] == "/epg"


def test_channel_health_dashboard_shows_status_and_links() -> None:
    core = Core()
    core.state.upload_attempts.append({
        "id": "attempt-1",
        "channel_id": "ch1",
        "schedule_version": 3,
        "status": "success",
        "message": "Uploaded schedule version.",
        "details": {"mode": "manual"},
        "created_at": "2026-05-28T15:00:00+00:00",
    })
    client = make_client(core)

    response = client.get("/channel-health")

    assert response.status_code == 200
    assert "Channel Health" in response.text
    assert "Database" in response.text
    assert "Jellyfin" in response.text
    assert "Tunarr" in response.text
    assert "v3" in response.text
    assert "/channels/ch1#schedule-health" in response.text
    assert "/schedules/ch1/preview/3" in response.text
    assert "/uploads?channel_id=ch1" in response.text
    assert "/channels/ch1/config?return_to=/channel-health" in response.text
    assert "/diagnostics/bundle" in response.text


def test_diagnostic_bundle_download_redacts_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    core = Core()
    core.config_manager.config().jellyfin.api_key = "secret-key"
    core.config_manager.config().auth.session_secret = "secret-session"
    client = make_client(core)

    response = client.post("/diagnostics/bundle")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive_path = tmp_path / "bundle.zip"
    archive_path.write_bytes(response.content)
    with zipfile.ZipFile(archive_path) as zf:
        assert "config.redacted.json" in zf.namelist()
        redacted = zf.read("config.redacted.json").decode()
        assert "secret-key" not in redacted
        assert "secret-session" not in redacted
        assert "***REDACTED***" in redacted


def test_public_epg_export_inlines_css_and_keeps_export_navigation() -> None:
    core = Core()
    client = TestClient(create_app(core))

    response = client.get("/public/epg/export?view=day&date=2026-05-28")

    assert response.status_code == 200
    assert "<style>" in response.text
    assert '<link href="/static/app.css"' not in response.text
    assert 'action="/public/epg/export"' in response.text
    assert "/public/epg/export?view=week" in response.text
    assert "/public/epg.json" in response.text
    assert "/public/epg.xml" in response.text
    assert "http://testserver/public/epg/images/episode-1" in response.text
    assert "Dashboard" not in response.text


def test_public_epg_json_export_is_stable_and_read_only() -> None:
    core = Core()
    client = TestClient(create_app(core))

    response = client.get("/public/epg.json?date=2026-05-28")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "flixwolf.public_epg.v1"
    assert payload["channels"][0]["id"] == "ch1"
    assert payload["channels"][0]["programs"][0]["title"] == "Series One"
    assert payload["channels"][0]["programs"][0]["start"].startswith("2026-05-28")
    assert response.headers["cache-control"] == "public, max-age=60"


def test_public_epg_xmltv_export() -> None:
    core = Core()
    client = TestClient(create_app(core))

    response = client.get("/public/epg.xml?date=2026-05-28")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert '<tv generator-info-name="FlixWolf Tunarr AutoScheduler">' in response.text
    assert '<channel id="ch1">' in response.text
    assert "<display-name>Channel</display-name>" in response.text
    assert "<programme" in response.text
    assert "<title>Series One</title>" in response.text


def test_public_epg_uses_optional_tmdb_enrichment(monkeypatch) -> None:
    core = Core()
    core.config_manager.config().metadata.tmdb_enabled = True
    core.config_manager.config().metadata.tmdb_api_key = "tmdb-key"

    async def fake_fetch_tmdb_metadata(**kwargs):
        assert kwargs["api_key"] == "tmdb-key"
        return {
            "tmdb_id": "123",
            "image_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
            "overview": "External overview.",
            "year": "2026",
        }

    monkeypatch.setattr(
        public_routes,
        "_fetch_tmdb_metadata",
        fake_fetch_tmdb_metadata,
    )
    client = TestClient(create_app(core))

    response = client.get("/epg?date=2026-05-28")

    assert response.status_code == 200
    assert "https://image.tmdb.org/t/p/w500/poster.jpg" in response.text
    assert "https://www.themoviedb.org/tv/123" in response.text


def test_public_epg_hides_channels_disabled_for_public_guide() -> None:
    core = Core()
    core.config_manager.config().channels[0].public_epg_enabled = False
    client = TestClient(create_app(core))

    response = client.get("/epg")

    assert response.status_code == 200
    assert "No public schedule available" in response.text
    assert "Series One" not in response.text
    assert "<option value=\"ch1\"" not in response.text


def test_public_epg_merges_consecutive_offline_blocks() -> None:
    now = datetime.now(tz=UTC)
    items = [
        {
            "type": "offline",
            "kind": "Off-Air",
            "title": "Standby Loop",
            "subtitle": "Night",
            "merge_key": "offline:standby",
            "image_url": "",
            "start": now,
            "end": now + timedelta(minutes=30),
            "time": "",
            "duration_minutes": 30,
            "runtime_label": "30m",
            "is_current": True,
            "starts_after_now": False,
            "progress_percent": 0,
            "ends_label": "",
        },
        {
            "type": "offline",
            "kind": "Off-Air",
            "title": "Off-Air",
            "subtitle": "Standby",
            "merge_key": "offline:standby",
            "image_url": "",
            "start": now + timedelta(minutes=30),
            "end": now + timedelta(minutes=90),
            "time": "",
            "duration_minutes": 60,
            "runtime_label": "60m",
            "is_current": False,
            "starts_after_now": True,
            "progress_percent": 0,
            "ends_label": "",
        },
    ]

    merged = _merge_consecutive_epg_items(items, now + timedelta(minutes=45))

    assert len(merged) == 1
    assert merged[0]["runtime_label"] == "90m"
    assert merged[0]["time"] == f"{now:%H:%M} - {(now + timedelta(minutes=90)):%H:%M}"
    assert merged[0]["is_current"] is True


def test_channel_config_can_toggle_public_epg_visibility() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/channels/ch1/config", data={
        "return_to": "/channels/ch1",
        "name": "Channel",
        "scheduling_enabled": "true",
        "public_epg_submitted": "1",
        "public_epg_order": "7",
        "public_epg_logo_url": "https://img.example/channel.png",
        "schedule_horizon_days": "1",
        "standby_custom_show_id": "",
        "daypart_indices": "",
        "rotations_yaml": "[]",
        "ads_enabled": "on",
        "ads_filler_list_id": "",
        "ads_break_after_programs": "1",
        "ads_min_total_minutes": "0",
        "ads_max_total_minutes": "0",
        "ads_ad_density": "0.08",
        "ads_min_ad_break_duration_minutes": "1",
        "ads_max_ad_break_duration_minutes": "5",
        "continuity_enabled": "on",
        "continuity_frequency": "4",
        "continuity_station_id_custom_show_id": "",
        "continuity_bumper_custom_show_id": "",
        "continuity_station_id_clip_ids": "",
        "continuity_bumper_clip_ids": "",
        "pipeline_text": "\n".join(core.config_manager.config().channels[0].pipeline),
    }, follow_redirects=False)

    assert response.status_code == 303
    assert core.config_manager.config().channels[0].public_epg_enabled is False
    assert core.config_manager.config().channels[0].public_epg_order == 7
    assert core.config_manager.config().channels[0].public_epg_logo_url == (
        "https://img.example/channel.png"
    )


def test_channel_config_export_downloads_json() -> None:
    core = Core()
    client = make_client(core)

    response = client.get("/channels/ch1/config/export")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        'attachment; filename="Channel-channel-config.json"'
    )
    payload = response.json()
    assert payload["schema"] == "tunarr_autoscheduler.channel_config.v1"
    assert payload["channel"]["id"] == "ch1"
    assert payload["channel"]["name"] == "Channel"


def test_channel_config_import_preserves_channel_id_and_saves() -> None:
    core = Core()
    payload = {
        "schema": "tunarr_autoscheduler.channel_config.v1",
        "channel": core.config_manager.config().channels[0].model_dump(mode="json"),
    }
    payload["channel"]["id"] = "other-channel"
    payload["channel"]["name"] = "Imported Channel"
    payload["channel"]["scheduling_enabled"] = False
    client = make_client(core)

    response = client.post(
        "/channels/ch1/config/import",
        data={
            "import_json": json.dumps(payload),
            "return_to": "/channels/ch1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    saved = core.config_manager.config().channels[0]
    assert saved.id == "ch1"
    assert saved.name == "Imported Channel"
    assert saved.scheduling_enabled is False
    assert core.config_manager.saved is True
    assert "imported=1" in response.headers["location"]


def test_channel_config_import_rejects_invalid_json() -> None:
    core = Core()
    client = make_client(core)

    response = client.post(
        "/channels/ch1/config/import",
        data={
            "import_json": "{not-json",
            "return_to": "/channels/ch1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert core.config_manager.config().channels[0].name == "Channel"
    assert "import_error=invalid" in response.headers["location"]
    assert "https://evil.example" not in response.headers["location"]


def test_channel_config_save_rejects_external_return_to() -> None:
    core = Core()
    client = make_client(core)

    response = client.post(
        "/channels/ch1/config",
        data={
            "name": "Channel",
            "schedule_horizon_days": "1",
            "return_to": "https://evil.example/phish",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?saved=1"


def test_settings_save_metadata_config() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/settings", data={
        "timezone": "Europe/Berlin",
        "metadata_submitted": "1",
        "metadata_tmdb_enabled": "on",
        "metadata_tmdb_api_key": "tmdb-key",
        "metadata_tmdb_language": "en-US",
        "metadata_tmdb_rate_limit_per_minute": "90",
        "metadata_tvdb_enabled": "on",
        "metadata_tvdb_api_key": "tvdb-key",
        "metadata_tvdb_rate_limit_per_minute": "45",
        "metadata_omdb_enabled": "on",
        "metadata_omdb_api_key": "omdb-key",
        "metadata_omdb_rate_limit_per_minute": "30",
        "metadata_jellystat_enabled": "on",
        "metadata_jellystat_url": "http://jellystat:3000",
        "metadata_jellystat_api_token": "js-token",
        "metadata_jellystat_days": "120",
        "metadata_jellystat_activity_weight": "14",
        "metadata_jellystat_completion_weight": "9",
        "metadata_jellystat_trend_weight": "7",
        "metadata_jellystat_genre_trend_weight": "11",
        "metadata_jellystat_underused_weight": "5",
        "metadata_jellystat_stale_weight": "3",
        "metadata_jellystat_rate_limit_per_minute": "20",
        "metadata_cache_ttl_days": "21",
    }, follow_redirects=False)

    assert response.status_code == 303
    assert core.config_manager.config().metadata.tmdb_enabled is True
    assert core.config_manager.config().metadata.tmdb_api_key == "tmdb-key"
    assert core.config_manager.config().metadata.tmdb_language == "en-US"
    assert core.config_manager.config().metadata.tmdb_rate_limit_per_minute == 90
    assert core.config_manager.config().metadata.tvdb_enabled is True
    assert core.config_manager.config().metadata.tvdb_api_key == "tvdb-key"
    assert core.config_manager.config().metadata.tvdb_rate_limit_per_minute == 45
    assert core.config_manager.config().metadata.omdb_enabled is True
    assert core.config_manager.config().metadata.omdb_api_key == "omdb-key"
    assert core.config_manager.config().metadata.omdb_rate_limit_per_minute == 30
    assert core.config_manager.config().metadata.jellystat_enabled is True
    assert core.config_manager.config().metadata.jellystat_url == "http://jellystat:3000"
    assert core.config_manager.config().metadata.jellystat_api_token == "js-token"
    assert core.config_manager.config().metadata.jellystat_days == 120
    assert core.config_manager.config().metadata.jellystat_activity_weight == 14
    assert core.config_manager.config().metadata.jellystat_completion_weight == 9
    assert core.config_manager.config().metadata.jellystat_trend_weight == 7
    assert core.config_manager.config().metadata.jellystat_genre_trend_weight == 11
    assert core.config_manager.config().metadata.jellystat_underused_weight == 5
    assert core.config_manager.config().metadata.jellystat_stale_weight == 3
    assert core.config_manager.config().metadata.jellystat_rate_limit_per_minute == 20
    assert core.config_manager.config().metadata.cache_ttl_days == 21
    assert core.config_manager.saved is True


def test_settings_save_backup_config() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/settings", data={
        "timezone": "Europe/Berlin",
        "backups_submitted": "1",
        "backups_enabled": "on",
        "backups_interval_hours": "12",
        "backups_output_dir": "/data/backups",
        "backups_retention_count": "5",
        "backups_min_free_mb": "2048",
        "backups_size_multiplier": "4",
    }, follow_redirects=False)

    backups = core.config_manager.config().backups
    assert response.status_code == 303
    assert backups.enabled is True
    assert backups.interval_hours == 12
    assert backups.output_dir == "/data/backups"
    assert backups.retention_count == 5
    assert backups.min_free_mb == 2048
    assert backups.size_multiplier == 4
    assert core.config_manager.saved is True


def test_settings_lists_downloads_deletes_and_restores_backups(tmp_path) -> None:
    core = Core()
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "scheduler.db"
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    config_path.write_text("timezone: UTC\n", encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    core.config_manager.config_path = str(config_path)
    core.config_manager.config().database.url = f"sqlite+aiosqlite:///{db_path}"
    core.config_manager.config().backups.output_dir = str(backups_dir)
    archive = backups_dir / "tunarr-autoscheduler-backup-test.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", "{}")
        zf.writestr("config.yaml", "timezone: Europe/Berlin\n")
        zf.writestr("scheduler.db", b"new-db")
    client = make_client(core)

    page = client.get("/settings")
    assert page.status_code == 200
    assert archive.name in page.text
    download = client.get(f"/settings/backups/{archive.name}/download")
    assert download.status_code == 200
    restore = client.post(
        "/settings/backups/restore",
        data={"backup_name": archive.name},
        follow_redirects=False,
    )
    assert restore.status_code == 303
    assert config_path.read_text(encoding="utf-8") == "timezone: Europe/Berlin\n"
    assert db_path.read_bytes() == b"new-db"
    delete = client.post(f"/settings/backups/{archive.name}/delete", follow_redirects=False)
    assert delete.status_code == 303
    assert not archive.exists()


def test_settings_save_public_access_config() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/settings", data={
        "timezone": "Europe/Berlin",
        "public_access_submitted": "1",
        "public_epg_access": "jellyfin_login",
    }, follow_redirects=False)

    assert response.status_code == 303
    assert core.config_manager.config().public_access.epg == "jellyfin_login"
    assert core.config_manager.saved is True


def test_settings_save_notification_smtp_security() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/settings", data={
        "timezone": "Europe/Berlin",
        "notifications_submitted": "1",
        "notifications_enabled": "on",
        "email_enabled": "on",
        "email_smtp_host": "smtp.example.test",
        "email_smtp_port": "465",
        "email_username": "mailer",
        "email_password": "secret",
        "email_from_address": "scheduler@example.test",
        "email_to_addresses": "ops@example.test",
        "email_smtp_security": "ssl",
        "webhook_headers_json": "{}",
    }, follow_redirects=False)

    email = core.config_manager.config().notifications.email
    assert response.status_code == 303
    assert email.smtp_security == "ssl"
    assert email.use_tls is False
    assert email.smtp_port == 465
    assert core.config_manager.saved is True


def test_settings_save_connection_config() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/settings", data={
        "timezone": "Europe/Berlin",
        "connections_submitted": "1",
        "jellyfin_url": "http://jellyfin.local:8096",
        "jellyfin_api_key": "new-jellyfin-key",
        "jellyfin_user_id": "new-user-id",
        "jellyfin_sync_interval_minutes": "30",
        "tunarr_url": "http://tunarr.local:8000",
    }, follow_redirects=False)

    config = core.config_manager.config()
    assert response.status_code == 303
    assert config.jellyfin.url == "http://jellyfin.local:8096"
    assert config.jellyfin.api_key == "new-jellyfin-key"
    assert config.jellyfin.user_id == "new-user-id"
    assert config.jellyfin.sync_interval_minutes == 30
    assert config.tunarr.url == "http://tunarr.local:8000"
    assert core.config_manager.saved is True


def test_settings_page_has_tabs_and_help_tooltips() -> None:
    client = make_client(Core())

    response = client.get("/settings")

    assert response.status_code == 200
    assert 'id="settings-tabs"' in response.text
    assert 'data-settings-tab="integrations"' in response.text
    assert 'data-settings-section="metadata"' in response.text
    assert 'data-bs-toggle="tooltip"' in response.text
    assert 'name="connections_submitted"' in response.text
    assert "Jellyfin / Tunarr Connections" in response.text
    assert "Jellyfin login required" in response.text
    assert "Dashboard &gt; Users" in response.text
    assert "SMTP Security" in response.text
    assert "Artwork and Metadata Providers" in response.text
    assert "Restore Uploaded Backup" in response.text


def test_settings_can_test_jellystat_without_saving(monkeypatch) -> None:
    core = Core()
    client = make_client(core)
    calls = []

    class FakeJellystatClient:
        def __init__(
            self,
            *,
            base_url: str,
            api_token: str,
            rate_limit_per_minute: int,
        ) -> None:
            calls.append({
                "base_url": base_url,
                "api_token": api_token,
                "rate_limit_per_minute": rate_limit_per_minute,
            })

        async def check_connection(self, *, days: int = 1) -> dict:
            calls[-1]["days"] = days
            return {
                "ok": True,
                "message": "Jellystat connection OK. Stats endpoint returned 2 rows.",
            }

    monkeypatch.setattr(settings_routes, "JellystatClient", FakeJellystatClient)

    page = client.get("/settings")
    assert page.status_code == 200
    assert "Test Jellystat" in page.text
    assert 'formaction="/settings/jellystat/test"' in page.text

    response = client.post("/settings/jellystat/test", data={
        "timezone": "Europe/Berlin",
        "metadata_submitted": "1",
        "metadata_jellystat_enabled": "on",
        "metadata_jellystat_url": "http://jellystat:3000",
        "metadata_jellystat_api_token": "js-token",
        "metadata_jellystat_days": "14",
        "metadata_jellystat_activity_weight": "10",
        "metadata_jellystat_completion_weight": "8",
        "metadata_jellystat_trend_weight": "8",
        "metadata_jellystat_genre_trend_weight": "6",
        "metadata_jellystat_underused_weight": "6",
        "metadata_jellystat_stale_weight": "4",
        "metadata_jellystat_rate_limit_per_minute": "12",
        "metadata_tmdb_rate_limit_per_minute": "90",
        "metadata_tvdb_rate_limit_per_minute": "45",
        "metadata_omdb_rate_limit_per_minute": "30",
        "metadata_cache_ttl_days": "21",
    })

    assert response.status_code == 200
    assert "Jellystat connection OK. Stats endpoint returned 2 rows." in response.text
    assert calls == [{
        "base_url": "http://jellystat:3000",
        "api_token": "js-token",
        "rate_limit_per_minute": 12,
        "days": 14,
    }]
    assert core.config_manager.saved is False
    assert core.config_manager.config().metadata.jellystat_url == ""


def test_channel_config_saves_movie_only_daypart_settings() -> None:
    core = Core()
    client = make_client(core)

    page = client.get("/channels/ch1/config")
    assert page.status_code == 200
    assert 'id="channel_profile"' in page.text
    assert "Movie Channel" in page.text
    assert "Series Marathon" in page.text
    assert 'data-profile-scope="advanced general_tv movie_channel"' in page.text
    assert "Movies Only" in page.text
    assert "Jellyfin random" in page.text

    response = client.post("/channels/ch1/config", data={
        "return_to": "/channels/ch1",
        "name": "Channel",
        "channel_profile": "movie_channel",
        "scheduling_enabled": "true",
        "public_epg_submitted": "1",
        "public_epg_enabled": "on",
        "schedule_horizon_days": "1",
        "standby_custom_show_id": "",
        "daypart_indices": "0",
        "daypart_name_0": "all_movies",
        "daypart_start_0": "06:00",
        "daypart_end_0": "02:00",
        "daypart_content_mode_0": "movies",
        "daypart_rotation_0": "default",
        "daypart_slot_duration_0": "120",
        "daypart_allow_movies_0": "on",
        "daypart_variable_movie_duration_0": "on",
        "daypart_movie_selection_0": "library_random",
        "daypart_movie_slot_count_0": "0",
        "daypart_end_tolerance_minutes_0": "30",
        "daypart_ad_density_0": "0.08",
        "daypart_continuity_frequency_0": "4",
        "daypart_days_0": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "rotations_yaml": "[]",
        "ads_enabled": "on",
        "ads_filler_list_id": "",
        "ads_break_after_programs": "1",
        "ads_min_total_minutes": "0",
        "ads_max_total_minutes": "0",
        "ads_ad_density": "0.08",
        "ads_min_ad_break_duration_minutes": "1",
        "ads_max_ad_break_duration_minutes": "5",
        "continuity_enabled": "on",
        "continuity_frequency": "4",
        "continuity_station_id_custom_show_id": "",
        "continuity_bumper_custom_show_id": "",
        "continuity_station_id_clip_ids": "",
        "continuity_bumper_clip_ids": "",
        "pipeline_text": "\n".join(core.config_manager.config().channels[0].pipeline),
    }, follow_redirects=False)

    daypart = core.config_manager.config().channels[0].dayparts[0]
    assert response.status_code == 303
    assert core.config_manager.config().channels[0].channel_profile == "movie_channel"
    assert daypart.content_mode == "movies"
    assert daypart.allow_movies is True
    assert daypart.variable_movie_duration is True
    assert daypart.movie_selection == "library_random"


def test_channel_config_shows_profile_recommendation_entry_points() -> None:
    core = Core()
    core.config_manager.config().channels[0].channel_profile = "movie_channel"
    client = make_client(core)

    page = client.get("/channels/ch1/config")

    assert page.status_code == 200
    assert "Movie Pool Recommendations" in page.text
    assert "Build Movie Channel" in page.text
    assert "balance_mode=movie_friendly" in page.text
    assert "profile=movie-channel-pool" in page.text
    assert "media_type=movie" in page.text
    assert "Marathon Series Recommendations" in page.text
    assert "Build Series Marathon" in page.text
    assert "balance_mode=series_only" in page.text


def test_playlist_crud_uses_grouped_series_and_movies() -> None:
    core = Core()
    now = datetime.now(tz=UTC)
    core.playlist_repo.categories["category-1"] = PlaylistCategory(
        id="category-1",
        name="FlixWolf One",
        description="Main channel",
        created_at=now,
        updated_at=now,
    )
    client = make_client(core)

    form = client.get("/playlists/new")
    assert form.status_code == 200
    assert "FlixWolf One" in form.text
    assert 'id="tags"' in form.text
    assert 'id="channel_scope"' in form.text
    assert 'id="media-search"' in form.text
    assert 'id="media-type"' in form.text
    assert 'id="media-browser"' in form.text
    assert 'form="playlist-form"' in form.text
    assert "dragstart" in form.text
    assert "playlist-selected-row" in form.text
    assert form.text.index('form="playlist-form"') < form.text.index('id="playlist-form"')

    options = client.get("/playlists/media-options")
    assert options.status_code == 200
    assert "Series One" in options.text
    assert "1 episodes" in options.text
    assert "Movie One" in options.text
    assert options.text.count(">Previous</button>") == 2
    assert options.text.count(">Next</button>") == 2

    response = client.post("/playlists", data={
        "name": "Prime Time",
        "description": "Ordered mix",
        "category_id": "category-1",
        "channel_scope": "ch1",
        "tags": "night, Crime, night",
        "item_order": "series:series-1\nmovie:movie-1",
    }, follow_redirects=False)

    assert response.status_code == 303
    playlist = core.playlist_repo.playlists["playlist-1"]
    assert playlist.category_id == "category-1"
    assert playlist.category_name == "FlixWolf One"
    assert playlist.channel_scope == "ch1"
    assert playlist.tags == ["night", "crime"]
    assert [(item.media_type, item.media_id) for item in playlist.items] == [
        ("series", "series-1"),
        ("movie", "movie-1"),
    ]

    edit = client.get("/playlists/playlist-1/edit")
    assert edit.status_code == 200
    assert "Prime Time" in edit.text
    assert "Add Recommendations" in edit.text
    assert "Builder from Playlist" in edit.text
    assert "/recommendations/builder?preview=1" in edit.text
    assert "playlist_id=playlist-1" in edit.text
    assert "playlist_mode=append" in edit.text
    assert edit.text.index("series:series-1") < edit.text.index("movie:movie-1")

    selected = client.get(
        "/playlists/media-options",
        params={"selected": "movie:movie-1\nseries:series-1"},
    )
    assert selected.status_code == 200
    assert selected.text.index("Movie One") < selected.text.index("Series One")
    assert selected.text.count("checked") == 2
    assert 'draggable="true"' in selected.text
    assert "Drag to reorder" in selected.text

    listing = client.get("/playlists?category=category-1&tag=crime&scope=ch1")
    assert listing.status_code == 200
    assert "Manage Categories" in listing.text
    assert "FlixWolf One" in listing.text
    assert "crime" in listing.text
    assert "Channel" in listing.text
    assert "source_category=category-1" in listing.text
    assert "source_tag=crime" in listing.text
    assert "Build channel" in listing.text


def test_recommendation_builder_can_start_from_playlist_category() -> None:
    core = Core()
    now = datetime.now(tz=UTC)
    core.playlist_repo.categories["category-1"] = PlaylistCategory(
        id="category-1",
        name="Sci-Fi Blocks",
        description="Space programming",
        created_at=now,
        updated_at=now,
    )
    core.playlist_repo.playlists["playlist-1"] = Playlist(
        id="playlist-1",
        name="Star Trek Rotation",
        category_id="category-1",
        category_name="Sci-Fi Blocks",
        tags=["sci-fi", "space"],
        items=[
            PlaylistItem(media_type="series", media_id="series-1", title="Series One"),
        ],
        created_at=now,
        updated_at=now,
    )
    client = make_client(core)

    response = client.get(
        "/recommendations/builder",
        params={
            "preview": "1",
            "mode": "channel",
            "source_category": "category-1",
        },
    )

    assert response.status_code == 200
    assert "Building from category" in response.text
    assert "Sci-Fi Blocks" in response.text
    assert "Sci-Fi Blocks Channel" in response.text
    assert 'name="source_category" value="category-1"' in response.text


def test_playlist_list_shows_usage_and_blocks_delete() -> None:
    core = Core()
    now = datetime.now(tz=UTC)
    core.playlist_repo.playlists["playlist-1"] = Playlist(
        id="playlist-1",
        name="Morning Playlist",
        items=[],
        created_at=now,
        updated_at=now,
    )
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="morning",
            days=[DayOfWeek.MON],
            start_time="06:00",
            end_time="12:00",
            playlist_ids=["playlist-1"],
        ),
    ]
    client = make_client(core)

    response = client.get("/playlists")

    assert response.status_code == 200
    assert "Used By" in response.text
    assert "Channel / morning" in response.text
    assert "disabled" in response.text

    delete = client.post("/playlists/playlist-1/delete", follow_redirects=False)

    assert delete.status_code == 303
    assert delete.headers["location"] == "/playlists?delete_blocked=1"
    assert "playlist-1" in core.playlist_repo.playlists


def test_playlist_category_management_page() -> None:
    core = Core()
    client = make_client(core)

    create = client.post(
        "/playlist-categories",
        data={"name": "Documentaries", "description": "Docu playlists"},
        follow_redirects=False,
    )

    assert create.status_code == 303
    category = core.playlist_repo.categories["category-1"]
    assert category.name == "Documentaries"

    page = client.get("/playlist-categories")
    assert page.status_code == 200
    assert "Documentaries" in page.text
    assert "Back to Playlists" in page.text

    update = client.post(
        "/playlist-categories/category-1",
        data={"name": "Docs", "description": "Updated"},
        follow_redirects=False,
    )

    assert update.status_code == 303
    assert core.playlist_repo.categories["category-1"].name == "Docs"

    delete = client.post(
        "/playlist-categories/category-1/delete",
        follow_redirects=False,
    )

    assert delete.status_code == 303
    assert "category-1" not in core.playlist_repo.categories


def test_playlist_media_browser_filters_and_limits_results() -> None:
    core = Core()
    core.media_repo.get_playlist_options = _many_playlist_options  # type: ignore[method-assign]
    client = make_client(core)

    first_page = client.get("/playlists/media-options")
    assert first_page.status_code == 200
    assert first_page.text.count('class="form-check-input item-check"') == 100
    assert "150 library items | page 1 of 2" in first_page.text

    movies = client.get(
        "/playlists/media-options",
        params={"media_type": "movie", "page": "99"},
    )
    assert movies.status_code == 200
    assert "75 library items | page 1 of 1" in movies.text
    assert "Series 000" not in movies.text

    search = client.get("/playlists/media-options", params={"q": "movie 010"})
    assert search.status_code == 200
    assert "Movie 010" in search.text
    assert "1 library items" in search.text


def test_recommendations_page_creates_playlist_from_results() -> None:
    core = Core()
    now = datetime.now(tz=UTC)
    core.playlist_repo.categories["category-1"] = PlaylistCategory(
        id="category-1",
        name="Movie Picks",
        description="Recommended movie playlists",
        created_at=now,
        updated_at=now,
    )
    client = make_client(core)

    page = client.get("/recommendations", params={"profile": "prime-time-movies"})

    assert page.status_code == 200
    assert "Recommendations" in page.text
    assert "Prime-Time Movies" in page.text
    assert "Movie One" in page.text
    assert "Create Playlist" in page.text
    assert "Top 10" in page.text
    assert "Replace existing playlist" in page.text
    assert 'name="q"' in page.text
    assert 'name="exclude_q"' in page.text
    assert 'href="/recommendations"' in page.text

    response = client.post(
        "/recommendations/playlists",
        data={
            "profile": "prime-time-movies",
            "language_rule": "profile_default",
            "limit": "50",
            "media_type": "all",
            "min_score": "0",
            "selected_keys": "movie:movie-1",
            "name": "Recommended Prime",
            "description": "From UI",
            "category_id": "category-1",
            "channel_scope": "ch1",
            "tags": "recommended, movies",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/playlists?saved=1"
    playlist = core.playlist_repo.playlists["playlist-1"]
    assert playlist.name == "Recommended Prime"
    assert playlist.category_name == "Movie Picks"
    assert playlist.channel_scope == "ch1"
    assert playlist.tags == ["recommended", "movies"]
    assert [(item.media_type, item.media_id, item.title) for item in playlist.items] == [
        ("movie", "movie-1", "Movie One"),
    ]


def test_recommendations_auto_creates_category_and_tags() -> None:
    core = Core()
    client = make_client(core)

    response = client.post(
        "/recommendations/playlists",
        data={
            "profile": "prime-time-movies",
            "language_rule": "profile_default",
            "limit": "10",
            "media_type": "movie",
            "min_score": "0",
            "selected_keys": ["movie:movie-1"],
            "name": "Auto Organized",
            "auto_organize": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert len(core.playlist_repo.categories) == 1
    category = next(iter(core.playlist_repo.categories.values()))
    playlist = core.playlist_repo.playlists["playlist-1"]
    assert category.name == "Prime-Time Movies"
    assert playlist.category_id == category.id
    assert playlist.tags[:2] == ["recommended", "prime-time-movies"]


def test_recommendations_diagnostics_page_reports_coverage() -> None:
    core = Core()
    client = make_client(core)

    response = client.get("/recommendations/diagnostics")

    assert response.status_code == 200
    assert "Recommendation Diagnostics" in response.text
    assert "Available Items" in response.text
    assert "Metadata Coverage" in response.text
    assert "Prime-Time Movies" in response.text
    assert "genre" in response.text


def test_recommendation_compare_profiles_page() -> None:
    core = Core()
    client = make_client(core)

    response = client.get(
        "/recommendations/compare",
        params={
            "profiles": "prime-time-movies,series-marathon",
            "limit": "10",
        },
    )

    assert response.status_code == 200
    assert "Compare Profiles" in response.text
    assert "Prime-Time Movies" in response.text
    assert "Series Marathon" in response.text
    assert "Overlap" in response.text


def test_recommendations_explain_page_shows_candidate_details() -> None:
    core = Core()
    client = make_client(core)

    response = client.get(
        "/recommendations/explain",
        params={"profile": "prime-time-movies", "item_id": "movie-1"},
    )

    assert response.status_code == 200
    assert "Explain Recommendation" in response.text
    assert "Movie One" in response.text
    assert "Reasons" in response.text
    assert "runtime fits profile" in response.text


def test_channel_daypart_links_to_contextual_recommendations() -> None:
    core = Core()
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="Primetime Movie",
            days=[DayOfWeek.MON],
            start_time="20:00",
            end_time="22:00",
            content_mode="movies",
            allow_movies=True,
        ),
    ]
    client = make_client(core)

    config_page = client.get("/channels/ch1/config")

    assert config_page.status_code == 200
    assert "Recommendations" in config_page.text
    assert "/recommendations?channel_id=ch1" in config_page.text

    recommendations = client.get(
        "/recommendations",
        params={"channel_id": "ch1", "daypart": "Primetime Movie"},
    )

    assert recommendations.status_code == 200
    assert "Recommendation context" in recommendations.text
    assert "Channel" in recommendations.text
    assert "Primetime Movie 20:00-22:00" in recommendations.text
    assert 'value="prime-time-movies" selected' in recommendations.text
    assert 'value="movie" selected' in recommendations.text
    assert "Create & Assign Playlist" in recommendations.text


def test_channel_config_warns_when_daypart_has_too_little_playlist_content() -> None:
    core = Core()
    now = datetime.now(tz=UTC)
    core.playlist_repo.playlists["playlist-1"] = Playlist(
        id="playlist-1",
        name="Tiny Movie Pool",
        description="",
        items=[
            PlaylistItem(
                media_type="movie",
                media_id="movie-1",
                title="Movie One",
                position=0,
            ),
        ],
        created_at=now,
        updated_at=now,
    )
    core.config_manager.config().channels[0].channel_profile = "movie_channel"
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="Prime Movies",
            days=[DayOfWeek.MON],
            start_time="18:00",
            end_time="02:00",
            content_mode="movies",
            allow_movies=True,
            playlist_ids=["playlist-1"],
        ),
    ]
    client = make_client(core)

    page = client.get("/channels/ch1/config")

    assert page.status_code == 200
    assert "Prime Movies has only 1 eligible movie source" in page.text
    assert "recommended minimum is 8" in page.text


def test_channel_config_daypart_fix_creates_and_assigns_playlist() -> None:
    core = Core()
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="Morning",
            days=[DayOfWeek.MON],
            start_time="06:00",
            end_time="11:00",
        ),
    ]
    client = make_client(core)

    page = client.get("/channels/ch1/config")

    assert page.status_code == 200
    assert "Fix Now" in page.text

    response = client.post(
        "/recommendations/daypart-fix",
        data={
            "profile": "series-marathon",
            "language_rule": "profile_default",
            "limit": "10",
            "media_type": "series",
            "min_score": "1",
            "channel_id": "ch1",
            "daypart": "Morning",
            "assign_to_daypart": "1",
        },
        follow_redirects=False,
    )

    daypart = core.config_manager.config().channels[0].dayparts[0]
    assert response.status_code == 303
    assert response.headers["location"] == "/?saved=1&assigned_playlist=1"
    assert daypart.playlist_ids == ["playlist-1"]
    playlist = core.playlist_repo.playlists["playlist-1"]
    assert playlist.channel_scope == "ch1"
    assert playlist.items[0].media_type == "series"
    assert "auto-fix" in playlist.tags
    assert core.config_manager.saved is True


def test_recommendations_can_assign_created_playlist_to_daypart() -> None:
    core = Core()
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="Primetime Movie",
            days=[DayOfWeek.MON],
            start_time="20:00",
            end_time="22:00",
            content_mode="movies",
            allow_movies=True,
        ),
    ]
    client = make_client(core)

    response = client.post(
        "/recommendations/playlists",
        data={
            "profile": "prime-time-movies",
            "language_rule": "profile_default",
            "limit": "50",
            "media_type": "movie",
            "min_score": "0",
            "channel_id": "ch1",
            "daypart": "Primetime Movie",
            "assign_to_daypart": "1",
            "selected_keys": "movie:movie-1",
            "name": "Recommended Primetime",
            "description": "From daypart",
            "channel_scope": "ch1",
            "tags": "recommended, primetime",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?saved=1&assigned_playlist=1"
    assert core.config_manager.saved is True
    assert core.config_manager.config().channels[0].dayparts[0].playlist_ids == [
        "playlist-1",
    ]
    playlist = core.playlist_repo.playlists["playlist-1"]
    assert playlist.channel_scope == "ch1"
    assert playlist.tags == ["recommended", "primetime"]


def test_recommendations_page_filters_and_updates_existing_playlist() -> None:
    core = Core()
    now = datetime.now(tz=UTC)
    core.playlist_repo.playlists["playlist-1"] = Playlist(
        id="playlist-1",
        name="Existing Picks",
        description="Before",
        channel_scope="",
        tags=[],
        items=[
            PlaylistItem(
                media_type="movie",
                media_id="movie-1",
                title="Movie One",
                position=0,
            ),
        ],
        created_at=now,
        updated_at=now,
    )
    client = make_client(core)

    filtered = client.get(
        "/recommendations",
        params={"profile": "prime-time-movies", "q": "missing title"},
    )
    assert filtered.status_code == 200
    assert "Movie One" not in filtered.text
    assert "No recommendations matched this profile." in filtered.text

    visible = client.get(
        "/recommendations",
        params={
            "profile": "prime-time-movies",
            "q": "movie",
            "media_type": "movie",
            "min_score": "1",
        },
    )
    assert visible.status_code == 200
    assert "Movie One" in visible.text

    excluded = client.get(
        "/recommendations",
        params={
            "profile": "prime-time-movies",
            "q": "movie",
            "exclude_q": "movie",
            "media_type": "movie",
            "min_score": "1",
        },
    )
    assert excluded.status_code == 200
    assert "Movie One" not in excluded.text
    assert 'value="movie"' in excluded.text
    assert "No recommendations matched this profile." in excluded.text

    inline_excluded = client.get(
        "/recommendations",
        params={
            "profile": "prime-time-movies",
            "q": "movie -one",
            "media_type": "movie",
            "min_score": "1",
        },
    )
    assert inline_excluded.status_code == 200
    assert "Movie One" not in inline_excluded.text

    append = client.post(
        "/recommendations/playlists",
        data={
            "profile": "prime-time-movies",
            "language_rule": "profile_default",
            "limit": "50",
            "q": "movie",
            "media_type": "movie",
            "min_score": "1",
            "playlist_mode": "append",
            "target_playlist_id": "playlist-1",
            "selected_keys": ["movie:movie-1", "movie:movie-1"],
            "name": "Existing Picks",
            "description": "After",
            "tags": "recommended",
        },
        follow_redirects=False,
    )

    assert append.status_code == 303
    playlist = core.playlist_repo.playlists["playlist-1"]
    assert playlist.description == "After"
    assert playlist.tags == ["recommended"]
    assert [(item.media_type, item.media_id) for item in playlist.items] == [
        ("movie", "movie-1"),
    ]


def test_recommendations_can_suggest_similar_to_playlist_and_append_to_source() -> None:
    core = Core()
    now = datetime.now(tz=UTC)
    core.playlist_repo.playlists["playlist-1"] = Playlist(
        id="playlist-1",
        name="Space Movies",
        description="Sci-fi space source list",
        channel_scope="ch1",
        tags=["sci-fi", "space"],
        items=[
            PlaylistItem(
                media_type="movie",
                media_id="movie-1",
                title="Movie One",
                position=0,
            ),
        ],
        created_at=now,
        updated_at=now,
    )
    client = make_client(core)

    page = client.get(
        "/recommendations",
        params={
            "profile": "late-night-genre",
            "source_playlist_id": "playlist-1",
            "media_type": "series",
            "min_score": "1",
        },
    )

    assert page.status_code == 200
    assert "Similarity source" in page.text
    assert "Space Movies" in page.text
    assert "Series One" in page.text
    assert "Movie One" not in page.text
    assert "similar to source playlist terms" in page.text

    append = client.post(
        "/recommendations/playlists",
        data={
            "profile": "late-night-genre",
            "language_rule": "profile_default",
            "limit": "50",
            "media_type": "series",
            "min_score": "1",
            "source_playlist_id": "playlist-1",
            "playlist_mode": "append",
            "selected_keys": "series:series-1",
            "name": "Space Movies",
            "description": "Updated from similar recommendations",
            "tags": "sci-fi, space",
        },
        follow_redirects=False,
    )

    assert append.status_code == 303
    playlist = core.playlist_repo.playlists["playlist-1"]
    assert playlist.description == "Updated from similar recommendations"
    assert [(item.media_type, item.media_id) for item in playlist.items] == [
        ("movie", "movie-1"),
        ("series", "series-1"),
    ]


def test_recommendation_builder_previews_and_saves_channel_plan() -> None:
    core = Core()
    client = make_client(core)

    page = client.get(
        "/recommendations/builder",
        params={
            "preview": "1",
            "mode": "channel",
            "channel_id": "ch1",
            "channel_name": "Builder Test",
            "profile": "series-marathon",
            "themes": "comedy, drama",
            "per_theme_limit": "5",
            "replace_dayparts": "1",
        },
    )

    assert page.status_code == 200
    assert "Builder Test recommendation plan" in page.text
    assert "Morning Comedy" in page.text
    assert "Daytime Drama" in page.text
    assert "Series One" in page.text
    assert "Save Reviewed Run" in page.text
    assert 'name="daypart_name_0"' in page.text
    assert 'name="daypart_profile_0"' in page.text
    assert 'name="balance_mode"' in page.text
    assert 'name="max_movies_per_theme"' in page.text
    assert 'name="min_series_per_theme"' in page.text

    saved = client.post(
        "/recommendations/builder/runs",
        data={
            "mode": "channel",
            "channel_id": "ch1",
            "channel_name": "Builder Test",
            "profile": "series-marathon",
            "themes": "comedy, drama",
            "per_theme_limit": "5",
            "replace_dayparts": "1",
        },
        follow_redirects=False,
    )

    assert saved.status_code == 303
    assert "run_id=run-1" in saved.headers["location"]
    assert core.recommendation_run_repo.runs["run-1"]["status"] == "draft"


def test_recommendation_builder_improves_existing_channel_windows() -> None:
    core = Core()
    channel = core.config_manager.config().channels[0]
    channel.dayparts = [
        DaypartTemplate(
            name="breakfast",
            days=list(DayOfWeek),
            start_time="06:30",
            end_time="10:00",
        ),
        DaypartTemplate(
            name="prime",
            days=list(DayOfWeek),
            start_time="20:00",
            end_time="23:00",
        ),
    ]
    client = make_client(core)

    page = client.get(
        "/recommendations/builder",
        params={
            "preview": "1",
            "mode": "channel",
            "builder_mode": "improve",
            "channel_id": "ch1",
            "channel_name": "Improved Channel",
            "profile": "auto",
            "themes": "science fiction, mystery",
            "per_theme_limit": "5",
        },
    )

    assert page.status_code == 200
    assert "Improve existing config" in page.text
    assert 'name="builder_mode"' in page.text
    assert 'value="breakfast"' in page.text
    assert 'value="prime"' in page.text
    assert 'value="06:30"' in page.text
    assert 'value="20:00"' in page.text
    assert "Morning Science Fiction" not in page.text


def test_recommendation_builder_reruns_saved_run_as_template() -> None:
    core = Core()
    client = make_client(core)

    saved = client.post(
        "/recommendations/builder/runs",
        data={
            "mode": "channel",
            "builder_mode": "scratch",
            "channel_id": "ch1",
            "channel_name": "Rerun Test",
            "profile": "series-marathon",
            "themes": "comedy",
            "per_theme_limit": "5",
            "replace_dayparts": "1",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303

    history = client.get("/recommendations/runs")
    assert history.status_code == 200
    assert "Rerun Preview" in history.text
    assert "Rerun & Save" in history.text

    preview = client.get("/recommendations/runs/run-1/rerun")
    assert preview.status_code == 200
    assert "Fresh preview generated from saved run run-1" in preview.text
    assert "Rerun Test recommendation plan" in preview.text

    rerun = client.post("/recommendations/runs/run-1/rerun", follow_redirects=False)
    assert rerun.status_code == 303
    assert "run_id=run-2" in rerun.headers["location"]
    assert len(core.recommendation_run_repo.runs) == 2


def test_recommendation_builder_auto_profiles_per_theme() -> None:
    core = Core()
    client = make_client(core)

    page = client.get(
        "/recommendations/builder",
        params={
            "preview": "1",
            "mode": "channel",
            "channel_id": "ch1",
            "channel_name": "Builder Test",
            "profile": "auto",
            "themes": "anime movies, documentary",
            "seed": "Movie One",
            "per_theme_limit": "5",
        },
    )

    assert page.status_code == 200
    assert "Auto profile per theme" in page.text
    assert "Anime Movies/OVAs" in page.text
    assert "Documentary" in page.text
    assert 'name="daypart_start_0"' in page.text


def test_recommendation_builder_auto_profile_inference_prefers_genre_terms() -> None:
    assert recommendation_routes._infer_builder_profile("Science Fiction", "auto", 0) == (
        "late-night-genre"
    )
    assert recommendation_routes._infer_builder_profile("Mystery", "auto", 1) == (
        "late-night-genre"
    )
    assert recommendation_routes._infer_builder_profile("Science Documentary", "auto", 0) == (
        "documentary"
    )


def test_recommendation_builder_caps_movies_for_mixed_tv_dayparts() -> None:
    class Candidate:
        def __init__(self, media_id: str, title: str, media_type: str) -> None:
            self.id = media_id
            self.title = title
            self.media_type = media_type

    class Result:
        def __init__(
            self,
            media_id: str,
            title: str,
            media_type: str,
            score: int,
            genres: list[str],
        ) -> None:
            self.candidate = Candidate(media_id, title, media_type)
            self.score = score
            self.accepted = True
            self.reasons = [f"matches profile terms: {', '.join(genres)}"]
            self._genres = genres

        def as_dict(self) -> dict[str, object]:
            return {"genres": self._genres, "tags": [], "manual_terms": []}

    results = [
        Result("movie-1", "Star Trek Movie 1", "movie", 100, ["science fiction"]),
        Result("movie-2", "Star Trek Movie 2", "movie", 99, ["science fiction"]),
        Result("movie-3", "Star Trek Movie 3", "movie", 98, ["science fiction"]),
        Result("series-1", "Star Trek: Voyager", "series", 80, ["science fiction"]),
        Result("series-2", "Stargate Universe", "series", 79, ["science fiction"]),
        Result("series-3", "The Expanse", "series", 78, ["science fiction"]),
    ]

    selected = recommendation_routes._select_theme_results(
        results,
        "Science Fiction",
        5,
        "Star Trek",
        max_movies=1,
    )

    assert sum(1 for item in selected if item.candidate.media_type == "movie") == 1
    assert sum(1 for item in selected if item.candidate.media_type == "series") == 3
    assert len(selected) == 4


def test_recommendation_builder_custom_balance_can_exclude_movies() -> None:
    class Candidate:
        def __init__(self, media_id: str, title: str, media_type: str) -> None:
            self.id = media_id
            self.title = title
            self.media_type = media_type

    class Result:
        def __init__(self, media_id: str, title: str, media_type: str) -> None:
            self.candidate = Candidate(media_id, title, media_type)
            self.score = 100
            self.accepted = True
            self.reasons = ["matches profile terms: science fiction"]

        def as_dict(self) -> dict[str, object]:
            return {"genres": ["science fiction"], "tags": [], "manual_terms": []}

    selected = recommendation_routes._select_theme_results(
        [
            Result("movie-1", "Star Trek Movie", "movie"),
            Result("series-1", "Star Trek: Voyager", "series"),
            Result("series-2", "Stargate Universe", "series"),
        ],
        "Science Fiction",
        3,
        "Star Trek",
        max_movies=0,
    )

    assert [item.candidate.media_type for item in selected] == ["series", "series"]


def test_recommendation_builder_apply_creates_playlists_and_dayparts() -> None:
    core = Core()
    client = make_client(core)
    response = client.post(
        "/recommendations/builder/runs",
        data={
            "mode": "channel",
            "channel_id": "ch1",
            "channel_name": "Builder Test",
            "profile": "series-marathon",
            "themes": "comedy, drama",
            "per_theme_limit": "5",
            "replace_dayparts": "1",
            "daypart_count": "2",
            "daypart_name_0": "Morning Laughs",
            "daypart_theme_0": "comedy",
            "daypart_start_0": "07:00",
            "daypart_end_0": "10:00",
            "daypart_profile_0": "morning-sitcoms",
            "daypart_enabled_0": "1",
            "daypart_name_1": "Prime Drama",
            "daypart_theme_1": "drama",
            "daypart_start_1": "20:00",
            "daypart_end_1": "22:30",
            "daypart_profile_1": "series-marathon",
            "daypart_enabled_1": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    applied = client.post("/recommendations/runs/run-1/apply", follow_redirects=False)

    assert applied.status_code == 303
    assert applied.headers["location"] == "/recommendations/runs?applied=1"
    assert core.recommendation_run_repo.runs["run-1"]["status"] == "applied"
    channel = core.config_manager.config().channels[0]
    assert [daypart.name for daypart in channel.dayparts][:2] == [
        "Morning Laughs",
        "Prime Drama",
    ]
    assert channel.dayparts[0].start_time == "07:00"
    assert channel.dayparts[1].end_time == "22:30"
    assert len(core.playlist_repo.playlists) == 2
    assert all(daypart.playlist_ids for daypart in channel.dayparts)
    assert core.config_manager.saved is True


def test_recommendation_builder_generate_draft_applies_run_and_starts_job() -> None:
    core = Core()
    client = make_client(core)
    response = client.post(
        "/recommendations/builder/runs",
        data={
            "mode": "channel",
            "channel_id": "ch1",
            "channel_name": "Builder Test",
            "profile": "series-marathon",
            "themes": "comedy",
            "per_theme_limit": "5",
            "replace_dayparts": "1",
            "daypart_count": "1",
            "daypart_name_0": "Morning Laughs",
            "daypart_theme_0": "comedy",
            "daypart_start_0": "07:00",
            "daypart_end_0": "10:00",
            "daypart_profile_0": "morning-sitcoms",
            "daypart_enabled_0": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    generated = client.post(
        "/recommendations/runs/run-1/generate-draft",
        follow_redirects=False,
    )

    assert generated.status_code == 303
    assert "generated=1" in generated.headers["location"]
    assert core.recommendation_run_repo.runs["run-1"]["status"] == "applied"
    assert core.job_manager.started is True
    assert core.job_manager.generation_mode == "fresh"
    assert core.config_manager.config().channels[0].dayparts[0].name == "Morning Laughs"


def test_recommendation_profile_management_and_custom_run() -> None:
    core = Core()
    client = make_client(core)

    listing = client.get("/recommendations/profiles")
    assert listing.status_code == 200
    assert "Built-In Profiles" in listing.text
    assert "New Profile" in listing.text

    form = client.get("/recommendations/profiles/new")
    assert form.status_code == 200
    assert "Preferred Genres" in form.text
    assert "Advanced Weights" in form.text

    created = client.post(
        "/recommendations/profiles",
        data={
            "id": "custom-movies",
            "name": "Custom Movies",
            "description": "Movies from UI",
            "media_types": "movie",
            "language_rule": "none",
            "preferred_genres": "Action",
            "min_runtime_minutes": "60",
            "max_runtime_minutes": "120",
            "min_items": "1",
            "weight_genre": "50",
            "weight_runtime": "20",
        },
        follow_redirects=False,
    )

    assert created.status_code == 303
    profile = core.recommendation_profile_repo.profiles["custom-movies"]
    assert profile.name == "Custom Movies"
    assert profile.media_types == ("movie",)
    assert profile.preferred_genres == ("Action",)

    recommendations = client.get("/recommendations", params={"profile": "custom-movies"})
    assert recommendations.status_code == 200
    assert "Custom Movies" in recommendations.text
    assert "Movie One" in recommendations.text

    edited = client.post(
        "/recommendations/profiles/custom-movies",
        data={
            "name": "Custom Movies Updated",
            "media_types": "movie",
            "language_rule": "none",
            "preferred_genres": "Sci-Fi",
            "min_items": "1",
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303
    assert core.recommendation_profile_repo.profiles["custom-movies"].name == (
        "Custom Movies Updated"
    )

    deleted = client.post(
        "/recommendations/profiles/custom-movies/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert "custom-movies" not in core.recommendation_profile_repo.profiles


def test_recommendations_playlist_create_requires_selection() -> None:
    core = Core()
    client = make_client(core)

    response = client.post(
        "/recommendations/playlists",
        data={
            "profile": "prime-time-movies",
            "language_rule": "profile_default",
            "limit": "50",
            "media_type": "all",
            "min_score": "0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/recommendations?")
    assert "playlist-1" not in core.playlist_repo.playlists


async def _many_playlist_options() -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for index in range(75):
        options.append({
            "key": f"series:series-{index}",
            "media_type": "series",
            "media_id": f"series-{index}",
            "title": f"Series {index:03d}",
            "details": "12 episodes",
        })
        options.append({
            "key": f"movie:movie-{index}",
            "media_type": "movie",
            "media_id": f"movie-{index}",
            "title": f"Movie {index:03d}",
            "details": "2025",
        })
    return options


def test_channel_daypart_saves_scheduler_playlist_selection() -> None:
    core = Core()
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="morning",
            days=[DayOfWeek.MON],
            start_time="06:00",
            end_time="12:00",
        ),
    ]
    core.config_manager.config().channels[0].ads = AdsConfig(
        enabled=True,
        filler_list_id="filler-1",
        ad_density=0.1,
        max_total_minutes=10,
        min_ad_break_duration_minutes=1,
        max_ad_break_duration_minutes=3,
    )
    client = make_client(core)

    response = client.post("/channels/ch1/config", data={
        "name": "Channel",
        "scheduling_enabled": "on",
        "schedule_horizon_days": "1",
        "daypart_name_0": "morning",
        "daypart_days_0": "mon",
        "daypart_start_0": "06:00",
        "daypart_end_0": "12:00",
        "daypart_rotation_0": "default",
        "daypart_playlist_ids_0": "playlist-1",
        "daypart_slot_duration_0": "30",
        "daypart_end_tolerance_minutes_0": "20",
        "daypart_ad_density_0": "0.08",
        "daypart_continuity_frequency_0": "4",
        "rotations_yaml": "[]",
        "ads_yaml": "{}",
        "pipeline_text": "daypart_applicator",
    })

    assert response.status_code == 200
    assert core.config_manager.config().channels[0].dayparts[0].playlist_ids == [
        "playlist-1",
    ]
    assert core.config_manager.config().channels[0].dayparts[0].end_tolerance_minutes == 20


def test_channel_daypart_sources_can_be_cleared() -> None:
    core = Core()
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="morning",
            days=[DayOfWeek.MON],
            start_time="06:00",
            end_time="12:00",
            playlist_ids=["playlist-1"],
            custom_show_list_ids=["custom-morning"],
        ),
    ]
    client = make_client(core)

    response = client.post("/channels/ch1/config", data={
        "name": "Channel",
        "scheduling_enabled": "on",
        "schedule_horizon_days": "1",
        "daypart_indices": "0",
        "daypart_name_0": "morning",
        "daypart_days_0": "mon",
        "daypart_start_0": "06:00",
        "daypart_end_0": "12:00",
        "daypart_rotation_0": "default",
        "daypart_slot_duration_0": "30",
        "daypart_ad_density_0": "0.08",
        "daypart_continuity_frequency_0": "4",
        "rotations_yaml": "[]",
        "ads_yaml": "{}",
        "pipeline_text": "daypart_applicator",
    })

    assert response.status_code == 200
    daypart = core.config_manager.config().channels[0].dayparts[0]
    assert daypart.playlist_ids == []
    assert daypart.custom_show_list_ids == []


def test_channel_dayparts_can_be_added_and_removed() -> None:
    core = Core()
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="morning",
            days=[DayOfWeek.MON],
            start_time="06:00",
            end_time="12:00",
        ),
    ]
    core.config_manager.config().channels[0].ads = AdsConfig(
        enabled=True,
        filler_list_id="filler-1",
        ad_density=0.1,
        max_total_minutes=10,
        min_ad_break_duration_minutes=1,
        max_ad_break_duration_minutes=3,
    )
    client = make_client(core)
    common = {
        "name": "Channel",
        "scheduling_enabled": "on",
        "schedule_horizon_days": "1",
        "rotations_yaml": "[]",
        "ads_yaml": "{}",
        "pipeline_text": "daypart_applicator",
    }

    added = client.post("/channels/ch1/config", data={
        **common,
        "daypart_indices": "0,1",
        "daypart_name_0": "morning",
        "daypart_days_0": "mon",
        "daypart_start_0": "06:00",
        "daypart_end_0": "12:00",
        "daypart_rotation_0": "default",
        "daypart_name_1": "afternoon",
        "daypart_days_1": ["mon", "tue"],
        "daypart_start_1": "12:00",
        "daypart_end_1": "18:00",
        "daypart_rotation_1": "default",
    })

    assert added.status_code == 200
    dayparts = core.config_manager.config().channels[0].dayparts
    assert [daypart.name for daypart in dayparts] == ["morning", "afternoon"]
    assert dayparts[1].days == [DayOfWeek.MON, DayOfWeek.TUE]

    removed = client.post("/channels/ch1/config", data={
        **common,
        "daypart_indices": "",
    })

    assert removed.status_code == 200
    assert core.config_manager.config().channels[0].dayparts == []


def test_channel_dayparts_can_be_reordered_from_editor_indices() -> None:
    core = Core()
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="morning",
            days=[DayOfWeek.MON],
            start_time="06:00",
            end_time="12:00",
        ),
        DaypartTemplate(
            name="evening",
            days=[DayOfWeek.MON],
            start_time="18:00",
            end_time="23:00",
        ),
    ]
    client = make_client(core)

    response = client.post("/channels/ch1/config", data={
        "name": "Channel",
        "scheduling_enabled": "on",
        "schedule_horizon_days": "1",
        "daypart_indices": "1,0",
        "daypart_name_0": "morning",
        "daypart_days_0": "mon",
        "daypart_start_0": "06:00",
        "daypart_end_0": "12:00",
        "daypart_rotation_0": "default",
        "daypart_name_1": "evening",
        "daypart_days_1": "mon",
        "daypart_start_1": "18:00",
        "daypart_end_1": "23:00",
        "daypart_rotation_1": "default",
        "rotations_yaml": "[]",
        "ads_yaml": "{}",
        "pipeline_text": "daypart_applicator",
    })

    assert response.status_code == 200
    assert [dp.name for dp in core.config_manager.config().channels[0].dayparts] == [
        "evening",
        "morning",
    ]


def test_channel_config_editor_shows_daypart_warnings_and_ad_estimate() -> None:
    core = Core()
    core.config_manager.config().channels[0].schedule_horizon_days = 7
    core.config_manager.config().channels[0].ads = AdsConfig(
        enabled=True,
        filler_list_id="",
        min_total_minutes=20,
        max_total_minutes=10,
        min_ad_break_duration_minutes=5,
        max_ad_break_duration_minutes=2,
    )
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="morning",
            days=[DayOfWeek.MON],
            start_time="06:00",
            end_time="12:00",
            ad_density=0.1,
            playlist_ids=["playlist-1"],
        ),
        DaypartTemplate(
            name="morning",
            days=[DayOfWeek.MON],
            start_time="11:00",
            end_time="14:00",
            ad_density=0.1,
        ),
    ]
    now = datetime.now(tz=UTC)
    core.playlist_repo.playlists["playlist-1"] = Playlist(
        id="playlist-1",
        name="Movies Only",
        items=[
            PlaylistItem(
                media_type="movie",
                media_id="movie-1",
                title="Movie One",
            ),
        ],
        created_at=now,
        updated_at=now,
    )
    client = make_client(core)

    response = client.get("/channels/ch1/config")

    assert response.status_code == 200
    assert "Daypart checks" in response.text
    assert "Duplicate daypart name: morning" in response.text
    assert "Mon has overlapping coverage near morning" in response.text
    assert "Estimated Target" in response.text
    assert "Daypart Ad Estimate" in response.text
    assert "Minimum total ad minutes is higher than maximum total ad minutes" in response.text
    assert "Minimum break length is higher than maximum break length" in response.text
    assert "No Tunarr filler list is selected" in response.text
    assert "Recommendation source check" in response.text
    assert (
        "morning has only 0 eligible series sources from selected scheduler playlist sources"
        in response.text
    )
    assert "Fix suggestions for underfilled daypart" in response.text
    assert "Build Fix" in response.text
    assert "builder_mode=improve" in response.text
    assert "assign_to_daypart=1" in response.text
    assert "move-daypart-up" in response.text
    assert "duplicate-daypart" in response.text


def test_auth_redirects_unauthenticated_requests_to_login() -> None:
    app = create_app(Core())
    client = TestClient(app)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_rejects_wrong_password() -> None:
    app = create_app(Core())
    client = TestClient(app)

    response = client.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
    )

    assert response.status_code == 401
    assert "Invalid username or password" in response.text


def test_login_rejects_wrong_username() -> None:
    app = create_app(Core())
    client = TestClient(app)

    response = client.post(
        "/login",
        data={"username": "other", "password": "password123"},
    )

    assert response.status_code == 401
    assert "Invalid username or password" in response.text


def test_setup_redirects_when_auth_is_missing() -> None:
    core = Core()
    core.config_manager.config().auth.password_hash = ""
    app = create_app(core)
    client = TestClient(app)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_setup_is_not_available_after_configuration() -> None:
    core = Core()
    core.config_manager.config().jellyfin.api_key = "jf-key"
    core.config_manager.config().jellyfin.user_id = "jf-user"
    app = create_app(core)
    client = TestClient(app)

    response = client.get("/setup", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_setup_tests_connections_and_saves_credentials() -> None:
    core = Core()
    core.config_manager.config().auth.password_hash = ""
    app = create_app(core)
    app.state.jellyfin_client_class = SetupJellyfinClient
    app.state.tunarr_client_class = SetupTunarrClient
    client = TestClient(app)

    response = client.post("/setup", data={
        "admin_username": "admin",
        "admin_password": "new-password",
        "jellyfin_url": "http://jellyfin.local",
        "jellyfin_api_key": "jf-key",
        "jellyfin_user_id": "jf-user",
        "tunarr_url": "http://tunarr.local",
    })

    config = core.config_manager.config()
    assert response.status_code == 200
    assert "Setup Complete" in response.text
    assert SetupJellyfinClient.checked is True
    assert SetupTunarrClient.checked is True
    assert config.jellyfin.api_key == "jf-key"
    assert config.tunarr.url == "http://tunarr.local"
    assert config.timezone == "Europe/Berlin"
    assert config.auth.username == "admin"
    assert config.auth.password_hash.startswith("pbkdf2_sha256$")
    assert core.config_manager.saved is True


def test_setup_saves_timezone() -> None:
    core = Core()
    core.config_manager.config().auth.password_hash = ""
    app = create_app(core)
    app.state.jellyfin_client_class = SetupJellyfinClient
    app.state.tunarr_client_class = SetupTunarrClient
    client = TestClient(app)

    response = client.post("/setup", data={
        "admin_username": "admin",
        "admin_password": "new-password",
        "jellyfin_url": "http://jellyfin.local",
        "jellyfin_api_key": "jf-key",
        "jellyfin_user_id": "jf-user",
        "tunarr_url": "http://tunarr.local",
        "timezone": "UTC",
    })

    assert response.status_code == 200
    assert core.config_manager.config().timezone == "UTC"


def test_settings_page_saves_timezone() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/settings", data={"timezone": "UTC"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"
    assert core.config_manager.config().timezone == "UTC"
    assert core.config_manager.saved is True


def test_settings_page_shows_admin_login_form() -> None:
    client = make_client(Core())

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Admin Login" in response.text
    assert 'name="auth_username"' in response.text
    assert 'name="auth_current_password"' not in response.text
    assert 'name="auth_new_password"' in response.text


def test_settings_admin_login_rejects_short_new_password() -> None:
    core = Core()
    client = make_client(core)
    original_hash = core.config_manager.config().auth.password_hash

    response = client.post(
        "/settings",
        data={
            "timezone": "Europe/Berlin",
            "auth_submitted": "1",
            "auth_username": "new-admin",
            "auth_new_password": "short",
            "auth_confirm_password": "short",
        },
    )

    assert response.status_code == 400
    assert "New password must be at least 8 characters long." in response.text
    assert core.config_manager.config().auth.username == "admin"
    assert core.config_manager.config().auth.password_hash == original_hash


def test_settings_admin_login_updates_credentials_and_requires_relogin() -> None:
    core = Core()
    client = make_client(core)

    response = client.post(
        "/settings",
        data={
            "timezone": "Europe/Berlin",
            "auth_submitted": "1",
            "auth_username": "new-admin",
            "auth_new_password": "new-password",
            "auth_confirm_password": "new-password",
        },
        follow_redirects=False,
    )

    config = core.config_manager.config()
    assert response.status_code == 303
    assert response.headers["location"] == "/login?credentials_changed=1"
    assert config.auth.username == "new-admin"
    assert config.auth.password_hash.startswith("pbkdf2_sha256$")
    assert core.config_manager.saved is True

    protected = client.get("/", follow_redirects=False)
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login"

    old_login = client.post(
        "/login",
        data={"username": "admin", "password": "password123"},
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/login",
        data={"username": "new-admin", "password": "new-password"},
        follow_redirects=False,
    )
    assert new_login.status_code == 303
    assert new_login.headers["location"] == "/"


def test_schedule_list_formats_times_in_configured_timezone() -> None:
    core = Core()
    core.config_manager.config().timezone = "Europe/Berlin"
    client = make_client(core)

    response = client.get("/schedules/ch1")

    assert response.status_code == 200
    assert "2026-05-28 14:00:00 CEST" in response.text


def test_generate_route_is_post_and_returns_immediately() -> None:
    core = Core()
    client = make_client(core)

    assert client.get("/channels/ch1/generate").status_code == 405

    response = client.post("/channels/ch1/generate")

    assert response.status_code == 200
    assert "New generation started: job1" in response.text
    assert 'hx-swap-oob="outerHTML"' in response.text
    assert core.job_manager.started is True
    assert core.job_manager.generation_mode == "fresh"


def test_generate_route_can_start_follow_up_schedule() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/channels/ch1/generate?mode=follow_up")

    assert response.status_code == 200
    assert "Follow-up generation started" in response.text
    assert core.job_manager.generation_mode == "follow_up"


def test_generate_route_can_use_explicit_follow_up_parent() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/channels/ch1/generate?mode=follow_up", data={
        "parent_version": "2",
    })

    assert response.status_code == 200
    assert "Follow-up generation after version 2 started" in response.text
    assert core.job_manager.generation_mode == "follow_up"
    assert core.job_manager.parent_version == 2


def test_channel_page_shows_follow_up_context() -> None:
    core = Core()
    client = make_client(core)

    response = client.get("/channels/ch1")

    assert response.status_code == 200
    assert "Next follow-up starts after version 3" in response.text
    assert 'name="parent_version"' in response.text
    assert "Version 3 / uploaded / ends" in response.text
    assert "Next start:" in response.text
    assert "Reserved before next start:" in response.text
    assert "1 episodes" in response.text


def test_channel_job_status_reports_recent_job() -> None:
    core = Core()
    client = make_client(core)

    response = client.get("/channels/ch1/job-status")

    assert response.status_code == 200
    assert "completed" in response.text
    assert "/schedules/ch1/preview/2" in response.text


def test_channel_job_status_reports_active_job() -> None:
    core = Core()
    core.job_manager.running = True
    client = make_client(core)

    response = client.get("/channels/ch1/job-status")

    assert response.status_code == 200
    assert "running" in response.text
    assert "validator" in response.text
    assert "Cancel" in response.text


def test_generate_route_reports_disabled_channel() -> None:
    core = Core()
    core.config_manager.config().channels[0].scheduling_enabled = False
    client = make_client(core)

    response = client.post("/channels/ch1/generate")

    assert response.status_code == 409
    assert "scheduling disabled" in response.text.lower()
    assert core.job_manager.started is False


def test_cancel_route_requests_running_job_cancellation() -> None:
    core = Core()
    core.job_manager.running = True
    client = make_client(core)

    assert client.get("/channels/ch1/cancel").status_code == 405

    response = client.post("/channels/ch1/cancel")

    assert response.status_code == 200
    assert core.job_manager.cancelled is True
    assert core.job_manager.running is False


def test_cancel_route_reports_no_running_job() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/channels/ch1/cancel")

    assert response.status_code == 404
    assert "No generation in progress" in response.text


def test_toggle_route_is_post() -> None:
    core = Core()
    client = make_client(core)

    assert client.get("/channels/ch1/toggle").status_code == 405

    response = client.post("/channels/ch1/toggle")

    assert response.status_code == 200
    assert core.config_manager.config().channels[0].scheduling_enabled is False
    assert core.config_manager.saved is True


def test_channel_config_editor_serializes_enum_values() -> None:
    core = Core()
    now = datetime.now(tz=UTC)
    core.playlist_repo.playlists["playlist-1"] = Playlist(
        id="playlist-1",
        name="Morning Playlist",
        items=[],
        created_at=now,
        updated_at=now,
    )
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="morning",
            days=[DayOfWeek.MON],
            start_time="06:00",
            end_time="12:00",
        ),
    ]
    core.config_manager.config().channels[0].ads = AdsConfig(
        enabled=True,
        filler_list_id="filler-1",
        ad_density=0.1,
        max_total_minutes=10,
        min_ad_break_duration_minutes=1,
        max_ad_break_duration_minutes=3,
    )
    client = make_client(core)

    response = client.get("/channels/ch1/config")

    assert response.status_code == 200
    assert 'value="mon"' in response.text
    assert "checked" in response.text
    assert "Morning Shows" in response.text
    assert "Station IDs" in response.text
    assert 'id="add-daypart"' in response.text
    assert "remove-daypart" in response.text
    assert "move-daypart-up" in response.text
    assert "duplicate-daypart" in response.text
    assert "toggle-daypart-body" in response.text
    assert "daypart-summary" in response.text
    assert "copy-daypart-select" in response.text
    assert "copy-daypart-sources" in response.text
    assert "copy-daypart-days" in response.text
    assert "daypart-warnings" in response.text
    assert "Estimated Target" in response.text
    assert "Max Total" in response.text
    assert "Filler Fit" in response.text
    assert "0.5-2.0m spots" in response.text
    assert "help-dot" in response.text
    assert "Approximate ad share" in response.text
    assert "option-list" in response.text
    assert 'name="daypart_playlist_ids_0"' in response.text
    assert 'name="daypart_custom_show_list_ids_0"' in response.text
    assert 'name="daypart_variable_movie_duration_0"' in response.text
    assert 'name="daypart_end_tolerance_minutes_0"' in response.text
    assert 'id="standby_custom_show_id"' in response.text


def test_channel_config_editor_warns_about_impossible_daypart_settings() -> None:
    core = Core()
    core.config_manager.config().channels[0].dayparts = [
        DaypartTemplate(
            name="overnight",
            days=[DayOfWeek.MON],
            start_time="02:00",
            end_time="03:00",
            slot_duration_minutes=90,
            allow_movies=False,
            movie_slot_count=1,
            variable_movie_duration=True,
            end_tolerance_minutes=60,
            off_air=True,
        ),
    ]
    client = make_client(core)

    response = client.get("/channels/ch1/config")

    assert response.status_code == 200
    assert "overnight slot duration is longer than the daypart window" in response.text
    assert "overnight has movie slots but movies are disabled" in response.text
    assert "overnight has variable movie timing enabled but movies are disabled" in response.text
    assert "overnight end tolerance is longer than the daypart window" in response.text
    assert "overnight is off-air but has no custom show loop selected" in response.text


def test_channel_config_editor_saves_nested_config() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/channels/ch1/config", data={
        "name": "Edited Channel",
        "scheduling_enabled": "on",
        "schedule_horizon_days": "3",
        "standby_custom_show_id": "custom-morning",
        "dayparts_yaml": """
- name: morning
  days: [mon]
  start_time: "06:00"
  end_time: "12:00"
  rotation: default
  custom_show_list_ids: ["custom-morning"]
  slot_duration_minutes: 30
  allow_movies: true
  variable_movie_duration: true
  end_tolerance_minutes: 15
""",
        "rotations_yaml": """
- name: default
  show_ids: ["show-1", "show-2"]
""",
        "ads_yaml": """
enabled: true
filler_list_id: filler-1
ad_density: 0.1
break_after_programs: 2
max_total_minutes: 5
max_ad_break_duration_minutes: 3
min_ad_break_duration_minutes: 1
""",
        "continuity_enabled": "on",
        "continuity_frequency": "3",
        "continuity_station_id_custom_show_id": "station-list",
        "continuity_bumper_custom_show_id": "",
        "continuity_station_id_clip_ids": "station-a, station-b",
        "continuity_bumper_clip_ids": "bumper-a",
        "pipeline_text": "daypart_applicator\nvalidator\nschedule_persister",
    })

    channel = core.config_manager.config().channels[0]
    assert response.status_code == 200
    assert channel.name == "Edited Channel"
    assert channel.schedule_horizon_days == 3
    assert channel.standby_custom_show_id == "custom-morning"
    assert channel.dayparts[0].days == [DayOfWeek.MON]
    assert channel.dayparts[0].allow_movies is True
    assert channel.dayparts[0].variable_movie_duration is True
    assert channel.dayparts[0].end_tolerance_minutes == 15
    assert channel.dayparts[0].custom_show_list_ids == ["custom-morning"]
    assert channel.rotations[0].show_ids == ["show-1", "show-2"]
    assert channel.ads.filler_list_id == "filler-1"
    assert channel.ads.break_after_programs == 2
    assert channel.ads.max_total_minutes == 5
    assert channel.continuity.frequency == 3
    assert channel.continuity.station_id_custom_show_id == "station-list"
    assert channel.continuity.station_id_clip_ids == ["station-a", "station-b"]
    assert channel.continuity.bumper_clip_ids == ["bumper-a"]
    assert channel.pipeline == ["daypart_applicator", "validator", "schedule_persister"]
    assert core.config_manager.saved is True


def test_channel_config_editor_rejects_invalid_yaml_shape() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/channels/ch1/config", data={
        "name": "Edited Channel",
        "schedule_horizon_days": "3",
        "dayparts_yaml": "not: a list",
        "rotations_yaml": "[]",
        "ads_yaml": "{}",
        "pipeline_text": "validator",
    })

    assert response.status_code == 400
    assert "Invalid channel config. Check the submitted fields and YAML sections." in response.text


def test_schedule_preview_uses_saved_version() -> None:
    core = Core()
    timeline = Timeline(metadata={
        "ad_target_seconds": 300,
        "ad_inserted_seconds": 180,
        "ad_warnings": ["Ad target could not be fully reached."],
        "ad_rotation_summary": {
            "spot_count": 5,
            "unique_spots_used": 3,
            "break_count": 1,
            "poor_fit_count": 1,
            "generic_break_count": 0,
        },
    })
    timeline.insert(EpisodeBlock(
        start_time=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 5, 28, 12, 30, tzinfo=UTC),
        duration=timedelta(minutes=30),
        episode_id="episode-1",
        show_id="show-1",
        season_number=1,
        episode_number=1,
        runtime_seconds=1800,
        metadata={"title": "Pilot", "show_name": "Series One", "daypart": "morning"},
    ))
    timeline.insert(AdBlock(
        start_time=datetime(2026, 5, 28, 12, 30, tzinfo=UTC),
        end_time=datetime(2026, 5, 28, 12, 33, tzinfo=UTC),
        duration=timedelta(minutes=3),
        ad_count=4,
        total_duration_seconds=180,
        metadata={"daypart": "morning", "filler_list_id": "ads"},
    ))
    timeline.insert(OfflineBlock(
        start_time=datetime(2026, 5, 28, 12, 33, tzinfo=UTC),
        end_time=datetime(2026, 5, 28, 13, 0, tzinfo=UTC),
        duration=timedelta(minutes=27),
        reason="standby",
        metadata={"daypart": "overnight", "custom_show_list_ids": ["standby-list"]},
    ))
    core.state.versions[("ch1", 1)]["timeline_json"] = json.dumps(timeline.snapshot())
    client = make_client(core)

    response = client.get("/schedules/ch1/preview/1")

    assert response.status_code == 200
    assert "Schedule Preview - Channel" in response.text
    assert "Back to Versions" in response.text
    assert "Back to Channel" in response.text
    assert "preview-table" in response.text
    assert "preview-summary" in response.text
    assert 'id="preview-type-filter"' in response.text
    assert 'id="preview-daypart-filter"' in response.text
    assert 'id="preview-hour-filter"' in response.text
    assert 'id="preview-status-filter"' in response.text
    assert 'id="preview-group-mode"' in response.text
    assert 'data-preview-type="ad"' in response.text
    assert 'data-preview-status="ok"' in response.text
    assert 'data-preview-hour="' in response.text
    assert "target 5m" in response.text
    assert "Ad target below plan" in response.text
    assert "Ad Rotation" in response.text
    assert "3/5" in response.text
    assert "1 fit warnings" in response.text
    assert "Ad target could not be fully reached" in response.text
    assert "Off-Air Loop" in response.text
    assert "Custom show loop" in response.text


def test_approve_schedule_marks_version_approved() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/schedules/ch1/1/approve")

    assert response.status_code == 200
    assert core.state.versions[("ch1", 1)]["status"] == "approved"
    assert "Approved schedule version 1" in response.text
    assert 'id="schedule-table"' in response.text
    assert 'id="schedule-versions"' in response.text
    assert 'hx-swap-oob="outerHTML"' in response.text
    assert "text-bg-success" in response.text
    assert "text-bg-primary" in response.text
    assert 'hx-disabled-elt="closest tr button"' in response.text
    assert "approved" in response.text


def test_approve_schedule_blocks_sanity_errors() -> None:
    core = Core()
    timeline = Timeline(metadata={"validation_errors": ["duplicate station id"]})
    timeline.insert(EpisodeBlock(
        start_time=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 5, 28, 12, 30, tzinfo=UTC),
        duration=timedelta(minutes=30),
        episode_id="episode-1",
        show_id="show-1",
        season_number=1,
        episode_number=1,
        runtime_seconds=1800,
    ))
    core.state.versions[("ch1", 1)]["timeline_json"] = json.dumps(timeline.snapshot())
    client = make_client(core)

    response = client.post("/schedules/ch1/1/approve")

    assert response.status_code == 409
    assert core.state.versions[("ch1", 1)]["status"] == "draft"
    assert "Schedule cannot be approved" in response.text
    assert "duplicate station id" in response.text


def test_schedule_list_color_codes_status_badges() -> None:
    core = Core()
    client = make_client(core)

    response = client.get("/schedules/ch1")

    assert response.status_code == 200
    assert "text-bg-warning" in response.text
    assert "text-bg-primary" in response.text
    assert "text-bg-success" in response.text


def test_schedule_list_has_diff_cleanup_bulk_delete_and_rollback_confirm() -> None:
    core = Core()
    client = make_client(core)

    response = client.get("/schedules/ch1")

    assert response.status_code == 200
    assert "Version Tools" in response.text
    assert "/schedules/ch1/diff" in response.text
    assert "/schedules/ch1/cleanup" in response.text
    assert "/schedules/ch1/bulk-delete" in response.text
    assert "Create a new draft by rolling back to schedule version" in response.text


def test_upload_schedule_requires_approved_version() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/schedules/ch1/1/upload")

    assert response.status_code == 409
    assert "Only approved or previously uploaded schedules can be uploaded" in response.text
    assert 'id="schedule-table"' in response.text
    assert 'hx-swap-oob="outerHTML"' in response.text
    assert core.tunarr_client.uploads == []


def test_upload_schedule_sends_approved_version_to_tunarr() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/schedules/ch1/2/upload")

    assert response.status_code == 200
    assert core.state.versions[("ch1", 2)]["status"] == "uploaded"
    assert len(core.tunarr_client.uploads) == 1
    assert "Uploaded schedule version 2" in response.text
    assert '<div class="upload-result">' in response.text
    assert "&lt;div class=&quot;upload-result&quot;&gt;" not in response.text
    assert "Persistent time" in response.text
    assert "not attempted" in response.text
    assert "Fallback used" in response.text
    assert "final 200, programming 200, channel 200" in response.text
    assert "Lineup entries" in response.text
    assert "Schedule table refreshed" in response.text
    assert 'id="schedule-table"' in response.text
    assert 'hx-swap-oob="outerHTML"' in response.text
    assert "uploaded" in response.text
    assert core.state.upload_attempts[0]["status"] == "success"
    assert core.state.upload_attempts[0]["schedule_version"] == 2
    assert core.audit_repo.events[0]["action"] == "schedule.upload"
    assert core.audit_repo.events[0]["channel_id"] == "ch1"
    assert core.audit_repo.events[0]["schedule_version"] == 2


def test_upload_schedule_can_retry_uploaded_version() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/schedules/ch1/3/upload")

    assert response.status_code == 200
    assert core.state.versions[("ch1", 3)]["status"] == "uploaded"
    assert len(core.tunarr_client.uploads) == 1
    assert "Uploaded schedule version 3" in response.text
    assert "Re-upload" in response.text


def test_upload_schedule_reports_tunarr_failure_inline() -> None:
    core = Core()
    core.tunarr_client.fail_upload = True
    client = make_client(core)

    response = client.post("/schedules/ch1/2/upload")

    assert response.status_code == 200
    assert "Tunarr rejected upload (404)." in response.text
    assert core.state.versions[("ch1", 2)]["status"] == "approved"
    assert core.state.upload_attempts[0]["status"] == "failed"
    assert "Tunarr rejected upload" in str(core.state.upload_attempts[0]["message"])
    assert "Not Found" in str(core.state.upload_attempts[0]["message"])


def test_upload_schedule_masks_upstream_error_body() -> None:
    core = Core()
    core.tunarr_client.fail_upload = True
    core.tunarr_client.fail_upload_text = '<script>alert("x")</script>'
    client = make_client(core)

    response = client.post("/schedules/ch1/2/upload")

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "Check upload history or server logs" in response.text
    assert "<script>" in str(core.state.upload_attempts[0]["message"])


def test_upload_history_page_lists_attempts() -> None:
    core = Core()
    core.state.upload_attempts.append({
        "id": "attempt-1",
        "channel_id": "ch1",
        "schedule_version": 2,
        "status": "success",
        "message": "Uploaded schedule version.",
        "details": {"mode": "manual", "final_status": 200, "fallback_used": False},
        "details_json": "{}",
        "created_at": "2026-05-28T15:00:00+00:00",
    })
    client = make_client(core)

    response = client.get("/uploads")

    assert response.status_code == 200
    assert "Upload History" in response.text
    assert "Channel" in response.text
    assert "Uploaded schedule version." in response.text
    assert "mode=manual" in response.text


def test_reject_schedule_marks_version_rejected() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/schedules/ch1/1/reject")

    assert response.status_code == 200
    assert core.state.versions[("ch1", 1)]["status"] == "rejected"


def test_reject_uploaded_schedule_is_blocked() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/schedules/ch1/3/reject")

    assert response.status_code == 409
    assert core.state.versions[("ch1", 3)]["status"] == "uploaded"
    assert "Uploaded schedules cannot be rejected" in response.text
    assert 'id="schedule-table"' in response.text


def test_rollback_schedule_creates_new_draft() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/schedules/ch1/2/rollback")

    assert response.status_code == 200
    assert core.state.versions[("ch1", 4)]["status"] == "draft"
    assert core.state.versions[("ch1", 4)]["parent_version"] == 2


def test_delete_schedule_removes_non_uploaded_version() -> None:
    core = Core()
    client = make_client(core)

    response = client.delete("/schedules/ch1/1")

    assert response.status_code == 200
    assert ("ch1", 1) not in core.state.versions


def test_delete_uploaded_schedule_removes_version() -> None:
    core = Core()
    client = make_client(core)

    response = client.delete("/schedules/ch1/3")

    assert response.status_code == 200
    assert ("ch1", 3) not in core.state.versions
    assert "Deleted schedule version 3" in response.text
    assert 'id="schedule-table"' in response.text


def test_schedule_diff_compares_two_versions() -> None:
    core = Core()
    timeline = Timeline()
    timeline.insert(EpisodeBlock(
        start_time=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 5, 28, 12, 45, tzinfo=UTC),
        duration=timedelta(minutes=45),
        episode_id="episode-2",
        show_id="show-1",
        season_number=1,
        episode_number=2,
        runtime_seconds=2700,
        metadata={"title": "Second", "show_name": "Series One"},
    ))
    core.state.versions[("ch1", 2)]["timeline_json"] = json.dumps(timeline.snapshot())
    client = make_client(core)

    response = client.get("/schedules/ch1/diff?from=1&to=2")

    assert response.status_code == 200
    assert "Schedule Diff - Channel" in response.text
    assert "Added Media" in response.text
    assert "Removed Media" in response.text
    assert "episode-2" in response.text
    assert "episode-1" in response.text


def test_bulk_delete_removes_selected_versions_including_uploaded() -> None:
    core = Core()
    client = make_client(core)

    response = client.post(
        "/schedules/ch1/bulk-delete",
        data={"selected_versions": ["1", "2", "3"]},
    )

    assert response.status_code == 200
    assert ("ch1", 1) not in core.state.versions
    assert ("ch1", 2) not in core.state.versions
    assert ("ch1", 3) not in core.state.versions
    assert "Deleted 3 schedule version(s)." in response.text


def test_cleanup_schedule_versions_keeps_uploaded_unless_selected() -> None:
    core = Core()
    for version in range(4, 9):
        core.state.versions[("ch1", version)] = {
            **core.state.versions[("ch1", 1)],
            "id": f"version-{version}",
            "version": version,
            "status": "invalid" if version % 2 == 0 else "draft",
            "created_at": f"2026-05-28T1{version}:00:00+00:00",
        }
    client = make_client(core)

    response = client.post(
        "/schedules/ch1/cleanup",
        data={
            "keep_latest": "2",
            "cleanup_statuses": ["draft", "invalid", "approved"],
        },
    )

    assert response.status_code == 200
    assert ("ch1", 8) in core.state.versions
    assert ("ch1", 7) in core.state.versions
    assert ("ch1", 6) not in core.state.versions
    assert ("ch1", 3) in core.state.versions
    assert "Cleanup deleted" in response.text


def test_cleanup_schedule_versions_can_delete_uploaded_when_selected() -> None:
    core = Core()
    for version in range(4, 9):
        core.state.versions[("ch1", version)] = {
            **core.state.versions[("ch1", 3)],
            "id": f"version-{version}",
            "version": version,
            "status": "uploaded",
            "created_at": f"2026-05-28T1{version}:00:00+00:00",
        }
    client = make_client(core)

    response = client.post(
        "/schedules/ch1/cleanup",
        data={
            "keep_latest": "2",
            "cleanup_statuses": ["uploaded"],
        },
    )

    assert response.status_code == 200
    assert ("ch1", 8) in core.state.versions
    assert ("ch1", 7) in core.state.versions
    assert ("ch1", 6) not in core.state.versions
    assert ("ch1", 3) not in core.state.versions
    assert "Cleanup deleted" in response.text


def test_job_history_links_schedule_version() -> None:
    client = make_client(Core())

    response = client.get("/jobs")

    assert response.status_code == 200
    assert "/schedules/ch1/preview/2" in response.text


def test_health_checks_database_when_available() -> None:
    app = create_app(Core())
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["checks"]["database"] is True


def test_ready_checks_dependencies() -> None:
    app = create_app(Core())
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["checks"] == {
        "database": True,
        "jellyfin": True,
        "tunarr": True,
    }


def test_metrics_endpoint_renders_prometheus_text() -> None:
    core = Core()
    core.metrics.record_generation("ch1", "completed")
    client = make_client(core)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "tunarr_generation_total" in response.text
    assert 'channel_id="ch1"' in response.text


def test_manual_media_sync_endpoint() -> None:
    core = Core()
    client = make_client(core)

    response = client.post("/api/media/sync")

    assert response.status_code == 200
    assert response.json()["new_episodes"] == 1
    assert core.media_sync.synced is True


def test_jellyfin_media_webhook_triggers_read_only_media_sync_without_login() -> None:
    core = Core()
    app = create_app(core)
    client = TestClient(app)

    response = client.post(
        "/api/webhooks/jellyfin/media",
        json={"ItemId": "abc", "NotificationType": "ItemUpdated"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["sync"]["item_id"] == "abc"
    assert response.json()["sync"]["status"] == "updated"
    assert core.media_sync.targeted_item_id == "abc"
    assert core.media_sync.targeted_event_name == "ItemUpdated"
    assert core.media_sync.synced is False


def test_jellyfin_media_webhook_ignores_payload_without_item_id() -> None:
    core = Core()
    app = create_app(core)
    client = TestClient(app)

    response = client.post("/api/webhooks/jellyfin/media", json={"Name": "No item"})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "missing_item_id"
    assert core.media_sync.targeted_item_id is None
    assert core.media_sync.synced is False
