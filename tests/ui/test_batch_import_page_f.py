"""Phase F (2026-11-24B): Qt batch page — durable outcome.json, real reopen/history,
safe thread shutdown."""

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


def test_batch_page_durable_outcome_and_reopen_history(qt_app, workspace, tmp_path) -> None:
    from drilling_intelligence.ui.controller import ReviewController

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Batch DDR Import")
        page = window.batch_import_page

        # Simulate a finished batch: staging dir with manifest + durable outcome.
        staging = workspace.root / ".drillintel" / "batch_imports" / "testbatch01"
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "manifest.json").write_text(
            json.dumps({"format": "ddr-batch-manifest/v1", "files": [{"path": "a.pdf"}]})
        )
        payload = {
            "total": 1,
            "imported": 0,
            "failed": 1,
            "entries": [
                {
                    "filename": "a.pdf",
                    "status": "FAILED",
                    "classification": None,
                    "pending_review": False,
                    "error": "boom",
                }
            ],
        }
        page._write_durable_outcome(staging, payload)
        outcome = staging / "outcome.json"
        assert outcome.is_file()
        assert json.loads(outcome.read_text())["failed"] == 1
        # no temp leftovers
        assert list(staging.glob("*.tmp")) == []

        page._reload_history()
        assert page.history_combo.count() >= 1
        page._reopen_selected_batch()
        assert page.results_table.rowCount() == 1
        assert page.results_table.item(0, 1).text() == "FAILED"
        assert page.retry_button.isEnabled()
        assert "Reopened batch" in page.status_label.text()
    finally:
        window.close()


def test_batch_page_reopen_without_outcome_is_refused(qt_app, workspace) -> None:
    from drilling_intelligence.ui.controller import ReviewController

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Batch DDR Import")
        page = window.batch_import_page
        staging = workspace.root / ".drillintel" / "batch_imports" / "nothere"
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "manifest.json").write_text("{}")
        page._reload_history()
        idx = page.history_combo.findText("nothere (no outcome)")
        assert idx >= 0
        page.history_combo.setCurrentIndex(idx)
        page._reopen_selected_batch()
        assert "no persisted outcome" in page.status_label.text()
    finally:
        window.close()


def test_batch_page_close_shuts_thread_down_safely(qt_app, workspace) -> None:
    from drilling_intelligence.ui.controller import ReviewController

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Batch DDR Import")
        page = window.batch_import_page
        page._cleanup_thread()  # no thread: must be a no-op, not an error
        window.close()
        assert page._thread is None
    finally:
        pass


def test_survey_page_shows_all_three_unit_columns(qt_app, workspace) -> None:
    """Phase C: MD, inclination and azimuth units are three separate visible columns."""
    from drilling_intelligence.ui.controller import ReviewController

    controller = ReviewController()
    window = _open_window(controller, workspace)
    try:
        _select_navigation_page(window, "Survey Import")
        page = window.survey_import_page
        headers = [
            page.station_table.horizontalHeaderItem(c).text()
            for c in range(page.station_table.columnCount())
        ]
        assert "MD unit" in headers and "Incl unit" in headers and "Az unit" in headers
    finally:
        window.close()
