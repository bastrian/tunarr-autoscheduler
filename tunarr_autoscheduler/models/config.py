from __future__ import annotations

from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator


class DayOfWeek(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


class DaypartTemplate(BaseModel):
    name: str
    days: list[DayOfWeek]
    start_time: str  # "HH:MM"
    end_time: str  # "HH:MM"
    content_mode: str = "series"
    rotation: str = "default"
    custom_show_list_ids: list[str] = Field(default_factory=list)
    playlist_ids: list[str] = Field(default_factory=list)
    slot_duration_minutes: int = 30
    allow_movies: bool = False
    variable_movie_duration: bool = False
    movie_selection: str = "best_fit"
    movie_slot_count: int = 0
    end_tolerance_minutes: int = 0
    ad_density: float = 0.08
    continuity_frequency: int = 4
    off_air: bool = False


class RotationConfig(BaseModel):
    name: str
    show_ids: list[str] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)
    marathon_mode: bool = False
    max_consecutive_episodes: int = 2


class ApprovalConfig(BaseModel):
    required: bool = True
    roles_that_can_approve: list[str] = Field(default_factory=lambda: ["admin", "programmer"])
    auto_approve_channels: list[str] = Field(default_factory=list)


class AutomaticFollowUpConfig(BaseModel):
    enabled: bool = False
    auto_approve: bool = False
    auto_upload: bool = False
    warning_hours: int = 12


class AdsConfig(BaseModel):
    enabled: bool = True
    filler_list_id: str = ""
    ad_density: float = 0.08
    break_after_programs: int = 1
    min_total_minutes: int = 0
    max_total_minutes: int = 0
    max_ad_break_duration_minutes: int = 5
    min_ad_break_duration_minutes: int = 1


class ContinuityConfig(BaseModel):
    enabled: bool = True
    frequency: int = 4
    station_id_custom_show_id: str = ""
    bumper_custom_show_id: str = ""
    station_id_clip_ids: list[str] = Field(default_factory=list)
    bumper_clip_ids: list[str] = Field(default_factory=list)


class HumanizerConfig(BaseModel):
    enabled: bool = True
    jitter_seconds: int = 15
    tolerance_seconds: int = 30


class ChannelConfig(BaseModel):
    id: str
    name: str = ""
    channel_profile: str = "advanced"
    scheduling_enabled: bool = False
    public_epg_enabled: bool = True
    public_epg_order: int = 100
    public_epg_logo_url: str = ""
    dayparts: list[DaypartTemplate] = Field(default_factory=list)
    rotations: list[RotationConfig] = Field(default_factory=list)
    ads: AdsConfig = Field(default_factory=AdsConfig)
    continuity: ContinuityConfig = Field(default_factory=ContinuityConfig)
    humanizer: HumanizerConfig = Field(default_factory=HumanizerConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    automatic_follow_up: AutomaticFollowUpConfig = Field(
        default_factory=AutomaticFollowUpConfig,
    )
    schedule_horizon_days: int = 1
    standby_custom_show_id: str = ""
    pipeline: list[str] = Field(default_factory=lambda: [
        "daypart_applicator",
        "rotation_scheduler",
        "movie_scheduler",
        "continuity_inserter",
        "ad_inserter",
        "gap_filler",
        "humanizer",
        "validator",
        "schedule_persister",
        "html_preview",
    ])


class JellyfinConfig(BaseModel):
    url: str = "http://jellyfin:8096"
    api_key: str = "YOUR_JELLYFIN_API_KEY"
    user_id: str = "YOUR_JELLYFIN_USER_ID"
    sync_interval_minutes: int = 15


class TunarrConfig(BaseModel):
    url: str = "http://tunarr:8000"


class MetadataConfig(BaseModel):
    tmdb_enabled: bool = False
    tmdb_api_key: str = ""
    tmdb_language: str = "de-DE"
    tvdb_enabled: bool = False
    tvdb_api_key: str = ""
    omdb_enabled: bool = False
    omdb_api_key: str = ""
    jellystat_enabled: bool = False
    jellystat_url: str = ""
    jellystat_api_token: str = ""
    jellystat_days: int = 90
    jellystat_activity_weight: int = 10
    jellystat_completion_weight: int = 8
    jellystat_trend_weight: int = 8
    jellystat_genre_trend_weight: int = 6
    jellystat_underused_weight: int = 6
    jellystat_stale_weight: int = 4
    tmdb_rate_limit_per_minute: int = 120
    tvdb_rate_limit_per_minute: int = 60
    omdb_rate_limit_per_minute: int = 60
    jellystat_rate_limit_per_minute: int = 30
    cache_ttl_days: int = 14


class TelegramNotificationConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class EmailNotificationConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    username: str = ""
    password: str = ""
    from_address: str = ""
    to_addresses: list[str] = Field(default_factory=list)
    use_tls: bool = True


class WebhookNotificationConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)


