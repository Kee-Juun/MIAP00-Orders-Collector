"""Shared cooperative-cancellation helpers for long-running collector work."""

from __future__ import annotations

import threading


class CollectionCancelled(RuntimeError):
    """Raised when the operator requests a safe stop."""


def raise_if_cancelled(
    cancel_event: threading.Event | None,
    context: str = "Collection stopped by user",
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CollectionCancelled(context)


def cancellable_wait(
    cancel_event: threading.Event | None,
    seconds: float,
    context: str = "Collection stopped by user",
) -> None:
    """Wait without making the Stop button wait for the full delay."""

    if seconds <= 0:
        raise_if_cancelled(cancel_event, context)
        return
    if cancel_event is None:
        threading.Event().wait(seconds)
        return
    if cancel_event.wait(seconds):
        raise CollectionCancelled(context)
