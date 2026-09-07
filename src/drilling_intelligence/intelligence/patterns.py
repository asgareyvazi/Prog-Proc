"""Recurring problems: a deterministic grouping, and the snapshot of it a person can review.

A pattern is not a prediction.  It is a ``GROUP BY`` that somebody looked at: *"stuck pipe, 8½ in
hole, eleven times, six wells, first seen in April, last in June"* is a fact about rows that exist, and
it stays true or false depending only on whether the rows still say it.

Why a table, then, when a query can answer that?  Because a pattern a person has *reviewed* is a
different object from a grouping that happens to be runnable: the reviewed one needs to keep the numbers
as they were when it was accepted, needs to be linkable to the lessons and procedures that came out of
it, and needs to be able to say "this was true in June and the records have moved since".  So:

*   :func:`find_recurring` runs the query and returns candidate rows.  It writes nothing.
*   :func:`snapshot` persists one, storing its own ``query`` (the exact parameters) alongside the
    numbers - occurrences, distinct events, affected wells, the hours - and links the problem rows behind
    it as evidence.
*   :func:`staleness` re-runs the stored parameters and reports what moved - it does not silently
    rewrite the accepted numbers, because a pattern that quietly changes every night is unauditable.
*   A snapshot is never invented: the minimum occurrence and well counts are parameters with defaults
    (2 and 2), and a grouping below them is not a pattern but an anecdote.

The signature is a digest of the parameters, which is what makes re-running the same snapshot a
*re-check* of one row rather than the creation of a second one.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core.enums import ConfirmationStatus, KnowledgeRelationType
from ..core.errors import ValidationError
from ..core.hashing import sha256_obj
from ..core.ids import new_id
from ..core.lifecycle import CONFIRMATION_LIFECYCLE as CONFIRMATION
from ..database.integrity import create_knowledge_relation
from ..database.models import FieldPattern, ProblemOccurrence, Recommendation, Well
from ..intelligence.field import problem_hours

__all__ = [
    "DEFAULT_MIN_OCCURRENCES",
    "DEFAULT_MIN_WELLS",
    "MAX_LINKED_EVIDENCE",
    "find_recurring",
    "signature_for",
    "snapshot",
    "staleness",
]

#: What counts as recurring: two occurrences, on two different wells.  One well twice is a habit; one
#: well once is an event.  Both defaults are parameters of :func:`find_recurring` because a mature asset
#: and a two-well block legitimately ask different questions.
DEFAULT_MIN_OCCURRENCES = 2
DEFAULT_MIN_WELLS = 2
#: How many problem ids a snapshot keeps as evidence.  The count is stored in full; the ids are capped so
#: a pattern over nine hundred rows does not carry nine hundred of them in a JSON column.
MAX_LINKED_EVIDENCE = 20

#: The fields that define a pattern's identity.  Everything else is measurement.
_PATTERN_KEYS: tuple[str, ...] = (
    "field_id",
    "project_id",
    "problem_type",
    "hole_size_in",
    "section_id",
)


def signature_for(**parameters: Any) -> str:
    """The digest of a pattern's parameters, so re-running a snapshot finds the row it made before.

    Sorted and JSON-encoded before hashing, because two callers passing the same five values in
    different orders must land on the same pattern - and because a signature that changed when the
    parameter order changed would silently fork every pattern in the field on the day a query was
    rewritten.
    """
    payload = {key: _clean(parameters.get(key)) for key in (*_PATTERN_KEYS, "since", "until")}
    return sha256_obj(payload)[:32]


def _clean(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, float):
        return round(value, 4)
    return str(value) if not isinstance(value, (int, float, bool)) else value


def find_recurring(
    session: Session,
    *,
    field_id: str = "",
    project_id: str = "",
    problem_type: str = "",
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    min_wells: int = DEFAULT_MIN_WELLS,
    since: object = None,
    until: object = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Group the problem records into what recurs, by type and hole size.

    One grouped query over ``problem_occurrence``, plus one over its NPT hours: every number in the rows
    comes back from the database, including the first and last occurrence dates, which are ``None`` when
    the records carry no dates at all rather than being filled in with today.
    """
    if not (field_id or project_id):
        raise ValidationError(
            "a pattern query needs a field or a project",
            hint="a pattern is a statement about a place; pass field_id or project_id",
        )
    clauses: list[Any] = []
    if field_id:
        clauses.append(Well.field_id == field_id)
    if project_id:
        clauses.append(Well.project_id == project_id)
    well_scope = select(Well.id).where(or_(*clauses))
    statement = (
        select(
            ProblemOccurrence.problem_type,
            ProblemOccurrence.hole_size_in,
            func.count(ProblemOccurrence.id),
            func.count(func.distinct(ProblemOccurrence.well_id)),
            func.min(ProblemOccurrence.occurred_at),
            func.max(ProblemOccurrence.occurred_at),
        )
        .where(ProblemOccurrence.well_id.in_(well_scope))
        .group_by(ProblemOccurrence.problem_type, ProblemOccurrence.hole_size_in)
    )
    if problem_type:
        statement = statement.where(ProblemOccurrence.problem_type == problem_type)
    if since is not None or until is not None:
        window: list[Any] = []
        if since is not None:
            window.append(ProblemOccurrence.occurred_at >= _boundary(since))
        if until is not None:
            window.append(ProblemOccurrence.occurred_at <= _boundary(until, end=True))
        statement = statement.where(*window)
    event_statement = (
        select(
            ProblemOccurrence.problem_type,
            ProblemOccurrence.hole_size_in,
            func.count(func.distinct(ProblemOccurrence.event_id)),
        )
        .where(
            ProblemOccurrence.well_id.in_(well_scope),
            ProblemOccurrence.event_id.is_not(None),
        )
        .group_by(ProblemOccurrence.problem_type, ProblemOccurrence.hole_size_in)
    )
    events = {
        (str(type_value or ""), None if hole is None else round(float(hole), 4)): int(count)
        for type_value, hole, count in session.execute(event_statement)
    }
    rows = []
    for type_value, hole, occurrences, wells, first, last in session.execute(statement):
        if int(occurrences) < max(1, int(min_occurrences)) or int(wells) < max(1, int(min_wells)):
            continue
        rows.append(
            {
                "problem_type": str(type_value or "uncategorised"),
                "hole_size_in": None if hole is None else float(hole),
                "occurrence_count": int(occurrences),
                "event_count": events.get(
                    (str(type_value or ""), None if hole is None else round(float(hole), 4)), 0
                ),
                "well_count": int(wells),
                "first_seen_at": _iso(first),
                "last_seen_at": _iso(last),
                "total_npt_hours": _hours(
                    session,
                    field_id=field_id,
                    project_id=project_id,
                    problem_type=str(type_value or ""),
                    hole_size_in=hole,
                ),
                "query": {
                    "field_id": field_id or None,
                    "project_id": project_id or None,
                    "problem_type": type_value or None,
                    "hole_size_in": None if hole is None else float(hole),
                    "since": _iso(since),
                    "until": _iso(until),
                },
            }
        )
    rows.sort(
        key=lambda row: (
            -row["occurrence_count"],
            -(row["total_npt_hours"] or 0.0),
            row["problem_type"],
            row["hole_size_in"] if row["hole_size_in"] is not None else 0.0,
        )
    )
    # 0 (or anything negative) means "all of them", the same convention the operational repository uses:
    # a caller who wants the whole field should not have to pass a number large enough to be a lie.
    return rows if limit is None or int(limit) <= 0 else rows[: int(limit)]


