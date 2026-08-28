"""Faithful, collector-oriented port of FileFlex's MIAP00 naming rules."""

from __future__ import annotations

import datetime
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .cancellation import CollectionCancelled, raise_if_cancelled


MIAP00_SOURCE_PATTERN = re.compile(
    r"(?:(\d{8})_C(\d+)(?:_\d+_\2[A-Z]?\.opn(?:_ORDER)?\.pdf|"
    r"\(\d+\)_RPTR_[A-Z0-9]+-\2-ASV\.+pdf)|(\d+)_\d+(?:_\d+)?\.pdf)$",
    re.IGNORECASE,
)
_MONTH_DATE_TEXT_PATTERN = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},\s+\d\s*\d\s*\d\s*\d"
)
_MIAP_DATE_PATTERN = re.compile(rf"\b{_MONTH_DATE_TEXT_PATTERN}\b", re.IGNORECASE)
_FINAL_NAME_PATTERN = re.compile(
    r"LDC_SMD_(?P<docket>\d+)(?P<suffix>[a-z]*)_(?P<date>\d{8})\.pdf",
    re.IGNORECASE,
)
_EVENT_REFERENCE_PLACEHOLDER_PATTERN = re.compile(r"SEE EVENT \d+\.?$")


class NamingError(ValueError):
    pass


class NonOrderDocumentError(NamingError):
    """The Orders search returned a document that is not a court order."""


def extract_source_docket(filename: str) -> str:
    match = MIAP00_SOURCE_PATTERN.search(Path(filename).name)
    if not match:
        raise NamingError(f"Source filename does not match FileFlex MIAP00 rules: {filename}")
    return (match.group(2) or match.group(3) or "").strip()


def docket_suffix(occurrence: int) -> str:
    """Return FileFlex's sequence: '', a..z, aa, ab..."""
    if occurrence == 0:
        return ""
    index = occurrence - 1
    letters: list[str] = []
    while True:
        index, remainder = divmod(index, 26)
        letters.append(chr(ord("a") + remainder))
        if index == 0:
            break
        index -= 1
    return "".join(reversed(letters))


def build_filename(docket: str, document_date: str, occurrence: int = 0) -> str:
    if not re.fullmatch(r"\d+", docket):
        raise NamingError(f"Invalid MIAP00 docket: {docket!r}")
    if not re.fullmatch(r"\d{8}", document_date):
        raise NamingError(f"Invalid MIAP00 MMDDYYYY date: {document_date!r}")
    return f"LDC_SMD_{docket}{docket_suffix(occurrence)}_{document_date}.pdf"


def normalize_final_key(filename: str) -> str:
    match = _FINAL_NAME_PATTERN.search(Path(filename).name)
    if not match:
        return ""
    return (
        f"{match.group('docket')}{match.group('suffix').lower()}|"
        f"{match.group('date')}"
    )


def extract_document_date(
    pdf_path: Path,
    logger=None,
    max_pages: int = 2,
    expected_date: str = "",
    allowed_date_range: tuple[datetime.date, datetime.date] | None = None,
    cancel_event=None,
) -> str:
    raise_if_cancelled(cancel_event, "Collection stopped before reading a PDF date")
    text = extract_pdf_text(
        pdf_path,
        max_pages=max_pages,
        logger=logger,
        cancel_event=cancel_event,
    )
    if _looks_like_event_reference_placeholder(text):
        raise NonOrderDocumentError(
            "Michigan Orders search returned an event-reference placeholder, "
            f"not a certified court order: {pdf_path.name}"
        )
    if _looks_like_non_order_clerk_correspondence(text):
        raise NonOrderDocumentError(
            f"Michigan Orders search returned clerk correspondence, not a "
            f"certified court order: {pdf_path.name}"
        )
    if _looks_like_received_party_filing(text):
        raise NonOrderDocumentError(
            f"Michigan Orders search returned a received party filing, not a "
            f"certified court order: {pdf_path.name}"
        )
    publication_date = _extract_publication_date(text, ("FOR PUBLICATION",))
    if publication_date:
        _validate_expected_date(
            publication_date,
            expected_date,
            pdf_path,
            allowed_date_range=allowed_date_range,
            logger=logger,
        )
        return publication_date

    # The certification date on Michigan orders is often absent from the
    # embedded text layer. Always OCR the footer instead of trusting body dates
    # that may describe deadlines, hearings, or transcript due dates.
    _log(logger, "info", f"Reading certification footer with OCR: {pdf_path.name}")
    footer_text = extract_pdf_footer_text_with_ocr(
        pdf_path,
        max_pages=max_pages,
        logger=logger,
        cancel_event=cancel_event,
    )
    date_value = _extract_order_date(footer_text)
    has_certified_footer_date = bool(date_value)
    if not date_value:
        date_value = _extract_expected_date_from_footer(footer_text, expected_date)
    if not date_value:
        _log(logger, "info", f"Footer OCR was inconclusive; trying full-page OCR: {pdf_path.name}")
        ocr_text = extract_pdf_text_with_ocr(
            pdf_path,
            max_pages=max_pages,
            logger=logger,
            cancel_event=cancel_event,
        )
        date_value = _extract_order_date(ocr_text)
        has_certified_footer_date = bool(date_value)
    if not date_value:
        raise NamingError(
            f"No certified MIAP00 decision date found in {pdf_path.name}"
        )
    _validate_expected_date(
        date_value,
        expected_date,
        pdf_path,
        allowed_date_range=allowed_date_range,
        certified_footer_date=has_certified_footer_date,
        logger=logger,
    )
    return date_value


