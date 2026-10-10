"""The engineering-report contract: plain values, stable identity, no sessions.

``engineering-report/1`` is a presentation document composed from a certified
decision pack or comparison pack. It is not a persisted domain record and it
does not recalculate the numbers it displays.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.hashing import sha256_obj

REPORT_SCHEMA = "engineering-report/1"
#: Bumped when HTML/SVG bytes change for the same pack. Not part of report identity.
RENDERER_VERSION = "engineering-report-html/1"

MODE_SINGLE_WELL = "single_well"
MODE_EXPLICIT_WELL_SET = "explicit_well_set"
MODE_NAMED_OFFSETS = "named_offsets"
MODE_DISCOVERED_OFFSETS = "discovered_offsets"
REPORT_MODES = (
    MODE_SINGLE_WELL,
    MODE_EXPLICIT_WELL_SET,
    MODE_NAMED_OFFSETS,
    MODE_DISCOVERED_OFFSETS,
)

SECTION_PRESENT = "PRESENT"
SECTION_UNSUPPORTED = "UNSUPPORTED"
SECTION_NOT_APPLICABLE = "NOT_APPLICABLE"
SECTION_NO_DATA = "NO_DATA"

EXHIBIT_RENDERED = "RENDERED"
EXHIBIT_NO_DATA = "NO_DATA"
EXHIBIT_MISSING = "MISSING"
EXHIBIT_INCOMPARABLE = "INCOMPARABLE"
EXHIBIT_UNRESOLVED = "UNRESOLVED"
EXHIBIT_STALE = "STALE"
EXHIBIT_NOT_APPLICABLE = "NOT_APPLICABLE"
EXHIBIT_UNSUPPORTED = "UNSUPPORTED"
EXHIBIT_STATES = (
    EXHIBIT_RENDERED,
    EXHIBIT_NO_DATA,
    EXHIBIT_MISSING,
    EXHIBIT_INCOMPARABLE,
    EXHIBIT_UNRESOLVED,
    EXHIBIT_STALE,
    EXHIBIT_NOT_APPLICABLE,
    EXHIBIT_UNSUPPORTED,
)

# Standing disclosures. They are true of this release, not inferred from a row.
STANDING_LIMITATIONS = (
    "logging_not_ready",
    "service_report_not_ready",
    "depth_series_unsupported",
    "timeline_not_in_source_pack",
)

LIMITATION_TEXT = {
    "logging_not_ready": (
        "LOGGING remains NOT READY. This report does not guess a log curve from figure metadata."
    ),
    "service_report_not_ready": (
        "SERVICE_REPORT remains NOT READY. This report does not invent a service-report shape."
    ),
    "depth_series_unsupported": (
        "No point-level depth series is in the certified packs. Figure records carry metadata, "
        "not portable image bytes or curve values. Endpoints are not plotted as a log."
    ),
    "timeline_not_in_source_pack": (
        "The certified packs do not carry a point-level timeline. This report does not issue "
        "a second timeline query."
    ),
    "incomparable_units": "Source values use different units and are not converted.",
    "mixed_currency": (
        "More than one currency is present. Totals are not summed or converted across currencies."
    ),
    "mixed_unit_line": "A cost line states more than one unit. The line is not converted.",
    "missing_metric": "At least one metric is stated on some subjects only.",
    "unresolved_conflict": "An open knowledge conflict is preserved and not resolved here.",
    "insufficient_shared_basis": (
        "The selected wells do not share a recorded problem type or hole size."
    ),
    "truncated_detail": (
        "A declared cap shortened a profile, evidence sample, or detail payload. "
        "The source pack reports the cap."
    ),
    "unassessed_risk": "A risk severity or scale identity is unstated. Bands are not scored.",
    "stale_pattern": "A pattern is past its recorded stale_at marker.",
    "stale_dependency": "A stored calculation dependency is stale. The calculation is not executed.",
    "unresolved_dependency": "A stored calculation dependency is unresolved. It is not assumed current.",
    "dependency_subjects_truncated": "Calculation dependency subjects exceeded the source cap.",
    "evidence_truncated": "An evidence sample hit its declared limit. The omitted ids are not invented.",
    "site_scoped_hse": "Site-scoped HSE rows exist and are not attached to a well.",
    "unknown_npt_duration": "Some NPT rows state no duration. Those rows are not zero-filled.",
    "undated_record": "Some records have no date. A date window does not pull them inside the interval.",
    "missing_plan": "A section has no plan or no target. Absence is not a zero variance.",
    "missing_actual": "A section has no recorded actual. Absence is not zero.",
    "ambiguous_plan_match": "A plan match is ambiguous. The report does not pick a winner.",
    "plan_matched_by_name": "A plan row matched by name. The match mode is the source's, not a new join.",
    "actual_unit_unstated": "An actual value has no stated unit, so it is not subtracted from the plan.",
    "unpriced_cost_lines": "Some cost lines have no price. They are counted, not priced as zero.",
    "cost_plan_missing": "A currency has actual lines and no planned lines.",
    "cost_actual_missing": "A currency has planned lines and no actual lines.",
    "no_evidence": "Some rows carry no provenance entry. They are counted, not given a citation.",
}


def _iso(value: Any) -> Any:
    if value is None or value == "":
        return None
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    return str(value)


def _plain(value: Any) -> Any:
    """JSON-compatible copy. No sessions, models, or host objects survive this."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, (str, int, float)):
        return value
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    return str(value)


