from __future__ import annotations

import logging

from tunarr_autoscheduler.core import app_logging
from tunarr_autoscheduler.core.app_logging import configure_logging


def test_configure_logging_creates_file_and_enables_debug(tmp_path) -> None:
    log_file = tmp_path / "logs" / "scheduler.log"

    configured_path = configure_logging("debug", str(log_file))
    logging.getLogger("test").debug("debug message")

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert configured_path == log_file
    assert "debug message" in log_file.read_text()


def test_configure_logging_falls_back_when_file_is_not_writable(
    tmp_path,
    monkeypatch,
) -> None:
    def raise_permission_error(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(app_logging, "RotatingFileHandler", raise_permission_error)

    configured_path = configure_logging("info", str(tmp_path / "scheduler.log"))
    logging.getLogger("test").info("stdout only")

    assert configured_path is None
    assert logging.getLogger().handlers
