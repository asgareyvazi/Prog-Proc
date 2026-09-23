"""Structured operational/domain records as a disposable search projection.

The search index already projects *documents* (extracted text plus the knowledge facts stored
about them).  This module is the second source type in that same projection: the authoritative
structured records - problems, NPT, events, lessons, recommendations, mud reports, BHA reports, bit
runs and survey runs - each turned into one searchable unit that cites its own row instead of a page
of a file.

The rules are the same ones the document half obeys, stated once here because they are the
whole point of the feature:

*   **The database is the authority.**  :func:`structured_records` reads the admitted model tables
    named in :data:`_RECORD_SOURCES` and formats rows; it writes nothing.  The projection may be
    deleted and rebuilt from those rows at any time.
*   **One searchable unit per *top-level* record, with its children folded in.**  The indexed types
    are the rows that answer a question about a well on their own: an assembly, a bit run, a survey
    set, a mud report, an NPT period.  A child row - a BHA component, a survey station, a mud
    measurement - is a *part* of its parent's statement rather than a separate answer, so it is not
    indexed as its own unit.  It is not thereby hidden: each parent builder walks its children and
    emits their values into the searchable text (``component 4: ...``, ``station 3: MD 9500.0 ft
    inclination 4.1 deg ...``) *and* collects their own provenance into
    ``component_evidence`` / ``station_evidence`` / ``measurement_evidence``.  "Which component was at
    sequence 4?" and "what was the inclination at MD 9500?" are therefore answerable from a search
    hit, and traceable to the child's own locator.  Child rows are read in one preloaded pass per
    child table (:class:`_Scope`), so folding costs three queries rather than one per parent.
*   **Identity is the record's own.**  A structured unit's id is
    ``structured:<record-type>:<row id>``, where ``<row id>`` is the authoritative primary key the
    domain already uses (``pdef-...``, ``prob-...``, ``npt-...``, ``ev-...``, ``les-...``,
    ``rec-...`` and ``mud-...``).  Re-indexing the same row always produces the same id; no UUID, timestamp or
    insertion position is invented here.
*   **Lifecycle is respected at build time.**  A record that the domain considers superseded or
    rejected is not searchable, exactly as a superseded document version is not (search answers
    "what should I act on").  The rule per type is :func:`is_searchable`.
*   **Provenance is retained, never fabricated.**  A promoted record cites its
    ``document_id``/``document_version_id`` and carries its ``provenance`` list; a hand-written
    lesson cites nothing and says so.  The projection keeps what the row keeps and invents none
    of it.

No AI, no embedding, no semantic anything lives here: this is a deterministic, one-way
``database rows -> text units`` formatting layer.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ..core.enums import ConfirmationStatus, RecommendationLifecycle
from ..database.models import (
    BhaComponent,
    BhaReport,
    BitRecord,
    LessonLearned,
    MudMeasurement,
    MudReport,
    NptRecord,
    ProblemDefinition,
    ProblemOccurrence,
    Recommendation,
    SurveyRun,
    SurveyStation,
    WellEvent,
)
from .tokenize import term_counts

__all__ = [
    "KIND_STRUCTURED",
    "STRUCTURED_RECORD_TYPES",
    "StructuredRecord",
    "is_searchable",
    "structured_record_id",
    "structured_records",
    "structured_row_ids",
    "structured_searchable_ids",
]

#: The one ``kind`` every structured unit carries, so a result can be told apart from a document
#: chunk at a glance.  Deliberately *not* in :data:`drilling_intelligence.search.chunking.CHUNK_KINDS`:
#: that tuple is the document chunk vocabulary; this is a different source type in the same index.
KIND_STRUCTURED = "structured"

#: The authoritative record types the projection indexes, as the ``record_type`` value and the
#: middle component of a structured identity.
STRUCTURED_RECORD_TYPES: tuple[str, ...] = (
    "problem_definition",
    "problem_occurrence",
    "npt_record",
    "well_event",
    "lesson_learned",
    "recommendation",
    "mud_report",
    "bha_report",
    "bit_record",
    "survey_run",
)


def structured_record_id(record_type: str, source_id: str) -> str:
    """The deterministic identity of one authoritative row in the search projection.

    ``structured:<record-type>:<row-id>`` is stable because both parts are: the record type is a
    fixed vocabulary (``STRUCTURED_RECORD_TYPES``) and ``source_id`` is the domain's own primary
    key.  Nothing derived from content or insertion order is involved.
    """
    return f"structured:{record_type}:{source_id}"


def is_searchable(record: Any) -> bool:
    """Whether a row should be in the searchable projection, by the domain's own lifecycle.

    *   :class:`ProblemDefinition` - always searchable.  A canonical definition has no supersede
        or reject state; ``status`` is informational and carried in the result, not used to hide it.
    *   :class:`ProblemOccurrence`, :class:`NptRecord`, :class:`WellEvent` - searchable unless a
        person rejected the record (:data:`ConfirmationStatus.REJECTED`).  A rejected record is
        evidence that the source claims something the field did not see, not something to act on.
    *   :class:`LessonLearned` - searchable only while it is the current revision.  A superseded
        revision is excluded exactly as a superseded document version is; approval state (DRAFT /
        REVIEW / APPROVED / REJECTED) is preserved as metadata rather than used to hide the row.
    *   :class:`Recommendation` - searchable unless superseded.  Proposed, accepted, declined and
        implemented advice are all decisions worth finding; only a replaced one is not.
    """
    if isinstance(record, ProblemDefinition):
        return True
    if isinstance(record, (ProblemOccurrence, NptRecord, WellEvent)):
        return str(getattr(record, "status", "") or "") != ConfirmationStatus.REJECTED.value
    if isinstance(record, (MudReport, BhaReport, BitRecord, SurveyRun)):
        # The source-versioned domains share one rule: an assembly, a bit run or a survey set that a
        # newer version of the same document replaced is history, not the answer to "what is in the
        # hole now" - exactly as a superseded document version is not indexed.
        return bool(getattr(record, "is_current", True)) and str(
            getattr(record, "status", "") or ""
        ) not in {"SUPERSEDED", ConfirmationStatus.REJECTED.value}
    if isinstance(record, LessonLearned):
        return bool(getattr(record, "is_current", True))
    if isinstance(record, Recommendation):
        return str(getattr(record, "status", "") or "") != RecommendationLifecycle.SUPERSEDED.value
    return True


def _iso(value: Any) -> str:
    """ISO-8601 text for anything date-like, so a lexicographic range filter is chronological."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    iso = getattr(value, "isoformat", None)
    return str(iso()) if callable(iso) else str(value)


