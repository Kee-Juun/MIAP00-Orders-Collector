"""Read-only live smoke check: navigate, filter, and parse one results page."""

from __future__ import annotations

from dataclasses import replace
import logging
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from browser.michigan_courts import MichiganOrdersSite
from config.settings import Settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    logger = logging.getLogger("MIAP00.smoke")
    settings = replace(Settings(), max_pages=1, headless=True)
    site = MichiganOrdersSite(settings, logger)
    try:
        rows = site.collect_result_metadata()
        if not rows:
            raise RuntimeError("No PDF order result cards were parsed")
        first = rows[0]
        print(f"PASS: {len(rows)} PDF orders parsed; first={first.original_filename} docket={first.docket}")
        return 0
    finally:
        site.close()


if __name__ == "__main__":
    raise SystemExit(main())
