from pathlib import Path
from datetime import date
import unittest
from unittest.mock import patch

from config.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_are_fail_closed(self):
        settings = Settings()
        self.assertTrue(settings.fail_closed_on_irt_error)
        self.assertTrue(settings.require_nonempty_irt_index)
        self.assertTrue(settings.headless)
        self.assertEqual(settings.irt_court_code, "STMIAP00")
        self.assertEqual(settings.page_size, 100)
        self.assertEqual(settings.sort_order, "Newest")

    def test_default_date_range_uses_configured_lookback(self):
        settings = Settings(default_days_back=7)
        self.assertEqual(
            settings.resolved_date_range(date(2026, 8, 14)),
            (date(2026, 8, 7), date(2026, 8, 14)),
        )

    def test_explicit_date_range_is_inclusive(self):
        settings = Settings(start_date="2026-08-01", end_date="2026-08-14")
        self.assertEqual(
            settings.resolved_date_range(),
            (date(2026, 8, 1), date(2026, 8, 14)),
        )

    def test_reversed_date_range_is_rejected(self):
        with self.assertRaises(ValueError):
            Settings(start_date="2026-08-15", end_date="2026-08-14").resolved_date_range()

    def test_unknown_config_keys_are_ignored(self):
        path = Path("synthetic-config.json")
        with patch.object(Path, "exists", return_value=True), patch.object(
            Path,
            "read_text",
            return_value='{"max_pages": 2, "unknown": true}',
        ):
            self.assertEqual(Settings.load(path).max_pages, 2)


if __name__ == "__main__":
    unittest.main()
