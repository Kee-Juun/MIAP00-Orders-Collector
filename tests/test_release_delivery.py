from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from config.settings import Settings
from core.collector import MIAP00Collector
from core.models import CounselRecord, ProcessingRecord
from core.release_delivery import (
    ReleaseDeliveryError,
    build_consolidated_release_folder,
    publish_consolidated_release_folder,
)


def collected_order(
    docket: str,
    document_date: str,
) -> ProcessingRecord:
    return ProcessingRecord(
        status="collected",
        docket=docket,
        title="",
        release_date="",
        source_filename=f"{docket}_source.pdf",
        source_url="",
        target_filename=f"LDC_SMD_{docket}_{document_date}.pdf",
        document_date=document_date,
    )


class ReleaseDeliveryTests(unittest.TestCase):
    def test_combines_orders_and_counsels_under_majority_date(self):
        with TemporaryDirectory() as directory:
            run_dir = Path(directory) / "MIAP00_test"
            orders = run_dir / "Collected Orders"
            counsels = run_dir / "Collected Counsels"
            orders.mkdir(parents=True)
            counsels.mkdir()
            (orders / "LDC_SMD_1_09222026.pdf").write_bytes(b"order-1")
            (orders / "LDC_SMD_2_09222026.pdf").write_bytes(b"order-2")
            (orders / "LDC_SMD_3_09212026.pdf").write_bytes(b"order-3")
            (counsels / "LDC_SMD_1counsel.html").write_text(
                "counsel", encoding="utf-8"
            )
            records = [
                collected_order("1", "09222026"),
                collected_order("2", "09222026"),
                collected_order("3", "09212026"),
            ]

            release = build_consolidated_release_folder(
                run_dir,
                records,
                orders,
                counsels,
                Mock(),
            )

            self.assertEqual(release.name, "09222026 Release Date")
            self.assertEqual(
                sorted(path.name for path in release.iterdir()),
                [
                    "LDC_SMD_1_09222026.pdf",
                    "LDC_SMD_1counsel.html",
                    "LDC_SMD_2_09222026.pdf",
                    "LDC_SMD_3_09212026.pdf",
                ],
            )

    def test_publish_merges_identical_files_but_rejects_conflicts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "run" / "09222026 Release Date"
            shared = root / "shared"
            destination = shared / source.name
            source.mkdir(parents=True)
            destination.mkdir(parents=True)
            (source / "one.pdf").write_bytes(b"same")
            (destination / "one.pdf").write_bytes(b"same")
            (source / "two.html").write_bytes(b"new")

            published = publish_consolidated_release_folder(
                source, shared, Mock()
            )
            self.assertEqual(published, destination)
            self.assertEqual((destination / "two.html").read_bytes(), b"new")

            (source / "one.pdf").write_bytes(b"different")
            with self.assertRaises(ReleaseDeliveryError):
                publish_consolidated_release_folder(source, shared, Mock())

    def test_shared_copy_is_skipped_when_any_order_error_exists(self):
        collector = MIAP00Collector(Settings())
        collector.logger = Mock()
        collector.release_dir = Path("09222026 Release Date")
        records = [
            collected_order("1", "09222026"),
            ProcessingRecord(
                status="error",
                docket="2",
                title="",
                release_date="",
                source_filename="source.pdf",
                source_url="",
            ),
        ]
        with patch("core.collector.publish_consolidated_release_folder") as publish:
            changed = collector._publish_release_if_clean(records)
        self.assertFalse(changed)
        publish.assert_not_called()

    def test_shared_copy_is_skipped_when_any_counsel_error_exists(self):
        collector = MIAP00Collector(Settings())
        collector.logger = Mock()
        collector.release_dir = Path("09222026 Release Date")
        collector.counsel_records = [
            CounselRecord(docket="1", status="error", reason="failed")
        ]
        records = [collected_order("1", "09222026")]
        with patch("core.collector.publish_consolidated_release_folder") as publish:
            changed = collector._publish_release_if_clean(records)
        self.assertFalse(changed)
        publish.assert_not_called()

    def test_zero_error_run_is_published(self):
        collector = MIAP00Collector(Settings(release_shared_root="shared"))
        collector.logger = Mock()
        collector.release_dir = Path("09222026 Release Date")
        records = [collected_order("1", "09222026")]
        with patch("core.collector.publish_consolidated_release_folder") as publish:
            changed = collector._publish_release_if_clean(records)
        self.assertFalse(changed)
        publish.assert_called_once_with(
            collector.release_dir,
            Path("shared"),
            collector.logger,
            cancel_event=collector.cancel_event,
        )


if __name__ == "__main__":
    unittest.main()
