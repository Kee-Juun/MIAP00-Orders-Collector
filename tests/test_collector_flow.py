from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import Mock, patch

from config.settings import Settings
from core.collector import (
    MIAP00Collector,
    collected_counsels_directory_for_run,
    collected_directory_for_run,
    collected_orders_directory_for_run,
    excluded_directory_for_run,
)
from core.models import OrderResult
from core.naming import sha256_file


class CollectorFlowTests(unittest.TestCase):
    def test_collected_files_use_separate_run_named_subfolders(self):
        run_dir = Path("output") / "MIAP00_08-15-2026_18-01-23-355"
        self.assertEqual(
            collected_orders_directory_for_run(run_dir),
            run_dir / "Collected_Orders_MIAP00_08-15-2026_18-01-23-355",
        )
        self.assertEqual(
            collected_counsels_directory_for_run(run_dir),
            run_dir / "Collected_Counsels_MIAP00_08-15-2026_18-01-23-355",
        )
        self.assertEqual(
            collected_directory_for_run(run_dir),
            collected_orders_directory_for_run(run_dir),
        )
        self.assertEqual(excluded_directory_for_run(run_dir), run_dir / "Excluded")

    def test_collected_folder_is_created_only_when_needed_and_empty_one_is_removed(self):
        with TemporaryDirectory() as directory:
            collector = MIAP00Collector(Settings())
            collector.collected_dir = Path(directory) / "Collected_Orders_test"
            collector.counsel_dir = Path(directory) / "Collected_Counsels_test"
            collector.excluded_dir = Path(directory) / "Excluded"

            self.assertFalse(collector.collected_dir.exists())
            self.assertFalse(collector.counsel_dir.exists())
            collector._remove_empty_collected_dir()
            self.assertFalse(collector.collected_dir.exists())

            collector._ensure_collected_dir()
            self.assertTrue(collector.collected_dir.is_dir())
            collector._remove_empty_collected_dir()
            self.assertFalse(collector.collected_dir.exists())

            collector._ensure_counsel_dir()
            self.assertTrue(collector.counsel_dir.is_dir())
            collector._remove_empty_collected_dir()
            self.assertFalse(collector.counsel_dir.exists())

            collector._ensure_excluded_dir()
            self.assertTrue(collector.excluded_dir.is_dir())
            collector._remove_empty_collected_dir()
            self.assertFalse(collector.excluded_dir.exists())

    def test_nonempty_collected_folder_is_never_removed(self):
        with TemporaryDirectory() as directory:
            collector = MIAP00Collector(Settings())
            collector.collected_dir = Path(directory) / "Collected_Orders_test"
            collector.counsel_dir = Path(directory) / "Collected_Counsels_test"
            collector.excluded_dir = Path(directory) / "Excluded"
            collector._ensure_collected_dir()
            artifact = collector.collected_dir / "sample.pdf"
            artifact.write_bytes(b"%PDF-test")

            collector._remove_empty_collected_dir()

            self.assertTrue(artifact.is_file())

    def test_excluded_file_is_preserved_with_original_filename(self):
        with TemporaryDirectory() as directory:
            run_dir = Path(directory)
            source = run_dir / "00015_379060_48_01.pdf"
            source.write_bytes(b"%PDF-party-filing")
            collector = MIAP00Collector(Settings())
            collector.logger = Mock()
            collector.excluded_dir = run_dir / "Excluded"
            order = OrderResult(
                page=1,
                position=15,
                docket="379060",
                title="LARSON V LARSON",
                lower_court="KENT CIRCUIT COURT",
                release_date="08/17/2026",
                order_type="Order",
                pdf_url="https://example.test/379060_48_01.pdf",
                original_filename="379060_48_01.pdf",
            )

            record = collector._preserve_excluded_file(
                order,
                source,
                source.stat().st_size,
                "Received party filing",
            )

            destination = collector.excluded_dir / "379060_48_01.pdf"
            self.assertTrue(destination.is_file())
            self.assertFalse(source.exists())
            self.assertEqual(record.status, "non_order")
            self.assertEqual(record.target_filename, "379060_48_01.pdf")
            self.assertEqual(record.sha256, sha256_file(destination))

    def _run_one(self, duplicate_records, document_date="08142026"):
        settings = Settings(
            output_root="synthetic-output",
            start_date="2026-08-07",
            end_date="2026-08-14",
            collect_counsel=False,
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

        expected = f"LDC_SMD_381603_{document_date}.pdf"
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

        with patch("core.collector.verify_us_location") as location_check, patch(
            "core.collector.MichiganOrdersSite", return_value=site
        ), patch(
            "core.collector.IRTDuplicateChecker", return_value=irt
        ), patch(
            "core.collector.extract_document_date", return_value=document_date
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
        location_check.assert_called_once_with(timeout_seconds=8)
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

    def test_irt_snapshot_starts_at_oldest_certified_decision_date(self):
        _run_dir, irt, events, _unlink = self._run_one(
            [], document_date="07132026"
        )
        expected = "LDC_SMD_381603_07132026.pdf"

        self.assertEqual(
            events,
            [
                "download",
                f"rename:{expected}",
                "irt-load:2026-07-13:2026-08-14",
                f"irt-compare:{expected}",
                f"rename:{expected}",
            ],
        )
        irt.load_existing.assert_called_once_with(
            date(2026, 7, 13), date(2026, 8, 14)
        )

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
