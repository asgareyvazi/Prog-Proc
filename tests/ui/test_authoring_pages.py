"""Offscreen tests for the write-capable Program/Procedure Builder pages."""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt

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

        # Add and edit the planned target entirely through UI controls.
        page.target_add_button.click()
        assert page.target_list.count() == 1
        page.target_list.setCurrentRow(0)
        page.target_name_input.setText("12 1/4 in")
        page.target_hole_input.setText("12.25")
        page.target_casing_input.setText("9 5/8 in casing")
        page.target_formation_input.setText("Fixture Sand")
        page.target_depth_value_input.setText("1800.5")
        page.target_depth_unit_input.setText("m")
        page.target_duration_input.setText("9.5")
        page.target_mud_value_input.setText("1.42")
        page.target_mud_unit_input.setText("kg/m3")
        page.target_npt_input.setText("4")
        page.target_cost_value_input.setText("98000")
        page.target_cost_unit_input.setText("EUR")
        page.target_source_input.setText("FIXTURE-SRC-UI")
        page.target_save_button.click()
        assert "Target saved" in page.status_label.text()
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
            targets = repo.list_targets(program.id)
            assert len(targets) == 1
            t = targets[0]
            assert t.name == "12 1/4 in"
            assert t.hole_size_in == 12.25
            assert t.casing_program == "9 5/8 in casing"
            assert t.formation_top == "Fixture Sand"
            assert t.planned_depth_md_value == 1800.5
            assert t.planned_depth_md_unit == "m"
            assert t.planned_duration_days == 9.5
            assert t.planned_mud_weight_value == 1.42
            assert t.planned_mud_weight_unit == "kg/m3"
            assert t.planned_npt_hours == 4.0
            assert t.planned_cost_value == 98000.0
            assert t.planned_cost_unit == "EUR"
            assert t.provenance == [{"source_reference": "FIXTURE-SRC-UI"}]
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
        page.step_equipment_input.setPlainText("radio, PPE")
        page.step_preconditions_input.setPlainText("permit signed")
        page.step_parameters_input.setPlainText(
            '[{"name": "WOB", "value": 25, "unit": "kN", "source_reference": "FIXTURE-SRC-UI"}]'
        )
        page.step_expected_input.setPlainText("acknowledged")
        page.step_acceptance_input.setPlainText("Acknowledged in log.")
        page.step_evidence_input.setPlainText("DDR entry")
        page.step_safety_input.setPlainText("fixture control text from source")
        page.step_contingency_input.setPlainText("stop and escalate per source")
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
            step = steps[0]
            assert step.hold_point is True
            assert step.responsible_role == "driller"
            assert step.equipment == "radio, PPE"
            assert step.preconditions == "permit signed"
            assert step.parameters == [
                {"name": "WOB", "value": 25, "unit": "kN", "source_reference": "FIXTURE-SRC-UI"}
            ]
            assert step.expected_result == "acknowledged"
            assert step.acceptance_criteria == "Acknowledged in log."
            assert step.evidence_required == "DDR entry"
            assert step.safety_controls == "fixture control text from source"
            assert step.contingency == "stop and escalate per source"
            assert step.source_reference == "FIXTURE-SRC-UI"
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


