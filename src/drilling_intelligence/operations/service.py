"""The service face of the operational layer: promote, look, confirm.

Three jobs, and the boundary between them is the point:

*   **promote** — turn stored artefacts into operational rows (this is the only writer, and it lives
    in :mod:`drilling_intelligence.operations.promote`);
*   **look** — read the history back through the repositories, scoped to a well, a field or a
    project, with the ordering rules the timeline depends on;
*   **confirm** — move a promoted row along the confirmation lifecycle, which is how a candidate
    produced by a script becomes a record a person has vouched for.

Like the knowledge service it wraps, this class holds a *database*, not a session: every method takes
``session=None`` and either borrows the caller's transaction (a test, or a caller composing several
writes) or opens and commits its own.  What it never does is compute an answer twice - "how many
hours did this field lose" is the aggregation service's question, and the numbers here are the rows
that question reads.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import ConfirmationStatus
from ..database.models import (
    DdrReport,
    Document,
    Extraction,
    NptRecord,
    ProblemOccurrence,
    Well,
    WellEvent,
    WellOperation,
)
from .assets import AssetRepository
from .promote import PromotionResult, VersionPromoter, find_npt_tables
from .repository import REPORT_CLASSIFICATIONS, OperationsRepository

__all__ = ["OperationalService"]

#: The record tables ``status()`` counts, in dependency order (the same order a delete needs).
RECORD_TABLES: tuple[tuple[str, type], ...] = (
    ("reports", DdrReport),
    ("operations", WellOperation),
    ("events", WellEvent),
    ("npt", NptRecord),
    ("problems", ProblemOccurrence),
)


class OperationalService:
    """Operations, events, NPT and problems for one workspace: promoted, queried, confirmed."""

    def __init__(self, *, database: Any, settings: Any = None) -> None:
        if database is None:
            raise ValueError(
                "the operational service needs the registry database; there is no fallback to files"
            )
        self.database = database
        self.settings = settings

    @classmethod
    def for_workspace(cls, workspace: Any) -> OperationalService:
        """Wire the service to an opened workspace (the CLI and the UI both use this)."""
        return cls(database=workspace.database, settings=getattr(workspace, "settings", None))

    # -- sessions -------------------------------------------------------------
    @contextmanager
    def _session(self, session: Session | None) -> Iterator[Session]:
        """Borrow the caller's session, or own the transaction.  The knowledge service's rule, kept
        identical here so a caller composing a knowledge sync and a promotion in one transaction gets
        one atomic unit rather than a surprise commit in the middle."""
        if session is not None:
            yield session
            return
        with self.database.session() as own:
            yield own
            own.commit()

    # -- promotion ------------------------------------------------------------
    def promote(
        self,
        *,
        document_id: str,
        version_id: str = "",
        replace: bool = True,
        session: Session | None = None,
    ) -> PromotionResult:
        """Promote one document's current version (or an explicit one) into operational records."""
        with self._session(session) as active:
            return VersionPromoter(active).promote(
                document_id=document_id, version_id=version_id, replace=replace
            )

    def promote_workspace(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        limit: int = 0,
        replace: bool = True,
        session: Session | None = None,
    ) -> dict[str, Any]:
        """Promote every report-like document version in scope, and report what changed.

        Only versions with a stored artefact are visited, and only documents whose classification can
        become a report or whose artefact holds a recognisable NPT table: a mud log is not a schedule
        of lost time, and running the promoter over every file would spend the pass deciding that.
        """
        results: list[PromotionResult] = []
        with self._session(session) as active:
            pairs = self._candidate_versions(
                active,
                well_id=well_id,
                field_id=field_id,
                project_id=project_id,
                limit=limit,
            )
            promoter = VersionPromoter(active)
            for document_id, version_id in pairs:
                results.append(
                    promoter.promote(
                        document_id=document_id, version_id=version_id, replace=replace
                    )
                )
        return combine_promotion_results(results, versions=len(pairs))

    def _candidate_versions(
        self,
        session: Session,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        limit: int = 0,
    ) -> list[tuple[str, str]]:
        """Current versions worth promoting, in a stable order (document path, then version)."""
        statement = (
            select(Document.id, Document.current_version_id, Document.classification)
            .where(Document.current_version_id.is_not(None))
            .join(Extraction, Extraction.document_version_id == Document.current_version_id)
        )
        if well_id:
            statement = statement.where(Document.well_id == well_id)
        if project_id:
            statement = statement.where(Document.project_id == project_id)
        if field_id:
            # A document is scoped to a field through its well.  Note which id is being matched: an
            # ``in_`` over ``select(Well.id)`` would compare document ids against well ids, find nothing,
            # and report a successful promotion of zero documents.
            statement = statement.where(
                Document.id.in_(
                    select(Document.id)
                    .join(Well, Document.well_id == Well.id)
                    .where(Well.field_id == field_id)
                )
            )
        statement = statement.order_by(Document.identity_path, Document.current_version_id)
        if limit:
            statement = statement.limit(max(0, int(limit)))
        pairs: list[tuple[str, str]] = []
        for document_id, version_id, classification in session.execute(statement).all():
            if str(classification or "") in REPORT_CLASSIFICATIONS:
                pairs.append((str(document_id), str(version_id)))
                continue
            # An NPT export nobody classified as one is still an NPT export; the header test is the
            # one that matters, and it is cheap because it reads the artefact we already joined on.
            extraction = session.execute(
                select(Extraction).where(Extraction.document_version_id == version_id)
            ).scalar_one_or_none()
            payload = dict(extraction.document_json or {}) if extraction else {}
            if find_npt_tables(payload):
                pairs.append((str(document_id), str(version_id)))
        return pairs

    # -- reading --------------------------------------------------------------
    def report(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        session: Session | None = None,
    ) -> dict[str, Any]:
        """What the operational history in scope adds up to, gaps included.

        A pass-through to :meth:`~drilling_intelligence.operations.repository.OperationsRepository.record_summary`
        rather than a second implementation: the counting lives with the queries, so the CLI, a future
        screen and a test all read the same numbers.
        """
        with self._session(session) as active:
            return OperationsRepository(active).record_summary(
                well_id=well_id, field_id=field_id, project_id=project_id
            )

    def list_npt(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        category: str = "",
        since: Any = None,
        until: Any = None,
        status: str = "",
        limit: int = 200,
        session: Session | None = None,
    ) -> list[NptRecord]:
        with self._session(session) as active:
            return OperationsRepository(active).list_npt(
                well_id=well_id,
                field_id=field_id,
                project_id=project_id,
                category=category,
                since=since,
                until=until,
                status=status,
                limit=limit,
            )

    def list_events(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        category: str = "",
        event_type: str = "",
        since: Any = None,
        until: Any = None,
        limit: int = 200,
        session: Session | None = None,
    ) -> list[WellEvent]:
        with self._session(session) as active:
            return OperationsRepository(active).list_events(
                well_id=well_id,
                field_id=field_id,
                project_id=project_id,
                category=category,
                event_type=event_type,
                since=since,
                until=until,
                limit=limit,
            )

    def list_problems(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        problem_type: str = "",
        root_cause_status: str = "",
        limit: int = 200,
        session: Session | None = None,
    ) -> list[ProblemOccurrence]:
        with self._session(session) as active:
            return OperationsRepository(active).list_problems(
                well_id=well_id,
                field_id=field_id,
                project_id=project_id,
                problem_type=problem_type,
                root_cause_status=root_cause_status,
                limit=limit,
            )

    def list_operations(
        self,
        *,
        well_id: str = "",
        report_id: str = "",
        section_id: str = "",
        operation_type: str = "",
        record_state: str = "",
        limit: int = 200,
        session: Session | None = None,
    ) -> list[WellOperation]:
        with self._session(session) as active:
            return OperationsRepository(active).list_operations(
                well_id=well_id,
                report_id=report_id,
                section_id=section_id,
                operation_type=operation_type,
                record_state=record_state,
                limit=limit,
            )

    def list_reports(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        since: Any = None,
        until: Any = None,
        limit: int = 200,
        session: Session | None = None,
    ) -> list[DdrReport]:
        with self._session(session) as active:
            return OperationsRepository(active).list_reports(
                well_id=well_id,
                field_id=field_id,
                project_id=project_id,
                since=since,
                until=until,
                limit=limit,
            )

    # -- confirmation ---------------------------------------------------------
    def set_status(
        self,
        table: str,
        row_id: str,
        new_status: ConfirmationStatus | str,
        *,
        by: str = "",
        reason: str = "",
        session: Session | None = None,
    ) -> dict[str, Any]:
        """Move one record along the confirmation lifecycle and report the new state.

        The only way a promoted row becomes something a person vouched for.  An illegal move is
        refused by the lifecycle with the states that *are* available, so a caller learns the machine
        from the error instead of from the source.
        """
        with self._session(session) as active:
            records = OperationsRepository(active)
            row = records.get_row(table, row_id)
            moved = records.set_status(row, new_status, by=by, reason=reason)
            return {
                "table": type(row).__tablename__,
                "id": row.id,
                "status": moved,
                "by": by or "",
            }

    # -- assets ---------------------------------------------------------------
    @contextmanager
    def assets(self, session: Session | None = None) -> Iterator[AssetRepository]:
        """Yield the rig/vendor repository, inside a transaction this service owns when needed.

        A context manager rather than a returned object because the repository writes: handing back a
        repository holding a session nobody owns is how an asset row silently never gets committed.
        """
        if session is not None:
            yield AssetRepository(session)
            return
        with self.database.session() as own:
            yield AssetRepository(own)
            own.commit()


