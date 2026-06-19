from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from tunarr_autoscheduler.models.config import AppConfig


class ConfigManager:
    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or os.path.expanduser("~/.tunarr/config.yaml")
        self._config: AppConfig | None = None

    def config(self) -> AppConfig:
        if self._config is None:
            raise RuntimeError("Config not loaded. Call load() first.")
        return self._config

    def load(self) -> AppConfig:
        raw = self._load_yaml()
        raw = self._substitute_env_vars(raw)
        try:
            self._config = AppConfig.model_validate(raw)
        except ValidationError as e:
            raise ValueError(f"Config validation failed: {e}") from e
        return self._config

    def save(self, config: AppConfig | None = None) -> None:
        if config is not None:
            self._config = config
        if self._config is None:
            raise RuntimeError("Config not loaded. Call load() before save().")
        data = self._config.model_dump(mode="json")
        path = Path(self.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))

    def exists(self) -> bool:
        return Path(self.config_path).exists()

    def credentials_configured(self) -> bool:
        config = self.config()
        values = [
            config.jellyfin.api_key,
            config.jellyfin.user_id,
        ]
        return all(_is_real_credential(value) for value in values)

    def auth_configured(self) -> bool:
        config = self.config()
        return (
            _is_real_credential(config.auth.username)
            and _is_real_credential(config.auth.password_hash)
            and _is_real_credential(config.auth.session_secret)
        )

    def write_default_template(self) -> None:
        default = AppConfig()
        default.auth.session_secret = secrets.token_urlsafe(32)
        path = Path(self.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(
            default.model_dump(mode="json"),
            default_flow_style=False,
            sort_keys=False,
        ))

    def _load_yaml(self) -> dict[str, Any]:
        path = Path(self.config_path)
        if not path.exists():
            return {}
        raw = path.read_text()
        try:
            return yaml.safe_load(raw) or {}
        except yaml.constructor.ConstructorError:
            return yaml.unsafe_load(raw) or {}

    def _substitute_env_vars(self, data: dict[str, Any]) -> dict[str, Any]:
        import re

        def _replace(value: str) -> str:
            def _env_var(match: re.Match[str]) -> str:
                var = match.group(1)
                return os.environ.get(var, match.group(0))
            return re.sub(r"\$\{(\w+)\}", _env_var, value)

        def _walk(obj: object) -> object:
            if isinstance(obj, str):
                return _replace(obj)
            elif isinstance(obj, dict):
                return {k: _walk(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [_walk(item) for item in obj]
            return obj

        return _walk(data)  # type: ignore[return-value]


def _is_real_credential(value: str) -> bool:
    value = value.strip()
    return bool(value) and not value.startswith("YOUR_")
