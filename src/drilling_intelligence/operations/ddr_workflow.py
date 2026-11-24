"""Phase G (2026-11-24B): public service surface for the source-to-DDR workflow.

One service used by the CLI, the Qt page, and tests alike, so all three surfaces
read and write the same append-only disposition state.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..database.models import DocumentVersion, Extraction
from .ddr_candidates import (
    accepted_values,
    apply_candidate_state,
    authorize_report,
    build_ddr_candidates,
    record_disposition,
)


class DdrWorkflowService:
    """Candidate listing, disposition, and report authorization for one workspace."""

    def __init__(self, *, database: Any) -> None:
        self._database = database

    @classmethod
    def for_workspace(cls, workspace: Any) -> DdrWorkflowService:
        return cls(database=workspace.database)

    def _version_with_text(self, session: Any, version_id: str) -> tuple[DocumentVersion, str]:
        version = session.get(DocumentVersion, str(version_id))
        if version is None:
            from ..core.errors import ValidationError

            raise ValidationError("unknown document version", version_id=version_id)
        rows = (
            session.execute(
                select(Extraction).where(Extraction.document_version_id == str(version_id))
            )
            .scalars()
            .all()
        )
        text = "\f".join(r.text_blob or "" for r in rows)  # form-feed = real page separator
        return version, text

    def list_candidates(self, version_id: str, *, session: Any = None) -> list[dict[str, Any]]:
        """Build candidates from the version's verified text and attach persisted state."""
        with self._session(session) as active:
            version, text = self._version_with_text(active, version_id)
            candidates = build_ddr_candidates(version, text)
            candidates = apply_candidate_state(active, version, candidates)
            return [c.to_dict() for c in candidates]

    def dispose(
        self,
        version_id: str,
        candidate_id: str,
        decision: str,
        *,
        actor: str,
        edited_value: str = "",
        session: Any = None,
    ) -> dict[str, Any]:
        """Append one disposition to the append-only table (never mutates metadata)."""
        with self._session(session) as active:
            version, text = self._version_with_text(active, version_id)
            candidates = build_ddr_candidates(version, text)
            cand = next((c for c in candidates if c.candidate_id == candidate_id), None)
            if cand is None:
                from ..core.errors import ValidationError

                raise ValidationError("unknown candidate", candidate_id=candidate_id)
            event = record_disposition(
                active, version, cand, decision, actor=actor, edited_value=edited_value
            )
            active.flush()
            return event

    def authorize(self, version_id: str, *, well_id: str, actor: str, session: Any = None) -> Any:
        """Authorize a CANDIDATE report through the strengthened gates."""
        with self._session(session) as active:
            report = authorize_report(active, version_id=version_id, well_id=well_id, actor=actor)
            active.flush()
            return report

    def accepted(self, version_id: str, *, session: Any = None) -> dict[str, str]:
        with self._session(session) as active:
            version = active.get(DocumentVersion, str(version_id))
            if version is None:
                from ..core.errors import ValidationError

                raise ValidationError("unknown document version", version_id=version_id)
            return accepted_values(active, version)

    def _session(self, session: Any):
        if session is not None:
            import contextlib

            return contextlib.nullcontext(session)
        return self._database.unit_of_work()
