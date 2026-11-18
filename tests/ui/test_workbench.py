"""Offscreen tests for the optional read-only desktop Review Workbench."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import inspect

pytestmark = pytest.mark.ui


def _select_navigation_page(window, label: str) -> None:
    """Select a workbench page by its visible label, not a brittle ordinal."""
    matches = [
        row
        for row in range(window.navigation.count())
        if window.navigation.item(row).text() == label
    ]
    assert len(matches) == 1
    row = matches[0]
    window.navigation.setCurrentRow(row)
    assert window.navigation.currentItem().text() == label
    assert window.pages.currentIndex() == row


def _database_fingerprint(workspace) -> tuple[Any, ...]:
    """Capture every SQLite row, including migration metadata, before/after UI reads."""
    names = sorted(inspect(workspace.database.engine).get_table_names())
    with workspace.database.engine.connect() as connection:
        return tuple(
            (
                name,
                tuple(
                    tuple(str(value) for value in row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{name}" ORDER BY rowid'  # noqa: S608
                    ).fetchall()
                ),
            )
            for name in names
        )


def test_default_package_is_import_light() -> None:
    """The headless package marker does not import Qt as a side effect."""
    import sys

    before = sys.modules.get("PySide6.QtWidgets")
    import drilling_intelligence.ui

    assert drilling_intelligence.ui.__all__ == []
    assert sys.modules.get("PySide6.QtWidgets") is before


def test_real_fixture_workbench_current_history_evidence_plan_actual_and_detail(
    qt_app, workspace
) -> None:
    """A real workspace and real DomainReviewService feed every major read-only view."""
    from tests.fixtures.fieldops import add_casing_program, ingest, promote, well_id_for

    from drilling_intelligence.review import DomainReviewRequest, DomainReviewService
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow

    ingest(workspace)
    promote(workspace)
    add_casing_program(workspace)
    well_id = well_id_for(workspace, "A-3")
    before = _database_fingerprint(workspace)

    controller = ReviewController()
    assert controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    window = MainWindow(controller)
    try:
        choices = controller.list_wells()
        assert any(choice.well_id == well_id for choice in choices)
        window.well_combo.addItem("A-3", well_id)

        service = DomainReviewService.for_workspace(controller.workspace)
        current = service.review(DomainReviewRequest(well_id=well_id, lifecycle="current"))
        window.set_review(current)
        qt_app.processEvents()

        assert window.overview_identity["well"].text() == "A-3"
        assert window.records_model.rowCount() == current.record_count
        assert window.sections_model.rowCount() == len(current.sections)
        assert window.plan_model.rowCount() == len(current.plan_actual)
        assert window.conflicts_model.rowCount() == len(current.conflicts)
        assert "CURRENT" in window.overview_counts.text()

        source_section = next(
            (index, row)
            for index in range(window.sections_model.row_count)
            if (row := window.sections_model.row_payload(index)) and row.get("provenance")
        )
        section_index, section_payload = source_section
        window.sections_table.selectRow(section_index)
        qt_app.processEvents()
        section_detail_keys = {
            window.sections_detail.topLevelItem(index).text(0)
            for index in range(window.sections_detail.topLevelItemCount())
        }
        assert {"id", "origin", "provenance", "planned_duration_days"}.issubset(section_detail_keys)
        provenance_item = next(
            window.sections_detail.topLevelItem(index)
            for index in range(window.sections_detail.topLevelItemCount())
            if window.sections_detail.topLevelItem(index).text(0) == "provenance"
        )
        assert provenance_item.childCount() > 0
        assert section_payload["provenance"], "the selected source section retains provenance"

        if window.records_model.rowCount():
            window.records_table.selectRow(0)
            qt_app.processEvents()
            assert window.record_detail.topLevelItemCount() > 0

        history = service.review(DomainReviewRequest(well_id=well_id, lifecycle="history"))
        window.set_review(history)
        qt_app.processEvents()
        assert window.history_model.rowCount() == sum(
            not record.current for record in history.records
        )
        assert window.lifecycle_combo.currentData() == "history"

        verified = service.review(
            DomainReviewRequest(well_id=well_id, lifecycle="current", verify_citations=True)
        )
        window.set_review(verified)
        qt_app.processEvents()
        assert verified.citation_audit is not None
        assert "Citation audit completed" in window.evidence_summary.text()
        assert window.audit_model.rowCount() == len(verified.citation_audit["checks"])
    finally:
        # Release, do not only close.  The application fixture is
        # session-scoped, so a window that is only closed stays alive until
        # the end of the session - and the workspace this window reads is
        # torn down before that.  The release path is the one the
        # application uses, so the test and the product share it.
        from drilling_intelligence.ui.lifetime import release_top_level_widgets

        release_top_level_widgets(qt_app)
        qt_app.processEvents()

    assert before == _database_fingerprint(workspace)


def test_presentation_filter_conflict_detail_and_explicit_error_states(qt_app) -> None:
    """Filtering is client-side, conflict detail is DTO-backed, and errors stay distinguishable."""
    from drilling_intelligence.review import ReviewConflict, ReviewRecord, ReviewVerification
    from drilling_intelligence.ui.main_window import MainWindow
    from drilling_intelligence.ui.worker import WorkerError

    window = MainWindow()
    try:
        # The window must be shown for isVisible() to mean anything: a child of a hidden
        # window reports False no matter what setVisible(True) did.  (This test could only
        # run once Qt loads on the host; the assertion was dormant until then.)
        window.show()
        qt_app.processEvents()
        record = ReviewRecord(
            record_type="calculation",
            record_id="calc-1",
            status="APPROVED",
            current=True,
            data={"outputs": {"ecd": 11.4}},
            verification=ReviewVerification(reproducibility="NOT_EXECUTABLE_IN_REPOSITORY"),
        )
        conflict = ReviewConflict(
            conflict_id="conflict-1",
            lookup_key="well/A-3/mud_weight",
            property_name="mud_weight",
            status="OPEN",
            record_state="ACTIVE",
            current=True,
            candidates=({"value": 10.2}, {"value": 11.0}),
        )
        from drilling_intelligence.review import DomainReview

        review = DomainReview(
            request={"well_id": "well-1", "lifecycle": "current"},
            subject={"well": {"id": "well-1", "name": "A-3"}},
            records=(record,),
            conflicts=(conflict,),
            plan_actual=(
                {
                    "metric": "duration_days",
                    "status": "NO_ACTUAL",
                    "planned": 10,
                    "actual": None,
                    "variance": None,
                },
            ),
            truncated=True,
        )
        window.set_review(review)
        window.records_table.selectRow(0)
        qt_app.processEvents()
        assert window.records_proxy.rowCount() == 1
        assert window.record_detail.topLevelItemCount() > 0
        assert window.banner.isVisible()
        assert window.plan_model.data(window.plan_model.index(0, 3)) == "NO_ACTUAL"

        window.conflicts_table.selectRow(0)
        qt_app.processEvents()
        assert window.conflict_detail.topLevelItemCount() > 0

        for category in ("input", "source", "integrity", "unexpected"):
            window._review_failed(WorkerError(category, f"{category} problem"))
            assert category.title() in window.status_label.text()
            assert f"{category} problem" in window.banner.text()
    finally:
        # Release, do not only close.  The application fixture is
        # session-scoped, so a window that is only closed stays alive until
        # the end of the session - and the workspace this window reads is
        # torn down before that.  The release path is the one the
        # application uses, so the test and the product share it.
        from drilling_intelligence.ui.lifetime import release_top_level_widgets

        release_top_level_widgets(qt_app)
        qt_app.processEvents()


def test_worker_uses_real_controller_boundary_for_current_and_history(qt_app, workspace) -> None:
    """The worker calls the controller boundary and keeps the service off the GUI call site."""
    from tests.fixtures.fieldops import ingest, promote, well_id_for

    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.worker import ReviewWorker

    ingest(workspace)
    promote(workspace)
    well_id = well_id_for(workspace, "A-3")
    controller = ReviewController()
    controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    worker = ReviewWorker(controller, well_id, "current", False)
    received: list[Any] = []
    worker.succeeded.connect(received.append)
    worker.run()
    assert received and received[0].request["lifecycle"] == "current"
    controller.close()


def test_decision_tab_renders_the_service_pack_without_touching_the_database(
    qt_app, workspace
) -> None:
    """The Decision tab displays the controller's payload; it computes nothing and writes nothing."""
    from tests.fixtures.fieldops import ingest, promote, well_id_for

    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow
    from drilling_intelligence.ui.worker import DecisionWorker

    ingest(workspace)
    promote(workspace)
    well_id = well_id_for(workspace, "A-3")
    before = _database_fingerprint(workspace)

    controller = ReviewController()
    assert controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    window = MainWindow(controller)
    try:
        # Select by its visible label; other pages may be inserted without shifting this test.
        _select_navigation_page(window, "Decision")

        payload = controller.decision(well_id)
        window.set_decision(payload)
        qt_app.processEvents()

        assert "decision-pack/" in window.decision_header.text()
        assert window.decision_tree.topLevelItemCount() > 0
        keys = {
            window.decision_tree.topLevelItem(index).text(0)
            for index in range(window.decision_tree.topLevelItemCount())
        }
        # The whole document is visible, section by section - not a widget-side summary of it.
        assert {"subject", "execution", "economics", "risk", "limitations"} <= keys

        # The worker runs the same controller boundary the CLI uses.
        worker = DecisionWorker(controller, well_id)
        received: list[Any] = []
        failed: list[Any] = []
        worker.succeeded.connect(received.append)
        worker.failed.connect(failed.append)
        worker.run()
        assert received and not failed
        assert received[0]["identity"] == payload["identity"], (
            "worker and direct read must be the same pack - same state, same identity"
        )
    finally:
        # Release, do not only close.  The application fixture is
        # session-scoped, so a window that is only closed stays alive until
        # the end of the session - and the workspace this window reads is
        # torn down before that.  The release path is the one the
        # application uses, so the test and the product share it.
        from drilling_intelligence.ui.lifetime import release_top_level_widgets

        release_top_level_widgets(qt_app)
        qt_app.processEvents()
        controller.close()

    assert before == _database_fingerprint(workspace), "rendering the pack wrote to the database"


