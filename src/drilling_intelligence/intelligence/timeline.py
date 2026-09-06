"""The well timeline: one ordered list built from the records that already have dates.

A timeline is a *query*, not a table.  Nothing in this module writes anything: every entry is a row from
a domain record whose own timestamp is what makes it appear - which is why the ordering can be trusted
after a re-promotion, why an edit to an event shows up immediately, and why there is no reconciliation
job between "what happened" and "what the screen lists".

Two rules keep it honest:

*   **A record with no date is listed, but never dated.**  A DDR that says "14 June 2025" in prose has
    no timestamp column, and inventing midnight on 2025-06-14 would put a fact into the database that no
    source stated.  Such a row appears in the ``undated`` tail with the wording it actually carries, and
    :func:`build_timeline`'s ``since``/``until`` filters exclude it rather than guess it in or out.
*   **Ordering is total and reproducible.**  Dated entries sort by timestamp, then by a fixed kind
    order (a report's day begins before the operations inside it), then by table and id.  Two records
    stamped at the same instant therefore always come back in the same sequence, which is what makes a
    timeline assertable in a test and readable in a diff.

The kinds a drilling reader expects but this build has no dated record for - total depth reached, the
end-of-well report, a BHA change that was never written as an activity - are simply absent.  Depth is
not a date, and a timeline that invented one would be a story rather than an index.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.errors import ValidationError
from ..database.models import (
    DdrReport,
    DrillingProgram,
    LessonLearned,
    NptRecord,
    ProblemOccurrence,
    ProcedureRecord,
    Well,
    WellEvent,
    WellOperation,
)

__all__ = ["TIMELINE_KINDS", "TimelineEntry", "build_timeline", "entry_comparator"]


@dataclass(frozen=True)
class TimelineEntry:
    """One row of a well's history, said in the record's own words.

    ``at`` is what the record states, never what it implies; ``text`` is the wording kept for a record
    whose date is prose (``"14 June 2025"``), so an undated entry still tells a reader what it saw.
    ``provenance`` rides along because an answer without a source is not an answer this platform gives.
    """

    at: datetime | None
    kind: str
    table: str
    row_id: str
    well_id: str
    title: str
    detail: str
    section_id: str = ""
    document_version_id: str = ""
    text: str = ""
    provenance: tuple[dict[str, Any], ...] = ()

    @property
    def dated(self) -> bool:
        return self.at is not None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["at"] = self.at.isoformat() if self.at is not None else None
        payload["dated"] = self.dated
        return payload


#: Every kind of entry this module can produce, in the order two entries at the same instant sort in.
#: A day is reported first, then what was done in it, then what went wrong, then what it cost, then what
#: was learnt - which is roughly the order an engineer reads an incident in.
TIMELINE_KINDS: tuple[str, ...] = (
    "well",
    "program",
    "procedure",
    "report",
    "operation",
    "event",
    "npt",
    "problem",
    "lesson",
)
_KIND_RANK: dict[str, int] = {kind: index for index, kind in enumerate(TIMELINE_KINDS)}

_WELL_EVENTS: tuple[tuple[str, str, str], ...] = (
    # (column on well, kind, title)
    ("spud_date", "spud", "Spud"),
    ("completion_date", "completion", "Completion"),
)


def _stamp(value: object) -> datetime | None:
    """The timestamp a record carries, as a datetime - or ``None``, which is the honest answer often."""
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


def entry_comparator(entry: TimelineEntry) -> tuple[int, float, int, str, str]:
    """The sort key: dated first, then by instant, kind order, table and id.

    Exposed because a caller that merges a timeline with something else (a screen that interleaves
    calendar entries, an export that unions two wells) needs the *same* total order, not an approximation
    of it.
    """
    if entry.at is None:
        # Undated entries sort after every dated one, among themselves by the same stable sub-keys.
        return (1, 0.0, _KIND_RANK.get(entry.kind, len(TIMELINE_KINDS)), entry.table, entry.row_id)
    return (
        0,
        entry.at.timestamp(),
        _KIND_RANK.get(entry.kind, len(TIMELINE_KINDS)),
        entry.table,
        entry.row_id,
    )


def _scope(statement, model: Any, *, well_id: str, field_id: str, project_id: str) -> Any:
    """Restrict a query to one well, or to every well in a field or project, in one join.

    The scope columns on a record point at a well; the field and project are reached through it, so an
    aggregate over a field cannot be answered by a ``field_id`` column that does not exist on these
    tables - and adding one would be the denormalisation that goes stale the day a well is moved.
    """
    if well_id:
        return statement.where(model.well_id == well_id)
    wanted: list[Any] = []
    if field_id:
        wanted.append(Well.field_id == field_id)
    if project_id:
        wanted.append(Well.project_id == project_id)
    if not wanted:
        raise ValidationError(
            "a timeline needs a scope",
            hint="pass well_id, field_id or project_id",
        )
    return statement.where(model.well_id.in_(select(Well.id).where(or_(*wanted))))


def _entry(
    *,
    kind: str,
    table: str,
    row_id: str,
    well_id: str,
    title: str,
    detail: str,
    at: object,
    text: str = "",
    section_id: str = "",
    document_version_id: str = "",
    provenance: Sequence[dict[str, Any]] | None = None,
) -> TimelineEntry:
    return TimelineEntry(
        at=_stamp(at),
        kind=kind,
        table=table,
        row_id=str(row_id),
        well_id=str(well_id or ""),
        title=title,
        detail=detail,
        section_id=str(section_id or ""),
        document_version_id=str(document_version_id or ""),
        text=str(text or ""),
        provenance=tuple(dict(item) for item in provenance or ()),
    )


def build_timeline(
    session: Session,
    *,
    well_id: str = "",
    field_id: str = "",
    project_id: str = "",
    kinds: Sequence[str] = (),
    since: object = None,
    until: object = None,
    include_undated: bool | None = None,
    limit: int = 0,
) -> list[TimelineEntry]:
    """Every dated thing the records say happened, in one deterministic list.

    One query per record table (nine, whatever the scope), each selecting only the columns an entry
    needs - not the ORM objects - so a busy field reads its timeline without loading provenance blobs
    for rows the caller filters out afterwards.  ``kinds`` narrows the tables visited at all, which is
    the difference between a screen that shows only NPT and one that scans every record twice.
    ``include_undated`` defaults to ``None``: undated records are listed when the whole scope is asked for
    and dropped when a ``since``/``until`` window is, and either can be forced.  Well milestone entries
    exist only for a well scope - a field-wide list of every well that has no spud date recorded is noise,
    not information - and a document appears here only through the report row that states its date, so a
    DDR and its source file never show up as two events on the same day.
    """
    wanted = {str(kind) for kind in kinds} or set(TIMELINE_KINDS)
    unknown = sorted(wanted - set(TIMELINE_KINDS))
    if unknown:
        raise ValidationError(
            f"no timeline kind named {', '.join(unknown)}", known=list(TIMELINE_KINDS)
        )
    low = _stamp(since)
    high = _stamp(until)
    # ``None`` means "follow the window".  A timeline asked for a period answers with the records that can
    # be placed inside it - an undated record is neither inside nor outside, and quietly keeping it would
    # make a windowed answer look like a complete one.  A timeline asked for a whole well lists the undated
    # records at the end instead, so that what the field has not dated stays visible.  True or False
    # overrides that either way.
    undated_wanted = (
        include_undated
        if include_undated is not None
        else not (low is not None or high is not None)
    )
    entries: list[TimelineEntry] = []

    if well_id and ("well" in wanted):
        well = session.get(Well, well_id)
        if well is None:
            raise ValidationError(f"no well {well_id!r}")
        for column, _kind, title in _WELL_EVENTS:
            value = getattr(well, column, None)
            if value is None and not undated_wanted:
                continue
            entries.append(
                _entry(
                    kind="well",
                    table="well",
                    row_id=well.id,
                    well_id=well.id,
                    title=f"{title}: {well.name}",
                    detail=f"{column.replace('_date', '').replace('_', ' ')} date recorded"
                    if value is not None
                    else "no date recorded on the well",
                    at=value,
                    text="" if value is not None else "no date recorded",
                    provenance=(well.attributes or {}).get("provenance") or (),
                )
            )

    if "report" in wanted:
        statement = select(
            DdrReport.id,
            DdrReport.well_id,
            DdrReport.report_date,
            DdrReport.report_date_text,
            DdrReport.report_number,
            DdrReport.summary,
            DdrReport.document_version_id,
            DdrReport.provenance,
        )
        statement = _scope(
            statement, DdrReport, well_id=well_id, field_id=field_id, project_id=project_id
        )
        for row_id, row_well, at, text, number, summary, version, source in session.execute(
            statement
        ):
            entries.append(
                _entry(
                    kind="report",
                    table="ddr_report",
                    row_id=row_id,
                    well_id=row_well,
                    title=f"Report {number}" if number else "Daily report",
                    detail=str(summary or "")[:200],
                    at=at,
                    text=text or "",
                    document_version_id=version or "",
                    provenance=source,
                )
            )

    if "operation" in wanted:
        statement = select(
            WellOperation.id,
            WellOperation.well_id,
            WellOperation.section_id,
            WellOperation.operation_type,
            WellOperation.label,
            WellOperation.description,
            WellOperation.started_at,
            WellOperation.ended_at,
            WellOperation.period_text,
            WellOperation.document_version_id,
            WellOperation.provenance,
        )
        statement = _scope(
            statement, WellOperation, well_id=well_id, field_id=field_id, project_id=project_id
        )
        for (
            row_id,
            row_well,
            section,
            operation_type,
            label,
            description,
            started,
            ended,
            period,
            version,
            source,
        ) in session.execute(statement):
            title = str(label or operation_type or "operation")
            detail = str(description or "")[:200]
            if started is not None:
                entries.append(
                    _entry(
                        kind="operation",
                        table="well_operation",
                        row_id=f"{row_id}:start",
                        well_id=row_well,
                        title=f"{title} started",
                        detail=detail,
                        at=started,
                        text=period or "",
                        section_id=section or "",
                        document_version_id=version or "",
                        provenance=source,
                    )
                )
            if ended is not None:
                entries.append(
                    _entry(
                        kind="operation",
                        table="well_operation",
                        row_id=f"{row_id}:end",
                        well_id=row_well,
                        title=f"{title} ended",
                        detail=detail,
                        at=ended,
                        text=period or "",
                        section_id=section or "",
                        document_version_id=version or "",
                        provenance=source,
                    )
                )
            if started is None and ended is None and undated_wanted:
                entries.append(
                    _entry(
                        kind="operation",
                        table="well_operation",
                        row_id=row_id,
                        well_id=row_well,
                        title=title,
                        detail=detail,
                        at=None,
                        text=period or "no period stated",
                        section_id=section or "",
                        document_version_id=version or "",
                        provenance=source,
                    )
                )

    if "event" in wanted:
        statement = select(
            WellEvent.id,
            WellEvent.well_id,
            WellEvent.section_id,
            WellEvent.label,
            WellEvent.event_type,
            WellEvent.description,
            WellEvent.occurred_at,
            WellEvent.occurred_at_text,
            WellEvent.severity,
            WellEvent.category,
            WellEvent.document_version_id,
            WellEvent.provenance,
        )
        statement = _scope(
            statement, WellEvent, well_id=well_id, field_id=field_id, project_id=project_id
        )
        for (
            row_id,
            row_well,
            section,
            label,
            event_type,
            description,
            at,
            text,
            severity,
            category,
            version,
            source,
        ) in session.execute(statement):
            entries.append(
                _entry(
                    kind="event",
                    table="well_event",
                    row_id=row_id,
                    well_id=row_well,
                    title=str(label or event_type or "event"),
                    detail=_join(str(description or "")[:200], severity, category),
                    at=at,
                    text=text or "",
                    section_id=section or "",
                    document_version_id=version or "",
                    provenance=source,
                )
            )

    if "npt" in wanted:
        statement = select(
            NptRecord.id,
            NptRecord.well_id,
            NptRecord.section_id,
            NptRecord.category,
            NptRecord.subcategory,
            NptRecord.description,
            NptRecord.started_at,
            NptRecord.duration_hours,
            NptRecord.duration_text,
            NptRecord.duration_basis,
            NptRecord.document_version_id,
            NptRecord.provenance,
        )
        statement = _scope(
            statement, NptRecord, well_id=well_id, field_id=field_id, project_id=project_id
        )
        for (
            row_id,
            row_well,
            section,
            category,
            subcategory,
            description,
            at,
            hours,
            hours_text,
            basis,
            version,
            source,
        ) in session.execute(statement):
            entries.append(
                _entry(
                    kind="npt",
                    table="npt_record",
                    row_id=row_id,
                    well_id=row_well,
                    title=f"NPT - {category or 'unclassified'}",
                    detail=_join(
                        str(description or "")[:200],
                        _hours(hours, hours_text, basis),
                        subcategory,
                    ),
                    at=at,
                    text=hours_text or "",
                    section_id=section or "",
                    document_version_id=version or "",
                    provenance=source,
                )
            )

    if "problem" in wanted:
        statement = select(
            ProblemOccurrence.id,
            ProblemOccurrence.well_id,
            ProblemOccurrence.section_id,
            ProblemOccurrence.problem_type,
            ProblemOccurrence.description,
            ProblemOccurrence.occurred_at,
            ProblemOccurrence.root_cause_status,
            ProblemOccurrence.document_version_id,
            ProblemOccurrence.provenance,
        )
        statement = _scope(
            statement, ProblemOccurrence, well_id=well_id, field_id=field_id, project_id=project_id
        )
        for (
            row_id,
            row_well,
            section,
            problem_type,
            description,
            at,
            root_status,
            version,
            source,
        ) in session.execute(statement):
            entries.append(
                _entry(
                    kind="problem",
                    table="problem_occurrence",
                    row_id=row_id,
                    well_id=row_well,
                    title=f"Problem - {problem_type or 'unclassified'}",
                    detail=_join(
                        str(description or "")[:200], f"root cause {str(root_status or '').lower()}"
                    ),
                    at=at,
                    section_id=section or "",
                    document_version_id=version or "",
                    provenance=source,
                )
            )

    if "lesson" in wanted:
        statement = select(
            LessonLearned.id,
            LessonLearned.well_id,
            LessonLearned.field_id,
            LessonLearned.title,
            LessonLearned.created_at,
            LessonLearned.approved_at,
            LessonLearned.status,
            LessonLearned.provenance,
        )
        rows = session.execute(
            _scope_lesson(statement, well_id=well_id, field_id=field_id, project_id=project_id)
        )
        for row_id, row_well, _field, title, created, approved, status, source in rows:
            for suffix, at in (("captured", created), ("approved", approved)):
                if at is None:
                    continue
                entries.append(
                    _entry(
                        kind="lesson",
                        table="lesson_learned",
                        row_id=f"{row_id}:{suffix}",
                        well_id=row_well or "",
                        title=f"Lesson {suffix}: {title}",
                        detail=f"status {status}",
                        at=at,
                        provenance=source,
                    )
                )

    if "program" in wanted or "procedure" in wanted:
        entries.extend(
            _revision_entries(
                session,
                well_id=well_id,
                field_id=field_id,
                project_id=project_id,
                wanted=wanted,
            )
        )

    entries = [entry for entry in entries if _within(entry, low, high, undated_wanted)]
    entries.sort(key=entry_comparator)
    if limit and limit > 0:
        return entries[: int(limit)]
    return entries


def _revision_entries(
    session: Session,
    *,
    well_id: str,
    field_id: str,
    project_id: str,
    wanted: set[str],
) -> list[TimelineEntry]:
    """Program and procedure revisions, as dated by the record's own creation and approval.

    A revision belongs on a timeline twice: once when it was written, once when somebody approved it.
    Those are different facts - and on a well where the approval came eleven days later, the eleven days
    are the part a reader cares about.
    """
    payload: list[TimelineEntry] = []
    pairs: list[tuple[Any, str, str]] = []
    if "program" in wanted:
        pairs.append((DrillingProgram, "program", "Programme"))
    if "procedure" in wanted:
        pairs.append((ProcedureRecord, "procedure", "Procedure"))
    for model, kind, label in pairs:
        statement = select(
            model.id,
            model.well_id,
            model.field_id,
            model.title,
            model.revision,
            model.revision_label,
            model.status,
            model.created_at,
            model.approved_at,
            model.provenance,
        )
        if well_id:
            statement = statement.where(model.well_id == well_id)
        else:
            wanted_scopes = []
            if field_id:
                wanted_scopes.append(model.field_id == field_id)
                wanted_scopes.append(
                    model.well_id.in_(select(Well.id).where(Well.field_id == field_id))
                )
            if project_id:
                wanted_scopes.append(model.project_id == project_id)
                wanted_scopes.append(
                    model.well_id.in_(select(Well.id).where(Well.project_id == project_id))
                )
            if not wanted_scopes:
                raise ValidationError(
                    "a timeline needs a scope", hint="pass well_id, field_id or project_id"
                )
            statement = statement.where(or_(*wanted_scopes))
        for (
            row_id,
            row_well,
            _field,
            title,
            revision,
            revision_label,
            status,
            created,
            approved,
            source,
        ) in session.execute(statement):
            detail = f"rev {revision}"
            if revision_label:
                detail = f"{detail} ({revision_label})"
            detail = f"{detail}, {label.lower()} status {status}"
            for suffix, at in (("written", created), ("approved", approved)):
                if at is None:
                    continue
                payload.append(
                    _entry(
                        kind=kind,
                        table=model.__tablename__,
                        row_id=f"{row_id}:{suffix}",
                        well_id=row_well or "",
                        title=f"{label} {title} {suffix}",
                        detail=detail,
                        at=at,
                        provenance=source,
                    )
                )
    return payload


def _scope_lesson(statement: Any, *, well_id: str, field_id: str, project_id: str) -> Any:
    """Lessons are scoped by their own field/project columns as well as by their well.

    A lesson learnt on one well but written against the field is field knowledge: excluding it from a
    field timeline because its ``well_id`` names a different well would hide the entry the timeline
    exists to surface.
    """
    if well_id:
        return statement.where(LessonLearned.well_id == well_id)
    scopes = []
    if field_id:
        scopes.extend(
            [
                LessonLearned.field_id == field_id,
                LessonLearned.well_id.in_(select(Well.id).where(Well.field_id == field_id)),
            ]
        )
    if project_id:
        scopes.extend(
            [
                LessonLearned.project_id == project_id,
                LessonLearned.well_id.in_(select(Well.id).where(Well.project_id == project_id)),
            ]
        )
    if not scopes:
        raise ValidationError(
            "a timeline needs a scope", hint="pass well_id, field_id or project_id"
        )
    return statement.where(or_(*scopes))


def _within(
    entry: TimelineEntry, low: datetime | None, high: datetime | None, include_undated: bool
) -> bool:
    if entry.at is None:
        return include_undated
    if low is not None and entry.at < low:
        return False
    return not (high is not None and entry.at > high)


def _join(*parts: object) -> str:
    return " - ".join(str(part).strip() for part in parts if str(part or "").strip())


def _hours(value: object, text: object, basis: object) -> str:
    """How long it took, phrased so the reader can tell a stated figure from a computed one."""
    if value is not None:
        number = f"{float(value):g} h"
        if str(basis or "").upper() == "STATED":
            return f"{number} (stated)"
        return f"{number} (from start and end)"
    if str(text or "").strip():
        return str(text).strip()
    return ""