def extract_miap00_date_from_text(text: str) -> str:
    if not text.strip():
        return ""
    publication_date = _extract_publication_date(text, ("FOR PUBLICATION",))
    return publication_date or _extract_order_date(text)


def _looks_like_non_order_clerk_correspondence(text: str) -> bool:
    """Recognize clerk letters without mistaking an attached order for one."""

    if not text.strip():
        return False
    normalized = _normalize_line(text)
    lines = [_normalize_line(line) for line in text.splitlines() if line.strip()]
    has_order_heading = any(
        re.fullmatch(r"(?:AMENDED |CORRECTED )?ORDER", line)
        for line in lines[:160]
    )
    if has_order_heading or "A TRUE COPY ENTERED AND CERTIFIED" in normalized:
        return False
    return (
        "MICHIGAN COURT OF APPEALS" in normalized
        and "OFFICE OF THE CLERK" in normalized
        and "DEAR COUNSEL" in normalized
        and "SINCERELY" in normalized
    )


def _looks_like_event_reference_placeholder(text: str) -> bool:
    """Recognize an otherwise empty ``See event N`` placeholder document."""
    return bool(_EVENT_REFERENCE_PLACEHOLDER_PATTERN.fullmatch(_normalize_line(text)))


def _looks_like_received_party_filing(text: str) -> bool:
    """Recognize court-received submissions without misusing their filing date."""

    if not text.strip():
        return False
    normalized = _normalize_line(text)
    if "RECEIVED BY MCOA" not in normalized:
        return False
    if "A TRUE COPY ENTERED AND CERTIFIED" in normalized:
        return False
    lines = [_normalize_line(line) for line in text.splitlines() if line.strip()]
    has_court_order_heading = any(
        re.fullmatch(r"(?:AMENDED |CORRECTED )?ORDER", line)
        for line in lines[:120]
    )
    return not has_court_order_heading


def _extract_publication_date(text: str, expected_headers: tuple[str, ...]) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    indexes: list[int] = []
    headers = set(expected_headers)
    for index, line in enumerate(lines[:160]):
        normalized = _normalize_line(line)
        if normalized in headers:
            indexes.append(index)
            continue
        if normalized.startswith("IF THIS OPINION INDICATES"):
            continue
        if any(re.search(rf"\b{re.escape(header)}\b", normalized) for header in headers):
            indexes.append(index)
    for index in indexes:
        for line in lines[index : index + 24]:
            found = _MIAP_DATE_PATTERN.search(line)
            if found:
                normalized = _normalize_date(found.group(0))
                if normalized:
                    return normalized
    return ""


