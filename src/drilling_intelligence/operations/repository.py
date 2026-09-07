"""The operational spine: reports, operations, events, NPT and problems.

Five kinds of row, one rule above all: **a record never claims more than its source did**.  A
missing timestamp stays missing, an unstated severity stays NULL, an unattributed cause says
``UNKNOWN`` in a column of its own, and a duration is labelled with the basis it was obtained on.
Each of those is enforced here rather than left to the discipline of whoever writes the next
caller, because the failure mode of an operational database is not an exception - it is a field
report that says "11 events, 38 hours, mostly stuck pipe" and cannot answer what the 38 hours were.

A record is also not a document and not a knowledge fact.  The document is the evidence, the fact
(since ADR-0008) is what the evidence asserted, and a row here is the platform's record that
something happened - which is why every table in this module keeps ``document_version_id`` and a
``provenance`` list alongside its own primary key, and why nothing in here is written by an
extractor directly: promotion
(:mod:`drilling_intelligence.operations.promote`) turns stored artefacts into rows, and a person
edits the rows.

Writes are idempotent on ``identity_key`` where a promotion produced the row, so re-running the
promoter over the same artefact neither duplicates nor skips: it updates in place, exactly as the
knowledge layer's content-addressed fact ids do.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..core.enums import (
    CauseStatus,
    ConfirmationStatus,
    DocumentClassification,
    KnowledgeOrigin,
    KnowledgeRelationType,
    RecordState,
)
from ..core.errors import ValidationError
from ..core.ids import new_id
from ..core.lifecycle import CONFIRMATION_LIFECYCLE, Lifecycle
from ..core.vocabulary import (
    event_category as match_category,
)
from ..core.vocabulary import (
    operation_type as match_operation,
)
from ..core.vocabulary import (
    problem_type as match_problem,
)
from ..core.vocabulary import (
    severity as match_severity,
)
from ..database.integrity import create_knowledge_relation
from ..database.models import (
    DdrReport,
    Document,
    DocumentVersion,
    NptRecord,
    ProblemOccurrence,
    Well,
    WellEvent,
    WellOperation,
    WellSection,
)

#: Hours, because that is the unit every report and every total agrees on.
HOURS_PER_DAY = 24.0
_SECONDS_PER_HOUR = 3600.0

#: The bases a caller may claim on an NPT row (see
#: :class:`~drilling_intelligence.core.enums.DurationBasis`, whose members are these strings).
_DURATION_BASES: frozenset[str] = frozenset({"STATED", "MEASURED", "DERIVED"})

#: Document classifications that describe a day's work, and so can become a :class:`DdrReport`.
REPORT_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        DocumentClassification.DDR.value,
        DocumentClassification.NPT.value,
        DocumentClassification.TIME_BREAKDOWN.value,
    }
)

#: The record tables whose ``status`` is a :class:`ConfirmationStatus`.
CONFIRMABLE_MODELS: tuple[type, ...] = (
    DdrReport,
    WellOperation,
    WellEvent,
    NptRecord,
    ProblemOccurrence,
)


def _stamp(value: object) -> datetime | None:
    """Coerce a date/datetime/ISO string to a datetime, or ``None`` when there is nothing to read.

    No timezone is invented and no partial date is completed: ``"14 June 2025"`` is not ISO-8601 and
    is not parsed into a plausible-looking midnight, because a fabricated midnight becomes the
    earliest thing that happened that day in every timeline that reads the column.  Callers that
    need the wording kept have a ``*_text`` column for it, which is what they are for.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _bound(value: object, *, label: str) -> datetime:
    """A range-filter bound that has to parse.

    ``since="14 June"`` is a human typing a date the ISO reader cannot see.  Coercing it to ``None``
    returns everything and reading it as "no match" returns nothing - both look like an answer - so
    the caller is told instead.
    """
    parsed = _stamp(value)
    if parsed is None:
        raise ValidationError(
            f"{label} is not a date or ISO timestamp: {value!r}",
            hint="the platform does not guess at a date it cannot read; use YYYY-MM-DD",
        )
    return parsed


def _cause_state(text: object, status: object, *, field: str) -> str:
    """Pair a cause with its epistemic state, or refuse the combination.

    Two one-sided mistakes are both common and both silent: a cause written without a state (so a
    reader cannot tell whether the source said it or somebody did) and a state with no cause (so the
    record claims to know the reason but has not recorded it).  Each is an error here, naming what to
    pass.
    """
    body = str(text or "").strip()
    declared = str(getattr(status, "value", status) or "").strip().upper()
    if not body:
        if declared and declared != CauseStatus.UNKNOWN.value:
            raise ValidationError(
                f"{field}_status is {declared} but {field} is empty: a cause has to be written down",
                field=field,
                allowed=sorted(state.value for state in CauseStatus),
            )
        return CauseStatus.UNKNOWN.value
    if not declared:
        raise ValidationError(
            f"{field} was given without {field}_status: say KNOWN (the source said so), INFERRED "
            "(a rule concluded it) or CONFLICTED (sources disagree)",
            field=field,
            allowed=sorted(state.value for state in CauseStatus),
        )
    if declared not in {state.value for state in CauseStatus}:
        raise ValidationError(
            f"{field}_status {declared!r} is not a cause status",
            allowed=sorted(state.value for state in CauseStatus),
        )
    return declared


