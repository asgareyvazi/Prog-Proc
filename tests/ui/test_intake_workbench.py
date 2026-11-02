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
