"""Offscreen tests for the write-capable Program/Procedure Builder pages."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


def _open_window(controller, workspace):
    from drilling_intelligence.ui.main_window import MainWindow

    assert controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    return MainWindow(controller)


def _select_navigation_page(window, label: str) -> None:
    matches = [
        row
        for row in range(window.navigation.count())
        if window.navigation.item(row).text() == label
    ]
    assert len(matches) == 1
    window.navigation.setCurrentRow(matches[0])
    assert window.pages.currentIndex() == matches[0]


def test_program_builder_creates_edits_and_persists_a_real_program(qt_app, workspace) -> None:
    from drilling_intelligence.engineering.repository import EngineeringRepository
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.wells.repository import WellRepository

    with workspace.database.unit_of_work() as session:
        wells = WellRepository(session)
        project = wells.get_or_create_project("UI Block")
        well = wells.create_well("UI-01", project_id=project.id)
        well_id = well.id

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Program Builder")
        page = window.program_builder_page

        page.title_input.setText("UI authored program")
        page.code_input.setText("UI-DRP-1")
        page.author_input.setText("ui-author")
        page.well_input.setText(well_id)
        page.template_combo.setCurrentIndex(0)
        page.create_button.click()
        assert page._program_id is not None
        assert "rev 1" in page.status_label.text()

        page.section_add_button.click()
        assert page.section_list.count() == 1
        page.section_list.setCurrentRow(0)
        page.section_title_input.setText("Design basis")
        page.section_key_input.setText("design_basis")
        page.section_state_combo.setCurrentText("SOURCE_PROVIDED")
        page.section_source_input.setText("FIXTURE-SRC-UI")
        page.section_content_input.setPlainText("UI-entered fixture basis text.")
        page.section_save_button.click()
        assert "Section saved" in page.status_label.text()

        # Add one planned target through the service (the UI target editor is a later phase).
        from drilling_intelligence.engineering.authoring import EngineeringAuthoringService

        with workspace.database.unit_of_work() as session:
            repo = EngineeringRepository(session)
            service = EngineeringAuthoringService(session)
            program = repo.get_program(page._program_id)
            service.add_program_target(
                program.id,
                expected_updated_at=service._token(program),
                name="12 1/4 in",
                hole_size_in=12.25,
                planned_depth_md_value=1800.0,
                planned_depth_md_unit="m",
            )
        page._load_record_into_editor()
        page.validate_button.click()
        assert "ready_for_review=True" in page.status_label.text()

        # Persistence: read back through a fresh session, not through the widget.
        with workspace.database.read_only() as session:
            repo = EngineeringRepository(session)
            program = repo.get_program(page._program_id)
            assert str(program.status) == "DRAFT"
            sections = repo.list_program_sections(program.id)
            assert len(sections) == 1
            assert sections[0].title == "Design basis"
            assert sections[0].completion_state == "SOURCE_PROVIDED"
            assert sections[0].content == "UI-entered fixture basis text."
    finally:
        window.close()


def test_program_builder_template_create_and_export_json(qt_app, workspace, tmp_path) -> None:
    from drilling_intelligence.ui.controller import ReviewController

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Program Builder")
        page = window.program_builder_page
        page.title_input.setText("Templated UI program")
        page.author_input.setText("ui-author")
        page.template_combo.setCurrentIndex(1)  # first real template
        page.create_button.click()
        assert page.section_list.count() > 3
        for row in range(page.section_list.count()):
            assert "OWNER_DECISION_REQUIRED" in page.section_list.item(row).text()

        out = tmp_path / "ui-program.json"
        from drilling_intelligence.engineering.export import export_record

        with workspace.database.unit_of_work() as session:
            from drilling_intelligence.engineering.authoring import EngineeringAuthoringService

            service = EngineeringAuthoringService(session)
            result = export_record(service, "program", page._program_id, fmt="json", out_path=out)
        assert result["watermarked_draft"] is True
        payload = json.loads(out.read_text())
        assert payload["title"] == "Templated UI program"
        assert len(payload["sections"]) == page.section_list.count()
    finally:
        window.close()


def test_procedure_builder_steps_and_exact_program_link(qt_app, workspace) -> None:
    from drilling_intelligence.engineering.authoring import EngineeringAuthoringService
    from drilling_intelligence.engineering.repository import EngineeringRepository
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.wells.repository import WellRepository

    with workspace.database.unit_of_work() as session:
        wells = WellRepository(session)
        project = wells.get_or_create_project("UI Block")
        well = wells.create_well("UI-02", project_id=project.id)
        well_id = well.id

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        # Create a Program first (headless service, then visible in the combo).
        with workspace.database.unit_of_work() as session:
            repo = EngineeringRepository(session)
            program = repo.create_program(
                code="UI-LINK-DRP",
                title="Link target",
                author="ui-author",
                created_by="ui-author",
                well_id=well_id,
            )
            program_id = program.id

        _select_navigation_page(window, "Procedure Builder")
        page = window.procedure_builder_page
        page._reload_programs()
        assert page.program_combo.count() == 1

        page.title_input.setText("UI authored procedure")
        page.code_input.setText("UI-PROC-1")
        page.author_input.setText("ui-author")
        page.well_input.setText(well_id)
        page.template_combo.setCurrentIndex(0)
        page.create_button.click()
        assert page._program_id is not None

        # Purpose/scope is a record-level field edited through the service boundary.
        with workspace.database.unit_of_work() as session:
            repo = EngineeringRepository(session)
            service = EngineeringAuthoringService(session)
            procedure = repo.get_procedure(page._program_id)
            service.update_record_content(
                "procedure",
                procedure.id,
                expected_updated_at=service._token(procedure),
                changes={"description": "UI fixture purpose and scope."},
            )

        page.step_add_button.click()
        assert page.step_list.count() == 1
        page.step_list.setCurrentRow(0)
        page.step_title_input.setText("Notify driller")
        page.step_instructions_input.setPlainText("UI fixture instruction text.")
        page.step_role_input.setText("driller")
        page.step_acceptance_input.setPlainText("Acknowledged in log.")
        page.step_hold_check.setCurrentIndex(1)
        page.step_source_input.setText("FIXTURE-SRC-UI")
        page.step_save_button.click()
        assert "Step saved" in page.status_label.text()

        page.validate_button.click()
        assert "ready_for_review=True" in page.status_label.text()

        page.link_button.click()
        assert page.link_status_label.text() == "LINKED"

        with workspace.database.read_only() as session:
            repo = EngineeringRepository(session)
            service = EngineeringAuthoringService(session)
            procedure = repo.get_procedure(page._program_id)
            assert procedure.program_revision_id == program_id
            assert procedure.program_link_status == "LINKED"
            steps = repo.list_procedure_steps(procedure.id)
            assert steps[0].hold_point is True
            assert steps[0].responsible_role == "driller"
            assert service.procedure_program_link(procedure.id)["link_status"] == "LINKED"
    finally:
        window.close()


def test_builder_pages_present_without_workspace(qt_app) -> None:
    from drilling_intelligence.ui.main_window import MainWindow

    window = MainWindow()
    try:
        _select_navigation_page(window, "Program Builder")
        assert window.program_builder_page.record_list.count() == 0
        _select_navigation_page(window, "Procedure Builder")
        assert window.procedure_builder_page.record_list.count() == 0
    finally:
        window.close()
