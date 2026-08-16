"""Post-collection duplicate detection based on PDF content."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
from pathlib import Path
import re

from .models import ProcessingRecord
from .cancellation import CollectionCancelled, raise_if_cancelled


_DOCKET_NUMBER_PATTERN = re.compile(r"\b\d{5,7}\b")
_DOCKET_REFERENCE_PATTERN = re.compile(
    r"\bdocket(?:\s+no\.?|\s+numbers?)?\s*[:#]?\s*"
    r"(?P<numbers>\d{5,7}(?:\s*(?:,|;|and|&)\s*\d{5,7})*)",
    re.IGNORECASE,
)
_SPACE_PATTERN = re.compile(r"\s+")
_CONSOLIDATED_SIMILARITY = 0.97


@dataclass
class _Profile:
    record: ProcessingRecord
    path: Path
    text: str
    comparable_text: str
    binary_digest: str
    text_digest: str
    visual_digest: str
    dockets: set[str]


@dataclass(frozen=True)
class IRTBackedConsolidatedMatch:
    parent_filename: str
    shared_dockets: tuple[str, ...]
    similarity: float
    irt_evidence: list[dict]


def find_irt_backed_consolidated_duplicates(
    candidates: list[tuple[ProcessingRecord, Path]],
    exact_irt_matches: dict[str, list[dict]],
    logger,
    cancel_event=None,
) -> dict[str, IRTBackedConsolidatedMatch]:
    """Match pending consolidated copies to a PDF already represented in IRT.

    The exact IRT match supplies the online-parent evidence. Both temporary PDFs
    must also share consolidated appellate docket numbers and have at least 97%
    normalized content similarity. This prevents a mere docket mention from
    excluding a distinct order.
    """

    if len(candidates) < 2 or not exact_irt_matches:
        return {}
    logger.info(
        "IRT-backed consolidated check: comparing %d renamed PDF(s)",
        len(candidates),
    )
    profiles = []
    for record, path in candidates:
        raise_if_cancelled(
            cancel_event,
            "Collection stopped during consolidated duplicate analysis",
        )
        if path.is_file():
            profiles.append(_build_profile(record, path, logger, cancel_event))
    parents = [
        profile
        for profile in profiles
        if exact_irt_matches.get(profile.record.target_filename)
    ]
    matches: dict[str, IRTBackedConsolidatedMatch] = {}
    for profile in profiles:
        raise_if_cancelled(cancel_event)
        filename = profile.record.target_filename
        if exact_irt_matches.get(filename):
            continue
        for parent in parents:
            raise_if_cancelled(cancel_event)
            similarity = _consolidated_similarity(parent, profile)
            if similarity is None:
                continue
            shared = tuple(sorted(parent.dockets & profile.dockets))
            matches[filename] = IRTBackedConsolidatedMatch(
                parent_filename=parent.record.target_filename,
                shared_dockets=shared,
                similarity=similarity,
                irt_evidence=list(
                    exact_irt_matches[parent.record.target_filename]
                ),
            )
            logger.info(
                "IRT-backed consolidated copy found: %s (online parent %s; "
                "shared dockets %s; similarity %.1f%%)",
                filename,
                parent.record.target_filename,
                ", ".join(shared),
                similarity * 100,
            )
            break
    return matches


def remove_content_duplicates(
    records: list[ProcessingRecord], run_dir: Path, logger, cancel_event=None
) -> int:
    """Remove confidently matched duplicates and update their processing records.

    Records are considered in collection order, so the first finalized file is
    canonical. FileFlex assigns that file the unsuffixed name for same-docket
    siblings; later matches (``a``, ``b``, and so on) are removed.
    """

    candidates = [record for record in records if record.status == "collected"]
    if len(candidates) < 2:
        return 0

    logger.info(
        "Post-run content duplicate check: comparing %d collected PDF(s)",
        len(candidates),
    )
    profiles: list[_Profile] = []
    for record in candidates:
        raise_if_cancelled(
            cancel_event,
            "Collection stopped during post-run duplicate analysis",
        )
        path = run_dir / record.target_filename
        if not path.is_file():
            logger.warning(
                "Content duplicate check skipped missing file: %s",
                record.target_filename,
            )
            continue
        profiles.append(_build_profile(record, path, logger, cancel_event))

    kept: list[_Profile] = []
    exact_binary: dict[str, _Profile] = {}
    exact_text: dict[str, _Profile] = {}
    exact_visual: dict[str, _Profile] = {}
    removed = 0
    for profile in profiles:
        raise_if_cancelled(cancel_event)
        match: _Profile | None = None
        method = ""
        similarity: float | None = None

        if profile.binary_digest:
            match = exact_binary.get(profile.binary_digest)
            if match:
                method = "identical PDF bytes"
        if not match and profile.text_digest:
            match = exact_text.get(profile.text_digest)
            if match:
                method = "identical normalized PDF text"
        if not match and profile.visual_digest:
            match = exact_visual.get(profile.visual_digest)
            if match:
                method = "identical rendered PDF content"
        if not match:
            for prior in kept:
                raise_if_cancelled(cancel_event)
                similarity = _consolidated_similarity(prior, profile)
                if similarity is not None:
                    match = prior
                    method = f"consolidated-case content match ({similarity:.1%})"
                    break

        if match:
            try:
                profile.path.unlink()
            except OSError as exc:
                logger.error(
                    "Unable to remove confirmed content duplicate %s: %s",
                    profile.record.target_filename,
                    exc,
                )
                kept.append(profile)
                _index_profile(profile, exact_binary, exact_text, exact_visual)
                continue
            profile.record.status = "content_duplicate"
            profile.record.reason = (
                f"Removed after post-run quality check; kept "
                f"{match.record.target_filename}; {method}"
            )
            logger.info(
                "Content duplicate removed: %s (kept %s; %s)",
                profile.record.target_filename,
                match.record.target_filename,
                method,
            )
            removed += 1
            continue

        kept.append(profile)
        _index_profile(profile, exact_binary, exact_text, exact_visual)

    logger.info(
        "Post-run content duplicate check complete: kept=%d removed=%d",
        len(kept),
        removed,
    )
    return removed


def _build_profile(record: ProcessingRecord, path: Path, logger, cancel_event=None) -> _Profile:
    raise_if_cancelled(cancel_event)
    raw_text = (
        _read_pdf_text(path, logger)
        if cancel_event is None
        else _read_pdf_text(path, logger, cancel_event)
    )
    text = _normalize_text(raw_text)
    dockets = _extract_dockets(text, record.docket)
    comparable = text
    for docket in sorted(dockets, key=len, reverse=True):
        comparable = re.sub(rf"\b{re.escape(docket)}\b", "<docket>", comparable)
    text_digest = _digest(text.encode("utf-8")) if text else ""
    # Raster comparison is primarily a fallback for image-only PDFs. Avoiding
    # it for text PDFs keeps the quality pass fast for normal court orders.
    visual_digest = (
        ""
        if text
        else _rendered_pdf_digest(path, logger)
        if cancel_event is None
        else _rendered_pdf_digest(path, logger, cancel_event)
    )
    return _Profile(
        record=record,
        path=path,
        text=text,
        comparable_text=comparable,
        binary_digest=record.sha256,
        text_digest=text_digest,
        visual_digest=visual_digest,
        dockets=dockets,
    )


def _index_profile(
    profile: _Profile,
    exact_binary: dict[str, _Profile],
    exact_text: dict[str, _Profile],
    exact_visual: dict[str, _Profile],
) -> None:
    if profile.binary_digest:
        exact_binary.setdefault(profile.binary_digest, profile)
    if profile.text_digest:
        exact_text.setdefault(profile.text_digest, profile)
    if profile.visual_digest:
        exact_visual.setdefault(profile.visual_digest, profile)


def _consolidated_similarity(left: _Profile, right: _Profile) -> float | None:
    if not left.comparable_text or not right.comparable_text:
        return None
    if left.record.document_date != right.record.document_date:
        return None
    shared_dockets = left.dockets & right.dockets
    if not shared_dockets:
        return None
    # A consolidated order must identify more than one appellate docket in at
    # least one copy. This avoids merging two distinct orders for one case/date.
    if len(left.dockets) < 2 and len(right.dockets) < 2:
        return None
    ratio = SequenceMatcher(
        None, left.comparable_text, right.comparable_text, autojunk=False
    ).ratio()
    return ratio if ratio >= _CONSOLIDATED_SIMILARITY else None


def _normalize_text(text: str) -> str:
    return _SPACE_PATTERN.sub(" ", text).strip().casefold()


def _extract_dockets(text: str, primary_docket: str) -> set[str]:
    dockets = {primary_docket} if primary_docket else set()
    for match in _DOCKET_REFERENCE_PATTERN.finditer(text):
        dockets.update(_DOCKET_NUMBER_PATTERN.findall(match.group("numbers")))
    return dockets


def _read_pdf_text(path: Path, logger, cancel_event=None) -> str:
    try:
        from pypdf import PdfReader

        raise_if_cancelled(cancel_event)
        reader = PdfReader(str(path))
        text: list[str] = []
        for page in reader.pages:
            raise_if_cancelled(cancel_event)
            text.append(page.extract_text() or "")
        return "\n".join(text)
    except CollectionCancelled:
        raise
    except Exception as exc:
        logger.warning("Unable to read PDF content from %s: %s", path.name, exc)
        return ""


def _rendered_pdf_digest(path: Path, logger, cancel_event=None) -> str:
    try:
        import fitz

        digest = hashlib.sha256()
        with fitz.open(str(path)) as document:
            for page in document:
                raise_if_cancelled(cancel_event)
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(1, 1), colorspace=fitz.csGRAY, alpha=False
                )
                digest.update(f"{pixmap.width}x{pixmap.height}|".encode("ascii"))
                digest.update(pixmap.samples)
        return digest.hexdigest()
    except CollectionCancelled:
        raise
    except Exception as exc:
        logger.warning("Unable to render PDF content from %s: %s", path.name, exc)
        return ""


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