class OperationsRepository:
    """Storage and queries for the operational spine.

    The session is always borrowed, never created here: an ingestion pass writes a document, its
    version, its artefact, its facts and its operational rows in one unit of work, and a repository
    that opened its own session would either miss the uncommitted rows or commit half of them.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- shared bits ----------------------------------------------------------
    def _require_well(self, well_id: str) -> Well:
        well = self.session.get(Well, str(well_id)) if well_id else None
        if well is None:
            raise ValidationError(
                f"no well {well_id!r} in this workspace",
                hint="create the well first: an operational row without a well cannot be found by "
                "field, by project or by the workspace that owns it",
            )
        return well

    def _require_section(self, section_id: str, *, well_id: str) -> str:
        if not section_id:
            return ""
        section = self.session.get(WellSection, str(section_id))
        if section is None:
            raise ValidationError(f"no hole section {section_id!r}")
        if well_id and str(section.well_id) != str(well_id):
            raise ValidationError(
                f"section {section_id!r} belongs to well {section.well_id!r}, not {well_id!r}",
                hint="a record whose section and well disagree is counted in neither well's totals, "
                "so it is refused rather than quietly re-scoped",
            )
        return str(section.id)

    def _require_row(self, model: type, row_id: str, *, label: str, well_id: str = "") -> str:
        """Check a cross-record link, including that the two rows speak about the same well.

        The second half matters as much as the first: an NPT row pointing at an event on another well
        satisfies every foreign key in the database and corrupts a total.
        """
        if not row_id:
            return ""
        row = self.session.get(model, str(row_id))
        if row is None:
            raise ValidationError(f"no {label} {row_id!r}")
        linked = getattr(row, "well_id", None)
        if well_id and linked and str(linked) != str(well_id):
            raise ValidationError(
                f"{label} {row_id!r} belongs to well {linked!r}, not {well_id!r}",
                label=label,
            )
        return str(row.id)

    def _find_by_identity(self, model: type, identity_key: str) -> Any:
        if not identity_key:
            return None
        return self.session.execute(
            select(model).where(model.identity_key == identity_key)
        ).scalar_one_or_none()

    @staticmethod
    def _extra(match: Any, attributes: Mapping[str, Any] | None) -> dict[str, Any]:
        """Carry the source's own wording when the vocabulary had to fall back on it."""
        extra = dict(attributes or {})
        if not match.recognised and match.raw:
            extra[match.key] = match.raw
        return extra

    # -- reports --------------------------------------------------------------
    def register_report(
        self,
        *,
        well_id: str,
        document_id: str = "",
        document_version_id: str = "",
        report_number: str = "",
        report_date: object = None,
        report_date_text: str = "",
        shift: str = "",
        summary: str = "",
        status: ConfirmationStatus | str | None = None,
        record_state: RecordState | str = RecordState.ACTUAL,
        document_status: str = "",
        provenance: Sequence[Mapping[str, Any]] | None = None,
        origin: str = KnowledgeOrigin.MANUAL.value,
        created_by: str = "system",
        attributes: Mapping[str, Any] | None = None,
    ) -> DdrReport:
        """Register - or re-find - the report one well's day produced.

        ``report_date`` is the day the report is *for*.  It is never taken from a file's mtime, and
        when the document gave no date the row keeps ``None`` with ``report_date_text`` holding what
        the source actually wrote, so a person can see what was there to read.
        """
        well = self._require_well(well_id)
        if document_version_id:
            version = self.session.get(DocumentVersion, str(document_version_id))
            if version is None:
                raise ValidationError(f"no document version {document_version_id!r}")
            existing = self.session.execute(
                select(DdrReport).where(
                    DdrReport.document_version_id == version.id, DdrReport.well_id == well.id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            document_id = document_id or str(version.document_id)
        if document_id and self.session.get(Document, str(document_id)) is None:
            raise ValidationError(f"no document {document_id!r}")
        origin_value = str(getattr(origin, "value", origin))
        row = DdrReport(
            id=new_id("ddr"),
            well_id=well.id,
            document_id=document_id or None,
            document_version_id=document_version_id or None,
            report_number=(str(report_number).strip() or None) if report_number else None,
            report_date=_stamp(report_date),
            report_date_text=(report_date_text or None),
            shift=shift or None,
            record_state=str(getattr(record_state, "value", record_state)),
            # A person writing a report down has vouched for it; a script promoting one has not.
            status=str(
                status
                if status is not None
                else (
                    ConfirmationStatus.CONFIRMED.value
                    if origin_value == KnowledgeOrigin.MANUAL.value
                    else ConfirmationStatus.CANDIDATE.value
                )
            ),
            document_status=document_status or None,
            summary=str(summary or ""),
            provenance=[dict(item) for item in provenance or ()],
            origin=origin_value,
            created_by=created_by,
            attributes=dict(attributes or {}),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_report(self, report_id: str) -> DdrReport | None:
        return self.session.get(DdrReport, str(report_id)) if report_id else None

    def list_reports(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        since: object = None,
        until: object = None,
        status: str = "",
        limit: int = 200,
    ) -> list[DdrReport]:
        """Reports, newest first, filtered by scope and by the date the reports are *for*.

        Undated reports are excluded by ``since``/``until`` - a range cannot include them - and a
        caller who needs them asks without a bound.  The alternative, quietly treating "unknown" as
        "in range", makes every report without a date appear in every period.
        """
        statement = select(DdrReport)
        if well_id or field_id or project_id:
            statement = statement.join(Well, Well.id == DdrReport.well_id)
        if well_id:
            statement = statement.where(DdrReport.well_id == well_id)
        if field_id:
            statement = statement.where(Well.field_id == field_id)
        if project_id:
            statement = statement.where(Well.project_id == project_id)
        if since is not None:
            statement = statement.where(DdrReport.report_date >= _bound(since, label="since"))
        if until is not None:
            statement = statement.where(DdrReport.report_date <= _bound(until, label="until"))
        if status:
            statement = statement.where(DdrReport.status == str(status))
        statement = self._ordered(statement, DdrReport.report_date.desc(), DdrReport.id)
        return list(self.session.execute(statement.limit(limit)).scalars())

    # -- operations -----------------------------------------------------------
    def record_operation(
        self,
        *,
        well_id: str,
        operation_type: str = "",
        label: str = "",
        description: str = "",
        started_at: object = None,
        ended_at: object = None,
        period_text: str = "",
        section_id: str = "",
        report_id: str = "",
        record_state: RecordState | str = RecordState.ACTUAL,
        status: ConfirmationStatus | str = ConfirmationStatus.CANDIDATE,
        depth_md: tuple[float, str] | None = None,
        rig_id: str = "",
        service_company_id: str = "",
        provenance: Sequence[Mapping[str, Any]] | None = None,
        origin: str = KnowledgeOrigin.MANUAL.value,
        created_by: str = "system",
        identity_key: str = "",
        document_id: str = "",
        document_version_id: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> WellOperation:
        """Write - or re-find - one operation on a well's timeline.

        ``operation_type`` is the token the queries group by and ``label`` is what the source called
        it; pass either.  A planned operation and the same operation as executed are different rows
        (``record_state``), which is what lets plan-vs-actual be a join instead of a memory.
        """
        well = self._require_well(well_id)
        found = self._find_by_identity(WellOperation, identity_key)
        if found is not None:
            return found
        if not str(operation_type or label or "").strip():
            raise ValidationError("an operation needs a type or a label: both were empty")
        match = match_operation(operation_type or label)
        started, ended = _stamp(started_at), _stamp(ended_at)
        if started and ended and ended < started:
            raise ValidationError(
                "an operation cannot end before it starts",
                started_at=started.isoformat(),
                ended_at=ended.isoformat(),
            )
        row = WellOperation(
            id=new_id("op"),
            well_id=well.id,
            section_id=self._require_section(section_id, well_id=well.id) or None,
            report_id=self._require_row(DdrReport, report_id, label="report", well_id=well.id)
            or None,
            operation_type=match.token,
            label=str(label or match.raw or match.token)[:200],
            description=str(description or ""),
            started_at=started,
            ended_at=ended,
            period_text=period_text or None,
            record_state=str(getattr(record_state, "value", record_state)),
            status=str(getattr(status, "value", status)),
            rig_id=rig_id or None,
            service_company_id=service_company_id or None,
            provenance=[dict(item) for item in provenance or ()],
            origin=str(getattr(origin, "value", origin)),
            created_by=created_by,
            identity_key=identity_key or None,
            document_id=document_id or None,
            document_version_id=document_version_id or None,
            attributes=self._extra(match, attributes),
        )
        if depth_md is not None:
            value, unit = depth_md
            row.depth_md_value = float(value)
            row.depth_md_unit = str(unit)
        self.session.add(row)
        self.session.flush()
        return row

    def list_operations(
        self,
        *,
        well_id: str = "",
        report_id: str = "",
        section_id: str = "",
        operation_type: str = "",
        record_state: str = "",
        status: str = "",
        limit: int = 500,
    ) -> list[WellOperation]:
        statement: Select = select(WellOperation)
        if well_id:
            statement = statement.where(WellOperation.well_id == well_id)
        if report_id:
            statement = statement.where(WellOperation.report_id == report_id)
        if section_id:
            statement = statement.where(WellOperation.section_id == section_id)
        if operation_type:
            statement = statement.where(
                WellOperation.operation_type == match_operation(str(operation_type)).token
            )
        if record_state:
            statement = statement.where(WellOperation.record_state == str(record_state))
        if status:
            statement = statement.where(WellOperation.status == str(status))
        statement = self._ordered(statement, WellOperation.started_at.asc(), WellOperation.id)
        return list(self.session.execute(statement.limit(limit)).scalars())

    # -- events ---------------------------------------------------------------
    def record_event(
        self,
        *,
        well_id: str,
        event_type: str,
        category: str = "",
        label: str = "",
        severity_text: str = "",
        description: str = "",
        occurred_at: object = None,
        ended_at: object = None,
        occurred_at_text: str = "",
        operation_id: str = "",
        report_id: str = "",
        section_id: str = "",
        depth: tuple[float, str] | None = None,
        equipment_item_id: str = "",
        rig_id: str = "",
        service_company_id: str = "",
        status: ConfirmationStatus | str = ConfirmationStatus.CANDIDATE,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        origin: str = KnowledgeOrigin.MANUAL.value,
        created_by: str = "system",
        identity_key: str = "",
        document_id: str = "",
        document_version_id: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> WellEvent:
        """One occurrence: what happened, when the source said, and how bad the source said it was.

        Severity is read from ``severity_text`` and nowhere else - there is no numeric parameter on
        purpose.  A report that does not characterise an event must not end up with a severity,
        because the average of invented severities is the first number a reviewer discards and the
        last one anybody can trace.
        """
        well = self._require_well(well_id)
        found = self._find_by_identity(WellEvent, identity_key)
        if found is not None:
            return found
        if not str(event_type or "").strip():
            raise ValidationError("an event needs a type")
        kind = match_problem(event_type)
        category_match = match_category(category)
        level = match_severity(severity_text)
        started, ended = _stamp(occurred_at), _stamp(ended_at)
        if started and ended and ended < started:
            raise ValidationError(
                "an event cannot end before it started",
                occurred_at=started.isoformat(),
                ended_at=ended.isoformat(),
            )
        row = WellEvent(
            id=new_id("ev"),
            well_id=well.id,
            operation_id=self._require_row(
                WellOperation, operation_id, label="operation", well_id=well.id
            )
            or None,
            section_id=self._require_section(section_id, well_id=well.id) or None,
            report_id=self._require_row(DdrReport, report_id, label="report", well_id=well.id)
            or None,
            category=category_match.token,
            event_type=kind.token,
            label=str(label or kind.raw or event_type)[:200],
            description=str(description or ""),
            occurred_at=started,
            ended_at=ended,
            occurred_at_text=occurred_at_text or None,
            severity=level.value if level else None,
            equipment_item_id=equipment_item_id or None,
            rig_id=rig_id or None,
            service_company_id=service_company_id or None,
            status=str(getattr(status, "value", status)),
            provenance=[dict(item) for item in provenance or ()],
            origin=str(getattr(origin, "value", origin)),
            created_by=created_by,
            identity_key=identity_key or None,
            document_id=document_id or None,
            document_version_id=document_version_id or None,
            attributes=self._extra(kind, self._extra(category_match, attributes)),
        )
        if depth is not None:
            value, unit = depth
            row.depth_md_value = float(value)
            row.depth_md_unit = str(unit)
        self.session.add(row)
        self.session.flush()
        if operation_id:
            create_knowledge_relation(
                self.session,
                source_type="well_operation",
                source_id=str(operation_id),
                relation=KnowledgeRelationType.OPERATION_HAS_EVENT.value,
                target_type="well_event",
                target_id=row.id,
                provenance=[dict(item) for item in provenance or ()],
            )
        return row

    def list_events(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        operation_id: str = "",
        report_id: str = "",
        category: str = "",
        event_type: str = "",
        severity: str = "",
        status: str = "",
        since: object = None,
        until: object = None,
        limit: int = 500,
    ) -> list[WellEvent]:
        statement: Select = self._scope_statement(
            select(WellEvent),
            WellEvent,
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
        )
        for column, value in (
            (WellEvent.operation_id, operation_id),
            (WellEvent.report_id, report_id),
            (WellEvent.category, category),
            (WellEvent.severity, severity),
            (WellEvent.status, status),
        ):
            if value:
                statement = statement.where(column == str(value))
        if event_type:
            statement = statement.where(WellEvent.event_type == match_problem(event_type).token)
        if since is not None:
            statement = statement.where(WellEvent.occurred_at >= _bound(since, label="since"))
        if until is not None:
            statement = statement.where(WellEvent.occurred_at <= _bound(until, label="until"))
        statement = self._ordered(statement, WellEvent.occurred_at.asc(), WellEvent.id)
        return list(self.session.execute(statement.limit(limit)).scalars())

    # -- NPT ------------------------------------------------------------------
    def record_npt(
        self,
        *,
        well_id: str,
        category: str = "",
        code: str = "",
        description: str = "",
        started_at: object = None,
        ended_at: object = None,
        started_at_text: str = "",
        duration_hours: float | None = None,
        duration_text: str = "",
        duration_basis: str = "STATED",
        cause: str = "",
        immediate_cause: str = "",
        immediate_cause_status: str = "",
        root_cause: str = "",
        root_cause_status: str = "",
        event_id: str = "",
        operation_id: str = "",
        section_id: str = "",
        report_id: str = "",
        rig_id: str = "",
        service_company_id: str = "",
        equipment_item_id: str = "",
        cost_impact: tuple[float, str] | None = None,
        status: ConfirmationStatus | str = ConfirmationStatus.CANDIDATE,
        confidence: float | None = None,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        origin: str = KnowledgeOrigin.MANUAL.value,
        created_by: str = "system",
        identity_key: str = "",
        document_id: str = "",
        document_version_id: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> NptRecord:
        """One block of non-productive time, with its duration's basis and its causes' states.

        Three rules, all of them about not lying with a missing number:

        *   a duration is quoted (``STATED``), measured between two timestamps the source gave
            (``MEASURED``, computed here when only the stamps were passed), or derived from partial
            information (``DERIVED``).  With neither a number nor two stamps the row keeps ``None``
            and the aggregate reports it as unquantified.
        *   a cause without a state, or a state without a cause, is refused (:func:`_cause_state`).
        *   ``code`` is the report's own reason code, kept verbatim in ``subcategory``, so the token
            an aggregation groups by never becomes the only surviving copy of what was written.
        """
        well = self._require_well(well_id)
        found = self._find_by_identity(NptRecord, identity_key)
        if found is not None:
            return found
        match = match_problem(code or category)
        start, end = _stamp(started_at), _stamp(ended_at)
        measured = None if duration_hours is None else float(duration_hours)
        basis = str(getattr(duration_basis, "value", duration_basis) or "").strip().upper() or None
        if basis not in _DURATION_BASES:
            raise ValidationError(
                f"duration_basis {basis!r} is not a basis",
                allowed=sorted(_DURATION_BASES),
            )
        if measured is None and start and end:
            if end < start:
                raise ValidationError(
                    "an NPT block cannot end before it starts",
                    started_at=start.isoformat(),
                    ended_at=end.isoformat(),
                )
            # Nobody gave a duration, both bounds are there and the arithmetic is exact: this is a
            # measurement, and saying so is what lets a reader recompute the number.
            measured = (end - start).total_seconds() / _SECONDS_PER_HOUR
            basis = "MEASURED"
        if measured is not None and basis == "MEASURED" and not (start and end):
            raise ValidationError(
                "a MEASURED duration needs a start and an end to have been measured between them",
                started_at=_iso(start),
                ended_at=_iso(end),
            )
        if measured is not None and measured < 0:
            raise ValidationError("NPT duration cannot be negative", duration_hours=measured)
        row = NptRecord(
            id=new_id("npt"),
            well_id=well.id,
            event_id=self._require_row(WellEvent, event_id, label="event", well_id=well.id) or None,
            operation_id=self._require_row(
                WellOperation, operation_id, label="operation", well_id=well.id
            )
            or None,
            section_id=self._require_section(section_id, well_id=well.id) or None,
            report_id=self._require_row(DdrReport, report_id, label="report", well_id=well.id)
            or None,
            category=match.token,
            subcategory=(str(code).strip() or None) if code else None,
            description=str(description or ""),
            started_at=start,
            ended_at=end,
            started_at_text=started_at_text or None,
            duration_hours=measured,
            duration_text=(str(duration_text).strip() or None) if duration_text else None,
            duration_basis=basis or "STATED",
            cause=(str(cause).strip() or None) if cause else None,
            immediate_cause=(str(immediate_cause).strip() or None) if immediate_cause else None,
            immediate_cause_status=_cause_state(
                immediate_cause, immediate_cause_status, field="immediate_cause"
            ),
            root_cause=(str(root_cause).strip() or None) if root_cause else None,
            root_cause_status=_cause_state(root_cause, root_cause_status, field="root_cause"),
            rig_id=rig_id or None,
            service_company_id=service_company_id or None,
            equipment_item_id=equipment_item_id or None,
            status=str(getattr(status, "value", status)),
            confidence=None if confidence is None else float(confidence),
            provenance=[dict(item) for item in provenance or ()],
            origin=str(getattr(origin, "value", origin)),
            created_by=created_by,
            identity_key=identity_key or None,
            document_id=document_id or None,
            document_version_id=document_version_id or None,
            attributes=self._extra(match, attributes),
        )
        if cost_impact is not None:
            value, unit = cost_impact
            row.cost_impact_value = float(value)
            row.cost_impact_unit = str(unit)
        self.session.add(row)
        self.session.flush()
        if event_id:
            create_knowledge_relation(
                self.session,
                source_type="well_event",
                source_id=str(event_id),
                relation=KnowledgeRelationType.EVENT_CAUSES_NPT.value,
                target_type="npt_record",
                target_id=row.id,
                provenance=[dict(item) for item in provenance or ()],
            )
        return row

    def list_npt(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        category: str = "",
        event_id: str = "",
        operation_id: str = "",
        status: str = "",
        root_cause_status: str = "",
        since: object = None,
        until: object = None,
        limit: int = 500,
    ) -> list[NptRecord]:
        statement: Select = self._scope_statement(
            select(NptRecord),
            NptRecord,
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
        )
        if category:
            statement = statement.where(NptRecord.category == match_problem(category).token)
        for column, value in (
            (NptRecord.event_id, event_id),
            (NptRecord.operation_id, operation_id),
            (NptRecord.status, status),
            (NptRecord.root_cause_status, root_cause_status),
        ):
            if value:
                statement = statement.where(column == str(value))
        if since is not None:
            statement = statement.where(NptRecord.started_at >= _bound(since, label="since"))
        if until is not None:
            statement = statement.where(NptRecord.started_at <= _bound(until, label="until"))
        statement = self._ordered(statement, NptRecord.started_at.asc(), NptRecord.id)
        return list(self.session.execute(statement.limit(limit)).scalars())

    def npt_totals(
        self,
        *,
        group_by: str = "category",
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        since: object = None,
        until: object = None,
    ) -> list[dict[str, Any]]:
        """Hours lost per group, counted by the database and nothing else.

        This is the only place a total is produced, and it is a ``GROUP BY``: no rounding, no
        smoothing, no model.  A row whose duration is unknown is counted in ``records`` and reported
        in ``records_without_duration``, so a total that could not be computed is visible instead of
        silently reading as zero hours - which is the difference between "this field lost 38 hours"
        and "3 of 11 rows never said how long".
        """
        columns = {
            "category": NptRecord.category,
            "well": NptRecord.well_id,
            "operation": NptRecord.operation_id,
            "event": NptRecord.event_id,
            "report": NptRecord.report_id,
            "rig": NptRecord.rig_id,
            "service_company": NptRecord.service_company_id,
            "root_cause_status": NptRecord.root_cause_status,
            "duration_basis": NptRecord.duration_basis,
            "subcategory": NptRecord.subcategory,
        }
        key = columns.get(str(group_by))
        if key is None:
            raise ValidationError(
                f"cannot group NPT by {group_by!r}",
                allowed=sorted(columns),
                hint="grouping is a column, so a new axis is one entry in this table",
            )
        statement = self._scope_statement(
            select(
                key,
                func.count(),
                func.count(NptRecord.duration_hours),
                func.sum(NptRecord.duration_hours),
                func.max(NptRecord.started_at),
                func.min(NptRecord.started_at),
            ),
            NptRecord,
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
        )
        if since is not None:
            statement = statement.where(NptRecord.started_at >= _bound(since, label="since"))
        if until is not None:
            statement = statement.where(NptRecord.started_at <= _bound(until, label="until"))
        totals: list[dict[str, Any]] = []
        rows = self.session.execute(statement.group_by(key)).all()
        for value, records, with_duration, hours, latest, earliest in rows:
            total = None if hours is None else round(float(hours), 4)
            totals.append(
                {
                    "group": "" if value is None else str(value),
                    "records": int(records),
                    "records_with_duration": int(with_duration or 0),
                    "records_without_duration": int(records) - int(with_duration or 0),
                    "hours": total,
                    "first_seen_at": _iso(earliest),
                    "last_seen_at": _iso(latest),
                }
            )
        # Hours first, then the group: a reader scanning the table wants the worst at the top, and
        # two groups with no hours still need the same order in every run.
        totals.sort(key=lambda item: (-(item["hours"] or 0.0), item["group"]))
        return totals

    # -- problems -------------------------------------------------------------
    def record_problem(
        self,
        *,
        well_id: str,
        problem_type: str = "",
        code: str = "",
        description: str = "",
        occurred_at: object = None,
        event_id: str = "",
        npt_id: str = "",
        operation_id: str = "",
        section_id: str = "",
        depth_from: tuple[float, str] | None = None,
        depth_to: tuple[float, str] | None = None,
        hole_size_in: float | None = None,
        formation: str = "",
        immediate_cause: str = "",
        immediate_cause_status: str = "",
        root_cause: str = "",
        root_cause_status: str = "",
        contributing_factors: Sequence[str] | None = None,
        corrective_action: str = "",
        preventive_action: str = "",
        status: ConfirmationStatus | str = ConfirmationStatus.CANDIDATE,
        confidence: float | None = None,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        origin: str = KnowledgeOrigin.MANUAL.value,
        created_by: str = "system",
        identity_key: str = "",
        document_id: str = "",
        document_version_id: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> ProblemOccurrence:
        """One problem, seen once, at one well.

        A problem is not an event: the event is what happened, the problem is the *type* field
        intelligence groups by, and keeping them apart is what makes "6 wells, 11 events, all in the
        8 1/2 section" a query rather than a paragraph.  ``corrective_action`` (what was done at the
        time) and ``preventive_action`` (what will be done differently) are separate columns because
        a report nearly always has the first and only a lesson has the second.
        """
        well = self._require_well(well_id)
        found = self._find_by_identity(ProblemOccurrence, identity_key)
        if found is not None:
            return found
        match = match_problem(problem_type or code)
        if not match.raw:
            raise ValidationError(
                "a problem needs a type or the source's own code",
                hint="an empty problem row would be counted as a problem of no kind",
            )
        row = ProblemOccurrence(
            id=new_id("prob"),
            well_id=well.id,
            section_id=self._require_section(section_id, well_id=well.id) or None,
            operation_id=self._require_row(
                WellOperation, operation_id, label="operation", well_id=well.id
            )
            or None,
            event_id=self._require_row(WellEvent, event_id, label="event", well_id=well.id) or None,
            npt_id=self._require_row(NptRecord, npt_id, label="NPT record", well_id=well.id)
            or None,
            problem_type=match.token,
            code=(str(code).strip() or None) if code else None,
            description=str(description or ""),
            occurred_at=_stamp(occurred_at),
            hole_size_in=None if hole_size_in is None else float(hole_size_in),
            formation=(str(formation).strip() or None) if formation else None,
            immediate_cause=(str(immediate_cause).strip() or None) if immediate_cause else None,
            immediate_cause_status=_cause_state(
                immediate_cause, immediate_cause_status, field="immediate_cause"
            ),
            root_cause=(str(root_cause).strip() or None) if root_cause else None,
            root_cause_status=_cause_state(root_cause, root_cause_status, field="root_cause"),
            contributing_factors=[str(item) for item in contributing_factors or ()],
            corrective_action=(str(corrective_action).strip() or None)
            if corrective_action
            else None,
            preventive_action=(str(preventive_action).strip() or None)
            if preventive_action
            else None,
            status=str(getattr(status, "value", status)),
            confidence=None if confidence is None else float(confidence),
            provenance=[dict(item) for item in provenance or ()],
            origin=str(getattr(origin, "value", origin)),
            created_by=created_by,
            identity_key=identity_key or None,
            document_id=document_id or None,
            document_version_id=document_version_id or None,
            attributes=self._extra(match, attributes),
        )
        for prefix, pair in (("depth_from", depth_from), ("depth_to", depth_to)):
            if pair is not None:
                value, unit = pair
                setattr(row, f"{prefix}_value", float(value))
                setattr(row, f"{prefix}_unit", str(unit))
        self.session.add(row)
        self.session.flush()
        provenance_list = [dict(item) for item in provenance or ()]
        if event_id:
            create_knowledge_relation(
                self.session,
                source_type="well_event",
                source_id=str(event_id),
                relation=KnowledgeRelationType.EVENT_HAS_PROBLEM.value,
                target_type="problem_occurrence",
                target_id=row.id,
                provenance=provenance_list,
            )
        if npt_id:
            create_knowledge_relation(
                self.session,
                source_type="problem_occurrence",
                source_id=row.id,
                relation=KnowledgeRelationType.PROBLEM_CAUSES_NPT.value,
                target_type="npt_record",
                target_id=str(npt_id),
                provenance=provenance_list,
            )
        return row

    def list_problems(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        problem_type: str = "",
        event_id: str = "",
        operation_id: str = "",
        section_id: str = "",
        root_cause_status: str = "",
        status: str = "",
        limit: int = 500,
    ) -> list[ProblemOccurrence]:
        statement: Select = self._scope_statement(
            select(ProblemOccurrence),
            ProblemOccurrence,
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
        )
        if problem_type:
            statement = statement.where(
                ProblemOccurrence.problem_type == match_problem(str(problem_type)).token
            )
        for column, value in (
            (ProblemOccurrence.event_id, event_id),
            (ProblemOccurrence.operation_id, operation_id),
            (ProblemOccurrence.section_id, section_id),
            (ProblemOccurrence.root_cause_status, root_cause_status),
            (ProblemOccurrence.status, status),
        ):
            if value:
                statement = statement.where(column == str(value))
        statement = self._ordered(
            statement, ProblemOccurrence.occurred_at.asc(), ProblemOccurrence.id
        )
        return list(self.session.execute(statement.limit(limit)).scalars())

    # -- summary --------------------------------------------------------------
    def get_row(self, table: str, row_id: str) -> Any:
        """One confirmable record, looked up by its table name.

        Refuses a table whose status is not a confirmation status rather than returning a row the
        caller then cannot act on: the CLI's ``confirm`` command takes a name a person typed, and the
        useful failure is "that table has no confirmation status", not a validation error raised three
        frames away.
        """
        key = str(table or "").strip()
        for model in CONFIRMABLE_MODELS:
            if key in {model.__tablename__, model.__name__}:
                row = self.session.get(model, str(row_id))
                if row is None:
                    raise ValidationError(f"no {model.__tablename__} row {row_id!r}")
                return row
        raise ValidationError(
            f"{key!r} is not a confirmable record table",
            allowed=[model.__tablename__ for model in CONFIRMABLE_MODELS],
        )

    def record_summary(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
    ) -> dict[str, Any]:
        """What the operational history in scope adds up to - every figure a ``COUNT`` or ``SUM``.

        The unknowns are counted alongside the totals on purpose.  A report that says "18.5 h lost,
        3 events" is only trustworthy if the reader can also see that one of those events has no
        duration and another has no date, because those are the gaps that make the total move later.
        """
        payload: dict[str, Any] = {}
        for label, model in (
            ("reports", DdrReport),
            ("operations", WellOperation),
            ("events", WellEvent),
            ("npt", NptRecord),
            ("problems", ProblemOccurrence),
        ):
            statement = self._scope_statement(
                select(func.count()).select_from(model),
                model,
                well_id=well_id,
                field_id=field_id,
                project_id=project_id,
            )
            payload[label] = int(self.session.execute(statement).scalar_one() or 0)

        statement = self._scope_statement(
            select(
                func.count(NptRecord.id),
                func.count(NptRecord.duration_hours),
                func.sum(NptRecord.duration_hours),
                func.count(NptRecord.started_at),
                func.count(NptRecord.document_version_id),
            ),
            NptRecord,
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
        )
        rows, with_duration, hours, dated, promoted = self.session.execute(statement).one()
        payload["npt"] = {
            "rows": int(rows or 0),
            "with_duration": int(with_duration or 0),
            "unknown_duration": int(rows or 0) - int(with_duration or 0),
            "undated": int(rows or 0) - int(dated or 0),
            "promoted": int(promoted or 0),
            "total_hours": None if not rows else round(float(hours or 0.0), 4),
        }

        by_status = self._scope_statement(
            select(NptRecord.status, func.count(NptRecord.id)).group_by(NptRecord.status),
            NptRecord,
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
        )
        payload["npt_by_status"] = {
            str(status): int(count) for status, count in self.session.execute(by_status).all()
        }
        payload["npt_by_category"] = self.npt_totals(
            group_by="category",
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
        )
        return payload

    # -- links ----------------------------------------------------------------
    def link(
        self,
        *,
        source_type: str,
        source_id: str,
        relation: str,
        target_type: str,
        target_id: str,
        weight: float = 1.0,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        note: str = "",
    ) -> Any:
        """Assert an edge between two records, through the one graph the platform has.

        Thin on purpose: :func:`~drilling_intelligence.database.integrity.create_knowledge_relation`
        is the only sanctioned write path for edges (it validates both endpoints exist, and re-asserting
        an edge strengthens it instead of tripping the unique constraint), and a second wrapper that
        knew a second set of rules is how a graph ends up with two meanings for one arrow.
        """
        return create_knowledge_relation(
            self.session,
            source_type=source_type,
            source_id=str(source_id),
            relation=str(relation),
            target_type=target_type,
            target_id=str(target_id),
            weight=float(weight),
            provenance=[dict(item) for item in provenance or ()],
            note=note or None,
        )

    def links_from(
        self, *, source_type: str, source_id: str, relation: str = "", limit: int = 200
    ) -> list[Any]:
        """The edges leaving a record, for the "based on" trees a reviewer opens."""
        from ..database.models import KnowledgeRelation

        statement = select(KnowledgeRelation).where(
            KnowledgeRelation.source_type == source_type,
            KnowledgeRelation.source_id == str(source_id),
        )
        if relation:
            statement = statement.where(KnowledgeRelation.relation == str(relation))
        statement = self._ordered(statement, KnowledgeRelation.relation, KnowledgeRelation.id)
        return list(self.session.execute(statement.limit(limit)).scalars())

    # -- lifecycle ------------------------------------------------------------
    def set_status(
        self,
        row: Any,
        new_status: ConfirmationStatus | str,
        *,
        by: str = "",
        reason: str = "",
        lifecycle: Lifecycle = CONFIRMATION_LIFECYCLE,
    ) -> str:
        """Move a record between candidate, confirmed and rejected - validated, attributed, noted.

        ``by`` is required for anything but a step back to ``CANDIDATE``: a status a reader cannot
        attribute is a status a reader has to re-check, and the whole value of confirming a row is
        that nobody else has to.
        """
        if not isinstance(row, CONFIRMABLE_MODELS):
            raise ValidationError(
                f"{type(row).__name__} rows have no confirmation status",
                allowed=[model.__tablename__ for model in CONFIRMABLE_MODELS],
            )
        return set_record_status(
            self.session,
            row,
            new_status,
            by=by,
            reason=reason,
            lifecycle=lifecycle,
        )

    # -- internals ------------------------------------------------------------
    def _scope_statement(
        self,
        statement: Select,
        model: type,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
    ) -> Select:
        """Filter by scope, joining the well only when the caller needs one.

        Records carry ``well_id`` and nothing above it: field and project are reached by joining the
        well, because a copy of the hierarchy on every row is a copy that can disagree with the
        hierarchy - and the checker that would catch the disagreement is easier to reason about when
        there is one place the hierarchy lives.
        """
        if field_id or project_id:
            statement = statement.join(Well, Well.id == model.well_id)
            if field_id:
                statement = statement.where(Well.field_id == field_id)
            if project_id:
                statement = statement.where(Well.project_id == project_id)
        if well_id:
            statement = statement.where(model.well_id == well_id)
        return statement

    def _ordered(self, statement: Select, *keys: Any) -> Select:
        """Order a statement, undated rows last, ids last of all.

        Two clauses, both necessary.  ``NULLS LAST`` because SQLite and PostgreSQL disagree by
        default on where a NULL sorts, and a timeline that changes order when the system of record
        moves is a bug nobody attributes to the database.  ``id`` because many rows share a
        timestamp - a report with a date and no time produces several - and without a final
        tie-break the page layout decides the order, which a reader experiences as the platform
        changing its mind between runs.
        """
        ordered = [
            key.nulls_last() if hasattr(key, "nulls_last") and key is not None else key
            for key in keys
        ]
        return statement.order_by(*ordered)


def set_record_status(
    session: Session,
    row: Any,
    new_status: ConfirmationStatus | str,
    *,
    by: str = "",
    reason: str = "",
    lifecycle: Lifecycle = CONFIRMATION_LIFECYCLE,
) -> str:
    """The shared half of a status change: validate, write, attribute, keep the reason.

    A free function rather than a method because the lessons, engineering and intelligence
    repositories change the same kind of status on their own tables, and a status written from one
    surface has to be indistinguishable from one written from another - otherwise the audit trail
    describes the caller instead of the decision.
    """
    target = lifecycle.parse(new_status)
    current = lifecycle.parse(getattr(row, "status", "") or lifecycle.initial)
    if target is not current and not str(by or "").strip():
        raise ValidationError(
            f"a {type(row).__name__} cannot move from {current.value} to {target.value} "
            "without an author",
            hint="pass by=<who decided>",
        )
    if target is not current:
        lifecycle.require(current, target)
        row.status = target.value
        note = str(getattr(row, "status_note", "") or "")
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        detail = f"{stamp} {current.value}->{target.value} by {by}"
        if reason:
            detail = f"{detail}: {reason}"
        if "status_note" in type(row).__table__.c:
            row.status_note = f"{note}\n{detail}".strip()
        else:
            attributes = dict(getattr(row, "attributes", None) or {})
            history = list(attributes.get("status_history") or [])
            history.append(
                {
                    "at": stamp,
                    "from": current.value,
                    "to": target.value,
                    "by": by,
                    "reason": reason or "",
                }
            )
            attributes["status_history"] = history
            row.attributes = attributes
    session.flush()
    return target.value


def _iso(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


__all__ = [
    "CONFIRMABLE_MODELS",
    "HOURS_PER_DAY",
    "REPORT_CLASSIFICATIONS",
    "OperationsRepository",
    "set_record_status",
]