def _boundary(value: object, *, end: bool = False) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 10:  # a date, as the CLI accepts them
            parsed = datetime.fromisoformat(text)
            return parsed.replace(hour=23, minute=59, second=59) if end else parsed
        return datetime.fromisoformat(text)
    raise ValidationError("a date bound must be an ISO date or datetime", value=repr(value)[:60])


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _hours(
    session: Session,
    *,
    field_id: str,
    project_id: str,
    problem_type: str,
    hole_size_in: object,
) -> float | None:
    """The NPT hours the problems of one grouping cost, summed where the rows state a duration.

    Counted over :func:`~drilling_intelligence.intelligence.field.problem_hours`, i.e. only through rows
    that are linked to a *problem* of this grouping - by ``npt_id``, or by the shared event when there is
    no direct link.  An NPT row that cost hours on a well but produced no problem record is not evidence
    of this pattern, and folding it in would inflate a recurring problem with time nobody attributed to
    it.
    """
    clauses: list[Any] = []
    if field_id:
        clauses.append(Well.field_id == field_id)
    if project_id:
        clauses.append(Well.project_id == project_id)
    hours = problem_hours()
    statement = (
        select(func.sum(hours.c.hours)).select_from(hours).join(Well, Well.id == hours.c.well_id)
    )
    if clauses:
        statement = statement.where(or_(*clauses))
    if problem_type and problem_type != "uncategorised":
        statement = statement.where(hours.c.problem_type == problem_type)
    if hole_size_in is not None:
        statement = statement.where(hours.c.hole_size_in == float(hole_size_in))
    value = session.execute(statement).scalar_one_or_none()
    return None if value is None else round(float(value), 4)