def _emit(lines: Iterable[tuple[str, Any]]) -> str:
    """The indexed body of a structured record: present fields as ``label: value`` lines.

    Nothing is invented: a field the row does not carry simply contributes no line, so the text is
    exactly the record's own content, deterministically ordered.
    """
    rendered = [f"{label}: {value}" for label, value in lines if value not in (None, "", [], {})]
    return "\n".join(rendered)


def _evidence(record: Any, *, field: str = "provenance") -> list[dict[str, Any]]:
    """The record's own provenance/evidence list, as a list of plain dicts (or ``[]``)."""
    value = getattr(record, field, None) or []
    if isinstance(value, Mapping):
        value = [value]
    return [dict(item) for item in value if isinstance(item, Mapping)] or [
        dict(item) for item in value if item
    ]


def _unit(
    *,
    record_type: str,
    source_id: str,
    text: str,
    provenance: Mapping[str, Any],
    well_id: str = "",
    project_id: str = "",
    field_id: str = "",
    company_id: str = "",
    well_name: str = "",
    project_name: str = "",
    company_name: str = "",
    category: str = "",
    status: str = "",
    record_date: str = "",
    title: str = "",
    locator_ref: str = "",
) -> StructuredRecord:
    counts = term_counts(text)
    return StructuredRecord(
        record_id=structured_record_id(record_type, source_id),
        record_type=record_type,
        source_id=str(source_id),
        text=text,
        terms=counts,
        length=sum(counts.values()),
        char_count=len(text),
        well_id=well_id,
        project_id=project_id,
        field_id=field_id,
        company_id=company_id,
        well_name=well_name,
        project_name=project_name,
        company_name=company_name,
        category=category,
        status=status,
        record_date=record_date,
        title=title,
        locator_ref=locator_ref,
        provenance=provenance,
    )