def test_program_lifecycle_through_ui_buttons(qt_app, workspace) -> None:
    """Submit -> review -> approve -> revise -> compare, all via Qt controls."""
    from drilling_intelligence.engineering.repository import EngineeringRepository
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.wells.repository import WellRepository

    with workspace.database.unit_of_work() as session:
        wells = WellRepository(session)
        project = wells.get_or_create_project("UI Block")
        well = wells.create_well("UI-03", project_id=project.id)
        well_id = well.id

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Program Builder")
        page = window.program_builder_page
        page.title_input.setText("Lifecycle program")
        page.code_input.setText("UI-LC-1")
        page.author_input.setText("author-ui")
        page.well_input.setText(well_id)
        page.template_combo.setCurrentIndex(0)  # blank; sections added via UI below
        page.create_button.click()
        program_id = page._program_id

        page.section_add_button.click()
        page.section_list.setCurrentRow(0)
        page.section_title_input.setText("Design basis")
        page.section_state_combo.setCurrentText("SOURCE_PROVIDED")
        page.section_source_input.setText("SRC-UI")
        page.section_content_input.setPlainText("UI lifecycle basis.")
        page.section_save_button.click()
        page.target_add_button.click()
        page.target_list.setCurrentRow(0)
        page.target_name_input.setText("8 1/2 in")
        page.target_hole_input.setText("8.5")
        page.target_depth_value_input.setText("2200")
        page.target_depth_unit_input.setText("m")
        page.target_save_button.click()

        page.submit_button.click()
        assert "Submitted" in page.status_label.text()
        page.author_input.setText("reviewer-ui")  # distinct declared reviewer attribution
        page.note_input.setText("structural check done")
        page.review_button.click()
        assert "Review recorded" in page.status_label.text()
        page.note_input.setText("approved as exact revision")
        page.approve_button.click()
        assert "APPROVED" in page.status_label.text()

        with workspace.database.read_only() as session:
            repo = EngineeringRepository(session)
            approved = repo.get_program(program_id)
            assert str(approved.status) == "APPROVED"

        page.revise_button.click()
        assert "DRAFT revision created" in page.status_label.text()
        with workspace.database.read_only() as session:
            repo = EngineeringRepository(session)
            new_rev = repo.get_program(page._program_id)
            # revise creates a new current revision; the page now shows it
            assert new_rev.revision == 2
            assert str(new_rev.status) == "DRAFT"
            old = repo.get_program(program_id)
            assert old.is_current is False
            # supersession is mechanical; the approved content stays readable and unchanged
            assert str(old.status) in {"APPROVED", "SUPERSEDED"}
            assert old.approved_at is not None

        page.compare_button.click()
        assert "Comparison" in page.status_label.text()
    finally:
        window.close()


def _make_program_with_target(qt_app, workspace, code: str, well_id: str, depth: float) -> str:
    """Create a minimal valid DRAFT program through the Program Builder UI."""
    from drilling_intelligence.ui.controller import ReviewController

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Program Builder")
        page = window.program_builder_page
        page.title_input.setText(f"Program {code}")
        page.code_input.setText(code)
        page.author_input.setText("author-ui")
        page.well_input.setText(well_id)
        page.template_combo.setCurrentIndex(0)
        page.create_button.click()
        program_id = page._program_id
        page.section_add_button.click()
        page.section_list.setCurrentRow(0)
        page.section_title_input.setText("Design basis")
        page.section_state_combo.setCurrentText("SOURCE_PROVIDED")
        page.section_source_input.setText("SRC-UI")
        page.section_content_input.setPlainText("UI basis.")
        page.section_save_button.click()
        page.target_add_button.click()
        page.target_list.setCurrentRow(0)
        page.target_name_input.setText("8 1/2 in")
        page.target_hole_input.setText("8.5")
        page.target_depth_value_input.setText(str(depth))
        page.target_depth_unit_input.setText("m")
        page.target_save_button.click()
        return program_id
    finally:
        window.close()


def _select_record_by_id(list_widget, record_id: str) -> None:
    for row in range(list_widget.count()):
        item = list_widget.item(row)
        if item.data(Qt.ItemDataRole.UserRole) == record_id:
            list_widget.setCurrentRow(row)
            return
    raise AssertionError(f"record {record_id} not present in list")


