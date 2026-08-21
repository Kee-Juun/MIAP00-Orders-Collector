from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
import logging
from pathlib import Path
import re
import shutil
import threading
from typing import Callable

from config.settings import Settings
from .cancellation import CollectionCancelled, raise_if_cancelled
from .content_duplicates import (
    find_irt_backed_content_duplicates,
    remove_content_duplicates,
)
from browser.irt import IRTDuplicateChecker, IRTError
from browser.michigan_counsel import CounselCollectionError, MichiganCounselSite
from browser.michigan_courts import MichiganOrdersSite
from reporting.excel_report import ReportWriter
from utils.logging import create_logger
from .models import CounselRecord, OrderResult, ProcessingRecord
from .location_check import verify_us_location
from .naming import (
    NonOrderDocumentError,
    build_filename,
    extract_document_date,
    extract_source_docket,
    sha256_file,
)


class CollectionError(RuntimeError):
    pass


def collected_orders_directory_for_run(run_dir: Path) -> Path:
    return run_dir / f"Collected_Orders_{run_dir.name}"


def collected_counsels_directory_for_run(run_dir: Path) -> Path:
    return run_dir / f"Collected_Counsels_{run_dir.name}"


def excluded_directory_for_run(run_dir: Path) -> Path:
    return run_dir / "Excluded"


def _irt_record_date(record: dict) -> date:
    """Return the best available IRT inventory date without guessing from the LNI."""

    for field in ("Received Date", "Decided Date"):
        value = str(record.get(field, "")).strip()
        match = re.search(
            r"(?<!\d)(\d{1,4})[/-](\d{1,2})[/-](\d{1,4})(?!\d)",
            value,
        )
        if not match:
            continue
        first, middle, last = (int(part) for part in match.groups())
        year, month, day = (
            (first, middle, last) if first >= 1000 else (last, first, middle)
        )
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date.min


def select_most_recent_counsel_match(matches: list[dict]) -> dict:
    """Select one newest counsel row, preferring rows that expose an LNI."""

    if not matches:
        return {}
    with_lni = [match for match in matches if str(match.get("LNI", "")).strip()]
    candidates = with_lni or matches
    _, selected = max(
        enumerate(candidates),
        key=lambda item: (_irt_record_date(item[1]), -item[0]),
    )
    return selected