@dataclass(frozen=True)
class ReportRequest:
    """The exact request that produced a report. Output path is not a field."""

    mode: str
    well_ids: tuple[str, ...] = ()
    anchor: str = ""
    offsets: tuple[str, ...] = ()
    since: Any = None
    until: Any = None
    detail: int = 1
    evidence_limit: int = 10
    offset_limit: int = 10

    def payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "well_ids": list(self.well_ids),
            "anchor": self.anchor,
            "offsets": list(self.offsets),
            "since": _iso(self.since),
            "until": _iso(self.until),
            "detail": int(self.detail),
            "evidence_limit": int(self.evidence_limit),
            "offset_limit": int(self.offset_limit),
        }


@dataclass(frozen=True)
class ReportTable:
    table_id: str
    title: str
    columns: tuple[str, ...]
    rows: tuple[Mapping[str, Any], ...]
    note: str = ""
    column_labels: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.column_labels is None:
            object.__setattr__(self, "column_labels", {})

    def payload(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "title": self.title,
            "columns": list(self.columns),
            "column_labels": _plain(self.column_labels),
            "rows": _plain(self.rows),
            "note": self.note,
        }


@dataclass(frozen=True)
class ReportExhibit:
    """One chart specification. Values are copied from a certified cell, not recomputed."""

    exhibit_id: str
    exhibit_type: str
    title: str
    metric: str
    unit: str | None
    state: str
    subjects: tuple[Mapping[str, Any], ...]
    series: tuple[Mapping[str, Any], ...]
    categories: tuple[str, ...]
    data: Mapping[str, Any]
    caption: str
    evidence_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    reason: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "exhibit_id": self.exhibit_id,
            "exhibit_type": self.exhibit_type,
            "title": self.title,
            "metric": self.metric,
            "unit": self.unit,
            "state": self.state,
            "subjects": _plain(self.subjects),
            "series": _plain(self.series),
            "categories": list(self.categories),
            "data": _plain(self.data),
            "caption": self.caption,
            "evidence_refs": list(self.evidence_refs),
            "limitations": list(self.limitations),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReportSection:
    section_id: str
    title: str
    state: str
    claim_kind: str
    note: str = ""
    tables: tuple[ReportTable, ...] = ()
    exhibits: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "state": self.state,
            "claim_kind": self.claim_kind,
            "note": self.note,
            "tables": [table.payload() for table in self.tables],
            "exhibits": list(self.exhibits),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class ReportPack:
    """A self-contained engineering report. Rendering must not reopen a database."""

    schema: str
    request: ReportRequest
    mode: str
    title: str
    subject: Mapping[str, Any]
    source_packs: tuple[Mapping[str, Any], ...]
    sections: tuple[ReportSection, ...]
    exhibits: tuple[ReportExhibit, ...]
    evidence: tuple[Mapping[str, Any], ...]
    limitations: tuple[str, ...]
    freshness: Mapping[str, str]
    observations: tuple[str, ...]
    identity: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "request": self.request.payload(),
            "mode": self.mode,
            "title": self.title,
            "subject": _plain(self.subject),
            "source_packs": _plain(self.source_packs),
            "sections": [section.payload() for section in self.sections],
            "exhibits": [exhibit.payload() for exhibit in self.exhibits],
            "evidence": _plain(self.evidence),
            "limitations": list(self.limitations),
            "freshness": dict(self.freshness),
            "observations": list(self.observations),
            "identity": self.identity,
        }


def limitation_text(code: str) -> str:
    return LIMITATION_TEXT.get(code, f"{code} (as recorded by the source pack)")


def content_identity(payload: Mapping[str, Any]) -> str:
    """Identity of the report body. Callers must omit ``identity`` itself."""
    body = dict(payload)
    body.pop("identity", None)
    return sha256_obj(body)


def with_identity(pack: ReportPack) -> ReportPack:
    """Return the same pack with its content identity filled in."""
    identity = content_identity(pack.to_dict())
    return ReportPack(
        schema=pack.schema,
        request=pack.request,
        mode=pack.mode,
        title=pack.title,
        subject=pack.subject,
        source_packs=pack.source_packs,
        sections=pack.sections,
        exhibits=pack.exhibits,
        evidence=pack.evidence,
        limitations=pack.limitations,
        freshness=pack.freshness,
        observations=pack.observations,
        identity=identity,
    )


def merge_limitations(*groups: Sequence[str]) -> tuple[str, ...]:
    found: list[str] = []
    for group in groups:
        for item in group:
            if item and item not in found:
                found.append(item)
    for item in STANDING_LIMITATIONS:
        if item not in found:
            found.append(item)
    return tuple(found)
