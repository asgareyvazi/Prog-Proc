"""Optional citation verification for a report that has already been built.

The manifest says what the pack cites. This module, only when asked, resolves retrieval-eligible
sample identities through :meth:`RetrievalService.resolve_structured` and re-reads file citations
through :meth:`CitationAuditor.audit_items`. It does not rebuild the pack, does not invent an
``EvidencePackage``, and does not treat a matching number as a citation.

A completed audit with no mismatch is not ``FULLY_VERIFIED``. Aggregate methods, truncated
samples, unresolved ids, omitted checks and rows with no file citation stay visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from ..core.errors import DrillingIntelligenceError
from ..core.hashing import sha256_obj
from ..evidence.verify import (
    STATUS_MISMATCH,
    STATUS_NOT_CHECKABLE,
    STATUS_UNREADABLE,
    CitationAuditor,
)
from ..retrieval.service import RetrievalService
from .lineage import REL_AGGREGATE, REL_COUNT_ONLY, REL_TRUNCATED, traceability_manifest

AUDIT_SCHEMA = "report-evidence-audit/1"
AUDITED_SCHEMA = "engineering-report-audited/1"

# A report can cite more sample ids than a read-only check should open in one pass.
# The cap is applied before resolution. Omitted ids are counted, never silently dropped.
AUDIT_CAP = 40

OVERALL_FULL = "FULLY_VERIFIED"
OVERALL_PARTIAL = "PARTIALLY_VERIFIED"
OVERALL_FAILED = "FAILED"
OVERALL_INCOMPLETE = "INCOMPLETE"

RES_RESOLVED = "RESOLVED"
RES_UNRESOLVED = "UNRESOLVED"
RES_AGGREGATE = "AGGREGATE"
RES_NOT_ON_PATH = "NOT_ON_RETRIEVAL_PATH"
RES_OMITTED = "OMITTED_BY_CAP"
RES_SCOPE = "SCOPE_MISMATCH"
RES_NOT_ATTEMPTED = "NOT_ATTEMPTED"

_STRUCTURED_PREFIX = "structured:"


def safe_detail(text: str) -> str:
    """Drop host paths from a verifier diagnostic. The filename, if any, remains."""
    kept: list[str] = []
    for token in str(text or "").split():
        if "/" in token or "\\" in token:
            leaf = token.replace("\\", "/").rstrip(".,;:").split("/")[-1]
            kept.append(leaf or "[path]")
        else:
            kept.append(token)
    return " ".join(kept)


def overall_status(entries: list[dict[str, Any]], *, omitted: int, incomplete: bool) -> str:
    """The audit's own status. A quiet result is not a pass."""
    if incomplete:
        return OVERALL_INCOMPLETE
    statuses = [str(item.get("citation_status") or "") for item in entries]
    resolutions = [str(item.get("resolution") or "") for item in entries]
    if any(status in {STATUS_MISMATCH, STATUS_UNREADABLE} for status in statuses):
        return OVERALL_FAILED
    if omitted or any(
        resolution in {RES_UNRESOLVED, RES_OMITTED, RES_SCOPE, RES_NOT_ATTEMPTED}
        for resolution in resolutions
    ):
        return OVERALL_PARTIAL
    if any(status == STATUS_NOT_CHECKABLE for status in statuses) or any(
        resolution in {RES_AGGREGATE, RES_NOT_ON_PATH} for resolution in resolutions
    ):
        return OVERALL_PARTIAL
    if entries and all(status == "MATCH" for status in statuses):
        return OVERALL_FULL
    return OVERALL_PARTIAL


@dataclass(frozen=True)
class ReportEvidenceAudit:
    schema: str
    report_identity: str
    manifest_identity: str
    cap: int
    eligible: int
    attempted: int
    completed: int
    omitted: int
    overall: str
    counts: dict[str, int]
    entries: tuple[dict[str, Any], ...]
    identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema": self.schema,
            "report_identity": self.report_identity,
            "manifest_identity": self.manifest_identity,
            "cap": self.cap,
            "eligible": self.eligible,
            "attempted": self.attempted,
            "completed": self.completed,
            "omitted": self.omitted,
            "overall": self.overall,
            "counts": dict(sorted(self.counts.items())),
            "entries": [dict(item) for item in self.entries],
        }
        body["identity"] = self.identity or sha256_obj(
            {key: value for key, value in body.items() if key != "identity"}
        )
        return body


