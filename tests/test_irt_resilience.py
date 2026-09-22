import logging
import unittest
from unittest.mock import Mock, patch
from datetime import date

from selenium.common.exceptions import StaleElementReferenceException

from browser.irt import IRTDuplicateChecker, IRTError
from config.settings import Settings


class IRTResilienceTests(unittest.TestCase):
    def setUp(self):
        self.checker = IRTDuplicateChecker(
            Settings(irt_timeout_seconds=2), logging.getLogger("test.irt")
        )
        self.checker.driver = Mock()

    def test_table_state_uses_atomic_document_lookup(self):
        expected = {
            "present": True,
            "processing": False,
            "has_records": True,
            "no_records": False,
            "signature": "row",
        }
        self.checker.driver.execute_script.return_value = expected
        self.assertEqual(self.checker._table_state(), expected)
        self.assertEqual(self.checker.driver.execute_script.call_args.args[1], "searchTable")

    def test_search_treats_stale_table_as_still_loading(self):
        stale = StaleElementReferenceException("table refreshed")
        before = {"present": True, "processing": False, "signature": "old"}
        ready = {
            "present": True,
            "processing": False,
            "has_records": True,
            "no_records": False,
            "signature": "new",
            "search_token": "search-1",
            "search_mutations": 1,
            "search_quiet_ms": 300,
            "request_started": True,
        }
        with patch.object(
            self.checker, "_table_state", side_effect=[before, stale, ready]
        ), patch.object(
            self.checker, "_arm_search_observer", return_value="search-1"
        ), patch.object(
            self.checker, "_wait_for_request_activity", return_value=True
        ), patch.object(self.checker, "_server_down", return_value=False), patch(
            "browser.irt.time.sleep"
        ):
            self.checker._search()
        self.checker.driver.find_element.return_value.click.assert_called_once()

    def test_search_accepts_confirmed_empty_result(self):
        before = {"present": True, "processing": False, "signature": "old"}
        empty = {
            "present": True,
            "processing": False,
            "has_records": False,
            "no_records": True,
            "signature": "No data found.",
            "search_token": "search-2",
            "xhr_complete": True,
            "search_quiet_ms": 300,
        }
        with patch.object(
            self.checker, "_table_state", side_effect=[before, empty]
        ), patch.object(
            self.checker, "_arm_search_observer", return_value="search-2"
        ), patch.object(
            self.checker, "_wait_for_request_activity", return_value=True
        ), patch.object(self.checker, "_server_down", return_value=False), patch(
            "browser.irt.time.sleep"
        ):
            self.checker._search()
        self.checker.driver.find_element.return_value.click.assert_called_once()

    def test_search_waits_for_ajax_completion_before_accepting_empty_result(self):
        pending = {
            "present": True,
            "processing": False,
            "request_in_flight": True,
            "has_records": False,
            "no_records": True,
            "signature": "No data found.",
            "search_token": "search-empty",
            "request_started": True,
            "search_mutations": 1,
            "search_quiet_ms": 300,
        }
        complete = {**pending, "xhr_complete": True}
        with patch.object(
            self.checker, "_table_state", side_effect=[pending, complete]
        ) as table_state, patch.object(
            self.checker, "_arm_search_observer", return_value="search-empty"
        ), patch.object(
            self.checker, "_wait_for_request_activity", return_value=True
        ), patch.object(self.checker, "_server_down", return_value=False), patch(
            "browser.irt.time.sleep"
        ):
            self.checker._search()
        self.assertEqual(table_state.call_count, 2)

    def test_search_accepts_completed_empty_result_when_jquery_counter_is_stuck(self):
        empty = {
            "present": True,
            "processing": False,
            "request_in_flight": True,
            "has_records": False,
            "no_records": True,
            "signature": "No data found.",
            "search_token": "search-empty-complete",
            "request_started": True,
            "xhr_complete": True,
            "search_mutations": 1,
            "search_quiet_ms": 300,
        }
        with patch.object(
            self.checker, "_table_state", return_value=empty
        ), patch.object(
            self.checker,
            "_arm_search_observer",
            return_value="search-empty-complete",
        ), patch.object(
            self.checker, "_wait_for_request_activity", return_value=True
        ), patch.object(self.checker, "_server_down", return_value=False), patch(
            "browser.irt.time.sleep"
        ):
            self.checker._search()

    def test_search_ignores_temporary_empty_table_until_overlay_disappears(self):
        blocked = {
            "present": True,
            "processing": False,
            "block_ui_visible": True,
            "please_wait_visible": True,
            "request_in_flight": True,
            "has_records": False,
            "no_records": True,
            "signature": "No data found.",
            "search_token": "search-overlay",
            "request_started": True,
            "search_mutations": 1,
            "search_quiet_ms": 5000,
        }
        completed = {
            **blocked,
            "block_ui_visible": False,
            "please_wait_visible": False,
            "has_records": True,
            "no_records": False,
            "signature": "LNI|LDC_SMD_381120_09222026.pdf",
            "search_mutations": 2,
            "search_quiet_ms": 300,
        }
        with patch.object(
            self.checker, "_table_state", side_effect=[blocked, completed]
        ) as table_state, patch.object(
            self.checker, "_arm_search_observer", return_value="search-overlay"
        ), patch.object(
            self.checker, "_wait_for_request_activity", return_value=True
        ), patch.object(self.checker, "_server_down", return_value=False), patch(
            "browser.irt.time.sleep"
        ):
            self.checker._search(timeout_seconds=30)

        self.assertEqual(table_state.call_count, 2)

    def test_search_uses_javascript_fallback_when_click_starts_no_request(self):
        ready = {
            "present": True,
            "processing": False,
            "request_in_flight": False,
            "has_records": True,
            "no_records": False,
            "search_token": "search-fallback",
            "request_started": True,
            "search_mutations": 1,
            "search_quiet_ms": 300,
        }
        self.checker.driver.execute_script.return_value = True
        with patch.object(
            self.checker, "_wait_for_request_activity", side_effect=[False, True]
        ) as activity, patch.object(
            self.checker, "_table_state", return_value=ready
        ), patch.object(
            self.checker, "_arm_search_observer", return_value="search-fallback"
        ), patch.object(self.checker, "_server_down", return_value=False), patch(
            "browser.irt.time.sleep"
        ):
            self.checker._search()

        self.assertEqual(activity.call_count, 2)
        fallback_script = self.checker.driver.execute_script.call_args.args[0]
        self.assertIn("getInventorySearch(1)", fallback_script)

    def test_search_rejects_unchanged_empty_table_until_current_refresh(self):
        stale_empty = {
            "present": True,
            "processing": False,
            "has_records": False,
            "no_records": True,
            "signature": "No data found.",
            "search_token": "search-3",
            "search_mutations": 0,
            "search_quiet_ms": 5000,
            "request_started": True,
        }
        refreshed_empty = {
            **stale_empty,
            "search_mutations": 2,
            "search_quiet_ms": 300,
            "xhr_complete": True,
        }
        with patch.object(
            self.checker,
            "_table_state",
            side_effect=[stale_empty, refreshed_empty],
        ) as table_state, patch.object(
            self.checker, "_arm_search_observer", return_value="search-3"
        ), patch.object(
            self.checker, "_wait_for_request_activity", return_value=True
        ), patch.object(self.checker, "_server_down", return_value=False), patch(
            "browser.irt.time.sleep"
        ):
            self.checker._search()
        self.assertEqual(table_state.call_count, 2)

    def test_records_are_extracted_without_passing_a_table_element(self):
        self.checker.driver.execute_script.return_value = [{"File Name": "sample.pdf"}]
        self.assertEqual(self.checker._records(), [{"File Name": "sample.pdf"}])
        self.assertEqual(self.checker.driver.execute_script.call_args.args[1], "searchTable")

    def test_bulk_index_uses_one_full_range_search(self):
        first = {"File Name": "LDC_SMD_381120_08122026.pdf", "LNI": "one"}
        second = {"File Name": "LDC_SMD_381269_08122026.pdf", "LNI": "two"}
        with patch.object(self.checker, "initialize"), patch.object(
            self.checker, "_set_field"
        ), patch.object(self.checker, "_set_date_field") as set_date, patch.object(
            self.checker, "_search"
        ) as search, patch.object(
            self.checker, "_result_count", return_value=2
        ), patch.object(
            self.checker, "_records", return_value=[first, second]
        ), patch.object(self.checker, "_next_page", return_value=False):
            index = self.checker.load_existing(date(2026, 8, 12), date(2026, 8, 14))

        search.assert_called_once_with()
        self.assertEqual(
            set_date.call_args_list,
            [
                unittest.mock.call(self.checker.DATE_FROM_ID, "08-12-2026"),
                unittest.mock.call(self.checker.DATE_TO_ID, "08-14-2026"),
            ],
        )
        self.assertEqual(len(index), 2)

    def test_bulk_index_restarts_browser_after_failed_snapshot(self):
        expected = {
            "381120|08122026": [
                {"File Name": "LDC_SMD_381120_08122026.pdf"}
            ]
        }
        with patch.object(
            self.checker,
            "_load_existing_once",
            side_effect=[IRTError("stalled request"), expected],
        ) as load_once, patch.object(
            self.checker, "_restart_browser"
        ) as restart, patch(
            "browser.irt.cancellable_wait"
        ):
            result = self.checker.load_existing(
                date(2026, 8, 12), date(2026, 8, 14)
            )

        self.assertEqual(result, expected)
        self.assertEqual(load_once.call_count, 2)
        restart.assert_called_once_with("retrying the complete snapshot")

    def test_preflight_restarts_browser_after_suspicious_empty_result(self):
        with patch.object(
            self.checker,
            "_preflight_once",
            side_effect=[IRTError("zero inventory records"), None],
        ) as preflight_once, patch.object(
            self.checker, "_restart_browser"
        ) as restart, patch(
            "browser.irt.cancellable_wait"
        ):
            self.checker.preflight(date(2026, 8, 1), date(2026, 8, 31))

        self.assertEqual(preflight_once.call_count, 2)
        restart.assert_called_once_with("retrying the preflight")

    def test_preflight_uses_short_timeout_and_requires_visible_inventory(self):
        self.checker.settings.irt_timeout_seconds = 120
        self.checker.settings.irt_preflight_timeout_seconds = 30
        row = {"File Name": "LDC_SMD_381120_08122026.pdf"}
        with patch.object(self.checker, "initialize"), patch.object(
            self.checker, "_set_field"
        ), patch.object(self.checker, "_set_date_field"), patch.object(
            self.checker, "_search"
        ) as search, patch.object(
            self.checker, "_records", return_value=[row]
        ):
            self.checker._preflight_once(
                date(2026, 8, 1), date(2026, 8, 31)
            )

        search.assert_called_once_with(timeout_seconds=30)

    def test_bulk_index_fails_closed_when_capture_is_incomplete(self):
        row = {"File Name": "LDC_SMD_381120_08122026.pdf", "LNI": "one"}
        with patch.object(self.checker, "initialize"), patch.object(
            self.checker, "_set_field"
        ), patch.object(self.checker, "_set_date_field"), patch.object(
            self.checker, "_search"
        ), patch.object(
            self.checker, "_result_count", return_value=2
        ), patch.object(
            self.checker, "_records", return_value=[row]
        ), patch.object(self.checker, "_next_page", return_value=False):
            with self.assertRaisesRegex(Exception, "captured 1 of 2"):
                self.checker.load_existing(date(2026, 8, 12), date(2026, 8, 14))

    def test_counsel_lookup_uses_court_docket_pattern_and_all_result_pages(self):
        first = {"File Name": "LDC_SMD_380275counsel.html", "LNI": "111"}
        unrelated = {"File Name": "LDC_SMD_999999counsel.html", "LNI": "222"}
        with patch.object(self.checker, "initialize"), patch.object(
            self.checker, "_set_field"
        ) as set_field, patch.object(self.checker, "_set_date_field"), patch.object(
            self.checker, "_search"
        ) as search, patch.object(
            self.checker, "_capture_current_results", return_value=[first, unrelated]
        ):
            matches = self.checker.find_existing_counsel(
                "380275", date(2024, 8, 17), date(2026, 8, 17)
            )

        search.assert_called_once_with()
        self.assertEqual(
            set_field.call_args_list,
            [
                unittest.mock.call(self.checker.COURT_ID, "STMIAP00"),
                unittest.mock.call(self.checker.DOCKET_ID, "380275"),
                unittest.mock.call(
                    self.checker.FILE_NAME_ID, "*380275*counsel*"
                ),
            ],
        )
        self.assertEqual(matches, [first])

if __name__ == "__main__":
    unittest.main()
