"""Build and publish the final release-date delivery folder."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
from pathlib import Path
import re
import shutil
import threading

from .cancellation import raise_if_cancelled
from .models import ProcessingRecord


FINAL_ORDER_DATE_RE = re.compile(r"_(\d{8})\.pdf$", re.IGNORECASE)


class ReleaseDeliveryError(RuntimeError):
    """Raised when a release folder cannot be assembled or published safely."""


def dominant_collected_document_date(
    records: list[ProcessingRecord],
) -> str | None:
    """Return the most common certified date, preferring the latest on a tie."""

    dates: list[str] = []
    for record in records:
        if record.status != "collected" or not record.target_filename:
            continue
        value = record.document_date.strip()
        if not value:
            match = FINAL_ORDER_DATE_RE.search(record.target_filename)
            value = match.group(1) if match else ""
        try:
            datetime.strptime(value, "%m%d%Y")
        except (TypeError, ValueError):
            continue
        dates.append(value)
    if not dates:
        return None
    counts = Counter(dates)
    highest_count = max(counts.values())
    tied = [value for value, count in counts.items() if count == highest_count]
    return max(tied, key=lambda value: datetime.strptime(value, "%m%d%Y"))


def consolidated_release_directory_for_run(
    run_dir: Path,
    records: list[ProcessingRecord],
) -> Path | None:
    document_date = dominant_collected_document_date(records)
    if document_date is None:
        return None
    return run_dir / f"{document_date} Release Date"


def build_consolidated_release_folder(
    run_dir: Path,
    records: list[ProcessingRecord],
    collected_orders_dir: Path,
    collected_counsels_dir: Path,
    logger,
    *,
    cancel_event: threading.Event | None = None,
) -> Path | None:
    """Copy newly collected orders and counsels into one dated local folder."""

    destination = consolidated_release_directory_for_run(run_dir, records)
    if destination is None:
        logger.info(
            "Consolidated release folder not created: no collected orders"
        )
        return None

    sources = _collected_files(collected_orders_dir) + _collected_files(
        collected_counsels_dir
    )
    if not sources:
        raise ReleaseDeliveryError(
            "Collected records exist, but no collected order or counsel files were found"
        )

    logger.info("Building consolidated release folder: %s", destination.name)
    destination.mkdir(parents=False, exist_ok=True)
    for source in sources:
        raise_if_cancelled(
            cancel_event,
            "Collection stopped while building the consolidated release folder",
        )
        _copy_without_conflicting_overwrite(source, destination / source.name)
    logger.info(
        "Consolidated release folder ready: %s (%d file(s))",
        destination.name,
        len(sources),
    )
    return destination


def publish_consolidated_release_folder(
    source: Path,
    shared_root: Path,
    logger,
    *,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Copy or safely merge the local release folder into the shared location."""

    if not source.is_dir():
        raise ReleaseDeliveryError(
            f"Consolidated release folder does not exist: {source}"
        )
    logger.info("Publishing consolidated release folder to: %s", shared_root)
    shared_root.mkdir(parents=True, exist_ok=True)
    destination = shared_root / source.name
    destination.mkdir(exist_ok=True)
    copied = 0
    already_present = 0
    for source_file in _collected_files(source):
        raise_if_cancelled(
            cancel_event,
            "Collection stopped while copying the release folder to the shared path",
        )
        target = destination / source_file.name
        if target.exists() and _files_equal(source_file, target):
            already_present += 1
            continue
        _copy_without_conflicting_overwrite(source_file, target)
        copied += 1
    logger.info(
        "Shared release folder ready: %s (copied=%d already_present=%d)",
        destination,
        copied,
        already_present,
    )
    return destination


def _collected_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (path for path in directory.iterdir() if path.is_file()),
        key=lambda path: path.name.lower(),
    )


def _copy_without_conflicting_overwrite(source: Path, destination: Path) -> None:
    if destination.exists():
        if _files_equal(source, destination):
            return
        raise ReleaseDeliveryError(
            "A different file already exists at the release destination: "
            f"{destination}"
        )
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        raise ReleaseDeliveryError(
            f"Could not copy {source.name} to {destination}: {exc}"
        ) from exc


def _files_equal(first: Path, second: Path) -> bool:
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        return _sha256(first) == _sha256(second)
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
