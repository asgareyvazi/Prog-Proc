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

from sqlalchemy import Select, case, func, inspect, or_, select, union_all
from sqlalchemy.orm import Session

from ..core.errors import ValidationError
from ..database.models import (
    DdrReport,
    Field,
    HseIncident,
    LessonLearned,
    MudMeasurement,
    MudReport,
    NptRecord,
    ProblemOccurrence,
    Well,
    WellControlEvent,
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

    Either way the result holds **at most one row per problem**.  That is the contract the callers rely
    on: counting rows in this subquery counts problems, so a problem whose event carries four NPT
    records is still one problem with four records' worth of hours beside it.
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
    # The event path is collapsed to one row per problem *before* it is joined.  An event can carry
    # several NPT records - one incident, three separate waiting-on-weather entries - and joining
    # straight onto ``npt_record`` returned one row per record, so a single problem occurrence came
    # back two or three times.  The hours were right in total but the row count was not, and every
    # caller that counts rows rather than summing them would have seen one problem as two.
    event_hours = (
        select(
            NptRecord.event_id.label("event_id"),
            func.sum(NptRecord.duration_hours).label("hours"),
        )
        .where(NptRecord.event_id.is_not(None), NptRecord.duration_hours.is_not(None))
        .group_by(NptRecord.event_id)
        .subquery("event_hours")
    )
    # The event link is weaker evidence than the direct one, and it has to be treated as such.  A
    # direct ``npt_id`` is the source stating "this problem caused that NPT record".  A shared
    # ``event_id`` only says the problem and the NPT belong to the same *incident* - it never says
    # which problem on that incident caused which record.  With one problem on the event there is no
    # ambiguity to resolve, so the incident's hours belong to it.  With several problems on one
    # event, handing the same hours to each of them multiplied an incident-level quantity across
    # problem-level rows: two problems on a six-hour incident summed to twelve hours of NPT, and
    # every consumer here sums this subquery.  Splitting the hours between them would invent a
    # proportion the source never stated, so the honest answer is that the attribution is ambiguous
    # and the hours are left unattributed - see :func:`ambiguous_event_attribution`.
    sole_problem = (
        select(ProblemOccurrence.event_id.label("event_id"))
        .where(ProblemOccurrence.event_id.is_not(None))
        .group_by(ProblemOccurrence.event_id)
        .having(func.count(ProblemOccurrence.id) == 1)
        .subquery("sole_problem_on_event")
    )
    by_event = (
        select(
            ProblemOccurrence.id,
            ProblemOccurrence.well_id,
            ProblemOccurrence.problem_type,
            ProblemOccurrence.hole_size_in,
            ProblemOccurrence.occurred_at,
            event_hours.c.hours,
        )
        .join(event_hours, event_hours.c.event_id == ProblemOccurrence.event_id)
        .join(sole_problem, sole_problem.c.event_id == ProblemOccurrence.event_id)
        .where(
            ProblemOccurrence.npt_id.is_(None),
            ProblemOccurrence.event_id.is_not(None),
        )
    )
    return union_all(by_npt, by_event).subquery("problem_hours")


def ambiguous_event_attribution() -> Any:
    """The problems whose lost time cannot be attributed, and why.

    The complement of :func:`problem_hours`'s event path: problems that share an event carrying NPT
    with at least one other problem.  Their hours exist and are reported by the event- and
    NPT-scoped aggregations; what cannot be said is *which* of the problems on that incident they
    belong to.  Leaving them out of :func:`problem_hours` without naming them would trade one
    silent answer for another, so the set is queryable and the aggregations count it.
    """
    shared_event = (
        select(ProblemOccurrence.event_id.label("event_id"))
        .where(ProblemOccurrence.event_id.is_not(None))
        .group_by(ProblemOccurrence.event_id)
        .having(func.count(ProblemOccurrence.id) > 1)
        .subquery("shared_event")
    )
    timed_event = (
        select(NptRecord.event_id.label("event_id"))
        .where(NptRecord.event_id.is_not(None), NptRecord.duration_hours.is_not(None))
        .group_by(NptRecord.event_id)
        .subquery("timed_event")
    )
    return (
        select(
            ProblemOccurrence.id.label("problem_id"),
            ProblemOccurrence.well_id.label("well_id"),
            ProblemOccurrence.problem_type.label("problem_type"),
            ProblemOccurrence.event_id.label("event_id"),
        )
        .join(shared_event, shared_event.c.event_id == ProblemOccurrence.event_id)
        .join(timed_event, timed_event.c.event_id == ProblemOccurrence.event_id)
        .where(ProblemOccurrence.npt_id.is_(None), ProblemOccurrence.event_id.is_not(None))
        .subquery("ambiguous_event_attribution")
    )


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
        """NPT in scope, with all counts and sums produced by grouped SQL.

        ``unknown_duration`` is part of the answer rather than a footnote.  A detail list is not
        needed to calculate it, so this path never materialises every NPT row in Python.
        """
        # Database sessions deliberately disable autoflush.  Flush only the caller's transaction so a
        # read sees its own pending edits without committing them; the service still owns no commit.
        self.session.flush()
        scope = self._wells(field_id=field_id, project_id=project_id, well_id=well_id)
        filters: list[Any] = [NptRecord.well_id.in_(scope)]
        window = self._window(NptRecord.started_at, since, until)
        filters.extend(window)
        totals = self.session.execute(
            select(
                func.count(NptRecord.id),
                func.count(NptRecord.duration_hours),
                func.sum(NptRecord.duration_hours),
                func.count(NptRecord.started_at),
            ).where(*filters)
        ).one()
        rows_count, quantified, hours, _dated = (
            int(totals[0] or 0),
            int(totals[1] or 0),
            totals[2],
            int(totals[3] or 0),
        )
        category = func.coalesce(NptRecord.category, "uncategorised")
        category_rows = self.session.execute(
            select(
                category,
                func.count(NptRecord.id),
                func.count(NptRecord.duration_hours),
                func.sum(NptRecord.duration_hours),
                func.count(func.distinct(NptRecord.well_id)),
                func.min(NptRecord.started_at),
                func.max(NptRecord.started_at),
            )
            .where(*filters)
            .group_by(category)
        ).all()
        by_category: dict[str, dict[str, Any]] = {}
        for name, records, with_duration, total, wells, first, last in category_rows:
            by_category[str(name)] = {
                "records": int(records or 0),
                "hours": round(float(total or 0.0), 4),
                "unknown_duration": int(records or 0) - int(with_duration or 0),
                "wells": int(wells or 0),
                "first_seen_at": _iso(first),
                "last_seen_at": _iso(last),
            }
        well_rows = self.session.execute(
            select(
                NptRecord.well_id,
                func.count(NptRecord.id),
                func.count(NptRecord.duration_hours),
                func.sum(NptRecord.duration_hours),
                func.min(NptRecord.started_at),
                func.max(NptRecord.started_at),
            )
            .where(*filters)
            .group_by(NptRecord.well_id)
        ).all()
        by_well = {
            str(well): {
                "records": int(records or 0),
                "hours": round(float(total or 0.0), 4),
                "unknown_duration": int(records or 0) - int(with_duration or 0),
                "first_seen_at": _iso(first),
                "last_seen_at": _iso(last),
            }
            for well, records, with_duration, total, first, last in well_rows
        }
        undated = int(
            self.session.execute(
                select(func.count(NptRecord.id)).where(
                    NptRecord.well_id.in_(scope), NptRecord.started_at.is_(None)
                )
            ).scalar_one()
            or 0
        )
        return {
            "scope": {
                "field_id": field_id or None,
                "project_id": project_id or None,
                "well_id": well_id or None,
                "since": _iso(since),
                "until": _iso(until),
            },
            "rows": rows_count,
            "total_hours": round(float(hours or 0.0), 4),
            "unknown_duration": rows_count - quantified,
            # This is intentionally unwindowed: it answers how many scoped rows could not be placed in
            # time, while ``rows`` and the totals answer what fell inside the requested window.
            "undated": undated,
            "by_category": dict(
                sorted(by_category.items(), key=lambda item: (-item[1]["hours"], item[0]))
            ),
            "by_well": dict(sorted(by_well.items(), key=lambda item: (-item[1]["hours"], item[0]))),
        }

    def mud(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        since: object = None,
        until: object = None,
    ) -> dict[str, Any]:
        """Current mud reports and unit-aware property counts, grouped by SQLite.

        Measurements are never summed across units: cP, lb/100ft2 and mg/l are source units with no
        certified V3 conversion.  The report is therefore a count/discovery surface, not a hidden
        engineering calculation.
        """
        if not inspect(self.session.get_bind()).has_table(MudReport.__tablename__):
            return {
                "scope": {
                    "field_id": field_id or None,
                    "project_id": project_id or None,
                    "well_id": well_id or None,
                    "since": _iso(since),
                    "until": _iso(until),
                },
                "reports": 0,
                "measurements": 0,
                "by_property": {},
                "by_well": {},
            }
        scope = self._wells(field_id=field_id, project_id=project_id, well_id=well_id)
        report_filters: list[Any] = [
            MudReport.well_id.in_(scope),
            MudReport.is_current.is_(True),
        ]
        window = self._window(MudReport.report_date, since, until)
        report_filters.extend(window)
        report_count = int(
            self.session.execute(
                select(func.count()).select_from(MudReport).where(*report_filters)
            ).scalar_one()
            or 0
        )
        measurement_count = int(
            self.session.execute(
                select(func.count())
                .select_from(MudMeasurement)
                .join(MudReport, MudReport.id == MudMeasurement.mud_report_id)
                .where(*report_filters, MudMeasurement.is_current.is_(True))
            ).scalar_one()
            or 0
        )
        by_property_rows = self.session.execute(
            select(
                MudMeasurement.property_name,
                MudMeasurement.unit,
                MudMeasurement.quality,
                func.count(MudMeasurement.id),
            )
            .select_from(MudMeasurement)
            .join(MudReport, MudReport.id == MudMeasurement.mud_report_id)
            .where(*report_filters, MudMeasurement.is_current.is_(True))
            .group_by(
                MudMeasurement.property_name,
                MudMeasurement.unit,
                MudMeasurement.quality,
            )
            .order_by(MudMeasurement.property_name, MudMeasurement.unit, MudMeasurement.quality)
        ).all()
        by_property: dict[str, dict[str, Any]] = {}
        for property_name, unit, quality, count in by_property_rows:
            bucket = by_property.setdefault(
                str(property_name), {"measurements": 0, "units": {}, "qualities": {}}
            )
            bucket["measurements"] += int(count or 0)
            bucket["units"][str(unit or "")] = bucket["units"].get(str(unit or ""), 0) + int(
                count or 0
            )
            bucket["qualities"][str(quality or "")] = bucket["qualities"].get(
                str(quality or ""), 0
            ) + int(count or 0)
        by_well_rows = self.session.execute(
            select(MudReport.well_id, func.count(MudReport.id))
            .where(*report_filters)
            .group_by(MudReport.well_id)
            .order_by(MudReport.well_id)
        ).all()
        return {
            "scope": {
                "field_id": field_id or None,
                "project_id": project_id or None,
                "well_id": well_id or None,
                "since": _iso(since),
                "until": _iso(until),
            },
            "reports": report_count,
            "measurements": measurement_count,
            "by_property": dict(sorted(by_property.items())),
            "by_well": {str(well): int(count or 0) for well, count in by_well_rows},
        }

    def well_control(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        since: object = None,
        until: object = None,
    ) -> dict[str, Any]:
        """Well-control events in scope, counted by SQLite rather than read into Python.

        Every number here is a count of what the source stated.  ``with_pit_gain`` means the row
        carries a source-stated pit gain - it does **not** mean the event was a kick, and
        ``with_sidpp`` does not mean the well was shut in.  Measurements are never summed: SIDPP in
        psi and SIDPP in bar are two populations with no certified conversion between them, so the
        pressure and volume columns appear only as presence counts.

        Current rows only, consistent with the rest of this class: a superseded reading stays
        readable as history and must not be counted a second time in a live field answer.
        """
        scope_meta = {
            "field_id": field_id or None,
            "project_id": project_id or None,
            "well_id": well_id or None,
            "since": _iso(since),
            "until": _iso(until),
        }
        if not inspect(self.session.get_bind()).has_table(WellControlEvent.__tablename__):
            return {
                "scope": scope_meta,
                "events": 0,
                "wells": 0,
                "by_event_type": {},
                "by_severity": {},
                "by_well": {},
                "first_seen_at": None,
                "last_seen_at": None,
                "undated": 0,
                "with_sidpp": 0,
                "with_sicp": 0,
                "with_pit_gain": 0,
                "with_depth": 0,
                "with_explicit_cause": 0,
                "with_npt_wording": 0,
            }
        wells = self._wells(field_id=field_id, project_id=project_id, well_id=well_id)
        filters: list[Any] = [
            WellControlEvent.well_id.in_(wells),
            WellControlEvent.is_current.is_(True),
        ]
        filters.extend(self._window(WellControlEvent.occurred_at, since, until))

        # One grouped pass produces the totals, the presence counts and the extremes together, so
        # the aggregate costs a constant number of queries whatever the corpus size.
        present = lambda column: func.sum(case((column.isnot(None), 1), else_=0))  # noqa: E731
        totals = self.session.execute(
            select(
                func.count(WellControlEvent.id),
                func.count(func.distinct(WellControlEvent.well_id)),
                func.min(WellControlEvent.occurred_at),
                func.max(WellControlEvent.occurred_at),
                present(WellControlEvent.sidpp_text),
                present(WellControlEvent.sicp_text),
                present(WellControlEvent.pit_gain_text),
                present(WellControlEvent.depth_text),
                present(WellControlEvent.cause),
                present(WellControlEvent.npt_hours_text),
            )
            .select_from(WellControlEvent)
            .where(*filters)
        ).one()
        by_event_type = self._grouped(WellControlEvent, WellControlEvent.event_type, filters)
        by_severity = self._grouped(WellControlEvent, WellControlEvent.severity, filters)
        by_well = self._grouped(WellControlEvent, WellControlEvent.well_id, filters)
        return {
            "scope": scope_meta,
            "events": int(totals[0] or 0),
            "wells": int(totals[1] or 0),
            "by_event_type": by_event_type,
            "by_severity": by_severity,
            "by_well": by_well,
            "first_seen_at": _iso(totals[2]),
            "last_seen_at": _iso(totals[3]),
            "undated": self._undated(
                select(WellControlEvent.id)
                .select_from(WellControlEvent)
                .where(*[f for f in filters if f is not None]),
                WellControlEvent.occurred_at,
            ),
            "with_sidpp": int(totals[4] or 0),
            "with_sicp": int(totals[5] or 0),
            "with_pit_gain": int(totals[6] or 0),
            "with_depth": int(totals[7] or 0),
            "with_explicit_cause": int(totals[8] or 0),
            "with_npt_wording": int(totals[9] or 0),
        }

    def hse(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        since: object = None,
        until: object = None,
    ) -> dict[str, Any]:
        """HSE incidents in scope, keeping well scope and site scope visibly distinct.

        ``well_id`` NULL means *site-scoped*, not "every well".  So a well scope returns only that
        well's own incidents and never picks up a camp or laydown-area incident whose document
        merely belonged to the same project - that would file someone's slip against a hole it never
        happened at.  A field or project scope does include those rows, because that is exactly the
        scope they belong to, and reports them separately as ``site_scoped_incidents`` so a caller
        can see the two populations were not merged.

        ``with_lost_time_wording`` counts rows where the source wrote a lost-time figure.  It is not
        an NPT total and it must not be summed: ADR-32 keeps that wording as evidence precisely
        because a duration in a cell neither creates nor identifies an :class:`NptRecord`.
        """
        scope_meta = {
            "field_id": field_id or None,
            "project_id": project_id or None,
            "well_id": well_id or None,
            "since": _iso(since),
            "until": _iso(until),
        }
        empty = {
            "scope": scope_meta,
            "incidents": 0,
            "well_scoped_incidents": 0,
            "site_scoped_incidents": 0,
            "by_incident_type": {},
            "by_severity": {},
            "by_well": {},
            "by_location": {},
            "with_spill_volume": 0,
            "with_lost_time_wording": 0,
            "with_root_cause": 0,
            "with_immediate_cause": 0,
            "first_seen_at": None,
            "last_seen_at": None,
            "undated": 0,
        }
        if not inspect(self.session.get_bind()).has_table(HseIncident.__tablename__):
            return empty
        if not (field_id or project_id or well_id):
            raise ValidationError(
                "an HSE aggregation needs a scope",
                hint="pass field_id, project_id or well_id",
            )

        # Site-scoped rows have no well to join through, so scope is decided on the incident's own
        # columns as well as on its well - the same reasoning the timeline's HSE scope uses.
        if well_id:
            scope_clause: Any = HseIncident.well_id == well_id
        else:
            wells = self._wells(field_id=field_id, project_id=project_id)
            parts: list[Any] = [HseIncident.well_id.in_(wells)]
            if field_id:
                parts.append(HseIncident.field_id == field_id)
            if project_id:
                parts.append(HseIncident.project_id == project_id)
            scope_clause = or_(*parts)

        filters: list[Any] = [scope_clause, HseIncident.is_current.is_(True)]
        filters.extend(self._window(HseIncident.occurred_at, since, until))

        present = lambda column: func.sum(case((column.isnot(None), 1), else_=0))  # noqa: E731
        totals = self.session.execute(
            select(
                func.count(HseIncident.id),
                func.sum(case((HseIncident.well_id.isnot(None), 1), else_=0)),
                func.sum(case((HseIncident.well_id.is_(None), 1), else_=0)),
                func.min(HseIncident.occurred_at),
                func.max(HseIncident.occurred_at),
                present(HseIncident.spill_volume_text),
                present(HseIncident.npt_hours_text),
                present(HseIncident.root_cause),
                present(HseIncident.immediate_cause),
            )
            .select_from(HseIncident)
            .where(*filters)
        ).one()
        return {
            "scope": scope_meta,
            "incidents": int(totals[0] or 0),
            "well_scoped_incidents": int(totals[1] or 0),
            "site_scoped_incidents": int(totals[2] or 0),
            "by_incident_type": self._grouped(HseIncident, HseIncident.incident_type, filters),
            "by_severity": self._grouped(HseIncident, HseIncident.severity, filters),
            "by_well": self._grouped(HseIncident, HseIncident.well_id, filters),
            "by_location": self._grouped(HseIncident, HseIncident.location_text, filters),
            "with_spill_volume": int(totals[5] or 0),
            "with_lost_time_wording": int(totals[6] or 0),
            "with_root_cause": int(totals[7] or 0),
            "with_immediate_cause": int(totals[8] or 0),
            "first_seen_at": _iso(totals[3]),
            "last_seen_at": _iso(totals[4]),
            "undated": self._undated(
                select(HseIncident.id).select_from(HseIncident).where(*filters),
                HseIncident.occurred_at,
            ),
        }

    def _grouped(self, model: Any, column: Any, filters: Sequence[Any]) -> dict[str, int]:
        """One GROUP BY, deterministically ordered, with NULL folded to an empty-string key.

        A grouped count is the safe aggregation across mixed units: how many rows stated a thing is
        a fact, while a total over their values would silently add psi to bar.
        """
        rows = self.session.execute(
            select(column, func.count())
            .select_from(model)
            .where(*filters)
            .group_by(column)
            .order_by(column)
        ).all()
        return {str(key if key is not None else ""): int(count or 0) for key, count in rows}

    def problems(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        since: object = None,
        until: object = None,
    ) -> dict[str, Any]:
        """Problem occurrences by type, grouped in SQL without loading occurrence rows."""
        scope = self._wells(field_id=field_id, project_id=project_id, well_id=well_id)
        filters: list[Any] = [ProblemOccurrence.well_id.in_(scope)]
        window = self._window(ProblemOccurrence.occurred_at, since, until)
        filters.extend(window)
        total_occurrences, total_wells = self.session.execute(
            select(
                func.count(ProblemOccurrence.id),
                func.count(func.distinct(ProblemOccurrence.well_id)),
            ).where(*filters)
        ).one()
        problem_type_column = func.coalesce(ProblemOccurrence.problem_type, "uncategorised")
        type_rows = self.session.execute(
            select(
                problem_type_column,
                func.count(ProblemOccurrence.id),
                func.count(func.distinct(ProblemOccurrence.well_id)),
                func.count(func.distinct(ProblemOccurrence.section_id)),
                func.sum(
                    case(
                        (
                            func.upper(func.coalesce(ProblemOccurrence.root_cause_status, ""))
                            == "KNOWN",
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.min(ProblemOccurrence.occurred_at),
                func.max(ProblemOccurrence.occurred_at),
            )
            .where(*filters)
            .group_by(problem_type_column)
        ).all()
        hours = problem_hours()
        hour_filters: list[Any] = [hours.c.well_id.in_(scope)]
        if since is not None or until is not None:
            hour_filters.extend(self._window(hours.c.occurred_at, since, until))
        hours_rows = self.session.execute(
            select(hours.c.problem_type, func.sum(hours.c.hours))
            .where(*hour_filters)
            .group_by(hours.c.problem_type)
        ).all()
        hours_by_type = {
            str(name or "uncategorised"): round(float(total or 0.0), 4)
            for name, total in hours_rows
        }
        # Problems whose lost time is deliberately *not* in ``hours_by_type``: they share an event
        # carrying NPT with at least one other problem, and the source never says which of them the
        # hours belong to.  The hours are not lost - the event and NPT aggregations still report
        # them - but attributing them here would multiply one incident across every problem on it.
        # Naming the count is what keeps that a stated limitation instead of a silent gap.
        ambiguous = ambiguous_event_attribution()
        ambiguous_rows = self.session.execute(
            select(ambiguous.c.problem_type, func.count(func.distinct(ambiguous.c.problem_id)))
            .where(ambiguous.c.well_id.in_(scope))
            .group_by(ambiguous.c.problem_type)
        ).all()
        ambiguous_by_type = {
            str(name or "uncategorised"): int(count or 0) for name, count in ambiguous_rows
        }
        well_ids = self.session.execute(
            select(problem_type_column, ProblemOccurrence.well_id).where(*filters).distinct()
        ).all()
        section_ids = self.session.execute(
            select(problem_type_column, ProblemOccurrence.section_id)
            .where(*filters, ProblemOccurrence.section_id.is_not(None))
            .distinct()
        ).all()
        wells_by_type: dict[str, set[str]] = {}
        sections_by_type: dict[str, set[str]] = {}
        for name, value in well_ids:
            wells_by_type.setdefault(str(name), set()).add(str(value))
        for name, value in section_ids:
            sections_by_type.setdefault(str(name), set()).add(str(value))
        by_type: dict[str, dict[str, Any]] = {}
        for name, occurrences, wells, _sections, known, first, last in type_rows:
            key = str(name)
            by_type[key] = {
                "occurrences": int(occurrences or 0),
                "wells": int(wells or 0),
                "well_ids": sorted(wells_by_type.get(key, set())),
                "sections": sorted(sections_by_type.get(key, set())),
                "npt_hours": hours_by_type.get(key),
                "unattributed_npt_problems": ambiguous_by_type.get(key, 0),
                "root_cause_known": int(known or 0),
                "first_seen_at": _iso(first),
                "last_seen_at": _iso(last),
            }
        return {
            "scope": {
                "field_id": field_id or None,
                "project_id": project_id or None,
                "well_id": well_id or None,
                "since": _iso(since),
                "until": _iso(until),
            },
            "occurrences": int(total_occurrences or 0),
            "wells": int(total_wells or 0),
            "unattributed_npt_problems": sum(ambiguous_by_type.values()),
            "by_type": dict(
                sorted(by_type.items(), key=lambda item: (-item[1]["occurrences"], item[0]))
            ),
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
        """Events by category/type/severity, grouped in SQL with no full event-row load."""
        scope = self._wells(field_id=field_id, project_id=project_id, well_id=well_id)
        filters: list[Any] = [WellEvent.well_id.in_(scope)]
        window = self._window(WellEvent.occurred_at, since, until)
        filters.extend(window)
        total = int(
            self.session.execute(select(func.count(WellEvent.id)).where(*filters)).scalar_one() or 0
        )
        category_column = func.coalesce(WellEvent.category, "uncategorised")
        type_column = func.coalesce(WellEvent.event_type, "unclassified")
        category_rows = self.session.execute(
            select(
                category_column,
                func.count(WellEvent.id),
                func.count(func.distinct(WellEvent.well_id)),
                func.min(WellEvent.occurred_at),
                func.max(WellEvent.occurred_at),
            )
            .where(*filters)
            .group_by(category_column)
        ).all()
        category_type_rows = self.session.execute(
            select(category_column, type_column, func.count(WellEvent.id))
            .where(*filters)
            .group_by(category_column, type_column)
        ).all()
        category_well_rows = self.session.execute(
            select(category_column, WellEvent.well_id).where(*filters).distinct()
        ).all()
        severity_column = func.coalesce(WellEvent.severity, "not_stated")
        severity_rows = self.session.execute(
            select(severity_column, func.count(WellEvent.id))
            .where(*filters)
            .group_by(severity_column)
        ).all()
        type_rows = self.session.execute(
            select(type_column, func.count(WellEvent.id)).where(*filters).group_by(type_column)
        ).all()
        types_by_category: dict[str, dict[str, int]] = {}
        for category, event_type, count in category_type_rows:
            types_by_category.setdefault(str(category), {})[str(event_type)] = int(count or 0)
        wells_by_category: dict[str, set[str]] = {}
        for category, event_well in category_well_rows:
            wells_by_category.setdefault(str(category), set()).add(str(event_well))
        by_category = {
            str(category): {
                "events": int(events or 0),
                "types": types_by_category.get(str(category), {}),
                "wells": int(wells or 0),
                "well_ids": sorted(wells_by_category.get(str(category), set())),
                "first_seen_at": _iso(first),
                "last_seen_at": _iso(last),
            }
            for category, events, wells, first, last in category_rows
        }
        return {
            "scope": {
                "field_id": field_id or None,
                "project_id": project_id or None,
                "well_id": well_id or None,
                "since": _iso(since),
                "until": _iso(until),
            },
            "events": total,
            "by_category": dict(
                sorted(by_category.items(), key=lambda item: (-item[1]["events"], item[0]))
            ),
            "by_type": dict(
                sorted(
                    ((str(name), int(count or 0)) for name, count in type_rows),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
            "by_severity": dict(
                sorted(
                    ((str(name), int(count or 0)) for name, count in severity_rows),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
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
        profiles = self._operational_profiles(
            [
                str(value)
                for value in self.session.execute(
                    select(Well.id).where(Well.id != str(well.id))
                ).scalars()
            ]
        )
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
                    # Descriptive profile, deliberately *not* part of the match above.  Two wells are
                    # offsets because they share problem types and hole sizes; what else an offset
                    # recorded is context for the reader, never a reason to rank it.  Nothing here
                    # says an offset is "safer" - a count of recorded incidents is not a risk score.
                    "profile": profiles.get(str(other_id), {}),
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

    def _operational_profiles(self, well_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """What each well recorded, in a constant number of grouped queries.

        Three GROUP BYs answer this for every well at once.  A per-well loop would turn a
        twenty-well comparison into sixty round trips for numbers SQLite can produce in three, and
        the profile is descriptive only - it never selects or ranks a candidate.
        """
        ids = [str(value) for value in well_ids if value]
        if not ids:
            return {}
        profiles: dict[str, dict[str, Any]] = {}

        def bucket(well: Any) -> dict[str, Any]:
            return profiles.setdefault(
                str(well),
                {
                    "well_control_events": 0,
                    "well_control_by_type": {},
                    "hse_incidents": 0,
                    "hse_by_type": {},
                    "hse_by_severity": {},
                },
            )

        for model, type_column, count_key, group_key in (
            (
                WellControlEvent,
                WellControlEvent.event_type,
                "well_control_events",
                "well_control_by_type",
            ),
            (HseIncident, HseIncident.incident_type, "hse_incidents", "hse_by_type"),
        ):
            rows = self.session.execute(
                select(model.well_id, type_column, func.count(model.id))
                .where(model.well_id.in_(ids), model.is_current.is_(True))
                .group_by(model.well_id, type_column)
                .order_by(model.well_id, type_column)
            ).all()
            for well, kind, count in rows:
                entry = bucket(well)
                entry[count_key] += int(count or 0)
                label = str(kind or "")
                entry[group_key][label] = entry[group_key].get(label, 0) + int(count or 0)
        rows = self.session.execute(
            select(HseIncident.well_id, HseIncident.severity, func.count(HseIncident.id))
            .where(HseIncident.well_id.in_(ids), HseIncident.is_current.is_(True))
            .group_by(HseIncident.well_id, HseIncident.severity)
            .order_by(HseIncident.well_id, HseIncident.severity)
        ).all()
        for well, severity, count in rows:
            entry = bucket(well)
            label = str(severity or "")
            entry["hse_by_severity"][label] = entry["hse_by_severity"].get(label, 0) + int(
                count or 0
            )
        return profiles

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
        mud = self.mud(field_id=field_id, project_id=project_id, since=since, until=until)
        well_control = self.well_control(
            field_id=field_id, project_id=project_id, since=since, until=until
        )
        hse = self.hse(field_id=field_id, project_id=project_id, since=since, until=until)
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
            "mud_reports": mud["reports"],
            "mud_measurements": mud["measurements"],
            "mud_by_property": mud["by_property"],
            # V7.3A: the two V7.2 domains join the field snapshot.  Every key above is unchanged -
            # a CLI consumer reading ``npt_hours`` or ``mud_reports`` keeps working - and the new
            # domains arrive as their own sections rather than being folded into ``events``,
            # because a kick and a near miss are not generic well events.
            "well_control_events": well_control["events"],
            "well_control_by_event_type": dict(well_control["by_event_type"]),
            "well_control_undated": well_control["undated"],
            "hse_incidents": hse["incidents"],
            "hse_well_scoped": hse["well_scoped_incidents"],
            "hse_site_scoped": hse["site_scoped_incidents"],
            "hse_by_incident_type": dict(hse["by_incident_type"]),
            "hse_undated": hse["undated"],
            "well_control": well_control,
            "hse": hse,
        }