def test_comparison_tab_renders_the_matrix_without_touching_the_database(qt_app, workspace) -> None:
    """The Comparison tab displays the controller's matrix; it computes nothing and writes nothing."""
    from tests.fixtures.fieldops import ingest, promote, well_id_for

    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow
    from drilling_intelligence.ui.worker import ComparisonWorker

    ingest(workspace)
    promote(workspace)
    a3 = well_id_for(workspace, "A-3")
    b11 = well_id_for(workspace, "B-11")
    before = _database_fingerprint(workspace)

    controller = ReviewController()
    assert controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    window = MainWindow(controller)
    try:
        _select_navigation_page(window, "Comparison")

        payload = controller.compare([a3, b11])
        window.set_comparison(payload)
        qt_app.processEvents()

        assert "well-comparison/" in window.comparison_header.text()
        header_labels = [
            window.comparison_table.horizontalHeaderItem(column).text()
            for column in range(window.comparison_table.columnCount())
        ]
        assert header_labels[0] == "Metric"
        assert header_labels[-1] == "State"
        assert window.comparison_table.rowCount() > 0
        # The matrix shows every metric row of the pack, both subjects, and the verdict.
        metrics = {
            window.comparison_table.item(row, 0).text()
            for row in range(window.comparison_table.rowCount())
        }
        assert "npt.rows" in metrics and "cost.planned" in metrics
        # Missing is visibly distinct from zero: an unvalued cell is the em dash, a counted
        # zero is a zero - the exact distinction the JSON carries.
        cells = {
            window.comparison_table.item(row, column).text()
            for row in range(window.comparison_table.rowCount())
            for column in range(1, window.comparison_table.columnCount() - 1)
        }
        assert "—" in cells, cells
        assert "0" in cells, cells
        assert window.comparison_limitations.text().startswith("Limitations:")

        # The worker runs the same controller boundary the CLI uses.
        worker = ComparisonWorker(controller, [a3, b11])
        received: list[Any] = []
        failed: list[Any] = []
        worker.succeeded.connect(received.append)
        worker.failed.connect(failed.append)
        worker.run()
        assert received and not failed
        assert received[0]["identity"] == payload["identity"], (
            "worker and direct read must be the same pack - same state, same identity"
        )
    finally:
        # Release, do not only close.  The application fixture is
        # session-scoped, so a window that is only closed stays alive until
        # the end of the session - and the workspace this window reads is
        # torn down before that.  The release path is the one the
        # application uses, so the test and the product share it.
        from drilling_intelligence.ui.lifetime import release_top_level_widgets

        release_top_level_widgets(qt_app)
        qt_app.processEvents()
        controller.close()

    assert before == _database_fingerprint(workspace), "rendering the pack wrote to the database"


