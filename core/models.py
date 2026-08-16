"""Shared collector domain models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class OrderResult:
    page: int
    position: int
    docket: str
    title: str
    lower_court: str
    release_date: str
    order_type: str
    pdf_url: str
    original_filename: str
    case_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProcessingRecord:
    status: str
    docket: str
    title: str
    release_date: str
    source_filename: str
    source_url: str
    target_filename: str = ""
    document_date: str = ""
    lower_court: str = ""
    page: int = 0
    reason: str = ""
    irt_evidence: list[dict[str, Any]] = field(default_factory=list)
    bytes: int = 0
    sha256: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["irt_evidence"] = self.irt_evidence
        return data