class NotificationRuleConfig(BaseModel):
    event_type: str = "*"
    enabled: bool = True
    providers: list[str] = Field(default_factory=list)
    throttle_minutes: int = 30
    quiet_hours_start: str = ""
    quiet_hours_end: str = ""


class NotificationsConfig(BaseModel):
    enabled: bool = False
    telegram: TelegramNotificationConfig = Field(default_factory=TelegramNotificationConfig)
    email: EmailNotificationConfig = Field(default_factory=EmailNotificationConfig)
    webhook: WebhookNotificationConfig = Field(default_factory=WebhookNotificationConfig)
    rules: list[NotificationRuleConfig] = Field(default_factory=lambda: [
        NotificationRuleConfig(
            event_type="upload_failed",
            providers=["telegram", "email", "webhook"],
            throttle_minutes=15,
        ),
        NotificationRuleConfig(
            event_type="generation_failed",
            providers=["telegram", "email"],
            throttle_minutes=15,
        ),
        NotificationRuleConfig(
            event_type="schedule_invalid",
            providers=["telegram", "email"],
            throttle_minutes=30,
        ),
        NotificationRuleConfig(
            event_type="schedule_expiring_soon",
            providers=["telegram", "email"],
            throttle_minutes=240,
        ),
        NotificationRuleConfig(
            event_type="follow_up_missing",
            providers=["telegram"],
            throttle_minutes=240,
        ),
        NotificationRuleConfig(
            event_type="auto_follow_up_failed",
            providers=["telegram", "email"],
            throttle_minutes=60,
        ),
        NotificationRuleConfig(
            event_type="jellyfin_sync_failed",
            providers=["telegram", "email"],
            throttle_minutes=60,
        ),
        NotificationRuleConfig(
            event_type="tunarr_connectivity_failed",
            providers=["telegram", "email"],
            throttle_minutes=60,
        ),
        NotificationRuleConfig(
            event_type="backup_failed",
            providers=["telegram", "email"],
            throttle_minutes=60,
        ),
    ])


class ScheduleMonitorConfig(BaseModel):
    enabled: bool = True
    interval_minutes: int = 30
    warning_hours: int = 12


class BackupConfig(BaseModel):
    enabled: bool = False
    interval_hours: int = 24
    output_dir: str = "~/.tunarr/backups"
    retention_count: int = 7
    min_free_mb: int = 1024
    size_multiplier: int = 3


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"
    log_file: str = "~/.tunarr/logs/scheduler.log"


class DatabaseConfig(BaseModel):
    url: str = "sqlite+aiosqlite:///~/.tunarr/scheduler.db"


class PluginsConfig(BaseModel):
    directories: list[str] = Field(default_factory=lambda: ["~/.tunarr/plugins"])
    disabled: list[str] = Field(default_factory=list)


class AuthConfig(BaseModel):
    username: str = "admin"
    password_hash: str = ""
    session_secret: str = "YOUR_SESSION_SECRET"


class PublicAccessConfig(BaseModel):
    epg: Literal["disabled", "jellyfin_login", "public"] = "public"


class AppConfig(BaseModel):
    timezone: str = "Europe/Berlin"
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    jellyfin: JellyfinConfig = Field(default_factory=JellyfinConfig)
    tunarr: TunarrConfig = Field(default_factory=TunarrConfig)
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    schedule_monitor: ScheduleMonitorConfig = Field(default_factory=ScheduleMonitorConfig)
    backups: BackupConfig = Field(default_factory=BackupConfig)
    public_access: PublicAccessConfig = Field(default_factory=PublicAccessConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    channels: list[ChannelConfig] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = value.strip() or "UTC"
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"Unknown timezone: {value}") from e
        return value
