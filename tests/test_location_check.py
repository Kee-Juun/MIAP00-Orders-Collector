import unittest
from unittest.mock import Mock, patch

import requests

from config.settings import Settings
from core.collector import MIAP00Collector
from core.location_check import (
    LocationVerificationError,
    NonUSLocationError,
    verify_us_location,
)


class LocationCheckTests(unittest.TestCase):
    @patch("core.location_check.requests.get")
    def test_us_public_ip_passes(self, get):
        response = Mock()
        response.json.return_value = {
            "country_code": "US",
            "country_name": "United States",
            "region": "Michigan",
            "city": "Detroit",
        }
        get.return_value = response

        location = verify_us_location(
            timeout_seconds=4, endpoint="https://ipapi.co/json/"
        )

        self.assertEqual(location.country_code, "US")
        self.assertEqual(location.display_name, "Detroit, Michigan, United States")
        get.assert_called_once_with(
            "https://ipapi.co/json/",
            headers={"Accept": "application/json"},
            timeout=4.0,
        )
        response.raise_for_status.assert_called_once_with()

    @patch("core.location_check.requests.get")
    def test_non_us_public_ip_halts_with_vpn_guidance(self, get):
        response = Mock()
        response.json.return_value = {
            "country_code": "SG",
            "country_name": "Singapore",
            "city": "Singapore",
        }
        get.return_value = response

        with self.assertRaisesRegex(NonUSLocationError, "Singapore") as raised:
            verify_us_location()

        self.assertIn("any state or city in the U.S.A.", str(raised.exception))

    @patch("core.location_check.requests.get")
    def test_lookup_failure_halts_instead_of_guessing(self, get):
        get.side_effect = requests.ConnectionError("offline")

        with self.assertRaisesRegex(LocationVerificationError, "Unable to verify"):
            verify_us_location()

        self.assertEqual(get.call_count, 3)

    @patch("core.location_check.requests.get")
    def test_rate_limited_provider_falls_back_to_another_lookup(self, get):
        limited = Mock()
        limited.raise_for_status.side_effect = requests.HTTPError("429")
        fallback = Mock()
        fallback.json.return_value = {
            "country_code": "US",
            "country_name": "United States",
            "region": "New York",
            "city": "New York",
        }
        get.side_effect = [limited, fallback]

        location = verify_us_location(timeout_seconds=6)

        self.assertEqual(location.country_code, "US")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["timeout"], 2.0)

    @patch(
        "core.collector.verify_us_location",
        side_effect=NonUSLocationError("not in the U.S.A."),
    )
    @patch("core.collector.MichiganOrdersSite")
    def test_collector_stops_before_creating_site_or_run_folder(self, site, _check):
        collector = MIAP00Collector(Settings(output_root="synthetic-output"))

        with patch("pathlib.Path.mkdir") as mkdir, self.assertRaises(
            NonUSLocationError
        ):
            collector.run()

        site.assert_not_called()
        mkdir.assert_not_called()
        self.assertIsNone(collector.run_dir)


if __name__ == "__main__":
    unittest.main()
