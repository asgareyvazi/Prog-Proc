"""Field intelligence: the aggregations, computed by the database from the records.

Six questions a drilling group asks constantly - how many wells, how much NPT, which problems, which
events, what was learnt, and what does the offset well say - and each one is answered here with a
grouped ``SELECT`` over the record tables.  No rows are pulled into Python to be counted (that works
until the field has forty thousand events), and no number is smoothed, rounded into a headline, or
filled in when the data is missing.

Three rules that make the answers trustworthy:

*   **A missing date is excluded by a date filter, never treated as "recent enough".**  ``since`` and
    ``until`` restrict to rows whose own timestamp falls inside the window; a row with no timestamp is
    reported separately as ``undated`` (and counted in the totals when no filter is applied).  Silently
    including undated rows in a "June NPT" number, or silently dropping them from the total, would both
    be wrong, and in opposite directions.
*   **Hours of unknown duration are counted, not zeroed.**  A record that says "the bit was pulled"
    without a duration is one row of real experience and ``unknown_duration`` hours of lost time;
    pretending it cost nothing is how a field's history becomes flattering.
*   **Every number names its own query.**  Each aggregation returns the scope it was run with, so a
    screenshot, a report and a test can all tell that "28.75 h stuck pipe" was counted over the whole
    field rather than over one well - the difference being the entire point of the question.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Select, func, or_, select, union_all
from sqlalchemy.orm import Session

from ..core.errors import ValidationError
from ..database.models import (
    DdrReport,
    Field,
    LessonLearned,
    NptRecord,
    ProblemOccurrence,
    Well,
    WellEvent,
    WellSection,
)

__all__ = ["FieldIntelligence", "problem_hours"]


def problem_hours() -> Any:
    """A subquery of ``(problem_id, well_id, problem_type, hole_size_in, hours)``.

    A problem's lost time is reachable by two links, and the promoter fills whichever one its source
    stated: ``problem_occurrence.npt_id`` when the row named the NPT record, or the shared ``event_id``
    when the problem was raised from an event.  Both are followed here - with the event path used only
    when there is no direct link, so an hour is never counted twice for one problem.

    It is a subquery rather than a helper method because two callers need the same arithmetic (the field
    aggregation and the pattern grouping), and because grouping in SQL is what keeps a hundred thousand
    problems from becoming a hundred thousand queries.
    """
    by_npt = (
        select(
            ProblemOccurrence.id.label("problem_id"),
            ProblemOccurrence.well_id.label("well_id"),
            ProblemOccurrence.problem_type.label("problem_type"),
            ProblemOccurrence.hole_size_in.label("hole_size_in"),
            ProblemOccurrence.occurred_at.label("occurred_at"),
            NptRecord.duration_hours.label("hours"),
        )
        .join(NptRecord, NptRecord.id == ProblemOccurrence.npt_id)
        .where(NptRecord.duration_hours.is_not(None))
    )
    by_event = (
        select(
            ProblemOccurrence.id,
            ProblemOccurrence.well_id,
            ProblemOccurrence.problem_type,
            ProblemOccurrence.hole_size_in,
            ProblemOccurrence.occurred_at,
            NptRecord.duration_hours,
        )
        .join(NptRecord, NptRecord.event_id == ProblemOccurrence.event_id)
        .where(
            ProblemOccurrence.npt_id.is_(None),
            ProblemOccurrence.event_id.is_not(None),
            NptRecord.duration_hours.is_not(None),
        )
    )
    return union_all(by_npt, by_event).subquery("problem_hours")


def _stamp(value: object) -> datetime | None:
    """The timestamp a *record* carries.  Lenient on purpose: it reads what the data has, and a
    value it cannot parse is reported as undated rather than raised from a read path.

    Window *bounds* are different - they are the question a caller asked - and those go through
    :func:`parse_boundary`, which refuses to answer the wrong question.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_boundary(value: object, *, end: bool = False) -> datetime | None:
    """A date-window bound, strictly.

    ``None`` and the empty string mean "no bound" - the window is open on that side.  Anything else
    must parse, or the call fails: a caller who asked for "NPT from June" and mistyped the date must
    not get the whole field back under a filter the answer does not carry.  A bound with no time of
    day covers the whole day (a window ending on 2025-06-01 includes the last second of it), which
    is the reading a date on a report line has.

    Aware datetimes are normalised to naive UTC, the representation the SQLite store round-trips,
    so a bound compares with the stored rows the way the rows compare with each other.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.max.time() if end else datetime.min.time())
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as err:
        raise ValidationError(
            f"a date bound must be an ISO date or datetime, got {text!r}",
            value=text[:60],
        ) from err
    if len(text) == 10:  # a date, as the CLI accepts them - the window covers the whole day
        return datetime.combine(parsed.date(), datetime.max.time() if end else datetime.min.time())
    return parsed


def _iso(value: object) -> str | None:
    stamp = _stamp(value)
    return stamp.isoformat() if stamp is not None else None


class FieldIntelligence:
    """Aggregations over one field, project or well, each answered in SQL."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- scope ----------------------------------------------------------------
    def _wells(
        self, *, field_id: str, project_id: str, well_id: str = ""
    ) -> Select[tuple[Any, ...]]:
        """The well ids in scope, as a subquery - the one place scope is decided for every aggregation.

        A named well is the whole scope: ``well_id`` takes precedence over ``field_id`` and
        ``project_id`` rather than joining them, the same rule the timeline applies.  A union would
        answer "field A's NPT" when the caller asked for "well W's NPT in field A", and a well that
        is not in the field would ride along with the field's numbers under a scope label that names
        one well.
        """
        statement = select(Well.id)
        if well_id:
            return statement.where(Well.id == well_id)
        clauses: list[Any] = []
        if field_id:
            clauses.append(Well.field_id == field_id)
        if project_id:
            clauses.append(Well.project_id == project_id)
        if not clauses:
            raise ValidationError(
                "a field aggregation needs a scope",
                hint="pass field_id, project_id or well_id",
            )
        return statement.where(or_(*clauses))

    def _window(self, column: Any, since: object, until: object) -> list[Any]:
        """The date predicates for one timestamp column.

        Only the bounds the caller gave are applied; ``None`` means "no bound", not "no rows" - and
        there is deliberately no ``IS NULL`` clause here, because a row with no date is reported by
        :meth:`_undated`, not quietly included in a filtered count.  Bounds are parsed strictly
        (:func:`parse_boundary`): a mistyped date is an error, never a silently dropped filter.
        """
        predicates: list[Any] = []
        low = parse_boundary(since)
        high = parse_boundary(until, end=True)
        if low is not None:
            predicates.append(column >= low)
        if high is not None:
            predicates.append(column <= high)
        return predicates

    def _undated(self, statement: Any, column: Any) -> int:
        return int(
            self.session.execute(
                select(func.count()).select_from(statement.where(column.is_(None)).subquery())
            ).scalar_one()
            or 0
        )

    # -- the six questions ----------------------------------------------------
    def wells(self, *, field_id: str = "", project_id: str = "") -> dict[str, Any]:
        """Every well in scope, with the counts a field review opens with."""
        if not (field_id or project_id):
            raise ValidationError(
                "a well list needs field_id or project_id",
                hint="pass --field or --project",
            )
        clauses: list[Any] = []
        if field_id:
            clauses.append(Well.field_id == field_id)
        if project_id:
            clauses.append(Well.project_id == project_id)
        rows = list(
            self.session.execute(
                select(Well)
                .where(or_(*clauses))
                .order_by(Well.spud_date.asc().nulls_last(), Well.name, Well.id)
            ).scalars()
        )
        # A well that is absent from this grouping has no NPT row with a duration at all, which is not
        # the same fact as "0.0 hours": a reader comparing wells must be able to tell a dry hole record
        # from a measured zero.
        npt = dict(
            self.session.execute(
                select(NptRecord.well_id, func.sum(NptRecord.duration_hours))
                .where(NptRecord.well_id.in_([row.id for row in rows] or [""]))
                .group_by(NptRecord.well_id)
            ).all()
        )
        problems = dict(
            self.session.execute(
                select(ProblemOccurrence.well_id, func.count())
                .where(ProblemOccurrence.well_id.in_([row.id for row in rows] or [""]))
                .group_by(ProblemOccurrence.well_id)
            ).all()
        )
        reports = dict(
            self.session.execute(
                select(DdrReport.well_id, func.count())
                .where(DdrReport.well_id.in_([row.id for row in rows] or [""]))
                .group_by(DdrReport.well_id)
            ).all()
        )
        sections = self._grouped_count(WellSection.well_id, WellSection, [row.id for row in rows])
        return {
            "scope": {"field_id": field_id or None, "project_id": project_id or None},
            "count": len(rows),
            "wells": [
                {
                    "id": row.id,
                    "name": row.name,
                    "field_id": row.field_id,
                    "project_id": row.project_id,
                    "lifecycle_status": row.lifecycle_status,
                    "spud_date": _iso(row.spud_date),
                    "completion_date": _iso(row.completion_date),
                    "total_depth_md": row.total_depth_md_value,
                    "sections": int(sections.get(row.id, 0)),
                    "npt_hours": None if npt.get(row.id) is None else round(float(npt[row.id]), 4),
                    "problems": int(problems.get(row.id, 0)),
                    "reports": int(reports.get(row.id, 0)),
                }
                for row in rows
            ],
        }

    def _grouped_count(self, column: Any, model: Any, well_ids: Sequence[str]) -> dict[str, int]:
        """Rows per well for one table, in a single grouped query.

        The four counts a well list needs (sections, NPT hours, problems, reports) are four group-bys
        rather than four queries per well: a field with sixty wells and a loop over each of them is 240
        round trips for numbers the database can produce in four.
        """
        return dict(
            self.session.execute(
                select(column, func.count())
                .where(column.in_(list(well_ids) or [""]))
                .group_by(column)
            ).all()
        )

    def npt(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        since: object = None,
        until: object = None,
    ) -> dict[str, Any]:
        """NPT in scope: total hours, rows, and the same split by category and by well.

        ``unknown_duration`` is part of the answer rather than a footnote: it says how many of the rows
        lost time that nobody wrote down, which is the number that decides whether the hours total can be
        compared with another field's.
        """
        base = select(NptRecord).where(
            NptRecord.well_id.in_(
                self._wells(field_id=field_id, project_id=project_id, well_id=well_id)
            )
        )
        window = self._window(NptRecord.started_at, since, until)
        if window:
            base = base.where(*window)
        rows = list(
            self.session.execute(
                base.order_by(NptRecord.started_at.asc().nulls_last(), NptRecord.id)
            ).scalars()
        )
        hours = [row.duration_hours for row in rows if row.duration_hours is not None]
        by_category: dict[str, dict[str, Any]] = {}
        by_well: dict[str, dict[str, Any]] = {}
        for row in rows:
            for key, bucket, label in (
                (str(row.category or "uncategorised"), by_category, None),
                (str(row.well_id), by_well, row.well_id),
            ):
                entry = bucket.setdefault(
                    key,
                    {
                        "records": 0,
                        "hours": 0.0,
                        "unknown_duration": 0,
                        "wells": set() if key != label or label is None else None,
                        "first_seen_at": None,
                        "last_seen_at": None,
                    },
                )
                entry["records"] += 1
                if row.duration_hours is None:
                    entry["unknown_duration"] += 1
                else:
                    entry["hours"] = round(float(entry["hours"]) + float(row.duration_hours), 4)
                if entry["wells"] is not None:
                    entry["wells"].add(str(row.well_id))
                stamp = _iso(row.started_at)
                if stamp is not None:
                    if entry["first_seen_at"] is None or stamp < entry["first_seen_at"]:
                        entry["first_seen_at"] = stamp
                    if entry["last_seen_at"] is None or stamp > entry["last_seen_at"]:
                        entry["last_seen_at"] = stamp
        return {
            "scope": {
                "field_id": field_id or None,
                "project_id": project_id or None,
                "well_id": well_id or None,
                "since": _iso(since),
                "until": _iso(until),
            },
            "rows": len(rows),
            # ``undated`` is deliberately not windowed: it answers "how many of this scope's rows could not
            # be placed in time", which is the number a reader needs next to a windowed total - the total
            # is what fell inside, this is what the window could not see.
            "total_hours": round(float(sum(hours)), 4),
            "unknown_duration": len(rows) - len(hours),
            "undated": self._undated(
                select(NptRecord).where(
                    NptRecord.well_id.in_(
                        self._wells(field_id=field_id, project_id=project_id, well_id=well_id)
                    )
                ),
                NptRecord.started_at,
            ),
            "by_category": {
                key: {
                    **{name: value for name, value in entry.items() if name != "wells"},
                    "wells": len(entry["wells"] or ()) if entry["wells"] is not None else None,
                }
                for key, entry in sorted(
                    by_category.items(), key=lambda item: (-item[1]["hours"], item[0])
                )
            },
            # A per-well breakdown has no "how many wells" answer to give, so it omits the key instead of
            # carrying a null one that a caller would have to know how to read.
            "by_well": {
                key: {name: value for name, value in entry.items() if name != "wells"}
                for key, entry in sorted(
                    by_well.items(), key=lambda item: (-item[1]["hours"], item[0])
                )
            },
        }

    def problems(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        since: object = None,
        until: object = None,
    ) -> dict[str, Any]:
        """Problem occurrences by type: how often, on which wells, when first and last."""
        base = select(ProblemOccurrence).where(
            ProblemOccurrence.well_id.in_(
                self._wells(field_id=field_id, project_id=project_id, well_id=well_id)
            )
        )
        window = self._window(ProblemOccurrence.occurred_at, since, until)
        if window:
            base = base.where(*window)
        rows = list(
            self.session.execute(
                base.order_by(
                    ProblemOccurrence.occurred_at.asc().nulls_last(), ProblemOccurrence.id
                )
            ).scalars()
        )
        hours = problem_hours()
        grouped_hours = dict(
            self.session.execute(
                select(hours.c.problem_id, func.sum(hours.c.hours)).group_by(hours.c.problem_id)
            ).all()
        )
        by_type: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.problem_type or "uncategorised")
            entry = by_type.setdefault(
                key,
                {
                    "occurrences": 0,
                    "wells": set(),
                    "sections": set(),
                    # None, not 0.0: "no NPT hours are linked to this type" and "it cost exactly
                    # zero" are different answers, and the patterns layer keeps the same
                    # distinction - a type with no linked hours must not read as a free problem.
                    "npt_hours": None,
                    "root_cause_known": 0,
                    "first_seen_at": None,
                    "last_seen_at": None,
                },
            )
            entry["occurrences"] += 1
            entry["wells"].add(str(row.well_id))
            if row.section_id:
                entry["sections"].add(str(row.section_id))
            if str(row.root_cause_status or "").upper() == "KNOWN":
                entry["root_cause_known"] += 1
            stamp = _iso(row.occurred_at)
            if stamp is not None:
                if entry["first_seen_at"] is None or stamp < entry["first_seen_at"]:
                    entry["first_seen_at"] = stamp
                if entry["last_seen_at"] is None or stamp > entry["last_seen_at"]:
                    entry["last_seen_at"] = stamp
            if row.id in grouped_hours:
                entry["npt_hours"] = round(
                    (entry["npt_hours"] or 0.0) + float(grouped_hours[row.id]), 4
                )
        return {
            "scope": {
                "field_id": field_id or None,
                "project_id": project_id or None,
                "well_id": well_id or None,
                "since": _iso(since),
                "until": _iso(until),
            },
            "occurrences": len(rows),
            "wells": len({str(row.well_id) for row in rows}),
            "by_type": {
                key: {
                    **{
                        name: value
                        for name, value in entry.items()
                        if name not in {"wells", "sections"}
                    },
                    "wells": len(entry["wells"]),
                    "well_ids": sorted(entry["wells"]),
                    "sections": sorted(entry["sections"]),
                }
                for key, entry in sorted(
                    by_type.items(), key=lambda item: (-item[1]["occurrences"], item[0])
                )
            },
        }

    def events(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        since: object = None,
        until: object = None,
    ) -> dict[str, Any]:
        """Events by category and type, with the severity split left open where severity is absent."""
        base = select(WellEvent).where(
            WellEvent.well_id.in_(
                self._wells(field_id=field_id, project_id=project_id, well_id=well_id)
            )
        )
        window = self._window(WellEvent.occurred_at, since, until)
        if window:
            base = base.where(*window)
        rows = list(
            self.session.execute(
                base.order_by(WellEvent.occurred_at.asc().nulls_last(), WellEvent.id)
            ).scalars()
        )
        by_category: dict[str, dict[str, Any]] = {}
        by_severity: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for row in rows:
            category = str(row.category or "uncategorised")
            entry = by_category.setdefault(
                category,
                {
                    "events": 0,
                    "types": {},
                    "wells": set(),
                    "first_seen_at": None,
                    "last_seen_at": None,
                },
            )
            entry["events"] += 1
            entry["wells"].add(str(row.well_id))
            event_type = str(row.event_type or "unclassified")
            entry["types"][event_type] = entry["types"].get(event_type, 0) + 1
            by_type[event_type] = by_type.get(event_type, 0) + 1
            severity = str(row.severity or "not_stated")
            by_severity[severity] = by_severity.get(severity, 0) + 1
            stamp = _iso(row.occurred_at)
            if stamp is not None:
                if entry["first_seen_at"] is None or stamp < entry["first_seen_at"]:
                    entry["first_seen_at"] = stamp
                if entry["last_seen_at"] is None or stamp > entry["last_seen_at"]:
                    entry["last_seen_at"] = stamp
        return {
            "scope": {
                "field_id": field_id or None,
                "project_id": project_id or None,
                "well_id": well_id or None,
                "since": _iso(since),
                "until": _iso(until),
            },
            "events": len(rows),
            "by_category": {
                key: {
                    **{name: value for name, value in entry.items() if name != "wells"},
                    "wells": len(entry["wells"]),
                    "well_ids": sorted(entry["wells"]),
                }
                for key, entry in sorted(
                    by_category.items(), key=lambda item: (-item[1]["events"], item[0])
                )
            },
            "by_type": dict(sorted(by_type.items(), key=lambda item: (-item[1], item[0]))),
            "by_severity": dict(sorted(by_severity.items(), key=lambda item: (-item[1], item[0]))),
        }

    def lessons(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        approved_only: bool = True,
        limit: int = 200,
    ) -> dict[str, Any]:
        """What the field has learnt, with each lesson's own evidence counted where it stands.

        The evidence count comes from the lesson row's provenance rather than from a graph walk, because
        a lesson with a provenance entry and no edges is still traceable, and one with neither is
        exactly as trustworthy as the count says.
        """
        statement = select(LessonLearned)
        if well_id:
            # A named well is the whole scope, as in :meth:`_wells` - a lesson written against the
            # field still counts when the question is about the well it was learnt on.
            statement = statement.where(LessonLearned.well_id == well_id)
        else:
            clauses: list[Any] = []
            if field_id:
                clauses.append(
                    or_(
                        LessonLearned.field_id == field_id,
                        LessonLearned.well_id.in_(self._wells(field_id=field_id, project_id="")),
                    )
                )
            if project_id:
                clauses.append(
                    or_(
                        LessonLearned.project_id == project_id,
                        LessonLearned.well_id.in_(self._wells(field_id="", project_id=project_id)),
                    )
                )
            if not clauses:
                raise ValidationError(
                    "a lesson list needs field_id, project_id or well_id",
                    hint="scope it to a well, a field or a project",
                )
            statement = statement.where(or_(*clauses))
        statement = statement.where(LessonLearned.is_current.is_(True))
        if approved_only:
            statement = statement.where(LessonLearned.status == "APPROVED")
        rows = list(
            self.session.execute(
                statement.order_by(
                    LessonLearned.approved_at.desc().nulls_last(), LessonLearned.id
                ).limit(limit if limit and limit > 0 else None)
            ).scalars()
        )
        return {
            "scope": {
                "field_id": field_id or None,
                "project_id": project_id or None,
                "well_id": well_id or None,
                "approved_only": approved_only,
            },
            "count": len(rows),
            "lessons": [
                {
                    "id": row.id,
                    "code": row.code,
                    "title": row.title,
                    "lesson": row.lesson,
                    "status": row.status,
                    "revision": row.revision,
                    "well_id": row.well_id,
                    "field_id": row.field_id,
                    "problem_type": row.problem_type,
                    "root_cause_status": row.root_cause_status,
                    "approved_by": row.approved_by,
                    "approved_at": _iso(row.approved_at),
                    "evidence": len(row.provenance or []),
                }
                for row in rows
            ],
        }

    # -- the per-object histories --------------------------------------------
    def well_problem_history(self, well_id: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(ProblemOccurrence)
            .where(ProblemOccurrence.well_id == well_id)
            .order_by(ProblemOccurrence.occurred_at.asc().nulls_last(), ProblemOccurrence.id)
        ).scalars()
        return [self._problem_row(row) for row in rows]

    def section_problem_history(self, section_id: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(ProblemOccurrence)
            .where(ProblemOccurrence.section_id == section_id)
            .order_by(
                ProblemOccurrence.depth_from_value.asc().nulls_last(),
                ProblemOccurrence.occurred_at.asc().nulls_last(),
                ProblemOccurrence.id,
            )
        ).scalars()
        return [self._problem_row(row) for row in rows]

    def operation_events(self, operation_id: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(WellEvent)
            .where(WellEvent.operation_id == operation_id)
            .order_by(WellEvent.occurred_at.asc().nulls_last(), WellEvent.id)
        ).scalars()
        return [
            {
                "id": row.id,
                "well_id": row.well_id,
                "section_id": row.section_id,
                "report_id": row.report_id,
                "category": row.category,
                "event_type": row.event_type,
                "label": row.label,
                "description": row.description,
                "severity": row.severity,
                "occurred_at": _iso(row.occurred_at),
                "occurred_at_text": row.occurred_at_text,
                "status": row.status,
                "origin": row.origin,
                "provenance": list(row.provenance or []),
            }
            for row in rows
        ]

    @staticmethod
    def _problem_row(row: ProblemOccurrence) -> dict[str, Any]:
        return {
            "id": row.id,
            "well_id": row.well_id,
            "section_id": row.section_id,
            "operation_id": row.operation_id,
            "event_id": row.event_id,
            "npt_id": row.npt_id,
            "problem_type": row.problem_type,
            "code": row.code,
            "description": row.description,
            "occurred_at": _iso(row.occurred_at),
            "depth_from_value": row.depth_from_value,
            "depth_to_value": row.depth_to_value,
            "hole_size_in": row.hole_size_in,
            "formation": row.formation,
            "immediate_cause": row.immediate_cause,
            "immediate_cause_status": row.immediate_cause_status,
            "root_cause": row.root_cause,
            "root_cause_status": row.root_cause_status,
            "contributing_factors": list(row.contributing_factors or []),
            "corrective_action": row.corrective_action,
            "preventive_action": row.preventive_action,
            "status": row.status,
            "origin": row.origin,
            "provenance": list(row.provenance or []),
        }

    # -- offsets --------------------------------------------------------------
    def offset_candidates(
        self, well_id: str, *, same_field_only: bool = True, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Other wells that resemble this one, ranked on what the records actually share.

        The score is a count of shared attributes - field, formation of the problem, hole size, problem
        type - each of which is a column on a row, never a judgement.  It is a *ranking of what to read*,
        not a similarity claim: the answer a reader wants is "these three wells had the same stuck-pipe
        signature at the same depth, and here are the events", so the rows are returned with their
        problem types and hours rather than a single opaque number.

        ``same_field_only`` compares within the well's own field; ``False`` reaches the well's whole
        project.  Both scope by a recorded column - and a well without that column has no candidates
        rather than a fabricated comparison set.
        """
        well = self.session.get(Well, str(well_id))
        if well is None:
            raise ValidationError(f"no well {well_id!r}")
        # The comparison is scoped to the well's own field or project.  A well that has no field
        # (or no project) has nothing to compare against - an empty list, because answering "every
        # field's wells" to "which of my neighbours" would be an invented geography.
        if same_field_only:
            if well.field_id is None:
                return []
            scope = [Well.field_id == well.field_id]
        else:
            if well.project_id is None:
                return []
            scope = [Well.project_id == well.project_id]
        candidates = list(
            self.session.execute(select(Well.id, Well.name).where(Well.id != well.id, *scope)).all()
        )
        # One pair of queries for every candidate well, not one per well: the signatures are the
        # problems of the whole candidate set in a single select, with the hours summed per problem
        # in the grouped subquery beside it.  A field of sixty wells is two round trips, not sixty.
        signatures = self._problem_signatures(
            [str(well.id), *[str(other_id) for other_id, _ in candidates]]
        )
        mine = signatures.get(str(well.id), self._empty_signature())
        payload: list[dict[str, Any]] = []
        for other_id, other_name in candidates:
            theirs = signatures.get(str(other_id), self._empty_signature())
            shared_types = sorted(mine["types"] & theirs["types"])
            shared_holes = sorted(mine["holes"] & theirs["holes"])
            if not shared_types and not shared_holes:
                continue
            payload.append(
                {
                    "well_id": str(other_id),
                    "name": str(other_name),
                    "shared_problem_types": shared_types,
                    "shared_hole_sizes": [float(value) for value in shared_holes],
                    "problems": len(theirs["rows"]),
                    # None, not 0.0: "this offset well lost no time" and "this offset well has no timed
                    # NPT to compare" are different reasons to look at it.
                    "npt_hours": theirs["hours"],
                    "first_seen_at": theirs["first"],
                    "last_seen_at": theirs["last"],
                }
            )
        payload.sort(
            key=lambda row: (
                -len(row["shared_problem_types"]),
                -len(row["shared_hole_sizes"]),
                row["name"],
                row["well_id"],
            )
        )
        return payload if not (limit and limit > 0) else payload[: int(limit)]

    @staticmethod
    def _empty_signature() -> dict[str, Any]:
        return {
            "rows": [],
            "types": set(),
            "holes": set(),
            "hours": None,
            "first": None,
            "last": None,
        }

    @staticmethod
    def _signature_from_rows(rows: list[Any]) -> dict[str, Any]:
        """The signature of one well's problems from preloaded rows.

        ``hours`` is the sum of the linked NPT durations, and ``None`` - not 0.0 - when the rows
        link no duration at all: "no timed NPT to compare" and "lost no time" are different
        reasons to look at a well.
        """
        stamps = [_iso(row[0].occurred_at) for row in rows if _iso(row[0].occurred_at) is not None]
        values = [float(row[1]) for row in rows if row[1] is not None]
        return {
            "rows": [row[0] for row in rows],
            "types": {str(row[0].problem_type) for row in rows if row[0].problem_type},
            "holes": {row[0].hole_size_in for row in rows if row[0].hole_size_in is not None},
            "hours": round(sum(values), 4) if values else None,
            "first": min(stamps) if stamps else None,
            "last": max(stamps) if stamps else None,
        }

    def _problem_signatures(self, well_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """The problems of a set of wells, with the hours behind them, in two queries.

        The hours come from :func:`problem_hours`, the same subquery the field aggregation and the
        pattern grouping use, so "what did this problem cost" has exactly one answer in this codebase.
        An earlier version joined only ``npt_id`` here, which quietly reported a well whose problems
        were linked through their event as a well that lost no time at all - the kind of wrong number
        that makes an offset comparison worthless.
        """
        grouped = problem_hours()
        per_problem = (
            select(grouped.c.problem_id, func.sum(grouped.c.hours).label("hours"))
            .group_by(grouped.c.problem_id)
            .subquery("problem_hours_total")
        )
        rows = list(
            self.session.execute(
                select(ProblemOccurrence, per_problem.c.hours)
                .outerjoin(per_problem, per_problem.c.problem_id == ProblemOccurrence.id)
                .where(ProblemOccurrence.well_id.in_(list(well_ids) or [""]))
            ).all()
        )
        by_well: dict[str, list[Any]] = {}
        for row in rows:
            by_well.setdefault(str(row[0].well_id), []).append(row)
        return {well: self._signature_from_rows(bucket) for well, bucket in by_well.items()}

    # -- one call for a screen or a CLI --------------------------------------
    def summary(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        since: object = None,
        until: object = None,
    ) -> dict[str, Any]:
        """The field in one payload: wells, hours, problems, events, lessons.

        Built from the same methods a caller would call individually, which is the only way the totals
        and the detail are guaranteed to agree - a separate "quick" query would drift the first time one
        of the two grew a filter.
        """
        if not (field_id or project_id):
            raise ValidationError(
                "a field summary needs field_id or project_id",
                hint="pass --field or --project",
            )
        wells = self.wells(field_id=field_id, project_id=project_id)
        npt = self.npt(field_id=field_id, project_id=project_id, since=since, until=until)
        problems = self.problems(field_id=field_id, project_id=project_id, since=since, until=until)
        events = self.events(field_id=field_id, project_id=project_id, since=since, until=until)
        lessons = self.lessons(field_id=field_id, project_id=project_id, approved_only=False)
        field_row: Field | None = None
        if field_id:
            field_row = self.session.get(Field, field_id)
        return {
            "field": str(getattr(field_row, "name", "") or "") or None,
            "scope": {"field_id": field_id or None, "project_id": project_id or None},
            "wells": wells["count"],
            "reports": sum(row["reports"] for row in wells["wells"]),
            "npt_rows": npt["rows"],
            "npt_hours": npt["total_hours"],
            "npt_unknown_duration": npt["unknown_duration"],
            "npt_undated": npt["undated"],
            "npt_by_category": {
                key: {"hours": entry["hours"], "records": entry["records"], "wells": entry["wells"]}
                for key, entry in npt["by_category"].items()
            },
            "problems": problems["occurrences"],
            "problem_types": {
                key: {
                    "occurrences": entry["occurrences"],
                    "wells": entry["wells"],
                    "npt_hours": entry["npt_hours"],
                    "first_seen_at": entry["first_seen_at"],
                    "last_seen_at": entry["last_seen_at"],
                }
                for key, entry in problems["by_type"].items()
            },
            "events": events["events"],
            "events_by_category": {
                key: entry["events"] for key, entry in events["by_category"].items()
            },
            "events_by_severity": events["by_severity"],
            "lessons": lessons["count"],
        }