def collected_directory_for_run(run_dir: Path) -> Path:
    """Backward-compatible name for the collected Orders destination."""

    return collected_orders_directory_for_run(run_dir)


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
        self.counsel_dir: Path | None = None
        self.excluded_dir: Path | None = None
        self.logger: logging.Logger | None = None
        self.last_counts: dict[str, int] = {}
        self.counsel_records: list[CounselRecord] = []
        self.was_cancelled = False

    def run(self) -> Path:
        self.was_cancelled = False
        self.counsel_records = []
        location = verify_us_location(
            timeout_seconds=self.settings.location_check_timeout_seconds
        )
        started_at = datetime.now()
        timestamp = started_at.strftime("%m-%d-%Y_%H-%M-%S-%f")[:-3]
        self.run_dir = self.settings.resolved_output_root() / f"MIAP00_{timestamp}"
        self.collected_dir = collected_orders_directory_for_run(self.run_dir)
        self.counsel_dir = collected_counsels_directory_for_run(self.run_dir)
        self.excluded_dir = excluded_directory_for_run(self.run_dir)
        temp_dir = self.run_dir / ".temporary_downloads"
        self.run_dir.mkdir(parents=True, exist_ok=False)
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
            self.logger.info(
                "U.S. location preflight passed: %s", location.display_name
            )
            self.logger.info("Run folder: %s", self.run_dir)
            self.logger.info(
                "Collected Orders folder (created when needed): %s",
                self.collected_dir,
            )
            self.logger.info(
                "Collected Counsels folder (created when needed): %s",
                self.counsel_dir,
            )
            self.logger.info(
                "Excluded review folder (created when needed): %s",
                self.excluded_dir,
            )
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
                except NonOrderDocumentError as exc:
                    try:
                        records.append(
                            self._preserve_excluded_file(
                                order,
                                temp_path,
                                byte_count,
                                str(exc),
                            )
                        )
                    except Exception as preserve_exc:
                        temp_path.unlink(missing_ok=True)
                        self.logger.exception(
                            "Failed preserving excluded file %s",
                            order.original_filename,
                        )
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
                                reason=(
                                    f"{type(preserve_exc).__name__}: {preserve_exc}"
                                ),
                            )
                        )
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
                certified_dates = [
                    datetime.strptime(item.document_date, "%m%d%Y").date()
                    for item in pending
                ]
                irt_start_date = min(start_date, min(certified_dates))
                irt_end_date = max(end_date, max(certified_dates))
                if irt_start_date != start_date or irt_end_date != end_date:
                    self.logger.info(
                        "IRT snapshot range expanded to cover certified decision dates: "
                        "%s through %s (user range %s through %s)",
                        irt_start_date.strftime("%m-%d-%Y"),
                        irt_end_date.strftime("%m-%d-%Y"),
                        start_date.strftime("%m-%d-%Y"),
                        end_date.strftime("%m-%d-%Y"),
                    )
                self.logger.info(
                    "IRT bulk duplicate check: capturing %s from %s through %s once",
                    self.settings.irt_court_code,
                    irt_start_date.strftime("%m-%d-%Y"),
                    irt_end_date.strftime("%m-%d-%Y"),
                )
                try:
                    existing = irt.load_existing(irt_start_date, irt_end_date)
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
                irt_content_matches = find_irt_backed_content_duplicates(
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

                    content_match = irt_content_matches.get(item.target_filename)
                    if content_match:
                        shared = ", ".join(content_match.shared_dockets)
                        if content_match.match_kind == "same_docket":
                            reason = (
                                "IRT-backed same-docket content duplicate; online parent "
                                f"{content_match.parent_filename}; {content_match.method}"
                            )
                            self.logger.info(
                                "IRT same-docket content duplicate skipped: %s "
                                "(online parent %s; %s)",
                                item.target_filename,
                                content_match.parent_filename,
                                content_match.method,
                            )
                            records.append(
                                self._record(
                                    item.order,
                                    "content_duplicate",
                                    item.target_filename,
                                    item.document_date,
                                    item.byte_count,
                                    item.path,
                                    reason,
                                    content_match.irt_evidence,
                                )
                            )
                            item.path.unlink(missing_ok=True)
                            continue
                        reason = (
                            "IRT-backed consolidated copy; online parent "
                            f"{content_match.parent_filename}; shared appellate dockets "
                            f"{shared}; normalized content similarity "
                            f"{content_match.similarity:.1%}"
                        )
                        self.logger.info(
                            "IRT consolidated duplicate skipped: %s "
                            "(online parent %s; shared dockets %s)",
                            item.target_filename,
                            content_match.parent_filename,
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
                                content_match.irt_evidence,
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
                    self._ensure_collected_dir()
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
            if self.settings.collect_counsel:
                self._collect_counsel(records, site, irt)
            return self._finish_report(discovered, records, started_at)
        except CollectionCancelled as exc:
            self.was_cancelled = True
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
            self._remove_empty_collected_dir()
            if self.logger:
                self.logger.info("Browser sessions and temporary storage closed")

    def _ensure_collected_dir(self) -> None:
        if self.collected_dir is None:
            raise CollectionError("Collected files destination was not initialized")
        self.collected_dir.mkdir(parents=False, exist_ok=True)

    def _ensure_counsel_dir(self) -> None:
        if self.counsel_dir is None:
            raise CollectionError("Collected Counsels destination was not initialized")
        self.counsel_dir.mkdir(parents=False, exist_ok=True)

    def _ensure_excluded_dir(self) -> None:
        if self.excluded_dir is None:
            raise CollectionError("Excluded review destination was not initialized")
        self.excluded_dir.mkdir(parents=False, exist_ok=True)

    def _preserve_excluded_file(
        self,
        order: OrderResult,
        source_path: Path,
        byte_count: int,
        reason: str,
    ) -> ProcessingRecord:
        """Keep an excluded source PDF unchanged for post-run spot-checking."""

        if self.excluded_dir is None:
            raise CollectionError("Excluded review destination was not initialized")
        destination = self.excluded_dir / order.original_filename
        if destination.exists():
            raise CollectionError(
                f"Excluded source filename already exists: {order.original_filename}"
            )
        digest = sha256_file(source_path)
        self._ensure_excluded_dir()
        source_path.replace(destination)
        self.logger.warning(
            "Excluded non-order document saved for review without renaming: %s (%s)",
            order.original_filename,
            reason,
        )
        return ProcessingRecord(
            status="non_order",
            docket=order.docket,
            title=order.title,
            release_date=order.release_date,
            source_filename=order.original_filename,
            source_url=order.pdf_url,
            target_filename=order.original_filename,
            lower_court=order.lower_court,
            page=order.page,
            reason=reason,
            bytes=byte_count,
            sha256=digest,
        )

    def _remove_empty_collected_dir(self) -> None:
        """Remove only the run-scoped collected directory when it is empty."""

        for label, directory in (
            ("Orders", self.collected_dir),
            ("Counsels", self.counsel_dir),
            ("excluded files", self.excluded_dir),
        ):
            if directory is None:
                continue
            try:
                directory.rmdir()
            except FileNotFoundError:
                continue
            except OSError:
                # A non-empty folder, open handle, or external filesystem issue
                # is intentionally left untouched.
                continue
            if self.logger:
                self.logger.info(
                    "No %s collected; empty collected folder removed", label
                )

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
        self.last_counts["counsel_collected"] = sum(
            record.status == "collected" for record in self.counsel_records
        )
        self.last_counts["counsel_irt_existing"] = sum(
            record.status == "irt_existing" for record in self.counsel_records
        )
        counsel_errors = sum(
            record.status == "error" for record in self.counsel_records
        )
        if counsel_errors:
            self.last_counts["error"] = self.last_counts.get("error", 0) + counsel_errors
        ReportWriter(self.run_dir, self.logger).write(
            discovered,
            records,
            self.counsel_records,
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
        errors = counts.get("error", 0) + counsel_errors
        run_outcome = "stopped" if self.was_cancelled else "complete"
        self.logger.info(
            "Run %s: discovered=%d collected=%d duplicates=%d errors=%d",
            run_outcome,
            len(discovered),
            collected,
            duplicates,
            errors,
        )
        return self.run_dir

    def _collect_counsel(
        self,
        records: list[ProcessingRecord],
        site: MichiganOrdersSite,
        irt: IRTDuplicateChecker,
    ) -> None:
        """Collect or recycle one counsel artifact for every retained docket."""

        collected_records = [record for record in records if record.status == "collected"]
        if not collected_records:
            return
        ordered_dockets: list[str] = []
        for record in collected_records:
            dockets = record.related_dockets or ([record.docket] if record.docket else [])
            for docket in dockets:
                if docket and docket not in ordered_dockets:
                    ordered_dockets.append(docket)
        if not ordered_dockets:
            return

        start_date, end_date = self._counsel_irt_date_range()
        self.logger.info(
            "Starting counsel IRT preflight for %d docket(s): %s through %s",
            len(ordered_dockets),
            start_date.strftime("%m-%d-%Y"),
            end_date.strftime("%m-%d-%Y"),
        )
        irt_matches: dict[str, list[dict]] = {}
        try:
            for index, docket in enumerate(ordered_dockets, 1):
                raise_if_cancelled(
                    self.cancel_event,
                    "Collection stopped during counsel IRT preflight",
                )
                self.logger.info(
                    "[%d/%d] Checking IRT counsel: %s",
                    index,
                    len(ordered_dockets),
                    docket,
                )
                irt_matches[docket] = irt.find_existing_counsel(
                    docket,
                    start_date,
                    end_date,
                )
        except IRTError as exc:
            reason = (
                "Counsel collection skipped because the complete IRT preflight "
                f"could not be verified: {exc}"
            )
            self.logger.error(reason)
            self.counsel_records.extend(
                CounselRecord(docket=docket, status="error", reason=reason)
                for docket in ordered_dockets
            )
            return

        counsel_site = MichiganCounselSite(self.settings, self.logger, site)
        total = len(ordered_dockets)
        for index, docket in enumerate(ordered_dockets, 1):
            raise_if_cancelled(
                self.cancel_event,
                "Collection stopped during counsel collection",
            )
            matches = irt_matches[docket]
            if matches:
                selected_match = select_most_recent_counsel_match(matches)
                selected_lni = str(selected_match.get("LNI", "")).strip()
                lnis = [selected_lni] if selected_lni else []
                self.counsel_records.append(
                    CounselRecord(
                        docket=docket,
                        status="irt_existing",
                        lnis=lnis,
                        irt_evidence=[selected_match],
                        reason=(
                            "Most recent matching counsel filename already exists "
                            f"in IRT (selected from {len(matches)} match(es))"
                        ),
                    )
                )
                self.logger.info(
                    "[%d/%d] Counsel recycled from IRT for %s: %s "
                    "(most recent of %d match(es))",
                    index,
                    total,
                    docket,
                    selected_lni or "LNI unavailable",
                    len(matches),
                )
                continue

            filename = f"LDC_SMD_{docket}counsel.html"
            if self.counsel_dir is None:
                raise CollectionError("Collected Counsels destination was not initialized")
            destination = self.counsel_dir / filename
            try:
                self.logger.info(
                    "[%d/%d] Collecting counsel file: %s",
                    index,
                    total,
                    filename,
                )
                if destination.exists():
                    raise CounselCollectionError(
                        f"Counsel target already exists and will not be overwritten: {filename}"
                    )
                self._ensure_counsel_dir()
                case_url = counsel_site.collect(
                    docket,
                    destination,
                    cancel_event=self.cancel_event,
                )
                self.counsel_records.append(
                    CounselRecord(
                        docket=docket,
                        status="collected",
                        target_filename=filename,
                        case_url=case_url,
                        reason="Passed STMIAP00 IRT counsel duplicate preflight",
                    )
                )
            except CollectionCancelled:
                destination.unlink(missing_ok=True)
                raise
            except Exception as exc:
                destination.unlink(missing_ok=True)
                reason = f"{type(exc).__name__}: {exc}"
                self.logger.exception("Counsel collection failed for docket %s", docket)
                self.counsel_records.append(
                    CounselRecord(docket=docket, status="error", reason=reason)
                )

        counsel_by_docket = {record.docket: record for record in self.counsel_records}
        for record in collected_records:
            dockets = record.related_dockets or ([record.docket] if record.docket else [])
            include_docket = len(dict.fromkeys(dockets)) > 1
            record.counsel_references = [
                counsel_by_docket[docket].reference(include_docket=include_docket)
                for docket in dockets
                if docket in counsel_by_docket
                and counsel_by_docket[docket].reference(
                    include_docket=include_docket
                )
            ]
        self.logger.info(
            "Counsel phase complete: collected=%d existing_in_irt=%d errors=%d",
            sum(record.status == "collected" for record in self.counsel_records),
            sum(record.status == "irt_existing" for record in self.counsel_records),
            sum(record.status == "error" for record in self.counsel_records),
        )

    def _counsel_irt_date_range(self, today: date | None = None) -> tuple[date, date]:
        end = today or date.today()
        years = max(1, int(self.settings.counsel_irt_years_back))
        try:
            start = end.replace(year=end.year - years)
        except ValueError:
            start = end.replace(year=end.year - years, day=28)
        return start, end

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
