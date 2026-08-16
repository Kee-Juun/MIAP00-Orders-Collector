from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from config.settings import Settings
from core.collector import (
    MIAP00Collector,
    collected_directory_for_run,
)
from core.models import OrderResult


class CollectorFlowTests(unittest.TestCase):
    def test_collected_files_use_run_named_subfolder(self):
        run_dir = Path("output") / "MIAP00_08-15-2026_18-01-23-355"
        self.assertEqual(
            collected_directory_for_run(run_dir),
            run_dir / "Collected_MIAP00_08-15-2026_18-01-23-355",
        )

    def _run_one(self, duplicate_records):
        settings = Settings(
            output_root="synthetic-output",
            start_date="2026-08-07",
            end_date="2026-08-14",
        )
        order = OrderResult(
            page=1,
            position=1,
            docket="381603",
            title="Test order",
            lower_court="",
            release_date="08/14/2026",
            order_type="Order",
            pdf_url="https://example.test/381603_6_01.pdf",
            original_filename="381603_6_01.pdf",
        )
        events = []
        logger = Mock()
        logger.exception.side_effect = lambda *_args, **_kwargs: events.append(
            f"error:{sys.exc_info()[1]}"
        )
        site = Mock()
        site.collect_result_metadata.return_value = [order]
        site.download_pdf.side_effect = (
            lambda _order, _path, cancel_event=None: events.append("download") or 137
        )
        irt = Mock()

        expected = "LDC_SMD_381603_08142026.pdf"
        existing = {expected.lower(): duplicate_records} if duplicate_records else {}

        def load_existing(start_date, end_date):
            events.append(f"irt-load:{start_date.isoformat()}:{end_date.isoformat()}")
            return existing

        def duplicate_lookup(filename, index):
            events.append(f"irt-compare:{filename}")
            return index.get(filename.lower(), [])

        irt.load_existing.side_effect = load_existing
        irt.duplicate_records.side_effect = duplicate_lookup

        def record_replace(destination):
            events.append(f"rename:{Path(destination).name}")

        with patch("core.collector.MichiganOrdersSite", return_value=site), patch(
            "core.collector.IRTDuplicateChecker", return_value=irt
        ), patch(
            "core.collector.extract_document_date", return_value="08142026"
        ), patch("core.collector.ReportWriter.write"), patch(
            "core.collector.create_logger", return_value=(logger, Path("log"))
        ), patch("pathlib.Path.mkdir"), patch(
            "pathlib.Path.exists", return_value=False
        ), patch("pathlib.Path.replace", side_effect=record_replace), patch(
            "pathlib.Path.unlink"
        ) as unlink, patch("core.collector.sha256_file", return_value="digest"), patch(
            "core.collector.shutil.rmtree"
        ):
            collector = MIAP00Collector(settings)
            run_dir = collector.run()
        return run_dir, irt, events, unlink

    def test_all_downloads_are_renamed_before_one_bulk_irt_check(self):
        _run_dir, irt, events, _unlink = self._run_one([])
        expected = "LDC_SMD_381603_08142026.pdf"
        self.assertEqual(
            events,
            [
                "download",
                f"rename:{expected}",
                "irt-load:2026-08-07:2026-08-14",
                f"irt-compare:{expected}",
                f"rename:{expected}",
            ],
        )
        irt.load_existing.assert_called_once()
        irt.check_one.assert_not_called()

    def test_irt_duplicate_is_deleted_without_finalization(self):
        _run_dir, irt, events, unlink = self._run_one([{"LNI": "duplicate"}])
        expected = "LDC_SMD_381603_08142026.pdf"
        self.assertEqual(
            events,
            [
                "download",
                f"rename:{expected}",
                "irt-load:2026-08-07:2026-08-14",
                f"irt-compare:{expected}",
            ],
        )
        irt.load_existing.assert_called_once()
        irt.check_one.assert_not_called()
        unlink.assert_called()


if __name__ == "__main__":
    unittest.main()
