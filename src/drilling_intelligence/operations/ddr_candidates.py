"""Phase F (D-20 reopened): real source-to-DDR candidate workflow.

A DDR-classified document version is mapped to structured *candidates* by scanning
its verified extracted text with explicit, labelled rules. Every candidate carries
its source locator (page/line) and the raw extracted value. An operator then
accepts, edits, or rejects each candidate; the original extracted value and any
human-edited value are preserved as separate evidence. Only accepted candidates
feed a ``DdrReport`` created through the governed repository path, and the report
stays CANDIDATE until the established review lifecycle changes it.

Missing fields stay missing: nothing is inferred (no dates, depths, durations,
units, NPT causes, or activity parameters are invented). Ambiguous fields (more
than one distinct value) are returned UNRESOLVED and never auto-mapped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..core.errors import ValidationError
from ..database.models import DocumentVersion
from ..engineering.source_to_draft import disposition_event

#: Field rules applied to verified extracted text. Each rule is labelled so a
#: disposition can state why a value was mapped. Patterns are deliberately
#: conservative: an absent or ambiguous match yields UNRESOLVED, never a guess.
FIELD_RULES: dict[str, re.Pattern[str]] = {
    "report_number": re.compile(r"Report\s*(?:No\.?|Number)[:\s]+([A-Z0-9][A-Z0-9\-]*)", re.I),
    "well": re.compile(r"Well[:\s]+([A-Z0-9][A-Za-z0-9\-]*)", re.I),
    "report_date": re.compile(r"(?:Date|Report\s+Date)[:\s]+(\d{4}-\d{2}-\d{2})", re.I),
    "depth_from_m": re.compile(r"(?:Depth\s+From|from)\s*:?\s*([0-9]+(?:\.[0-9]+)?)", re.I),
    "depth_to_m": re.compile(r"(?:Depth\s+To|to)\s*:?\s*([0-9]+(?:\.[0-9]+)?)", re.I),
}


@dataclass
class DdrCandidate:
    candidate_id: str
    field: str
    extracted_value: str
    locator: dict[str, Any]
    confidence: float
    status: str = "PENDING"  # PENDING | ACCEPTED | EDITED | REJECTED | UNRESOLVED
    edited_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "field": self.field,
            "extracted_value": self.extracted_value,
            "locator": self.locator,
            "confidence": self.confidence,
            "status": self.status,
            "edited_value": self.edited_value,
        }


def _line_locator(text: str, match: re.Match[str]) -> dict[str, Any]:
    line_no = text.count("\n", 0, match.start()) + 1
    return {"page": 1, "line": line_no, "span": [match.start(), match.end()]}


def build_ddr_candidates(version: DocumentVersion, extraction_text: str) -> list[DdrCandidate]:
    """Map one document version's verified text to labelled DDR candidates.

    Ambiguous fields (multiple distinct values) are returned once with status
    UNRESOLVED and no mapped value; absent fields produce no candidate at all.
    """
    if not isinstance(extraction_text, str) or not extraction_text.strip():
        raise ValidationError("document version has no extracted text to map")
    candidates: list[DdrCandidate] = []
    for field_name, pattern in FIELD_RULES.items():
        values = [(m.group(1).strip(), m) for m in pattern.finditer(extraction_text)]
        distinct = {v for v, _ in values}
        if not values:
            continue  # absent field: stays missing
        first_value, first_match = values[0]
        if len(distinct) > 1:
            candidates.append(
                DdrCandidate(
                    candidate_id=f"{version.id}:{field_name}",
                    field=field_name,
                    extracted_value="|".join(sorted(distinct)),
                    locator=_line_locator(extraction_text, first_match),
                    confidence=0.0,
                    status="UNRESOLVED",
                )
            )
        else:
            candidates.append(
                DdrCandidate(
                    candidate_id=f"{version.id}:{field_name}",
                    field=field_name,
                    extracted_value=first_value,
                    locator=_line_locator(extraction_text, first_match),
                    confidence=0.9,
                )
            )
    return candidates


def record_disposition(
    session: Any,
    version: DocumentVersion,
    candidate: DdrCandidate,
    decision: str,
    *,
    actor: str,
    edited_value: str = "",
) -> dict[str, Any]:
    """Record one human disposition as a canonical extraction_disposition event.

    The event lives in the version's provenance-bearing metadata so the exact
    current-version review queue (D-17) sees it; the original extracted value is
    never overwritten by an edit.
    """
    if decision not in ("ACCEPTED", "EDITED", "REJECTED"):
        raise ValidationError("decision must be ACCEPTED, EDITED, or REJECTED", decision=decision)
    if decision == "EDITED" and not edited_value.strip():
        raise ValidationError("an edit disposition requires the edited value")
    candidate.status = decision
    if decision == "EDITED":
        candidate.edited_value = edited_value.strip()
    meta = dict(version.metadata_json or {})
    events = list(meta.get("extraction_dispositions") or [])
    event = disposition_event(
        decision,
        by=actor,
        candidate_id=candidate.candidate_id,
        document_id=str(version.document_id),
        version_id=str(version.id),
        sha256=str(version.sha256),
        locator=candidate.locator,
        edited=decision == "EDITED",
    )
    event["field"] = candidate.field
    event["extracted_value"] = candidate.extracted_value
    event["edited_value"] = candidate.edited_value
    events.append(event)
    meta["extraction_dispositions"] = events
    version.metadata_json = meta
    session.flush()
    return event


def accepted_values(version: DocumentVersion) -> dict[str, str]:
    """Field -> value for candidates whose latest disposition is accept/edit."""
    meta = version.metadata_json or {}
    latest: dict[str, dict[str, Any]] = {}
    for event in meta.get("extraction_dispositions") or []:
        if event.get("locator", {}).get("document_version_id") not in (None, str(version.id)):
            continue
        latest[event.get("field", "")] = event
    out: dict[str, str] = {}
    for field_name, event in latest.items():
        if event.get("decision") == "REJECTED":
            continue
        out[field_name] = event.get("edited_value") or event.get("extracted_value") or ""
    return out


def authorize_report(
    session: Any,
    *,
    version_id: str,
    well_id: str,
    actor: str,
) -> Any:
    """Create a CANDIDATE DdrReport from the version's accepted candidates.

    Only fields with an accept/edit disposition are mapped. The report is created
    through the governed repository path with full provenance and stays CANDIDATE.
    """
    from ..operations.repository import OperationsRepository

    version = session.get(DocumentVersion, str(version_id))
    if version is None:
        raise ValidationError("unknown document version", version_id=version_id)
    values = accepted_values(version)
    repo = OperationsRepository(session)
    return repo.register_report(
        well_id=well_id,
        document_id=str(version.document_id),
        document_version_id=str(version.id),
        report_number=values.get("report_number", ""),
        report_date_text=values.get("report_date", ""),
        summary=(
            f"depth {values.get('depth_from_m', '?')} -> {values.get('depth_to_m', '?')} m "
            f"(source-stated)"
            if values.get("depth_from_m") or values.get("depth_to_m")
            else ""
        ),
        status="CANDIDATE",
        origin="DERIVED",
        created_by=actor,
        provenance=[
            {
                "event": "source_to_ddr_candidate",
                "document_version_id": str(version.id),
                "sha256": str(version.sha256),
                "fields": values,
                "unresolved": [
                    c.field for c in (version.metadata_json or {}).get("unresolved_fields", [])
                ],
            }
        ],
    )
