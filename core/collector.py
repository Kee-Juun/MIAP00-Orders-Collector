from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import shutil
import threading
from typing import Callable

from config.settings import Settings
from .cancellation import CollectionCancelled, raise_if_cancelled
from .content_duplicates import (
    find_irt_backed_consolidated_duplicates,
    remove_content_duplicates,
)
from browser.irt import IRTDuplicateChecker, IRTError
from browser.michigan_courts import MichiganOrdersSite
from reporting.excel_report import ReportWriter
from utils.logging import create_logger
from .models import OrderResult, ProcessingRecord
from .naming import (
    build_filename,
    extract_document_date,
    extract_source_docket,
    sha256_file,
)


class CollectionError(RuntimeError):
    pass


def collected_directory_for_run(run_dir: Path) -> Path:
    return run_dir / f"Collected_{run_dir.name}"


@dataclass
class PendingDownload:
    order: OrderResult
    source_docket: str
    target_filename: str
    document_date: str
    byte_count: int
    path: Path


class MIAP00Collector:
    def __init__(
        self,
        settings: Settings,
        *,
        log_callback: Callable[[str], None] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.settings = settings
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.cancel_event = cancel_event or threading.Event()
        self.run_dir: Path | None = None
        self.collected_dir: Path | None = None
        self.logger: logging.Logger | None = None
        self.last_counts: dict[str, int] = {}

    def run(self) -> Path:
        started_at = datetime.now()
        timestamp = started_at.strftime("%m-%d-%Y_%H-%M-%S-%f")[:-3]
        self.run_dir = self.settings.resolved_output_root() / f"MIAP00_{timestamp}"
        self.collected_dir = collected_directory_for_run(self.run_dir)
        temp_dir = self.run_dir / ".temporary_downloads"
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.collected_dir.mkdir()
        temp_dir.mkdir()
        self.logger, _ = create_logger(self.run_dir, self.log_callback)
        site = MichiganOrdersSite(self.settings, self.logger)
        irt = IRTDuplicateChecker(
            self.settings,
            self.logger,
            cancel_event=self.cancel_event,
        )
        discovered: list[OrderResult] = []
        records: list[ProcessingRecord] = []
        pending: list[PendingDownload] = []
        try:
            self.logger.info("MIAP00 Orders Collector started")
            self.logger.info("Run folder: %s", self.run_dir)
            self.logger.info("Collected files folder: %s", self.collected_dir)
            self.logger.info("Selector strategy: text/labels/options/stable result classes; uid selectors disabled")
            raise_if_cancelled(self.cancel_event)
            discovered = site.collect_result_metadata(self.cancel_event)
            if not discovered:
                self.logger.warning("No PDF order results were discovered")
                return self._finish_report(discovered, records, started_at)

            start_date, end_date = self.settings.resolved_date_range()
            ordered = sorted(discovered, key=lambda item: item.original_filename.lower())
            occurrences: defaultdict[tuple[str, str], int] = defaultdict(int)
            total = len(ordered)
            for index, order in enumerate(ordered, 1):
                raise_if_cancelled(
                    self.cancel_event,
                    f"Collection stopped after {index - 1}/{total} downloads",
                )
                self.logger.info("[%d/%d] Downloading to temporary storage: %s", index, total, order.original_filename)
                if self.progress_callback:
                    self.progress_callback(index - 1, total)
                temp_path = temp_dir / f"{index:05d}_{order.original_filename}"
                try:
                    byte_count = site.download_pdf(
                        order,
                        temp_path,
                        cancel_event=self.cancel_event,
                    )
                    raise_if_cancelled(self.cancel_event)
                    docket = extract_source_docket(order.original_filename)
                    document_date = extract_document_date(
                        temp_path,
                        logger=self.logger,
                        expected_date=order.release_date,
                        allowed_date_range=(start_date, end_date),
                        cancel_event=self.cancel_event,
                    )
                    raise_if_cancelled(self.cancel_event)
                    occurrence_key = (docket, document_date)
                    occurrence = occurrences[occurrence_key]
                    occurrences[occurrence_key] += 1
                    target_filename = build_filename(docket, document_date, occurrence)
                    self.logger.info(
                        "FileFlex MIAP00 name: %s -> %s",
                        order.original_filename,
                        target_filename,
                    )
                    renamed_temp_path = temp_dir / target_filename
                    if renamed_temp_path.exists():
                        raise CollectionError(
                            f"Temporary target filename already exists: {target_filename}"
                        )
                    temp_path.replace(renamed_temp_path)
                    temp_path = renamed_temp_path
                    self.logger.info("Renamed temporary PDF: %s", target_filename)

                    pending.append(
                        PendingDownload(
                            order=order,
                            source_docket=docket,
                            target_filename=target_filename,
                            document_date=document_date,
                            byte_count=byte_count,
                            path=temp_path,
                        )
                    )
                except CollectionCancelled:
                    temp_path.unlink(missing_ok=True)
                    raise
                except Exception as exc:
                    temp_path.unlink(missing_ok=True)
                    self.logger.exception("Failed processing %s", order.original_filename)
                    records.append(
                        ProcessingRecord(
                            status="error",
                            docket=order.docket,
                            title=order.title,
                            release_date=order.release_date,
                            source_filename=order.original_filename,
                            source_url=order.pdf_url,
                            lower_court=order.lower_court,
                            page=order.page,
                            reason=f"{type(exc).__name__}: {exc}",
                        )
                    )
                finally:
                    if self.progress_callback:
                        self.progress_callback(index, total)

            raise_if_cancelled(
                self.cancel_event,
                "Collection stopped before IRT duplicate validation",
            )
            if pending:
                self.logger.info(
                    "IRT bulk duplicate check: capturing %s from %s through %s once",
                    self.settings.irt_court_code,
                    start_date.strftime("%m-%d-%Y"),
                    end_date.strftime("%m-%d-%Y"),
                )
                try:
                    existing = irt.load_existing(start_date, end_date)
                except IRTError as exc:
                    for item in pending:
                        item.path.unlink(missing_ok=True)
                    raise CollectionError(
                        "IRT could not load a complete date-range duplicate index. "
                        "No unverified PDF was finalized."
                    ) from exc

                self.logger.info(
                    "Comparing %d renamed PDF filename(s) against the captured IRT index",
                    len(pending),
                )
                exact_matches = {}
                for item in pending:
                    raise_if_cancelled(
                        self.cancel_event,
                        "Collection stopped while comparing the IRT index",
                    )
                    exact_matches[item.target_filename] = irt.duplicate_records(
                        item.target_filename,
                        existing,
                    )
                comparison_records = [
                    (
                        ProcessingRecord(
                            status="pending",
                            docket=item.source_docket,
                            title=item.order.title,
                            release_date=item.order.release_date,
                            source_filename=item.order.original_filename,
                            source_url=item.order.pdf_url,
                            target_filename=item.target_filename,
                            document_date=item.document_date,
                            lower_court=item.order.lower_court,
                            page=item.order.page,
                            bytes=item.byte_count,
                        ),
                        item.path,
                    )
                    for item in pending
                ]
                consolidated_matches = find_irt_backed_consolidated_duplicates(
                    comparison_records,
                    exact_matches,
                    self.logger,
                    cancel_event=self.cancel_event,
                )
                for item in pending:
                    raise_if_cancelled(
                        self.cancel_event,
                        "Collection stopped while finalizing IRT-cleared PDFs",
                    )
                    duplicates = exact_matches[item.target_filename]
                    if duplicates:
                        self.logger.info(
                            "IRT duplicate skipped: %s (LNI evidence: %s)",
                            item.target_filename,
                            ", ".join(str(row.get("LNI", "")) for row in duplicates),
                        )
                        records.append(
                            self._record(
                                item.order,
                                "duplicate",
                                item.target_filename,
                                item.document_date,
                                item.byte_count,
                                item.path,
                                "Matching filename in complete IRT date-range snapshot",
                                duplicates,
                            )
                        )
                        item.path.unlink(missing_ok=True)
                        continue

                    consolidated = consolidated_matches.get(item.target_filename)
                    if consolidated:
                        shared = ", ".join(consolidated.shared_dockets)
                        reason = (
                            "IRT-backed consolidated copy; online parent "
                            f"{consolidated.parent_filename}; shared appellate dockets "
                            f"{shared}; normalized content similarity "
                            f"{consolidated.similarity:.1%}"
                        )
                        self.logger.info(
                            "IRT consolidated duplicate skipped: %s "
                            "(online parent %s; shared dockets %s)",
                            item.target_filename,
                            consolidated.parent_filename,
                            shared,
                        )
                        records.append(
                            self._record(
                                item.order,
                                "consolidated_duplicate",
                                item.target_filename,
                                item.document_date,
                                item.byte_count,
                                item.path,
                                reason,
                                consolidated.irt_evidence,
                            )
                        )
                        item.path.unlink(missing_ok=True)
                        continue

                    target_path = self.collected_dir / item.target_filename
                    if target_path.exists():
                        reason = (
                            "Target filename already exists in the collected files "
                            "folder; no overwrite performed"
                        )
                        self.logger.warning("Local duplicate skipped: %s", item.target_filename)
                        records.append(
                            self._record(
                                item.order,
                                "local_duplicate",
                                item.target_filename,
                                item.document_date,
                                item.byte_count,
                                item.path,
                                reason,
                                [],
                            )
                        )
                        item.path.unlink(missing_ok=True)
                        continue

                    digest = sha256_file(item.path)
                    item.path.replace(target_path)
                    record = self._record(
                        item.order,
                        "collected",
                        item.target_filename,
                        item.document_date,
                        item.byte_count,
                        target_path,
                        "Passed complete IRT date-range snapshot duplicate check",
                        [],
                    )
                    record.sha256 = digest
                    records.append(record)
                    self.logger.info(
                        "Collected: %s (%d bytes)",
                        item.target_filename,
                        item.byte_count,
                    )
            remove_content_duplicates(
                records,
                self.collected_dir,
                self.logger,
                cancel_event=self.cancel_event,
            )
            return self._finish_report(discovered, records, started_at)
        except CollectionCancelled as exc:
            self.logger.info("Collection stopped by user: %s", exc)
            self._record_cancelled_items(discovered, records, pending)
            return self._finish_report(discovered, records, started_at)
        except Exception as exc:
            self.logger.exception("Collection stopped with a fatal error")
            records.append(
                ProcessingRecord(
                    status="error",
                    docket="",
                    title="Run-level failure",
                    release_date="",
                    source_filename="",
                    source_url=self.settings.source_url,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            self._finish_report(discovered, records, started_at)
            raise
        finally:
            site.close()
            irt.close()
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            if self.logger:
                self.logger.info("Browser sessions and temporary storage closed")

    def _record_cancelled_items(
        self,
        discovered: list[OrderResult],
        records: list[ProcessingRecord],
        pending: list[PendingDownload],
    ) -> None:
        """Delete unverified temporary PDFs and document every unfinished order."""

        represented = {record.source_url for record in records if record.source_url}
        pending_by_url = {item.order.pdf_url: item for item in pending}
        for order in discovered:
            if order.pdf_url in represented:
                continue
            item = pending_by_url.get(order.pdf_url)
            if item is not None:
                item.path.unlink(missing_ok=True)
            records.append(
                ProcessingRecord(
                    status="cancelled",
                    docket=item.source_docket if item else order.docket,
                    title=order.title,
                    release_date=order.release_date,
                    source_filename=order.original_filename,
                    source_url=order.pdf_url,
                    target_filename=item.target_filename if item else "",
                    document_date=item.document_date if item else "",
                    lower_court=order.lower_court,
                    page=order.page,
                    reason="User requested stop before finalization",
                    bytes=item.byte_count if item else 0,
                )
            )

    def _finish_report(
        self,
        discovered: list[OrderResult],
        records: list[ProcessingRecord],
        started_at: datetime,
    ) -> Path:
        counts: dict[str, int] = {}
        for record in records:
            counts[record.status] = counts.get(record.status, 0) + 1
        self.last_counts = {"discovered": len(discovered), **counts}
        ReportWriter(self.run_dir, self.logger).write(
            discovered,
            records,
            started_at,
            datetime.now(),
            self.settings.to_dict(),
        )
        collected = counts.get("collected", 0)
        duplicates = sum(
            counts.get(status, 0)
            for status in (
                "duplicate",
                "consolidated_duplicate",
                "local_duplicate",
                "content_duplicate",
            )
        )
        errors = counts.get("error", 0)
        self.logger.info(
            "Run complete: discovered=%d collected=%d duplicates=%d errors=%d",
            len(discovered),
            collected,
            duplicates,
            errors,
        )
        return self.run_dir

    @staticmethod
    def _record(
        order: OrderResult,
        status: str,
        target: str,
        document_date: str,
        byte_count: int,
        path: Path,
        reason: str,
        evidence: list[dict],
    ) -> ProcessingRecord:
        return ProcessingRecord(
            status=status,
            docket=order.docket,
            title=order.title,
            release_date=order.release_date,
            source_filename=order.original_filename,
            source_url=order.pdf_url,
            target_filename=target,
            document_date=document_date,
            lower_court=order.lower_court,
            page=order.page,
            reason=reason,
            irt_evidence=evidence,
            bytes=byte_count,
            sha256=sha256_file(path) if path.exists() else "",
        )