def test_report_export_uses_the_canonical_renderer_and_the_chart_does_not_query(
    qt_app, workspace, tmp_path
) -> None:
    """The workbench chart and HTML export consume the report contract, not a second fold."""
    from tests.fixtures.fieldops import ingest, promote, well_id_for

    from drilling_intelligence.intelligence.service import IntelligenceService
    from drilling_intelligence.reporting.html import render_html
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow
    from drilling_intelligence.ui.worker import ReportWorker

    ingest(workspace)
    promote(workspace)
    a3 = well_id_for(workspace, "A-3")
    b11 = well_id_for(workspace, "B-11")
    before = _database_fingerprint(workspace)
    controller = ReviewController()
    assert controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    window = MainWindow(controller)
    try:
        _select_navigation_page(window, "Report")
        payload = controller.report(well_ids=[a3, b11])
        comparison = controller.compare([a3, b11])
        window.set_comparison(comparison)
        qt_app.processEvents()
        assert window.comparison_table.rowCount() > 0
        assert window.comparison_chart.state()
        assert window.comparison_chart.subject_ids() == [
            subject["well_id"] for subject in comparison["basis"]["subjects"]
        ]
        chart_before = _database_fingerprint(workspace)
        window.comparison_chart.repaint()
        qt_app.processEvents()
        assert _database_fingerprint(workspace) == chart_before
        window.set_report(payload)
        qt_app.processEvents()
        assert payload["identity"] in window.report_header.text()
        path = tmp_path / "report.html"
        assert window.export_loaded_report(str(path)) == payload["identity"]
        assert path.read_text(encoding="utf-8") == render_html(payload)
        with workspace.database.read_only() as session:
            service_payload = (
                IntelligenceService.for_workspace(workspace)
                .report(well_ids=[a3, b11], session=session)
                .to_dict()
            )
        assert service_payload["identity"] == payload["identity"]
        worker = ReportWorker(controller, well_ids=[a3])
        received: list[Any] = []
        failed: list[Any] = []
        worker.succeeded.connect(received.append)
        worker.failed.connect(failed.append)
        worker.run()
        assert received and not failed
        assert received[0]["mode"] == "single_well"
        assert received[0]["schema"] == "engineering-report/1"
    finally:
        # Release, do not only close.  The application fixture is
        # session-scoped, so a window that is only closed stays alive until
        # the end of the session - and the workspace this window reads is
        # torn down before that.  The release path is the one the
        # application uses, so the test and the product share it.
        from drilling_intelligence.ui.lifetime import release_top_level_widgets

        release_top_level_widgets(qt_app)
        qt_app.processEvents()
        controller.close()
    assert before == _database_fingerprint(workspace)