def combine_promotion_results(
    results: Sequence[PromotionResult], *, versions: int
) -> dict[str, Any]:
    """Fold a batch of promotion results into one reportable summary.

    Kept a free function because the CLI, a future UI and the tests all need the same arithmetic, and
    "what did promoting this folder do" must not have three answers of different shapes.
    """
    kinds = ("report", "operation", "event", "npt", "problem", "removed")
    counts = {kind: {"created": 0, "unchanged": 0, "conflict": 0} for kind in kinds}
    skipped: dict[str, int] = {}
    details: list[dict[str, str]] = []
    touched = 0
    for result in results:
        if result.counts:
            touched += 1
        for kind, values in result.counts.items():
            bucket = counts.setdefault(kind, {"created": 0, "unchanged": 0, "conflict": 0})
            for outcome in ("created", "unchanged", "conflict"):
                bucket[outcome] += int(values.get(outcome, 0))
        for entry in result.skipped:
            reason = str(entry.get("reason") or "UNKNOWN")
            skipped[reason] = skipped.get(reason, 0) + 1
            details.append({**entry, "version_id": result.version_id})
    return {
        "versions": int(versions),
        "versions_with_records": touched,
        "counts": counts,
        "totals": {
            outcome: sum(bucket[outcome] for bucket in counts.values())
            for outcome in ("created", "unchanged", "conflict")
        },
        "skipped": skipped,
        "skipped_details": details,
    }
