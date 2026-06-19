from __future__ import annotations

import tempfile

from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.db.schema import SCHEMA_VERSION, run_migrations


async def test_initial_schema_has_job_schedule_version_link() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            row = await db.fetch_one("SELECT version FROM schema_version")
            columns = await db.fetch_all("PRAGMA table_info(generation_jobs)")
            playlist_columns = await db.fetch_all("PRAGMA table_info(playlists)")
            item_columns = await db.fetch_all("PRAGMA table_info(playlist_items)")
            category_columns = await db.fetch_all("PRAGMA table_info(playlist_categories)")
            tag_columns = await db.fetch_all("PRAGMA table_info(playlist_tags)")
            upload_columns = await db.fetch_all("PRAGMA table_info(upload_attempts)")
            profile_columns = await db.fetch_all("PRAGMA table_info(recommendation_profiles)")
            external_cache_columns = await db.fetch_all(
                "PRAGMA table_info(external_metadata_cache)",
            )
            notification_columns = await db.fetch_all(
                "PRAGMA table_info(notification_events)",
            )
        finally:
            await db.disconnect()

    assert row is not None
    assert row["version"] == SCHEMA_VERSION
    assert "schedule_version_id" in {column["name"] for column in columns}
    assert {"category_id", "channel_scope"} <= {
        column["name"] for column in playlist_columns
    }
    assert {"playlist_id", "media_type", "media_id", "position"} <= {
        column["name"] for column in item_columns
    }
    assert {"id", "name"} <= {column["name"] for column in category_columns}
    assert {"playlist_id", "tag"} <= {column["name"] for column in tag_columns}
    assert {"channel_id", "schedule_version", "status", "details_json"} <= {
        column["name"] for column in upload_columns
    }
    assert {"id", "name", "profile_json"} <= {
        column["name"] for column in profile_columns
    }
    assert {"provider", "provider_id", "payload_json", "expires_at"} <= {
        column["name"] for column in external_cache_columns
    }
    assert {"event_type", "provider", "status", "details_json"} <= {
        column["name"] for column in notification_columns
    }


async def test_migration_adds_job_schedule_version_link() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            await db.execute("INSERT INTO schema_version (version) VALUES (1)")
            await db.execute(
                "CREATE TABLE generation_jobs ("
                "id TEXT PRIMARY KEY, "
                "channel_id TEXT NOT NULL, "
                "status TEXT NOT NULL, "
                "current_stage TEXT, "
                "error_message TEXT, "
                "checkpoint_id TEXT, "
                "started_at TEXT NOT NULL, "
                "completed_at TEXT)"
            )

            await run_migrations(db)
            row = await db.fetch_one("SELECT version FROM schema_version")
            columns = await db.fetch_all("PRAGMA table_info(generation_jobs)")
        finally:
            await db.disconnect()

    assert row is not None
    assert row["version"] == SCHEMA_VERSION
    assert "schedule_version_id" in {column["name"] for column in columns}


async def test_migration_adds_playlist_tables() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            await db.execute("INSERT INTO schema_version (version) VALUES (2)")
            await run_migrations(db)
            row = await db.fetch_one("SELECT version FROM schema_version")
            playlist_columns = await db.fetch_all("PRAGMA table_info(playlists)")
            item_columns = await db.fetch_all("PRAGMA table_info(playlist_items)")
        finally:
            await db.disconnect()

    assert row is not None
    assert row["version"] == SCHEMA_VERSION
    assert "name" in {column["name"] for column in playlist_columns}
    assert "category_id" in {column["name"] for column in playlist_columns}
    assert "position" in {column["name"] for column in item_columns}


async def test_migration_adds_playlist_organization_to_existing_playlists() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            await db.execute("INSERT INTO schema_version (version) VALUES (3)")
            await db.execute(
                "CREATE TABLE playlists ("
                "id TEXT PRIMARY KEY, "
                "name TEXT NOT NULL, "
                "description TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE playlist_items ("
                "playlist_id TEXT NOT NULL, "
                "media_type TEXT NOT NULL, "
                "media_id TEXT NOT NULL, "
                "title TEXT NOT NULL, "
                "position INTEGER NOT NULL, "
                "PRIMARY KEY (playlist_id, media_type, media_id))"
            )
            await db.execute(
                "INSERT INTO playlists "
                "(id, name, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                "playlist-1",
                "Existing",
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            )

            await run_migrations(db)
            row = await db.fetch_one("SELECT * FROM playlists WHERE id = ?", "playlist-1")
            categories = await db.fetch_all("PRAGMA table_info(playlist_categories)")
            tags = await db.fetch_all("PRAGMA table_info(playlist_tags)")
        finally:
            await db.disconnect()

    assert row is not None
    assert row["category_id"] == ""
    assert row["channel_scope"] == ""
    assert "name" in {column["name"] for column in categories}
    assert "tag" in {column["name"] for column in tags}


async def test_migration_adds_upload_attempts() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            await db.execute("INSERT INTO schema_version (version) VALUES (4)")
            await run_migrations(db)
            row = await db.fetch_one("SELECT version FROM schema_version")
            columns = await db.fetch_all("PRAGMA table_info(upload_attempts)")
        finally:
            await db.disconnect()

    assert row is not None
    assert row["version"] == SCHEMA_VERSION
    assert {"channel_id", "schedule_version", "status", "created_at"} <= {
        column["name"] for column in columns
    }


async def test_migration_adds_recommendation_profiles() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            await db.execute("INSERT INTO schema_version (version) VALUES (5)")
            await run_migrations(db)
            row = await db.fetch_one("SELECT version FROM schema_version")
            columns = await db.fetch_all("PRAGMA table_info(recommendation_profiles)")
        finally:
            await db.disconnect()

    assert row is not None
    assert row["version"] == SCHEMA_VERSION
    assert {"id", "name", "description", "profile_json"} <= {
        column["name"] for column in columns
    }


async def test_migration_adds_external_metadata_cache() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            await db.execute("INSERT INTO schema_version (version) VALUES (6)")
            await run_migrations(db)
            row = await db.fetch_one("SELECT version FROM schema_version")
            columns = await db.fetch_all("PRAGMA table_info(external_metadata_cache)")
        finally:
            await db.disconnect()

    assert row is not None
    assert row["version"] == SCHEMA_VERSION
    assert {"provider", "media_type", "provider_id", "expires_at"} <= {
        column["name"] for column in columns
    }


async def test_migration_adds_notification_events() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            await db.execute("INSERT INTO schema_version (version) VALUES (7)")
            await run_migrations(db)
            row = await db.fetch_one("SELECT version FROM schema_version")
            columns = await db.fetch_all("PRAGMA table_info(notification_events)")
        finally:
            await db.disconnect()

    assert row is not None
    assert row["version"] == SCHEMA_VERSION
    assert {"event_type", "provider", "status", "title", "created_at"} <= {
        column["name"] for column in columns
    }
