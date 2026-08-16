"""Command-line parsing and GUI/terminal launch orchestration."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys

from config.settings import Settings
from core.collector import MIAP00Collector
from ui.main_window import launch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Michigan Court of Appeals Orders collector"
    )
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Run directly in the terminal",
    )
    parser.add_argument("--max-pages", type=int, help="0 means all result pages")
    parser.add_argument("--start-date", help="Inclusive release date, YYYY-MM-DD")
    parser.add_argument("--end-date", help="Inclusive release date, YYYY-MM-DD")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome without visible windows",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load(args.config)
    if args.max_pages is not None:
        settings = replace(settings, max_pages=max(0, args.max_pages))
    if args.start_date is not None:
        settings = replace(settings, start_date=args.start_date)
    if args.end_date is not None:
        settings = replace(settings, end_date=args.end_date)
    settings.resolved_date_range()
    if args.headless:
        settings = replace(settings, headless=True)
    if not args.no_gui:
        launch(settings)
        return 0
    try:
        run_dir = MIAP00Collector(settings).run()
        print(f"Run complete: {run_dir}")
        return 0
    except Exception as exc:
        print(f"Collection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
