"""Governed authoring for manual daily drilling reports.

This is an authoring boundary, not a document-class promotion handler. Draft text and
activities are explicitly entered by an actor, frozen on submission, and linked to
candidate ``WellOperation`` rows. A reviewer confirms or rejects those rows through the
existing review service. No source fields, timestamps, durations, or engineering values
are inferred here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..core.enums import KNOWN_OPERATION_TYPES
from ..core.errors import ValidationError
from ..core.hashing import sha256_obj
from ..core.ids import new_id
from ..database.audit import AuditLog
from ..database.models import DdrActivity, DdrReport, Well, WellOperation
from ..database.session import Database
from ..operations.repository import OperationsRepository


@dataclass(frozen=True)
class DdrActivityInput:
    operation_type: str
    label: str
    description: str = ""
    period_text: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> DdrActivityInput:
        operation_type = str(raw.get("operation_type") or "").strip().lower()
        label = _required(str(raw.get("label") or ""), "activity label", 200)
        if operation_type not in KNOWN_OPERATION_TYPES:
            raise ValidationError(
                f"operation type {operation_type!r} is not in the registered operation vocabulary",
                allowed=sorted(KNOWN_OPERATION_TYPES),
            )
        description = _bounded(str(raw.get("description") or ""), "activity description", 4000)
        period_text = _bounded(str(raw.get("period_text") or ""), "activity period", 200)
        return cls(operation_type, label, description, period_text)

    def to_dict(self) -> dict[str, str]:
        return {
            "operation_type": self.operation_type,
            "label": self.label,
            "description": self.description,
            "period_text": self.period_text,
        }


@dataclass(frozen=True)
class DdrWorkspaceRecord:
    report_id: str
    well_id: str
    report_number: str
    report_date: str
    report_date_text: str
    shift: str
    summary: str
    status: str
    authoring_state: str
    revision_group_id: str
    revision_number: int
    supersedes_report_id: str
    is_current: bool
    created_by: str
    origin: str
    document_version_id: str
    created_at: str
    updated_at: str
    provenance: tuple[dict[str, Any], ...]
    activities: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "well_id": self.well_id,
            "report_number": self.report_number,
            "report_date": self.report_date,
            "report_date_text": self.report_date_text,
            "shift": self.shift,
            "summary": self.summary,
            "status": self.status,
            "authoring_state": self.authoring_state,
            "revision_group_id": self.revision_group_id,
            "revision_number": self.revision_number,
            "supersedes_report_id": self.supersedes_report_id,
            "is_current": self.is_current,
            "created_by": self.created_by,
            "origin": self.origin,
            "document_version_id": self.document_version_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provenance": [dict(item) for item in self.provenance],
            "activities": [dict(item) for item in self.activities],
        }


class DdrAuthoringService:
    """CRUD + lifecycle for one workspace's manually authored DDRs."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def list_reports(
        self, well_id: str, *, include_history: bool = True
    ) -> tuple[dict[str, Any], ...]:
        with self._database.read_only() as session:
            statement = select(DdrReport).where(DdrReport.well_id == str(well_id))
            if not include_history:
                statement = statement.where(DdrReport.is_current.is_(True))
            statement = statement.order_by(
                DdrReport.report_date.desc().nulls_last(),
                DdrReport.revision_group_id,
                DdrReport.revision_number.desc(),
                DdrReport.id,
            )
            return tuple(
                self._to_record(session, row).to_dict() for row in session.scalars(statement)
            )

    def get(self, report_id: str) -> dict[str, Any]:
        with self._database.read_only() as session:
            row = session.get(DdrReport, str(report_id))
            if row is None:
                raise ValidationError(f"DDR report {report_id!r} was not found")
            return self._to_record(session, row).to_dict()

    def create_draft(
        self,
        *,
        well_id: str,
        report_date: str,
        report_number: str = "",
        shift: str = "",
        summary: str = "",
        activities: Sequence[Mapping[str, Any]] = (),
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        actor = _required(actor, "actor", 80)
        reason = _required(reason, "reason", 2000)
        parsed_date, date_text = _report_day(report_date)
        content = _content(
            report_number=report_number,
            shift=shift,
            summary=summary,
            activities=activities,
        )
        with self._database.unit_of_work() as session:
            well = self._well(session, well_id)
            report = OperationsRepository(session).register_report(
                well_id=well.id,
                report_number=content.report_number,
                report_date=parsed_date,
                report_date_text=date_text,
                shift=content.shift,
                summary=content.summary,
                status="CANDIDATE",
                origin="MANUAL",
                created_by=actor,
                provenance=_report_provenance(
                    actor=actor, reason=reason, action="created", revision=1
                ),
                attributes={"authoring_channel": "DDR_WORKSPACE"},
            )
            report.authoring_state = "DRAFT"
            report.revision_group_id = new_id("ddrgrp")
            report.revision_number = 1
            report.is_current = True
            self._replace_activities(
                session, report, content.activities, actor=actor, reason=reason
            )
            self._audit(
                session,
                report,
                action="ddr.draft_created",
                actor=actor,
                reason=reason,
                content=content,
            )
            session.flush()
            return self._to_record(session, report).to_dict()

    def save_draft(
        self,
        report_id: str,
        *,
        expected_updated_at: str,
        report_date: str,
        report_number: str = "",
        shift: str = "",
        summary: str = "",
        activities: Sequence[Mapping[str, Any]] = (),
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        actor = _required(actor, "actor", 80)
        reason = _required(reason, "reason", 2000)
        expected_updated_at = _required(expected_updated_at, "expected_updated_at", 80)
        parsed_date, date_text = _report_day(report_date)
        content = _content(
            report_number=report_number,
            shift=shift,
            summary=summary,
            activities=activities,
        )
        with self._database.unit_of_work() as session:
            report = self._report(session, report_id)
            self._assert_editable(report, expected_updated_at)
            self._claim_snapshot(session, report, expected_updated_at)
            report.report_date = parsed_date
            report.report_date_text = date_text
            report.report_number = content.report_number or None
            report.shift = content.shift or None
            report.summary = content.summary
            report.provenance = list(report.provenance or ()) + _report_provenance(
                actor=actor,
                reason=reason,
                action="saved",
                revision=report.revision_number,
            )
            self._replace_activities(
                session, report, content.activities, actor=actor, reason=reason
            )
            self._audit(
                session,
                report,
                action="ddr.draft_saved",
                actor=actor,
                reason=reason,
                content=content,
            )
            report.updated_at = _next_updated_at(report.updated_at)
            session.flush()
            return self._to_record(session, report).to_dict()

    def submit(
        self, report_id: str, *, expected_updated_at: str, actor: str, reason: str
    ) -> dict[str, Any]:
        actor = _required(actor, "actor", 80)
        reason = _required(reason, "reason", 2000)
        expected_updated_at = _required(expected_updated_at, "expected_updated_at", 80)
        with self._database.unit_of_work() as session:
            report = self._report(session, report_id)
            self._assert_editable(report, expected_updated_at)
            self._claim_snapshot(session, report, expected_updated_at)
            activities = list(
                session.scalars(
                    select(DdrActivity)
                    .where(DdrActivity.ddr_report_id == report.id)
                    .order_by(DdrActivity.sequence, DdrActivity.id)
                )
            )
            if not report.summary.strip() and not activities:
                raise ValidationError("a DDR needs a summary or at least one authored activity")
            operation_links: list[tuple[DdrActivity, WellOperation]] = []
            for activity in activities:
                operation = WellOperation(
                    id=new_id("operation"),
                    well_id=report.well_id,
                    report_id=report.id,
                    operation_type=activity.operation_type,
                    label=activity.label,
                    description=activity.description,
                    period_text=activity.period_text or None,
                    record_state="ACTUAL",
                    status="CANDIDATE",
                    provenance=[
                        {
                            "source_kind": "manual_authoring",
                            "ddr_report_id": report.id,
                            "ddr_activity_id": activity.id,
                            "revision_number": report.revision_number,
                            "actor": actor,
                            "reason": reason,
                            "sequence": activity.sequence,
                        }
                    ],
                    origin="MANUAL",
                    created_by=actor,
                    attributes={"authoring_channel": "DDR_WORKSPACE"},
                )
                session.add(operation)
                operation_links.append((activity, operation))
            # Persist operational parents before setting the activity foreign keys; without this
            # explicit flush SQLAlchemy has no ORM relationship edge to order those writes.
            session.flush()
            for activity, operation in operation_links:
                activity.operation_id = operation.id
            report.authoring_state = "SUBMITTED"
            report.updated_at = _next_updated_at(report.updated_at)
            report.provenance = list(report.provenance or ()) + _report_provenance(
                actor=actor,
                reason=reason,
                action="submitted",
                revision=report.revision_number,
            )
            session.flush()
            self._audit(
                session,
                report,
                action="ddr.submitted",
                actor=actor,
                reason=reason,
                content=self._content_from_session(session, report),
            )
            for activity in activities:
                if activity.operation_id:
                    self._audit_operation(
                        session,
                        activity,
                        actor=actor,
                        reason=reason,
                        report=report,
                    )
            session.flush()
            return self._to_record(session, report).to_dict()

    def create_revision(
        self,
        report_id: str,
        *,
        expected_updated_at: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        actor = _required(actor, "actor", 80)
        reason = _required(reason, "reason", 2000)
        expected_updated_at = _required(expected_updated_at, "expected_updated_at", 80)
        with self._database.unit_of_work() as session:
            previous = self._report(session, report_id)
            self._assert_fresh(previous, expected_updated_at)
            if not previous.is_current:
                raise ValidationError("a historical DDR revision cannot be revised")
            if previous.origin != "MANUAL" or previous.document_version_id:
                raise ValidationError(
                    "source-derived DDRs are not editable in the authoring workspace; create a "
                    "separate manual report instead"
                )
            if previous.authoring_state != "SUBMITTED":
                raise ValidationError("save or submit the current draft before starting a revision")
            group_id = previous.revision_group_id
            if not group_id:
                raise ValidationError("manual DDR revision has no revision-group identity")
            self._claim_snapshot(session, previous, expected_updated_at)
            activities = list(
                session.scalars(
                    select(DdrActivity)
                    .where(DdrActivity.ddr_report_id == previous.id)
                    .order_by(DdrActivity.sequence, DdrActivity.id)
                )
            )
            previous.is_current = False
            previous.updated_at = _next_updated_at(previous.updated_at)
            prior_operations = list(
                session.scalars(select(WellOperation).where(WellOperation.report_id == previous.id))
            )
            for operation in prior_operations:
                operation.is_current = False
            revision = DdrReport(
                id=new_id("ddr"),
                well_id=previous.well_id,
                document_id=None,
                document_version_id=None,
                report_number=previous.report_number,
                report_date=previous.report_date,
                report_date_text=previous.report_date_text,
                shift=previous.shift,
                record_state="ACTUAL",
                status="CANDIDATE",
                document_status=None,
                summary=previous.summary,
                authoring_state="DRAFT",
                revision_group_id=group_id,
                revision_number=previous.revision_number + 1,
                supersedes_report_id=previous.id,
                is_current=True,
                provenance=_report_provenance(
                    actor=actor,
                    reason=reason,
                    action="revision_started",
                    revision=previous.revision_number + 1,
                ),
                origin="MANUAL",
                created_by=actor,
                attributes={"authoring_channel": "DDR_WORKSPACE"},
            )
            session.add(revision)
            session.flush()
            cloned = tuple(
                DdrActivityInput(
                    operation_type=activity.operation_type,
                    label=activity.label,
                    description=activity.description,
                    period_text=activity.period_text or "",
                )
                for activity in activities
            )
            self._replace_activities(session, revision, cloned, actor=actor, reason=reason)
            self._audit(
                session,
                previous,
                action="ddr.revision_superseded",
                actor=actor,
                reason=reason,
                content=self._content_from_session(session, previous),
            )
            self._audit(
                session,
                revision,
                action="ddr.revision_created",
                actor=actor,
                reason=reason,
                content=self._content_from_session(session, revision),
            )
            session.flush()
            return self._to_record(session, revision).to_dict()

    def _report(self, session: Session, report_id: str) -> DdrReport:
        row = session.get(DdrReport, str(report_id))
        if row is None:
            raise ValidationError(f"DDR report {report_id!r} was not found")
        return row

    def _well(self, session: Session, well_id: str) -> Well:
        row = session.get(Well, str(well_id))
        if row is None:
            raise ValidationError(f"well {well_id!r} was not found")
        return row

    def _assert_fresh(self, report: DdrReport, expected_updated_at: str) -> None:
        actual = _stamp(report.updated_at)
        if actual != _stamp(expected_updated_at):
            raise ValidationError(
                "DDR draft changed since it was loaded; reload before continuing",
                expected_updated_at=expected_updated_at,
                actual_updated_at=actual,
            )

    def _claim_snapshot(
        self, session: Session, report: DdrReport, expected_updated_at: str
    ) -> None:
        """Atomically reserve the exact row snapshot before a multi-row authoring write.

        A read-then-write check alone permits two SQLite sessions to load the same token and both
        proceed before either commits. The no-op conditional UPDATE obtains the database writer
        lock and rechecks status, currentness, revision and timestamp in the database; only one
        competing transaction can claim that snapshot.
        """
        self._assert_fresh(report, expected_updated_at)
        result = session.execute(
            update(DdrReport)
            .where(
                DdrReport.id == report.id,
                DdrReport.updated_at == report.updated_at,
                DdrReport.status == report.status,
                DdrReport.authoring_state == report.authoring_state,
                DdrReport.revision_number == report.revision_number,
                DdrReport.is_current.is_(report.is_current),
            )
            .values(updated_at=DdrReport.updated_at)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise ValidationError(
                "DDR draft changed since it was loaded; reload before continuing",
                expected_updated_at=expected_updated_at,
            )

    def _assert_editable(self, report: DdrReport, expected_updated_at: str) -> None:
        self._assert_fresh(report, expected_updated_at)
        if not report.is_current:
            raise ValidationError("a historical DDR revision is read-only")
        if report.authoring_state != "DRAFT":
            raise ValidationError("only an unsubmitted DDR draft can be edited")
        if report.status != "CANDIDATE":
            raise ValidationError("a DDR with a review decision must be revised, not edited")

    def _replace_activities(
        self,
        session: Session,
        report: DdrReport,
        activities: Sequence[DdrActivityInput],
        *,
        actor: str,
        reason: str,
    ) -> None:
        existing = list(
            session.scalars(select(DdrActivity).where(DdrActivity.ddr_report_id == report.id))
        )
        if any(activity.operation_id for activity in existing):
            raise ValidationError("submitted DDR activities cannot be replaced")
        for activity in existing:
            session.delete(activity)
        # The sequence key is unique per report; flush removals before reusing sequence 1..N.
        session.flush()
        for sequence, value in enumerate(activities, start=1):
            session.add(
                DdrActivity(
                    id=new_id("ddractivity"),
                    ddr_report_id=report.id,
                    sequence=sequence,
                    operation_type=value.operation_type,
                    label=value.label,
                    description=value.description,
                    period_text=value.period_text or None,
                    provenance=[
                        {
                            "source_kind": "manual_authoring",
                            "actor": actor,
                            "reason": reason,
                            "revision_number": report.revision_number,
                            "sequence": sequence,
                        }
                    ],
                )
            )

    def _to_record(self, session: Session, report: DdrReport) -> DdrWorkspaceRecord:
        activities = session.scalars(
            select(DdrActivity)
            .where(DdrActivity.ddr_report_id == report.id)
            .order_by(DdrActivity.sequence, DdrActivity.id)
        )
        provenance = [dict(item) for item in report.provenance or () if isinstance(item, Mapping)]
        attributes = report.attributes if isinstance(report.attributes, Mapping) else {}
        status_history = attributes.get("status_history")
        if isinstance(status_history, Sequence) and not isinstance(status_history, str | bytes):
            provenance.extend(
                {
                    "source_kind": "review_decision",
                    "action": f"{item.get('from', '')}->{item.get('to', '')}",
                    "actor": str(item.get("by") or ""),
                    "reason": str(item.get("reason") or ""),
                    "at": str(item.get("at") or ""),
                }
                for item in status_history
                if isinstance(item, Mapping)
            )
        return DdrWorkspaceRecord(
            report_id=str(report.id),
            well_id=str(report.well_id),
            report_number=str(report.report_number or ""),
            report_date=report.report_date.date().isoformat() if report.report_date else "",
            report_date_text=str(report.report_date_text or ""),
            shift=str(report.shift or ""),
            summary=str(report.summary or ""),
            status=str(report.status),
            authoring_state=str(report.authoring_state),
            revision_group_id=str(report.revision_group_id or ""),
            revision_number=int(report.revision_number),
            supersedes_report_id=str(report.supersedes_report_id or ""),
            is_current=bool(report.is_current),
            created_by=str(report.created_by or ""),
            origin=str(report.origin or ""),
            document_version_id=str(report.document_version_id or ""),
            created_at=_stamp(report.created_at),
            updated_at=_stamp(report.updated_at),
            provenance=tuple(provenance),
            activities=tuple(
                {
                    "id": str(activity.id),
                    "sequence": int(activity.sequence),
                    "created_at": _stamp(activity.created_at),
                    "updated_at": _stamp(activity.updated_at),
                    "operation_type": str(activity.operation_type),
                    "label": str(activity.label),
                    "description": str(activity.description or ""),
                    "period_text": str(activity.period_text or ""),
                    "operation_id": str(activity.operation_id or ""),
                    "provenance": [
                        dict(item)
                        for item in activity.provenance or ()
                        if isinstance(item, Mapping)
                    ],
                }
                for activity in activities
            ),
        )

    def _content_from_session(self, session: Session, report: DdrReport) -> _DdrContent:
        activities = tuple(
            DdrActivityInput(
                operation_type=row.operation_type,
                label=row.label,
                description=row.description or "",
                period_text=row.period_text or "",
            )
            for row in session.scalars(
                select(DdrActivity)
                .where(DdrActivity.ddr_report_id == report.id)
                .order_by(DdrActivity.sequence, DdrActivity.id)
            )
        )
        return _DdrContent(
            report_number=report.report_number or "",
            shift=report.shift or "",
            summary=report.summary or "",
            activities=activities,
        )

    @staticmethod
    def _audit(
        session: Session,
        report: DdrReport,
        *,
        action: str,
        actor: str,
        reason: str,
        content: _DdrContent,
    ) -> None:
        AuditLog(session).record(
            action=action,
            subject_type="ddr_report",
            subject_id=str(report.id),
            actor=actor,
            detail={
                "well_id": report.well_id,
                "revision_group_id": report.revision_group_id,
                "revision_number": report.revision_number,
                "authoring_state": report.authoring_state,
                "status": report.status,
                "reason": reason,
                "content_sha256": sha256_obj(content.to_dict()),
                "content": content.to_dict(),
            },
        )

    @staticmethod
    def _audit_operation(
        session: Session,
        activity: DdrActivity,
        *,
        actor: str,
        reason: str,
        report: DdrReport,
    ) -> None:
        AuditLog(session).record(
            action="ddr.activity_submitted",
            subject_type="well_operation",
            subject_id=str(activity.operation_id),
            actor=actor,
            detail={
                "ddr_report_id": report.id,
                "ddr_activity_id": activity.id,
                "revision_number": report.revision_number,
                "reason": reason,
            },
        )


@dataclass(frozen=True)
class _DdrContent:
    report_number: str
    shift: str
    summary: str
    activities: tuple[DdrActivityInput, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_number": self.report_number,
            "shift": self.shift,
            "summary": self.summary,
            "activities": [item.to_dict() for item in self.activities],
        }


def _content(
    *,
    report_number: str,
    shift: str,
    summary: str,
    activities: Sequence[Mapping[str, Any]],
) -> _DdrContent:
    if len(activities) > 200:
        raise ValidationError("a DDR draft may contain at most 200 activities")
    normalized_activities = tuple(DdrActivityInput.from_mapping(item) for item in activities)
    return _DdrContent(
        report_number=_bounded(report_number, "report number", 64),
        shift=_bounded(shift, "shift", 40),
        summary=_bounded(summary, "summary", 20_000),
        activities=normalized_activities,
    )


def _report_day(raw: str) -> tuple[datetime, str]:
    text = _required(raw, "report date", 80)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(
            "report date must be an explicit ISO calendar day (YYYY-MM-DD)", report_date=text
        ) from exc
    if parsed.isoformat() != text:
        raise ValidationError(
            "report date must use canonical YYYY-MM-DD spelling", report_date=text
        )
    return datetime.combine(parsed, datetime.min.time(), tzinfo=UTC), text


def _report_provenance(
    *, actor: str, reason: str, action: str, revision: int
) -> list[dict[str, Any]]:
    return [
        {
            "source_kind": "manual_authoring",
            "actor": actor,
            "reason": reason,
            "action": action,
            "revision_number": revision,
            "at": datetime.now(UTC).isoformat(),
        }
    ]


def _required(value: str, name: str, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > limit:
        raise ValidationError(f"{name} is required and must not exceed {limit} characters")
    return normalized


def _bounded(value: str, name: str, limit: int) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > limit:
        raise ValidationError(f"{name} must not exceed {limit} characters")
    return normalized


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _next_updated_at(previous: datetime | None) -> datetime:
    """Advance the DDR concurrency token even when the OS clock repeats a tick."""
    now = _utc_now()
    if previous is None:
        return now
    prior = previous if previous.tzinfo else previous.replace(tzinfo=UTC)
    return max(now, prior.astimezone(UTC) + timedelta(microseconds=1))


def _stamp(value: datetime | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value.strip()
    parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


__all__ = ["DdrActivityInput", "DdrAuthoringService", "DdrWorkspaceRecord"]
