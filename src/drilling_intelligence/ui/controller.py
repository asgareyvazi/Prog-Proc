"""Application/controller boundary for the optional desktop Review Workbench.

This module contains no Qt imports.  It opens the existing :class:`Workspace`, resolves wells through
``WellRepository``, and delegates every review read to ``DomainReviewService``.  The small
``WellChoice`` value is presentation metadata for a selector, not a second domain read model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config.settings import Settings
from ..core.errors import ValidationError, WorkspaceError
from ..review import (
    DomainReview,
    DomainReviewRequest,
    DomainReviewService,
    ReviewAction,
    ReviewActionRequest,
    ReviewActionResult,
    ReviewActionService,
    ReviewConflict,
    ReviewRecord,
)
from ..wells.repository import WellRepository
from ..wells.workspace import Workspace


@dataclass(frozen=True)
class WellChoice:
    """The minimum hierarchy context needed to identify a well in a selector."""

    well_id: str
    name: str
    field_name: str = ""
    project_name: str = ""
    company_name: str = ""
    lifecycle_status: str = ""

    @property
    def label(self) -> str:
        hierarchy = " / ".join(
            value for value in (self.company_name, self.project_name, self.field_name) if value
        )
        return f"{self.name} — {hierarchy}" if hierarchy else self.name


class ReviewController:
    """Own workspace lifetime and delegate review reads without embedding domain rules."""

    def __init__(self) -> None:
        self._workspace: Workspace | None = None
        self._settings: Settings | None = None

    @property
    def workspace(self) -> Workspace | None:
        return self._workspace

    @property
    def settings(self) -> Settings | None:
        return self._settings

    def open_workspace(
        self,
        root: str | Path,
        *,
        config_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Open an existing workspace and expose its existing schema status.

        ``Workspace.database`` is deliberately touched here because that is the repository's existing
        schema/migration gate.  No UI-specific database or settings file is created.
        """
        path = Path(root).expanduser().resolve()
        settings = Settings.load(config_path)
        workspace = Workspace.open(path, settings, create=False)
        try:
            # Existing Workspace semantics perform the authoritative schema check here.
            _ = workspace.database
            info = self.workspace_info(workspace)
        except Exception:
            workspace.close()
            raise

        previous = self._workspace
        self._workspace = workspace
        self._settings = settings
        if previous is not None and previous is not workspace:
            previous.close()
        return info

    @staticmethod
    def workspace_info(workspace: Workspace) -> dict[str, Any]:
        migration = workspace.migration.to_dict() if workspace.migration is not None else None
        return {
            "root": str(workspace.root),
            "name": workspace.config.name,
            "database_path": str(workspace.database_path),
            "schema": migration,
        }

    def list_wells(self) -> list[WellChoice]:
        workspace = self._require_workspace()
        with workspace.database.read_only() as session:
            rows = WellRepository(session).list_wells()
            return [
                WellChoice(
                    well_id=str(row.id),
                    name=str(row.name),
                    field_name=str(row.field.name) if row.field is not None else "",
                    project_name=str(row.project.name) if row.project is not None else "",
                    company_name=(
                        str(row.project.company.name)
                        if row.project is not None and row.project.company is not None
                        else ""
                    ),
                    lifecycle_status=str(row.lifecycle_status or ""),
                )
                for row in rows
            ]

    def resolve_well(self, reference: str) -> str:
        """Resolve an id or name using the existing well repository path."""
        workspace = self._require_workspace()
        wanted = str(reference or "").strip()
        if not wanted:
            raise ValidationError("a well selection is required", hint="choose a well")
        with workspace.database.read_only() as session:
            repository = WellRepository(session)
            row = repository.get_well(wanted) or repository.find_well(wanted)
            if row is not None:
                return str(row.id)
            known = [item.name for item in repository.list_wells(limit=50)]
        raise ValidationError(
            f"no well matches {wanted!r} in this workspace",
            reference=wanted,
            known_wells=known,
        )

    def load_review(
        self,
        well_id: str,
        *,
        lifecycle: str = "current",
        verify_citations: bool = False,
        limit: int = 0,
    ) -> DomainReview:
        """Load exactly one existing DomainReview result."""
        workspace = self._require_workspace()
        request = DomainReviewRequest(
            well_id=str(well_id),
            lifecycle=lifecycle,
            verify_citations=verify_citations,
            limit=limit,
        )
        return DomainReviewService.for_workspace(workspace).review(request)

    def available_actions(
        self, record: ReviewRecord | ReviewConflict
    ) -> tuple[ReviewAction, ...]:
        """Return domain-derived capabilities without opening a write transaction."""
        workspace = self._require_workspace()
        return ReviewActionService.for_workspace(workspace).available_actions(record)

    def execute_action(
        self, request: ReviewActionRequest | dict[str, Any]
    ) -> ReviewActionResult:
        """Execute one already-confirmed action through the headless action boundary."""
        workspace = self._require_workspace()
        return ReviewActionService.for_workspace(workspace).execute(request)

    def close(self) -> None:
        workspace, self._workspace = self._workspace, None
        self._settings = None
        if workspace is not None:
            workspace.close()

    def _require_workspace(self) -> Workspace:
        if self._workspace is None:
            raise WorkspaceError(
                "no workspace is open",
                hint="open an existing workspace before selecting a well",
            )
        return self._workspace


__all__ = ["ReviewController", "WellChoice"]
