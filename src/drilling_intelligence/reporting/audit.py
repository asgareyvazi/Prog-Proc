"""Optional citation verification for a report that has already been built.

The manifest says what the pack cites. This module, only when asked, resolves retrieval-eligible
sample identities through :meth:`RetrievalService.resolve_structured` and re-reads file citations
through :meth:`CitationAuditor.audit_items`. It does not rebuild the pack, does not invent an
``EvidencePackage``, and does not treat a matching number as a citation.

A citation match is one fact. It does not make a record current, in scope, the right domain, or
complete coverage. Those are separate fields, and the overall status reads all of them.

Precedence, applied the same way by every caller:

1. ``FAILED`` if any completed citation is ``MISMATCH`` or ``UNREADABLE``. A detected integrity
   failure is conclusive. It outranks a later operational failure so a mismatch cannot hide
   behind ``INCOMPLETE``.
2. ``INCOMPLETE`` if an operational failure prevented a conclusive audit and no integrity failure
   was completed. Completed outcomes that were already known stay on the audit.
3. ``PARTIALLY_VERIFIED`` if the audit finished with no integrity failure, but coverage, scope,
   lifecycle, domain, or citation checkability is incomplete. An empty eligible list is this
   state, not a pass.
4. ``FULLY_VERIFIED`` only when every required reference resolved in the expected domain and
   subject scope, is current for a current-state claim, has a checkable citation that matched,
   and is a complete enumeration rather than an aggregate, sample, or truncation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from ..core.errors import DrillingIntelligenceError
from ..core.hashing import sha256_obj
from ..evidence.verify import (
    STATUS_MATCH,
    STATUS_MISMATCH,
    STATUS_UNREADABLE,
    CitationAuditor,
)
from ..retrieval.service import RetrievalService
from ..search.structured import STRUCTURED_RECORD_TYPES, structured_record_id
from .lineage import (
    REL_AGGREGATE,
    REL_COUNT_ONLY,
    REL_DIRECT,
    REL_ENUMERATED,
    REL_SAMPLE,
    REL_TRUNCATED,
    traceability_manifest,
)

AUDIT_SCHEMA = "report-evidence-audit/1"
AUDITED_SCHEMA = "engineering-report-audited/1"

# Unique retrieval identities, and the citation checks those identities can start.
# Both limits are the same number: a citation check is not started for an identity
# that was not resolved, and resolution is not started past the cap.
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
RES_DOMAIN = "DOMAIN_MISMATCH"
RES_MALFORMED = "MALFORMED_IDENTITY"
RES_INCONSISTENT = "INCONSISTENT_ITEM"
RES_NOT_ATTEMPTED = "NOT_ATTEMPTED"

SCOPE_IN = "IN_SCOPE"
SCOPE_OUT = "OUT_OF_SCOPE"
SCOPE_SITE = "SITE_LEVEL"
SCOPE_UNKNOWN = "UNKNOWN"
SCOPE_NA = "NOT_APPLICABLE"

LIFE_CURRENT = "CURRENT"
LIFE_NOT_CURRENT = "NOT_CURRENT"
LIFE_UNKNOWN = "UNKNOWN"
LIFE_NA = "NOT_APPLICABLE"
LIFE_HISTORICAL = "HISTORICAL_WINDOW"

DOMAIN_AGREE = "AGREE"
DOMAIN_MISMATCH = "MISMATCH"
DOMAIN_NA = "NOT_APPLICABLE"
DOMAIN_UNSUPPORTED = "UNSUPPORTED"

# Windowed operational domains. A date window makes a non-current row a historical
# source for these domains only. It does not make the row current, and it does not
# apply to cost, risk, learning, or any other current-state section.
_WINDOWED_DOMAINS = frozenset(
    {"npt_record", "problem_occurrence", "well_control_event", "hse_incident"}
)
_COMPLETE_RELATIONSHIPS = frozenset({REL_DIRECT, REL_ENUMERATED})

_PATH = re.compile(
    r"(?ix)"
    r"(?:file://+[^\s\"'<>|]+"
    r"|\\\\[^\s\"'<>|]+"
    r"|[A-Za-z]:[\\/][^\s\"'<>|]+"
    r"|/(?:[^\s/\"'<>|]+/)+[^\s\"'<>|,;:)]+)"
)


def safe_detail(text: str) -> str:
    """Replace host paths with a filename. The diagnostic stays; the machine path does not."""

    def _leaf(match: re.Match[str]) -> str:
        token = match.group(0).replace("\\", "/")
        if token.lower().startswith("file:"):
            token = token.split("://", 1)[-1]
        leaf = token.rstrip("/").split("/")[-1]
        return leaf or "[path]"

    return _PATH.sub(_leaf, str(text or ""))


def audit_exit_code(overall: str) -> int:
    """CLI exit for an audit. Partial is a completed honest result, not a clean pass.

    ``FAILED`` and ``INCOMPLETE`` are non-zero so a pipeline cannot treat them as success.
    ``PARTIALLY_VERIFIED`` stays zero because the command finished and the JSON says the
    coverage is incomplete. The overall field, not the exit code, is the coverage claim.
    """
    return 1 if overall in {OVERALL_FAILED, OVERALL_INCOMPLETE} else 0


def overall_status(entries: list[dict[str, Any]], *, omitted: int, incomplete: bool) -> str:
    """Derive one status from every dimension. A quiet citation list is not a pass."""
    statuses = [str(item.get("citation_status") or "") for item in entries]
    if any(status in {STATUS_MISMATCH, STATUS_UNREADABLE} for status in statuses):
        return OVERALL_FAILED
    if incomplete:
        return OVERALL_INCOMPLETE
    if omitted or not entries or not _all_fully_covered(entries):
        return OVERALL_PARTIAL
    return OVERALL_FULL


def _all_fully_covered(entries: list[dict[str, Any]]) -> bool:
    for item in entries:
        if str(item.get("citation_status") or "") != STATUS_MATCH:
            return False
        if str(item.get("resolution") or "") != RES_RESOLVED:
            return False
        if str(item.get("scope_status") or "") != SCOPE_IN:
            return False
        if str(item.get("domain_status") or "") != DOMAIN_AGREE:
            return False
        lifecycle = str(item.get("lifecycle") or "")
        if lifecycle == LIFE_CURRENT and item.get("current") is not True:
            return False
        if lifecycle == LIFE_HISTORICAL and item.get("current") is not False:
            return False
        if lifecycle not in {LIFE_CURRENT, LIFE_HISTORICAL}:
            return False
        relationship = str(item.get("relationship") or "")
        if relationship and relationship not in _COMPLETE_RELATIONSHIPS:
            return False
        if str(item.get("coverage") or "COMPLETE") not in {"COMPLETE", ""}:
            return False
    return True


def report_subject_wells(payload: dict[str, Any]) -> set[str]:
    """Wells the report actually selected. An empty set is unknown scope, not every well."""
    subject = payload.get("subject") if isinstance(payload.get("subject"), dict) else {}
    found: set[str] = set()
    if isinstance(subject, dict):
        for well in subject.get("subjects") or ():
            if isinstance(well, dict) and well.get("well_id"):
                found.add(str(well["well_id"]))
            elif isinstance(well, str) and well:
                found.add(well)
        if str(subject.get("kind") or "") == "well" and subject.get("id"):
            found.add(str(subject["id"]))
        anchor = subject.get("anchor")
        if isinstance(anchor, dict) and anchor.get("well_id"):
            found.add(str(anchor["well_id"]))
        elif isinstance(anchor, str) and anchor and subject.get("basis_kind") == "well":
            found.add(anchor)
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    if isinstance(request, dict):
        for well_id in request.get("well_ids") or ():
            if well_id:
                found.add(str(well_id))
    return found


def window_applied(payload: dict[str, Any]) -> bool:
    subject = payload.get("subject") if isinstance(payload.get("subject"), dict) else {}
    window = subject.get("window") if isinstance(subject, dict) else None
    if isinstance(window, dict) and window.get("applied"):
        return True
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    return bool(isinstance(request, dict) and (request.get("since") or request.get("until")))


def expects_current(payload: dict[str, Any], domain: str) -> bool:
    """Whether a non-current row blocks full verification for this report and domain.

    A date window licenses historical rows only for the windowed operational domains.
    It does not make those rows current, and it does not relax any other domain.
    """
    return not (window_applied(payload) and domain in _WINDOWED_DOMAINS)


def classify_sample(ref: dict[str, Any], sample_id: str) -> dict[str, str]:
    """Validate one sample against its evidence reference. Does not look up a row.

    A ``structured:<domain>:<id>`` token is trusted only when the embedded domain is
    the reference's domain and that domain is on the retrieval path. A matching prefix
    is not a license to read a different table.
    """
    token = str(sample_id or "")
    domain = str(ref.get("domain") or "")
    identity_kind = str(ref.get("identity_kind") or ref.get("identity") or "")
    if token.startswith("structured:"):
        prefix, sep, rest = token.partition(":")
        record_type, sep2, source_id = rest.partition(":")
        if (
            prefix != "structured"
            or not sep
            or not sep2
            or not record_type
            or not source_id.strip()
        ):
            return {
                "action": "malformed",
                "identity": token,
                "reason": "malformed structured identity",
            }
        if domain and record_type != domain:
            return {
                "action": "domain_mismatch",
                "identity": token,
                "record_type": record_type,
                "reason": "embedded domain does not match the evidence reference",
            }
        if not domain:
            return {
                "action": "domain_mismatch",
                "identity": token,
                "record_type": record_type,
                "reason": "evidence reference has no domain to agree with",
            }
        if record_type not in STRUCTURED_RECORD_TYPES:
            return {
                "action": "unsupported",
                "identity": token,
                "record_type": record_type,
                "reason": "domain is not on the retrieval path",
            }
        return {
            "action": "resolve",
            "identity": structured_record_id(record_type, source_id),
            "record_type": record_type,
            "source_id": source_id,
        }
    if identity_kind == "structured":
        if not domain or not token or ":" in token:
            return {
                "action": "malformed",
                "identity": token,
                "reason": "bare structured id is incomplete",
            }
        if domain not in STRUCTURED_RECORD_TYPES:
            return {
                "action": "unsupported",
                "identity": token,
                "record_type": domain,
                "reason": "domain is not on the retrieval path",
            }
        return {
            "action": "resolve",
            "identity": structured_record_id(domain, token),
            "record_type": domain,
            "source_id": token,
        }
    return {"action": "not_on_path", "identity": token, "reason": "not a retrieval identity"}


def item_agrees(item: Any, identity: str) -> bool:
    """The resolved object is the requested row, not merely a mapping hit."""
    _prefix, _sep, rest = str(identity).partition(":")
    record_type, _sep2, source_id = rest.partition(":")
    return (
        str(getattr(item, "identity", "") or "") == identity
        and str(getattr(item, "record_type", "") or "") == record_type
        and str(getattr(item, "source_id", "") or "") == source_id
    )


@dataclass(frozen=True)
class ReportEvidenceAudit:
    schema: str
    report_identity: str
    manifest_identity: str
    cap: int
    check_cap: int
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
            "check_cap": self.check_cap,
            "eligible": self.eligible,
            "attempted": self.attempted,
            "completed": self.completed,
            "omitted": self.omitted,
            "overall": self.overall,
            "counts": dict(sorted(self.counts.items())),
            "entries": [dict(item) for item in self.entries],
            "precedence": ["FAILED", "INCOMPLETE", "PARTIALLY_VERIFIED", "FULLY_VERIFIED"],
        }
        body["identity"] = self.identity or sha256_obj(
            {key: value for key, value in body.items() if key != "identity"}
        )
        return body


def verify_report_citations(
    workspace: Any,
    payload: dict[str, Any],
    *,
    session: Any | None = None,
    cap: int = AUDIT_CAP,
) -> ReportEvidenceAudit:
    """Resolve and check the report's retrieval-eligible samples. Read-only.

    The cap is applied to unique retrieval identities before any resolve or file read.
    Repeating an identity across references does not spend the cap twice and does not
    hide the second reference. A cross-domain token is not sent to retrieval.
    """
    report = dict(payload)
    manifest = traceability_manifest(report)
    bound = max(0, int(cap))
    subjects = report_subject_wells(report)
    pairs: list[tuple[dict[str, Any], str, dict[str, str]]] = []
    retrieval_ids: list[str] = []
    seen: set[str] = set()
    for source in manifest.sources:
        for sample_id in source.get("sample") or ():
            classified = classify_sample(source, str(sample_id))
            pairs.append((source, str(sample_id), classified))
            if classified.get("action") != "resolve":
                continue
            identity = classified["identity"]
            if identity in seen:
                continue
            seen.add(identity)
            retrieval_ids.append(identity)
    attempted_ids = retrieval_ids[:bound]
    omitted_ids = set(retrieval_ids[bound:])
    attempted_set = set(attempted_ids)
    retrieval = RetrievalService.for_workspace(workspace)
    resolved: dict[str, Any] = {}
    checks: tuple[Any, ...] | None = ()
    failure: BaseException | None = None
    if attempted_ids:
        try:
            resolved = retrieval.resolve_structured(attempted_ids, session=session)
        except (OSError, SQLAlchemyError, DrillingIntelligenceError) as exc:
            failure = exc
            resolved = {}
            checks = None
    if checks is not None:
        items = [
            item
            for identity in attempted_ids
            if (item := resolved.get(identity)) is not None and item_agrees(item, identity)
        ]
        if items:
            try:
                checks = CitationAuditor.for_workspace(workspace).audit_items(items)
            except (OSError, SQLAlchemyError, DrillingIntelligenceError) as exc:
                failure = exc
                checks = None
    by_identity = {check.identity: check for check in (checks or ())}
    entries: list[dict[str, Any]] = []
    seen_ref_notes: set[tuple[str, str]] = set()
    for source, sample_id, classified in pairs:
        action = classified.get("action")
        identity = str(classified.get("identity") or "")
        relationship = str(source.get("relationship") or "")
        if action == "resolve" and identity in omitted_ids:
            entries.append(
                _sample_entry(
                    source,
                    sample_id,
                    identity,
                    resolution=RES_OMITTED,
                    citation="NOT_ATTEMPTED",
                    detail=f"omitted by the audit cap of {bound}",
                    scope_status=SCOPE_NA,
                    lifecycle=LIFE_NA,
                    domain_status=DOMAIN_NA,
                )
            )
            continue
        if (
            action == "resolve"
            and failure is not None
            and not resolved
            and identity in attempted_set
        ):
            entries.append(
                _sample_entry(
                    source,
                    sample_id,
                    identity,
                    resolution=RES_NOT_ATTEMPTED,
                    citation="NOT_ATTEMPTED",
                    detail=safe_detail(f"{type(failure).__name__}: {failure}"),
                    scope_status=SCOPE_NA,
                    lifecycle=LIFE_UNKNOWN,
                    domain_status=DOMAIN_NA,
                )
            )
            continue
        if action != "resolve":
            entries.append(_classified_entry(source, sample_id, classified))
            continue
        item = resolved.get(identity)
        if item is None:
            entries.append(
                _sample_entry(
                    source,
                    sample_id,
                    identity,
                    resolution=RES_UNRESOLVED,
                    citation="NOT_ATTEMPTED",
                    detail="the sample identity is not in the authoritative tables",
                    scope_status=SCOPE_UNKNOWN,
                    lifecycle=LIFE_UNKNOWN,
                    domain_status=DOMAIN_AGREE,
                )
            )
            continue
        if not item_agrees(item, identity):
            entries.append(
                _sample_entry(
                    source,
                    sample_id,
                    identity,
                    resolution=RES_INCONSISTENT,
                    citation="NOT_CHECKABLE",
                    detail="resolver returned an item that is not the requested identity",
                    scope_status=SCOPE_UNKNOWN,
                    lifecycle=LIFE_UNKNOWN,
                    domain_status=DOMAIN_MISMATCH,
                )
            )
            continue
        scope_status, scope_detail, well_id = _scope_of(item, source, subjects)
        lifecycle, current = _lifecycle_of(item, report, str(source.get("domain") or ""))
        check = by_identity.get(identity)
        check_completed = False
        if failure is not None and checks is None:
            citation = "NOT_ATTEMPTED"
            detail = safe_detail(f"{type(failure).__name__}: {failure}")
        elif check is None:
            citation = "NOT_CHECKABLE"
            detail = "no citation check was recorded for the resolved row"
        else:
            citation = check.status
            detail = safe_detail(check.detail)
            check_completed = True
        if scope_detail:
            detail = (detail + "; " if detail else "") + scope_detail
        if lifecycle == LIFE_NOT_CURRENT:
            detail = (
                detail + "; " if detail else ""
            ) + "row is not current; a citation match does not make it current"
        entries.append(
            _sample_entry(
                source,
                sample_id,
                identity,
                resolution=RES_SCOPE if scope_status == SCOPE_OUT else RES_RESOLVED,
                citation=citation,
                detail=detail,
                scope_status=scope_status,
                lifecycle=lifecycle,
                domain_status=DOMAIN_AGREE,
                well_id=well_id,
                current=current,
                status=str(getattr(item, "status", "") or ""),
                expected=check.expected_sha256 if check is not None else "",
                actual=check.actual_sha256 if check is not None else "",
                relationship=relationship,
                check_completed=check_completed,
            )
        )
    for source in manifest.sources:
        relationship = str(source.get("relationship") or "")
        note = _coverage_note(relationship, bool(source.get("resolvable")))
        if note is None:
            continue
        key = (str(source["ref_id"]), note[0])
        if key in seen_ref_notes:
            continue
        seen_ref_notes.add(key)
        entries.append(
            _sample_entry(
                source,
                "",
                "",
                resolution=note[0],
                citation="NOT_CHECKABLE",
                detail=note[1],
                scope_status=SCOPE_NA,
                lifecycle=LIFE_NA,
                domain_status=DOMAIN_NA,
                relationship=relationship,
                method=str(source.get("method") or ""),
            )
        )
    if failure is not None and not entries:
        entries.append(
            _entry(
                "",
                "",
                "",
                RES_NOT_ATTEMPTED,
                "NOT_ATTEMPTED",
                safe_detail(f"{type(failure).__name__}: {failure}"),
                scope_status=SCOPE_NA,
                lifecycle=LIFE_UNKNOWN,
                domain_status=DOMAIN_NA,
            )
        )
    counts: dict[str, int] = {}
    for entry in entries:
        for key in (
            f"{entry['resolution']}:{entry['citation_status']}",
            f"scope:{entry.get('scope_status')}",
            f"lifecycle:{entry.get('lifecycle')}",
            f"citation:{entry.get('citation_status')}",
        ):
            counts[key] = counts.get(key, 0) + 1
    completed = len(
        {str(entry.get("identity") or "") for entry in entries if entry.get("check_completed")}
    )
    overall = overall_status(entries, omitted=len(omitted_ids), incomplete=failure is not None)
    return ReportEvidenceAudit(
        schema=AUDIT_SCHEMA,
        report_identity=str(report.get("identity") or ""),
        manifest_identity=manifest.identity,
        cap=bound,
        check_cap=bound,
        eligible=len(retrieval_ids),
        attempted=len(attempted_ids),
        completed=completed,
        omitted=len(omitted_ids),
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


def _coverage_note(relationship: str, resolvable: bool) -> tuple[str, str] | None:
    if relationship == REL_AGGREGATE:
        return (RES_AGGREGATE, "method and scope only; individual rows were not verified")
    if relationship == REL_TRUNCATED:
        return (RES_NOT_ON_PATH, "sample is truncated; unchecked rows are not verified")
    if relationship == REL_SAMPLE:
        return (RES_NOT_ON_PATH, "bounded sample; the unchecked remainder is not verified")
    if relationship == REL_COUNT_ONLY or (
        not resolvable and relationship not in _COMPLETE_RELATIONSHIPS
    ):
        if relationship in {REL_DIRECT, REL_ENUMERATED} and resolvable:
            return None
        if relationship in {REL_DIRECT, REL_ENUMERATED}:
            return (RES_NOT_ON_PATH, "recorded reference is not on the retrieval-resolvable path")
        if relationship == REL_COUNT_ONLY:
            return (RES_NOT_ON_PATH, "count only; no row identity was recorded")
    return None


def _classified_entry(
    source: dict[str, Any], sample_id: str, classified: dict[str, str]
) -> dict[str, Any]:
    action = classified.get("action")
    if action == "domain_mismatch":
        resolution, domain_status = RES_DOMAIN, DOMAIN_MISMATCH
    elif action == "malformed":
        resolution, domain_status = RES_MALFORMED, DOMAIN_NA
    elif action == "unsupported":
        resolution, domain_status = RES_NOT_ON_PATH, DOMAIN_UNSUPPORTED
    else:
        resolution, domain_status = RES_NOT_ON_PATH, DOMAIN_NA
    return _sample_entry(
        source,
        sample_id,
        str(classified.get("identity") or ""),
        resolution=resolution,
        citation="NOT_CHECKABLE",
        detail=str(classified.get("reason") or "not checkable"),
        scope_status=SCOPE_NA,
        lifecycle=LIFE_NA,
        domain_status=domain_status,
    )


def _scope_of(item: Any, source: dict[str, Any], subjects: set[str]) -> tuple[str, str, str]:
    well_id = str(getattr(item, "well_id", "") or "")
    ref_scope = source.get("scope") if isinstance(source.get("scope"), dict) else {}
    ref_well = str(ref_scope.get("well_id") or "") if isinstance(ref_scope, dict) else ""
    if not well_id:
        return (
            SCOPE_SITE,
            "site-scoped row; empty well id is not support for a selected well",
            "",
        )
    if ref_well and well_id != ref_well:
        return (
            SCOPE_OUT,
            "resolved row is outside the evidence reference's well",
            well_id,
        )
    if not subjects:
        return (SCOPE_UNKNOWN, "report has no selected well scope to test", well_id)
    if well_id not in subjects:
        return (
            SCOPE_OUT,
            "resolved row is outside the report's subject wells; not attached as support",
            well_id,
        )
    return (SCOPE_IN, "", well_id)


def _lifecycle_of(item: Any, report: dict[str, Any], domain: str) -> tuple[str, bool | None]:
    current = getattr(item, "current", None)
    if current is None:
        return LIFE_UNKNOWN, None
    current_flag = bool(current)
    if current_flag:
        return LIFE_CURRENT, True
    if not expects_current(report, domain):
        return LIFE_HISTORICAL, False
    return LIFE_NOT_CURRENT, False


def _sample_entry(
    source: dict[str, Any],
    sample_id: str,
    identity: str,
    *,
    resolution: str,
    citation: str,
    detail: str,
    scope_status: str,
    lifecycle: str,
    domain_status: str,
    well_id: str = "",
    current: bool | None = None,
    status: str = "",
    expected: str = "",
    actual: str = "",
    relationship: str = "",
    method: str = "",
    check_completed: bool = False,
) -> dict[str, Any]:
    relationship = relationship or str(source.get("relationship") or "")
    coverage = _coverage(relationship)
    return _entry(
        str(source.get("ref_id") or ""),
        sample_id,
        identity,
        resolution,
        citation,
        detail,
        well_id=well_id,
        current=current,
        expected=expected,
        actual=actual,
        method=method or str(source.get("method") or ""),
        scope_status=scope_status,
        lifecycle=lifecycle,
        domain_status=domain_status,
        relationship=relationship,
        coverage=coverage,
        status=status,
        record_domain=str(source.get("domain") or ""),
        check_completed=check_completed,
    )


def _coverage(relationship: str) -> str:
    if relationship in _COMPLETE_RELATIONSHIPS:
        return "COMPLETE"
    if relationship == REL_TRUNCATED:
        return "TRUNCATED"
    if relationship == REL_SAMPLE:
        return "SAMPLED"
    if relationship == REL_AGGREGATE:
        return "AGGREGATE"
    if relationship == REL_COUNT_ONLY:
        return "COUNT_ONLY"
    return "NONE"


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
    scope_status: str = SCOPE_NA,
    lifecycle: str = LIFE_NA,
    domain_status: str = DOMAIN_NA,
    relationship: str = "",
    coverage: str = "",
    status: str = "",
    record_domain: str = "",
    check_completed: bool = False,
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
        "scope_status": scope_status,
        "lifecycle": lifecycle,
        "domain_status": domain_status,
        "relationship": relationship,
        "coverage": coverage,
        "record_domain": record_domain,
        "row_status": status,
        "check_completed": check_completed,
    }
    if current is not None:
        body["current"] = current
    if method:
        body["method"] = method
    return body
