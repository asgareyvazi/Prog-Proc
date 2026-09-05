"""Persistence for the well-centric hierarchy and workspaces (section 10).

The well is the organising concept, so this repository owns the hierarchy above
it (company -> project -> field -> well -> section) and the workspace record that
ties a folder on disk to the registry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import WELL_LIFECYCLE_TRANSITIONS, RecordState, WellLifecycleStatus
from ..core.errors import ValidationError
from ..core.ids import new_id
from ..database.models import Company, Document, Field, Project, Well, WellSection, Workspace


class WellRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- workspaces ---------------------------------------------------------
    def get_or_create_workspace(self, root_path: str, *, name: str = "", data_dir: str = "", project_id: str | None = None, config: dict[str, Any] | None = None) -> Workspace:
        existing = self.session.execute(select(Workspace).where(Workspace.root_path == root_path)).scalar_one_or_none()
        if existing is not None:
            changed = False
            if data_dir and existing.data_dir != data_dir:
                existing.data_dir = data_dir
                changed = True
            if project_id and existing.project_id != project_id:
                existing.project_id = project_id
                changed = True
            if changed:
                self.session.flush()
            return existing
        workspace = Workspace(
            id=new_id("ws"),
            root_path=root_path,
            name=name or root_path.rstrip("/").rsplit("/", 1)[-1] or "workspace",
            data_dir=data_dir or "",
            project_id=project_id,
            config=config or {},
        )
        self.session.add(workspace)
        self.session.flush()
        return workspace

    def list_workspaces(self) -> list[Workspace]:
        return list(self.session.execute(select(Workspace).order_by(Workspace.name)).scalars())

    def workspace_by_root(self, root_path: str) -> Workspace | None:
        return self.session.execute(select(Workspace).where(Workspace.root_path == root_path)).scalar_one_or_none()

    def mark_scanned(self, workspace: Workspace, at: datetime | None = None) -> None:
        workspace.last_scan_at = at or datetime.now(UTC)
        self.session.flush()

    # -- hierarchy ----------------------------------------------------------
    def get_company(self, company_id: str) -> Company | None:
        return self.session.get(Company, company_id)

    def get_or_create_company(self, name: str, code: str | None = None) -> Company:
        company = self.session.execute(select(Company).where(Company.name == name)).scalar_one_or_none()
        if company is not None:
            return company
        company = Company(id=new_id("co"), name=name, code=code)
        self.session.add(company)
        self.session.flush()
        return company

    def get_or_create_project(self, name: str, *, company: Company | None = None, code: str | None = None, country: str | None = None) -> Project:
        stmt = select(Project).where(Project.name == name)
        if company is not None:
            stmt = stmt.where(Project.company_id == company.id)
        project = self.session.execute(stmt).scalar_one_or_none()
        if project is not None:
            return project
        project = Project(id=new_id("prj"), name=name, company_id=company.id if company else None, code=code, country=country)
        self.session.add(project)
        self.session.flush()
        return project

    def get_or_create_field(self, name: str, *, project: Project | None = None, basin: str | None = None) -> Field:
        stmt = select(Field).where(Field.name == name)
        if project is not None:
            stmt = stmt.where(Field.project_id == project.id)
        field = self.session.execute(stmt).scalar_one_or_none()
        if field is not None:
            return field
        field = Field(id=new_id("fld"), name=name, project_id=project.id if project else None, basin=basin)
        self.session.add(field)
        self.session.flush()
        return field

    def get_well(self, well_id: str) -> Well | None:
        return self.session.get(Well, well_id)

    def find_well(self, name: str, *, project_id: str | None = None) -> Well | None:
        stmt = select(Well).where(Well.name == name)
        if project_id:
            stmt = stmt.where(Well.project_id == project_id)
        return self.session.execute(stmt).limit(1).scalar_one_or_none()

    def list_wells(self, *, project_id: str | None = None, status: str | None = None, limit: int = 500) -> list[Well]:
        stmt = select(Well)
        if project_id:
            stmt = stmt.where(Well.project_id == project_id)
        if status:
            stmt = stmt.where(Well.lifecycle_status == status)
        return list(self.session.execute(stmt.order_by(Well.name).limit(limit)).scalars())

    def create_well(
        self,
        name: str,
        *,
        project_id: str | None = None,
        field_id: str | None = None,
        well_identifier: str | None = None,
        well_type: str | None = None,
        trajectory_type: str | None = None,
        lifecycle_status: WellLifecycleStatus | str = WellLifecycleStatus.PLANNED,
        total_depth: tuple[float, str] | None = None,
        notes: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Well:
        if not (name or "").strip():
            raise ValidationError("A well must have a name")
        status = WellLifecycleStatus(str(getattr(lifecycle_status, "value", lifecycle_status)))
        well = Well(
            id=new_id("well"),
            name=name.strip(),
            project_id=project_id,
            field_id=field_id,
            well_identifier=well_identifier,
            well_type=well_type,
            trajectory_type=trajectory_type,
            lifecycle_status=status.value,
            notes=notes,
            attributes=attributes or {},
        )
        if total_depth is not None:
            value, unit = total_depth
            well.total_depth_md_value = float(value)
            well.total_depth_md_unit = unit
        self.session.add(well)
        self.session.flush()
        return well

    def set_well_status(self, well: Well, new_status: WellLifecycleStatus | str, *, allow_same: bool = False) -> WellLifecycleStatus:
        """Transition the lifecycle, validating it.  Illegal jumps are refused."""
        target = WellLifecycleStatus(str(getattr(new_status, "value", new_status)))
        current = WellLifecycleStatus(well.lifecycle_status)
        if target is current:
            if allow_same:
                return current
            raise ValidationError(f"well is already {current.value}", well_id=well.id, status=current.value)
        allowed = WELL_LIFECYCLE_TRANSITIONS.get(current, ())
        if target not in allowed:
            raise ValidationError(
                f"illegal lifecycle transition {current.value} -> {target.value}",
                well_id=well.id,
                allowed=[state.value for state in allowed],
            )
        well.lifecycle_status = target.value
        self.session.flush()
        return target

    def update_well(self, well: Well, values: dict[str, Any]) -> list[str]:
        """Update whitelisted well attributes; returns the keys applied."""
        allowed = {
            "well_identifier",
            "well_type",
            "trajectory_type",
            "spud_date",
            "completion_date",
            "total_depth_md_value",
            "total_depth_md_unit",
            "total_depth_tvd_value",
            "total_depth_tvd_unit",
            "kb_elevation_value",
            "kb_elevation_unit",
            "surface_x_value",
            "surface_y_value",
            "coordinate_system",
            "notes",
            "field_id",
            "project_id",
        }
        applied: list[str] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            setattr(well, key, value)
            applied.append(key)
        self.session.flush()
        return applied

    # -- sections -----------------------------------------------------------
    def list_sections(self, well_id: str) -> list[WellSection]:
        return list(self.session.execute(select(WellSection).where(WellSection.well_id == well_id).order_by(WellSection.sequence)).scalars())

    def get_or_create_section(
        self,
        well: Well,
        name: str,
        *,
        sequence: int | None = None,
        hole_size_in: float | None = None,
        casing_program: str | None = None,
        planned_mud_weight: tuple[float, str] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> WellSection:
        existing = self.session.execute(select(WellSection).where(WellSection.well_id == well.id, WellSection.name == name)).scalar_one_or_none()
        if existing is not None:
            return existing
        section = WellSection(
            id=new_id("sec"),
            well_id=well.id,
            name=name,
            sequence=sequence or (len(self.list_sections(well.id)) + 1),
            hole_size_in=hole_size_in,
            casing_program=casing_program,
            attributes=attributes or {},
        )
        if planned_mud_weight is not None:
            value, unit = planned_mud_weight
            section.planned_mud_weight_value = float(value)
            section.planned_mud_weight_unit = unit
        self.session.add(section)
        self.session.flush()
        return section

    def update_section(self, section: WellSection, values: dict[str, Any], *, state: RecordState | str = RecordState.PLANNED) -> list[str]:
        """Write section attributes for a *state*.

        Planned and actual mud weight/duration are separate columns on purpose:
        an actual value can never overwrite the plan (section 11).
        """
        record_state = RecordState(str(getattr(state, "value", state)))
        pairs = {
            RecordState.PLANNED: {
                "mud_weight": ("planned_mud_weight_value", "planned_mud_weight_unit"),
                "duration_days": ("planned_duration_days", None),
                "top_depth": ("top_depth_value", "top_depth_unit"),
                "bottom_depth": ("bottom_depth_value", "bottom_depth_unit"),
            },
            RecordState.ACTUAL: {
                "mud_weight": ("actual_mud_weight_value", "actual_mud_weight_unit"),
                "duration_days": ("actual_duration_days", None),
                "top_depth": ("top_depth_value", "top_depth_unit"),
                "bottom_depth": ("bottom_depth_value", "bottom_depth_unit"),
            },
        }
        table = pairs.get(record_state)
        if table is None:
            raise ValidationError(
                f"section attribute {record_state.value} is not a planned/actual pair target; use knowledge items for FORECAST values",
                state=record_state.value,
            )
        applied: list[str] = []
        for key, value in values.items():
            if key in table:
                value_attr, unit_attr = table[key]
                if isinstance(value, (tuple, list)) and len(value) == 2:
                    setattr(section, value_attr, float(value[0]))
                    if unit_attr:
                        setattr(section, unit_attr, str(value[1]))
                else:
                    setattr(section, value_attr, float(value))
                applied.append(f"{record_state.value}.{key}")
            elif key in {"hole_size_in", "casing_program", "formation_top", "notes", "sequence"}:
                setattr(section, key, value)
                applied.append(key)
        self.session.flush()
        return applied

    # -- statistics ---------------------------------------------------------
    def well_statistics(self, well_id: str) -> dict[str, Any]:
        sections = self.list_sections(well_id)
        documents = list(self.session.execute(select(Document).where(Document.well_id == well_id)).scalars())
        by_class: dict[str, int] = {}
        for document in documents:
            by_class[document.classification] = by_class.get(document.classification, 0) + 1
        return {
            "sections": len(sections),
            "documents": len(documents),
            "documents_by_classification": by_class,
            "planned_sections": sum(1 for section in sections if section.planned_mud_weight_value is not None),
            "actual_sections": sum(1 for section in sections if section.actual_mud_weight_value is not None),
        }


__all__ = ["WellRepository"]