def test_report_audit_does_not_recompose_and_a_late_result_cannot_replace_a_newer_report(
    qt_app, workspace, tmp_path
) -> None:
    """Citation verification uses the loaded pack. A stale worker result is ignored."""
    from drilling_intelligence.reporting.html import render_html
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow
    from drilling_intelligence.ui.worker import ReportAuditWorker

    before = _database_fingerprint(workspace)
    controller = ReviewController()
    assert controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    window = MainWindow(controller)
    first = {
        "schema": "engineering-report/1",
        "identity": "report-first",
        "mode": "single_well",
        "title": "First",
        "subject": {"kind": "well", "id": "well-a", "name": "A"},
        "source_packs": [],
        "sections": [],
        "exhibits": [],
        "evidence": [],
        "limitations": [],
        "observations": [],
        "freshness": {},
    }
    second = dict(first)
    second["identity"] = "report-second"
    second["title"] = "Second"
    try:
        window.set_report(first)
        qt_app.processEvents()
        assert "not requested" in window.report_lineage.text()
        marker = object()
        window._report_audit_thread = marker
        window._request_report_audit()
        assert window._report_audit_thread is marker
        window._report_audit_thread = None
        window.report_verify_button.setEnabled(True)

        worker = ReportAuditWorker(controller, dict(first))
        received: list[Any] = []
        worker.succeeded.connect(received.append)
        worker.run()
        assert received
        assert received[0]["report_identity"] == "report-first"
        assert received[0]["overall"] != "FULLY_VERIFIED"
        window._report_audit_succeeded(received[0], window._report_audit_generation)
        assert window._report_audit["overall"] == received[0]["overall"]
        path = tmp_path / "audited.html"
        window.export_loaded_report(str(path))
        text = path.read_text(encoding="utf-8")
        assert text == render_html(first, audit=received[0])
        assert "Citation verification was requested" in text
        assert "report-first" in text
        assert "<script" not in text.lower()

        window.set_report(second)
        qt_app.processEvents()
        assert window._report_audit is None
        window._report_audit_succeeded(received[0], window._report_audit_generation - 1)
        assert window._report_audit is None
        assert "report-second" in window.report_header.text()
        assert "not requested" in window.report_lineage.text()
    finally:
        # Release, do not only close.  The application fixture is
        # session-scoped, so a window that is only closed stays alive until
        # the end of the session - and the workspace this window reads is
        # torn down before that.  The release path is the one the
        # application uses, so the test and the product share it.
        from drilling_intelligence.ui.lifetime import release_top_level_widgets

        release_top_level_widgets(qt_app)
        qt_app.processEvents()
        controller.close()
    assert before == _database_fingerprint(workspace)