def evidence_for(
    session: Session,
    *,
    field_id: str = "",
    project_id: str = "",
    problem_type: str = "",
    hole_size_in: object = None,
    limit: int = MAX_LINKED_EVIDENCE,
) -> list[dict[str, Any]]:
    """The problem rows behind a grouping, oldest first, with their well and date.

    Returned as ids rather than ORM rows: the snapshot stores them, a screen shows the first few, and a
    reader who wants the text can fetch the row - which is the point of the evidence existing at all.
    """
    clauses: list[Any] = []
    if field_id:
        clauses.append(Well.field_id == field_id)
    if project_id:
        clauses.append(Well.project_id == project_id)
    statement = (
        select(
            ProblemOccurrence.id,
            ProblemOccurrence.well_id,
            ProblemOccurrence.section_id,
            ProblemOccurrence.problem_type,
            ProblemOccurrence.occurred_at,
            ProblemOccurrence.description,
            Well.name,
        )
        .join(Well, Well.id == ProblemOccurrence.well_id)
        .order_by(
            ProblemOccurrence.occurred_at.asc().nulls_last(),
            ProblemOccurrence.id,
        )
        .limit(max(1, int(limit)))
    )
    if problem_type and problem_type != "uncategorised":
        statement = statement.where(ProblemOccurrence.problem_type == problem_type)
    if hole_size_in is not None:
        statement = statement.where(ProblemOccurrence.hole_size_in == float(hole_size_in))
    if clauses:
        statement = statement.where(or_(*clauses))
    return [
        {
            "problem_id": row_id,
            "well_id": well_id,
            "well": well_name,
            "section_id": section_id,
            "problem_type": problem_type,
            "occurred_at": _iso(occurred),
            "description": str(description or "")[:200],
        }
        for (
            row_id,
            well_id,
            section_id,
            problem_type,
            occurred,
            description,
            well_name,
        ) in session.execute(statement)
    ]


def snapshot(
    session: Session,
    candidate: Mapping[str, Any],
    *,
    detected_by: str = "intelligence",
    status: ConfirmationStatus | str | None = None,
    note: str = "",
    link_evidence: bool = True,
) -> FieldPattern:
    """Persist one grouping as a reviewable record, or update the row that already holds it.

    The stored ``query`` is what makes this auditable years later: the numbers in the columns are the
    numbers somebody accepted, and the parameters that produced them are on the same row, so
    :func:`staleness` can re-run them instead of trusting that nothing has changed.

    ``link_evidence=False`` skips the graph edges; it never skips storing which wells and which rows the
    count came from, because that is the measurement's own content.
    """
    parameters = dict(candidate.get("query") or {})
    if not parameters:
        raise ValidationError("a pattern candidate must carry the query that produced it")
    signature = signature_for(**parameters)
    row = session.execute(
        select(FieldPattern).where(FieldPattern.signature == signature)
    ).scalar_one_or_none()
    scope = {
        key: parameters.get(key)
        for key in ("field_id", "project_id", "problem_type", "hole_size_in")
        if parameters.get(key) is not None
    }
    # The evidence is part of the measurement, not part of the graph: what the snapshot counted stays on
    # the row whether or not the caller also wants the edges written.
    entries = list(candidate.get("evidence") or []) or evidence_for(
        session, limit=MAX_LINKED_EVIDENCE, **scope
    )
    if row is None:
        row = FieldPattern(
            id=new_id("pat"),
            signature=signature,
            project_id=parameters.get("project_id") or None,
            field_id=parameters.get("field_id") or None,
            problem_type=str(parameters.get("problem_type") or "") or None,
            hole_size_in=parameters.get("hole_size_in"),
            occurrence_count=int(candidate.get("occurrence_count") or 0),
            well_count=int(candidate.get("well_count") or 0),
            event_count=int(candidate.get("event_count") or 0),
            total_npt_hours=candidate.get("total_npt_hours"),
            first_seen_at=_parse(candidate.get("first_seen_at")),
            last_seen_at=_parse(candidate.get("last_seen_at")),
            evidence=entries,
            well_ids=sorted(
                {str(entry.get("well_id")) for entry in entries if entry.get("well_id")}
            ),
            query=dict(parameters),
            status=str(CONFIRMATION.parse(status) if status is not None else CONFIRMATION.initial),
            detected_by=str(detected_by or "") or "intelligence",
            note=str(note or ""),
            computed_at=datetime.now(UTC),
            attributes={},
        )
        session.add(row)
        session.flush()
    else:
        # Only the measurement is refreshed; a status a person set is theirs until they change it.
        row.occurrence_count = int(candidate.get("occurrence_count") or row.occurrence_count)
        row.well_count = int(candidate.get("well_count") or row.well_count)
        row.total_npt_hours = candidate.get("total_npt_hours", row.total_npt_hours)
        row.first_seen_at = _parse(candidate.get("first_seen_at")) or row.first_seen_at
        row.last_seen_at = _parse(candidate.get("last_seen_at")) or row.last_seen_at
        row.event_count = int(candidate.get("event_count") or row.event_count or 0)
        row.computed_at = datetime.now(UTC)
        row.stale_at = None
        row.stale_snapshot = {}
        row.evidence = entries
        row.well_ids = sorted(
            {str(entry.get("well_id")) for entry in entries if entry.get("well_id")}
        )
        session.flush()
    if link_evidence:
        link_rows(session, row)
    return row