@dataclass(frozen=True)
class StructuredRecord:
    """One structured row as a searchable unit, with its identity, text and provenance."""

    record_id: str
    record_type: str
    source_id: str
    text: str
    terms: Mapping[str, int]
    length: int
    char_count: int
    well_id: str = ""
    project_id: str = ""
    field_id: str = ""
    company_id: str = ""
    well_name: str = ""
    project_name: str = ""
    company_name: str = ""
    category: str = ""
    status: str = ""
    record_date: str = ""
    title: str = ""
    locator_ref: str = ""
    provenance: Mapping[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "record_type": self.record_type,
            "source_id": self.source_id,
            "text": self.text,
            "well_id": self.well_id,
            "project_id": self.project_id,
            "field_id": self.field_id,
            "company_id": self.company_id,
            "well_name": self.well_name,
            "project_name": self.project_name,
            "company_name": self.company_name,
            "category": self.category,
            "status": self.status,
            "record_date": self.record_date,
            "title": self.title,
            "locator_ref": self.locator_ref,
            "provenance_json": json.dumps(self.provenance, sort_keys=True, ensure_ascii=False)
            if self.provenance
            else None,
            "terms_json": json.dumps(dict(self.terms), sort_keys=True),
            "length": self.length,
            "char_count": self.char_count,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> StructuredRecord:
        provenance = row.get("provenance_json")
        terms = row.get("terms_json") or "{}"
        return cls(
            record_id=str(row["record_id"]),
            record_type=str(row["record_type"]),
            source_id=str(row["source_id"]),
            text=str(row.get("text") or ""),
            well_id=str(row.get("well_id") or ""),
            project_id=str(row.get("project_id") or ""),
            field_id=str(row.get("field_id") or ""),
            company_id=str(row.get("company_id") or ""),
            well_name=str(row.get("well_name") or ""),
            project_name=str(row.get("project_name") or ""),
            company_name=str(row.get("company_name") or ""),
            category=str(row.get("category") or ""),
            status=str(row.get("status") or ""),
            record_date=str(row.get("record_date") or ""),
            title=str(row.get("title") or ""),
            locator_ref=str(row.get("locator_ref") or ""),
            provenance=json.loads(provenance) if provenance else None,
            terms=json.loads(terms),
            length=int(row.get("length") or 0),
            char_count=int(row.get("char_count") or 0),
        )


class _Scope:
    """Resolves a ``well_id`` to the denormalised (project, field, company, names) a filter needs.

    Preloaded once per rebuild/stats pass, so "which field does this NPT belong to" is a dict read
    instead of a join per row - the same denormalisation the document half performs, and for the
    same reason: the index is disposable and must answer a filter without re-reading the registry.
    """

    def __init__(self, session: Any) -> None:
        from ..database.models import Company, Project, Well

        self._wells = {w.id: w for w in session.execute(select(Well)).scalars()}
        self._projects = {p.id: p for p in session.execute(select(Project)).scalars()}
        self._companies = {c.id: c for c in session.execute(select(Company)).scalars()}
        self._mud_measurements: dict[str, list[MudMeasurement]] = {}
        self._bha_components: dict[str, list[BhaComponent]] = {}
        self._survey_stations: dict[str, list[SurveyStation]] = {}
        for measurement in session.execute(
            select(MudMeasurement).order_by(
                MudMeasurement.mud_report_id,
                MudMeasurement.sample_key,
                MudMeasurement.property_name,
                MudMeasurement.id,
            )
        ).scalars():
            self._mud_measurements.setdefault(str(measurement.mud_report_id), []).append(
                measurement
            )

        for component in session.execute(
            select(BhaComponent).order_by(
                BhaComponent.bha_report_id, BhaComponent.sequence, BhaComponent.id
            )
        ).scalars():
            self._bha_components.setdefault(str(component.bha_report_id), []).append(component)
        for station in session.execute(
            select(SurveyStation).order_by(
                SurveyStation.survey_run_id, SurveyStation.sequence, SurveyStation.id
            )
        ).scalars():
            self._survey_stations.setdefault(str(station.survey_run_id), []).append(station)

    def mud_measurements(self, report_id: Any) -> tuple[MudMeasurement, ...]:
        return tuple(self._mud_measurements.get(str(report_id), ()))

    def bha_components(self, report_id: Any) -> tuple[BhaComponent, ...]:
        """The assembly's components, in the source's own order - never re-sorted."""
        return tuple(self._bha_components.get(str(report_id), ()))

    def survey_stations(self, run_id: Any) -> tuple[SurveyStation, ...]:
        """The set's stations, in the source's own order - never re-sorted."""
        return tuple(self._survey_stations.get(str(run_id), ()))

    def resolve(self, well_id: Any) -> tuple[str, str, str, str, str, str]:
        """``(project_id, field_id, company_id, well_name, project_name, company_name)``."""
        if not well_id:
            return "", "", "", "", "", ""
        well = self._wells.get(str(well_id))
        if well is None:
            return "", "", "", "", "", ""
        project = self._projects.get(well.project_id)
        company = (
            self._companies.get(project.company_id) if project and project.company_id else None
        )
        return (
            str(well.project_id or ""),
            str(well.field_id or ""),
            str(project.company_id or "") if project else "",
            str(well.name or ""),
            str(project.name or "") if project else "",
            str(company.name or "") if company else "",
        )


# --------------------------------------------------------------------------- builders
def _problem_definition(row: ProblemDefinition) -> StructuredRecord:
    text = _emit(
        [
            ("canonical key", row.canonical_key),
            ("problem type", row.problem_type),
            ("name", row.name),
            ("description", row.description),
        ]
    )
    return _unit(
        record_type="problem_definition",
        source_id=row.id,
        text=text,
        provenance={
            "source_type": "structured",
            "record_type": "problem_definition",
            "record_id": str(row.id),
            "canonical_key": row.canonical_key,
            "problem_type": row.problem_type,
            "name": row.name,
            "status": row.status,
            "origin": str(row.origin or ""),
            "evidence": _evidence(row),
        },
        category=row.problem_type,
        status=str(row.status or ""),
        title=row.name,
        locator_ref=f"problem definition {row.canonical_key}",
    )


def _problem_occurrence(row: ProblemOccurrence, scope: _Scope) -> StructuredRecord:
    project_id, field_id, company_id, well_name, project_name, company_name = scope.resolve(
        row.well_id
    )
    text = _emit(
        [
            ("problem type", row.problem_type),
            ("code", row.code),
            ("description", row.description),
            ("occurred", _iso(row.occurred_at)),
            ("hole size", f"{row.hole_size_in} in" if row.hole_size_in is not None else ""),
            ("formation", row.formation),
            ("immediate cause", row.immediate_cause),
            ("root cause", row.root_cause),
            ("contributing factors", ", ".join(row.contributing_factors or [])),
            ("corrective action", row.corrective_action),
            ("preventive action", row.preventive_action),
        ]
    )
    provenance: dict[str, Any] = {
        "source_type": "structured",
        "record_type": "problem_occurrence",
        "record_id": str(row.id),
        "problem_type": row.problem_type,
        "problem_definition_id": str(row.problem_definition_id or ""),
        "well_id": str(row.well_id or ""),
        "project_id": project_id,
        "field_id": field_id,
        "status": str(row.status or ""),
        "origin": str(row.origin or ""),
        "document_id": str(row.document_id or ""),
        "document_version_id": str(row.document_version_id or ""),
        "event_id": str(row.event_id or ""),
        "npt_id": str(row.npt_id or ""),
        "evidence": _evidence(row),
    }
    provenance = {key: value for key, value in provenance.items() if value}
    return _unit(
        record_type="problem_occurrence",
        source_id=row.id,
        text=text,
        provenance=provenance,
        well_id=str(row.well_id or ""),
        project_id=project_id,
        field_id=field_id,
        company_id=company_id,
        well_name=well_name,
        project_name=project_name,
        company_name=company_name,
        category=row.problem_type,
        status=str(row.status or ""),
        record_date=_iso(row.occurred_at),
        title=f"Problem - {row.problem_type}",
        locator_ref=f"problem {row.problem_type}" + (f" at well {well_name}" if well_name else ""),
    )


def _npt_record(row: NptRecord, scope: _Scope) -> StructuredRecord:
    project_id, field_id, company_id, well_name, project_name, company_name = scope.resolve(
        row.well_id
    )
    text = _emit(
        [
            ("category", row.category),
            ("code", row.subcategory),
            ("description", row.description),
            ("cause", row.cause),
            ("immediate cause", row.immediate_cause),
            ("root cause", row.root_cause),
            ("started", _iso(row.started_at)),
            ("ended", _iso(row.ended_at)),
            ("duration", f"{row.duration_hours} h" if row.duration_hours is not None else ""),
            ("duration basis", row.duration_basis),
        ]
    )
    provenance: dict[str, Any] = {
        "source_type": "structured",
        "record_type": "npt_record",
        "record_id": str(row.id),
        "category": row.category,
        "well_id": str(row.well_id or ""),
        "project_id": project_id,
        "field_id": field_id,
        "status": str(row.status or ""),
        "origin": str(row.origin or ""),
        "duration_hours": row.duration_hours,
        "document_id": str(row.document_id or ""),
        "document_version_id": str(row.document_version_id or ""),
        "event_id": str(row.event_id or ""),
        "operation_id": str(row.operation_id or ""),
        "report_id": str(row.report_id or ""),
        "rig_id": str(row.rig_id or ""),
        "service_company_id": str(row.service_company_id or ""),
        "evidence": _evidence(row),
    }
    provenance = {key: value for key, value in provenance.items() if value not in (None, "")}
    return _unit(
        record_type="npt_record",
        source_id=row.id,
        text=text,
        provenance=provenance,
        well_id=str(row.well_id or ""),
        project_id=project_id,
        field_id=field_id,
        company_id=company_id,
        well_name=well_name,
        project_name=project_name,
        company_name=company_name,
        category=row.category,
        status=str(row.status or ""),
        record_date=_iso(row.started_at),
        title=f"NPT - {row.category}",
        locator_ref=f"npt {row.category}" + (f" at well {well_name}" if well_name else ""),
    )


def _well_event(row: WellEvent, scope: _Scope) -> StructuredRecord:
    project_id, field_id, company_id, well_name, project_name, company_name = scope.resolve(
        row.well_id
    )
    text = _emit(
        [
            ("category", row.category),
            ("event type", row.event_type),
            ("label", row.label),
            ("description", row.description),
            ("occurred", _iso(row.occurred_at)),
            ("severity", row.severity),
        ]
    )
    provenance: dict[str, Any] = {
        "source_type": "structured",
        "record_type": "well_event",
        "record_id": str(row.id),
        "category": row.category,
        "event_type": row.event_type,
        "well_id": str(row.well_id or ""),
        "project_id": project_id,
        "field_id": field_id,
        "severity": row.severity,
        "status": str(row.status or ""),
        "record_state": str(row.record_state or ""),
        "origin": str(row.origin or ""),
        "document_id": str(row.document_id or ""),
        "document_version_id": str(row.document_version_id or ""),
        "operation_id": str(row.operation_id or ""),
        "report_id": str(row.report_id or ""),
        "section_id": str(row.section_id or ""),
        "equipment_item_id": str(row.equipment_item_id or ""),
        "rig_id": str(row.rig_id or ""),
        "service_company_id": str(row.service_company_id or ""),
        "evidence": _evidence(row),
    }
    provenance = {key: value for key, value in provenance.items() if value not in (None, "")}
    return _unit(
        record_type="well_event",
        source_id=row.id,
        text=text,
        provenance=provenance,
        well_id=str(row.well_id or ""),
        project_id=project_id,
        field_id=field_id,
        company_id=company_id,
        well_name=well_name,
        project_name=project_name,
        company_name=company_name,
        category=row.category,
        status=str(row.status or ""),
        record_date=_iso(row.occurred_at),
        title=str(row.label or f"Event - {row.event_type}"),
        locator_ref=f"event {row.event_type}" + (f" at well {well_name}" if well_name else ""),
    )


def _mud_report(row: MudReport, scope: _Scope) -> StructuredRecord:
    project_id, field_id, company_id, well_name, project_name, company_name = scope.resolve(
        row.well_id
    )
    measurements = scope.mud_measurements(row.id)
    measurement_lines: list[tuple[str, Any]] = []
    measurement_evidence: list[Mapping[str, Any]] = []
    for measurement in measurements:
        sample = str(measurement.sample_label or measurement.sample_key or "SUMMARY")
        value = "" if measurement.value is None else str(measurement.value)
        unit = str(measurement.unit or "")
        quality = str(measurement.quality or "")
        measurement_lines.append(
            (
                f"{measurement.property_name} {sample}",
                f"{value}{(' ' + unit) if unit else ''} {quality}".strip(),
            )
        )
        for entry in measurement.provenance or ():
            if isinstance(entry, Mapping):
                measurement_evidence.append(dict(entry))
    text = _emit(
        [
            ("mud report", row.id),
            ("report date", _iso(row.report_date)),
            ("revision", row.revision),
            (
                "measured depth",
                f"{row.depth_md_value} {row.depth_md_unit}"
                if row.depth_md_value is not None
                else "",
            ),
            (
                "true vertical depth",
                f"{row.depth_tvd_value} {row.depth_tvd_unit}"
                if row.depth_tvd_value is not None
                else "",
            ),
            ("well", well_name),
            *measurement_lines,
        ]
    )
    provenance: dict[str, Any] = {
        "source_type": "structured",
        "record_type": "mud_report",
        "record_id": str(row.id),
        "well_id": str(row.well_id or ""),
        "project_id": project_id,
        "field_id": field_id,
        "section_id": str(row.section_id or ""),
        "document_id": str(row.document_id or ""),
        "document_version_id": str(row.document_version_id or ""),
        "status": str(row.status or ""),
        "record_state": str(row.record_state or ""),
        "origin": str(row.origin or ""),
        "evidence": [dict(entry) for entry in row.provenance or () if isinstance(entry, Mapping)],
        "measurement_evidence": measurement_evidence,
    }
    provenance = {key: value for key, value in provenance.items() if value not in (None, "", [])}
    return _unit(
        record_type="mud_report",
        source_id=row.id,
        text=text,
        provenance=provenance,
        well_id=str(row.well_id or ""),
        project_id=project_id,
        field_id=field_id,
        company_id=company_id,
        well_name=well_name,
        project_name=project_name,
        company_name=company_name,
        category="mud",
        status=str(row.status or ""),
        record_date=_iso(row.report_date),
        title=f"Mud report {row.revision or row.id}",
        locator_ref=f"mud report {row.id}",
    )


def _lesson_learned(row: LessonLearned) -> StructuredRecord:
    text = _emit(
        [
            ("title", row.title),
            ("code", row.code),
            ("problem type", row.problem_type),
            ("lesson", row.lesson),
            ("observation", row.observation),
            ("context", row.context),
            ("root cause", row.root_cause),
            ("recommendation", row.recommendation),
            ("conditions", row.conditions),
            ("applicable operations", ", ".join(row.applicable_operations or [])),
            ("applicable formations", ", ".join(row.applicable_formations or [])),
        ]
    )
    provenance: dict[str, Any] = {
        "source_type": "structured",
        "record_type": "lesson_learned",
        "record_id": str(row.id),
        "code": row.code,
        "problem_type": row.problem_type,
        "status": str(row.status or ""),
        "revision": row.revision,
        "origin": str(row.origin or ""),
        "well_id": str(row.well_id or ""),
        "project_id": str(row.project_id or ""),
        "field_id": str(row.field_id or ""),
        "section_id": str(row.section_id or ""),
        "evidence": _evidence(row),
    }
    provenance = {key: value for key, value in provenance.items() if value not in (None, "")}
    return _unit(
        record_type="lesson_learned",
        source_id=row.id,
        text=text,
        provenance=provenance,
        well_id=str(row.well_id or ""),
        project_id=str(row.project_id or ""),
        field_id=str(row.field_id or ""),
        category=str(row.problem_type or ""),
        status=str(row.status or ""),
        record_date=_iso(row.approved_at),
        title=str(row.title or ""),
        locator_ref=f"lesson {row.code or row.id}",
    )


def _recommendation(row: Recommendation) -> StructuredRecord:
    text = _emit(
        [
            ("statement", row.statement),
            ("reason", row.reason),
            ("generated by", row.generated_by),
            ("applicability", json.dumps(row.applicability or {}, sort_keys=True)),
        ]
    )
    provenance: dict[str, Any] = {
        "source_type": "structured",
        "record_type": "recommendation",
        "record_id": str(row.id),
        "signature": row.signature,
        "status": str(row.status or ""),
        "generated_by": str(row.generated_by or ""),
        "well_id": str(row.well_id or ""),
        "project_id": str(row.project_id or ""),
        "field_id": str(row.field_id or ""),
        "section_id": str(row.section_id or ""),
        "pattern_id": str(row.pattern_id or ""),
        "lesson_id": str(row.lesson_id or ""),
        "practice_id": str(row.practice_id or ""),
        "problem_id": str(row.problem_id or ""),
        "operation_id": str(row.operation_id or ""),
        "evidence": _evidence(row, field="evidence"),
    }
    provenance = {key: value for key, value in provenance.items() if value not in (None, "")}
    return _unit(
        record_type="recommendation",
        source_id=row.id,
        text=text,
        provenance=provenance,
        well_id=str(row.well_id or ""),
        project_id=str(row.project_id or ""),
        field_id=str(row.field_id or ""),
        status=str(row.status or ""),
        record_date=_iso(row.decided_at),
        title=str((row.statement or "").split(".")[0][:120]),
        locator_ref=f"recommendation {row.id}",
    )


def _bha_report(row: BhaReport, scope: _Scope) -> StructuredRecord:
    project_id, field_id, company_id, well_name, project_name, company_name = scope.resolve(
        row.well_id
    )
    component_lines: list[tuple[str, Any]] = []
    component_evidence: list[Mapping[str, Any]] = []
    for component in scope.bha_components(row.id):
        dimensions = " x ".join(
            part
            for part in (
                f"OD {component.od_value} {component.od_unit}".strip()
                if component.od_value is not None
                else "",
                f"ID {component.id_value} {component.id_unit}".strip()
                if component.id_value is not None
                else "",
                f"length {component.length_value} {component.length_unit}".strip()
                if component.length_value is not None
                else "",
            )
            if part
        )
        component_lines.append(
            (
                f"component {component.sequence}",
                " ".join(
                    part
                    for part in (
                        component.source_label,
                        component.component_type,
                        component.manufacturer,
                        component.model,
                        component.serial_number,
                        dimensions,
                        f"qty {component.quantity}" if component.quantity else "",
                    )
                    if part
                ).strip(),
            )
        )
        for entry in component.provenance or ():
            if isinstance(entry, Mapping):
                component_evidence.append(dict(entry))
    text = _emit(
        [
            ("bottom hole assembly", row.bha_number or row.id),
            ("run interval", (
                f"{row.top_depth_value} {row.top_depth_unit} to "
                f"{row.bottom_depth_value} {row.bottom_depth_unit}".strip()
                if row.top_depth_value is not None and row.bottom_depth_value is not None
                else ""
            )),
            ("report date", _iso(row.report_date)),
            ("assembly description", row.assembly_description),
            ("well", well_name),
            *component_lines,
        ]
    )
    provenance: dict[str, Any] = {
        "source_type": "structured",
        "record_type": "bha_report",
        "record_id": str(row.id),
        "well_id": str(row.well_id or ""),
        "project_id": project_id,
        "field_id": field_id,
        "section_id": str(row.section_id or ""),
        "document_id": str(row.document_id or ""),
        "document_version_id": str(row.document_version_id or ""),
        "status": str(row.status or ""),
        "record_state": str(row.record_state or ""),
        "origin": str(row.origin or ""),
        "section_resolution": str(row.section_resolution or ""),
        "evidence": [dict(entry) for entry in row.provenance or () if isinstance(entry, Mapping)],
        "component_evidence": component_evidence,
    }
    provenance = {key: value for key, value in provenance.items() if value not in (None, "", [])}
    return _unit(
        record_type="bha_report",
        source_id=row.id,
        text=text,
        provenance=provenance,
        well_id=str(row.well_id or ""),
        project_id=project_id,
        field_id=field_id,
        company_id=company_id,
        well_name=well_name,
        project_name=project_name,
        company_name=company_name,
        category="bha",
        status=str(row.status or ""),
        record_date=_iso(row.report_date),
        title=f"BHA {row.bha_number or row.id}",
        locator_ref=f"bottom hole assembly {row.bha_number or row.id}",
    )


def _bit_record(row: BitRecord, scope: _Scope) -> StructuredRecord:
    project_id, field_id, company_id, well_name, project_name, company_name = scope.resolve(
        row.well_id
    )
    text = _emit(
        [
            ("bit number", row.bit_number),
            ("run number", row.run_number),
            ("manufacturer", row.manufacturer),
            ("model", row.model),
            ("bit type", row.bit_type),
            ("iadc code", row.iadc_code),
            ("serial number", row.serial_number),
            ("size", f"{row.size_value} {row.size_unit}".strip() if row.size_value is not None else ""),
            ("depth in", f"{row.depth_in_value} {row.depth_in_unit}".strip() if row.depth_in_value is not None else ""),
            ("depth out", f"{row.depth_out_value} {row.depth_out_unit}".strip() if row.depth_out_value is not None else ""),
            ("footage", f"{row.footage_value} {row.footage_unit}".strip() if row.footage_value is not None else ""),
            ("rotating hours", row.rotating_hours),
            ("drilling hours", row.drilling_hours),
            ("pull reason", row.pull_reason),
            ("dull grade", row.dull_grade),
            ("nozzles", row.nozzle_size_text),
            ("well", well_name),
        ]
    )
    provenance: dict[str, Any] = {
        "source_type": "structured",
        "record_type": "bit_record",
        "record_id": str(row.id),
        "well_id": str(row.well_id or ""),
        "project_id": project_id,
        "field_id": field_id,
        "section_id": str(row.section_id or ""),
        "bha_report_id": str(row.bha_report_id or ""),
        "document_id": str(row.document_id or ""),
        "document_version_id": str(row.document_version_id or ""),
        "status": str(row.status or ""),
        "record_state": str(row.record_state or ""),
        "origin": str(row.origin or ""),
        "evidence": [dict(entry) for entry in row.provenance or () if isinstance(entry, Mapping)],
    }
    provenance = {key: value for key, value in provenance.items() if value not in (None, "", [])}
    return _unit(
        record_type="bit_record",
        source_id=row.id,
        text=text,
        provenance=provenance,
        well_id=str(row.well_id or ""),
        project_id=project_id,
        field_id=field_id,
        company_id=company_id,
        well_name=well_name,
        project_name=project_name,
        company_name=company_name,
        category="bit",
        status=str(row.status or ""),
        record_date=_iso(row.run_date),
        title=f"Bit {row.bit_number}" + (f" run {row.run_number}" if row.run_number else ""),
        locator_ref=f"bit record {row.id}",
    )


def _survey_run(row: SurveyRun, scope: _Scope) -> StructuredRecord:
    project_id, field_id, company_id, well_name, project_name, company_name = scope.resolve(
        row.well_id
    )
    station_lines: list[tuple[str, Any]] = []
    station_evidence: list[Mapping[str, Any]] = []
    for station in scope.survey_stations(row.id):
        station_lines.append(
            (
                f"station {station.station_number_text or station.sequence}",
                " ".join(
                    part
                    for part in (
                        f"MD {station.md_value} {station.md_unit}".strip(),
                        f"TVD {station.tvd_value} {station.tvd_unit}".strip()
                        if station.tvd_value is not None
                        else "",
                        f"inclination {station.inclination_value} {station.inclination_unit}".strip(),
                        f"azimuth {station.azimuth_value} {station.azimuth_unit}".strip(),
                        f"toolface {station.toolface_value}".strip()
                        if station.toolface_value is not None
                        else "",
                        f"DLS {station.dls_value}".strip() if station.dls_value is not None else "",
                    )
                    if part
                ).strip(),
            )
        )
        for entry in station.provenance or ():
            if isinstance(entry, Mapping):
                station_evidence.append(dict(entry))
    text = _emit(
        [
            ("survey run", row.run_label or row.id),
            ("survey tool", row.survey_tool),
            ("survey date", _iso(row.survey_date)),
            ("station count", row.station_count),
            ("measured depth range", (
                f"{row.min_md_value} to {row.max_md_value} {row.md_unit}".strip()
                if row.min_md_value is not None
                else ""
            )),
            ("well", well_name),
            *station_lines,
        ]
    )
    provenance: dict[str, Any] = {
        "source_type": "structured",
        "record_type": "survey_run",
        "record_id": str(row.id),
        "well_id": str(row.well_id or ""),
        "project_id": project_id,
        "field_id": field_id,
        "section_id": str(row.section_id or ""),
        "document_id": str(row.document_id or ""),
        "document_version_id": str(row.document_version_id or ""),
        "status": str(row.status or ""),
        "record_state": str(row.record_state or ""),
        "origin": str(row.origin or ""),
        "station_identity": str(row.station_identity or ""),
        "evidence": [dict(entry) for entry in row.provenance or () if isinstance(entry, Mapping)],
        "station_evidence": station_evidence,
    }
    provenance = {key: value for key, value in provenance.items() if value not in (None, "", [])}
    return _unit(
        record_type="survey_run",
        source_id=row.id,
        text=text,
        provenance=provenance,
        well_id=str(row.well_id or ""),
        project_id=project_id,
        field_id=field_id,
        company_id=company_id,
        well_name=well_name,
        project_name=project_name,
        company_name=company_name,
        category="survey",
        status=str(row.status or ""),
        record_date=_iso(row.survey_date),
        title=f"Directional survey {row.run_label or row.id}",
        locator_ref=f"survey run {row.id}",
    )


_BUILDERS = {
    "problem_definition": _problem_definition,
    "problem_occurrence": _problem_occurrence,
    "npt_record": _npt_record,
    "well_event": _well_event,
    "lesson_learned": _lesson_learned,
    "recommendation": _recommendation,
    "mud_report": _mud_report,
    "bha_report": _bha_report,
    "bit_record": _bit_record,
    "survey_run": _survey_run,
}

#: The model each record type is read from, and a deterministic order for its rows.
_RECORD_SOURCES: tuple[tuple[type, str, tuple[str, ...]], ...] = (
    (ProblemDefinition, "problem_definition", ("canonical_key", "id")),
    (ProblemOccurrence, "problem_occurrence", ("occurred_at", "id")),
    (NptRecord, "npt_record", ("started_at", "id")),
    (WellEvent, "well_event", ("occurred_at", "id")),
    (LessonLearned, "lesson_learned", ("revision", "id")),
    (Recommendation, "recommendation", ("created_at", "id")),
    (MudReport, "mud_report", ("report_date", "id")),
    (BhaReport, "bha_report", ("report_date", "id")),
    (BitRecord, "bit_record", ("run_date", "id")),
    (SurveyRun, "survey_run", ("survey_date", "id")),
)


def structured_records(session: Any) -> list[StructuredRecord]:
    """Every *searchable* structured row, in a deterministic order, ready to store.

    Ordering is by the record's own columns (and its id last), never by insertion position, so a
    rebuild over the same rows produces the same units in the same order.  Rows the domain
    considers non-current (:func:`is_searchable`) are simply not projected, exactly as superseded
    document versions are left out of a document rebuild.
    """
    scope = _Scope(session)
    units: list[StructuredRecord] = []
    for model, record_type, order_columns in _RECORD_SOURCES:
        statement = select(model)
        for column in order_columns:
            statement = statement.order_by(getattr(model, column))
        builder = _BUILDERS[record_type]
        for row in session.execute(statement).scalars():
            if not is_searchable(row):
                continue
            if record_type in {"problem_definition", "lesson_learned", "recommendation"}:
                units.append(builder(row))
            else:
                units.append(builder(row, scope))
    return units


def structured_searchable_ids(session: Any) -> set[str]:
    """The stable ids of every currently-searchable structured row (the prune/status work set)."""
    ids: set[str] = set()
    for model, record_type, _order_columns in _RECORD_SOURCES:
        for row in session.execute(select(model)).scalars():
            if is_searchable(row):
                ids.add(structured_record_id(record_type, str(row.id)))
    return ids


def structured_row_ids(session: Any) -> set[str]:
    """The stable ids of *all* authoritative structured rows, searchable or not.

    Prune and status need to tell "the row still exists but is no longer searchable" (stale) from
    "the row is gone entirely" (orphaned), and this is the set that draws the line.
    """
    ids: set[str] = set()
    for model, record_type, _order in _RECORD_SOURCES:
        for row in session.execute(select(model)).scalars():
            ids.add(structured_record_id(record_type, str(row.id)))
    return ids
