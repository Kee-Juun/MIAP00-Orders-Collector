"""Read-only duplicate lookup against the IRT inventory search."""

from __future__ import annotations

from datetime import date
import logging
import time
from typing import Any

from selenium.common.exceptions import JavascriptException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config.settings import Settings
from core.cancellation import (
    CollectionCancelled,
    cancellable_wait,
    raise_if_cancelled,
)
from core.naming import normalize_final_key
from .webdriver_factory import (
    cancellable_navigate,
    close_chrome_driver,
    create_chrome_driver,
)


class IRTError(RuntimeError):
    pass


class IRTDuplicateChecker:
    DATE_FROM_ID = "receivedDateandTimeSearchFrom"
    DATE_TO_ID = "receivedDateandTimeSearchTo"
    COURT_ID = "courtSearch"
    DOCKET_ID = "docketNumberSearch"
    FILE_NAME_ID = "originalFileNameSearch"
    SEARCH_ID = "search"
    TABLE_ID = "searchTable"

    SERVER_DOWN_MARKERS = (
        "there is some error in the application",
        "please raise a webstar",
    )

    def __init__(self, settings: Settings, logger: logging.Logger, cancel_event=None):
        self.settings = settings
        self.logger = logger
        self.cancel_event = cancel_event
        self.driver = None
        self.initialized = False
        self.bulk_load_success = False

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

    def _wait(self, timeout: float, condition):
        def cancellation_aware(driver):
            raise_if_cancelled(self.cancel_event)
            return condition(driver)

        return WebDriverWait(
            self.driver,
            timeout,
            poll_frequency=0.2,
        ).until(cancellation_aware)

    def initialize(self) -> None:
        if self.initialized:
            return
        self.start()
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                raise_if_cancelled(self.cancel_event)
                self.logger.info("IRT startup attempt %d/3", attempt)
                cancellable_navigate(
                    self.driver,
                    self.settings.irt_url,
                    self.cancel_event,
                    context="Collection stopped while opening IRT",
                )
                raise_if_cancelled(self.cancel_event)
                if self._server_down():
                    raise IRTError("IRT is displaying its application/server error page")
                search_inventory = self._wait(
                    self.settings.browser_timeout_seconds,
                    EC.element_to_be_clickable(
                        (
                            By.XPATH,
                            "//*[@id='menu']//a[contains(normalize-space(.),'Search Inventory')]"
                            " | //*[@id='menu']/table/thead/tr/td[3]/h3/a",
                        )
                    )
                )
                search_inventory.click()
                self._wait(
                    self.settings.browser_timeout_seconds,
                    EC.presence_of_element_located((By.ID, self.FILE_NAME_ID))
                )
                if self._server_down():
                    raise IRTError("IRT is displaying its application/server error page")
                self._set_field(self.COURT_ID, self.settings.irt_court_code)
                self.initialized = True
                self.logger.info("IRT search ready for court code %s", self.settings.irt_court_code)
                return
            except CollectionCancelled:
                raise
            except Exception as exc:
                last_error = exc
                self.logger.warning("IRT startup attempt %d failed: %s", attempt, exc)
                if attempt < 3:
                    cancellable_wait(
                        self.cancel_event,
                        2,
                        "Collection stopped while retrying IRT startup",
                    )
        raise IRTError(f"IRT duplicate search could not be initialized: {last_error}")

    def load_existing(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[str, list[dict[str, Any]]]:
        self.bulk_load_success = False
        raise_if_cancelled(self.cancel_event)
        self.initialize()
        self._set_field(self.DOCKET_ID, "")
        self._set_field(self.FILE_NAME_ID, "")
        self._set_field(self.COURT_ID, self.settings.irt_court_code)
        self._set_date_field(self.DATE_FROM_ID, start_date.strftime("%m-%d-%Y"))
        self._set_date_field(self.DATE_TO_ID, end_date.strftime("%m-%d-%Y"))
        self.logger.info(
            "Loading one complete IRT snapshot for %s from %s through %s",
            self.settings.irt_court_code,
            start_date.strftime("%m-%d-%Y"),
            end_date.strftime("%m-%d-%Y"),
        )
        existing: dict[str, list[dict[str, Any]]] = {}
        self._search()
        raise_if_cancelled(self.cancel_event)
        expected_count = self._result_count()
        captured_count = 0
        page = 1
        seen_pages: set[tuple[str, ...]] = set()
        while True:
            raise_if_cancelled(
                self.cancel_event,
                "Collection stopped while capturing IRT results",
            )
            records = self._records()
            signature = tuple(record.get("File Name", "") for record in records)
            if signature in seen_pages:
                raise IRTError(
                    "IRT pagination repeated a results page before the complete table was captured"
                )
            seen_pages.add(signature)
            captured_count += len(records)
            self.logger.info("IRT snapshot results page %d: %d record(s)", page, len(records))
            for record in records:
                key = normalize_final_key(record.get("File Name", ""))
                if key:
                    existing.setdefault(key, []).append(record)
            if not self._next_page():
                break
            page += 1

        if expected_count is not None and captured_count < expected_count:
            raise IRTError(
                "IRT snapshot was incomplete: "
                f"captured {captured_count} of {expected_count} displayed record(s)"
            )
        self.bulk_load_success = True
        if not existing and self.settings.require_nonempty_irt_index:
            self.bulk_load_success = False
            raise IRTError(
                "IRT returned no recognizable MIAP00 filenames for the configured court code "
                f"{self.settings.irt_court_code}. Confirm the court code before collecting."
            )
        self.logger.info(
            "IRT duplicate index ready: %d MIAP00 filename key(s) from %d captured record(s)",
            len(existing),
            captured_count,
        )
        return existing

    @staticmethod
    def duplicate_records(
        target_filename: str,
        existing: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        key = normalize_final_key(target_filename)
        return list(existing.get(key, [])) if key else []

    def find_existing_counsel(
        self,
        docket: str,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        """Return every matching counsel inventory row for one appellate docket."""

        raise_if_cancelled(self.cancel_event)
        self.initialize()
        pattern = f"*{docket}*counsel*"
        self._set_field(self.COURT_ID, self.settings.irt_court_code)
        self._set_field(self.DOCKET_ID, docket)
        self._set_field(self.FILE_NAME_ID, pattern)
        self._set_date_field(self.DATE_FROM_ID, start_date.strftime("%m-%d-%Y"))
        self._set_date_field(self.DATE_TO_ID, end_date.strftime("%m-%d-%Y"))
        self.logger.info(
            "IRT counsel duplicate check: court=%s docket=%s filename=%s",
            self.settings.irt_court_code,
            docket,
            pattern,
        )
        self._search()
        records = self._capture_current_results(
            context=f"counsel duplicate search for docket {docket}"
        )
        docket_key = docket.casefold()
        matches = [
            record
            for record in records
            if docket_key in str(record.get("File Name", "")).casefold()
            and "counsel" in str(record.get("File Name", "")).casefold()
        ]
        self.logger.info(
            "IRT counsel duplicate result for %s: %d matching record(s)",
            docket,
            len(matches),
        )
        return matches

    def _capture_current_results(self, *, context: str) -> list[dict[str, Any]]:
        """Capture every page from the current IRT search and verify completeness."""

        expected_count = self._result_count()
        captured: list[dict[str, Any]] = []
        seen_pages: set[tuple[str, ...]] = set()
        page = 1
        while True:
            raise_if_cancelled(
                self.cancel_event,
                f"Collection stopped while capturing IRT {context}",
            )
            records = self._records()
            signature = tuple(record.get("File Name", "") for record in records)
            if signature in seen_pages:
                raise IRTError(
                    f"IRT pagination repeated a page during {context}"
                )
            seen_pages.add(signature)
            captured.extend(records)
            self.logger.info(
                "IRT %s results page %d: %d record(s)",
                context,
                page,
                len(records),
            )
            if not self._next_page():
                break
            page += 1
        if expected_count is not None and len(captured) < expected_count:
            raise IRTError(
                f"IRT {context} was incomplete: captured {len(captured)} of "
                f"{expected_count} displayed record(s)"
            )
        return captured

    def _set_field(self, element_id: str, value: str) -> None:
        field = self._wait(
            self.settings.browser_timeout_seconds,
            EC.presence_of_element_located((By.ID, element_id))
        )
        field.clear()
        if value:
            field.send_keys(value)

    def _set_date_field(self, element_id: str, value: str) -> None:
        """Set an IRT date and fire the browser events used by its search form."""

        field = self._wait(
            self.settings.browser_timeout_seconds,
            EC.presence_of_element_located((By.ID, element_id))
        )
        self.driver.execute_script(
            """
            const field = arguments[0];
            const value = arguments[1];
            field.focus();
            field.value = value;
            field.setAttribute('value', value);
            field.dispatchEvent(new Event('input', {bubbles: true}));
            field.dispatchEvent(new Event('change', {bubbles: true}));
            field.dispatchEvent(new Event('blur', {bubbles: true}));
            """,
            field,
            value,
        )
        actual = (field.get_attribute("value") or "").strip()
        if actual != value:
            raise IRTError(
                f"IRT date field {element_id} did not retain {value!r}; found {actual!r}"
            )

    def _search(self) -> None:
        raise_if_cancelled(self.cancel_event)
        search_token = self._arm_search_observer()
        button = self.driver.find_element(By.ID, self.SEARCH_ID)
        button.click()
        if not self._wait_for_request_activity():
            forced = self.driver.execute_script(
                """
                if (typeof getInventorySearch === 'function') {
                  getInventorySearch(1);
                  return true;
                }
                return false;
                """
            )
            if not forced:
                raise IRTError("IRT Search click did not start a results request")
            self.logger.info("Triggered IRT snapshot search using the verified JavaScript fallback")
            if not self._wait_for_request_activity():
                raise IRTError("IRT snapshot search did not start after the JavaScript fallback")

        def ready(driver) -> bool:
            raise_if_cancelled(
                self.cancel_event,
                "Collection stopped while waiting for IRT results",
            )
            if self._server_down():
                raise IRTError("IRT failed while loading duplicate results")
            try:
                state = self._table_state()
            except (JavascriptException, StaleElementReferenceException):
                return False
            if (
                not state.get("present")
                or state.get("processing")
                or state.get("request_in_flight")
            ):
                return False
            has_results = bool(state.get("has_records") or state.get("no_records"))
            if not has_results:
                return False
            # Never accept a table based on elapsed time. The table must prove
            # that this specific Search click caused an Ajax completion or DOM
            # redraw, then remain quiet briefly with the processing layer off.
            belongs_to_search = state.get("search_token") == search_token
            refreshed = bool(
                state.get("xhr_complete")
                or state.get("processing_seen")
                or (
                    state.get("request_started")
                    and state.get("search_mutations", 0) > 0
                )
            )
            settled = state.get("search_quiet_ms", 0) >= 250
            return belongs_to_search and refreshed and settled

        try:
            self._wait(self.settings.irt_timeout_seconds, ready)
        except TimeoutException as exc:
            final_state = self._table_state()
            self.logger.error("IRT table state at timeout: %s", final_state)
            raise IRTError(
                "IRT result table did not finish loading before timeout "
                f"(present={final_state.get('present')}, "
                f"processing={final_state.get('processing')}, "
                f"has_records={final_state.get('has_records')}, "
                f"no_records={final_state.get('no_records')})"
            ) from exc
        cancellable_wait(
            self.cancel_event,
            0.5,
            "Collection stopped while IRT results settled",
        )

    def _wait_for_request_activity(self, timeout: float = 2.5) -> bool:
        """Confirm that Search actually initiated Ajax or a blocking UI transition."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raise_if_cancelled(
                self.cancel_event,
                "Collection stopped while starting the IRT search",
            )
            state = self.driver.execute_script(
                """
                const search = window.__miapIrtSearchState || {};
                const jqActive = typeof window.jQuery !== 'undefined'
                  ? Number(window.jQuery.active || 0) : 0;
                const blocked = Array.from(
                  document.querySelectorAll('div.blockUI, div.blockOverlay')
                ).some(node => {
                  const style = getComputedStyle(node);
                  return style.display !== 'none' && style.visibility !== 'hidden';
                });
                const active = jqActive > 0 || blocked || Boolean(
                  search.requestStarted || search.xhrComplete || search.processingSeen
                );
                if (active) search.requestStarted = true;
                return active;
                """
            )
            if state:
                return True
            cancellable_wait(
                self.cancel_event,
                0.1,
                "Collection stopped while starting the IRT search",
            )
        return False

    def _arm_search_observer(self) -> str:
        """Track the DOM/Ajax transition caused by one specific Search click."""

        token = str(time.time_ns())
        returned = self.driver.execute_script(
            r"""
            const id = arguments[0];
            const token = arguments[1];
            const table = document.getElementById(id);
            if (!table) return '';
            if (window.__miapIrtObserver) {
              try { window.__miapIrtObserver.disconnect(); } catch (_) {}
            }
            const state = {
              token: token, mutations: 0, lastMutation: Date.now(),
              processingSeen: false, requestStarted: false, xhrComplete: false
            };
            window.__miapIrtSearchState = state;
            const wrapper = document.getElementById(id + '_wrapper');
            const target = wrapper || table.parentElement || table;
            const observer = new MutationObserver(() => {
              state.mutations += 1;
              state.lastMutation = Date.now();
              const processing = document.getElementById(id + '_processing');
              if (processing && getComputedStyle(processing).display !== 'none') {
                state.processingSeen = true;
              }
            });
            observer.observe(target, {
              subtree: true, childList: true, characterData: true,
              attributes: true, attributeFilter: ['style', 'class']
            });
            window.__miapIrtObserver = observer;

            const jq = window.jQuery;
            if (jq && jq.fn && jq.fn.dataTable) {
              jq(table).one('preXhr.dt.miapSearch', () => {
                state.requestStarted = true;
                state.lastMutation = Date.now();
              });
              jq(table).one('xhr.dt.miapSearch', () => {
                state.xhrComplete = true;
                state.lastMutation = Date.now();
              });
            }
            return token;
            """,
            self.TABLE_ID,
            token,
        )
        return str(returned or token)

    def _table_state(self) -> dict[str, Any]:
        """Read the live DataTables state in one atomic browser operation."""
        state = self.driver.execute_script(
            r"""
            const id = arguments[0];
            const table = document.getElementById(id);
            if (!table) return {present: false, processing: false, signature: ''};
            const processing = document.getElementById(id + '_processing');
            const processingVisible = Boolean(processing) &&
              getComputedStyle(processing).display !== 'none' &&
              getComputedStyle(processing).visibility !== 'hidden' &&
              Number(getComputedStyle(processing).opacity || 1) > 0 &&
              processing.getClientRects().length > 0 &&
              processing.offsetWidth > 0 && processing.offsetHeight > 0;
            const search = window.__miapIrtSearchState || {};
            if (processingVisible) search.processingSeen = true;
            const jqActive = typeof window.jQuery !== 'undefined'
              ? Number(window.jQuery.active || 0) : 0;
            const blockUiVisible = Array.from(
              document.querySelectorAll('div.blockUI, div.blockOverlay')
            ).some(node => {
              const style = getComputedStyle(node);
              return style.display !== 'none' && style.visibility !== 'hidden';
            });
            const rows = Array.from(table.querySelectorAll('tbody tr'));
            const rowText = rows.map(row =>
              (row.textContent || '').replace(/\s+/g, ' ').trim()
            );
            const text = rowText.join('\n').toLowerCase();
            const emptyMarkers = [
              'no matching records', 'no data available', 'no records found', 'no data found'
            ];
            const noRecords = emptyMarkers.some(marker => text.includes(marker));
            const hasRecords = rows.some((row, index) => {
              const cells = row.querySelectorAll('td');
              return cells.length >= 2 && rowText[index] &&
                !emptyMarkers.some(marker => rowText[index].toLowerCase().includes(marker));
            });
            return {
              present: true,
              processing: processingVisible,
              request_in_flight: jqActive > 0 || blockUiVisible,
              no_records: noRecords,
              has_records: hasRecords,
              signature: rowText.join('|'),
              search_token: search.token || '',
              search_mutations: Number(search.mutations || 0),
              search_quiet_ms: Math.max(0, Date.now() - Number(search.lastMutation || Date.now())),
              processing_seen: Boolean(search.processingSeen),
              request_started: Boolean(search.requestStarted),
              xhr_complete: Boolean(search.xhrComplete)
            };
            """,
            self.TABLE_ID,
        )
        return dict(state or {})

    def _records(self) -> list[dict[str, Any]]:
        extraction = self.driver.execute_script(
            r"""
            const table = document.getElementById(arguments[0]);
            if (!table) return [];
            const headers = Array.from(table.querySelectorAll('thead tr:last-child th'))
              .map(x => (x.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase());
            const idx = (name, fallback) => {
              const found = headers.indexOf(name.toLowerCase());
              return found >= 0 ? found : fallback;
            };
            const indexes = {
              lni: idx('lni', 0), file: idx('file name', 1), decided: idx('decided date', 5),
              received: idx('received date', 6), route: idx('route', 14), source: idx('source detail', 15)
            };
            const text = (cells, i) => cells[i] ? (cells[i].textContent || '').trim() : '';
            return Array.from(table.querySelectorAll('tbody tr')).map(row => {
              const cells = Array.from(row.querySelectorAll('td'));
              return {
                'LNI': text(cells, indexes.lni), 'File Name': text(cells, indexes.file),
                'Decided Date': text(cells, indexes.decided), 'Received Date': text(cells, indexes.received),
                'Route': text(cells, indexes.route), 'Source Detail': text(cells, indexes.source)
              };
            }).filter(record => record['File Name']);
            """,
            self.TABLE_ID,
        )
        return list(extraction or [])

    def _result_count(self) -> int | None:
        """Return DataTables' total filtered result count when the page exposes it."""

        value = self.driver.execute_script(
            r"""
            const id = arguments[0];
            const table = document.getElementById(id);
            const jq = window.jQuery;
            try {
              if (table && jq && jq.fn && jq.fn.dataTable && jq.fn.dataTable.isDataTable(table)) {
                return Number(jq(table).DataTable().page.info().recordsDisplay);
              }
            } catch (_) {}
            const labels = [
              document.getElementById(id + '_info'),
              document.getElementById('searchCount')
            ].filter(Boolean).map(node => (node.textContent || '').replace(/,/g, ''));
            for (const label of labels) {
              const showing = label.match(/of\s+(\d+)\s+entries/i);
              if (showing) return Number(showing[1]);
              const count = label.match(/(?:total|found|records?)\D+(\d+)/i);
              if (count) return Number(count[1]);
            }
            return null;
            """,
            self.TABLE_ID,
        )
        return int(value) if value is not None else None

    def _next_page(self) -> bool:
        raise_if_cancelled(
            self.cancel_event,
            "Collection stopped while paging through IRT results",
        )
        selectors = (
            f"#{self.TABLE_ID}_next:not(.disabled) a",
            f"#{self.TABLE_ID}_next:not(.disabled)",
            "a.paginate_button.next:not(.disabled)",
            "button.paginate_button.next:not(:disabled)",
        )
        for selector in selectors:
            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if not elements:
                continue
            element = elements[0]
            if not element.is_displayed() or not element.is_enabled():
                continue
            old_first = ""
            records = self._records()
            if records:
                old_first = records[0].get("File Name", "")
            element.click()
            self._wait(
                self.settings.irt_timeout_seconds,
                lambda _driver: not old_first
                or not self._records()
                or self._records()[0].get("File Name", "") != old_first
            )
            return True
        return False

    def _server_down(self) -> bool:
        if not self.driver:
            return False
        try:
            text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            return False
        return all(marker in text for marker in self.SERVER_DOWN_MARKERS)

    def close(self) -> None:
        if self.driver:
            try:
                close_chrome_driver(self.driver, self.logger)
            finally:
                self.driver = None
                self.initialized = False
