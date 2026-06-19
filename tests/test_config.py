from __future__ import annotations

import os
import tempfile

import pytest
import yaml

from tunarr_autoscheduler.core.config import ConfigManager
from tunarr_autoscheduler.models.config import (
    AppConfig,
    ChannelConfig,
    DayOfWeek,
    DaypartTemplate,
    JellyfinConfig,
    RotationConfig,
)


class TestAppConfig:
    def test_default_config(self):
        config = AppConfig()
        assert config.timezone == "Europe/Berlin"
        assert config.server.port == 8000
        assert config.server.log_level == "info"
        assert config.server.log_file == "~/.tunarr/logs/scheduler.log"
        assert config.jellyfin.url == "http://jellyfin:8096"
        assert config.tunarr.url == "http://tunarr:8000"
        assert config.metadata.tmdb_enabled is False
        assert config.metadata.tmdb_api_key == ""
        assert config.metadata.tmdb_language == "de-DE"
        assert config.metadata.tvdb_enabled is False
        assert config.metadata.omdb_enabled is False
        assert config.metadata.tmdb_rate_limit_per_minute == 120
        assert config.metadata.tvdb_rate_limit_per_minute == 60
        assert config.metadata.omdb_rate_limit_per_minute == 60
        assert config.metadata.cache_ttl_days == 14
        assert config.auth.password_hash == ""
        assert config.auth.username == "admin"
        assert config.public_access.epg == "public"
        assert config.backups.enabled is False
        assert config.backups.output_dir == "~/.tunarr/backups"
        assert config.backups.retention_count == 7
        assert config.backups.min_free_mb == 1024
        assert config.backups.size_multiplier == 3
        assert len(config.channels) == 0

    def test_channel_config_defaults(self):
        channel = ChannelConfig(id="test-channel")
        assert channel.channel_profile == "advanced"
        assert channel.scheduling_enabled is False
        assert channel.public_epg_enabled is True
        assert channel.public_epg_order == 100
        assert channel.public_epg_logo_url == ""
        assert len(channel.dayparts) == 0
        assert len(channel.rotations) == 0
        assert channel.ads.enabled is True
        assert channel.continuity.station_id_custom_show_id == ""
        assert channel.continuity.bumper_custom_show_id == ""
        assert channel.ads.filler_list_id == ""
        assert channel.ads.break_after_programs == 1
        assert channel.ads.max_total_minutes == 0
        assert channel.ads.ad_density == 0.08
        assert "schedule_persister" in channel.pipeline
        assert "tunarr_uploader" not in channel.pipeline

    def test_daypart_template(self):
        dp = DaypartTemplate(
            name="morning",
            days=[DayOfWeek.MON, DayOfWeek.TUE],
            start_time="06:00",
            end_time="12:00",
        )
        assert dp.allow_movies is False
        assert dp.variable_movie_duration is False
        assert dp.end_tolerance_minutes == 0
        assert dp.ad_density == 0.08
        assert dp.custom_show_list_ids == []

    def test_rotation_config(self):
        rot = RotationConfig(name="default", show_ids=["s1", "s2"])
        assert len(rot.show_ids) == 2
        assert rot.marathon_mode is False

    def test_config_roundtrip(self):
        config = AppConfig(
            channels=[
                ChannelConfig(
                    id="ch1",
                    name="Test Channel",
                    scheduling_enabled=True,
                    dayparts=[
                        DaypartTemplate(
                            name="primetime",
                            days=[
                                DayOfWeek.MON,
                                DayOfWeek.TUE,
                                DayOfWeek.WED,
                                DayOfWeek.THU,
                                DayOfWeek.FRI,
                            ],
                            start_time="18:00",
                            end_time="23:00",
                            allow_movies=True,
                        ),
                    ],
                    rotations=[RotationConfig(name="default", show_ids=["s1", "s2"])],
                ),
            ],
        )
        data = config.model_dump(mode="python")
        restored = AppConfig.model_validate(data)
        assert len(restored.channels) == 1
        assert restored.channels[0].id == "ch1"
        assert restored.channels[0].scheduling_enabled is True
        assert len(restored.channels[0].dayparts) == 1
        assert restored.channels[0].dayparts[0].name == "primetime"
        assert restored.channels[0].dayparts[0].allow_movies is True

    def test_jellyfin_config(self):
        jf = JellyfinConfig(api_key="test-key", user_id="test-user")
        assert jf.url == "http://jellyfin:8096"
        assert jf.api_key == "test-key"
        assert jf.user_id == "test-user"


