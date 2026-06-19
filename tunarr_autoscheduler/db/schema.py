from __future__ import annotations

from tunarr_autoscheduler.db.database import Database

SCHEMA_VERSION = 10

CREATE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS channels (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        config_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rotation_state (
        id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL REFERENCES channels(id),
        rotation_name TEXT NOT NULL,
        current_index INTEGER NOT NULL DEFAULT 0,
        current_show_id TEXT,
        episode_counter INTEGER NOT NULL DEFAULT 0,
        last_rotation_time TEXT,
        UNIQUE(channel_id, rotation_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS air_history (
        id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL REFERENCES channels(id),
        item_id TEXT NOT NULL,
        item_type TEXT NOT NULL,
        aired_at TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL,
        show_id TEXT,
        schedule_version INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cooldowns (
        item_id TEXT PRIMARY KEY,
        item_type TEXT NOT NULL,
        cooldown_until TEXT NOT NULL,
        channel_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schedule_items (
        id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL REFERENCES channels(id),
        version INTEGER NOT NULL,
        status TEXT NOT NULL,
        block_type TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        approved_by TEXT,
        approved_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schedule_versions (
        id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        status TEXT NOT NULL,
        timeline_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        parent_version INTEGER,
        UNIQUE(channel_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generation_jobs (
        id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        status TEXT NOT NULL,
        current_stage TEXT,
        error_message TEXT,
        checkpoint_id TEXT,
        schedule_version_id TEXT,
        started_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS media_cache (
        id TEXT PRIMARY KEY,
        item_type TEXT NOT NULL,
        source_type TEXT NOT NULL,
        source_id TEXT NOT NULL,
        title TEXT NOT NULL,
        duration_seconds INTEGER,
        metadata_json TEXT,
        last_synced_at TEXT NOT NULL,
        available INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        generation_id TEXT NOT NULL,
        stage_name TEXT NOT NULL,
        timeline_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlists (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        category_id TEXT NOT NULL DEFAULT '',
        channel_scope TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_categories (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        description TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_tags (
        playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        tag TEXT NOT NULL,
        PRIMARY KEY (playlist_id, tag)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_items (
        playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        media_type TEXT NOT NULL,
        media_id TEXT NOT NULL,
        title TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (playlist_id, media_type, media_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS upload_attempts (
        id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        schedule_version INTEGER NOT NULL,
        status TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        details_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recommendation_profiles (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        description TEXT NOT NULL DEFAULT '',
        profile_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS external_metadata_cache (
        provider TEXT NOT NULL,
        media_type TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (provider, media_type, provider_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_events (
        id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        provider TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        details_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recommendation_runs (
        id TEXT PRIMARY KEY,
        run_type TEXT NOT NULL,
        title TEXT NOT NULL,
        status TEXT NOT NULL,
        request_json TEXT NOT NULL DEFAULT '{}',
        result_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        applied_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id TEXT PRIMARY KEY,
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        channel_id TEXT NOT NULL DEFAULT '',
        schedule_version INTEGER,
        target_type TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT '',
        details_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
]


async def run_migrations(db: Database) -> None:
    current_version = await _get_current_version(db)

    if current_version is None:
        for table_sql in CREATE_TABLES:
            await db.execute(table_sql)
        await db.execute("INSERT INTO schema_version (version) VALUES (?)", SCHEMA_VERSION)
    elif current_version < SCHEMA_VERSION:
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            migration = _get_migration(version)
            if migration:
                for sql in migration:
                    await db.execute(sql)
        await db.execute("UPDATE schema_version SET version = ?", SCHEMA_VERSION)


async def _get_current_version(db: Database) -> int | None:
    try:
        row = await db.fetch_one("SELECT version FROM schema_version")
        return row["version"] if row else None
    except Exception:
        return None


def _get_migration(version: int) -> list[str] | None:
    migrations: dict[int, list[str]] = {
        2: [
            "ALTER TABLE generation_jobs ADD COLUMN schedule_version_id TEXT",
        ],
        3: [
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS playlist_items (
                playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                media_type TEXT NOT NULL,
                media_id TEXT NOT NULL,
                title TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (playlist_id, media_type, media_id)
            )
            """,
        ],
        4: [
            "ALTER TABLE playlists ADD COLUMN category_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE playlists ADD COLUMN channel_scope TEXT NOT NULL DEFAULT ''",
            """
            CREATE TABLE IF NOT EXISTS playlist_categories (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS playlist_tags (
                playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                PRIMARY KEY (playlist_id, tag)
            )
            """,
        ],
        5: [
            """
            CREATE TABLE IF NOT EXISTS upload_attempts (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                schedule_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
        ],
        6: [
            """
            CREATE TABLE IF NOT EXISTS recommendation_profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                description TEXT NOT NULL DEFAULT '',
                profile_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ],
        7: [
            """
            CREATE TABLE IF NOT EXISTS external_metadata_cache (
                provider TEXT NOT NULL,
                media_type TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (provider, media_type, provider_id)
            )
            """,
        ],
        8: [
            """
            CREATE TABLE IF NOT EXISTS notification_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
        ],
        9: [
            """
            CREATE TABLE IF NOT EXISTS recommendation_runs (
                id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                applied_at TEXT
            )
            """,
        ],
        10: [
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                channel_id TEXT NOT NULL DEFAULT '',
                schedule_version INTEGER,
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
        ],
    }
    return migrations.get(version)
