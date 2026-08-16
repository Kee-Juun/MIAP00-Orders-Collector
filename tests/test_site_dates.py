from datetime import date
import unittest

from browser.michigan_courts import MichiganOrdersSite
from core.models import OrderResult


class SiteDateTests(unittest.TestCase):
    def test_release_date_formats(self):
        self.assertEqual(MichiganOrdersSite._parse_release_date("08/14/2026"), date(2026, 8, 14))
        self.assertEqual(MichiganOrdersSite._parse_release_date("August 14, 2026"), date(2026, 8, 14))

    def test_unreadable_release_date(self):
        self.assertIsNone(MichiganOrdersSite._parse_release_date("Pending"))

    def test_page_filter_is_inclusive_and_stops_after_crossing_start(self):
        def row(release_date: str) -> OrderResult:
            return OrderResult(1, 1, "1", "Order", "", release_date, "", "x.pdf", release_date)

        newest = row("08/15/2026")
        inside = row("08/10/2026")
        boundary = row("08/07/2026")
        older = row("08/06/2026")
        dated = [
            (item, MichiganOrdersSite._parse_release_date(item.release_date))
            for item in (newest, inside, boundary, older)
        ]
        matching, should_stop = MichiganOrdersSite._filter_page_to_date_range(
            dated, date(2026, 8, 7), date(2026, 8, 14)
        )
        self.assertEqual(matching, [inside, boundary])
        self.assertTrue(should_stop)


if __name__ == "__main__":
    unittest.main()
