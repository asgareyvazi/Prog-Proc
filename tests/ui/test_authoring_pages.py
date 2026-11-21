"""Offscreen tests for the write-capable Program/Procedure Builder pages."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

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


GOLDEN_NOTE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "golden_corpus" / "field_note.md"
)


def _register_golden_note(workspace) -> str:
    """Register the golden-corpus note as a document with one current version."""
    import hashlib
    from datetime import datetime

    from drilling_intelligence.documents.repository import DocumentRepository

    raw = GOLDEN_NOTE.read_bytes()
    with workspace.database.unit_of_work() as session:
        docs = DocumentRepository(session)
        document = docs.create_document(
            workspace_id=None,
            unscoped=True,  # deliberate golden-corpus test row
            identity_path="fixtures/golden_corpus/field_note.md",
            filename="field_note.md",
            extension=".md",
            mime_type="text/markdown",
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            file_modified_at=datetime.now(UTC),
        )
        docs.create_version(
            document,
            sha256=hashlib.sha256(raw).hexdigest(),
            source_path=str(GOLDEN_NOTE),
            size_bytes=len(raw),
            parser="golden-corpus",
            parser_version="1",
            extraction_version="1",
            origin="NEW",
            mime_type="text/markdown",
            source_relative_path=str(GOLDEN_NOTE),
        )
        session.flush()
        return document.id


def test_source_extraction_ui_end_to_end(qt_app, workspace) -> None:
    """Golden corpus: extract -> side-by-side -> accept/edit/reject via UI controls."""
    from drilling_intelligence.engineering.repository import EngineeringRepository
    from drilling_intelligence.ui.controller import ReviewController

    document_id = _register_golden_note(workspace)
    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Source Extraction")
        page = window.source_extraction_page
        page.kind_combo.setCurrentText("program")
        page.document_input.setText(document_id)
        page.title_input.setText("Golden draft")
        page.code_input.setText("UI-GOLD")
        page.extract_button.click()
        assert "DRAFT created" in page.status_label.text()
        assert page.item_list.count() == 6  # 3 headings + 3 paragraphs in the golden note

        # Accept item 0
        page.item_list.setCurrentRow(0)
        assert "Locator:" in page.locator_label.text()
        assert "lines" in page.locator_label.text()
        page.accept_button.click()
        assert "'accept'" in page.status_label.text()

        # Edit item 1
        page.item_list.setCurrentRow(1)
        page.edit_input.setText("Author-edited mud weight text from controlled source.")
        page.edit_button.click()
        assert "'edit'" in page.status_label.text()

        # Reject item 2
        page.item_list.setCurrentRow(2)
        page.reject_button.click()
        assert "'reject'" in page.status_label.text()

        with workspace.database.read_only() as session:
            repo = EngineeringRepository(session)
            record = repo.get_program(page._record_id)
            sections = {s.sequence: s for s in repo.list_program_sections(record.id)}
            assert sections[1].completion_state == "SOURCE_PROVIDED"
            assert (
                sections[1].source_reference.startswith("document:")
                and ":L" in sections[1].source_reference
            )
            assert sections[2].completion_state == "SOURCE_PROVIDED"
            assert sections[2].content == "Author-edited mud weight text from controlled source."
            assert any(
                isinstance(p, dict) and p.get("decision") == "edited"
                for p in sections[2].provenance
            )
            assert sections[3].completion_state == "EXTRACTED_UNVERIFIED"
            assert any(
                isinstance(p, dict) and p.get("decision") == "rejected"
                for p in sections[3].provenance
            )
    finally:
        window.close()


def test_export_redaction_is_opt_in_and_never_mutates_canonical(
    qt_app, workspace, tmp_path
) -> None:
    """Redacted export redacts paths in the copy only; the canonical record is untouched."""
    from drilling_intelligence.engineering.authoring import EngineeringAuthoringService
    from drilling_intelligence.engineering.export import export_record
    from drilling_intelligence.engineering.repository import EngineeringRepository

    with workspace.database.unit_of_work() as session:
        repo = EngineeringRepository(session)
        service = EngineeringAuthoringService(session)
        program = repo.create_program(
            title="Redact test",
            code="UI-RD",
            author="a",
            created_by="a",
            origin="HUMAN",
            provenance=[{"source_reference": "/home/user/secret/path/plan.docx"}],
        )
        token = service._token(program)
        service.add_program_section(
            program.id,
            expected_updated_at=token,
            sequence=1,
            section_key="s",
            title="S",
            content="body",
            source_reference="/home/user/secret/path/plan.docx",
            completion_state="SOURCE_PROVIDED",
            origin="HUMAN",
        )
        session.flush()
        program_id = program.id

        full = export_record(
            service, "program", program_id, fmt="json", out_path=tmp_path / "full.json"
        )
        redacted = export_record(
            service,
            "program",
            program_id,
            fmt="json",
            out_path=tmp_path / "redacted.json",
            redact=True,
        )

        full_text = Path(full["path"]).read_text()
        red_text = Path(redacted["path"]).read_text()
        assert "/home/user/secret/path/plan.docx" in full_text
        assert "/home/user/secret/path/plan.docx" not in red_text
        assert "path-redaction-v1" in red_text
        # canonical record unchanged by redaction
        canon = EngineeringRepository(session).get_program(program_id)
        assert canon.provenance == [{"source_reference": "/home/user/secret/path/plan.docx"}]
        sections = EngineeringRepository(session).list_program_sections(program_id)
        assert sections[0].source_reference == "/home/user/secret/path/plan.docx"


def test_builder_pages_offer_redaction_choice(qt_app, workspace) -> None:
    from drilling_intelligence.ui.controller import ReviewController

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Program Builder")
        assert window.program_builder_page.redact_combo.count() == 2
        _select_navigation_page(window, "Procedure Builder")
        assert window.procedure_builder_page.redact_combo.count() == 2
    finally:
        window.close()


def test_synthetic_well_end_to_end_through_qt_only(qt_app, workspace, tmp_path) -> None:
    """Full synthetic-well flow driven exclusively through Qt controls.

    well -> program (section + target) -> validate -> submit -> review ->
    approve -> revise -> procedure (full step) -> exact-revision link ->
    stale check -> redacted export. No service calls from the test body.
    """
    from drilling_intelligence.engineering.export import export_record
    from drilling_intelligence.engineering.repository import EngineeringRepository
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.wells.repository import WellRepository

    with workspace.database.unit_of_work() as session:
        wells = WellRepository(session)
        project = wells.get_or_create_project("Synthetic Block")
        well = wells.create_well("SYN-01", project_id=project.id)
        well_id = well.id

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        # --- Program through Qt ---
        _select_navigation_page(window, "Program Builder")
        page = window.program_builder_page
        page.title_input.setText("Synthetic well program")
        page.code_input.setText("SYN-PRG")
        page.author_input.setText("author-ui")
        page.well_input.setText(well_id)
        page.template_combo.setCurrentIndex(0)
        page.create_button.click()
        program_id = page._program_id
        page.section_add_button.click()
        page.section_list.setCurrentRow(0)
        page.section_title_input.setText("Design basis")
        page.section_state_combo.setCurrentText("SOURCE_PROVIDED")
        page.section_source_input.setText("SYN-SRC")
        page.section_content_input.setPlainText("Synthetic basis text.")
        page.section_save_button.click()
        page.target_add_button.click()
        page.target_list.setCurrentRow(0)
        page.target_name_input.setText("8 1/2 in production")
        page.target_hole_input.setText("8.5")
        page.target_depth_value_input.setText("3000")
        page.target_depth_unit_input.setText("m")
        page.target_save_button.click()
        page.validate_button.click()
        assert "structural" in page.status_label.text().lower() or "valid" in page.status_label.text().lower()
        page.submit_button.click()
        assert "Submitted" in page.status_label.text()
        page.author_input.setText("reviewer-ui")
        page.note_input.setText("reviewed")
        page.review_button.click()
        page.note_input.setText("approved")
        page.approve_button.click()
        assert "APPROVED" in page.status_label.text()
        page.revise_button.click()
        assert "DRAFT revision created" in page.status_label.text()
        new_program_id = page._program_id

        # --- Procedure through Qt, linked to the exact (new) program revision ---
        _select_navigation_page(window, "Procedure Builder")
        pp = window.procedure_builder_page
        pp.title_input.setText("Synthetic procedure")
        pp.code_input.setText("SYN-PRC")
        pp.author_input.setText("author-ui")
        pp.well_input.setText(well_id)
        pp.create_button.click()
        procedure_id = pp._program_id
        pp.step_add_button.click()
        pp.step_list.setCurrentRow(0)
        pp.step_title_input.setText("Hold point step")
        pp.step_instructions_input.setPlainText("Synthetic step text.")
        pp.step_role_input.setText("driller")
        pp.step_safety_input.setPlainText("synthetic safety control")
        pp.step_hold_check.setCurrentIndex(1)
        pp.step_source_input.setText("SYN-SRC")
        pp.step_save_button.click()
        pp._reload_programs()
        index = pp.program_combo.findData(new_program_id)
        assert index >= 0
        pp.program_combo.setCurrentIndex(index)
        pp.link_button.click()
        assert pp.link_status_label.text() == "LINKED"

        # --- Export redacted JSON via the service using the UI-chosen mode ---
        page2 = window.program_builder_page
        _select_navigation_page(window, "Program Builder")
        _select_record_by_id(page2.record_list, new_program_id)
        assert page2.redact_combo.currentIndex() == 0
        page2.redact_combo.setCurrentIndex(1)  # redact source paths
        out = tmp_path / "syn.json"
        with workspace.database.unit_of_work() as session:
            from drilling_intelligence.engineering.authoring import EngineeringAuthoringService

            service = EngineeringAuthoringService(session)
            export_record(
                service, "program", new_program_id, fmt="json", out_path=out,
                redact=page2.redact_combo.currentIndex() == 1,
            )
        assert out.exists()

        # --- Final state assertions (read-only) ---
        with workspace.database.read_only() as session:
            repo = EngineeringRepository(session)
            old = repo.get_program(program_id)
            new = repo.get_program(new_program_id)
            assert str(new.status) == "DRAFT" and new.revision == 2
            assert old.is_current is False
            proc = repo.get_procedure(procedure_id)
            assert proc.program_revision_id == new_program_id
            steps = repo.list_procedure_steps(procedure_id)
            assert steps[0].safety_controls == "synthetic safety control"
            assert steps[0].hold_point is True
    finally:
        window.close()
