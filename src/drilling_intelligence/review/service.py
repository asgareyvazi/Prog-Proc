"""Deterministic, read-only aggregation of the existing domain for human inspection.

This service closes a read-side gap, not a storage gap.  It reads the authoritative SQLite rows
through the existing repositories, keeps current/history and plan/actual semantics visible, carries
row provenance and evidence references without copying them into a new table, and optionally invokes
the existing citation auditor.  It never reads the search sidecar and never writes a row, timestamp,
cache entry, audit event or review snapshot.  Calculation classification is likewise inspection only:
review never re-runs the bounded NPT executor.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.enums import KnowledgeOrigin, KnowledgeStatus
from ..core.errors import ValidationError
from ..core.hashing import sha256_obj
from ..core.ids import SubjectKey
from ..database.models import (
    BestPractice,
    BhaComponent,
    BhaReport,
    BitRecord,
    Calculation,
    CalculationInput,
    Company,
    Document,
    DocumentVersion,
    DrillingProgram,
    Extraction,
    Field,
    KnowledgeRelation,
    MudMeasurement,
    MudReport,
    NptRecord,
    ProblemDefinition,
    ProgramTarget,
    Project,
    SurveyRun,
    SurveyStation,
    Well,
)
from ..database.serialize import record_to_dict
from ..documents.repository import DocumentRepository
from ..engineering.costs import CostRepository
from ..engineering.repository import (
    DEPENDENCY_CURRENT,
    DEPENDENCY_STALE,
    DEPENDENCY_UNRESOLVED,
    EngineeringRepository,
)
from ..engineering.risk import RiskRepository
from ..engineering.service import (
    NPT_ROLLUP_METHOD_ID,
    NPT_ROLLUP_METHOD_VERSION,
    NPT_ROLLUP_PROPERTY,
    NPT_ROLLUP_UNIT,
)
from ..evidence.contract import EvidencePackage, PackageEvidence
from ..evidence.verify import CitationAuditor
from ..intelligence.patterns import list_patterns
from ..knowledge.entities import EntityRef
from ..knowledge.facts import KnowledgeFact
from ..knowledge.repository import KnowledgeRepository
from ..lessons.repository import LessonRepository
from ..operations.assets import AssetRepository
from ..operations.contracts import promotion_contract
from ..operations.repository import OperationsRepository
from ..wells.repository import WellRepository
from .contract import (
    REVIEW_CURRENT,
    REVIEW_HISTORY,
    DomainReview,
    DomainReviewRequest,
    ReviewConflict,
    ReviewRecord,
    ReviewVerification,
)

__all__ = ["DomainReviewService"]

#: Repository list methods have deliberately different default limit conventions.  The review
#: boundary uses one explicit safety bound so a future screen cannot accidentally request a zero-row
#: result from one repository and an unbounded result from another.  A caller that needs a larger
#: review can pass ``limit``; truncation is reported rather than hidden.
_SAFE_LIMIT = 10_000

_OPERATIONAL_TABLES = frozenset(
    {
        "ddr_report",
        "well_operation",
        "well_event",
        "npt_record",
        "problem_occurrence",
        "field_pattern",
    }
)
_CANDIDATE_STATUSES = frozenset({"CANDIDATE", "DRAFT", "PROPOSED", "UNVERIFIED"})
_PENDING_REVIEW_STATUSES = frozenset({"REVIEW", "IN_REVIEW", "ISSUED_FOR_REVIEW", "UNREVIEWED"})
_EXPLICITLY_REVIEWED_STATUSES = frozenset(
    {"APPROVED", "CONFIRMED", "CHECKED", "ACCEPTED", "IMPLEMENTED"}
)


def _plain(value: Any) -> Any:
    """Return JSON-safe values without inventing a rendering for domain content."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, set):
        return sorted((_plain(item) for item in value), key=str)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _plain(value.to_dict())
    return value


def _mapping_entries(value: Any) -> tuple[Mapping[str, Any], ...]:
    """Keep provenance/evidence entries as mappings, the shape the evidence contract stores."""
    if isinstance(value, Mapping):
        return (dict(_plain(value)),)
    if isinstance(value, (list, tuple)):
        return tuple(dict(_plain(entry)) for entry in value if isinstance(entry, Mapping))
    return ()


