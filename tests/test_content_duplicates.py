from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from core.content_duplicates import (
    find_irt_backed_consolidated_duplicates,
    remove_content_duplicates,
)
from core.models import ProcessingRecord


class ContentDuplicateTests(unittest.TestCase):
    def _record(self, filename: str, docket: str) -> ProcessingRecord:
        return ProcessingRecord(
            status="collected",
            docket=docket,
            title="",
            release_date="08/13/2026",
            source_filename=f"{docket}_source.pdf",
            source_url="",
            target_filename=filename,
            document_date="08132026",
        )

    def test_identical_text_keeps_unsuffixed_first_file(self):
        run_dir = Path("synthetic-run")
        first = self._record("LDC_SMD_381480_08132026.pdf", "381480")
        sibling = self._record("LDC_SMD_381480a_08132026.pdf", "381480")
        text = "ORDER Docket No. 381480 The motion is GRANTED. August 13, 2026"

        with patch("pathlib.Path.is_file", return_value=True), patch(
            "core.content_duplicates._read_pdf_text", return_value=text
        ), patch("pathlib.Path.unlink", autospec=True) as unlink:
            removed = remove_content_duplicates([first, sibling], run_dir, Mock())

        self.assertEqual(removed, 1)
        self.assertEqual(first.status, "collected")
        self.assertEqual(sibling.status, "content_duplicate")
        unlink.assert_called_once()
        self.assertEqual(unlink.call_args.args[0].name, sibling.target_filename)
        self.assertIn(first.target_filename, sibling.reason)

    def test_consolidated_case_copies_with_different_headers_are_removed(self):
        run_dir = Path("synthetic-run")
        first = self._record("LDC_SMD_380001_08132026.pdf", "380001")
        second = self._record("LDC_SMD_381480_08132026.pdf", "381480")
        shared_body = (
            "The motion to dismiss this appeal is GRANTED. Defendant may challenge "
            "all portions of the order. Docket numbers 380001 and 381480 are now "
            "DISCONSOLIDATED. Presiding Judge August 13, 2026."
        )
        texts = {
            first.target_filename: "ORDER Case Alpha Docket No. 380001 " + shared_body,
            second.target_filename: "ORDER Case Alpha Docket No. 381480 " + shared_body,
        }

        with patch("pathlib.Path.is_file", return_value=True), patch(
            "core.content_duplicates._read_pdf_text",
            side_effect=lambda path, _logger: texts[path.name],
        ), patch("pathlib.Path.unlink", autospec=True):
            removed = remove_content_duplicates([first, second], run_dir, Mock())

        self.assertEqual(removed, 1)
        self.assertEqual(second.status, "content_duplicate")
        self.assertIn("consolidated-case content match", second.reason)

    def test_distinct_orders_for_same_case_and_date_are_kept(self):
        run_dir = Path("synthetic-run")
        first = self._record("LDC_SMD_381480_08132026.pdf", "381480")
        second = self._record("LDC_SMD_381480a_08132026.pdf", "381480")
        texts = {
            first.target_filename: "ORDER Docket No. 381480 Motion to dismiss is denied.",
            second.target_filename: "ORDER Docket No. 381480 Transcript extension is granted.",
        }

        with patch("pathlib.Path.is_file", return_value=True), patch(
            "core.content_duplicates._read_pdf_text",
            side_effect=lambda path, _logger: texts[path.name],
        ), patch("pathlib.Path.unlink", autospec=True) as unlink:
            removed = remove_content_duplicates([first, second], run_dir, Mock())

        self.assertEqual(removed, 0)
        self.assertEqual([first.status, second.status], ["collected", "collected"])
        unlink.assert_not_called()

    def test_irt_parent_excludes_matching_consolidated_sibling(self):
        parent = self._record("LDC_SMD_373922_08122026.pdf", "373922")
        sibling = self._record("LDC_SMD_373857_08122026.pdf", "373857")
        parent.document_date = sibling.document_date = "08122026"
        shared_text = (
            "Court of Appeals ORDER Docket No. 373922; 373857 "
            "The motion to hold the appeal in abeyance is DENIED."
        )
        evidence = [{"File Name": parent.target_filename, "LNI": "parent-lni"}]
        paths = [
            (parent, Path("synthetic") / parent.target_filename),
            (sibling, Path("synthetic") / sibling.target_filename),
        ]

        with patch("pathlib.Path.is_file", return_value=True), patch(
            "core.content_duplicates._read_pdf_text",
            return_value=shared_text,
        ):
            matches = find_irt_backed_consolidated_duplicates(
                paths,
                {parent.target_filename: evidence, sibling.target_filename: []},
                Mock(),
            )

        match = matches[sibling.target_filename]
        self.assertEqual(match.parent_filename, parent.target_filename)
        self.assertEqual(match.shared_dockets, ("373857", "373922"))
        self.assertEqual(match.irt_evidence, evidence)

    def test_irt_parent_does_not_exclude_distinct_order(self):
        parent = self._record("LDC_SMD_380001_08132026.pdf", "380001")
        sibling = self._record("LDC_SMD_381480_08132026.pdf", "381480")
        texts = {
            parent.target_filename: (
                "ORDER Docket numbers 380001 and 381480. "
                "The motion for reconsideration is DENIED."
            ),
            sibling.target_filename: (
                "ORDER Docket numbers 380001 and 381480. "
                "The transcript deadline is extended by sixty days."
            ),
        }
        paths = [
            (parent, Path("synthetic") / parent.target_filename),
            (sibling, Path("synthetic") / sibling.target_filename),
        ]

        with patch("pathlib.Path.is_file", return_value=True), patch(
            "core.content_duplicates._read_pdf_text",
            side_effect=lambda path, _logger: texts[path.name],
        ):
            matches = find_irt_backed_consolidated_duplicates(
                paths,
                {parent.target_filename: [{"LNI": "parent"}]},
                Mock(),
            )

        self.assertNotIn(sibling.target_filename, matches)


if __name__ == "__main__":
    unittest.main()
