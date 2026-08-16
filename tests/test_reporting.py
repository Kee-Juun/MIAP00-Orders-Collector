from pathlib import Path
import unittest
from unittest.mock import Mock

from openpyxl import Workbook

from core.models import ProcessingRecord
from reporting.excel_report import ReportWriter, report_path_for_run


class ReportingTests(unittest.TestCase):
    def test_report_filename_mirrors_run_folder(self):
        run_dir = Path("output") / "MIAP00_08-14-2026_22-10-03-027"
        self.assertEqual(
            report_path_for_run(run_dir).name,
            "Report_MIAP00_08-14-2026_22-10-03-027.xlsx",
        )

    def test_filenames_sheet_contains_only_final_collected_names(self):
        workbook = Workbook()
        workbook.remove(workbook.active)
        records = [
            ProcessingRecord(
                status="collected",
                docket="1",
                title="",
                release_date="",
                source_filename="source-1.pdf",
                source_url="",
                target_filename="LDC_SMD_1_08142026.pdf",
            ),
            ProcessingRecord(
                status="duplicate",
                docket="2",
                title="",
                release_date="",
                source_filename="source-2.pdf",
                source_url="",
                target_filename="LDC_SMD_2_08142026.pdf",
            ),
            ProcessingRecord(
                status="collected",
                docket="3",
                title="",
                release_date="",
                source_filename="source-3.pdf",
                source_url="",
                target_filename="LDC_SMD_3_08142026.pdf",
            ),
            ProcessingRecord(
                status="content_duplicate",
                docket="4",
                title="",
                release_date="",
                source_filename="source-4.pdf",
                source_url="",
                target_filename="LDC_SMD_4_08142026.pdf",
            ),
        ]
        ReportWriter(Mock(), Mock())._add_filenames_sheet(workbook, records)
        sheet = workbook["Filenames"]
        self.assertEqual(
            [cell.value for cell in sheet["A"]],
            [
                "Filename",
                "LDC_SMD_1_08142026.pdf",
                "LDC_SMD_3_08142026.pdf",
            ],
        )
        self.assertEqual(sheet.freeze_panes, "A2")
        self.assertEqual(sheet.auto_filter.ref, "A1:A3")


if __name__ == "__main__":
    unittest.main()
