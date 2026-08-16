"""Run-scoped logging helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable


LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT = "%m/%d/%Y - %I:%M %p"


def log_path_for_run(run_dir: Path) -> Path:
    return run_dir / f"Log_{run_dir.name}.log"


def build_log_formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


class CallbackHandler(logging.Handler):
    def __init__(self, callback: Callable[[str], None]):
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.callback(self.format(record))
        except Exception:
            self.handleError(record)


def create_logger(
    run_dir: Path,
    callback: Callable[[str], None] | None = None,
) -> tuple[logging.Logger, Path]:
    log_path = log_path_for_run(run_dir)
    logger = logging.getLogger(f"MIAP00.{run_dir.name}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = build_log_formatter()
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    if callback:
        callback_handler = CallbackHandler(callback)
        callback_handler.setFormatter(formatter)
        logger.addHandler(callback_handler)
    return logger, log_path
