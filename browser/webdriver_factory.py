"""Chrome WebDriver construction shared by browser integrations."""

from __future__ import annotations

import logging
from pathlib import Path
import threading

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from core.cancellation import CollectionCancelled, raise_if_cancelled


class ChromeStartupError(RuntimeError):
    pass


def create_chrome_driver(
    logger: logging.Logger,
    *,
    headless: bool,
    download_dir: Path | None = None,
    chromedriver_path: str = "",
    timeout: int = 60,
) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.page_load_strategy = "eager"
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1100")
    options.add_argument("--disable-popup-blocking")
    if download_dir:
        options.add_experimental_option(
            "prefs",
            {
                "download.default_directory": str(download_dir.resolve()),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "plugins.always_open_pdf_externally": True,
            },
        )
    try:
        if chromedriver_path:
            path = Path(chromedriver_path).expanduser()
            if not path.is_file():
                raise ChromeStartupError(f"Configured ChromeDriver does not exist: {path}")
            driver = webdriver.Chrome(service=Service(str(path)), options=options)
        else:
            driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(timeout)
        driver.implicitly_wait(0)
        capabilities = driver.capabilities or {}
        logger.info(
            "Chrome ready: browser=%s driver=%s",
            capabilities.get("browserVersion", "unknown"),
            str((capabilities.get("chrome") or {}).get("chromedriverVersion", "unknown")).split()[0],
        )
        return driver
    except ChromeStartupError:
        raise
    except (WebDriverException, OSError) as exc:
        details = " ".join(str(exc).split())
        raise ChromeStartupError(
            "Chrome could not start. Confirm Chrome is installed and Selenium Manager can "
            f"obtain a matching driver. Details: {details}"
        ) from exc


def close_chrome_driver(
    driver: webdriver.Chrome,
    logger: logging.Logger,
    *,
    timeout: float = 3.0,
) -> None:
    """Quit Chrome cleanly without letting a stuck driver freeze Stop forever."""

    finished = threading.Event()

    def quit_driver() -> None:
        try:
            driver.quit()
        except Exception as exc:
            logger.warning("Chrome shutdown reported an error: %s", exc)
        finally:
            finished.set()

    thread = threading.Thread(
        target=quit_driver,
        name="miap00-chrome-shutdown",
        daemon=True,
    )
    thread.start()
    if finished.wait(timeout):
        return

    logger.warning("Chrome did not close within %.1f seconds; stopping its driver service", timeout)
    service = getattr(driver, "service", None)
    process = getattr(service, "process", None)
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=1)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def cancellable_navigate(
    driver: webdriver.Chrome,
    url: str,
    cancel_event=None,
    *,
    context: str = "Collection stopped during browser navigation",
) -> None:
    """Navigate on a helper thread so Stop can abort a blocked page load."""

    raise_if_cancelled(cancel_event, context)
    if cancel_event is None:
        driver.get(url)
        return

    finished = threading.Event()
    failures: list[BaseException] = []

    def navigate() -> None:
        try:
            driver.get(url)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(
        target=navigate,
        name="miap00-browser-navigation",
        daemon=True,
    )
    thread.start()
    while not finished.wait(0.1):
        if not cancel_event.is_set():
            continue
        service = getattr(driver, "service", None)
        process = getattr(service, "process", None)
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
        finished.wait(0.5)
        raise CollectionCancelled(context)

    raise_if_cancelled(cancel_event, context)
    if failures:
        raise failures[0]
