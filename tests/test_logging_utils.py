import logging
import re
import unittest
from unittest.mock import Mock

from pathlib import Path

from ui.main_window import (
    CollectionWorker,
    CollectorWindow,
    checkbox_checkmark_path,
    format_outcome_summary,
    friendly_status,
)
from utils.logging import build_log_formatter, log_path_for_run


class LoggingFormatTests(unittest.TestCase):
    def test_checkbox_checkmark_is_a_three_point_path_inside_indicator(self):
        from PyQt6.QtCore import QRectF

        indicator = QRectF(0, 0, 17, 17)
        path = checkbox_checkmark_path(indicator)

        self.assertEqual(path.elementCount(), 3)
        self.assertTrue(indicator.contains(path.boundingRect()))

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

    def test_friendly_status_reports_stopped_run(self):
        line = (
            "08/17/2026 - 10:45 AM - INFO - "
            "Run stopped: discovered=117 collected=0 duplicates=0 errors=0"
        )
        self.assertEqual(friendly_status(line), "Collection stopped.")

    def test_friendly_status_reports_counsel_irt_progress(self):
        line = (
            "08/18/2026 - 03:09 PM - INFO - "
            "[6/15] Checking IRT counsel: 379809"
        )
        self.assertEqual(
            friendly_status(line),
            "Checking counsel files in IRT  •  6/15",
        )

    def test_friendly_status_reports_counsel_collection_progress(self):
        line = (
            "08/18/2026 - 03:10 PM - INFO - "
            "[4/15] Collecting counsel file: LDC_SMD_379538counsel.html"
        )
        self.assertEqual(
            friendly_status(line),
            "Collecting counsel files  •  4/15  •  "
            "LDC_SMD_379538counsel.html",
        )

    def test_friendly_status_reports_recycled_counsel_progress(self):
        line = (
            "08/18/2026 - 03:10 PM - INFO - "
            "[3/15] Counsel recycled from IRT for 379272: LNI-1"
        )
        self.assertEqual(
            friendly_status(line),
            "Counsel already exists in IRT  •  3/15",
        )

    def test_worker_emits_explicit_cancelled_outcome(self):
        collector = Mock()
        collector.run.return_value = Path("output/run")
        collector.was_cancelled = True
        collector.last_counts = {"cancelled": 3}
        emitted = []

        with unittest.mock.patch(
            "ui.main_window.MIAP00Collector", return_value=collector
        ):
            worker = CollectionWorker(Mock(), Mock())
            worker.finished.connect(lambda *args: emitted.append(args))
            worker.run()

        self.assertEqual(emitted[0][0], "cancelled")
        self.assertEqual(emitted[0][2], "Collection stopped")

    def test_cancelled_outcome_dialog_is_stopped_without_fake_error(self):
        window = Mock()
        window.last_run_dir = None
        window.progress_bar.maximum.return_value = 100

        with unittest.mock.patch(
            "ui.main_window.show_themed_message", return_value=False
        ) as show_message:
            CollectorWindow._finished(
                window,
                "cancelled",
                None,
                "Collection stopped",
                {"cancelled": 3},
            )

        self.assertEqual(show_message.call_args.args[1], "Collection stopped")
        self.assertIn("Errors: 0", show_message.call_args.args[2])
        self.assertIn("Orders collected: 0", show_message.call_args.args[2])
        self.assertIn("Counsels collected: 0", show_message.call_args.args[2])

    def test_outcome_summary_combines_all_excluded_statuses(self):
        summary = format_outcome_summary(
            {
                "collected": 4,
                "duplicate": 57,
                "consolidated_duplicate": 4,
                "local_duplicate": 1,
                "content_duplicate": 2,
                "non_order": 1,
                "counsel_collected": 3,
                "error": 3,
            }
        )
        self.assertEqual(
            summary,
            "Orders collected: 4\nCounsels collected: 3\n"
            "Duplicates: 64\nExcludes: 1\nErrors: 3",
        )

    def test_failed_summary_reports_error_when_counts_are_unavailable(self):
        self.assertEqual(
            format_outcome_summary({}, failed=True),
            "Orders collected: 0\nCounsels collected: 0\n"
            "Duplicates: 0\nExcludes: 0\nErrors: 1",
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
