# MIAP00 Orders Collector

Desktop and CLI automation for collecting Michigan Court of Appeals PDF orders from the Michigan Courts Case Search site. The collector uses IRT as a mandatory duplicate gate and faithfully ports FileFlex's MIAP00 filename logic.

## What it does

1. Opens the Michigan Courts Case Search page.
2. Opens **Advanced Search**, selects **Orders**, and selects **Court Of Appeals**.
3. Lets the operator choose an inclusive **release-date range** in the PyQt6 desktop UI.
4. Sets **Items per page** to 100 and **Sort By** to **Newest**, then stops pagination as soon as a page crosses the requested start date.
5. Keeps only results inside the chosen date window and captures docket, title, lower court, release date, case URL, source filename, and PDF URL.
6. Downloads each PDF into a run-scoped hidden temporary folder and validates the `%PDF-` signature.
7. Extracts the MIAP00 docket from the source filename and the authoritative document date from PDF text, with Tesseract OCR fallback.
8. Builds the FileFlex name, `LDC_SMD_<docket><occurrence-suffix>_<MMDDYYYY>.pdf`, and renames the temporary PDF immediately.
9. Runs one complete `STMIAP00` IRT search for the operator's full date range, captures every results page, and builds an in-memory filename index.
10. Compares every newly assigned filename locally against that snapshot. Duplicates are deleted from temporary storage and recorded with their IRT evidence.
11. Uses exact IRT matches as online parent copies and excludes high-confidence consolidated siblings when their appellate docket sets and normalized PDF content match.
12. Moves only accepted PDFs into the run's `Collected_<run-folder-name>` subfolder, then writes an Excel report and background log named after that run folder.

## Verified live selectors (August 14, 2026)

The implementation intentionally does not use the site's generated `uid-*` identifiers.

- Advanced Search: visible button text `Advanced Search`
- Search type: button `Orders` inside role group `Advanced Search Form Selection Group`
- Appellate court: visible `select.appellate-court-options`, option text `Court Of Appeals`
- Search: enabled submit button in the selected Orders form
- Page size: select containing options `10` and `100`
- Sort: select containing options `Newest` and `Oldest`
- Result card: `div.order-item`
- PDF link: `a.document-url`
- Pagination: `button.next-button` / accessible name `Next Page`

The verified result URL shape is:

```text
https://www.courts.michigan.gov/case-search/?page=1&resultType=orders&sortOrder=Newest&pageSize=100&aAppellateCourt=Court%20Of%20Appeals
```

The semantic UI flow is primary. The verified URL is a logged fallback if the public site's modal interaction changes while its query contract remains available.

## Architecture

The project root follows the same functional grouping used by the WVPUC0, ALLCUS, and FCC collectors:

```text
./
  app/
    cli.py               # Reusable CLI argument and launch logic
  browser/
    michigan_courts.py   # Court search, pagination, and PDF transfer
    irt.py               # Full-date-range IRT snapshot and evidence
    webdriver_factory.py # Shared Chrome construction
  config/
    settings.py          # Configuration and date-range validation
  core/
    cancellation.py      # Prompt cooperative stop signal and cancellable waits
    collector.py         # Run orchestration and fail-closed lifecycle
    content_duplicates.py # IRT-backed and post-download content checks
    models.py            # Domain records
    naming.py            # FileFlex naming, certified dates, and OCR
  reporting/
    excel_report.py      # Excel run report
  ui/
    assets/
      miap00_app_icon.ico       # Multi-resolution Windows executable icon
      miap00_app_icon.png       # Transparent high-resolution source icon
      paper_uncrumple_sprite.png # 16 native-resolution transparent keyframes
    main_window.py       # Compact threaded PyQt6 desktop UI
  utils/
    logging.py           # Run-scoped file and callback logging
```

`main.py` is the application and PyInstaller entry point. `app/cli.py` contains its reusable argument and launch logic, while `scripts/` contains operational smoke checks and `tests/` contains the automated regression suite. There is no `__main__.py` wrapper.

Each run folder keeps its documentation files at the root and places finalized
non-duplicate PDFs in `Collected_<run-folder-name>/`.

## First-time setup

```powershell
cd "MIAP00 Orders Collector"
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
python main.py
```