def _structured_identity(ref: dict[str, Any], sample_id: str) -> str:
    token = str(sample_id or "")
    if token.startswith(_STRUCTURED_PREFIX) and token.count(":") >= 2:
        return token
    if str(ref.get("identity_kind") or ref.get("identity") or "") != "structured":
        return ""
    domain = str(ref.get("domain") or "")
    if not domain or not token or ":" in token:
        return ""
    return f"structured:{domain}:{token}"


def _subject_wells(payload: dict[str, Any]) -> set[str]:
    subject = payload.get("subject") if isinstance(payload.get("subject"), dict) else {}
    wells = subject.get("subjects") if isinstance(subject, dict) else ()
    found: set[str] = set()
    for well in wells or ():
        if isinstance(well, dict) and well.get("well_id"):
            found.add(str(well["well_id"]))
        elif isinstance(well, str) and well:
            found.add(well)
    return found


def verify_report_citations(
    workspace: Any,
    payload: dict[str, Any],
    *,
    session: Any | None = None,
    cap: int = AUDIT_CAP,
) -> ReportEvidenceAudit:
    """Resolve and check the report's retrieval-eligible samples. Read-only."""
    report = dict(payload)
    manifest = traceability_manifest(report)
    bound = max(0, int(cap))
    subjects = _subject_wells(report)
    eligible: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for source in manifest.sources:
        relationship = str(source.get("relationship") or "")
        if relationship in {REL_AGGREGATE, REL_COUNT_ONLY} and not source.get("resolvable"):
            continue
        for sample_id in source.get("sample") or ():
            identity = _structured_identity(source, str(sample_id))
            if not identity or identity in seen:
                continue
            seen.add(identity)
            eligible.append((str(source["ref_id"]), str(sample_id), identity))
    attempted_keys = eligible[:bound]
    omitted_keys = eligible[bound:]
    retrieval = RetrievalService.for_workspace(workspace)
    try:
        resolved = retrieval.resolve_structured(
            [identity for _ref, _sample, identity in attempted_keys], session=session
        )
        items = [item for item in resolved.values() if item is not None]
        checks = CitationAuditor.for_workspace(workspace).audit_items(items) if items else ()
    except (OSError, SQLAlchemyError, DrillingIntelligenceError) as exc:
        return _incomplete(report, manifest, bound, len(eligible), len(attempted_keys), exc)
    by_identity = {check.identity: check for check in checks}
    entries: list[dict[str, Any]] = []
    for ref_id, sample_id, identity in attempted_keys:
        item = resolved.get(identity)
        if item is None:
            entries.append(
                _entry(
                    ref_id,
                    sample_id,
                    identity,
                    RES_UNRESOLVED,
                    "NOT_ATTEMPTED",
                    "the sample identity is not in the authoritative tables",
                )
            )
            continue
        well_id = str(item.well_id or "")
        if subjects and well_id and well_id not in subjects:
            entries.append(
                _entry(
                    ref_id,
                    sample_id,
                    identity,
                    RES_SCOPE,
                    "NOT_CHECKABLE",
                    "resolved row is outside the report's subject wells; not attached as support",
                    well_id=well_id,
                )
            )
            continue
        check = by_identity.get(identity)
        citation = check.status if check is not None else "NOT_CHECKABLE"
        detail = safe_detail(check.detail if check is not None else "")
        if not well_id:
            detail = (detail + "; " if detail else "") + "site-scoped row; not a well record"
        entries.append(
            _entry(
                ref_id,
                sample_id,
                identity,
                RES_RESOLVED,
                citation,
                detail,
                well_id=well_id,
                current=bool(item.current),
                expected=check.expected_sha256 if check is not None else "",
                actual=check.actual_sha256 if check is not None else "",
            )
        )
    for ref_id, sample_id, identity in omitted_keys:
        entries.append(
            _entry(
                ref_id,
                sample_id,
                identity,
                RES_OMITTED,
                "NOT_ATTEMPTED",
                f"omitted by the audit cap of {bound}",
            )
        )
    for source in manifest.sources:
        relationship = str(source.get("relationship") or "")
        if relationship == REL_AGGREGATE:
            entries.append(
                _entry(
                    str(source["ref_id"]),
                    "",
                    "",
                    RES_AGGREGATE,
                    "NOT_CHECKABLE",
                    "method and scope only; individual rows were not verified",
                    method=str(source.get("method") or ""),
                )
            )
        elif relationship in {REL_COUNT_ONLY, REL_TRUNCATED} and not source.get("resolvable"):
            entries.append(
                _entry(
                    str(source["ref_id"]),
                    "",
                    "",
                    RES_NOT_ON_PATH,
                    "NOT_CHECKABLE",
                    "recorded reference is not on the retrieval-resolvable path",
                )
            )
        elif relationship == REL_TRUNCATED and source.get("resolvable"):
            entries.append(
                _entry(
                    str(source["ref_id"]),
                    "",
                    "",
                    RES_NOT_ON_PATH,
                    "NOT_CHECKABLE",
                    "sample is truncated; unchecked rows are not verified",
                )
            )
        elif not source.get("resolvable"):
            entries.append(
                _entry(
                    str(source["ref_id"]),
                    "",
                    "",
                    RES_NOT_ON_PATH,
                    "NOT_CHECKABLE",
                    "recorded reference is not on the retrieval-resolvable path",
                )
            )
    counts: dict[str, int] = {}
    for entry in entries:
        key = f"{entry['resolution']}:{entry['citation_status']}"
        counts[key] = counts.get(key, 0) + 1
    completed = sum(1 for entry in entries if entry["resolution"] == RES_RESOLVED)
    overall = overall_status(entries, omitted=len(omitted_keys), incomplete=False)
    return ReportEvidenceAudit(
        schema=AUDIT_SCHEMA,
        report_identity=str(report.get("identity") or ""),
        manifest_identity=manifest.identity,
        cap=bound,
        eligible=len(eligible),
        attempted=len(attempted_keys),
        completed=completed,
        omitted=len(omitted_keys),
        overall=overall,
        counts=counts,
        entries=tuple(entries),
    )


