from datetime import date, timedelta
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

from browser.michigan_courts import MichiganOrdersSite
from config.settings import Settings
from core.collector import (
    CollectionError,
    MIAP00Collector,
    collected_counsels_directory_for_run,
    collected_directory_for_run,
    collected_orders_directory_for_run,
    excluded_directory_for_run,
    main_irt_received_date_range,
    replace_file_with_retry,
)
from browser.irt import IRTError
from core.models import OrderResult
from core.naming import MissingCertifiedDecisionDateError, sha256_file


class CollectorFlowTests(unittest.TestCase):
    def test_main_irt_window_looks_back_one_month_through_today(self):
        self.assertEqual(
            main_irt_received_date_range(
                date(2026, 9, 4),
                date(2026, 9, 4),
                [date(2026, 9, 4)],
                today=date(2026, 9, 8),
            ),
            (date(2026, 8, 4), date(2026, 9, 8)),
        )

    def test_main_irt_window_preserves_older_certified_decision_coverage(self):
        self.assertEqual(
            main_irt_received_date_range(
                date(2026, 8, 7),
                date(2026, 8, 14),
                [date(2026, 7, 13)],
                today=date(2026, 8, 20),
            ),
            (date(2026, 6, 13), date(2026, 8, 20)),
        )

    def test_transient_windows_pdf_lock_is_retried_before_rename_failure(self):
        source = Path("temporary-order.pdf")
        destination = Path("LDC_SMD_379083_08262026.pdf")
        locked = PermissionError(13, "file is being used by another process")
        locked.winerror = 32
        logger = Mock()

        with patch.object(
            Path,
            "replace",
            side_effect=[locked, destination],
        ) as replace, patch("core.file_ops.cancellable_wait") as wait:
            replace_file_with_retry(source, destination, logger=logger)

        self.assertEqual(replace.call_count, 2)
        wait.assert_called_once()
        logger.warning.assert_called_once()

    def test_download_finalization_retries_transient_windows_file_lock(self):
        payload = b"%PDF-transient-lock-test" + (b"0" * 128)
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [payload]
        site = MichiganOrdersSite(Settings(), Mock())
        order = OrderResult(
            page=1,
            position=1,
            docket="380798",
            title="Test order",
            lower_court="",
            release_date="08/26/2026",
            order_type="Order",
            pdf_url="https://example.test/380798_16_01.pdf",
            original_filename="380798_16_01.pdf",
        )
        locked = PermissionError(13, "file is being used by another process")
        locked.winerror = 32
        attempts = 0

        def replace_after_lock(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise locked
            os.replace(source, destination)
            return destination

        with TemporaryDirectory() as directory, patch.object(
            site,
            "_open_download_response",
            return_value=response,
        ), patch.object(
            Path,
            "replace",
            autospec=True,
            side_effect=replace_after_lock,
        ), patch(
            "core.file_ops.cancellable_wait"
        ) as wait:
            destination = Path(directory) / "380798_16_01.pdf"
            byte_count = site.download_pdf(order, destination)

            self.assertEqual(byte_count, len(payload))
            self.assertEqual(destination.read_bytes(), payload)

        self.assertEqual(attempts, 2)
        wait.assert_called_once()

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

    def test_blank_certification_date_is_preserved_as_excluded_not_error(self):
        with TemporaryDirectory() as directory:
            settings = Settings(
                output_root=directory,
                start_date="2026-09-15",
                end_date="2026-09-16",
                collect_counsel=False,
            )
            order = OrderResult(
                page=1,
                position=1,
                docket="380983",
                title="IN RE ARD",
                lower_court="WEXFORD CIRCUIT COURT",
                release_date="09/15/2026",
                order_type="Order",
                pdf_url="https://example.test/380983_26_01.pdf",
                original_filename="380983_26_01.pdf",
            )
            site = Mock()
            site.collect_result_metadata.return_value = [order]

            def download(_order, destination, cancel_event=None):
                Path(destination).write_bytes(b"%PDF-undated-order")
                return Path(destination).stat().st_size

            site.download_pdf.side_effect = download
            irt = Mock()
            with patch(
                "core.collector.verify_us_location",
                return_value=Mock(display_name="United States"),
            ), patch(
                "core.collector.MichiganOrdersSite", return_value=site
            ), patch(
                "core.collector.IRTDuplicateChecker", return_value=irt
            ), patch(
                "core.collector.extract_document_date",
                side_effect=MissingCertifiedDecisionDateError(
                    "Certified MIAP00 decision date is blank"
                ),
            ):
                collector = MIAP00Collector(settings)
                run_dir = collector.run()

            excluded = run_dir / "Excluded" / order.original_filename
            self.assertTrue(excluded.is_file())
            self.assertEqual(collector.last_counts.get("missing_certified_date"), 1)
            self.assertEqual(collector.last_counts.get("error", 0), 0)

            workbook = __import__("openpyxl").load_workbook(
                run_dir / f"Report_{run_dir.name}.xlsx", data_only=True
            )
            self.assertEqual(
                workbook["Excluded"]["A2"].value,
                "missing_certified_date",
            )
            summary = dict(workbook["Summary"].values)
            self.assertEqual(summary["Orders missing certified dates excluded"], 1)
            for handler in list(collector.logger.handlers):
                handler.close()
                collector.logger.removeHandler(handler)

    def _run_one(
        self,
        duplicate_records,
        document_date="08142026",
        primary_docket="381603",
    ):
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

        expected = f"LDC_SMD_{primary_docket}_{document_date}.pdf"
        existing = {expected.lower(): duplicate_records} if duplicate_records else {}

        def preflight(start_date, end_date):
            events.append(
                f"irt-preflight:{start_date.isoformat()}:{end_date.isoformat()}"
            )

        def load_existing(start_date, end_date):
            events.append(f"irt-load:{start_date.isoformat()}:{end_date.isoformat()}")
            return existing

        def duplicate_lookup(filename, index):
            events.append(f"irt-compare:{filename}")
            return index.get(filename.lower(), [])

        irt.preflight.side_effect = preflight
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
        ), patch(
            "core.collector.extract_primary_docket", return_value=primary_docket
        ), patch("core.collector.ReportWriter.write"), patch(
            "core.collector.build_consolidated_release_folder",
            return_value=None,
        ), patch(
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

    def test_pdf_header_docket_controls_filename_and_irt_comparison(self):
        _run_dir, _irt, events, _unlink = self._run_one(
            [],
            primary_docket="381409",
        )
        expected_end = max(date.today(), date(2026, 8, 14)).isoformat()
        self.assertIn("rename:LDC_SMD_381409_08142026.pdf", events)
        self.assertIn("irt-compare:LDC_SMD_381409_08142026.pdf", events)
        self.assertIn(f"irt-load:2026-07-07:{expected_end}", events)

    def test_all_downloads_are_renamed_before_one_bulk_irt_check(self):
        _run_dir, irt, events, _unlink = self._run_one([])
        expected = "LDC_SMD_381603_08142026.pdf"
        expected_end = max(date.today(), date(2026, 8, 14)).isoformat()
        expected_preflight_start = (
            max(date.today(), date(2026, 8, 14)) - timedelta(days=13)
        ).isoformat()
        self.assertEqual(
            events,
            [
                f"irt-preflight:{expected_preflight_start}:{expected_end}",
                "download",
                f"rename:{expected}",
                f"irt-load:2026-07-07:{expected_end}",
                f"irt-compare:{expected}",
                f"rename:{expected}",
            ],
        )
        irt.load_existing.assert_called_once()
        irt.preflight.assert_called_once()
        irt.check_one.assert_not_called()

    def test_irt_snapshot_starts_at_oldest_certified_decision_date(self):
        _run_dir, irt, events, _unlink = self._run_one(
            [], document_date="07132026"
        )
        expected = "LDC_SMD_381603_07132026.pdf"
        expected_end = max(date.today(), date(2026, 8, 14)).isoformat()
        expected_preflight_start = (
            max(date.today(), date(2026, 8, 14)) - timedelta(days=13)
        ).isoformat()

        self.assertEqual(
            events,
            [
                f"irt-preflight:{expected_preflight_start}:{expected_end}",
                "download",
                f"rename:{expected}",
                f"irt-load:2026-06-13:{expected_end}",
                f"irt-compare:{expected}",
                f"rename:{expected}",
            ],
        )
        irt.load_existing.assert_called_once_with(
            date(2026, 6, 13), max(date.today(), date(2026, 8, 14))
        )

    def test_irt_duplicate_is_deleted_without_finalization(self):
        _run_dir, irt, events, unlink = self._run_one([{"LNI": "duplicate"}])
        expected = "LDC_SMD_381603_08142026.pdf"
        expected_end = max(date.today(), date(2026, 8, 14)).isoformat()
        expected_preflight_start = (
            max(date.today(), date(2026, 8, 14)) - timedelta(days=13)
        ).isoformat()
        self.assertEqual(
            events,
            [
                f"irt-preflight:{expected_preflight_start}:{expected_end}",
                "download",
                f"rename:{expected}",
                f"irt-load:2026-07-07:{expected_end}",
                f"irt-compare:{expected}",
            ],
        )
        irt.load_existing.assert_called_once()
        irt.check_one.assert_not_called()
        unlink.assert_called()

    def test_failed_irt_preflight_stops_before_pdf_download(self):
        with TemporaryDirectory() as directory:
            settings = Settings(
                output_root=directory,
                start_date="2026-08-14",
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
            site = Mock()
            site.collect_result_metadata.return_value = [order]
            irt = Mock()
            irt.preflight.side_effect = IRTError("IRT unavailable")
            logger = Mock()
            with patch(
                "core.collector.verify_us_location",
                return_value=Mock(display_name="United States"),
            ), patch(
                "core.collector.MichiganOrdersSite", return_value=site
            ), patch(
                "core.collector.IRTDuplicateChecker", return_value=irt
            ), patch(
                "core.collector.ReportWriter.write"
            ), patch(
                "core.collector.create_logger",
                return_value=(logger, Path(directory) / "run.log"),
            ):
                collector = MIAP00Collector(settings)
                with self.assertRaisesRegex(CollectionError, "IRT preflight"):
                    collector.run()

            site.download_pdf.assert_not_called()
            irt.load_existing.assert_not_called()
            self.assertEqual(collector.last_counts.get("error"), 1)


if __name__ == "__main__":
    unittest.main()