Chrome is controlled by Selenium Manager. If the workstation is offline, put the compatible driver path in `chromedriver_path`.

Tesseract is optional only when embedded PDF text contains a usable date. Install Tesseract OCR or set `MIAP00_TESSERACT_PATH` for scanned/image PDFs.

## IRT production check

The verified IRT court filter for this workflow is `STMIAP00`. It is applied automatically to the single date-range snapshot search and does not require operator input.

IRT validation is fail-closed. All PDFs are downloaded and renamed in temporary storage first. The collector then searches the operator's complete date range once, waits for the refreshed table, captures every available results page, validates the displayed total against the number captured, and compares the renamed files against that local index. A confirmed duplicate is deleted; a confirmed non-duplicate is finalized; and an empty, stale, or incomplete IRT snapshot prevents every unverified PDF from being finalized.

Before deleting the exact IRT matches from temporary storage, the collector uses them as online-parent reference copies. A remaining PDF is classified as a `consolidated_duplicate` only when it shares consolidated appellate docket numbers with an exact IRT parent, has the same decision date, and reaches at least 97% normalized-content similarity. Its report row preserves the parent filename, shared dockets, similarity, LNI, and other IRT evidence. A docket mention alone is never enough to exclude a file.

## Run modes

Desktop UI:

```powershell
python main.py
```

Chrome runs in the background by default. Uncheck **Run Chrome in background** in the UI when a visible browser session is needed.

CLI:

```powershell
python main.py --no-gui --start-date 2026-08-01 --end-date 2026-08-14
```

Read-only live selector smoke test (no PDF downloads and no IRT access):

```powershell
python scripts\smoke_site.py
```

Unit tests:

```powershell
python -m unittest discover -s tests -v
```

## Output

Each run creates:

```text
Downloads/MIAP00 Orders Collections/MIAP00_MM-DD-YYYY_HH-MM-SS-ms/
  LDC_SMD_<docket>_<MMDDYYYY>.pdf
  Log_MIAP00_MM-DD-YYYY_HH-MM-SS-ms.log
  Report_MIAP00_MM-DD-YYYY_HH-MM-SS-ms.xlsx
```

After all IRT-cleared PDFs are finalized, the collector performs a second quality pass against the downloaded content. It removes identical normalized-text or rendered-content copies, and also detects high-confidence consolidated-case copies that share appellate docket numbers. The first collected file is retained (normally the unsuffixed FileFlex name), while removed copies are recorded as `content_duplicate`.

Order filenames use the certified decision date read from the rendered footer. The collector OCRs the lower portion of each order's final pages, anchors the date to the certification/signature block, and validates it against the live-site release date before querying IRT. A missing or conflicting date fails closed for that PDF instead of allowing a deadline or hearing date from the order body into the filename.

The one IRT snapshot waits for a browser-observed Ajax/DOM refresh tied to that Search click; elapsed time alone can never validate the result table. The start and end dates are entered with the same input/change/blur events used by the working PLR000-CCA001 workflow, and the collector fails closed if the captured row count is smaller than IRT's displayed result count.

The workbook includes Summary, Filenames, Collected, Duplicates, Errors, and Discovered sheets. Filenames is a copy-friendly single-column list of successfully collected final filenames after the content quality pass. Duplicate rows preserve the IRT LNI, stored filename, decided/received dates, route, source detail, and post-run content-duplicate reason where applicable.

## Operational notes

- The Michigan Courts page states that bulk downloads and commercial use are prohibited. Configure a responsible page limit and delay appropriate to the authorized workflow and the site's terms.
- The date window is inclusive. Results are newest-first, and the collector stops paging immediately after it encounters a release date older than the selected start date.
- `max_pages` remains an optional configuration/CLI safety cap, but it is no longer the collection criterion exposed by the desktop UI.
- The collector waits for state changes instead of using fixed sleeps for page transitions; `request_delay_seconds` provides additional load between pages.
- No existing output is overwritten.
- Temporary downloads are removed after every run, including cancellation and fatal errors.
- Stop is cooperative and prompt: it interrupts result/IRT waits, streamed downloads, OCR, and duplicate analysis without using unsafe thread termination. Unverified temporary PDFs are deleted and IRT is not started after cancellation.