def test_daily_ddr_workbench_creates_saves_reviews_reopens_and_compares_revisions(
    qt_app, workspace, committed_well_id, monkeypatch
) -> None:
    """The desktop exposes a writable draft composer, not just a DDR history table."""
    import time

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtTest import QSignalSpy

    from drilling_intelligence.database.models import WellOperation
    from drilling_intelligence.operations.ddr import DdrAuthoringService
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow

    controller = ReviewController()
    assert controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    window = MainWindow(controller)
    window.well_combo.blockSignals(True)
    window.well_combo.addItem("A-3", committed_well_id)
    window.well_combo.setCurrentIndex(0)
    window.well_combo.blockSignals(False)
    spy = QSignalSpy(window.ddr_operation_finished)

    def wait_for_idle(timeout_ms: int = 15_000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            qt_app.processEvents()
            if window._ddr_thread is None:
                qt_app.processEvents()
                if window._ddr_thread is None and not window._pending_ddr_refresh:
                    return True
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return False
            loop = QEventLoop()
            timer = QTimer()
            timer.setSingleShot(True)
            thread = window._ddr_thread
            if thread is not None:
                thread.finished.connect(loop.quit)
            window.ddr_operation_finished.connect(loop.quit)
            timer.timeout.connect(loop.quit)
            timer.start(remaining_ms)
            loop.exec()
            timer.stop()
            window.ddr_operation_finished.disconnect(loop.quit)

    def wait_for_review_idle(timeout_ms: int = 15_000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while window._review_thread is not None:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return False
            thread = window._review_thread
            loop = QEventLoop()
            timer = QTimer()
            timer.setSingleShot(True)
            thread.finished.connect(loop.quit)
            timer.timeout.connect(loop.quit)
            timer.start(remaining_ms)
            loop.exec()
            timer.stop()
        return True

    def wait_for_action_review_idle(timeout_ms: int = 15_000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while window._action_thread is not None or window._review_thread is not None:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return False
            thread = window._action_thread or window._review_thread
            assert thread is not None
            loop = QEventLoop()
            timer = QTimer()
            timer.setSingleShot(True)
            thread.finished.connect(loop.quit)
            timer.timeout.connect(loop.quit)
            timer.start(remaining_ms)
            loop.exec()
            timer.stop()
        return True

    def select_ddr_review_record(record_id: str) -> None:
        window.record_type_filter.setCurrentText("ddr_report")
        qt_app.processEvents()
        selected = None
        for row in range(window.records_proxy.rowCount()):
            index = window.records_proxy.index(row, 0)
            source = window.records_proxy.mapToSource(index)
            candidate = window.records_model.record_at(source.row())
            if candidate is not None and candidate.record_id == record_id:
                selected = index
                break
        assert selected is not None, f"DDR report {record_id} is absent from the reloaded review"
        window.records_table.setCurrentIndex(selected)
        qt_app.processEvents()
        assert window._selected_record is not None
        assert window._selected_record.record_id == record_id

    try:
        _select_navigation_page(window, "Daily DDR")
        assert wait_for_idle(), "initial DDR list did not finish before its bounded deadline"
        assert window.ddr_new_button.isEnabled()
        assert not window.ddr_create_button.isEnabled()

        window.ddr_new_button.click()
        assert window._ddr_new_draft_mode
        assert not window.ddr_date_input.isReadOnly()
        assert not window.ddr_summary_input.isReadOnly()
        assert window.ddr_add_activity_button.isEnabled()
        window.ddr_date_input.setText("2026-11-01")
        window.ddr_summary_input.setPlainText("Drilling continued as recorded by the operator.")
        window.ddr_actor_input.setText("operator-1")
        window.ddr_reason_input.setText("Manual daily report entry")
        window.ddr_add_activity_button.click()
        operation_type = window.ddr_activities_table.cellWidget(0, 0)
        assert operation_type is not None
        operation_type.setCurrentText(controller.ddr_operation_types()[0])
        window.ddr_activities_table.item(0, 1).setText("Drilling continued")
        window.ddr_activities_table.item(0, 2).setText("Operator-entered activity")
        window.ddr_activities_table.item(0, 3).setText("Night shift")
        assert window.ddr_create_button.isEnabled()
        window.ddr_create_button.click()
        assert wait_for_idle(), "DDR draft creation/save refresh exceeded its bounded deadline"

        service = DdrAuthoringService(controller.workspace.database)
        rows = service.list_reports(committed_well_id)
        assert len(rows) == 1
        report = rows[0]
        assert report["authoring_state"] == "DRAFT"
        assert report["status"] == "CANDIDATE"
        assert report["summary"] == "Drilling continued as recorded by the operator."
        assert report["activities"][0]["label"] == "Drilling continued"
        assert not window._ddr_new_draft_mode

        window.ddr_summary_input.setPlainText("Operator wording updated before submission.")
        window.ddr_reason_input.setText("Corrected the draft wording")
        assert window.ddr_save_button.isEnabled()
        window.ddr_save_button.click()
        assert wait_for_idle(), "DDR draft save/refresh exceeded its bounded deadline"
        assert "failed" not in window.status_label.text().lower(), window.status_label.text()
        assert (
            service.get(report["report_id"])["summary"]
            == "Operator wording updated before submission."
        )

        window.ddr_reason_input.setText("Ready for independent review")
        window.ddr_submit_button.click()
        assert wait_for_idle(), "DDR submit/refresh exceeded its bounded deadline"
        assert wait_for_review_idle(), "DDR submission review reload exceeded its bounded deadline"
        submitted = service.get(report["report_id"])
        assert submitted["authoring_state"] == "SUBMITTED"
        assert submitted["status"] == "CANDIDATE"
        with controller.workspace.database.read_only() as session:
            operations = list(session.query(WellOperation).filter_by(report_id=report["report_id"]))
        assert len(operations) == 1
        assert operations[0].status == "CANDIDATE"
        assert operations[0].provenance[0]["source_kind"] == "manual_authoring"
        assert submitted["provenance"]
        assert submitted["provenance"][0]["actor"] == "operator-1"

        # Revisions are distinct persisted rows. The UI exposes the parent comparison and keeps
        # authored actor/reason/time beside exact field values instead of deriving engineering data.
        assert window.ddr_revision_button.isEnabled()
        window.ddr_reason_input.setText("Open a corrected revision after review")
        window.ddr_revision_button.click()
        assert wait_for_idle(), "DDR revision creation/list refresh exceeded its bounded deadline"
        assert window._selected_ddr_record is not None
        revision_id = window._selected_ddr_record["report_id"]
        assert revision_id != report["report_id"]
        assert window._selected_ddr_record["supersedes_report_id"] == report["report_id"]
        window.ddr_add_activity_button.click()
        inserted_row = window.ddr_activities_table.rowCount() - 1
        inserted_type = window.ddr_activities_table.cellWidget(inserted_row, 0)
        assert inserted_type is not None
        inserted_type.setCurrentText(controller.ddr_operation_types()[0])
        window.ddr_activities_table.item(inserted_row, 1).setText("Revision-only activity")
        window.ddr_activities_table.item(inserted_row, 2).setText("Exact inserted operator text")
        window.ddr_activities_table.item(inserted_row, 3).setText("Changed shift")
        window.ddr_summary_input.setPlainText("Operator wording updated in revision two.")
        window.ddr_reason_input.setText("Corrected the submitted summary")
        window.ddr_save_button.click()
        assert wait_for_idle(), "DDR revision save/list refresh exceeded its bounded deadline"
        window.ddr_reason_input.setText("Corrected the submitted summary")
        window.ddr_submit_button.click()
        assert wait_for_idle(), "DDR revision submission/list refresh exceeded its bounded deadline"
        assert wait_for_review_idle(), "DDR revision review reload exceeded its bounded deadline"
        revised = service.get(revision_id)
        assert revised["revision_number"] == 2
        assert revised["summary"] == "Operator wording updated in revision two."
        assert any(
            item.get("reason") == "Corrected the submitted summary"
            and item.get("action") == "saved"
            for item in revised["provenance"]
        )
        assert not service.get(report["report_id"])["is_current"]
        assert window.ddr_compare_button.isEnabled()
        # A lineage reference with an unavailable history row remains inspectable and is reported,
        # rather than silently hiding the comparison control or guessing a predecessor.
        parent_record = window._ddr_records_by_id.pop(report["report_id"])
        assert window.ddr_compare_button.isEnabled()
        window.ddr_compare_button.click()
        assert "not in loaded history" in window.ddr_compare_tree.topLevelItem(0).text(1)
        window._ddr_records_by_id[report["report_id"]] = parent_record
        before_compare = _database_fingerprint(controller.workspace)
        window.ddr_compare_button.click()
        assert window.ddr_compare_tree.topLevelItemCount() == 1
        compared = window.ddr_compare_tree.topLevelItem(0)
        labels = []
        pending = [compared]
        while pending:
            item = pending.pop()
            labels.extend((item.text(0), item.text(1)))
            pending.extend(item.child(i) for i in range(item.childCount()))
        assert "summary" in labels and "modified" in labels
        assert "report_date_text" in labels
        assert "Read-only revision comparison — not an approval" in labels
        assert "Activities (authored order; no calculation)" in labels
        assert "Authored positions" in labels
        assert "Authored provenance (declared metadata; not authentication)" in labels
        assert "Corrected the submitted summary" in labels
        assert "Revision-only activity" in labels
        assert "added" in labels
        assert _database_fingerprint(controller.workspace) == before_compare

        # The review UI enforces reason and a distinct declared actor before confirmation. The
        # affirmative action is still only a declared actor string, not an authenticated identity.
        _select_navigation_page(window, "Records")
        select_ddr_review_record(revision_id)
        assert window._current_record_action() is not None
        assert window._current_record_action().action_id == "confirm"
        window.record_actor_input.setText("operator-1")
        window.record_reason_input.setText("This is not an independent review")
        window.confirm_record_action()
        assert "different from the record author" in window.status_label.text()
        assert window._action_thread is None
        window.record_actor_input.setText("reviewer-2")
        window.record_reason_input.clear()
        window.confirm_record_action()
        assert "needs a reason" in window.status_label.text()
        assert window._action_thread is None

        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
        )
        window.record_reason_input.setText("Reviewed the submitted values against the report")
        window.confirm_record_action()
        assert wait_for_action_review_idle(), "DDR confirmation/review reload exceeded its deadline"
        assert service.get(revision_id)["status"] == "CONFIRMED"
        assert any(
            item.get("actor") == "reviewer-2"
            and item.get("reason") == "Reviewed the submitted values against the report"
            for item in service.get(revision_id)["provenance"]
        )

        _select_navigation_page(window, "Daily DDR")
        assert wait_for_idle(), "DDR list did not refresh after confirmation"
        window.ddr_compare_button.click()
        provenance_labels = []
        pending = [window.ddr_compare_tree.topLevelItem(0)]
        while pending:
            item = pending.pop()
            if item is not None:
                provenance_labels.extend((item.text(0), item.text(1)))
                pending.extend(item.child(i) for i in range(item.childCount()))
        assert "reviewer-2" in provenance_labels
        assert "Reviewed the submitted values against the report" in provenance_labels
        window.ddr_reason_input.setText("Create a new draft for the rejected revision case")
        assert window.ddr_revision_button.isEnabled()
        window.ddr_revision_button.click()
        assert wait_for_idle(), "third DDR revision creation exceeded its deadline"
        assert window._selected_ddr_record is not None
        rejected_revision_id = window._selected_ddr_record["report_id"]
        assert window._selected_ddr_record["revision_number"] == 3
        window.ddr_summary_input.setPlainText("Revision three with a documented correction.")
        window.ddr_reason_input.setText("Record a correction for review")
        window.ddr_save_button.click()
        assert wait_for_idle(), "third DDR revision save exceeded its deadline"
        window.ddr_reason_input.setText("Submit the third revision for review")
        window.ddr_submit_button.click()
        assert wait_for_idle(), "third DDR revision submission exceeded its deadline"
        assert wait_for_review_idle(), "third DDR review reload exceeded its deadline"

        _select_navigation_page(window, "Records")
        select_ddr_review_record(rejected_revision_id)
        reject_index = window.record_action_combo.findData("reject")
        assert reject_index >= 0
        window.record_action_combo.setCurrentIndex(reject_index)
        window.record_actor_input.setText("reviewer-3")
        window.record_reason_input.setText("Rejected because the correction needs another review")
        window.confirm_record_action()
        assert wait_for_action_review_idle(), "DDR rejection/review reload exceeded its deadline"
        rejected = service.get(rejected_revision_id)
        assert rejected["status"] == "REJECTED"
        assert any(
            item.get("actor") == "reviewer-3"
            and item.get("reason") == "Rejected because the correction needs another review"
            for item in rejected["provenance"]
        )

        _select_navigation_page(window, "Overview")
        _select_navigation_page(window, "Daily DDR")
        assert wait_for_idle(), "DDR persisted list did not reopen within its bounded deadline"
        assert len(service.list_reports(committed_well_id)) == 3
        assert spy.count() >= 11
    finally:
        from drilling_intelligence.ui.lifetime import release_top_level_widgets

        release_top_level_widgets(qt_app)
        qt_app.processEvents()
        controller.close()