def _extract_order_date(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # Only inspect text after the certification legend. A date immediately
    # before it is normally a deadline in the order body.
    for index, line in enumerate(lines):
        if "A TRUE COPY ENTERED AND CERTIFIED" not in _normalize_line(line):
            continue
        for candidate in lines[index : index + 10]:
            date_value = _extract_date_from_line(candidate)
            if date_value:
                return date_value

    # The certification footer is often an image and therefore absent from
    # embedded PDF text. OCR commonly recovers only its date plus these labels.
    # Accept a date only when it is close to a footer label; never fall back to
    # an arbitrary date near the end because orders frequently end with filing
    # deadlines, hearing dates, and other dates that are not the order date.
    # OCR consistently places the printed date directly before the Chief Clerk
    # label, even when the signature or the word "Chief" is slightly garbled.
    for clerk_index, line in enumerate(lines):
        normalized_line = _normalize_line(line)
        if "A TRUE COPY ENTERED AND CERTIFIED" in normalized_line:
            continue
        letters = re.sub(r"[^A-Z]", "", normalized_line)
        if "CHIE" not in letters or "LERK" not in letters:
            continue
        for candidate_index in (
            clerk_index - 1,
            clerk_index - 2,
            clerk_index - 3,
            clerk_index,
            clerk_index + 1,
            clerk_index + 2,
        ):
            if 0 <= candidate_index < len(lines):
                date_value = _extract_date_from_line(lines[candidate_index])
                if date_value:
                    return date_value

    # A standalone "Date" label is not sufficient evidence by itself: when
    # the rendered footer value is absent from embedded text, a nearby body
    # deadline can sit only two extracted lines away. Fail closed instead.
    return ""


def _validate_expected_date(
    date_value: str,
    expected_date: str,
    pdf_path: Path,
    *,
    allowed_date_range: tuple[datetime.date, datetime.date] | None = None,
    certified_footer_date: bool = False,
    logger=None,
) -> None:
    expected = _normalize_expected_date(expected_date)
    if expected and date_value != expected:
        certified_date = datetime.datetime.strptime(date_value, "%m%d%Y").date()
        inside_selected_range = (
            allowed_date_range is not None
            and allowed_date_range[0] <= certified_date <= allowed_date_range[1]
        )
        if certified_footer_date or inside_selected_range:
            if certified_footer_date and not inside_selected_range:
                acceptance_reason = (
                    "The site released or reposted an older certified order; "
                    "the certification footer controls the final filename."
                )
            elif allowed_date_range is not None:
                acceptance_reason = (
                    "The certified date is inside the selected range "
                    f"{allowed_date_range[0].isoformat()} through "
                    f"{allowed_date_range[1].isoformat()}."
                )
            else:
                acceptance_reason = "The certification footer controls the final filename."
            _log(
                logger,
                "warning",
                "Court-card release date mismatch accepted for "
                f"{pdf_path.name}: certified PDF date "
                f"{certified_date.strftime('%m/%d/%Y')}; site card "
                f"{datetime.datetime.strptime(expected, '%m%d%Y').strftime('%m/%d/%Y')}. "
                f"{acceptance_reason}",
            )
            return
        raise NamingError(
            f"Certified decision date {date_value} does not match source release date "
            f"{expected} in {pdf_path.name}"
        )


def _extract_expected_date_from_footer(text: str, expected_date: str) -> str:
    """Accept the source date only when OCR visibly found it in the footer crop.

    Panel orders use a Presiding Judge signature block instead of a Chief Clerk
    block. The cropped footer may contain only the printed date and a "Date"
    label, so the already-known live-site release date is used strictly as a
    selector, never as a substitute for missing OCR evidence.
    """

    expected = _normalize_expected_date(expected_date)
    if not expected:
        return ""
    for line in text.splitlines():
        if _extract_date_from_line(line) == expected:
            return expected
    return ""


def _normalize_expected_date(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    if re.fullmatch(r"\d{8}", cleaned):
        return cleaned
    for date_format in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.datetime.strptime(cleaned, date_format).strftime("%m%d%Y")
        except ValueError:
            continue
    raise NamingError(f"Unrecognized source release date: {value!r}")


def _extract_date_from_line(line: str) -> str:
    for candidate in (line, _decode_shifted_pdf_text(line)):
        found = _MIAP_DATE_PATTERN.search(candidate)
        if found:
            normalized = _normalize_date(found.group(0))
            if normalized:
                return normalized
    return ""


def _normalize_date(raw_date: str) -> str:
    cleaned = re.sub(r"\s+", " ", raw_date.strip().rstrip(".,"))
    cleaned = re.sub(
        r"(,\s*)([\d\s]{4,})$",
        lambda match: match.group(1) + re.sub(r"\s+", "", match.group(2)),
        cleaned,
    )
    for date_format in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.datetime.strptime(cleaned, date_format).strftime("%m%d%Y")
        except ValueError:
            continue
    return ""


def _decode_shifted_pdf_text(text: str) -> str:
    if not any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        return text
    return "".join(
        chr(ord(char) + 29)
        if char not in "\t\n\r" and 0 <= ord(char) <= 97
        else char
        for char in text
    )


def _normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.upper()).strip()


def extract_pdf_text(
    pdf_path: Path,
    max_pages: int = 2,
    logger=None,
    cancel_event=None,
) -> str:
    try:
        from pypdf import PdfReader

        raise_if_cancelled(cancel_event)
        # Passing a path directly lets PdfReader own a lazy file stream whose
        # lifetime can extend until garbage collection. On Windows that can
        # briefly lock the downloaded PDF and make the immediate FileFlex
        # rename fail with WinError 32. Own the stream here and close it
        # deterministically before returning to the collector.
        with pdf_path.open("rb") as pdf_stream:
            reader = PdfReader(pdf_stream)
            texts: list[str] = []
            for page in list(reader.pages)[:max_pages]:
                raise_if_cancelled(
                    cancel_event,
                    "Collection stopped while reading PDF text",
                )
                texts.append(page.extract_text() or "")
            return "\n".join(texts)
    except CollectionCancelled:
        raise
    except Exception as exc:
        _log(logger, "warning", f"Unable to extract PDF text from {pdf_path.name}: {exc}")
        return ""


def extract_pdf_text_with_ocr(
    pdf_path: Path,
    max_pages: int = 2,
    logger=None,
    cancel_event=None,
) -> str:
    tesseract = _find_tesseract()
    if not tesseract:
        _log(logger, "warning", "OCR unavailable: Tesseract is not installed or on PATH")
        return ""
    try:
        import fitz
        from PIL import Image

        tessdata = Path(tesseract).resolve().parent / "tessdata"
        if tessdata.is_dir():
            os.environ["TESSDATA_PREFIX"] = str(tessdata)
        texts: list[str] = []
        with fitz.open(str(pdf_path)) as document:
            page_limit = min(max_pages, document.page_count)
            for page_index in range(page_limit):
                raise_if_cancelled(cancel_event, "Collection stopped during PDF OCR")
                _log(logger, "info", f"OCR page {page_index + 1}/{page_limit}: {pdf_path.name}")
                pixmap = document.load_page(page_index).get_pixmap(
                    matrix=fitz.Matrix(2, 2), alpha=False
                )
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                texts.append(_ocr_image(image, tesseract, cancel_event))
        return "\n".join(texts)
    except CollectionCancelled:
        raise
    except Exception as exc:
        _log(logger, "warning", f"OCR failed for {pdf_path.name}: {exc}")
        return ""


def extract_pdf_footer_text_with_ocr(
    pdf_path: Path,
    max_pages: int = 2,
    logger=None,
    cancel_event=None,
) -> str:
    """OCR the lower portion of the final pages containing certification."""

    tesseract = _find_tesseract()
    if not tesseract:
        _log(logger, "warning", "OCR unavailable: Tesseract is not installed or on PATH")
        return ""
    try:
        import fitz
        from PIL import Image

        tessdata = Path(tesseract).resolve().parent / "tessdata"
        if tessdata.is_dir():
            os.environ["TESSDATA_PREFIX"] = str(tessdata)
        texts: list[str] = []
        with fitz.open(str(pdf_path)) as document:
            page_limit = min(max_pages, document.page_count)
            page_indexes = range(
                document.page_count - 1,
                document.page_count - page_limit - 1,
                -1,
            )
            for ordinal, page_index in enumerate(page_indexes, 1):
                raise_if_cancelled(cancel_event, "Collection stopped during footer OCR")
                _log(
                    logger,
                    "info",
                    f"OCR footer page {ordinal}/{page_limit}: {pdf_path.name}",
                )
                page = document.load_page(page_index)
                clip = fitz.Rect(
                    page.rect.x0,
                    page.rect.y0 + page.rect.height * 0.55,
                    page.rect.x1,
                    page.rect.y1,
                )
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(2, 2), clip=clip, alpha=False
                )
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                texts.append(_ocr_image(image, tesseract, cancel_event))
        return "\n".join(texts)
    except CollectionCancelled:
        raise
    except Exception as exc:
        _log(logger, "warning", f"Footer OCR failed for {pdf_path.name}: {exc}")
        return ""


