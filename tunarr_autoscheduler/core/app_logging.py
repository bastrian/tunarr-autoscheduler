from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(level_name: str, log_file: str | None = None) -> Path | None:
    level = _level_from_name(level_name)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_path = _resolve_log_path(log_file)
    log_file_error: OSError | None = None
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(
                log_path,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ))
        except OSError as e:
            log_file_error = e
            log_path = None

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    for handler in handlers:
        handler.setLevel(level)
        handler.setFormatter(formatter)
        root.addHandler(handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(logger_name).setLevel(level)

    if log_file_error is not None:
        logging.getLogger(__name__).warning(
            "Log file disabled because it is not writable: %s",
            log_file_error,
        )

    logging.getLogger(__name__).info(
        "Logging configured level=%s file=%s",
        logging.getLevelName(level),
        log_path or "disabled",
    )
    return log_path


def _level_from_name(level_name: str) -> int:
    return getattr(logging, level_name.strip().upper(), logging.INFO)


def _resolve_log_path(log_file: str | None) -> Path | None:
    if log_file is None or not log_file.strip():
        return None
    return Path(os.path.expanduser(log_file)).resolve()