def link_rows(session: Session, pattern: FieldPattern) -> dict[str, int]:
    """Link the snapshot to the wells it was seen in and the problem rows behind it.

    Both directions are edges in the same graph the documents and facts use, so a lesson that cites a
    pattern and a pattern that cites its evidence are checked by the same validator - and a pattern whose
    evidence was deleted reports a dangling edge in ``doctor`` rather than showing a confident number with
    nothing under it.

    The returned counts are the links the pattern has, so calling this again after a refresh reports the
    same numbers instead of adding to them.
    """
    counts = {"wells": 0, "evidence": 0}
    for well_id in pattern.well_ids or []:
        _link(
            session,
            pattern,
            relation=KnowledgeRelationType.PATTERN_SEEN_IN_WELL.value,
            target_type="well",
            target_id=str(well_id),
            note="seen in this well",
        )
        counts["wells"] += 1
    for entry in (pattern.evidence or [])[:MAX_LINKED_EVIDENCE]:
        problem_id = str(entry.get("problem_id") or "")
        if not problem_id:
            continue
        _link(
            session,
            pattern,
            relation=KnowledgeRelationType.PATTERN_CITES_EVIDENCE.value,
            target_type="problem_occurrence",
            target_id=problem_id,
            note="one occurrence behind the count",
        )
        counts["evidence"] += 1
    return counts