def audited_payload(
    report: dict[str, Any], manifest: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any]:
    """The versioned response used only when citation verification was requested."""
    return {
        "schema": AUDITED_SCHEMA,
        "report": report,
        "traceability": manifest,
        "audit": audit,
    }


def _entry(
    ref_id: str,
    sample_id: str,
    identity: str,
    resolution: str,
    citation_status: str,
    detail: str,
    *,
    well_id: str = "",
    current: bool | None = None,
    expected: str = "",
    actual: str = "",
    method: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ref_id": ref_id,
        "sample_id": sample_id,
        "identity": identity,
        "resolution": resolution,
        "citation_status": citation_status,
        "detail": detail,
        "well_id": well_id,
        "expected_sha256": expected,
        "actual_sha256": actual,
    }
    if current is not None:
        body["current"] = current
    if method:
        body["method"] = method
    return body


def _incomplete(
    report: dict[str, Any],
    manifest: Any,
    cap: int,
    eligible: int,
    attempted: int,
    exc: BaseException,
) -> ReportEvidenceAudit:
    detail = safe_detail(f"{type(exc).__name__}: {exc}")
    entry = _entry("", "", "", RES_NOT_ATTEMPTED, "NOT_ATTEMPTED", detail)
    return ReportEvidenceAudit(
        schema=AUDIT_SCHEMA,
        report_identity=str(report.get("identity") or ""),
        manifest_identity=str(getattr(manifest, "identity", "") or ""),
        cap=cap,
        eligible=eligible,
        attempted=attempted,
        completed=0,
        omitted=0,
        overall=OVERALL_INCOMPLETE,
        counts={"NOT_ATTEMPTED:NOT_ATTEMPTED": 1},
        entries=(entry,),
    )
