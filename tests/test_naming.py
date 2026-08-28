from datetime import date
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.naming import (
    NamingError,
    NonOrderDocumentError,
    build_filename,
    docket_suffix,
    extract_document_date,
    extract_miap00_date_from_text,
    extract_pdf_text,
    extract_source_docket,
    normalize_final_key,
)


class NamingTests(unittest.TestCase):
    def test_pdf_text_stream_is_closed_before_extraction_returns(self):
        observed = {}

        class FakePage:
            @staticmethod
            def extract_text():
                return "ORDER"

        class FakeReader:
            def __init__(self, stream):
                observed["stream"] = stream
                self.pages = [FakePage()]

        with TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "order.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            with patch.dict(
                sys.modules,
                {"pypdf": SimpleNamespace(PdfReader=FakeReader)},
            ):
                self.assertEqual(extract_pdf_text(pdf_path), "ORDER")

        self.assertTrue(observed["stream"].closed)

    def test_live_site_order_filename(self):
        self.assertEqual(extract_source_docket("381603_6_01.pdf"), "381603")

    def test_live_site_order_filename_without_part_segment(self):
        self.assertEqual(extract_source_docket("372786_81.pdf"), "372786")

    def test_fileflex_opinion_order_variants(self):
        self.assertEqual(extract_source_docket("20260813_C381603_1_381603.opn_ORDER.pdf"), "381603")
        self.assertEqual(extract_source_docket("20260813_C381603(1)_RPTR_X-381603-ASV..pdf"), "381603")

    def test_invalid_source_name_is_rejected(self):
        with self.assertRaises(NamingError):
            extract_source_docket("order.pdf")

    def test_fileflex_suffix_sequence(self):
        expected = {0: "", 1: "a", 2: "b", 26: "z", 27: "aa", 28: "ab", 52: "az", 53: "ba"}
        for occurrence, suffix in expected.items():
            self.assertEqual(docket_suffix(occurrence), suffix)

    def test_target_filename(self):
        self.assertEqual(build_filename("381603", "08132026"), "LDC_SMD_381603_08132026.pdf")
        self.assertEqual(build_filename("381603", "08132026", 1), "LDC_SMD_381603a_08132026.pdf")

    def test_publication_date(self):
        text = "FOR PUBLICATION\nSTATE OF MICHIGAN\nAugust 13, 2026\nCourt of Appeals"
        self.assertEqual(extract_miap00_date_from_text(text), "08132026")

    def test_order_certification_date(self):
        text = "ORDER\nSome body text\nA TRUE COPY ENTERED AND CERTIFIED\nAugust 13, 2 0 2 6"
        self.assertEqual(extract_miap00_date_from_text(text), "08132026")

    def test_body_deadline_is_not_used_as_order_date(self):
        text = (
            "ORDER\nThe motion to extend the brief is GRANTED until "
            "October 30, 2026."
        )
        self.assertEqual(extract_miap00_date_from_text(text), "")

    def test_ocr_footer_date_is_used_instead_of_body_deadline(self):
        text = (
            "ORDER\nThe motion to extend the brief is GRANTED until October 30, 2026.\n"
            "August 12, 2026 Signature\nChief Clerk\nDate"
        )
        self.assertEqual(extract_miap00_date_from_text(text), "08122026")

    def test_body_date_before_certification_is_not_accepted(self):
        text = (
            "ORDER\nThe brief is due September 15, 2026.\n"
            "A true copy entered and certified by the Chief Clerk\nDate"
        )
        self.assertEqual(extract_miap00_date_from_text(text), "")

    def test_date_label_before_certification_value(self):
        text = (
            "ORDER\nThe reply brief is due August 31, 2026.\nDate\n"
            "August 12, 2026 Signature\nChief Clerk"
        )
        self.assertEqual(extract_miap00_date_from_text(text), "08122026")

    def test_order_pdf_always_uses_footer_ocr(self):
        body = "ORDER\nThe brief is due September 15, 2026."
        footer = "August 12, 2026 Signature\nChief Clerk\nDate"
        with patch("core.naming.extract_pdf_text", return_value=body), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value=footer,
        ) as footer_ocr, patch(
            "core.naming.extract_pdf_text_with_ocr"
        ) as full_ocr:
            result = extract_document_date(
                Path("order.pdf"), expected_date="08/12/2026"
            )
        self.assertEqual(result, "08122026")
        footer_ocr.assert_called_once()
        full_ocr.assert_not_called()

    def test_received_party_filing_is_excluded_before_ocr(self):
        filing = (
            "ANNE ARGIROFF\nAttorney at Law\nAugust 17, 2026\n"
            "Dear Clerk:\nPlease withdraw the Motion for Extension.\n"
            "RECEIVED by MCOA 8/17/2026 7:32:47 AM"
        )
        with patch("core.naming.extract_pdf_text", return_value=filing), patch(
            "core.naming.extract_pdf_footer_text_with_ocr"
        ) as footer_ocr, patch("core.naming.extract_pdf_text_with_ocr") as full_ocr:
            with self.assertRaisesRegex(NonOrderDocumentError, "party filing"):
                extract_document_date(
                    Path("379060_48_01.pdf"), expected_date="08/17/2026"
                )

        footer_ocr.assert_not_called()
        full_ocr.assert_not_called()

    def test_event_reference_placeholder_is_excluded_before_ocr(self):
        for placeholder in ("See event 39", "\n  See   event 35.  \n"):
            with self.subTest(placeholder=placeholder), patch(
                "core.naming.extract_pdf_text", return_value=placeholder
            ), patch(
                "core.naming.extract_pdf_footer_text_with_ocr"
            ) as footer_ocr, patch(
                "core.naming.extract_pdf_text_with_ocr"
            ) as full_ocr:
                with self.assertRaisesRegex(
                    NonOrderDocumentError, "event-reference placeholder"
                ):
                    extract_document_date(Path("placeholder.pdf"))

                footer_ocr.assert_not_called()
                full_ocr.assert_not_called()

    def test_order_that_mentions_event_reference_is_not_excluded(self):
        body = "ORDER\nFor supporting materials, see event 39."
        with patch("core.naming.extract_pdf_text", return_value=body), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value="August 28, 2026\nChief Clerk\nDate",
        ) as footer_ocr:
            result = extract_document_date(
                Path("order.pdf"), expected_date="08/28/2026"
            )

        self.assertEqual(result, "08282026")
        footer_ocr.assert_called_once()

    def test_clerk_correspondence_is_excluded_before_ocr(self):
        letter = (
            "Michigan Court of Appeals\nOffice of the Clerk\nAugust 18, 2026\n"
            "Dear Counsel:\nThe publication request that was filed in this matter "
            "was submitted to the panel that filed the opinion. Please be advised "
            "that the panel denied the request.\nSincerely,\nJerome W. Zimmer Jr."
        )
        with patch("core.naming.extract_pdf_text", return_value=letter), patch(
            "core.naming.extract_pdf_footer_text_with_ocr"
        ) as footer_ocr, patch("core.naming.extract_pdf_text_with_ocr") as full_ocr:
            with self.assertRaisesRegex(NonOrderDocumentError, "clerk correspondence"):
                extract_document_date(
                    Path("372786_81.pdf"), expected_date="08/18/2026"
                )

        footer_ocr.assert_not_called()
        full_ocr.assert_not_called()

    def test_clerk_cover_letter_does_not_exclude_attached_certified_order(self):
        body = (
            "Michigan Court of Appeals\nOffice of the Clerk\nDear Counsel:\n"
            "Sincerely,\nORDER\nA TRUE COPY ENTERED AND CERTIFIED"
        )
        with patch("core.naming.extract_pdf_text", return_value=body), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value="August 18, 2026\nChief Clerk\nDate",
        ):
            self.assertEqual(
                extract_document_date(
                    Path("cover-and-order.pdf"), expected_date="08/18/2026"
                ),
                "08182026",
            )

    def test_received_stamp_does_not_exclude_certified_order(self):
        text = (
            "ORDER\nRECEIVED by MCOA 8/17/2026\n"
            "A TRUE COPY ENTERED AND CERTIFIED\nAugust 17, 2026"
        )
        with patch("core.naming.extract_pdf_text", return_value=text), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value="August 17, 2026\nChief Clerk",
        ):
            self.assertEqual(
                extract_document_date(
                    Path("real-order.pdf"), expected_date="08/17/2026"
                ),
                "08172026",
            )

    def test_panel_order_footer_uses_visible_release_date(self):
        with patch("core.naming.extract_pdf_text", return_value="ORDER"), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value="August 13, 2026\nDate",
        ) as footer_ocr, patch(
            "core.naming.extract_pdf_text_with_ocr"
        ) as full_ocr:
            result = extract_document_date(
                Path("panel-order.pdf"), expected_date="08/13/2026"
            )
        self.assertEqual(result, "08132026")
        footer_ocr.assert_called_once()
        full_ocr.assert_not_called()

    def test_certified_footer_date_overrides_release_date_before_irt(self):
        logger = Mock()
        with patch("core.naming.extract_pdf_text", return_value="ORDER"), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value="August 13, 2026\nChief Clerk\nDate",
        ):
            result = extract_document_date(
                Path("order.pdf"), logger=logger, expected_date="08/12/2026"
            )

        self.assertEqual(result, "08132026")
        self.assertIn("certification footer controls", logger.warning.call_args.args[0])

    def test_certified_date_in_selected_range_overrides_stale_site_date(self):
        logger = Mock()
        with patch("core.naming.extract_pdf_text", return_value="ORDER"), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value="August 14, 2026\nChief Clerk\nDate",
        ):
            result = extract_document_date(
                Path("375536_71_08.pdf"),
                logger=logger,
                expected_date="08/12/2026",
                allowed_date_range=(date(2026, 8, 12), date(2026, 8, 15)),
            )

        self.assertEqual(result, "08142026")
        logger.warning.assert_called_once()
        self.assertIn("mismatch accepted", logger.warning.call_args.args[0])

    def test_reposted_certified_date_outside_selected_range_is_accepted(self):
        logger = Mock()
        with patch("core.naming.extract_pdf_text", return_value="ORDER"), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value="July 13, 2026\nChief Clerk\nDate",
        ):
            result = extract_document_date(
                Path("377922_65_02.pdf"),
                logger=logger,
                expected_date="08/20/2026",
                allowed_date_range=(date(2026, 8, 17), date(2026, 8, 21)),
            )

        self.assertEqual(result, "07132026")
        warning = logger.warning.call_args.args[0]
        self.assertIn("older certified order", warning)
        self.assertIn("certification footer controls", warning)

    def test_hearing_dates_are_not_used_without_certification_footer(self):
        text = (
            "ORDER\nHearings were held on December 14, 2023, February 23, 2024, "
            "and September 20, 2024."
        )
        self.assertEqual(extract_miap00_date_from_text(text), "")

    def test_irt_key_includes_fileflex_suffix(self):
        self.assertEqual(normalize_final_key("LDC_SMD_381603a_08132026.pdf"), "381603a|08132026")


if __name__ == "__main__":
    unittest.main()
