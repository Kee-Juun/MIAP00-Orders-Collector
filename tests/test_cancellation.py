from pathlib import Path
import logging
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

from PIL import Image

from browser.irt import IRTDuplicateChecker
from browser.michigan_courts import MichiganOrdersSite
from browser.webdriver_factory import cancellable_navigate
from config.settings import Settings
from core.cancellation import CollectionCancelled
from core.collector import MIAP00Collector
from core.content_duplicates import remove_content_duplicates
from core.models import OrderResult, ProcessingRecord
from core.naming import _ocr_image


class CancellationTests(unittest.TestCase):
    def _order(self) -> OrderResult:
        return OrderResult(
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

    def test_collector_stop_after_rename_skips_irt_and_removes_pending_pdf(self):
        cancel_event = threading.Event()
        site = Mock()
        site.collect_result_metadata.return_value = [self._order()]

        def download(_order, destination, cancel_event=None):
            destination.write_bytes(b"%PDF- synthetic")
            return destination.stat().st_size

        site.download_pdf.side_effect = download
        irt = Mock()
        logger = Mock()

        def progress(current, total):
            if current == total == 1:
                cancel_event.set()

        with tempfile.TemporaryDirectory() as temp_root, patch(
            "core.collector.verify_us_location"
        ), patch(
            "core.collector.MichiganOrdersSite",
            return_value=site,
        ), patch(
            "core.collector.IRTDuplicateChecker",
            return_value=irt,
        ), patch(
            "core.collector.extract_document_date",
            return_value="08142026",
        ), patch(
            "core.collector.ReportWriter.write"
        ), patch(
            "core.collector.create_logger",
            return_value=(logger, Path(temp_root) / "run.log"),
        ):
            collector = MIAP00Collector(
                Settings(
                    output_root=temp_root,
                    start_date="2026-08-14",
                    end_date="2026-08-14",
                ),
                progress_callback=progress,
                cancel_event=cancel_event,
            )
            run_dir = collector.run()

            orders_dir = run_dir / f"Collected_Orders_{run_dir.name}"
            counsels_dir = run_dir / f"Collected_Counsels_{run_dir.name}"
            excluded_dir = run_dir / "Excluded"
            self.assertFalse(orders_dir.exists())
            self.assertFalse(counsels_dir.exists())
            self.assertFalse(excluded_dir.exists())

        irt.load_existing.assert_not_called()
        self.assertEqual(collector.last_counts.get("cancelled"), 1)
        self.assertEqual(collector.last_counts.get("error", 0), 0)
        self.assertTrue(collector.was_cancelled)
        self.assertTrue(
            any(
                call.args and call.args[0].startswith("Run %s:")
                and call.args[1] == "stopped"
                for call in logger.info.call_args_list
            )
        )
        site.close.assert_called_once()
        irt.close.assert_called_once()

    def test_irt_wait_reacts_to_preexisting_stop_without_clicking_search(self):
        event = threading.Event()
        event.set()
        checker = IRTDuplicateChecker(
            Settings(),
            logging.getLogger("test.cancel.irt"),
            cancel_event=event,
        )
        checker.driver = Mock()

        started = time.monotonic()
        with self.assertRaises(CollectionCancelled):
            checker._search()
        self.assertLess(time.monotonic() - started, 0.5)
        checker.driver.find_element.assert_not_called()

    def test_blocked_browser_navigation_terminates_driver_service_on_stop(self):
        event = threading.Event()
        navigation_released = threading.Event()
        driver = Mock()
        driver.get.side_effect = lambda _url: navigation_released.wait(5)
        driver.service.process.poll.return_value = None
        driver.service.process.terminate.side_effect = navigation_released.set
        timer = threading.Timer(0.05, event.set)
        timer.start()
        try:
            with self.assertRaises(CollectionCancelled):
                cancellable_navigate(driver, "https://example.test", event)
        finally:
            timer.cancel()

        driver.service.process.terminate.assert_called_once()

    def test_streaming_download_removes_partial_file_when_stopped(self):
        event = threading.Event()
        site = MichiganOrdersSite(Settings(), Mock())
        response = MagicMock()
        response.__enter__.return_value = response
        response.raise_for_status.return_value = None

        def chunks(_size):
            yield b"%PDF- first chunk"
            event.set()
            yield b"second chunk"

        response.iter_content.side_effect = chunks
        session = MagicMock()
        session.get.return_value = response

        with tempfile.TemporaryDirectory() as temp_root, patch(
            "browser.michigan_courts.requests.Session",
            return_value=session,
        ):
            destination = Path(temp_root) / "order.pdf"
            with self.assertRaises(CollectionCancelled):
                site.download_pdf(self._order(), destination, event)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".pdf.part").exists())

    def test_blocked_pdf_connection_returns_as_soon_as_stop_is_requested(self):
        event = threading.Event()
        connection_released = threading.Event()
        session = MagicMock()
        session.get.side_effect = lambda *_args, **_kwargs: connection_released.wait(5)
        session.close.side_effect = connection_released.set
        site = MichiganOrdersSite(Settings(), Mock())
        timer = threading.Timer(0.05, event.set)
        timer.start()
        try:
            with tempfile.TemporaryDirectory() as temp_root, patch(
                "browser.michigan_courts.requests.Session",
                return_value=session,
            ):
                started = time.monotonic()
                with self.assertRaises(CollectionCancelled):
                    site.download_pdf(
                        self._order(),
                        Path(temp_root) / "order.pdf",
                        event,
                    )
                self.assertLess(time.monotonic() - started, 0.75)
        finally:
            timer.cancel()

        session.close.assert_called()

    def test_ocr_does_not_start_when_stop_is_already_requested(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(CollectionCancelled), patch(
            "core.naming.subprocess.Popen"
        ) as popen:
            _ocr_image(Image.new("RGB", (8, 8)), "tesseract", event)
        popen.assert_not_called()

    def test_active_ocr_process_is_terminated_when_stop_arrives(self):
        event = threading.Event()
        process = Mock()
        process.poll.return_value = None

        def still_running(timeout):
            event.set()
            raise subprocess.TimeoutExpired("tesseract", timeout)

        process.communicate.side_effect = still_running
        with patch(
            "core.naming.subprocess.Popen",
            return_value=process,
        ):
            with self.assertRaises(CollectionCancelled):
                _ocr_image(Image.new("RGB", (8, 8)), "tesseract", event)

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=1)

    def test_duplicate_analysis_stops_before_reading_files(self):
        event = threading.Event()
        event.set()
        records = [
            ProcessingRecord(
                status="collected",
                docket=str(381600 + index),
                title="",
                release_date="08/14/2026",
                source_filename=f"source-{index}.pdf",
                source_url="",
                target_filename=f"LDC_SMD_{381600 + index}_08142026.pdf",
                document_date="08142026",
            )
            for index in range(2)
        ]
        with self.assertRaises(CollectionCancelled), patch(
            "pathlib.Path.is_file",
            return_value=True,
        ), patch(
            "core.content_duplicates._read_pdf_text"
        ) as read_text:
            remove_content_duplicates(records, Path("synthetic"), Mock(), event)
        read_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