def test_procedure_link_stale_and_cross_well_refusal_via_ui(qt_app, workspace) -> None:
    """Exact-revision link: LINKED -> STALE after program revision; cross-well refused."""
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.wells.repository import WellRepository

    with workspace.database.unit_of_work() as session:
        wells = WellRepository(session)
        project = wells.get_or_create_project("UI Block")
        well_a = wells.create_well("UI-A", project_id=project.id)
        well_b = wells.create_well("UI-B", project_id=project.id)
        well_a_id, well_b_id = well_a.id, well_b.id

    program_a = _make_program_with_target(qt_app, workspace, "UI-PA", well_a_id, 2000.0)

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Procedure Builder")
        page = window.procedure_builder_page
        page.title_input.setText("Procedure A")
        page.code_input.setText("UI-PR-A")
        page.author_input.setText("author-ui")
        page.well_input.setText(well_a_id)
        page.create_button.click()
        procedure_id = page._program_id

        page._reload_programs()
        index = page.program_combo.findData(program_a)
        assert index >= 0
        page.program_combo.setCurrentIndex(index)
        page.link_button.click()
        assert "Linked to exact Program revision" in page.status_label.text()
        assert page.link_status_label.text() == "LINKED"
        assert "exact revision" in page.link_impact_label.text()

        # Revise the program through its UI page -> the procedure link becomes STALE.
        _select_navigation_page(window, "Program Builder")
        ppage = window.program_builder_page
        ppage._refresh_records()
        _select_record_by_id(ppage.record_list, program_a)
        ppage.author_input.setText("reviewer-ui")
        ppage.revise_button.click()
        assert "DRAFT revision created" in ppage.status_label.text()

        _select_navigation_page(window, "Procedure Builder")
        ppage2 = window.procedure_builder_page
        ppage2._refresh_records()
        _select_record_by_id(ppage2.record_list, procedure_id)
        assert ppage2.link_status_label.text() == "STALE"
        assert "STALE" in ppage2.link_impact_label.text()
        assert "never auto-updated" in ppage2.link_impact_label.text()

        # Cross-well link attempt must be refused by the service through the UI.
        program_b = _make_program_with_target(qt_app, workspace, "UI-PB", well_b_id, 3000.0)
        ppage2._reload_programs()
        index = ppage2.program_combo.findData(program_b)
        assert index >= 0
        ppage2.program_combo.setCurrentIndex(index)
        ppage2.link_button.click()
        assert "cross-well" in ppage2.status_label.text()
        # link must remain the previous (stale) one, not the cross-well program
        assert ppage2.link_status_label.text() == "STALE"
    finally:
        window.close()


def test_template_library_browser_shows_versioned_families(qt_app, workspace) -> None:
    """The Template Library page lists families with immutable version history."""
    from drilling_intelligence.ui.controller import ReviewController

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Template Library")
        page = window.template_library_page
        assert page.family_list.count() == 2
        texts = [page.family_list.item(i).text() for i in range(page.family_list.count())]
        assert any("drilling_program" in t and "current v2" in t for t in texts)
        page.family_list.setCurrentRow(
            next(i for i in range(page.family_list.count()) if "drilling_program" in texts[i])
        )
        versions = [page.version_list.item(i).text() for i in range(page.version_list.count())]
        assert any("v1 [SUPERSEDED]" in v for v in versions)
        assert any("v2 [CURRENT]" in v for v in versions)
        assert "immutable" in page.detail_label.text()
    finally:
        window.close()


def test_create_from_superseded_template_version_is_flagged(qt_app, workspace) -> None:
    """Creating from a SUPERSEDED version is allowed but provenance records the version."""
    from drilling_intelligence.engineering.authoring import EngineeringAuthoringService
    from drilling_intelligence.engineering.repository import EngineeringRepository
    from drilling_intelligence.engineering.templates import create_from_template
    from drilling_intelligence.wells.repository import WellRepository

    with workspace.database.unit_of_work() as session:
        wells = WellRepository(session)
        project = wells.get_or_create_project("UI Block")
        well = wells.create_well("UI-04", project_id=project.id)
        well_id = well.id
        service = EngineeringAuthoringService(session)
        record = create_from_template(
            service,
            "program",
            "standard_drilling_program",
            content={"title": "T", "code": "UI-TPL", "author": "a", "well_id": well_id},
            template_version=1,
        )
        session.flush()
        assert record.provenance == [
            {
                "template": "standard_drilling_program",
                "kind": "structure_only",
                "template_version": 1,
            }
        ]
        repo = EngineeringRepository(session)
        sections = repo.list_program_sections(record.id)
        assert len(sections) == 12, "superseded v1 keeps its own 12-section structure"
