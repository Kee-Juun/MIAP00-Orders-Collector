"""Filesystem operations shared across collection stages."""

from __future__ import annotations

import logging
from pathlib import Path
import threading

from .cancellation import cancellable_wait, raise_if_cancelled


def replace_file_with_retry(
    source: Path,
    destination: Path,
    *,
    logger: logging.Logger | None = None,
    cancel_event: threading.Event | None = None,
    attempts: int = 6,
    delay_seconds: float = 0.2,
) -> None:
    """Rename a file after short-lived Windows scanner/PDF locks release."""

    attempt_limit = max(1, attempts)
    for attempt in range(1, attempt_limit + 1):
        raise_if_cancelled(cancel_event)
        try:
            source.replace(destination)
            return
        except PermissionError as exc:
            # WinError 32 is a sharing violation; 33 is a lock violation.
            # Other permission failures are not expected to improve with time.
            if getattr(exc, "winerror", None) not in (32, 33) or attempt >= attempt_limit:
                raise
            if logger:
                logger.warning(
                    "Temporary PDF rename was blocked by another process "
                    "(attempt %d/%d); retrying: %s",
                    attempt,
                    attempt_limit,
                    source.name,
                )
            cancellable_wait(
                cancel_event,
                delay_seconds * attempt,
                "Collection stopped while waiting for a temporary PDF lock",
            )
