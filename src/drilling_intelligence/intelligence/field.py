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
    LessonLearned,
    MudMeasurement,
    MudReport,
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
        mud = self.mud(field_id=field_id, project_id=project_id, since=since, until=until)
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
        }