def _link(
    session: Session,
    pattern: FieldPattern,
    *,
    relation: str,
    target_type: str,
    target_id: str,
    note: str,
) -> bool:
    """One edge, unless it is already there.

    A snapshot is refreshed whenever the grouping is re-run, and the links must survive that without
    turning into a stack of identical rows: an edge is a fact about the pattern and the well, not an event
    log entry, so re-asserting it changes nothing.  The counts a caller gets back are the edges the
    pattern has, which is why they stay stable across re-links.
    """
    from ..database.models import KnowledgeRelation

    existing = session.execute(
        select(KnowledgeRelation.id)
        .where(
            KnowledgeRelation.source_type == "pattern",
            KnowledgeRelation.source_id == pattern.id,
            KnowledgeRelation.relation == relation,
            KnowledgeRelation.target_type == target_type,
            KnowledgeRelation.target_id == target_id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return False
    create_knowledge_relation(
        session,
        source_type="pattern",
        source_id=pattern.id,
        relation=relation,
        target_type=target_type,
        target_id=target_id,
        note=note,
    )
    return True


def staleness(session: Session, pattern_id: str) -> dict[str, Any]:
    """Re-run a snapshot's own query and report what has moved since it was taken.

    The stored numbers are not touched.  A pattern that quietly updated itself overnight would be
    indistinguishable from one that had been reviewed, and the whole value of the reviewed figure is
    that it is frozen - with a difference report beside it.
    """
    row = get_pattern(session, pattern_id)
    parameters = dict(row.query or {})
    live = find_recurring(
        session,
        field_id=str(parameters.get("field_id") or ""),
        project_id=str(parameters.get("project_id") or ""),
        problem_type=str(parameters.get("problem_type") or ""),
        min_occurrences=1,
        min_wells=1,
        limit=500,
    )
    hole = parameters.get("hole_size_in")
    match = next(
        (
            candidate
            for candidate in live
            if str(candidate["problem_type"]) == str(row.problem_type or "uncategorised")
            and (
                (hole is None and candidate["hole_size_in"] is None)
                or (hole is not None and candidate["hole_size_in"] == float(hole))
            )
        ),
        None,
    )
    differences: dict[str, Any] = {}
    for key, stored, current in (
        (
            "occurrence_count",
            row.occurrence_count,
            None if match is None else match["occurrence_count"],
        ),
        ("well_count", row.well_count, None if match is None else match["well_count"]),
        (
            "total_npt_hours",
            row.total_npt_hours,
            None if match is None else match["total_npt_hours"],
        ),
    ):
        if _number(stored) != _number(current):
            differences[key] = {"stored": _number(stored), "now": _number(current)}
    return {
        "pattern_id": row.id,
        "signature": row.signature,
        "query": parameters,
        "found": match is not None,
        "stale": bool(differences),
        "differences": differences,
        "computed_at": _iso(row.computed_at),
    }


def _number(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        rounded = round(float(value), 4)
    except (TypeError, ValueError):
        return None
    return int(rounded) if float(rounded).is_integer() and abs(rounded) < 1e15 else rounded


def _parse(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def get_pattern(session: Session, pattern_id: str) -> FieldPattern:
    row = session.get(FieldPattern, str(pattern_id))
    if row is None:
        raise ValidationError(f"no pattern {pattern_id!r}")
    return row


def list_patterns(
    session: Session,
    *,
    field_id: str = "",
    project_id: str = "",
    status: str = "",
    stale_only: bool = False,
    limit: int = 100,
) -> list[FieldPattern]:
    statement = select(FieldPattern)
    for label, value in (("field_id", field_id), ("project_id", project_id)):
        if value:
            statement = statement.where(getattr(FieldPattern, label) == value)
    if status:
        statement = statement.where(FieldPattern.status == str(CONFIRMATION.parse(status)))
    if stale_only:
        statement = statement.where(FieldPattern.stale_at.is_not(None))
    return list(
        session.execute(
            statement.order_by(
                FieldPattern.occurrence_count.desc(), FieldPattern.problem_type, FieldPattern.id
            ).limit(max(1, int(limit)))
        ).scalars()
    )


def set_pattern_status(
    session: Session,
    pattern_id: str,
    new_status: ConfirmationStatus | str,
    *,
    by: str = "",
    reason: str = "",
) -> FieldPattern:
    """Confirm, reject or dismiss a pattern - the same lifecycle every promoted row uses."""
    from ..operations.repository import set_record_status

    row = get_pattern(session, pattern_id)
    set_record_status(session, row, new_status, by=by, reason=reason)
    return row


def propose_recommendation(
    session: Session,
    pattern_id: str,
    *,
    statement: str,
    reason: str = "",
    by: str = "intelligence",
) -> Recommendation:
    """Turn a pattern into advice a person can decide on, with the pattern's own evidence attached.

    The recommendation stores the pattern's ``query`` as well as its ids, so the advice can be re-derived
    and argued with; and it starts ``PROPOSED``, because what a grouping of history licenses is a
    proposal, not a change to somebody's programme.
    """
    from ..lessons.repository import LessonRepository

    row = get_pattern(session, pattern_id)
    evidence = [
        {
            "kind": "pattern",
            "pattern_id": row.id,
            "problem_id": str(entry.get("problem_id") or ""),
            "well": str(entry.get("well") or ""),
            "occurred_at": entry.get("occurred_at"),
        }
        for entry in (row.evidence or [])[:MAX_LINKED_EVIDENCE]
    ]
    return LessonRepository(session).propose_recommendation(
        statement=str(statement or "").strip(),
        reason=str(reason or "")
        or (
            f"{row.occurrence_count} occurrences across {row.well_count} wells"
            f"{' costing ' + format(row.total_npt_hours, '.2f') + ' h' if row.total_npt_hours else ''}"
        ),
        evidence=evidence,
        query=dict(row.query or {}),
        confidence=None if row.confidence is None else float(row.confidence),
        pattern_id=row.id,
        field_id=row.field_id or "",
        project_id=row.project_id or "",
        generated_by=by,
    )
