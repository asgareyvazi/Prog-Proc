"""Headless controller checks for the optional desktop boundary."""

from __future__ import annotations

import pytest


def test_controller_opens_existing_workspace_resolves_hierarchy_and_reads_without_writing(
    workspace,
) -> None:
    from drilling_intelligence.core.errors import ValidationError
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.wells.repository import WellRepository

    with workspace.database.unit_of_work() as session:
        repository = WellRepository(session)
        project = repository.get_or_create_project("Controller Project")
        field = repository.get_or_create_field("Controller Field", project=project)
        well = repository.create_well("Controller-1", project_id=project.id, field_id=field.id)
        well_id = str(well.id)

    controller = ReviewController()
    info = controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    with workspace.database.engine.connect() as connection:
        before = connection.exec_driver_sql('SELECT COUNT(*) FROM "well"').scalar_one()
    try:
        choices = controller.list_wells()
        choice = next(item for item in choices if item.well_id == well_id)
        assert choice.label == "Controller-1 — Controller Project / Controller Field"
        assert controller.resolve_well("Controller-1") == well_id
        assert controller.resolve_well(well_id) == well_id
        assert info["database_path"] == str(workspace.database_path)
        with pytest.raises(ValidationError, match="no well matches"):
            controller.resolve_well("missing-controller-well")
    finally:
        controller.close()

    with workspace.database.engine.connect() as connection:
        after = connection.exec_driver_sql('SELECT COUNT(*) FROM "well"').scalar_one()
    assert before == after