def _ocr_image(image, tesseract: str, cancel_event=None) -> str:
    """Run one Tesseract process that can be terminated by the Stop button."""

    raise_if_cancelled(cancel_event, "Collection stopped before OCR")
    handle, image_name = tempfile.mkstemp(prefix="miap00_ocr_", suffix=".png")
    os.close(handle)
    image_path = Path(image_name)
    process = None
    try:
        image.save(image_path, format="PNG")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [tesseract, str(image_path), "stdout", "-l", "eng"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
            env=os.environ.copy(),
        )
        while True:
            raise_if_cancelled(cancel_event, "Collection stopped during OCR")
            try:
                output, errors = process.communicate(timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            details = " ".join((errors or "").split())
            raise RuntimeError(
                f"Tesseract exited with code {process.returncode}: {details}"
            )
        return output or ""
    except CollectionCancelled:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        raise
    finally:
        image_path.unlink(missing_ok=True)


def _find_tesseract() -> str:
    candidates = (
        os.environ.get("MIAP00_TESSERACT_PATH"),
        os.environ.get("FILEFLEX_TESSERACT_PATH"),
        os.environ.get("TESSERACT_CMD"),
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _log(logger, method: str, message: str) -> None:
    if logger and hasattr(logger, method):
        getattr(logger, method)(message)
