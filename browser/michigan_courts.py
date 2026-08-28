from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
import re
import threading
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from urllib3.util.retry import Retry

from config.settings import Settings
from core.cancellation import (
    CollectionCancelled,
    cancellable_wait,
    raise_if_cancelled,
)
from core.models import OrderResult
from core.file_ops import replace_file_with_retry
from .webdriver_factory import (
    cancellable_navigate,
    close_chrome_driver,
    create_chrome_driver,
)


class SiteAutomationError(RuntimeError):
    pass


class MichiganOrdersSite:
    RESULT_CARD = "div.order-item"
    PDF_LINK = "a.document-url"
    NEXT_BUTTON = "button.next-button"

    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self.driver = None
        self.cancel_event = None

    def start(self) -> None:
        if self.driver:
            return
        raise_if_cancelled(self.cancel_event)
        self.driver = create_chrome_driver(
            self.logger,
            headless=self.settings.headless,
            chromedriver_path=self.settings.chromedriver_path,
            timeout=self.settings.browser_timeout_seconds,
        )
        raise_if_cancelled(self.cancel_event)

    def collect_result_metadata(self, cancel_event=None) -> list[OrderResult]:
        self.cancel_event = cancel_event
        self.start()
        self._open_filtered_results()
        start_date, end_date = self.settings.resolved_date_range()
        self.logger.info(
            "Collecting orders released from %s through %s (inclusive)",
            start_date.isoformat(),
            end_date.isoformat(),
        )
        results: list[OrderResult] = []
        seen_urls: set[str] = set()
        page = 1
        while True:
            raise_if_cancelled(
                self.cancel_event,
                "Collection stopped while reading court result pages",
            )
            self._wait(
                self.settings.browser_timeout_seconds,
                EC.presence_of_element_located((By.CSS_SELECTOR, self.RESULT_CARD))
            )
            page_rows = self._read_current_page(page)
            if not page_rows:
                self.logger.warning("Result cards were temporarily empty; waiting for stabilization")
                for _attempt in range(3):
                    cancellable_wait(
                        self.cancel_event,
                        1,
                        "Collection stopped while waiting for court results",
                    )
                    page_rows = self._read_current_page(page)
                    if page_rows:
                        break
            dated_rows = [(row, self._parse_release_date(row.release_date)) for row in page_rows]
            invalid_dates = sum(release_date is None for _, release_date in dated_rows)
            if invalid_dates:
                self.logger.warning(
                    "Results page %d: skipped %d order(s) with an unreadable release date",
                    page,
                    invalid_dates,
                )
            matching_rows, crossed_start = self._filter_page_to_date_range(
                dated_rows, start_date, end_date
            )
            new_rows = [row for row in matching_rows if row.pdf_url not in seen_urls]
            for row in new_rows:
                seen_urls.add(row.pdf_url)
                results.append(row)
            self.logger.info(
                "Results page %d: %d PDF order(s), %d in date range, %d new (total %d)",
                page,
                len(page_rows),
                len(matching_rows),
                len(new_rows),
                len(results),
            )
            parsed_dates = [release_date for _, release_date in dated_rows if release_date is not None]
            if crossed_start:
                self.logger.info(
                    "Date-range boundary reached on page %d (oldest result %s); pagination stopped",
                    page,
                    min(parsed_dates).isoformat(),
                )
                break
            if self.settings.max_pages and page >= self.settings.max_pages:
                self.logger.info("Reached configured max_pages=%d", self.settings.max_pages)
                break
            if not self._next_page(page):
                break
            page += 1
            cancellable_wait(
                self.cancel_event,
                max(0.0, self.settings.request_delay_seconds),
                "Collection stopped between court result pages",
            )
        return results

    def _wait(self, timeout: float, condition):
        def cancellation_aware(driver):
            raise_if_cancelled(self.cancel_event)
            return condition(driver)

        return WebDriverWait(
            self.driver,
            timeout,
            poll_frequency=0.2,
        ).until(cancellation_aware)

    @staticmethod
    def _filter_page_to_date_range(
        dated_rows: list[tuple[OrderResult, date | None]],
        start_date: date,
        end_date: date,
    ) -> tuple[list[OrderResult], bool]:
        """Filter one newest-first page and flag when its oldest row crosses the boundary."""
        matching = [
            row
            for row, release_date in dated_rows
            if release_date is not None and start_date <= release_date <= end_date
        ]
        parsed_dates = [release_date for _, release_date in dated_rows if release_date is not None]
        return matching, bool(parsed_dates and min(parsed_dates) < start_date)

    @staticmethod
    def _parse_release_date(value: str) -> date | None:
        for pattern in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(value.strip(), pattern).date()
            except (AttributeError, ValueError):
                continue
        return None

    def _open_filtered_results(self) -> None:
        raise_if_cancelled(self.cancel_event)
        self.logger.info("Opening Michigan Courts Case Search")
        cancellable_navigate(
            self.driver,
            self.settings.source_url,
            self.cancel_event,
            context="Collection stopped while opening Michigan Courts",
        )
        raise_if_cancelled(self.cancel_event)
        try:
            advanced = self._wait(
                self.settings.browser_timeout_seconds,
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//button[normalize-space(.)='Advanced Search' or "
                        ".//*[normalize-space(.)='Advanced Search']]",
                    )
                )
            )
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", advanced)
            advanced.click()
            self.logger.info("Opened Advanced Search")

            orders = self._wait(
                self.settings.browser_timeout_seconds,
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//*[@role='group' and @aria-label='Advanced Search Form Selection Group']"
                        "//button[normalize-space(.)='Orders' or .//*[normalize-space(.)='Orders']]",
                    )
                )
            )
            orders.click()
            try:
                self._wait(
                    3,
                    lambda _driver: self._active_orders_button()
                )
            except TimeoutException:
                orders = self._orders_button()
                self.driver.execute_script("arguments[0].click();", orders)
                self._wait(
                    5,
                    lambda _driver: self._active_orders_button()
                )
            self.logger.info("Selected Orders")

            appellate = self._wait(
                self.settings.browser_timeout_seconds,
                lambda driver: self._orders_appellate_select(),
            )
            Select(appellate).select_by_visible_text("Court Of Appeals")
            self.logger.info("Selected Appellate Court: Court Of Appeals")

            # Selecting an option causes Vue to re-render this form, so reacquire
            # the button instead of waiting on a now-stale reference.
            search = self._wait(
                12,
                lambda _driver: self._visible_orders_search_button()
            )
            if search.is_displayed() and search.is_enabled():
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", search)
                search.click()
            else:
                self.driver.execute_script("arguments[0].closest('form').requestSubmit();", search)
            try:
                self._wait(5, EC.url_contains("resultType=orders"))
            except TimeoutException:
                self.logger.info("Search click did not navigate promptly; retrying form submission")
                self.driver.execute_script("arguments[0].closest('form').requestSubmit();", search)
                self._wait(5, EC.url_contains("resultType=orders"))
            self.logger.info("Orders search submitted")

            self._select_page_size_and_sort()
        except CollectionCancelled:
            raise
        except Exception as exc:
            self.logger.warning(
                "Semantic UI navigation failed (%s: %s); using the verified canonical result URL",
                type(exc).__name__,
                exc,
            )
            self._open_verified_result_url()

        self._wait(
            self.settings.browser_timeout_seconds,
            EC.presence_of_element_located((By.CSS_SELECTOR, self.RESULT_CARD)),
        )
        current_url = self.driver.current_url
        required = ("resultType=orders", "aAppellateCourt=Court", "pageSize=100", "sortOrder=Newest")
        if not all(token in current_url for token in required):
            raise SiteAutomationError(f"Result filters could not be verified in URL: {current_url}")
        self.logger.info("Verified live result filters: Court of Appeals / Orders / 100 / Newest")

    def _visible_appellate_select(self):
        for element in self.driver.find_elements(By.CSS_SELECTOR, "select.appellate-court-options"):
            if element.is_displayed() and element.is_enabled():
                return element
        return False

    def _orders_button(self):
        elements = self.driver.find_elements(
            By.XPATH,
            "//*[@role='group' and @aria-label='Advanced Search Form Selection Group']"
            "//button[normalize-space(.)='Orders' or .//*[normalize-space(.)='Orders']]",
        )
        return elements[0] if elements else False

    def _active_orders_button(self):
        button = self._orders_button()
        if button and "btn-secondary" in (button.get_attribute("class") or ""):
            return button
        return False

    def _orders_appellate_select(self):
        """Return the select scoped to the site's stable Orders form container."""
        elements = self.driver.find_elements(
            By.CSS_SELECTOR, ".orders-form select.appellate-court-options"
        )
        return elements[0] if elements else False

    def _visible_orders_search_button(self):
        appellate = self._orders_appellate_select()
        if not appellate:
            return False
        form = appellate.find_element(By.XPATH, "ancestor::form[1]")
        buttons = form.find_elements(By.CSS_SELECTOR, "button[type='submit']")
        return buttons[0] if buttons else False

    def _select_page_size_and_sort(self) -> None:
        page_size = self._wait(
            self.settings.browser_timeout_seconds,
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//select[.//option[normalize-space(.)='10'] and "
                    ".//option[normalize-space(.)='100']]",
                )
            )
        )
        if page_size.get_attribute("value") != str(self.settings.page_size):
            Select(page_size).select_by_visible_text(str(self.settings.page_size))
            self._wait(
                self.settings.browser_timeout_seconds,
                EC.url_contains(f"pageSize={self.settings.page_size}"),
            )
        sort_box = self._wait(
            self.settings.browser_timeout_seconds,
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//select[.//option[normalize-space(.)='Newest'] and "
                    ".//option[normalize-space(.)='Oldest']]",
                )
            )
        )
        if sort_box.get_attribute("value") != self.settings.sort_order:
            Select(sort_box).select_by_visible_text(self.settings.sort_order)
            self._wait(
                self.settings.browser_timeout_seconds,
                EC.url_contains(f"sortOrder={self.settings.sort_order}"),
            )

    def _open_verified_result_url(self) -> None:
        base = self.settings.source_url.rstrip("/") + "/"
        url = (
            f"{base}?page=1&resultType=orders&sortOrder={self.settings.sort_order}"
            f"&pageSize={self.settings.page_size}&aAppellateCourt=Court%20Of%20Appeals"
        )
        raise_if_cancelled(self.cancel_event)
        cancellable_navigate(
            self.driver,
            url,
            self.cancel_event,
            context="Collection stopped while opening court order results",
        )
        raise_if_cancelled(self.cancel_event)

    def _read_current_page(self, page: int) -> list[OrderResult]:
        try:
            raw_rows = self.driver.execute_script(
                r"""
                const text = (card, selector) => {
                  const node = card.querySelector(selector);
                  return node ? (node.textContent || '').replace(/\s+/g, ' ').trim() : '';
                };
                return Array.from(document.querySelectorAll('div.order-item')).map((card, index) => {
                  const pdf = card.querySelector('a.document-url');
                  const caseLink = card.querySelector("a[href*='/case/']");
                  return {
                    position: index + 1,
                    docketText: text(card, '.coa-case-number'),
                    title: text(card, '.order-title-link') || (pdf ? pdf.title : ''),
                    lowerCourt: text(card, '.lower-court'),
                    releaseDate: text(card, '.release-date'),
                    orderType: text(card, '.order-label'),
                    pdfUrl: pdf ? pdf.href : '',
                    caseUrl: caseLink ? caseLink.href : ''
                  };
                });
                """
            )
            output: list[OrderResult] = []
            for row in raw_rows or []:
                href = str(row.get("pdfUrl") or "")
                if not urlparse(href).path.lower().endswith(".pdf"):
                    continue
                source_filename = Path(urlparse(href).path).name
                docket_match = re.search(r"#\s*(\d+)", str(row.get("docketText") or ""))
                source_docket = re.match(r"(\d+)_", source_filename)
                docket = docket_match.group(1) if docket_match else source_docket.group(1) if source_docket else ""
                release = re.sub(
                    r"^\s*Release Date:\s*",
                    "",
                    str(row.get("releaseDate") or ""),
                    flags=re.IGNORECASE,
                )
                output.append(
                    OrderResult(
                        page=page,
                        position=int(row.get("position") or len(output) + 1),
                        docket=docket,
                        title=str(row.get("title") or "").strip(),
                        lower_court=str(row.get("lowerCourt") or "").strip(),
                        release_date=release.strip(),
                        order_type=str(row.get("orderType") or "").strip(),
                        pdf_url=href,
                        original_filename=source_filename,
                        case_url=str(row.get("caseUrl") or ""),
                    )
                )
            return output
        except Exception as exc:
            self.logger.warning("Fast result-card extraction failed; using Selenium fallback: %s", exc)
            return self._read_current_page_selenium(page)

    def _read_current_page_selenium(self, page: int) -> list[OrderResult]:
        output: list[OrderResult] = []
        cards = self.driver.find_elements(By.CSS_SELECTOR, self.RESULT_CARD)
        for position, card in enumerate(cards, 1):
            try:
                pdf_link = card.find_element(By.CSS_SELECTOR, self.PDF_LINK)
                href = urljoin(self.driver.current_url, pdf_link.get_attribute("href") or "")
                if not href or not urlparse(href).path.lower().endswith(".pdf"):
                    continue
                docket_text = self._text(card, ".coa-case-number")
                docket_match = re.search(r"#\s*(\d+)", docket_text)
                source_filename = Path(urlparse(href).path).name
                source_docket = re.match(r"(\d+)_", source_filename)
                docket = (
                    docket_match.group(1)
                    if docket_match
                    else source_docket.group(1) if source_docket else ""
                )
                title = self._text(card, ".order-title-link") or (pdf_link.get_attribute("title") or "")
                release = self._text(card, ".release-date")
                release = re.sub(r"^\s*Release Date:\s*", "", release, flags=re.IGNORECASE)
                case_url = ""
                case_links = card.find_elements(By.CSS_SELECTOR, "a[href*='/case/']")
                if case_links:
                    case_url = urljoin(self.driver.current_url, case_links[0].get_attribute("href") or "")
                output.append(
                    OrderResult(
                        page=page,
                        position=position,
                        docket=docket,
                        title=title.strip(),
                        lower_court=self._text(card, ".lower-court"),
                        release_date=release.strip(),
                        order_type=self._text(card, ".order-label"),
                        pdf_url=href,
                        original_filename=source_filename,
                        case_url=case_url,
                    )
                )
            except StaleElementReferenceException:
                raise
            except Exception as exc:
                self.logger.warning("Skipped unreadable result card %d on page %d: %s", position, page, exc)
        return output

    @staticmethod
    def _text(parent, selector: str) -> str:
        elements = parent.find_elements(By.CSS_SELECTOR, selector)
        return elements[0].text.strip() if elements else ""

    def _next_page(self, current_page: int) -> bool:
        buttons = self.driver.find_elements(By.CSS_SELECTOR, self.NEXT_BUTTON)
        if not buttons:
            return False
        button = buttons[0]
        if not button.is_enabled() or button.get_attribute("disabled") is not None:
            self.logger.info("Pagination complete at page %d", current_page)
            return False
        first_link = self.driver.find_element(By.CSS_SELECTOR, f"{self.RESULT_CARD} {self.PDF_LINK}")
        old_url = self.driver.current_url
        button.click()
        self._wait(
            self.settings.browser_timeout_seconds,
            lambda driver: driver.current_url != old_url or EC.staleness_of(first_link)(driver)
        )
        self._wait(
            self.settings.browser_timeout_seconds,
            EC.presence_of_element_located((By.CSS_SELECTOR, self.RESULT_CARD))
        )
        return True

    def download_pdf(self, order: OrderResult, destination: Path, cancel_event=None) -> int:
        active_cancel_event = cancel_event or self.cancel_event
        raise_if_cancelled(active_cancel_event, "Collection stopped before PDF download")
        session = requests.Session()
        retries = Retry(total=3, connect=3, read=3, backoff_factor=0.8, status_forcelist=(429, 500, 502, 503, 504))
        session.mount("https://", HTTPAdapter(max_retries=retries))
        if self.driver:
            for cookie in self.driver.get_cookies():
                session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"))
        headers = {"User-Agent": self.settings.user_agent, "Referer": self.settings.source_url}
        part = destination.with_suffix(destination.suffix + ".part")
        response = None
        try:
            response = self._open_download_response(
                session,
                order.pdf_url,
                headers,
                active_cancel_event,
            )
            raise_if_cancelled(active_cancel_event, "Collection stopped during PDF download")
            response.raise_for_status()
            with part.open("wb") as handle:
                for chunk in response.iter_content(1024 * 256):
                    raise_if_cancelled(
                        active_cancel_event,
                        "Collection stopped during PDF download",
                    )
                    if chunk:
                        handle.write(chunk)
            raise_if_cancelled(active_cancel_event, "Collection stopped during PDF download")
            if part.stat().st_size < 100 or part.read_bytes()[:5] != b"%PDF-":
                raise SiteAutomationError(f"Downloaded content is not a valid PDF: {order.pdf_url}")
            replace_file_with_retry(
                part,
                destination,
                logger=self.logger,
                cancel_event=active_cancel_event,
            )
            return destination.stat().st_size
        finally:
            part.unlink(missing_ok=True)
            if response is not None:
                response.close()
            session.close()

    def _open_download_response(self, session, url: str, headers: dict, cancel_event):
        """Open a streamed request without trapping Stop behind connect/retry waits."""

        if cancel_event is None:
            return session.get(
                url,
                headers=headers,
                stream=True,
                timeout=(
                    min(15, self.settings.download_timeout_seconds),
                    min(5, self.settings.download_timeout_seconds),
                ),
            )

        finished = threading.Event()
        responses = []
        failures: list[BaseException] = []

        def open_response() -> None:
            try:
                response = session.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(
                        min(15, self.settings.download_timeout_seconds),
                        min(5, self.settings.download_timeout_seconds),
                    ),
                )
                if cancel_event.is_set():
                    response.close()
                else:
                    responses.append(response)
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(
            target=open_response,
            name="miap00-pdf-connect",
            daemon=True,
        )
        thread.start()
        while not finished.wait(0.1):
            if cancel_event.is_set():
                session.close()
                raise CollectionCancelled("Collection stopped while connecting to a PDF")
        if cancel_event.is_set():
            for response in responses:
                response.close()
            raise CollectionCancelled("Collection stopped while connecting to a PDF")
        if failures:
            raise failures[0]
        return responses[0]

    def close(self) -> None:
        if self.driver:
            try:
                close_chrome_driver(self.driver, self.logger)
            finally:
                self.driver = None
