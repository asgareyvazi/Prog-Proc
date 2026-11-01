"""Offscreen Qt coverage for the persistent operator intake inbox."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui


def test_intake_page_uses_worker_and_renders_persisted_queue(qt_app, tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtTest import QSignalSpy

    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow
    from drilling_intelligence.wells.workspace import Workspace

    root = tmp_path / "workspace"
    from drilling_intelligence.cli.app import main

    assert main(["workspace", "create", str(root)]) == 0
    workspace = Workspace.open(root)
    _ = workspace.database
    source = tmp_path / "handover.txt"
    source.write_text("Shift handover\nDrilling continued", encoding="utf-8")
    controller = ReviewController()
    controller.open_workspace(root)
    window = MainWindow(controller)
    try:
        intake_rows = [
            row
            for row in range(window.navigation.count())
            if window.navigation.item(row).text() == "Intake"
        ]
        assert len(intake_rows) == 1
        window.navigation.setCurrentRow(intake_rows[0])
        assert window.pages.currentIndex() == intake_rows[0]
        queued = controller.enqueue_intake([source], actor="operator")
        assert queued[0].status == "QUEUED"
        spy = QSignalSpy(window.intake_operation_finished)
        window._start_intake_operation("list")
        assert spy.wait(15000), (
            "intake list worker did not finish before its bounded 15-second deadline; "
            f"status={window.status_label.text()!r}, "
            f"thread_running={window._intake_thread.isRunning() if window._intake_thread else False}"
        )
        assert window.intake_table.rowCount() == 1
        assert window.intake_table.item(0, 0).text() == source.name

        spy = QSignalSpy(window.intake_operation_finished)
        window._start_intake_operation("preview", item_id=queued[0].id)
        assert spy.wait(10000), (
            "local extraction preview did not finish before its bounded deadline"
        )
        assert window.intake_preview_tree.topLevelItemCount() > 0
        assert "no document or domain row was written" in window.status_label.text()
    finally:
        window.close()
        qt_app.processEvents()
        workspace.close()
