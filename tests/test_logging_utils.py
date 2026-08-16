import logging
import re
import unittest
from unittest.mock import Mock

from pathlib import Path

from ui.main_window import (
    CollectorWindow,
    format_outcome_summary,
    friendly_status,
)
from utils.logging import build_log_formatter, log_path_for_run


class LoggingFormatTests(unittest.TestCase):
    def test_log_line_uses_operator_friendly_timestamp(self):
        record = logging.LogRecord(
            name="MIAP00.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="[65/76] Downloading to temporary storage: 381120_16_01.pdf",
            args=(),
            exc_info=None,
        )
        rendered = build_log_formatter().format(record)
        self.assertRegex(
            rendered,
            re.compile(
                r"^\d{2}/\d{2}/\d{4} - \d{2}:\d{2} [AP]M - INFO - "
                r"\[65/76\] Downloading to temporary storage: 381120_16_01\.pdf$"
            ),
        )

    def test_friendly_status_reads_new_log_format(self):
        line = (
            "08/14/2026 - 09:06 PM - INFO - "
            "[65/76] Downloading to temporary storage: 381120_16_01.pdf"
        )
        status = friendly_status(line)
        self.assertIsNotNone(status)
        self.assertIn("65/76", status)

    def test_outcome_summary_combines_all_excluded_statuses(self):
        summary = format_outcome_summary(
            {
                "collected": 4,
                "duplicate": 57,
                "consolidated_duplicate": 4,
                "local_duplicate": 1,
                "content_duplicate": 2,
                "error": 3,
            }
        )
        self.assertEqual(summary, "Collected: 4\nExcluded: 64\nErrors: 3")

    def test_failed_summary_reports_error_when_counts_are_unavailable(self):
        self.assertEqual(
            format_outcome_summary({}, failed=True),
            "Collected: 0\nExcluded: 0\nErrors: 1",
        )

    def test_log_filename_mirrors_run_folder(self):
        run_dir = Path("output") / "MIAP00_08-14-2026_22-10-03-027"
        self.assertEqual(
            log_path_for_run(run_dir).name,
            "Log_MIAP00_08-14-2026_22-10-03-027.log",
        )

    def test_ready_reset_clears_progress_and_status(self):
        window = Mock()
        CollectorWindow._reset_ready_state(window)
        window.progress_bar.setRange.assert_called_once_with(0, 100)
        window.progress_bar.setValue.assert_called_once_with(0)
        window._set_status.assert_called_once_with("Ready.")


if __name__ == "__main__":
    unittest.main()
