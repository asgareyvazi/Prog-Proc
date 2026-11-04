"""Offscreen Qt coverage for the persistent operator intake inbox."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui


def test_intake_page_uses_worker_and_renders_persisted_queue(qt_app, tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtTest import QSignalSpy

    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow
    from drilling_intelligence.wells.workspace import Workspace

    def wait_for_signal(spy: QSignalSpy, signal, timeout_ms: int) -> bool:
        if spy.count():
            return True
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        signal.connect(loop.quit)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        loop.exec()
        timer.stop()
        signal.disconnect(loop.quit)
        return bool(spy.count())

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
        assert wait_for_signal(spy, window.intake_operation_finished, 15000), (
            "intake list worker did not finish before its bounded 15-second deadline; "
            f"status={window.status_label.text()!r}, "
            f"thread_running={window._intake_thread.isRunning() if window._intake_thread else False}"
        )
        assert window._intake_thread is None
        assert window._intake_worker is None
        assert window.intake_table.rowCount() == 1
        assert window.intake_table.item(0, 0).text() == source.name

        spy = QSignalSpy(window.intake_operation_finished)
        window._start_intake_operation("preview", item_id=queued[0].id)
        assert wait_for_signal(spy, window.intake_operation_finished, 10000), (
            "local extraction preview did not finish before its bounded deadline"
        )
        assert window._intake_thread is None
        assert window._intake_worker is None
        assert window.intake_preview_tree.topLevelItemCount() > 0
        assert "no document or domain row was written" in window.status_label.text()

        for _ in range(5):
            spy = QSignalSpy(window.intake_operation_finished)
            window._start_intake_operation("list")
            assert wait_for_signal(spy, window.intake_operation_finished, 15000), (
                "consecutive intake worker did not reach its complete lifecycle"
            )
            assert window._intake_thread is None
            assert window._intake_worker is None
    finally:
        window.close()
        qt_app.processEvents()
        workspace.close()


def test_intake_window_close_joins_worker_without_late_ui_callback(qt_app, tmp_path: Path) -> None:
    """A worker can finish while close waits without racing QObject cleanup."""
    from PySide6.QtTest import QSignalSpy

    from drilling_intelligence.cli.app import main
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow

    root = tmp_path / "workspace"
    assert main(["workspace", "create", str(root)]) == 0
    controller = ReviewController()
    controller.open_workspace(root)
    operation_started = threading.Event()
    release_operation = threading.Event()
    close_entered = threading.Event()
    original_list = controller.intake_items

    def blocked_list(*, status: str = ""):
        operation_started.set()
        if not release_operation.wait(5):
            raise TimeoutError("test release was not signalled before the bounded deadline")
        return original_list(status=status)

    controller.intake_items = blocked_list

    class ClosingWindow(MainWindow):
        def closeEvent(self, event) -> None:
            close_entered.set()
            super().closeEvent(event)

    window = ClosingWindow(controller)
    window.show()
    finished = QSignalSpy(window.intake_operation_finished)
    window._start_intake_operation("list")
    assert operation_started.wait(5), "the intake worker did not enter its operation"

    def release_after_close_starts() -> None:
        if close_entered.wait(5):
            release_operation.set()

    releaser = threading.Thread(target=release_after_close_starts, daemon=True)
    releaser.start()
    try:
        assert window.close(), "close should be accepted after the worker exits"
        releaser.join(5)
        assert not releaser.is_alive(), "the bounded close coordinator did not finish"
        qt_app.processEvents()
        assert not window.owned_workers_running()
        assert window._intake_thread is None
        assert window._intake_worker is None
        assert finished.count() == 0, "a stale worker callback updated the closed intake page"
    finally:
        release_operation.set()
        releaser.join(5)
        if window.isVisible():
            window.close()
        qt_app.processEvents()
        controller.close()


def test_intake_worker_exception_still_destroys_worker_before_completion(
    qt_app, tmp_path: Path
) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtTest import QSignalSpy

    from drilling_intelligence.cli.app import main
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow

    root = tmp_path / "workspace"
    assert main(["workspace", "create", str(root)]) == 0
    controller = ReviewController()
    controller.open_workspace(root)

    def fail_list(*, status: str = ""):
        raise RuntimeError(f"synthetic intake failure ({status})")

    controller.intake_items = fail_list
    window = MainWindow(controller)
    finished = QSignalSpy(window.intake_operation_finished)
    try:
        window._start_intake_operation("list")
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        window.intake_operation_finished.connect(loop.quit)
        timer.timeout.connect(loop.quit)
        timer.start(10000)
        if not finished.count():
            loop.exec()
        timer.stop()
        window.intake_operation_finished.disconnect(loop.quit)
        assert finished.count() == 1, "failed worker did not reach bounded cleanup completion"
        assert "Intake list failed: synthetic intake failure" in window.status_label.text()
        assert window._intake_thread is None
        assert window._intake_worker is None
        assert not window._worker_destroyed_events
    finally:
        from drilling_intelligence.ui.lifetime import release_top_level_widgets

        release_top_level_widgets(qt_app)
        qt_app.processEvents()
        controller.close()


def test_intake_and_ddr_workers_can_finish_concurrently(qt_app, tmp_path: Path) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtTest import QSignalSpy

    from drilling_intelligence.cli.app import main
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow

    root = tmp_path / "workspace"
    assert main(["workspace", "create", str(root)]) == 0
    controller = ReviewController()
    controller.open_workspace(root)
    window = MainWindow(controller)
    intake_finished = QSignalSpy(window.intake_operation_finished)
    ddr_finished = QSignalSpy(window.ddr_operation_finished)

    def wait_for(spy: QSignalSpy, signal) -> bool:
        if spy.count():
            return True
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        signal.connect(loop.quit)
        timer.timeout.connect(loop.quit)
        timer.start(10000)
        loop.exec()
        timer.stop()
        signal.disconnect(loop.quit)
        return bool(spy.count())

    try:
        window._start_intake_operation("list")
        window._start_ddr_operation("list", well_id="unregistered-well", include_history=True)
        assert wait_for(intake_finished, window.intake_operation_finished)
        assert wait_for(ddr_finished, window.ddr_operation_finished)
        assert window._intake_thread is None
        assert window._intake_worker is None
        assert window._ddr_thread is None
        assert window._ddr_worker is None
        assert not window._worker_destroyed_events
    finally:
        from drilling_intelligence.ui.lifetime import release_top_level_widgets

        release_top_level_widgets(qt_app)
        qt_app.processEvents()
        controller.close()


def test_intake_review_registration_and_rejection_run_as_distinct_ui_transitions(
    qt_app, tmp_path: Path
) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtTest import QSignalSpy

    from drilling_intelligence.cli.app import main
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import MainWindow

    root = tmp_path / "workspace"
    assert main(["workspace", "create", str(root)]) == 0
    accepted_source = tmp_path / "accepted.txt"
    accepted_source.write_text("Daily operations handover", encoding="utf-8")
    rejected_source = tmp_path / "rejected.txt"
    rejected_source.write_text("Unrelated notes", encoding="utf-8")
    controller = ReviewController()
    controller.open_workspace(root)
    window = MainWindow(controller)
    window.intake_actor_input.setText("operator")
    window.intake_reason_input.setText("reviewed against the source")
    finished = QSignalSpy(window.intake_operation_finished)

    def wait_for_count(target: int) -> bool:
        if finished.count() >= target:
            return True
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        window.intake_operation_finished.connect(loop.quit)
        timer.timeout.connect(loop.quit)
        timer.start(10000)
        while finished.count() < target and timer.isActive():
            loop.exec()
        timer.stop()
        window.intake_operation_finished.disconnect(loop.quit)
        return finished.count() >= target

    def lifecycle_snapshot() -> str:
        thread = window._intake_thread
        worker = window._intake_worker
        destroyed = window._worker_destroyed_events.get("_intake_thread")
        if thread is None:
            thread_state = "none"
        else:
            thread_state = f"running={thread.isRunning()}, finished={thread.isFinished()}"
        return (
            f"completed={finished.count()}, active={window._active_intake_operation!r}, "
            f"status={window.status_label.text()!r}, "
            f"pending_refresh={window._pending_intake_refresh}, "
            f"thread={thread_state}, worker={type(worker).__name__ if worker else None}, "
            f"worker_destroyed={destroyed.is_set() if destroyed else None}, "
            f"queue_statuses={window._intake_status_by_id!r}"
        )

    def row_for(filename: str) -> int:
        for row in range(window.intake_table.rowCount()):
            if window.intake_table.item(row, 0).text() == filename:
                return row
        return -1

    try:
        window._start_intake_operation("enqueue", paths=[accepted_source], actor="operator")
        assert wait_for_count(2), (
            f"enqueue and its pending queue refresh did not both finish; {lifecycle_snapshot()}"
        )
        accepted_row = row_for(accepted_source.name)
        assert accepted_row >= 0
        window.intake_table.selectRow(accepted_row)
        accepted_id = window._selected_intake_item()
        assert accepted_id
        assert window.intake_review_button.isEnabled()
        assert not window.intake_register_button.isEnabled()

        next_count = finished.count() + 1
        window._preview_selected_intake()
        assert wait_for_count(next_count), "preview did not finish"
        assert window.intake_preview_tree.topLevelItemCount() > 0
        assert "no document or domain row was written" in window.status_label.text()

        next_count = finished.count() + 2
        window._mark_intake_reviewed()
        assert wait_for_count(next_count), "review disposition and pending refresh did not finish"
        assert window._intake_status_by_id[accepted_id] == "REVIEWED"
        accepted_row = row_for(accepted_source.name)
        window.intake_table.selectRow(accepted_row)
        assert window.intake_register_button.isEnabled()

        next_count = finished.count() + 2
        window._register_selected_intake()
        assert wait_for_count(next_count), "registration and pending refresh did not finish"
        assert window._intake_status_by_id[accepted_id] == "REGISTERED"
        registered = controller.intake_items(status="REGISTERED")
        assert len(registered) == 1
        assert registered[0].id == accepted_id
        assert registered[0].document_id
        assert registered[0].document_version_id

        next_count = finished.count() + 2
        window._start_intake_operation("enqueue", paths=[rejected_source], actor="operator")
        assert wait_for_count(next_count), "second enqueue and pending refresh did not finish"
        rejected_row = row_for(rejected_source.name)
        assert rejected_row >= 0
        window.intake_table.selectRow(rejected_row)
        rejected_id = window._selected_intake_item()
        assert rejected_id and rejected_id != accepted_id
        assert window.intake_reject_button.isEnabled()

        next_count = finished.count() + 2
        window._reject_selected_intake()
        assert wait_for_count(next_count), (
            f"rejection and pending refresh did not finish; {lifecycle_snapshot()}"
        )
        assert window._intake_status_by_id[rejected_id] == "REJECTED"
        rejected_row = row_for(rejected_source.name)
        window.intake_table.selectRow(rejected_row)
        assert not window.intake_register_button.isEnabled()
        assert [item.id for item in controller.intake_items(status="REGISTERED")] == [accepted_id]
    finally:
        window.close()
        qt_app.processEvents()
        controller.close()


def test_stale_worker_callbacks_and_new_requests_are_noops_after_close(qt_app) -> None:
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import _WORKER_THREAD_ATTRS, MainWindow

    window = MainWindow(ReviewController())
    assert window.close()
    qt_app.processEvents()
    before = window.status_label.text()
    callbacks = (
        (window._review_succeeded, (object(),)),
        (window._review_failed, (object(),)),
        (window._decision_succeeded, (object(),)),
        (window._decision_failed, (object(),)),
        (window._comparison_succeeded, (object(),)),
        (window._comparison_failed, (object(),)),
        (window._report_succeeded, (object(),)),
        (window._report_failed, (object(),)),
        (window._report_audit_succeeded, (object(), 0)),
        (window._report_audit_failed, (object(), 0)),
        (window._calculation_succeeded, (object(),)),
        (window._calculation_failed, (object(),)),
        (window._action_succeeded, (object(),)),
        (window._action_failed, (object(),)),
        (window._intake_succeeded, ("list", object())),
        (window._intake_failed, ("list", object())),
        (window._ddr_succeeded, ("list", object())),
        (window._ddr_failed, ("list", object())),
    )
    for callback, args in callbacks:
        callback(*args)
    window.request_review(verify_citations=False)
    window._request_decision("stale-well")
    window._request_comparison(["well-a", "well-b"])
    window._request_report()
    window._request_report_audit()
    window.run_calculation()
    window._start_action_worker(object())
    window._start_intake_operation("list")
    window._start_ddr_operation("list", well_id="stale-well")
    assert window.status_label.text() == before
    assert all(getattr(window, name) is None for name in _WORKER_THREAD_ATTRS)
    assert not window._worker_destroyed_events
