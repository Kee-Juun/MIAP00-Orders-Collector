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
    related_dockets: list[str] = field(default_factory=list)
    duplicate_parent_filename: str = ""
    counsel_references: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["irt_evidence"] = self.irt_evidence
        return data


@dataclass
class CounselRecord:
    docket: str
    status: str
    target_filename: str = ""
    lnis: list[str] = field(default_factory=list)
    irt_evidence: list[dict[str, Any]] = field(default_factory=list)
    case_url: str = ""
    reason: str = ""

    def reference(self, *, include_docket: bool = True) -> str:
        if self.status == "irt_existing":
            values = "; ".join(self.lnis)
        elif self.status == "collected":
            values = self.target_filename
        else:
            return ""
        if not values:
            return ""
        return f"{self.docket}: {values}" if include_docket else values

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
