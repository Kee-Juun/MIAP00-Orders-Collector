"""Michigan Court of Appeals counsel-page lookup and compact HTML export."""

from __future__ import annotations

from html import escape
import logging
from pathlib import Path
import re
from urllib.parse import quote, urljoin

import requests

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from config.settings import Settings
from core.cancellation import (
    CollectionCancelled,
    cancellable_wait,
    raise_if_cancelled,
)
from .michigan_courts import MichiganOrdersSite
from .webdriver_factory import cancellable_navigate


class CounselCollectionError(RuntimeError):
    pass


class MichiganCounselSite:
    """Reuse the Michigan Orders browser to collect one counsel page per docket."""

    CASE_DETAIL_ATTEMPTS = 3
    CASE_DETAIL_RETRY_DELAY_SECONDS = 1.0

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        orders_site: MichiganOrdersSite,
    ):
        self.settings = settings
        self.logger = logger
        self.orders_site = orders_site
        self.cancel_event = None

    @property
    def driver(self):
        return self.orders_site.driver

    def collect(self, docket: str, destination: Path, cancel_event=None) -> str:
        self.cancel_event = cancel_event
        self.orders_site.cancel_event = cancel_event
        self.orders_site.start()
        raise_if_cancelled(cancel_event, "Collection stopped before counsel lookup")
        self.logger.info("Opening Michigan case search for counsel docket %s", docket)
        cancellable_navigate(
            self.driver,
            self.settings.source_url,
            cancel_event,
            context=f"Collection stopped while opening counsel search for {docket}",
        )
        self._open_advanced_search()
        field = self._wait_for_case_number_field()
        field.clear()
        field.send_keys(docket)
        search_button = self._search_button_for(field)
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", search_button
        )
        search_button.click()

        link = self._wait(
            lambda _driver: self._exact_case_link(docket),
            f"No exact Court of Appeals case result appeared for docket {docket}",
        )
        href = (link.get_attribute("href") or "").strip()
        if not href:
            raise CounselCollectionError(
                f"The exact case result for docket {docket} had no usable link"
            )
        case_url = urljoin(self.settings.source_url, href)

        # The case-detail view now waits for an invisible hCaptcha callback before
        # Vue requests its data.  In headless Chrome that callback can leave the
        # page on its skeleton loader indefinitely.  Use the same official JSON
        # endpoint as the Vue page after the public search has identified the
        # exact case, and retain rendered-page extraction as a compatibility
        # fallback if the endpoint changes.
        try:
            case_data = self._load_case_detail_data(docket, case_url)
            payload = self._payload_from_case_data(docket, case_data)
            self.logger.info(
                "Loaded counsel data from Michigan case-detail service: %s", docket
            )
        except CollectionCancelled:
            raise
        except Exception as exc:
            self.logger.warning(
                "Direct counsel data lookup failed for %s (%s); "
                "trying the rendered case page",
                docket,
                exc,
            )
            cancellable_navigate(
                self.driver,
                case_url,
                cancel_event,
                context=f"Collection stopped while opening counsel case {docket}",
            )
            try:
                self._wait_for_case_content(docket)
                self._expand_all_parties()
                payload = self._extract_case_content(docket)
            except CollectionCancelled:
                raise
            except Exception as rendered_exc:
                self.logger.warning(
                    "Rendered counsel page failed for %s (%s); making one final "
                    "direct case-detail request",
                    docket,
                    rendered_exc,
                )
                try:
                    case_data = self._load_case_detail_data(
                        docket,
                        case_url,
                        attempts=1,
                    )
                    payload = self._payload_from_case_data(docket, case_data)
                    self.logger.info(
                        "Recovered counsel data from Michigan case-detail service: %s",
                        docket,
                    )
                except CollectionCancelled:
                    raise
                except Exception as final_exc:
                    raise CounselCollectionError(
                        f"Counsel data could not be loaded for docket {docket} "
                        "after direct and rendered-page recovery attempts"
                    ) from final_exc
        html = self._standalone_html(docket, payload)
        destination.write_text(html, encoding="utf-8", newline="\n")
        self.logger.info("Counsel collected: %s", destination.name)
        return case_url

    def _load_case_detail_data(
        self,
        docket: str,
        case_url: str,
        *,
        attempts: int | None = None,
    ) -> dict:
        """Load the structured record used by Michigan's case-detail Vue page."""

        raise_if_cancelled(self.cancel_event, "Collection stopped before counsel download")
        endpoint = urljoin(
            self.settings.source_url,
            f"/c/courts/getcourtofappealscasedetaildata/{quote(docket)}",
        )
        session = requests.Session()
        attempt_limit = max(1, attempts or self.CASE_DETAIL_ATTEMPTS)
        try:
            for cookie in self.driver.get_cookies():
                session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                )
            for attempt in range(1, attempt_limit + 1):
                raise_if_cancelled(
                    self.cancel_event,
                    "Collection stopped before counsel download",
                )
                try:
                    response = session.get(
                        endpoint,
                        headers={
                            "Accept": "application/json",
                            "User-Agent": self.settings.user_agent,
                            "Referer": case_url,
                        },
                        timeout=(5, min(self.settings.browser_timeout_seconds, 20)),
                    )
                    response.raise_for_status()
                    data = response.json()
                    break
                except (requests.RequestException, ValueError) as exc:
                    if attempt >= attempt_limit or not self._retryable_detail_error(exc):
                        detail = (
                            "returned invalid data"
                            if isinstance(exc, ValueError)
                            else "failed"
                        )
                        raise CounselCollectionError(
                            f"Michigan case-detail service {detail} for docket {docket}"
                        ) from exc
                    self.logger.warning(
                        "Michigan case-detail request %d/%d failed for %s (%s); "
                        "retrying",
                        attempt,
                        attempt_limit,
                        docket,
                        exc,
                    )
                    cancellable_wait(
                        self.cancel_event,
                        self.CASE_DETAIL_RETRY_DELAY_SECONDS * attempt,
                        f"Collection stopped while retrying counsel docket {docket}",
                    )
        finally:
            session.close()

        raise_if_cancelled(self.cancel_event, "Collection stopped during counsel download")
        if not isinstance(data, dict) or data.get("error") or not data.get("id"):
            detail = data.get("error") if isinstance(data, dict) else None
            raise CounselCollectionError(
                detail or f"No public case detail was returned for docket {docket}"
            )
        returned_dockets = {
            str(value)
            for value in (
                data.get("courtOfAppealsCaseNumber"),
                data.get("courtOfAppealsCaseId"),
                *(data.get("uniqueCourtOfAppealsCaseNumbers") or []),
            )
            if value not in (None, "")
        }
        if docket not in returned_dockets:
            raise CounselCollectionError(
                f"Michigan case-detail service returned a different case for docket {docket}"
            )
        return data

    @staticmethod
    def _retryable_detail_error(exc: Exception) -> bool:
        """Return whether another official endpoint request may recover safely."""

        if isinstance(exc, ValueError):
            return True
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return True
        if isinstance(exc, requests.HTTPError):
            status = exc.response.status_code if exc.response is not None else 0
            return status == 429 or status >= 500
        return False

    @staticmethod
    def _payload_from_case_data(docket: str, data: dict) -> dict[str, str]:
        """Render the counsel-only sections from Michigan's structured response."""

        title = str(data.get("title") or f"Court of Appeals docket {docket}").strip()
        status = str(data.get("courtOfAppealsStatus") or "").strip()
        header = f"""<section class="case-information-header">
  <h2>Case Information</h2>
  <div class="case-header-label">Case Header</div>
  <p><span class="court-code">COA</span> Court Of Appeals</p>
  <div class="label">Case Number</div>
  <p>COA #{escape(docket)}</p>
  <div class="label">Case Status</div>
  <p>{escape(status)}</p>
</section>"""

        party_blocks: list[str] = []
        for index, party in enumerate(data.get("courtOfAppealsParties") or [], start=1):
            if not isinstance(party, dict):
                continue
            number = party.get("number") or index
            name = escape(str(party.get("name") or "").strip())
            connection = escape(str(party.get("connectionsValue") or "").strip())
            prisoner_id = str(party.get("prisonerID") or "").strip()
            attorney_lines: list[str] = []
            for attorney in party.get("attorneys") or []:
                if not isinstance(attorney, dict):
                    continue
                attorney_name = escape(str(attorney.get("name") or "").strip())
                bar_number = str(attorney.get("pNumber") or "").strip()
                appoint = attorney.get("appointType") or {}
                role = escape(
                    str(
                        appoint.get("description")
                        if isinstance(appoint, dict)
                        else appoint
                    ).strip()
                )
                details = " ".join(
                    item for item in (f"#{escape(bar_number)}" if bar_number else "", role) if item
                )
                if attorney_name:
                    attorney_lines.append(
                        f'<div class="attorney"><div>{attorney_name}</div>'
                        f'<div class="attorney-detail">{details}</div></div>'
                    )
                elif role:
                    attorney_lines.append(
                        f'<div class="attorney"><div class="attorney-detail">{role}</div></div>'
                    )
            prisoner = (
                f'<div class="prisoner-id">#{escape(prisoner_id)}, Prisoner</div>'
                if prisoner_id
                else ""
            )
            attorneys_html = "\n".join(attorney_lines)
            party_blocks.append(
                f"""<article class="party-item">
  <div class="party-number">{escape(str(number))}</div>
  <div class="party-name">{name}</div>
  <div class="party-connection">{connection}</div>
  {prisoner}
  <div class="label">Attorney(s)</div>
  {attorneys_html}
</article>"""
            )

        if not party_blocks:
            raise CounselCollectionError(
                f"No Court of Appeals parties were returned for docket {docket}"
            )
        parties = """<section class="case-parties">
  <h2 class="parties-title">Parties &amp; Attorneys to the Case - Court of Appeals</h2>
  <div class="party-items-container">
{blocks}
  </div>
</section>""".format(blocks="\n".join(party_blocks))
        return {"title": title, "header": header, "parties": parties}

    def _wait(self, condition, error_message: str):
        def cancellation_aware(driver):
            raise_if_cancelled(self.cancel_event)
            try:
                return condition(driver)
            except StaleElementReferenceException:
                return False

        try:
            return WebDriverWait(
                self.driver,
                self.settings.browser_timeout_seconds,
                poll_frequency=0.2,
            ).until(cancellation_aware)
        except CollectionCancelled:
            raise
        except Exception as exc:
            raise CounselCollectionError(error_message) from exc

    def _open_advanced_search(self) -> None:
        """Reveal the case-search form before locating its generated inputs."""

        def locate(driver):
            buttons = driver.find_elements(
                By.XPATH,
                "//button[normalize-space(.)='Advanced Search' or "
                ".//*[normalize-space(.)='Advanced Search']]",
            )
            for button in buttons:
                if button.is_displayed() and button.is_enabled():
                    return button
            return False

        advanced = self._wait(
            locate,
            "Michigan Advanced Search button did not become available",
        )
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", advanced
        )
        try:
            advanced.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", advanced)
        self.logger.info("Opened Advanced Search for counsel lookup")

    def _wait_for_case_number_field(self):
        def locate(driver):
            # The visible "Case Number" text belongs to the fieldset legend.
            # Its generated label is intentionally empty and the input has no
            # stable name, placeholder, or aria-label, so anchor to the legend.
            fieldset_inputs = driver.find_elements(
                By.XPATH,
                "//fieldset[.//legend//*[translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='case number']]"
                "//input[@type='text']",
            )
            for field in fieldset_inputs:
                if field.is_displayed() and field.is_enabled():
                    return field

            labels = driver.find_elements(
                By.XPATH,
                "//label[contains(translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'case number')]",
            )
            for label in labels:
                field_id = (label.get_attribute("for") or "").strip()
                if field_id:
                    matches = driver.find_elements(By.ID, field_id)
                    if matches and matches[0].is_displayed() and matches[0].is_enabled():
                        return matches[0]
                matches = label.find_elements(By.XPATH, "following::input[1]")
                if matches and matches[0].is_displayed() and matches[0].is_enabled():
                    return matches[0]
            for selector in (
                "input[name*='case'][type='text']",
                "input[aria-label*='Case Number']",
                "input[placeholder*='Case Number']",
            ):
                for field in driver.find_elements(By.CSS_SELECTOR, selector):
                    if field.is_displayed() and field.is_enabled():
                        return field
            return False

        return self._wait(locate, "Michigan Case Number field did not become available")

    def _search_button_for(self, field):
        def locate(_driver):
            forms = field.find_elements(By.XPATH, "ancestor::form[1]")
            if not forms:
                return False
            buttons = forms[0].find_elements(
                By.XPATH,
                ".//button[@type='submit' or contains(translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'search')]",
            )
            for button in buttons:
                if button.is_displayed() and button.is_enabled():
                    return button
            return False

        return self._wait(
            locate,
            "Michigan case search button did not become enabled after entering the docket",
        )

    def _exact_case_link(self, docket: str):
        for link in self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/case/']"):
            if not link.is_displayed():
                continue
            context = self.driver.execute_script(
                r"""
                const link = arguments[0];
                let node = link;
                let text = (link.innerText || '').trim();
                for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                  const candidate = (node.innerText || '').trim();
                  if (candidate.length > text.length) text = candidate;
                  if (/\b\d{5,7}\b/.test(candidate)) return candidate;
                }
                return text;
                """,
                link,
            )
            if re.search(rf"\b{re.escape(docket)}\b", str(context or "")):
                return link
        return False

    def _wait_for_case_content(self, docket: str) -> None:
        def ready(driver):
            headers = driver.find_elements(By.CSS_SELECTOR, ".case-information-header")
            parties = driver.find_elements(By.CSS_SELECTOR, ".case-parties")
            if not headers or not parties:
                return False
            header_text = headers[0].text
            has_docket = re.search(rf"\b{re.escape(docket)}\b", header_text)
            has_coa_parties = any(
                "parties & attorneys to the case - court of appeals" in item.text.casefold()
                for item in parties
            )
            return bool(has_docket and has_coa_parties)

        self._wait(ready, f"Counsel content did not finish loading for docket {docket}")

    def _expand_all_parties(self) -> None:
        for _attempt in range(5):
            buttons = self.driver.find_elements(
                By.XPATH,
                "//button[contains(translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'expand all') "
                "or contains(translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'show ') "
                "and contains(translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'more')]",
            )
            clicked = False
            for button in buttons:
                if button.is_displayed() and button.is_enabled():
                    self.driver.execute_script("arguments[0].click();", button)
                    clicked = True
            if not clicked:
                return

    def _extract_case_content(self, docket: str) -> dict[str, str]:
        payload = self.driver.execute_script(
            """
            const docket = arguments[0];
            const header = document.querySelector('.case-information-header');
            const parties = Array.from(document.querySelectorAll('.case-parties')).find(node =>
              (node.innerText || '').toLowerCase().includes(
                'parties & attorneys to the case - court of appeals'
              )
            );
            if (!header || !parties) return null;
            const clean = (source) => {
              const node = source.cloneNode(true);
              node.querySelectorAll('script, style, iframe, form, button, input').forEach(x => x.remove());
              node.querySelectorAll('*').forEach(element => {
                for (const attribute of Array.from(element.attributes)) {
                  if (attribute.name.startsWith('on') || attribute.name.startsWith('data-v-')) {
                    element.removeAttribute(attribute.name);
                  }
                }
              });
              return node.outerHTML;
            };
            const title = document.querySelector('h1, .case-title');
            return {
              title: title ? (title.innerText || '').trim() : `Court of Appeals docket ${docket}`,
              header: clean(header),
              parties: clean(parties)
            };
            """,
            docket,
        )
        if not payload or not payload.get("header") or not payload.get("parties"):
            raise CounselCollectionError(
                f"Required Court of Appeals counsel sections were missing for docket {docket}"
            )
        return dict(payload)

    @staticmethod
    def _standalone_html(docket: str, payload: dict[str, str]) -> str:
        title = escape(payload.get("title") or f"Court of Appeals docket {docket}")
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="generator" content="MIAP00 Orders Collector">
  <title>{title} - Counsel</title>
  <style>
    body {{ margin: 32px; color: #1a202c; background: #fff; font: 15px/1.5 Arial, sans-serif; }}
    main {{ max-width: 980px; margin: 0 auto; }}
    .case-information-header, .case-parties {{ margin-bottom: 28px; }}
    .msc-row, .party-item, .party-items-container > div {{ margin-bottom: 14px; }}
    .parties-title {{ margin: 22px 0; font-size: 20px; font-weight: 700; }}
    .label, [class*="label"] {{ font-weight: 700; }}
  </style>
</head>
<body>
<main data-court="STMIAP00" data-docket="{escape(docket)}">
{payload['header']}
{payload['parties']}
</main>
</body>
</html>
"""
