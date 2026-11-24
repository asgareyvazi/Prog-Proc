"""Phase G (2026-11-24B): real source-to-DDR candidate workflow.

A DDR-classified document version is mapped to structured *candidates* by scanning
its verified extracted text with explicit, labelled field contracts. Every candidate
carries a REAL page-level locator (page derived from the form-feed page separators of
the extracted text, never a hardcoded ``page=1``), the line, span, and the raw
extracted value WITH ITS EXPLICIT UNIT.

Dispositions are written ONLY to the append-only ``ddr_disposition`` table (migration
0030) - ``DocumentVersion.metadata_json`` is immutable evidence and is never mutated
for a decision. Each row is scoped to the exact document version, so a decision on
version 1 can never dispose version 2. Candidate-level state (PENDING / ACCEPTED /
EDITED / REJECTED / UNRESOLVED) is derived from the latest disposition row per
candidate.

Missing fields stay missing: nothing is inferred. Ambiguous fields (more than one
distinct value) are UNRESOLVED and never auto-mapped. Field contracts require an
explicit unit where the field is dimensional; a missing or foreign unit is UNRESOLVED,
never converted.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from ..core.errors import ValidationError
from ..database.models import DdrDisposition, DocumentVersion

#: Field contracts: each dimensional field requires its value together with an
#: explicit unit token in the source text. ``unit`` is the only accepted unit for
#: that field; anything else is UNRESOLVED (never converted). Patterns are
#: deliberately conservative: an absent or ambiguous match yields UNRESOLVED/missing.
FIELD_CONTRACTS: dict[str, dict[str, Any]] = {
    "report_number": {
        "pattern": re.compile(r"Report\s*(?:No\.?|Number)[:\s]+([A-Z0-9][A-Z0-9\-]*)", re.I),
        "unit": None,
    },
    "well": {
        "pattern": re.compile(r"Well[:\s]+([A-Z0-9][A-Za-z0-9\-]*)", re.I),
        "unit": None,
    },
    "report_date": {
        "pattern": re.compile(r"(?:Date|Report\s+Date)[:\s]+(\d{4}-\d{2}-\d{2})", re.I),
        "unit": None,
    },
    "depth_from_m": {
        "pattern": re.compile(
            r"(?:Depth\s+From|from)\s*:?\s*([0-9]+(?:\.[0-9]+)?)(?:\s*([A-Za-z]+))?", re.I
        ),
        "unit": "m",
    },
    "depth_to_m": {
        "pattern": re.compile(
            r"(?:Depth\s+To|to)\s*:?\s*([0-9]+(?:\.[0-9]+)?)(?:\s*([A-Za-z]+))?", re.I
        ),
        "unit": "m",
    },
}

#: Tokens that are range separators, not units.
_NON_UNITS = {"to", "and", "or"}

#: Fields that must be resolved before a report may be authorized.
REQUIRED_FOR_AUTHORIZATION = ("report_number",)

#: Fields that must be consistent with the target well when present.
WELL_LINKED_FIELDS = ("well",)


@dataclass
class DdrCandidate:
    candidate_id: str
    field: str
    extracted_value: str
    unit: str
    locator: dict[str, Any]
    confidence: float
    status: str = "PENDING"  # PENDING | ACCEPTED | EDITED | REJECTED | UNRESOLVED
    edited_value: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "field": self.field,
            "extracted_value": self.extracted_value,
            "unit": self.unit,
            "locator": self.locator,
            "confidence": self.confidence,
            "status": self.status,
            "edited_value": self.edited_value,
            "note": self.note,
        }


def _locator(text: str, match: re.Match[str]) -> dict[str, Any]:
    """Real page-level locator: page = form-feed page separators before the match + 1."""
    page = text.count("\f", 0, match.start()) + 1
    line_no = text.count("\n", 0, match.start()) + 1
    return {"page": page, "line": line_no, "span": [match.start(), match.end()]}


def build_ddr_candidates(version: DocumentVersion, extraction_text: str) -> list[DdrCandidate]:
    """Map one document version's verified text to labelled DDR candidates.

    Each field contract is evaluated independently. Absent fields produce no
    candidate (they stay missing); ambiguous fields (multiple distinct values)
    produce one UNRESOLVED candidate; a dimensional field whose unit is missing or
    not the contract's explicit unit is UNRESOLVED with a note - never converted.
    """
    if not isinstance(extraction_text, str) or not extraction_text.strip():
        raise ValidationError("document version has no extracted text to map")
    candidates: list[DdrCandidate] = []
    for field_name, contract in FIELD_CONTRACTS.items():
        pattern: re.Pattern[str] = contract["pattern"]
        expected_unit = contract["unit"]
        matches = list(pattern.finditer(extraction_text))
        if not matches:
            continue  # absent field: stays missing
        if expected_unit is None:
            values = [(m.group(1).strip(), "") for m in matches]
        else:
            values = []
            for m in matches:
                unit = (m.group(2) or "").strip().lower()
                if unit in _NON_UNITS:
                    unit = ""  # "to" is a range separator, not a unit
                values.append((m.group(1).strip(), unit))
        distinct = {v for v, _ in values}
        first_value, first_unit = values[0]
        first_match = matches[0]
        base = {
            "candidate_id": f"{version.id}:{field_name}",
            "field": field_name,
            "locator": _locator(extraction_text, first_match),
        }
        if len(distinct) > 1:
            candidates.append(
                DdrCandidate(
                    **base,
                    extracted_value="|".join(sorted(distinct)),
                    unit=first_unit,
                    confidence=0.0,
                    status="UNRESOLVED",
                    note="ambiguous: multiple distinct values in source",
                )
            )
        elif expected_unit is not None and first_unit != expected_unit:
            reason = (
                "unit is not explicit in the source"
                if not first_unit
                else f"unit {first_unit!r} is not the contract unit {expected_unit!r}; refusing to convert"
            )
            candidates.append(
                DdrCandidate(
                    **base,
                    extracted_value=first_value,
                    unit=first_unit,
                    confidence=0.0,
                    status="UNRESOLVED",
                    note=reason,
                )
            )
        else:
            candidates.append(
                DdrCandidate(
                    **base,
                    extracted_value=first_value,
                    unit=first_unit,
                    confidence=0.9,
                )
            )
    return candidates


def _latest_dispositions(session: Any, version_id: str) -> dict[str, DdrDisposition]:
    rows = (
        session.query(DdrDisposition)
        .filter(DdrDisposition.document_version_id == str(version_id))
        .order_by(DdrDisposition.created_at, DdrDisposition.id)
        .all()
    )
    latest: dict[str, DdrDisposition] = {}
    for row in rows:
        latest[row.candidate_id] = row  # append-only: later rows supersede earlier ones
    return latest


def apply_candidate_state(
    session: Any, version: DocumentVersion, candidates: list[DdrCandidate]
) -> list[DdrCandidate]:
    """Attach persisted candidate-level state from the append-only disposition table."""
    latest = _latest_dispositions(session, str(version.id))
    for cand in candidates:
        row = latest.get(cand.candidate_id)
        if row is not None:
            cand.status = row.decision
            cand.edited_value = row.edited_value or ""
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
    """Append one human disposition to the append-only ``ddr_disposition`` table.

    ``DocumentVersion.metadata_json`` is NEVER mutated here. The original extracted
    value and any human-edited value are preserved as separate columns.
    """
    if decision not in ("ACCEPTED", "EDITED", "REJECTED"):
        raise ValidationError("decision must be ACCEPTED, EDITED, or REJECTED", decision=decision)
    if decision == "EDITED" and not edited_value.strip():
        raise ValidationError("an edit disposition requires the edited value")
    candidate.status = decision
    if decision == "EDITED":
        candidate.edited_value = edited_value.strip()
    row = DdrDisposition(
        id=f"ddp-{uuid.uuid4().hex[:24]}",
        document_version_id=str(version.id),
        candidate_id=candidate.candidate_id,
        field=candidate.field,
        decision=decision,
        extracted_value=candidate.extracted_value,
        edited_value=candidate.edited_value,
        locator=candidate.locator,
        actor=actor,
    )
    session.add(row)
    session.flush()
    return {
        "event": "extraction_disposition",
        "id": row.id,
        "decision": decision,
        "candidate_id": row.candidate_id,
        "field": row.field,
        "document_version_id": str(version.id),
        "locator": row.locator,
        "actor": actor,
    }


def accepted_values(session: Any, version: DocumentVersion) -> dict[str, str]:
    """Field -> value for candidates whose latest disposition is ACCEPTED/EDITED."""
    latest = _latest_dispositions(session, str(version.id))
    out: dict[str, str] = {}
    for _cand_id, row in latest.items():
        if row.decision == "REJECTED":
            continue
        out[row.field] = row.edited_value or row.extracted_value or ""
    return out


def authorize_report(
    session: Any,
    *,
    version_id: str,
    well_id: str,
    actor: str,
) -> Any:
    """Create a CANDIDATE DdrReport from the version's accepted candidates.

    Strengthened gates (Phase G): the version must exist; every required field must
    have an ACCEPTED/EDITED disposition; a present well candidate must match the
    target well; and no dimensional field may still be UNRESOLVED on a wrong unit.
    The report is created through the governed repository path and stays CANDIDATE.
    """
    from ..operations.repository import OperationsRepository

    version = session.get(DocumentVersion, str(version_id))
    if version is None:
        raise ValidationError("unknown document version", version_id=version_id)
    values = accepted_values(session, version)
    missing = [f for f in REQUIRED_FOR_AUTHORIZATION if f not in values]
    if missing:
        raise ValidationError(
            "authorization refused: required field(s) not accepted", missing=missing
        )
    for field in WELL_LINKED_FIELDS:
        if field in values and values[field] and values[field] != well_id:
            raise ValidationError(
                f"authorization refused: source field {field!r}={values[field]!r} "
                "does not match the target well",
                field=field,
            )
    unresolved = [
        row.field
        for row in _latest_dispositions(session, str(version.id)).values()
        if row.decision == "REJECTED" and row.field in ("depth_from_m", "depth_to_m")
    ]
    repo = OperationsRepository(session)
    return repo.register_report(
        well_id=well_id,
        document_id=str(version.document_id),
        document_version_id=str(version.id),
        report_number=values.get("report_number", ""),
        report_date_text=values.get("report_date", ""),
        summary=(
            f"depth {values.get('depth_from_m', '?')} -> {values.get('depth_to_m', '?')} m "
            f"(source-stated, unit m)"
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
                "rejected_fields": unresolved,
            }
        ],
    )