def _dedupe_mappings(entries: Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """Deduplicate repeated graph/evidence pointers without changing their recorded contents."""
    found: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        payload = dict(_plain(entry))
        key = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        found.setdefault(key, payload)
    return tuple(found[key] for key in sorted(found))


def _dedupe_rows(rows: Iterable[Any]) -> list[Any]:
    """Merge overlapping inherited scopes by the authoritative table and primary key."""
    found: dict[tuple[str, str], Any] = {}
    for row in rows:
        table = str(getattr(type(row), "__tablename__", type(row).__name__))
        row_id = str(getattr(row, "id", ""))
        if row_id:
            found.setdefault((table, row_id), row)
    return list(found.values())


def _fetch_limit(request: DomainReviewRequest) -> int:
    """The bounded look-ahead used by every repository read.

    ``limit`` is a result cap, not a license for each repository to return ``limit`` rows and then
    silently discard the rest.  Fetching one extra row lets the service distinguish a complete group
    from a bounded group while keeping the existing deterministic prefix contract.  A zero request
    still has the explicit safety cap that the boundary has always promised.
    """
    requested = int(request.limit) if request.limit > 0 else _SAFE_LIMIT
    return requested + 1


def _result_limit(request: DomainReviewRequest) -> int:
    """The number of records the review may expose after its bounded reads."""
    return int(request.limit) if request.limit > 0 else _SAFE_LIMIT


def _citation_entries(entries: Iterable[Any]) -> tuple[Mapping[str, Any], ...]:
    """Flatten only recorded file citations from row/evidence graphs.

    Review records deliberately keep both the raw row provenance and richer evidence pointers.  A
    relation entry often nests its file provenance under ``provenance``; handing the wrapper to the
    citation auditor would make that citation invisible.  This helper does not manufacture a
    citation or reinterpret an evidence reference: it only carries mappings that already expose a
    ``locator`` and recursively visits their existing provenance/evidence containers.
    """
    found: list[Mapping[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if "locator" in value:
                found.append(dict(_plain(value)))
                return
            for nested in value.values():
                visit(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested)

    for entry in entries:
        visit(entry)
    return _dedupe_mappings(found)


def _scope_from(data: Mapping[str, Any]) -> dict[str, str]:
    return {
        key: str(data.get(key) or "") for key in ("well_id", "field_id", "project_id", "section_id")
    }


def _record_status(row: Any) -> str:
    for name in ("status", "processing_status"):
        value = getattr(row, name, None)
        if value not in (None, ""):
            return str(getattr(value, "value", value))
    return ""


def _record_state(row: Any) -> str:
    value = getattr(row, "record_state", "")
    return str(getattr(value, "value", value) or "")


def _current_for(
    row: Any,
    *,
    superseded_calculations: set[str],
    current_programs: set[str],
) -> bool:
    table = str(getattr(type(row), "__tablename__", ""))
    status = _record_status(row)
    if table == "program_target":
        return str(getattr(row, "program_id", "")) in current_programs
    if table == "document_version":
        return bool(getattr(row, "is_current", False))
    if table == "calculation":
        return str(getattr(row, "id", "")) not in superseded_calculations and status != "SUPERSEDED"
    if table == "knowledge_item":
        return status not in {KnowledgeStatus.SUPERSEDED.value, KnowledgeStatus.RETIRED.value}
    if table in _OPERATIONAL_TABLES:
        return status != "REJECTED"
    if table in {"risk_record", "recommendation"}:
        return status != "SUPERSEDED"
    if hasattr(row, "is_current"):
        return bool(row.is_current)
    return True


def _flags(
    *,
    row: Any,
    status: str,
    current: bool,
    provenance: Sequence[Mapping[str, Any]],
    conflict_ids: Sequence[str],
    data: Mapping[str, Any],
) -> tuple[str, ...]:
    table = str(getattr(type(row), "__tablename__", ""))
    flags: list[str] = ["current" if current else "historical"]
    if status in _CANDIDATE_STATUSES:
        flags.append("candidate")
    if status in _PENDING_REVIEW_STATUSES:
        flags.append("pending_review")
    if status in _EXPLICITLY_REVIEWED_STATUSES:
        flags.append("reviewed")
    if status in {"CONFLICTED", "CONFLICT"} or conflict_ids:
        flags.append("conflicting")
    if status in {"UNVERIFIED", "NOT_CHECKABLE"}:
        flags.append("unverifiable")
    if hasattr(row, "provenance") and not provenance:
        flags.append("no_recorded_provenance")
    origin = str(getattr(row, "origin", "") or "")
    if (
        origin in {KnowledgeOrigin.EXTRACTED.value, KnowledgeOrigin.DERIVED.value}
        and not provenance
    ):
        flags.append("unverifiable_source")
    if table == "field_pattern" and data.get("stale_at"):
        flags.append("stale_snapshot")
    if table == "document":
        if str(data.get("processing_status") or "") == "FAILED":
            flags.append("processing_failed")
        contract = promotion_contract(str(data.get("classification") or ""))
        if contract is None or not contract.domain_promotable:
            flags.append("promotion_unsupported")
        else:
            data_level = str(contract.level.value)
            if data_level not in {"END_TO_END_CERTIFIED", "DOMAIN_PROMOTABLE"}:
                flags.append("promotion_not_certified")
    if table == "document_version":
        extraction_status = str(data.get("extraction_status") or "")
        if not extraction_status:
            flags.append("missing_extraction_artefact")
        elif extraction_status != "OK":
            flags.append("processing_failed")
        router = data.get("extraction_router")
        if isinstance(router, Mapping) and (
            bool(router.get("degraded")) or bool(router.get("fallback_from"))
        ):
            flags.append("extraction_degraded")
        diagnostics = data.get("extraction_diagnostics") or ()
        if any(
            token in str(item).lower()
            for item in diagnostics
            for token in ("fallback", "truncated", "no text", "unavailable")
        ):
            flags.append("extraction_degraded")
    if table == "calculation":
        classification = str(data.get("calculation_classification") or "")
        if classification == "INCOMPLETE":
            flags.append("calculation_contract_incomplete")
        elif classification == "STORED_ONLY":
            flags.extend(("calculation_stored_only", "calculation_not_executable_in_repository"))
        elif classification == "EXECUTABLE":
            flags.append("calculation_executable")
        elif classification == "STALE":
            flags.append("calculation_stale_dependency")
        elif classification == "UNRESOLVED":
            flags.append("calculation_unresolved_dependency")
        elif not data.get("method_id") or not data.get("method_version") or not data.get("inputs"):
            flags.append("calculation_contract_incomplete")
        else:
            flags.append("calculation_stored_only")
            flags.append("calculation_not_executable_in_repository")
    record_state = _record_state(row)
    if record_state:
        flags.append(f"state:{record_state}")
    return tuple(flags)


def _row_evidence(
    row: Any,
    provenance: Sequence[Mapping[str, Any]],
    extra: Sequence[Mapping[str, Any]] = (),
) -> tuple[Mapping[str, Any], ...]:
    entries: list[Mapping[str, Any]] = list(provenance)
    entries.extend(_mapping_entries(getattr(row, "evidence", None)))
    entries.extend(extra)
    return _dedupe_mappings(entries)


def _record_from_row(
    row: Any,
    *,
    superseded_calculations: set[str],
    current_programs: set[str],
    conflict_ids: Sequence[str] = (),
    extra_evidence: Sequence[Mapping[str, Any]] = (),
    data_extra: Mapping[str, Any] | None = None,
    scope_extra: Mapping[str, Any] | None = None,
) -> ReviewRecord:
    data = dict(_plain(record_to_dict(row)))
    if str(getattr(type(row), "__tablename__", "")) == "document":
        contract = promotion_contract(str(data.get("classification") or ""))
        data["promotion_contract"] = contract.to_dict() if contract is not None else None
        data["promotion_eligibility"] = bool(contract is not None and contract.domain_promotable)
    if data_extra:
        data.update({str(key): _plain(value) for key, value in data_extra.items()})
    provenance = _mapping_entries(getattr(row, "provenance", None))
    status = _record_status(row)
    current = _current_for(
        row,
        superseded_calculations=superseded_calculations,
        current_programs=current_programs,
    )
    conflicts = tuple(sorted(str(value) for value in conflict_ids))
    scope = _scope_from(data)
    if scope_extra:
        scope.update(
            {
                str(key): str(value or "")
                for key, value in scope_extra.items()
                if key in {"well_id", "field_id", "project_id", "section_id"}
            }
        )
    return ReviewRecord(
        record_type=str(getattr(type(row), "__tablename__", type(row).__name__)),
        record_id=str(row.id),
        status=status,
        record_state=_record_state(row),
        current=current,
        scope=scope,
        data=data,
        provenance=provenance,
        evidence=_row_evidence(row, provenance, extra_evidence),
        conflict_ids=conflicts,
        flags=_flags(
            row=row,
            status=status,
            current=current,
            provenance=provenance,
            conflict_ids=conflicts,
            data=data,
        ),
        verification=ReviewVerification(
            reproducibility=(
                _reproducibility_for(str(data.get("calculation_classification") or ""))
                if str(getattr(type(row), "__tablename__", "")) == "calculation"
                else "NOT_ASSESSED"
            )
        ),
    )


def _record_from_fact(fact: KnowledgeFact, *, conflict_ids: Sequence[str] = ()) -> ReviewRecord:
    data = dict(_plain(fact.to_dict()))
    provenance = _mapping_entries(fact.provenance.to_dict()) if fact.provenance is not None else ()
    status = str(fact.status or "")
    current = status not in {KnowledgeStatus.SUPERSEDED.value, KnowledgeStatus.RETIRED.value}
    conflicts = tuple(sorted(str(value) for value in conflict_ids))
    flags = ["current" if current else "historical"]
    if status in _CANDIDATE_STATUSES:
        flags.append("candidate")
    if status in _PENDING_REVIEW_STATUSES:
        flags.append("pending_review")
    if status in _EXPLICITLY_REVIEWED_STATUSES:
        flags.append("reviewed")
    if status == KnowledgeStatus.CONFLICTED.value or conflicts:
        flags.append("conflicting")
    if status == KnowledgeStatus.UNVERIFIED.value:
        flags.append("unverifiable")
    if not provenance:
        flags.append("no_recorded_provenance")
    if (
        str(fact.origin) in {KnowledgeOrigin.EXTRACTED.value, KnowledgeOrigin.DERIVED.value}
        and not provenance
    ):
        flags.append("unverifiable_source")
    return ReviewRecord(
        record_type="knowledge_item",
        record_id=str(fact.item_id),
        status=status,
        record_state=str(fact.record_state or ""),
        current=current,
        scope={
            "well_id": str(fact.well_id or ""),
            "field_id": "",
            "project_id": str(fact.project_id or ""),
            "section_id": str(fact.section_id or ""),
        },
        data=data,
        provenance=provenance,
        evidence=_dedupe_mappings((*provenance, *fact.evidence)),
        conflict_ids=conflicts,
        flags=tuple(flags),
        verification=ReviewVerification(),
    )


def _edge_payload(row: KnowledgeRelation) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "source_type": str(row.source_type),
        "source_id": str(row.source_id),
        "relation": str(row.relation),
        "target_type": str(row.target_type),
        "target_id": str(row.target_id),
        "weight": float(row.weight),
        "provenance": _plain(row.provenance or []),
        "note": row.note,
    }


def _merge_by_id(*groups: Sequence[Any]) -> list[Any]:
    return _dedupe_rows(item for group in groups for item in group)


def _for_well(rows: Sequence[Any], well_id: str) -> list[Any]:
    """Keep direct rows and genuinely well-wide rows, not another well's denormalised scope."""
    return [
        row for row in rows if not getattr(row, "well_id", None) or str(row.well_id) == str(well_id)
    ]


def _identity_for_record(record: ReviewRecord) -> str:
    return f"structured:{record.record_type}:{record.record_id}"


def _calculation_dependency_impact(
    repository: EngineeringRepository,
    row: Calculation,
    indexed_inputs: Sequence[Mapping[str, Any]],
    *,
    source_rows: Mapping[str, NptRecord],
    versions: Mapping[str, DocumentVersion],
) -> dict[str, Any]:
    """Project the existing read-only impact query onto one calculation's input edges.

    This intentionally calls ``calculation_impact`` rather than re-running the NPT method.  The review
    boundary answers whether a stored dependency is current, stale or unresolved; it never changes the
    stored number or attempts to repair a source.
    """
    entries: list[dict[str, Any]] = []
    reports: dict[str, Mapping[str, Any]] = {}
    for item in indexed_inputs:
        subject = str(item.get("subject_key") or "").strip()
        input_name = str(item.get("name") or "")
        if not subject:
            entries.append(
                {
                    "calculation_id": str(row.id),
                    "input_name": input_name,
                    "subject_key": "",
                    "dependency": DEPENDENCY_UNRESOLVED,
                    "document_version_id": "",
                }
            )
            continue
        report = reports.get(subject)
        if report is None:
            report = repository.calculation_impact(subject, well_id=str(row.well_id or ""))
            reports[subject] = report
        match = next(
            (
                dict(candidate)
                for candidate in report.get("entries", ())
                if str(candidate.get("calculation_id")) == str(row.id)
                and str(candidate.get("input_name")) == input_name
            ),
            None,
        )
        if match is None:
            match = {
                "calculation_id": str(row.id),
                "input_name": input_name,
                "subject_key": subject,
                "document_version_id": str(
                    (item.get("provenance") or {}).get("document_version_id")
                    if isinstance(item.get("provenance"), Mapping)
                    else ""
                ),
                "dependency": DEPENDENCY_UNRESOLVED,
            }
        provenance = item.get("provenance")
        if isinstance(provenance, Mapping) and str(item.get("source_kind") or "") == "npt_record":
            npt_id = str(provenance.get("npt_record_id") or "")
            version_id = str(provenance.get("document_version_id") or "")
            source = source_rows.get(npt_id)
            version = versions.get(version_id)
            if not npt_id or source is None or not version_id or version is None:
                match["dependency"] = DEPENDENCY_UNRESOLVED
            elif not bool(version.is_current):
                match["dependency"] = DEPENDENCY_STALE
            match["document_version_id"] = version_id
        entries.append(match)
    entries.sort(
        key=lambda item: (str(item.get("input_name") or ""), str(item.get("subject_key") or ""))
    )
    counts = {
        DEPENDENCY_CURRENT: sum(item.get("dependency") == DEPENDENCY_CURRENT for item in entries),
        DEPENDENCY_STALE: sum(item.get("dependency") == DEPENDENCY_STALE for item in entries),
        DEPENDENCY_UNRESOLVED: sum(
            item.get("dependency") == DEPENDENCY_UNRESOLVED for item in entries
        ),
    }
    return {
        "calculation_id": str(row.id),
        "resolved": all(item.get("dependency") != DEPENDENCY_UNRESOLVED for item in entries),
        "counts": counts,
        "entries": entries,
    }


def _calculation_contract(
    data: Mapping[str, Any],
    indexed_inputs: Sequence[Mapping[str, Any]],
    *,
    source_rows: Mapping[str, NptRecord],
    versions: Mapping[str, DocumentVersion],
) -> tuple[str, tuple[str, ...]]:
    """Classify a stored calculation without evaluating its arithmetic.

    ``EXECUTABLE`` means the stored row satisfies the narrow registered NPT V1 contract and all of its
    source links are inspectable.  ``STALE``/``UNRESOLVED`` describe evidence state, not a recomputation;
    ``STORED_ONLY`` is the honest result for a complete calculation method that has no registered executor;
    and ``INCOMPLETE`` means the stored contract cannot be reproduced even before looking at source age.
    """
    issues: list[str] = []
    unresolved = False
    stale = False
    method = str(data.get("method_id") or "").strip()
    version = str(data.get("method_version") or "").strip()
    if not method or not version or not data.get("inputs"):
        issues.append("missing_method_version_or_inputs")
    if method != NPT_ROLLUP_METHOD_ID:
        raw_inputs = data.get("inputs")
        if not isinstance(raw_inputs, Mapping):
            issues.append("inputs_are_not_a_mapping")
        elif {str(key) for key in raw_inputs} != {
            str(item.get("name") or "") for item in indexed_inputs
        }:
            issues.append("calculation_input_index_mismatch")
        if not indexed_inputs:
            issues.append("no_indexed_inputs")
        if not isinstance(data.get("outputs"), Mapping):
            issues.append("missing_or_malformed_outputs")
        if not isinstance(data.get("validation"), Mapping):
            issues.append("missing_or_malformed_validation")
        if str(
            data.get("origin") or KnowledgeOrigin.MANUAL.value
        ) != KnowledgeOrigin.MANUAL.value and not data.get("provenance"):
            issues.append("missing_calculation_provenance")
        if not any(data.get(key) for key in ("well_id", "section_id", "project_id", "source_id")):
            issues.append("missing_calculation_scope")
        if issues:
            return "INCOMPLETE", tuple(sorted(set(issues)))
        return "STORED_ONLY", ("method_not_registered_for_execution",)
    if version != NPT_ROLLUP_METHOD_VERSION:
        if not version:
            return "INCOMPLETE", tuple(issues or ("missing_method_version",))
        return "STORED_ONLY", ("method_version_not_registered_for_execution",)
    if str(data.get("calculation_type") or "") != NPT_ROLLUP_PROPERTY:
        issues.append("wrong_calculation_type")
    if str(data.get("record_state") or "").upper() != "ACTUAL":
        issues.append("wrong_record_state")
    well_id = str(data.get("well_id") or "")
    if not well_id:
        issues.append("missing_well_scope")
    if str(data.get("origin") or "") != KnowledgeOrigin.DERIVED.value:
        issues.append("wrong_origin")
    if not data.get("provenance"):
        issues.append("missing_calculation_provenance")

    raw_inputs = data.get("inputs")
    if not isinstance(raw_inputs, Mapping):
        issues.append("inputs_are_not_a_mapping")
    elif {str(key) for key in raw_inputs} != {
        str(item.get("name") or "") for item in indexed_inputs
    }:
        issues.append("calculation_input_index_mismatch")
    if not indexed_inputs:
        issues.append("no_indexed_inputs")

    output = data.get("outputs")
    metric = output.get(NPT_ROLLUP_PROPERTY) if isinstance(output, Mapping) else None
    if not isinstance(metric, Mapping):
        issues.append("missing_npt_output")
    else:
        try:
            output_value = float(metric.get("value"))
        except (TypeError, ValueError, OverflowError):
            issues.append("malformed_npt_output")
        else:
            if not math.isfinite(output_value) or output_value < 0.0:
                issues.append("invalid_npt_output")
        if str(metric.get("unit") or "") != NPT_ROLLUP_UNIT:
            issues.append("wrong_output_unit")
        if str(metric.get("dimension") or "") != "TIME":
            issues.append("wrong_output_dimension")
    validation = data.get("validation")
    if not isinstance(validation, Mapping):
        issues.append("missing_validation")
    else:
        try:
            considered = int(validation.get("records_considered"))
            summed = int(validation.get("records_summed"))
            without_duration = int(validation.get("records_without_duration"))
            without_evidence = int(validation.get("records_without_evidence", 0))
        except (TypeError, ValueError):
            issues.append("malformed_validation")
        else:
            if (
                min(considered, summed, without_duration, without_evidence) < 0
                or considered != summed + without_duration
                or summed != len(indexed_inputs)
                or without_evidence != 0
            ):
                issues.append("inconsistent_validation")
        if isinstance(output, Mapping) and output.get("records") != validation.get(
            "records_summed"
        ):
            issues.append("output_validation_mismatch")

    expected_subject = (
        SubjectKey(
            well_id=well_id,
            property_name=NPT_ROLLUP_PROPERTY,
            record_state="ACTUAL",
        ).render()
        if well_id
        else ""
    )
    for item in indexed_inputs:
        value = item.get("value")
        numeric_value: float | None = None
        try:
            numeric_value = float(value)
        except (TypeError, ValueError, OverflowError):
            issues.append("malformed_input_value")
        else:
            if not math.isfinite(numeric_value) or numeric_value < 0.0:
                issues.append("invalid_input_value")
        if str(item.get("unit") or "") != NPT_ROLLUP_UNIT:
            issues.append("wrong_input_unit")
        if str(item.get("dimension") or "") != "TIME":
            issues.append("wrong_input_dimension")
        if str(item.get("source_kind") or "") != "npt_record":
            issues.append("wrong_input_source_kind")
        if expected_subject and str(item.get("subject_key") or "") != expected_subject:
            issues.append("wrong_input_scope")
        provenance = item.get("provenance")
        if not isinstance(provenance, Mapping):
            issues.append("missing_input_provenance")
            continue
        npt_id = str(provenance.get("npt_record_id") or "")
        version_id = str(provenance.get("document_version_id") or "")
        digest = str(provenance.get("source_sha256") or "")
        if not npt_id or not version_id or not digest:
            issues.append("incomplete_input_evidence")
            continue
        source = source_rows.get(npt_id)
        if source is None:
            unresolved = True
        else:
            if str(source.well_id or "") != well_id:
                issues.append("cross_well_source_row")
            if str(source.document_version_id or "") != version_id:
                issues.append("source_version_mismatch")
            source_status = str(source.status or "").upper()
            if source_status in {"REJECTED", "SUPERSEDED", "RETIRED"}:
                issues.append("rejected_source_row")
            try:
                source_value = float(source.duration_hours)
            except (TypeError, ValueError, OverflowError):
                issues.append("malformed_source_duration")
            else:
                if not math.isfinite(source_value) or source_value < 0.0:
                    issues.append("invalid_source_duration")
                elif numeric_value is not None and not math.isclose(
                    source_value, numeric_value, rel_tol=0.0, abs_tol=1e-9
                ):
                    issues.append("source_input_value_mismatch")
        cited = versions.get(version_id)
        if cited is None:
            unresolved = True
        elif not bool(cited.is_current):
            stale = True
        elif digest != str(cited.sha256 or ""):
            issues.append("source_digest_mismatch")

    if issues:
        return "INCOMPLETE", tuple(sorted(set(issues)))
    if unresolved:
        return "UNRESOLVED", ("source_dependency_unresolved",)
    if stale:
        return "STALE", ("source_dependency_stale",)
    return "EXECUTABLE", ()


def _source_navigation(
    data: Mapping[str, Any],
    indexed_inputs: Sequence[Mapping[str, Any]],
    *,
    versions: Mapping[str, DocumentVersion],
    documents: Mapping[str, Document],
) -> list[dict[str, Any]]:
    """Expose existing source paths/locators for a UI or CLI without opening the file here."""
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    candidates: list[Mapping[str, Any]] = []
    for item in indexed_inputs:
        provenance = item.get("provenance")
        if isinstance(provenance, Mapping):
            candidates.append({**provenance, "input_name": item.get("name")})
    for provenance in _mapping_entries(data.get("provenance")):
        candidates.append(provenance)
    if not candidates and data.get("document_version_id"):
        candidates.append(
            {
                "document_id": data.get("document_id"),
                "document_version_id": data.get("document_version_id"),
            }
        )
    for provenance in candidates:
        version_id = str(provenance.get("document_version_id") or "")
        version = versions.get(version_id)
        document_id = str(provenance.get("document_id") or (version.document_id if version else ""))
        document = documents.get(document_id)
        key = (version_id, str(provenance.get("npt_record_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "input_name": str(provenance.get("input_name") or ""),
                "npt_record_id": str(provenance.get("npt_record_id") or ""),
                "document_id": document_id,
                "document_version_id": version_id,
                "filename": str(document.filename if document is not None else ""),
                "source_path": str(
                    (version.source_relative_path or version.source_path)
                    if version is not None
                    else ""
                ),
                "version_number": int(version.version_number) if version is not None else None,
                "is_current": bool(version.is_current) if version is not None else False,
                "source_sha256": str(
                    version.sha256 if version is not None else provenance.get("source_sha256") or ""
                ),
                "locator": _plain(provenance.get("locator")),
                "excerpt": str(provenance.get("excerpt") or ""),
            }
        )
    return sorted(
        entries,
        key=lambda item: (
            str(item.get("document_version_id") or ""),
            str(item.get("npt_record_id") or ""),
            str(item.get("input_name") or ""),
        ),
    )


def _reproducibility_for(classification: str) -> str:
    if classification == "STORED_ONLY":
        # Preserve the established public wording for unregistered methods while flags/data expose the
        # sharper V1 distinction to new consumers.
        return "NOT_EXECUTABLE_IN_REPOSITORY"
    return classification or "NOT_ASSESSED"


class DomainReviewService:
    """Compose one deterministic, read-only well review from authoritative repositories."""

    def __init__(self, *, workspace: Any) -> None:
        if workspace is None:
            raise ValidationError("a domain review needs a workspace")
        self._workspace = workspace

    @classmethod
    def for_workspace(cls, workspace: Any) -> DomainReviewService:
        return cls(workspace=workspace)

    def review(self, request: DomainReviewRequest | Mapping[str, Any]) -> DomainReview:
        """Read a review without opening a unit of work or committing anything."""
        req = (
            request
            if isinstance(request, DomainReviewRequest)
            else DomainReviewRequest.from_dict(request)
        )
        with self._workspace.database.read_only() as session:
            review = self._build(session, req)
        if not req.verify_citations:
            return review
        return self._audit(review)

    def _build(self, session: Session, request: DomainReviewRequest) -> DomainReview:
        well = session.get(Well, request.well_id)
        if well is None:
            raise ValidationError(f"no well {request.well_id!r}")
        field = session.get(Field, str(well.field_id)) if well.field_id else None
        project = session.get(Project, str(well.project_id)) if well.project_id else None
        company = (
            session.get(Company, str(project.company_id))
            if project and project.company_id
            else None
        )
        context = {
            "well": _plain(record_to_dict(well)),
            "field": _plain(record_to_dict(field)) if field is not None else None,
            "project": _plain(record_to_dict(project)) if project is not None else None,
            "company": _plain(record_to_dict(company)) if company is not None else None,
            "scope": {
                "well_id": str(well.id),
                "field_id": str(well.field_id or ""),
                "project_id": str(well.project_id or ""),
            },
        }
        limit = _fetch_limit(request)
        result_limit = _result_limit(request)
        sections_rows = sorted(
            WellRepository(session).list_sections(well.id, limit=_SAFE_LIMIT + 1),
            key=lambda row: (row.sequence, row.id),
        )
        sections_truncated = len(sections_rows) > _SAFE_LIMIT
        sections = tuple(_plain(record_to_dict(row)) for row in sections_rows[:_SAFE_LIMIT])

        operational = OperationsRepository(session)
        rows: list[Any] = []
        bounded_sizes: list[int] = []

        def add_bounded(group: Sequence[Any], *, fetched_count: int | None = None) -> None:
            rows.extend(group)
            bounded_sizes.append(len(group) if fetched_count is None else int(fetched_count))

        add_bounded(operational.list_reports(well_id=well.id, limit=limit))
        add_bounded(operational.list_operations(well_id=well.id, limit=limit))
        add_bounded(operational.list_events(well_id=well.id, limit=limit))
        add_bounded(operational.list_npt(well_id=well.id, limit=limit))
        mud_statement = (
            select(MudReport)
            .where(MudReport.well_id == well.id)
            .order_by(MudReport.report_date.asc().nulls_last(), MudReport.id)
        )
        if request.lifecycle == REVIEW_CURRENT:
            mud_statement = mud_statement.where(MudReport.is_current.is_(True))
        mud_reports = list(session.execute(mud_statement.limit(limit)).scalars())
        add_bounded(mud_reports)
        mud_report_ids = [str(row.id) for row in mud_reports]
        mud_measurements_by_report: dict[str, list[MudMeasurement]] = defaultdict(list)
        if mud_report_ids:
            mud_measurements = list(
                session.execute(
                    select(MudMeasurement)
                    .where(MudMeasurement.mud_report_id.in_(mud_report_ids))
                    .order_by(
                        MudMeasurement.mud_report_id,
                        MudMeasurement.sample_key,
                        MudMeasurement.property_name,
                        MudMeasurement.id,
                    )
                    .limit(limit * max(1, len(mud_report_ids)))
                ).scalars()
            )
            bounded_sizes.append(len(mud_measurements))
            for measurement in mud_measurements:
                mud_measurements_by_report[str(measurement.mud_report_id)].append(measurement)
        # The V4 hardware and geometry domains are reviewed by the same contract as the mud report:
        # the parent row in scope, its ordered children in one bounded batch, and a lifecycle filter
        # that hides a superseded source version only when the reviewer asked for the current one.
        bha_statement = (
            select(BhaReport)
            .where(BhaReport.well_id == well.id)
            .order_by(BhaReport.report_date.asc().nulls_last(), BhaReport.id)
        )
        bit_statement = (
            select(BitRecord)
            .where(BitRecord.well_id == well.id)
            .order_by(BitRecord.run_date.asc().nulls_last(), BitRecord.id)
        )
        survey_statement = (
            select(SurveyRun)
            .where(SurveyRun.well_id == well.id)
            .order_by(SurveyRun.survey_date.asc().nulls_last(), SurveyRun.id)
        )
        if request.lifecycle == REVIEW_CURRENT:
            bha_statement = bha_statement.where(BhaReport.is_current.is_(True))
            bit_statement = bit_statement.where(BitRecord.is_current.is_(True))
            survey_statement = survey_statement.where(SurveyRun.is_current.is_(True))
        bha_reports = list(session.execute(bha_statement.limit(limit)).scalars())
        add_bounded(bha_reports)
        bit_records = list(session.execute(bit_statement.limit(limit)).scalars())
        add_bounded(bit_records)
        survey_runs = list(session.execute(survey_statement.limit(limit)).scalars())
        add_bounded(survey_runs)
        bha_report_ids = [str(row.id) for row in bha_reports]
        components_by_report: dict[str, list[BhaComponent]] = defaultdict(list)
        if bha_report_ids:
            components = list(
                session.execute(
                    select(BhaComponent)
                    .where(BhaComponent.bha_report_id.in_(bha_report_ids))
                    .order_by(BhaComponent.bha_report_id, BhaComponent.sequence, BhaComponent.id)
                    .limit(limit * max(1, len(bha_report_ids)))
                ).scalars()
            )
            bounded_sizes.append(len(components))
            for component in components:
                components_by_report[str(component.bha_report_id)].append(component)
        survey_run_ids = [str(row.id) for row in survey_runs]
        stations_by_run: dict[str, list[SurveyStation]] = defaultdict(list)
        if survey_run_ids:
            stations = list(
                session.execute(
                    select(SurveyStation)
                    .where(SurveyStation.survey_run_id.in_(survey_run_ids))
                    .order_by(SurveyStation.survey_run_id, SurveyStation.sequence, SurveyStation.id)
                    .limit(limit * max(1, len(survey_run_ids)))
                ).scalars()
            )
            bounded_sizes.append(len(stations))
            for station in stations:
                stations_by_run[str(station.survey_run_id)].append(station)
        problems = operational.list_problems(well_id=well.id, limit=limit)
        add_bounded(problems)
        problem_definition_ids = sorted(
            {
                str(row.problem_definition_id)
                for row in problems
                if getattr(row, "problem_definition_id", None)
            }
        )
        if problem_definition_ids:
            rows.extend(
                session.execute(
                    select(ProblemDefinition)
                    .where(ProblemDefinition.id.in_(problem_definition_ids))
                    .order_by(ProblemDefinition.id)
                ).scalars()
            )

        documents = DocumentRepository(session)
        document_rows = _for_well(
            _merge_by_id(
                documents.list_documents(well_id=well.id, limit=limit),
                documents.list_documents(
                    project_id=str(well.project_id),
                    limit=limit,
                    scope_wide_only=True,
                )
                if well.project_id
                else (),
            ),
            well.id,
        )
        add_bounded(document_rows)
        document_ids = [str(row.id) for row in document_rows]
        version_rows: list[DocumentVersion] = []
        if document_ids:
            version_rows = list(
                session.execute(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id.in_(document_ids))
                    .order_by(
                        DocumentVersion.document_id,
                        DocumentVersion.version_number,
                        DocumentVersion.id,
                    )
                    .limit(limit)
                ).scalars()
            )
            rows.extend(version_rows)
            bounded_sizes.append(len(version_rows))
        extraction_by_version: dict[str, Extraction] = {}
        version_ids = sorted({str(row.id) for row in version_rows})
        if version_ids:
            for extraction in session.scalars(
                select(Extraction)
                .where(Extraction.document_version_id.in_(version_ids))
                .order_by(Extraction.created_at.desc(), Extraction.id.desc())
            ):
                extraction_by_version.setdefault(str(extraction.document_version_id), extraction)

        engineering = EngineeringRepository(session)
        programs = _for_well(
            engineering.programs_for_well(
                well.id,
                include_superseded=request.lifecycle == REVIEW_HISTORY,
                limit=limit,
            ),
            well.id,
        )
        procedures = _for_well(
            engineering.procedures_for_well(
                well.id,
                include_superseded=request.lifecycle == REVIEW_HISTORY,
                limit=limit,
            ),
            well.id,
        )
        add_bounded(programs)
        add_bounded(procedures)

        program_ids = sorted({str(row.id) for row in programs})
        target_rows: list[Any] = []
        if program_ids:
            # ProgramTarget is owned by its program, but an inherited field/project program may
            # contain targets explicitly attached to another well's section.  Keep unbound template
            # targets and targets for this well's sections; never expose the other well's target in a
            # subject-scoped review.
            section_ids = [str(row.id) for row in sections_rows]
            target_scope = [ProgramTarget.section_id.is_(None)]
            if section_ids:
                target_scope.append(ProgramTarget.section_id.in_(section_ids))
            target_rows = list(
                session.execute(
                    select(ProgramTarget)
                    .where(
                        ProgramTarget.program_id.in_(program_ids),
                        or_(*target_scope),
                    )
                    .order_by(
                        ProgramTarget.program_id,
                        ProgramTarget.sequence,
                        ProgramTarget.name,
                        ProgramTarget.id,
                    )
                    .limit(limit)
                ).scalars()
            )
            rows.extend(target_rows)
            bounded_sizes.append(len(target_rows))

        risk_repo = RiskRepository(session)
        risks = risk_repo.list_risks(well_id=well.id, include_closed=True, limit=limit)
        if well.field_id:
            risks = _merge_by_id(
                risks,
                risk_repo.list_risks(
                    field_id=str(well.field_id),
                    include_closed=True,
                    include_child_wells=False,
                    limit=limit,
                ),
            )
        if well.project_id:
            risks = _merge_by_id(
                risks,
                risk_repo.list_risks(
                    project_id=str(well.project_id),
                    include_closed=True,
                    include_child_wells=False,
                    limit=limit,
                ),
            )
        risks = _for_well(risks, well.id)
        add_bounded(risks)
        cost_repository = CostRepository(session)
        costs = _for_well(
            _merge_by_id(
                cost_repository.list_items(well_id=well.id, limit=limit),
                cost_repository.list_items(
                    field_id=str(well.field_id), limit=limit, scope_wide_only=True
                )
                if well.field_id
                else (),
                cost_repository.list_items(
                    project_id=str(well.project_id), limit=limit, scope_wide_only=True
                )
                if well.project_id
                else (),
            ),
            well.id,
        )
        add_bounded(costs)

        lesson_repository = LessonRepository(session)
        lessons = _for_well(
            lesson_repository.lessons_for_well(
                well.id,
                approved_only=False,
                include_superseded=request.lifecycle == REVIEW_HISTORY,
                limit=limit,
            ),
            well.id,
        )
        add_bounded(lessons)
        practices: list[BestPractice] = list(
            lesson_repository.list_practices(
                well_id=well.id, include_superseded=True, include_child_wells=False, limit=limit
            )
        )
        if well.field_id:
            practices.extend(
                lesson_repository.list_practices(
                    field_id=str(well.field_id),
                    include_superseded=True,
                    include_child_wells=False,
                    limit=limit,
                )
            )
        if well.project_id:
            practices.extend(
                lesson_repository.list_practices(
                    project_id=str(well.project_id),
                    include_superseded=True,
                    include_child_wells=False,
                    limit=limit,
                )
            )
        practices = _for_well(_dedupe_rows(practices), well.id)
        add_bounded(practices)
        lesson_repo = lesson_repository
        recommendations = _for_well(
            _merge_by_id(
                lesson_repo.list_recommendations(well_id=well.id, limit=limit),
                lesson_repo.list_recommendations(
                    field_id=str(well.field_id or ""),
                    limit=limit,
                    include_child_wells=False,
                )
                if well.field_id
                else (),
                lesson_repo.list_recommendations(
                    project_id=str(well.project_id or ""),
                    limit=limit,
                    include_child_wells=False,
                )
                if well.project_id
                else (),
            ),
            well.id,
        )
        add_bounded(recommendations)

        patterns = list_patterns(
            session,
            field_id=str(well.field_id or ""),
            project_id=str(well.project_id or ""),
            limit=limit,
        )
        add_bounded(patterns)

        calculations = _for_well(
            _merge_by_id(
                engineering.calculations_for(well_id=well.id, limit=limit),
                engineering.calculations_for(
                    project_id=str(well.project_id or ""),
                    limit=limit,
                    scope_wide_only=True,
                )
                if well.project_id
                else (),
            ),
            well.id,
        )
        add_bounded(calculations)
        calculation_ids = sorted({str(row.id) for row in calculations})
        calculation_inputs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if calculation_ids:
            input_rows = session.execute(
                select(CalculationInput)
                .where(CalculationInput.calculation_id.in_(calculation_ids))
                .order_by(
                    CalculationInput.calculation_id, CalculationInput.name, CalculationInput.id
                )
                .limit(limit)
            ).scalars()
            input_rows = list(input_rows)
            bounded_sizes.append(len(input_rows))
            for input_row in input_rows:
                calculation_inputs[str(input_row.calculation_id)].append(
                    _plain(record_to_dict(input_row))
                )

        # These are enrichment reads for the existing calculation -> NPT -> document-version chain.  They
        # never execute a method and they are bounded by the already fetched input rows, not by a new
        # persistence structure.
        npt_ids = sorted(
            {
                str(item.get("provenance", {}).get("npt_record_id") or "")
                for values in calculation_inputs.values()
                for item in values
                if isinstance(item.get("provenance"), Mapping)
                and item.get("provenance", {}).get("npt_record_id")
            }
        )
        source_rows: dict[str, NptRecord] = {}
        if npt_ids:
            source_rows = {
                str(row.id): row
                for row in session.execute(
                    select(NptRecord).where(NptRecord.id.in_(npt_ids))
                ).scalars()
            }
        version_ids = sorted(
            {
                str(item.get("provenance", {}).get("document_version_id") or "")
                for values in calculation_inputs.values()
                for item in values
                if isinstance(item.get("provenance"), Mapping)
                and item.get("provenance", {}).get("document_version_id")
            }
            | {
                str(row.document_version_id or "")
                for row in calculations
                if row.document_version_id
            }
        )
        versions: dict[str, DocumentVersion] = {}
        if version_ids:
            versions = {
                str(row.id): row
                for row in session.execute(
                    select(DocumentVersion).where(DocumentVersion.id.in_(version_ids))
                ).scalars()
            }
        document_ids = sorted(
            {str(version.document_id) for version in versions.values() if version.document_id}
            | {str(row.document_id) for row in calculations if row.document_id}
        )
        documents_by_id: dict[str, Document] = {}
        if document_ids:
            documents_by_id = {
                str(row.id): row
                for row in session.execute(
                    select(Document).where(Document.id.in_(document_ids))
                ).scalars()
            }

        assets = AssetRepository(session)
        rig_rows = assets.rigs_for_well(well.id, limit=limit)
        service_company_rows = assets.service_companies_for_well(well.id, limit=limit)
        rows.extend(rig_rows)
        rows.extend(service_company_rows)
        bounded_sizes.extend((len(rig_rows), len(service_company_rows)))

        knowledge = KnowledgeRepository(session)
        facts = knowledge.facts_for_well(well.id, include_superseded=True, limit=limit)
        fetched_fact_count = len(facts)
        subject_section_ids = {str(row.id) for row in sections_rows}
        # The knowledge repository indexes the explicit ``well_id`` column.  A malformed or
        # hand-authored row can still carry a different section/project alongside that well, so
        # intersect the inherited identifiers here as well rather than trusting denormalised scope.
        facts = [
            fact
            for fact in facts
            if (not fact.resolved_section_id or fact.resolved_section_id in subject_section_ids)
            and (not fact.project_id or str(fact.project_id) == str(well.project_id or ""))
            and (not fact.resolved_well_id or str(fact.resolved_well_id) == str(well.id))
        ]
        conflicts = knowledge.conflicts(well_id=well.id, status=None, limit=limit)
        bounded_sizes.extend((fetched_fact_count, len(conflicts)))
        conflict_by_item: dict[str, list[str]] = defaultdict(list)
        review_conflicts: list[ReviewConflict] = []
        for conflict in conflicts:
            conflict_id = str(conflict.id)
            candidates = tuple(
                dict(_plain(candidate))
                for candidate in (conflict.candidates or [])
                if isinstance(candidate, Mapping)
            )
            current = str(conflict.status or "") == "OPEN"
            if current or request.lifecycle == REVIEW_HISTORY:
                for candidate in candidates:
                    item_id = str(candidate.get("item_id") or "")
                    if item_id:
                        conflict_by_item[item_id].append(conflict_id)
            review_conflicts.append(
                ReviewConflict(
                    conflict_id=conflict_id,
                    lookup_key=str(conflict.lookup_key or ""),
                    property_name=str(conflict.property_name or ""),
                    status=str(conflict.status or ""),
                    record_state=str(conflict.record_state or ""),
                    current=current,
                    candidates=candidates,
                    resolution=dict(_plain(conflict.resolution or {})),
                    data=dict(_plain(record_to_dict(conflict))),
                )
            )

        lesson_ids = sorted({str(row.id) for row in lessons})
        lesson_evidence: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        if lesson_ids:
            edge_rows = list(
                session.execute(
                    select(KnowledgeRelation)
                    .where(
                        KnowledgeRelation.source_type == "lesson",
                        KnowledgeRelation.source_id.in_(lesson_ids),
                    )
                    .order_by(
                        KnowledgeRelation.source_id,
                        KnowledgeRelation.relation,
                        KnowledgeRelation.target_id,
                        KnowledgeRelation.id,
                    )
                    .limit(limit)
                ).scalars()
            )
            bounded_sizes.append(len(edge_rows))
            for edge in edge_rows:
                lesson_evidence[str(edge.source_id)].append(
                    {
                        "relation": str(edge.relation),
                        "target_type": str(edge.target_type),
                        "target_id": str(edge.target_id),
                        "note": edge.note,
                        "provenance": _plain(edge.provenance or []),
                    }
                )

        superseded_calculations = (
            {
                str(value)
                for (value,) in session.execute(
                    select(Calculation.supersedes_id).where(
                        Calculation.supersedes_id.in_(calculation_ids)
                    )
                ).all()
                if value
            }
            if calculation_ids
            else set()
        )
        current_programs = {
            str(row.id) for row in programs if bool(getattr(row, "is_current", False))
        }
        program_by_id = {str(row.id): row for row in programs}

        def row_in_scope(row: Any) -> bool:
            row_well_id = str(getattr(row, "well_id", "") or "")
            row_field_id = str(getattr(row, "field_id", "") or "")
            row_project_id = str(getattr(row, "project_id", "") or "")
            row_section_id = str(getattr(row, "section_id", "") or "")
            return (
                (not row_well_id or row_well_id == str(well.id))
                and (not row_field_id or row_field_id == str(well.field_id or ""))
                and (not row_project_id or row_project_id == str(well.project_id or ""))
                and (not row_section_id or row_section_id in subject_section_ids)
            )

        row_records: list[ReviewRecord] = []
        for row in _dedupe_rows(rows):
            if not row_in_scope(row):
                continue
            table = str(getattr(type(row), "__tablename__", ""))
            row_data = dict(_plain(record_to_dict(row)))
            extra_entries: list[Mapping[str, Any]] = []
            data_extra: dict[str, Any] = {}
            scope_extra: dict[str, Any] = {}
            if table == "mud_report":
                measurement_rows = mud_measurements_by_report.get(str(row.id), [])
                data_extra["measurements"] = [
                    _plain(record_to_dict(measurement)) for measurement in measurement_rows
                ]
                for measurement in measurement_rows:
                    extra_entries.extend(_mapping_entries(getattr(measurement, "provenance", None)))
            if table == "bha_report":
                component_rows = components_by_report.get(str(row.id), [])
                # The components are shown in the source's own order, which is the assembly's order.
                data_extra["components"] = [
                    _plain(record_to_dict(component)) for component in component_rows
                ]
                for component in component_rows:
                    extra_entries.extend(_mapping_entries(getattr(component, "provenance", None)))
            if table == "survey_run":
                station_rows = stations_by_run.get(str(row.id), [])
                data_extra["stations"] = [
                    _plain(record_to_dict(station)) for station in station_rows
                ]
                for station in station_rows:
                    extra_entries.extend(_mapping_entries(getattr(station, "provenance", None)))
            if table == "document_version":
                extraction = extraction_by_version.get(str(row.id))
                if extraction is not None:
                    data_extra["extraction_status"] = str(extraction.status or "")
                    data_extra["extraction_error"] = str(extraction.error or "")
                    data_extra["extraction_router"] = _plain(extraction.router_decision or {})
                    payload = extraction.document_json or {}
                    data_extra["extraction_diagnostics"] = _plain(payload.get("diagnostics") or [])
                else:
                    data_extra["extraction_status"] = ""
            if table == "lesson_learned":
                extra_entries.extend(lesson_evidence.get(str(row.id), ()))
            if table == "calculation":
                indexed_inputs = calculation_inputs.get(str(row.id), [])
                data_extra["indexed_inputs"] = indexed_inputs
                dependency_impact = _calculation_dependency_impact(
                    engineering,
                    row,
                    indexed_inputs,
                    source_rows=source_rows,
                    versions=versions,
                )
                classification, contract_issues = _calculation_contract(
                    row_data,
                    indexed_inputs,
                    source_rows=source_rows,
                    versions=versions,
                )
                # A valid contract with an old or unresolved source is not executable *today*, even though
                # its historical result remains readable.  The dependency query is the authoritative
                # current/stale/unresolved distinction; no arithmetic is repeated here.
                dependency_counts = dependency_impact.get("counts") or {}
                if classification == "EXECUTABLE":
                    if dependency_counts.get(DEPENDENCY_UNRESOLVED, 0):
                        classification = "UNRESOLVED"
                        contract_issues = ("dependency_unresolved",)
                    elif dependency_counts.get(DEPENDENCY_STALE, 0):
                        classification = "STALE"
                        contract_issues = ("dependency_stale",)
                data_extra["dependency_impact"] = dependency_impact
                data_extra["calculation_classification"] = classification
                data_extra["calculation_contract_issues"] = list(contract_issues)
                data_extra["source_navigation"] = _source_navigation(
                    row_data,
                    indexed_inputs,
                    versions=versions,
                    documents=documents_by_id,
                )
                # CalculationInput is the indexed view of the same calculation contract.  Its
                # provenance is evidence too; leaving it only inside ``data`` makes a citation
                # visible but makes the optional audit skip it.
                for input_row in indexed_inputs:
                    extra_entries.extend(_mapping_entries(input_row.get("provenance")))
            if table == "program_target":
                owner = program_by_id.get(str(getattr(row, "program_id", "") or ""))
                if owner is not None:
                    scope_extra.update(
                        {
                            "well_id": getattr(owner, "well_id", ""),
                            "field_id": getattr(owner, "field_id", ""),
                            "project_id": getattr(owner, "project_id", ""),
                        }
                    )
            row_records.append(
                _record_from_row(
                    row,
                    superseded_calculations=superseded_calculations,
                    current_programs=current_programs,
                    conflict_ids=conflict_by_item.get(str(row.id), ()),
                    extra_evidence=tuple(extra_entries),
                    data_extra=data_extra,
                    scope_extra=scope_extra,
                )
            )
        row_records.extend(
            _record_from_fact(fact, conflict_ids=conflict_by_item.get(str(fact.item_id), ()))
            for fact in facts
        )

        if request.lifecycle == REVIEW_CURRENT:
            row_records = [record for record in row_records if record.current]
            review_conflicts = [conflict for conflict in review_conflicts if conflict.current]
        row_records.sort(key=lambda record: (record.record_type, record.record_id))
        review_conflicts.sort(key=lambda conflict: (conflict.lookup_key, conflict.conflict_id))

        plan_actual = self._plan_actual(
            engineering,
            programs,
            well_id=str(well.id),
            lifecycle=request.lifecycle,
        )
        relation_rows = knowledge.relations_for_entity(
            EntityRef("well", str(well.id)), direction="both", limit=limit
        )
        relations = tuple(
            _edge_payload(row)
            for row in sorted(
                relation_rows,
                key=lambda row: (
                    str(row.relation),
                    str(row.source_id),
                    str(row.target_id),
                    str(row.id),
                ),
            )
        )
        bounded_sizes.append(len(relation_rows))
        truncated = sections_truncated or any(size >= limit for size in bounded_sizes)
        if len(row_records) > result_limit:
            row_records = row_records[:result_limit]
            truncated = True
        if len(review_conflicts) > result_limit:
            review_conflicts = review_conflicts[:result_limit]
            truncated = True
        if len(relations) > result_limit:
            relations = relations[:result_limit]
            truncated = True
        if len(plan_actual) > _SAFE_LIMIT:
            plan_actual = plan_actual[:_SAFE_LIMIT]
            truncated = True

        observations = self._observations(row_records, review_conflicts, truncated=truncated)
        return DomainReview(
            request=request.to_dict(),
            subject=context,
            sections=sections,
            records=tuple(row_records),
            conflicts=tuple(review_conflicts),
            relations=relations,
            plan_actual=tuple(plan_actual),
            observations=observations,
            truncated=truncated,
        )

    @staticmethod
    def _plan_actual(
        repository: EngineeringRepository,
        programs: Sequence[DrillingProgram],
        *,
        well_id: str,
        lifecycle: str,
    ) -> list[Mapping[str, Any]]:
        if lifecycle == REVIEW_CURRENT:
            return [dict(_plain(row)) for row in repository.plan_actual_summary(well_id=well_id)]
        rows: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
        for program in programs:
            for row in repository.plan_actual_summary(program_id=str(program.id)):
                payload = dict(_plain(row))
                # A field/project program can own targets for several wells.  The repository must
                # return those rows for a program-scoped question, but a well review is still one
                # subject.  Filtering by the authoritative section-side well id prevents history
                # from carrying another well's actuals beside this well's plan.
                if str(payload.get("well_id") or "") != str(well_id):
                    continue
                key = (
                    str(payload.get("section_id") or ""),
                    str(payload.get("metric") or ""),
                    str(payload.get("program_id") or ""),
                    str(payload.get("target_id") or ""),
                )
                rows.setdefault(key, payload)
        return [rows[key] for key in sorted(rows)]

    @staticmethod
    def _observations(
        records: Sequence[ReviewRecord],
        conflicts: Sequence[ReviewConflict],
        *,
        truncated: bool,
    ) -> dict[str, Any]:
        by_type = Counter(record.record_type for record in records)
        by_status = Counter(record.status for record in records if record.status)
        by_flag = Counter(flag for record in records for flag in record.flags)
        return {
            "records": len(records),
            "by_type": dict(sorted(by_type.items())),
            "by_status": dict(sorted(by_status.items())),
            "current_records": by_flag.get("current", 0),
            "historical_records": by_flag.get("historical", 0),
            "candidate_records": by_flag.get("candidate", 0),
            "pending_review_records": by_flag.get("pending_review", 0),
            "explicitly_reviewed_records": by_flag.get("reviewed", 0),
            "conflicting_records": by_flag.get("conflicting", 0),
            "records_without_recorded_provenance": by_flag.get("no_recorded_provenance", 0),
            "source_derived_without_provenance": by_flag.get("unverifiable_source", 0),
            "unsupported_promotions": by_flag.get("promotion_unsupported", 0),
            "non_certified_promotions": by_flag.get("promotion_not_certified", 0),
            "failed_processing_records": by_flag.get("processing_failed", 0),
            "missing_extraction_artefacts": by_flag.get("missing_extraction_artefact", 0),
            "degraded_extractions": by_flag.get("extraction_degraded", 0),
            "executable_calculations": by_flag.get("calculation_executable", 0),
            "stored_only_calculations": by_flag.get("calculation_stored_only", 0),
            "incomplete_calculations": by_flag.get("calculation_contract_incomplete", 0),
            "stale_calculations": by_flag.get("calculation_stale_dependency", 0),
            "unresolved_calculations": by_flag.get("calculation_unresolved_dependency", 0),
            "open_conflicts": sum(1 for conflict in conflicts if conflict.current),
            "search_sidecar_used": False,
            "truncated": truncated,
        }

    def _audit(self, review: DomainReview) -> DomainReview:
        items = tuple(
            PackageEvidence(item=self._evidence_item(record), found_by=())
            for record in review.records
        )
        identity = "review:" + sha256_obj(
            {
                "request": dict(review.request),
                "records": sorted(_identity_for_record(record) for record in review.records),
            }
        )
        package = EvidencePackage(
            identity=identity,
            request=dict(review.request),
            items=items,
            policy=str(review.request.get("lifecycle") or REVIEW_CURRENT),
            scope=dict(review.subject.get("scope") or {}),
        )
        report = CitationAuditor.for_workspace(self._workspace).audit(package)
        checks = {check.identity: check.status for check in report.checks}
        records = tuple(
            replace(
                record,
                verification=replace(
                    record.verification,
                    citation_audit=checks.get(_identity_for_record(record), "NOT_CHECKABLE"),
                ),
            )
            for record in review.records
        )
        observations = dict(review.observations)
        observations["citation_audit"] = report.counts
        return replace(
            review,
            records=records,
            observations=observations,
            citation_audit=report.to_dict(),
        )

    @staticmethod
    def _evidence_item(record: ReviewRecord) -> Any:
        from ..retrieval.contract import SOURCE_STRUCTURED, EvidenceItem

        data = record.data
        title = str(
            data.get("title")
            or data.get("filename")
            or data.get("predicate")
            or data.get("record_type")
            or record.record_type
        )
        text = str(
            data.get("description")
            or data.get("summary")
            or data.get("content")
            or data.get("lesson")
            or data.get("statement")
            or data.get("text")
            or ""
        )
        record_date = str(
            data.get("occurred_at")
            or data.get("started_at")
            or data.get("report_date")
            or data.get("approved_at")
            or ""
        )
        return EvidenceItem(
            identity=_identity_for_record(record),
            source_type=SOURCE_STRUCTURED,
            record_type=record.record_type,
            source_id=record.record_id,
            well_id=record.scope.get("well_id", ""),
            field_id=record.scope.get("field_id", ""),
            project_id=record.scope.get("project_id", ""),
            status=record.status,
            current=record.current,
            document_id=str(data.get("document_id") or ""),
            document_version_id=str(data.get("document_version_id") or ""),
            # Structured review items are audited through the existing structured-row path.  Carry
            # every file citation already present in the row's evidence graph, including provenance
            # nested on relation entries; otherwise ``review`` would display a citation that its
            # optional audit never checked.
            provenance=[dict(entry) for entry in _citation_entries(record.evidence)],
            title=title,
            text=text,
            record_date=record_date,
            verified=True,
        )
