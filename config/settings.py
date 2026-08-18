"""Application settings and date-range validation."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass
class Settings:
    source_url: str = "https://www.courts.michigan.gov/case-search/"
    output_root: str = "Downloads/MIAP00 Orders Collections"
    irt_url: str = "https://tcfabprod.lexisnexis.com/shared/InventoryInvoicing/"
    irt_court_code: str = "STMIAP00"
    headless: bool = True
    browser_timeout_seconds: int = 60
    irt_timeout_seconds: int = 120
    download_timeout_seconds: int = 90
    location_check_timeout_seconds: int = 8
    request_delay_seconds: float = 1.0
    page_size: int = 100
    sort_order: str = "Newest"
    start_date: str = ""
    end_date: str = ""
    default_days_back: int = 7
    max_pages: int = 0
    fail_closed_on_irt_error: bool = True
    require_nonempty_irt_index: bool = True
    collect_counsel: bool = True
    counsel_irt_years_back: int = 2
    chromedriver_path: str = ""
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
    )

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        settings = cls()
        config_path = path or Path("config.json")
        if not config_path.exists():
            return settings
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        allowed = {item.name for item in fields(cls)}
        values = {key: value for key, value in raw.items() if key in allowed}
        return cls(**{**asdict(settings), **values})

    def resolved_output_root(self) -> Path:
        expanded = Path(os.path.expandvars(self.output_root)).expanduser()
        if expanded.is_absolute():
            return expanded
        return Path.home() / expanded

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def resolved_date_range(self, today: date | None = None) -> tuple[date, date]:
        """Return the inclusive release-date window for this run."""
        end = self._parse_iso_date(self.end_date) if self.end_date else (today or date.today())
        start = (
            self._parse_iso_date(self.start_date)
            if self.start_date
            else end - timedelta(days=max(0, self.default_days_back))
        )
        if start > end:
            raise ValueError("Start date cannot be after end date")
        return start, end

    @staticmethod
    def _parse_iso_date(value: str) -> date:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc
