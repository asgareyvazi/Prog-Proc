"""The operational corpus, shared by the promotion, intelligence and golden-scenario tests.

One place builds a workspace with the real generated corpus in it, because "what the files say" is the
thing all three suites are checking: if a fixture grew its own copy of the expectations, a change to the
corpus generator would be caught by only one of them - and the whole value of these tests is that the
numbers are the corpus's, not the test's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from tests.fixtures.generate import build_corpus

from drilling_intelligence.database.models import Field, Well
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.operations.service import OperationalService
from drilling_intelligence.wells.repository import WellRepository

__all__ = [
    "DDR_ACTIVITIES",
    "DDR_NPT_LINES",
    "DDR_TOTAL",
    "STATED",
    "ZERO_HOURS",
    "add_casing_program",
    "fetch",
    "field_id",
    "promote",
    "well_id_for",
]

#: The three lines of ``npt_summary_2025-06.csv`` that state lost time (well, date, hours, code).
STATED: tuple[tuple[str, str, float, str], ...] = (
    ("A-3", "2025-06-13", 6.5, "NPT-STUCK"),
    ("A-3", "2025-06-14", 12.0, "NPT-EQUIP"),
    ("B-11", "2025-04-02", 22.25, "NPT-STUCK"),
)
#: The fourth line, which states that nothing was lost.
ZERO_HOURS = 0.0
#: The daily report's "Activity / Hours" sheet: six activity rows, two of them coded NPT.
DDR_ACTIVITIES = 6
DDR_NPT_LINES: tuple[float, float] = (6.5, 12.0)
#: ...and the summary field beside them, which adds up to 18.5 h and is therefore not a seventh row.
DDR_TOTAL = 18.5
#: The hours the two files state for the same two events on A-3: 59.25 is the sum of the *records*, not
#: of distinct incidents, and a test that called it anything else would be rewriting the corpus.
TOTAL_NPT_HOURS = 59.25


def register_wells(workspace, *, wells: tuple[str, ...] = ("A-3", "B-11")) -> dict[str, Any]:
    """The field, the project and the wells, through the repository that owns them."""
    with workspace.database.session() as session:
        repository = WellRepository(session)
        repository.get_or_create_workspace(str(workspace.root), name="North Cormorant")
        project = repository.get_or_create_project("North Cormorant")
        field = repository.get_or_create_field("North Cormorant", project=project)
        rows = {
            name: repository.create_well(name, project_id=project.id, field_id=field.id)
            for name in wells
        }
        session.commit()
        return {"project": project, "field": field, "wells": rows}


def ingest(workspace, *, wells: tuple[str, ...] = ("A-3", "B-11")) -> Path:
    """Build the corpus on disk and run it through the real pipeline (no mock extractor anywhere)."""
    hierarchy = register_wells(workspace, wells=wells)
    root = workspace.root / "corpus"
    build_corpus(root)
    pipeline = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    )
    result = pipeline.run(root=root, well_id=str(hierarchy["wells"][wells[0]].id))
    assert result.ok, result.error
    assert result.failures == 0, [item.error for item in result.failures_report()]
    return root


def promote(workspace) -> dict[str, Any]:
    return OperationalService.for_workspace(workspace).promote_workspace()


def fetch(workspace, model: type, **filters: object) -> list[Any]:
    with workspace.database.read_only() as session:
        statement = select(model)
        for key, value in filters.items():
            statement = statement.where(getattr(model, key) == value)
        return list(session.scalars(statement.order_by(model.id)))


def field_id(workspace) -> str:
    with workspace.database.read_only() as session:
        return str(session.scalar(select(Field.id)) or "")


def well_id_for(workspace, name: str) -> str:
    with workspace.database.read_only() as session:
        return str(session.scalar(select(Well.id).where(Well.name == name)) or "")


def add_casing_program(workspace, *, well_name: str = "A-3") -> dict[str, Any]:
    """A program, a drilled section and its targets, so plan-versus-actual has two sides to compare.

    The promotion suite has no sections - a daily report in this corpus does not state a section - so the
    tests that need the planned half build it through the repositories that own those rows.  The section
    comes from :meth:`~drilling_intelligence.wells.repository.WellRepository.get_or_create_section` and
    its numbers from ``update_section``, because planned and actual are written through different state
    arguments there, and a fixture that set the columns by hand would not be testing the same path a
    caller uses.
    """
    from drilling_intelligence.core.enums import RecordState
    from drilling_intelligence.database.models import Well
    from drilling_intelligence.engineering.repository import EngineeringRepository

    provenance = [
        {
            "kind": "spreadsheet",
            "document": {"sheet": "Targets", "cell": "D7", "page": 1},
            "excerpt": "planned depth 9850 m MD",
            "method": "manual",
        }
    ]
    with workspace.database.session() as session:
        well = session.scalar(select(Well).where(Well.name == well_name))
        assert well is not None, f"no well {well_name!r} in this workspace"
        wells = WellRepository(session)
        section = wells.get_or_create_section(
            well,
            "8 1/2 in",
            sequence=1,
            hole_size_in=8.5,
            casing_program="9 5/8 in liner + 7 in liner",
        )
        wells.update_section(
            section,
            {
                "top_depth": (3500.0, "m"),
                "bottom_depth": (9850.0, "m"),
                "duration_days": 12.0,
                "mud_weight": (11.4, "ppg"),
            },
            state=RecordState.PLANNED,
        )
        wells.update_section(
            section,
            {
                "duration_days": 14.5,
                "mud_weight": (11.9, "ppg"),
            },
            state=RecordState.ACTUAL,
        )
        repository = EngineeringRepository(session)
        existing = repository.list_programs(well_id=str(well.id))
        program = (
            existing[0]
            if existing
            else repository.create_program(
                title=f"{well_name} 8 1/2 in programme",
                code=f"NCF-{well_name}-PROG",
                well_id=str(well.id),
                field_id=str(well.field_id),
                provenance=provenance,
            )
        )
        if not repository.list_targets(program.id):
            repository.add_target(
                program.id,
                name="8 1/2 in",
                section_id=str(section.id),
                sequence=1,
                planned_depth_md_value=9850.0,
                planned_depth_md_unit="m",
                planned_duration_days=12.0,
                planned_mud_weight_value=11.4,
                planned_mud_weight_unit="ppg",
                provenance=provenance,
            )
        session.commit()
        return {"program": program, "section": section, "well": well}
