"""Offscreen tests for the optional read-only desktop Review Workbench."""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import inspect

pytestmark = pytest.mark.ui


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


@pytest.fixture(scope="session")
def qt_app():
    """Use real Qt offscreen; skip with the dynamic-linker reason when the host lacks Qt libs."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except (ImportError, OSError) as exc:
        pytest.skip(f"QtWidgets unavailable for UI tests: {exc}")
    application = QApplication.instance() or QApplication(["pytest-review-workbench"])
    yield application
    application.quit()


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
        window.close()
        qt_app.processEvents()

    assert before == _database_fingerprint(workspace)


def test_presentation_filter_conflict_detail_and_explicit_error_states(qt_app) -> None:
    """Filtering is client-side, conflict detail is DTO-backed, and errors stay distinguishable."""
    from drilling_intelligence.review import ReviewConflict, ReviewRecord, ReviewVerification
    from drilling_intelligence.ui.main_window import MainWindow
    from drilling_intelligence.ui.worker import WorkerError

    window = MainWindow()
    try:
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
        window.close()
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
