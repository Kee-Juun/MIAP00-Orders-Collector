from datetime import date
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.naming import (
    MissingCertifiedDecisionDateError,
    NamingError,
    NonOrderDocumentError,
    build_filename,
    docket_suffix,
    extract_document_date,
    extract_miap00_date_from_text,
    extract_pdf_text,
    extract_primary_docket,
    extract_primary_docket_from_text,
    extract_source_docket,
    normalize_final_key,
    verify_tesseract,
    _find_tesseract,
    _extract_order_date,
    _ocr_footer_image,
)


class NamingTests(unittest.TestCase):
    def test_frozen_runtime_tesseract_is_preferred(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "Tesseract-OCR" / "tesseract.exe"
            executable.parent.mkdir()
            executable.touch()
            with patch.object(sys, "_MEIPASS", directory, create=True):
                self.assertEqual(_find_tesseract(), str(executable))

    def test_tesseract_verification_requires_english_language_data(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "tesseract.exe"
            executable.touch()
            with patch("core.naming._find_tesseract", return_value=str(executable)):
                available, details = verify_tesseract()

        self.assertFalse(available)
        self.assertIn("eng.traineddata", details)

    def test_tesseract_verification_launches_resolved_engine(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "tesseract.exe"
            executable.touch()
            tessdata = Path(directory) / "tessdata"
            tessdata.mkdir()
            (tessdata / "eng.traineddata").touch()
            completed = SimpleNamespace(returncode=0, stdout="tesseract 5.5.0\n", stderr="")
            with patch("core.naming._find_tesseract", return_value=str(executable)), patch(
                "core.naming.subprocess.run", return_value=completed
            ) as run:
                available, details = verify_tesseract()

        self.assertTrue(available)
        self.assertIn("tesseract 5.5.0", details)
        self.assertEqual(run.call_args.args[0], [str(executable), "--version"])
        self.assertEqual(run.call_args.kwargs["env"]["TESSDATA_PREFIX"], str(tessdata))

    def test_sparse_footer_ocr_retries_with_uniform_block_layout(self):
        recovered = (
            "A true copy entered and certified by Jerome W. Zimmer Jr., "
            "Chief Clerk, on\nAugust 28, 2026\nDate\nChief Clerk"
        )
        logger = Mock()

        with patch(
            "core.naming._ocr_image",
            side_effect=["", recovered],
        ) as ocr:
            result = _ocr_footer_image(Mock(), "tesseract", logger=logger)

        self.assertEqual(_extract_order_date(result), "08282026")
        self.assertEqual(ocr.call_count, 2)
        self.assertIsNone(ocr.call_args_list[0].kwargs.get("page_segmentation_mode"))
        self.assertEqual(
            ocr.call_args_list[1].kwargs["page_segmentation_mode"],
            6,
        )
        self.assertIn("sparse footer layout", logger.info.call_args.args[0])

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

    def test_pdf_header_docket_wins_over_later_consolidation_reference(self):
        text = """Court of Appeals, State of Michigan
ORDER
MICHIGAN FARM BUREAU V DEPT OF ENVIRONMENT GREAT LAKES AND ENERGY
Docket No. 381409
LC No. 25-006752-AA
The application for leave to appeal is GRANTED.
This case is CONSOLIDATED with the application filed in Docket No. 381408.
"""

        self.assertEqual(extract_primary_docket_from_text(text), "381409")
        logger = Mock()
        with patch("core.naming.extract_pdf_text", return_value=text):
            self.assertEqual(
                extract_primary_docket(
                    Path("381408_14_01.pdf"),
                    "381408",
                    logger=logger,
                ),
                "381409",
            )
        self.assertIn("381408 -> 381409", logger.warning.call_args.args[0])

    def test_primary_docket_falls_back_when_pdf_header_is_unavailable(self):
        with patch("core.naming.extract_pdf_text", return_value=""):
            self.assertEqual(
                extract_primary_docket(Path("381408_14_01.pdf"), "381408"),
                "381408",
            )

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

    def test_misspelled_footer_months_are_corrected_against_release_date(self):
        cases = (
            ("Sepember 17, 2026", "09/17/2026", "09172026"),
            ("Decmber 8, 2026", "12/08/2026", "12082026"),
            ("Febraruy 12, 2026", "02/12/2026", "02122026"),
            ("Agust 3, 2026", "08/03/2026", "08032026"),
        )
        for printed_date, release_date, expected in cases:
            footer = (
                "A true copy entered and certified by Jerome W. Zimmer Jr., "
                f"Chief Clerk, on\n{printed_date}\nDate\nChief Clerk"
            )
            with self.subTest(printed_date=printed_date), patch(
                "core.naming.extract_pdf_text", return_value="ORDER"
            ), patch(
                "core.naming.extract_pdf_footer_text_with_ocr",
                return_value=footer,
            ), patch(
                "core.naming.extract_pdf_text_with_ocr"
            ) as full_ocr:
                result = extract_document_date(
                    Path("order.pdf"), expected_date=release_date
                )

            self.assertEqual(result, expected)
            full_ocr.assert_not_called()

    def test_misspelled_month_requires_matching_release_date(self):
        footer = (
            "A true copy entered and certified by Jerome W. Zimmer Jr., "
            "Chief Clerk, on\nAgust 3, 2026\nDate\nChief Clerk"
        )
        with patch("core.naming.extract_pdf_text", return_value="ORDER"), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value=footer,
        ), patch(
            "core.naming.extract_pdf_footer_text", return_value=""
        ), patch(
            "core.naming.extract_pdf_text_with_ocr", return_value="ORDER"
        ):
            with self.assertRaises(MissingCertifiedDecisionDateError):
                extract_document_date(
                    Path("order.pdf"), expected_date="09/03/2026"
                )

    def test_misspelled_body_month_is_not_used_as_decision_date(self):
        body = "ORDER\nThe response is due Agust 3, 2026."
        blank_footer = (
            "A true copy entered and certified by Jerome W. Zimmer Jr., "
            "Chief Clerk, on\nDate\nChief Clerk"
        )
        with patch("core.naming.extract_pdf_text", return_value=body), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value=blank_footer,
        ), patch(
            "core.naming.extract_pdf_footer_text", return_value=""
        ), patch(
            "core.naming.extract_pdf_text_with_ocr", return_value=body
        ):
            with self.assertRaises(MissingCertifiedDecisionDateError):
                extract_document_date(
                    Path("order.pdf"), expected_date="08/03/2026"
                )

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

    def test_blank_certification_date_is_a_document_exclusion(self):
        footer = (
            "A true copy entered and certified by Jerome W. Zimmer Jr., "
            "Chief Clerk, on\nDate ChieTTlerk\nSe Date Chief Clerk"
        )
        with patch("core.naming.extract_pdf_text", return_value="ORDER"), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value=footer,
        ), patch(
            "core.naming.extract_pdf_text_with_ocr", return_value="ORDER"
        ):
            with self.assertRaises(MissingCertifiedDecisionDateError):
                extract_document_date(
                    Path("undated-order.pdf"), expected_date="09/15/2026"
                )

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

    def test_consolidated_cases_policy_handout_is_excluded_before_ocr(self):
        handout = (
            "Michigan Court of Appeals\nOffice of the Clerk\n"
            "POLICY ON CONSOLIDATED CASES\n"
            "The enclosed order consolidates the noted appeals. This statement "
            "explains the effect of consolidation on the appellate process.\n"
            "FILING DEADLINES regarding transcripts, motions or briefs will not "
            "be affected by the consolidation."
        )
        with patch("core.naming.extract_pdf_text", return_value=handout), patch(
            "core.naming.extract_pdf_footer_text_with_ocr"
        ) as footer_ocr, patch("core.naming.extract_pdf_text_with_ocr") as full_ocr:
            with self.assertRaisesRegex(
                NonOrderDocumentError, "consolidated-cases policy handout"
            ):
                extract_document_date(
                    Path("376940_40_02.pdf"), expected_date="09/01/2026"
                )

        footer_ocr.assert_not_called()
        full_ocr.assert_not_called()

    def test_certified_order_with_consolidation_policy_reference_is_not_excluded(self):
        body = (
            "Michigan Court of Appeals\nOffice of the Clerk\nORDER\n"
            "The cases are consolidated. See the POLICY ON CONSOLIDATED CASES.\n"
            "The enclosed order consolidates the noted appeals. This statement "
            "explains the effect of consolidation.\n"
            "A TRUE COPY ENTERED AND CERTIFIED"
        )
        with patch("core.naming.extract_pdf_text", return_value=body), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value="September 1, 2026\nChief Clerk\nDate",
        ):
            self.assertEqual(
                extract_document_date(
                    Path("consolidated-order.pdf"), expected_date="09/01/2026"
                ),
                "09012026",
            )

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

    def test_footer_text_layer_recovers_date_omitted_by_ocr(self):
        footer_ocr = (
            "A true copy entered and certified by Jerome W. Zimmer Jr., "
            "Chief Clerk, on\nDate ChieTTlerk"
        )
        logger = Mock()
        with patch("core.naming.extract_pdf_text", return_value="ORDER"), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value=footer_ocr,
        ), patch(
            "core.naming.extract_pdf_footer_text",
            return_value="September 18, 2026",
        ) as footer_layer, patch(
            "core.naming.extract_pdf_text_with_ocr"
        ) as full_ocr:
            result = extract_document_date(
                Path("381591_17_01.pdf"), expected_date="09/18/2026", logger=logger
            )

        self.assertEqual(result, "09182026")
        footer_layer.assert_called_once()
        full_ocr.assert_not_called()
        self.assertIn("footer text layer", logger.info.call_args.args[0])

    def test_footer_text_layer_is_not_used_without_certification_legend(self):
        with patch("core.naming.extract_pdf_text", return_value="ORDER"), patch(
            "core.naming.extract_pdf_footer_text_with_ocr",
            return_value="Date Chief Clerk",
        ), patch(
            "core.naming.extract_pdf_footer_text"
        ) as footer_layer, patch(
            "core.naming.extract_pdf_text_with_ocr", return_value="ORDER"
        ):
            with self.assertRaises(NamingError):
                extract_document_date(
                    Path("order.pdf"), expected_date="09/18/2026"
                )

        footer_layer.assert_not_called()

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
