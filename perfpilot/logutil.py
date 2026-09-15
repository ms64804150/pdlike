"""Agent file + console logging."""

from __future__ import annotations

import logging
import os
from pathlib import Path

_LOGGER_NAME = "perfpilot"
_configured = False


class _FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging() -> Path | None:
    global _configured
    if os.environ.get("PERFPILOT_COLLECT") == "1":
        return None
    from .paths import logs_root

    log_path = logs_root() / "agent.log"
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    if _configured:
        return log_path
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler = _FlushFileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    _configured = True
    logger.info("[Agent] logFile ready path=%s", log_path)
    return log_path


def get_logger(name: str | None = None) -> logging.Logger:
    if os.environ.get("PERFPILOT_COLLECT") == "1":
        logger = logging.getLogger(f"{_LOGGER_NAME}.collect")
        if not logger.handlers:
            logger.addHandler(logging.NullHandler())
            logger.propagate = False
        return logger
    if not _configured:
        setup_logging()
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)
