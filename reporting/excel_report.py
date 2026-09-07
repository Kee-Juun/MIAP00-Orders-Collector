from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from core.models import CounselRecord, OrderResult, ProcessingRecord


def report_path_for_run(run_dir: Path) -> Path:
    return run_dir / f"Report_{run_dir.name}.xlsx"


FINAL_ORDER_DATE_RE = re.compile(r"_(\d{8})\.pdf$", re.IGNORECASE)


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


def release_filenames_path_for_run(
    run_dir: Path,
    records: list[ProcessingRecord],
) -> Path | None:
    document_date = dominant_collected_document_date(records)
    if document_date is None:
        return None
    return run_dir / f"MIAP00 {document_date} Release Date Filenames.xlsx"


class ReportWriter:
    HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
    HEADER_FONT = Font(color="FFFFFF", bold=True)

    def __init__(self, run_dir: Path, logger):
        self.run_dir = run_dir
        self.logger = logger

    def write(
        self,
        discovered: list[OrderResult],
        records: list[ProcessingRecord],
        counsel_records: list[CounselRecord],
        started_at: datetime,
        finished_at: datetime,
        settings_snapshot: dict[str, Any],
    ) -> Path:
        report_path = report_path_for_run(self.run_dir)
        counts: dict[str, int] = {}
        for record in records:
            counts[record.status] = counts.get(record.status, 0) + 1
        counsel_counts: dict[str, int] = {}
        for record in counsel_records:
            counsel_counts[record.status] = counsel_counts.get(record.status, 0) + 1
        workbook = Workbook()
        workbook.remove(workbook.active)
        summary = workbook.create_sheet("Summary")
        summary.append(["Metric", "Value"])
        summary_rows = [
            ("Started", started_at.strftime("%Y-%m-%d %H:%M:%S")),
            ("Finished", finished_at.strftime("%Y-%m-%d %H:%M:%S")),
            ("Duration seconds", round((finished_at - started_at).total_seconds(), 2)),
            ("Discovered PDF orders", len(discovered)),
            ("Collected", counts.get("collected", 0)),
            ("IRT duplicates skipped", counts.get("duplicate", 0)),
            ("IRT-backed consolidated copies skipped", counts.get("consolidated_duplicate", 0)),
            ("Local duplicates skipped", counts.get("local_duplicate", 0)),
            ("Content duplicates removed", counts.get("content_duplicate", 0)),
            ("Non-order filings excluded", counts.get("non_order", 0)),
            ("Counsel files collected", counsel_counts.get("collected", 0)),
            ("Counsel files recycled from IRT", counsel_counts.get("irt_existing", 0)),
            ("Counsel errors", counsel_counts.get("error", 0)),
            ("Errors", counts.get("error", 0)),
            ("Cancelled", counts.get("cancelled", 0)),
            ("IRT court code", settings_snapshot.get("irt_court_code", "")),
        ]
        for row in summary_rows:
            summary.append(row)
        self._style(summary)

        collected = [row.as_dict() for row in records if row.status == "collected"]
        duplicates = [
            row.as_dict()
            for row in records
            if row.status
            in {"duplicate", "consolidated_duplicate", "local_duplicate", "content_duplicate"}
        ]
        errors = [row.as_dict() for row in records if row.status in {"error", "cancelled"}]
        excluded = [row.as_dict() for row in records if row.status == "non_order"]
        self._add_filenames_sheet(workbook, records)
        self._add_records_sheet(workbook, "Collected", collected)
        self._add_records_sheet(workbook, "Duplicates", duplicates)
        self._add_records_sheet(workbook, "Excluded", excluded)
        self._add_records_sheet(
            workbook, "Counsel", [row.as_dict() for row in counsel_records]
        )
        self._add_records_sheet(workbook, "Errors", errors)
        self._add_records_sheet(workbook, "Discovered", [row.as_dict() for row in discovered])
        workbook.save(report_path)
        self.logger.info("Report saved: %s", report_path.name)
        release_path = self._write_release_filenames_workbook(records)
        if release_path is None:
            self.logger.info(
                "Release-date filenames workbook not created: no collected orders"
            )
        else:
            self.logger.info(
                "Release-date filenames workbook saved: %s",
                release_path.name,
            )
        return report_path

    def _write_release_filenames_workbook(
        self,
        records: list[ProcessingRecord],
    ) -> Path | None:
        release_path = release_filenames_path_for_run(self.run_dir, records)
        if release_path is None:
            return None
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "ORDERS"
        self._populate_filenames_sheet(sheet, records)
        workbook.save(release_path)
        return release_path

    def _add_filenames_sheet(
        self, workbook, records: list[ProcessingRecord]
    ) -> None:
        sheet = workbook.create_sheet("Filenames")
        self._populate_filenames_sheet(sheet, records)

    def _populate_filenames_sheet(
        self,
        sheet,
        records: list[ProcessingRecord],
    ) -> None:
        sheet.append(["Main Document Filename", "Counsel Filename / Recycled LNI"])
        for record in records:
            if record.status == "collected" and record.target_filename:
                sheet.append(
                    [record.target_filename, "\n".join(record.counsel_references)]
                )
        self._style(sheet)

    def _add_records_sheet(self, workbook, name: str, rows: list[dict[str, Any]]) -> None:
        sheet = workbook.create_sheet(name)
        if not rows:
            sheet.append(["Status"])
            sheet.append([f"No {name.lower()} records"])
            self._style(sheet)
            return
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        sheet.append([self._label(key) for key in keys])
        for row in rows:
            sheet.append([self._cell_value(row.get(key, "")) for key in keys])
        self._style(sheet)

    @staticmethod
    def _cell_value(value: Any) -> Any:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
        return value

    @staticmethod
    def _label(key: str) -> str:
        return key.replace("_", " ").title().replace("Irt", "IRT").replace("Url", "URL")

    def _style(self, sheet) -> None:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.fill = self.HEADER_FILL
            cell.font = self.HEADER_FONT
            cell.alignment = Alignment(horizontal="center")
        for column_cells in sheet.columns:
            max_length = max((len(str(cell.value or "")) for cell in column_cells), default=0)
            sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max_length + 2, 60)
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
