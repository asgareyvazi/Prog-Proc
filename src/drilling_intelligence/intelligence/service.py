"""The intelligence service: timeline, field aggregations and pattern snapshots for a workspace.

Nothing here stores a derived answer twice.  The aggregations read the record tables; the timeline reads
the same rows through the same timestamps; only a *pattern snapshot* is persisted, and only because a
reviewed pattern is a different thing from a runnable query (see
:mod:`drilling_intelligence.intelligence.patterns`).  So there is no cache to invalidate, no rebuild to
schedule, and no window in which the number on screen and the number in the table disagree.

Like the operational and knowledge services, this class holds a *database*, not a session: every method
takes ``session=None`` and either borrows the caller's transaction or opens and commits its own.  That is
what lets a CLI command that promoted records and then summarised them do both against one unit of work,
and what lets a test see rows it has not committed.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import ValidationError
from ..database.models import Field, FieldPattern, Well
from .field import FieldIntelligence
from .patterns import (
    find_recurring,
    get_pattern,
    link_rows,
    list_patterns,
    propose_recommendation,
    set_pattern_status,
    signature_for,
    snapshot,
    staleness,
)
from .timeline import TimelineEntry, build_timeline

__all__ = ["IntelligenceService"]


class IntelligenceService:
    """Derived views over the operational and engineering records: timeline, field, patterns."""

    def __init__(self, *, database: Any, settings: Any = None) -> None:
        if database is None:
            raise ValidationError(
                "the intelligence service needs the registry database; there is no fallback to files"
            )
        self.database = database
        self.settings = settings

    @classmethod
    def for_workspace(cls, workspace: Any) -> IntelligenceService:
        """Wire the service to an opened workspace (the CLI and the UI both use this)."""
        return cls(database=workspace.database, settings=getattr(workspace, "settings", None))

    @contextmanager
    def _session(self, session: Session | None) -> Iterator[Session]:
        if session is not None:
            yield session
            return
        with self.database.session() as own:
            yield own
            own.commit()

    # -- timeline -------------------------------------------------------------
    def timeline(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        kinds: Sequence[str] = (),
        since: object = None,
        until: object = None,
        include_undated: bool | None = None,
        limit: int = 0,
        session: Session | None = None,
    ) -> list[TimelineEntry]:
        """The well's (or field's) history as one ordered list of entries.

        Entries are returned, not dicts, so a caller can sort or merge them with the same comparator the
        service used; ``to_dict`` is there for the CLI and the API boundary.  ``include_undated=None``
        follows the window: an unbounded scope lists its undated records at the end, a dated window does
        not, and either can be forced.
        """
        with self._session(session) as active:
            return build_timeline(
                active,
                well_id=well_id,
                field_id=field_id,
                project_id=project_id,
                kinds=kinds,
                since=since,
                until=until,
                include_undated=include_undated,
                limit=limit,
            )

    # -- field aggregations ---------------------------------------------------
    def wells(
        self, *, field_id: str = "", project_id: str = "", session: Session | None = None
    ) -> dict[str, Any]:
        with self._session(session) as active:
            return FieldIntelligence(active).wells(field_id=field_id, project_id=project_id)

    def npt(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        since: object = None,
        until: object = None,
        session: Session | None = None,
    ) -> dict[str, Any]:
        with self._session(session) as active:
            return FieldIntelligence(active).npt(
                field_id=field_id,
                project_id=project_id,
                well_id=well_id,
                since=since,
                until=until,
            )

    def problems(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        since: object = None,
        until: object = None,
        session: Session | None = None,
    ) -> dict[str, Any]:
        with self._session(session) as active:
            return FieldIntelligence(active).problems(
                field_id=field_id,
                project_id=project_id,
                well_id=well_id,
                since=since,
                until=until,
            )

    def events(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        since: object = None,
        until: object = None,
        session: Session | None = None,
    ) -> dict[str, Any]:
        with self._session(session) as active:
            return FieldIntelligence(active).events(
                field_id=field_id,
                project_id=project_id,
                well_id=well_id,
                since=since,
                until=until,
            )

    def lessons(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        well_id: str = "",
        approved_only: bool = True,
        session: Session | None = None,
    ) -> dict[str, Any]:
        with self._session(session) as active:
            return FieldIntelligence(active).lessons(
                field_id=field_id,
                project_id=project_id,
                well_id=well_id,
                approved_only=approved_only,
            )

    def well_problem_history(
        self, well_id: str, *, session: Session | None = None
    ) -> list[dict[str, Any]]:
        with self._session(session) as active:
            return FieldIntelligence(active).well_problem_history(well_id)

    def section_problem_history(
        self, section_id: str, *, session: Session | None = None
    ) -> list[dict[str, Any]]:
        with self._session(session) as active:
            return FieldIntelligence(active).section_problem_history(section_id)

    def operation_events(
        self, operation_id: str, *, session: Session | None = None
    ) -> list[dict[str, Any]]:
        with self._session(session) as active:
            return FieldIntelligence(active).operation_events(operation_id)

    def offsets(
        self,
        well_id: str,
        *,
        same_field_only: bool = True,
        limit: int = 10,
        session: Session | None = None,
    ) -> list[dict[str, Any]]:
        """Other wells worth reading before this one is finished.

        ``same_field_only=False`` reaches the whole project, which is what a person asks for when the
        neighbouring field drilled the same formation last year - a comparison across fields is still a
        comparison of recorded attributes, not a guess.
        """
        with self._session(session) as active:
            return FieldIntelligence(active).offset_candidates(
                well_id, same_field_only=same_field_only, limit=limit
            )

    def summary(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        since: object = None,
        until: object = None,
        session: Session | None = None,
    ) -> dict[str, Any]:
        with self._session(session) as active:
            return FieldIntelligence(active).summary(
                field_id=field_id, project_id=project_id, since=since, until=until
            )

    # -- patterns -------------------------------------------------------------
    def patterns(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        min_occurrences: int = 2,
        min_wells: int = 2,
        since: object = None,
        until: object = None,
        limit: int = 50,
        session: Session | None = None,
    ) -> list[dict[str, Any]]:
        """The recurring groupings, live - nothing read from a previous snapshot."""
        with self._session(session) as active:
            return find_recurring(
                active,
                field_id=field_id,
                project_id=project_id,
                min_occurrences=min_occurrences,
                min_wells=min_wells,
                since=since,
                until=until,
                limit=limit,
            )

    def snapshot_patterns(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        min_occurrences: int = 2,
        min_wells: int = 2,
        detected_by: str = "intelligence",
        session: Session | None = None,
    ) -> dict[str, Any]:
        """Persist every live grouping as a snapshot, and report what was created or refreshed.

        The result counts rows rather than listing them, because the point of running this from a CLI is
        to know whether anything changed; the snapshots themselves are read back with
        :meth:`list_patterns`.
        """
        with self._session(session) as active:
            candidates = find_recurring(
                active,
                field_id=field_id,
                project_id=project_id,
                min_occurrences=min_occurrences,
                min_wells=min_wells,
                limit=0,
            )
            created = 0
            refreshed = 0
            rows: list[dict[str, Any]] = []
            for candidate in candidates:
                parameters = dict(candidate.get("query") or {})
                existing = active.execute(
                    select(FieldPattern).where(
                        FieldPattern.signature == signature_for(**parameters)
                    )
                ).scalar_one_or_none()
                row = snapshot(active, candidate, detected_by=detected_by)
                if existing is None:
                    created += 1
                else:
                    refreshed += 1
                rows.append(
                    {
                        "id": row.id,
                        "problem_type": row.problem_type,
                        "occurrence_count": row.occurrence_count,
                        "well_count": row.well_count,
                        "total_npt_hours": row.total_npt_hours,
                        "status": row.status,
                        "new": existing is None,
                    }
                )
            return {
                "scope": {"field_id": field_id or None, "project_id": project_id or None},
                "candidates": len(candidates),
                "created": created,
                "refreshed": refreshed,
                "patterns": rows,
            }

    def list_patterns(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        status: str = "",
        stale_only: bool = False,
        limit: int = 100,
        session: Session | None = None,
    ) -> list[FieldPattern]:
        with self._session(session) as active:
            return list_patterns(
                active,
                field_id=field_id,
                project_id=project_id,
                status=status,
                stale_only=stale_only,
                limit=limit,
            )

    def pattern_staleness(
        self, pattern_id: str, *, session: Session | None = None
    ) -> dict[str, Any]:
        """Re-run one snapshot's own query and report what has moved since it was taken."""
        with self._session(session) as active:
            report = staleness(active, pattern_id)
            row = get_pattern(active, pattern_id)
            if report["stale"] and row.stale_at is None:
                # The flag is written once, on the first re-check that finds the numbers have moved: a
                # pattern that goes stale and is refreshed keeps a traceable date rather than a rolling
                # one that hides how long it has been out of date.
                row.stale_at = datetime.now(UTC)
                row.stale_snapshot = report["differences"]
                active.flush()
            return report

    def confirm_pattern(
        self,
        pattern_id: str,
        new_status: str,
        *,
        by: str = "",
        reason: str = "",
        session: Session | None = None,
    ) -> FieldPattern:
        with self._session(session) as active:
            return set_pattern_status(active, pattern_id, new_status, by=by, reason=reason)

    def recommend(
        self,
        pattern_id: str,
        *,
        statement: str,
        reason: str = "",
        session: Session | None = None,
    ) -> dict[str, Any]:
        """Turn a confirmed pattern into a recommendation a person has to accept or decline."""
        from ..database.serialize import record_to_dict

        with self._session(session) as active:
            row = propose_recommendation(active, pattern_id, statement=statement, reason=reason)
            return record_to_dict(row)

    def relink_patterns(self, pattern_id: str, *, session: Session | None = None) -> dict[str, int]:
        with self._session(session) as active:
            return link_rows(active, get_pattern(active, pattern_id))

    # -- scope helpers for the CLI -------------------------------------------
    def resolve_field(self, name_or_id: str, *, session: Session | None = None) -> str:
        """A field id from an id or a name, refusing politely when the name is ambiguous.

        The CLI accepts either, because a person typing a command has the name in their head and a
        script has the id; guessing between two fields that share a name is the one thing neither wants.
        """
        with self._session(session) as active:
            wanted = str(name_or_id or "").strip()
            if not wanted:
                raise ValidationError("a field name or id is required")
            row = active.get(Field, wanted)
            if row is not None:
                return str(row.id)
            matches = list(active.execute(select(Field).where(Field.name == wanted)).scalars())
            if len(matches) > 1:
                raise ValidationError(
                    f"{len(matches)} fields are named {wanted!r}",
                    hint="pass the field id, which is unique",
                    name=wanted,
                )
            if matches:
                return str(matches[0].id)
            raise ValidationError(
                f"no field {wanted!r}", hint="list the fields with drillintel fields"
            )

    def wells_of(
        self, *, field_id: str = "", project_id: str = "", session: Session | None = None
    ) -> list[Well]:
        with self._session(session) as active:
            clauses = []
            if field_id:
                clauses.append(Well.field_id == field_id)
            if project_id:
                clauses.append(Well.project_id == project_id)
            if not clauses:
                raise ValidationError("a well list needs field_id or project_id")
            return list(active.execute(select(Well).where(*clauses).order_by(Well.name)).scalars())