class TestConfigManager:
    def test_write_and_load(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            tmp_path = f.name

        try:
            mgr = ConfigManager(config_path=tmp_path)
            mgr.write_default_template()
            assert os.path.exists(tmp_path)

            loaded_text = open(tmp_path).read()
            assert "YOUR_JELLYFIN_API_KEY" in loaded_text

            config = mgr.load()
            assert isinstance(config, AppConfig)
            assert not hasattr(config.tunarr, "api_key")
            assert len(config.channels) == 0

        finally:
            os.unlink(tmp_path)

    def test_config_before_load_raises_runtime_error(self):
        mgr = ConfigManager(config_path="missing.yaml")

        with pytest.raises(RuntimeError, match="Config not loaded"):
            mgr.config()

    def test_save_before_load_raises_runtime_error(self):
        mgr = ConfigManager(config_path="missing.yaml")

        with pytest.raises(RuntimeError, match="Config not loaded"):
            mgr.save()

    def test_env_var_substitution(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("""
jellyfin:
  api_key: "${TEST_API_KEY}"
tunarr:
  url: "http://tunarr.local"
channels: []
            """)
            tmp_path = f.name

        try:
            os.environ["TEST_API_KEY"] = "resolved-value"
            mgr = ConfigManager(config_path=tmp_path)
            config = mgr.load()
            assert config.jellyfin.api_key == "resolved-value"
            assert config.tunarr.url == "http://tunarr.local"
        finally:
            os.unlink(tmp_path)
            os.environ.pop("TEST_API_KEY", None)

    def test_placeholder_credentials_are_not_configured(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("""
jellyfin:
  api_key: "YOUR_JELLYFIN_API_KEY"
  user_id: "real-user"
tunarr:
  url: "http://tunarr.local"
channels: []
            """)
            tmp_path = f.name

        try:
            mgr = ConfigManager(config_path=tmp_path)
            mgr.load()
            assert mgr.credentials_configured() is False
        finally:
            os.unlink(tmp_path)

    def test_real_credentials_are_configured(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("""
jellyfin:
  api_key: "jf-key"
  user_id: "jf-user"
tunarr:
  url: "http://tunarr.local"
channels: []
            """)
            tmp_path = f.name

        try:
            mgr = ConfigManager(config_path=tmp_path)
            mgr.load()
            assert mgr.credentials_configured() is True
        finally:
            os.unlink(tmp_path)

    def test_legacy_python_tagged_config_can_be_loaded_and_resaved_as_safe_yaml(self):
        config = AppConfig(channels=[
            ChannelConfig(
                id="ch1",
                dayparts=[
                    DaypartTemplate(
                        name="morning",
                        days=[DayOfWeek.MON],
                        start_time="06:00",
                        end_time="12:00",
                    ),
                ],
            ),
        ])
        config.jellyfin.api_key = "jf-key"
        config.jellyfin.user_id = "jf-user"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml.dump(config.model_dump(mode="python"), sort_keys=False))
            tmp_path = f.name

        try:
            mgr = ConfigManager(config_path=tmp_path)
            loaded = mgr.load()
            assert loaded.channels[0].dayparts[0].days == [DayOfWeek.MON]

            mgr.save()
            saved_text = open(tmp_path).read()
            assert "!!python/object" not in saved_text
            assert "mon" in saved_text
        finally:
            os.unlink(tmp_path)

    def test_auth_placeholder_is_not_configured(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("""
auth:
  password_hash: ""
  session_secret: "YOUR_SESSION_SECRET"
channels: []
            """)
            tmp_path = f.name

        try:
            mgr = ConfigManager(config_path=tmp_path)
            mgr.load()
            assert mgr.auth_configured() is False
        finally:
            os.unlink(tmp_path)

    def test_auth_hash_and_secret_are_configured(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("""
auth:
  username: "admin"
  password_hash: "pbkdf2_sha256$salt$hash"
  session_secret: "secret"
channels: []
            """)
            tmp_path = f.name

        try:
            mgr = ConfigManager(config_path=tmp_path)
            mgr.load()
            assert mgr.auth_configured() is True
        finally:
            os.unlink(tmp_path)

    def test_invalid_timezone_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown timezone"):
            AppConfig(timezone="Not/AZone")
