from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from browser.michigan_counsel import MichiganCounselSite
from config.settings import Settings
from core.collector import MIAP00Collector
from core.models import ProcessingRecord
from core.models import CounselRecord


class CounselFlowTests(unittest.TestCase):
    @staticmethod
    def _main(filename: str, docket: str, related: list[str]) -> ProcessingRecord:
        return ProcessingRecord(
            status="collected",
            docket=docket,
            title="",
            release_date="08/17/2026",
            source_filename=f"{docket}_source.pdf",
            source_url="",
            target_filename=filename,
            related_dockets=related,
        )

    def test_consolidated_parent_and_retained_sibling_share_all_counsel_results(self):
        collector = MIAP00Collector(Settings())
        collector.logger = Mock()
        collector.collected_dir = Path("synthetic-orders")
        collector.counsel_dir = Path("synthetic-counsels")
        parent = self._main(
            "LDC_SMD_380275_08172026.pdf",
            "380275",
            ["380275", "380358"],
        )
        retained_sibling = self._main(
            "LDC_SMD_380275a_08172026.pdf",
            "380275",
            ["380275", "380358"],
        )
        irt = Mock()
        irt.find_existing_counsel.side_effect = [
            [
                {"File Name": "LDC_SMD_380275counsel.html", "LNI": "LNI-A"},
                {"File Name": "LDC_SMD_380275_counsel.htm", "LNI": "LNI-B"},
            ],
            [],
        ]
        counsel_site = Mock()
        counsel_site.collect.return_value = "https://courts.example/case/380358"

        with patch.object(
            collector,
            "_counsel_irt_date_range",
            return_value=(date(2024, 8, 17), date(2026, 8, 17)),
        ), patch.object(
            collector,
            "_ensure_counsel_dir",
        ), patch(
            "core.collector.MichiganCounselSite", return_value=counsel_site
        ), patch("pathlib.Path.exists", return_value=False):
            collector._collect_counsel([parent, retained_sibling], Mock(), irt)

        self.assertEqual(irt.find_existing_counsel.call_count, 2)
        expected = [
            "380275: LNI-A; LNI-B",
            "380358: LDC_SMD_380358counsel.html",
        ]
        self.assertEqual(parent.counsel_references, expected)
        self.assertEqual(retained_sibling.counsel_references, expected)
        counsel_site.collect.assert_called_once_with(
            "380358",
            Path("synthetic-counsels/LDC_SMD_380358counsel.html"),
            cancel_event=collector.cancel_event,
        )

    def test_nonconsolidated_counsel_references_omit_redundant_docket(self):
        collected = CounselRecord(
            docket="379218",
            status="collected",
            target_filename="LDC_SMD_379218counsel.html",
        )
        existing = CounselRecord(
            docket="379272",
            status="irt_existing",
            lnis=["LNI-A", "LNI-B"],
        )

        self.assertEqual(
            collected.reference(include_docket=False),
            "LDC_SMD_379218counsel.html",
        )
        self.assertEqual(
            existing.reference(include_docket=False),
            "LNI-A; LNI-B",
        )
        self.assertEqual(
            collected.reference(include_docket=True),
            "379218: LDC_SMD_379218counsel.html",
        )

    def test_compact_counsel_html_contains_only_selected_sections(self):
        html = MichiganCounselSite._standalone_html(
            "374362",
            {
                "title": "PEOPLE OF MI V TEST",
                "header": '<section class="case-information-header">Header</section>',
                "parties": '<section class="case-parties">Counsel</section>',
            },
        )

        self.assertIn('data-court="STMIAP00"', html)
        self.assertIn('data-docket="374362"', html)
        self.assertIn("case-information-header", html)
        self.assertIn("case-parties", html)
        self.assertNotIn("<script", html)

    def test_counsel_lookup_opens_advanced_search_before_case_number_field(self):
        settings = Settings()
        logger = Mock()
        orders_site = Mock()
        orders_site.driver.current_url = "https://courts.example/case/379218"
        counsel_site = MichiganCounselSite(settings, logger, orders_site)
        events: list[str] = []
        field = Mock()
        search_button = Mock()
        link = Mock()
        link.get_attribute.return_value = "https://courts.example/case/379218"

        with TemporaryDirectory() as directory, patch(
            "browser.michigan_counsel.cancellable_navigate"
        ), patch.object(
            counsel_site,
            "_open_advanced_search",
            side_effect=lambda: events.append("advanced"),
        ), patch.object(
            counsel_site,
            "_wait_for_case_number_field",
            side_effect=lambda: events.append("field") or field,
        ), patch.object(
            counsel_site, "_search_button_for", return_value=search_button
        ), patch.object(
            counsel_site, "_wait", return_value=link
        ), patch.object(
            counsel_site,
            "_load_case_detail_data",
            return_value={"id": 1},
        ), patch.object(
            counsel_site,
            "_payload_from_case_data",
            return_value={"header": "<section></section>", "parties": "<section></section>"},
        ):
            destination = Path(directory) / "LDC_SMD_379218counsel.html"
            counsel_site.collect("379218", destination)

        self.assertEqual(events, ["advanced", "field"])
        field.send_keys.assert_called_once_with("379218")
        search_button.click.assert_called_once_with()

    def test_advanced_search_uses_visible_button_without_generated_uid(self):
        orders_site = Mock()
        advanced = Mock()
        counsel_site = MichiganCounselSite(Settings(), Mock(), orders_site)
        with patch.object(counsel_site, "_wait", return_value=advanced) as wait:
            counsel_site._open_advanced_search()

        condition = wait.call_args.args[0]
        driver = Mock()
        driver.find_elements.return_value = [advanced]
        advanced.is_displayed.return_value = True
        advanced.is_enabled.return_value = True
        self.assertIs(condition(driver), advanced)
        selector = driver.find_elements.call_args.args[1]
        self.assertIn("Advanced Search", selector)
        self.assertNotIn("uid-", selector)
        advanced.click.assert_called_once_with()

    def test_case_number_field_is_anchored_to_fieldset_legend(self):
        counsel_site = MichiganCounselSite(Settings(), Mock(), Mock())
        field = Mock()
        field.is_displayed.return_value = True
        field.is_enabled.return_value = True
        driver = Mock()

        def find_elements(_by, selector):
            return [field] if "//fieldset" in selector else []

        driver.find_elements.side_effect = find_elements
        with patch.object(counsel_site, "_wait") as wait:
            wait.side_effect = lambda condition, _message: condition(driver)
            result = counsel_site._wait_for_case_number_field()

        self.assertIs(result, field)
        selector = driver.find_elements.call_args_list[0].args[1]
        self.assertIn("fieldset", selector)
        self.assertIn("legend", selector)
        self.assertIn("case number", selector)
        self.assertNotIn("uid-", selector)

    def test_search_button_waits_for_vue_to_enable_it(self):
        counsel_site = MichiganCounselSite(Settings(), Mock(), Mock())
        field = Mock()
        form = Mock()
        button = Mock()
        field.find_elements.return_value = [form]
        form.find_elements.return_value = [button]
        button.is_displayed.return_value = True
        button.is_enabled.return_value = True
        with patch.object(counsel_site, "_wait") as wait:
            wait.side_effect = lambda condition, _message: condition(Mock())
            result = counsel_site._search_button_for(field)

        self.assertIs(result, button)
        wait.assert_called_once()

    def test_structured_case_data_builds_complete_counsel_sections(self):
        payload = MichiganCounselSite._payload_from_case_data(
            "379218",
            {
                "title": "DEONTAE JAREE GORDON V DEPARTMENT OF CORRECTIONS",
                "courtOfAppealsStatus": "Case Concluded; File Open",
                "courtOfAppealsParties": [
                    {
                        "number": 1,
                        "name": "GORDON DEONTAE J",
                        "connectionsValue": "Plaintiff - Appellant",
                        "prisonerID": "308075",
                        "attorneys": [
                            {
                                "appointType": {
                                    "abbreviation": "PRO",
                                    "description": "Self-Represented Party",
                                }
                            }
                        ],
                    },
                    {
                        "number": 2,
                        "name": "CORRECTIONS DEPARTMENT OF",
                        "connectionsValue": "Defendant - Appellee",
                        "attorneys": [
                            {
                                "name": "SOROS ALLAN J",
                                "pNumber": 43702,
                                "appointType": {
                                    "abbreviation": "AG",
                                    "description": "Attorney General",
                                },
                            }
                        ],
                    },
                ],
            },
        )

        combined = payload["header"] + payload["parties"]
        self.assertEqual(
            payload["title"],
            "DEONTAE JAREE GORDON V DEPARTMENT OF CORRECTIONS",
        )
        self.assertIn("COA #379218", combined)
        self.assertIn("Case Concluded; File Open", combined)
        self.assertIn("Parties &amp; Attorneys to the Case - Court of Appeals", combined)
        self.assertIn("GORDON DEONTAE J", combined)
        self.assertIn("#308075, Prisoner", combined)
        self.assertIn("Self-Represented Party", combined)
        self.assertIn("SOROS ALLAN J", combined)
        self.assertIn("#43702 Attorney General", combined)

    def test_structured_case_data_rejects_missing_parties(self):
        with self.assertRaisesRegex(Exception, "No Court of Appeals parties"):
            MichiganCounselSite._payload_from_case_data(
                "379218",
                {"title": "TEST", "courtOfAppealsParties": []},
            )


if __name__ == "__main__":
    unittest.main()
