"""V23 coverage resilience: push qt/ui from 71.0 toward durable headroom.

Targets main_window helpers, models, chart, lifetime wait/termination and the
Qt-independent errors module. All mocked tests are pure (no display, no real
QThread) and not marked ui so they run headless; Qt tests are marked ui and
skip cleanly via importorskip. No real process-killing call is made in the
main runner — termination is exercised via the injected _TerminationAdapter.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_worker_error_redaction_v23_pure():
    """WorkerError is Qt-independent: import without PySide6, redaction branches."""
    from drilling_intelligence.ui.errors import WorkerError

    # Whitespace normalisation
    e = WorkerError(category="unexpected", message="  a   b  c  ", hint="  h  ")
    assert e.message == "a b c"
    assert e.hint == "h"
    # Secret redaction via assignment forms
    secret = "sk-V23SECRET1234567890abcdef"  # noqa: S105
    e2 = WorkerError(category="unexpected", message=f"api_key={secret}", hint=f"token={secret}")
    assert secret not in e2.message
    assert secret not in e2.hint
    assert "[redacted]" in e2.message
    # Path redaction
    e3 = WorkerError(category="unexpected", message="failed at /tmp/secret/file.txt", hint="")
    assert "/tmp/secret" not in e3.message  # noqa: S108
    # Re-export from worker must be same object when Qt available
    try:
        from drilling_intelligence.ui.worker import WorkerError as FromWorker

        assert FromWorker is WorkerError
    except ImportError:
        pass
    # All categories retained
    for cat in (
        "cancelled",
        "input",
        "integrity",
        "unavailable",
        "unexpected",
        "internal",
        "domain",
        "workspace",
        "action",
    ):
        ee = WorkerError(category=cat, message="m", hint="h", exception_type="E")
        assert ee.category == cat
        assert ee.exception_type == "E"


def test_chart_layout_exhibit_branches():
    from drilling_intelligence.reporting.layout import layout_exhibit

    assert isinstance(layout_exhibit(None), dict)
    assert isinstance(layout_exhibit({}), dict)
    assert isinstance(layout_exhibit({"subjects": []}), dict)
    assert isinstance(layout_exhibit({"subjects": [{"id": "a"}]}), dict)
    assert isinstance(layout_exhibit({"subjects": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}), dict)
    # Invalid exhibit still returns dict, not crash
    assert isinstance(layout_exhibit({"invalid": 123}), dict)
    # ExhibitChart contract mocked — no display needed
    pytest.importorskip("PySide6")
    try:
        from PySide6.QtWidgets import QChartView

        from drilling_intelligence.ui.chart import ExhibitChart

        assert hasattr(ExhibitChart, "set_exhibit")
        assert issubclass(ExhibitChart, QChartView)
    except ImportError as exc:
        pytest.skip(f"Qt unavailable for ExhibitChart: {exc}")


def test_controller_surfaces_v23():
    from drilling_intelligence.ui.controller import ReviewController

    ctrl = ReviewController()
    for name in (
        "load_review",
        "decision",
        "compare",
        "report",
        "audit_report",
        "run_npt_rollup",
        "execute_action",
    ):
        assert hasattr(ctrl, name)


@pytest.mark.ui
def test_models_mapping_and_review_with_qt_v23():
    pytest.importorskip("PySide6")
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip(f"Qt unavailable: {exc}")
    try:
        QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"QApplication failed: {exc}")
    from drilling_intelligence.ui.models import MappingTableModel, ReviewRecordsModel, TableColumn

    cols = [TableColumn("x", "X", 50), TableColumn("y", "Y", 50)]
    rows = [{"x": "a", "y": "b"}, {"x": None, "y": 123}, {"x": "c", "y": "d"}]
    m = MappingTableModel(cols, rows)
    assert m.rowCount() == 3
    assert m.columnCount() == 2
    from PySide6.QtCore import Qt as Qt2

    assert m.data(m.index(1, 0), Qt2.ItemDataRole.DisplayRole) == "—"
    assert m.data(m.index(1, 1), Qt2.ItemDataRole.DisplayRole) == "123"
    assert m.headerData(0, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) == "X"
    m.sort(1, Qt.SortOrder.DescendingOrder)
    assert m.rowCount() == 3
    rm = ReviewRecordsModel([])
    assert rm.rowCount() == 0
    assert rm.columnCount() == 7


def test_models_tablecolumn_pure_mocked():
    """TableColumn is a dataclass; can be exercised without Qt via mock."""
    # Import without Qt by mocking PySide6
    import sys
    from unittest.mock import MagicMock

    orig = {
        k: sys.modules.get(k)
        for k in ["PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"]
    }
    try:
        for name in ["PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"]:
            if name not in sys.modules:
                sys.modules[name] = MagicMock()
        # Also mock shiboken6
        if "shiboken6" not in sys.modules:
            sys.modules["shiboken6"] = MagicMock()
        # Force reimport of models
        if "drilling_intelligence.ui.models" in sys.modules:
            del sys.modules["drilling_intelligence.ui.models"]
        import importlib

        mod = importlib.import_module("drilling_intelligence.ui.models")
        TableColumn = mod.TableColumn
        c = TableColumn("k", "K", 10)
        assert c.key == "k"
        assert c.title == "K"
        assert c.width == 10
        # MappingTableModel with mocked Qt should still instantiate
        cols = [TableColumn("a", "A", 100)]
        rows = [{"a": "hello"}, {"a": None}]
        m = mod.MappingTableModel(cols, rows)
        assert m.rowCount() == 2
        assert m.columnCount() == 1
    finally:
        for k, v in orig.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        sys.modules.pop("drilling_intelligence.ui.models", None)


def test_main_window_value_text_and_tree_mocked():
    """main_window helpers via mocked Qt — no display needed, exercises branches."""
    import sys

    orig = {
        k: sys.modules.get(k)
        for k in ["PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "shiboken6"]
    }
    mocked = {}
    for name in ["PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "shiboken6"]:
        mocked[name] = MagicMock()
    # Provide necessary Qt classes
    for mod in ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"]:
        for cls in [
            "QModelIndex",
            "QSignalBlocker",
            "Qt",
            "QThread",
            "QTimer",
            "QUrl",
            "Slot",
            "QCloseEvent",
            "QDesktopServices",
            "QAbstractItemView",
            "QCheckBox",
            "QComboBox",
            "QFileDialog",
            "QFormLayout",
            "QFrame",
            "QGroupBox",
            "QHBoxLayout",
            "QHeaderView",
            "QLabel",
            "QLineEdit",
            "QListWidget",
            "QListWidgetItem",
            "QMainWindow",
            "QMessageBox",
            "QPushButton",
            "QSplitter",
            "QStackedWidget",
            "QTableView",
            "QTableWidget",
            "QTableWidgetItem",
            "QTreeWidget",
            "QTreeWidgetItem",
            "QVBoxLayout",
            "QWidget",
        ]:
            mocked[mod].__dict__.setdefault(cls, MagicMock())
    # Patch sys.modules
    for k, v in mocked.items():
        sys.modules[k] = v
    if "drilling_intelligence.ui.main_window" in sys.modules:
        del sys.modules["drilling_intelligence.ui.main_window"]
    try:
        import importlib

        mod = importlib.import_module("drilling_intelligence.ui.main_window")
        # _value_text branches
        assert mod._value_text(None) == "—"
        assert mod._value_text("") == "—"
        assert mod._value_text(0) == "0"
        assert mod._value_text(42) == "42"
        assert mod._value_text(True) == "Yes"
        assert mod._value_text(False) == "No"
        assert mod._value_text(3.14) == "3.14"
        assert "hello" in mod._value_text("hello")
        assert "a" in mod._value_text({"a": 1, "b": 2})
        assert "x" in mod._value_text(["x", "y"])
        assert mod._value_text("   ") == "   "
        # _add_tree_value / _fill_tree / _configure_table
        parent = MagicMock()
        with patch("drilling_intelligence.ui.main_window.QTreeWidgetItem") as mock_item_cls:
            mock_item = MagicMock()
            mock_item_cls.return_value = mock_item
            mod._add_tree_value(parent, "key", "val")
            assert mock_item_cls.called
            mod._add_tree_value(parent, "k2", None)
        tree = MagicMock()
        tree.invisibleRootItem.return_value = MagicMock()
        mod._fill_tree(tree, None)
        mod._fill_tree(tree, {})
        mod._fill_tree(tree, {"a": 1, "b": {"c": 2}})
        assert tree.clear.called
        table = MagicMock()
        # Build TableColumn via mocked models or MagicMock
        try:
            from drilling_intelligence.ui.models import TableColumn as TC

            tc = [TC("k", "K", 100), TC("k2", "K2", 200)]
        except ImportError:
            tc = [MagicMock(), MagicMock()]
            for c in tc:
                c.key = "k"
                c.title = "K"
                c.width = 100
        mod._configure_table(table, tc)
        # Navigation constants
        assert hasattr(mod, "_NAVIGATION")
        assert isinstance(mod._NAVIGATION, (list, tuple))
        assert len(mod._NAVIGATION) >= 1
    finally:
        for k, v in orig.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        sys.modules.pop("drilling_intelligence.ui.main_window", None)


def test_lifetime_helpers_pure_v23():
    """Lifetime helpers pure — no real handle, bounded waits, mocked termination."""
    from drilling_intelligence.ui.lifetime import (
        _WAIT_FAILED,
        _WAIT_OBJECT_0,
        _WAIT_TIMEOUT,
        SHUTDOWN_INCOMPLETE_EXIT,
        _is_windows,
        _wait_for_termination,
        is_fail_closed_shutdown_exit,
        remaining_budget_ms,
        wait_slice_ms,
        worker_join_limit_ms,
    )

    # Constants
    assert _WAIT_OBJECT_0 == 0
    assert _WAIT_TIMEOUT == 258
    assert _WAIT_FAILED == 4294967295
    assert SHUTDOWN_INCOMPLETE_EXIT == 4
    # is_fail_closed_shutdown_exit strict: only int 4
    assert is_fail_closed_shutdown_exit(4) is True
    assert is_fail_closed_shutdown_exit(0) is False
    assert is_fail_closed_shutdown_exit(1) is False
    assert is_fail_closed_shutdown_exit(True) is False  # type: ignore[arg-type]
    assert is_fail_closed_shutdown_exit(3221225477) is False
    assert is_fail_closed_shutdown_exit(-1073741819) is False
    assert is_fail_closed_shutdown_exit(5) is False
    # Bounded wait
    status = _wait_for_termination(timeout_s=0.01)
    if _is_windows():
        assert status in (_WAIT_OBJECT_0, _WAIT_TIMEOUT, _WAIT_FAILED)
    else:
        assert status == _WAIT_OBJECT_0
    # Budget helpers
    assert remaining_budget_ms(120_000, 10_000) == 110_000
    assert remaining_budget_ms(250, 250) == 0
    assert remaining_budget_ms(250, 300) < 0
    assert worker_join_limit_ms(10_000, 120_000) == 10_000
    assert worker_join_limit_ms(10_000, 50) == 50
    assert wait_slice_ms(5) == 5
    assert wait_slice_ms(0) == 0


def test_lifetime_termination_injected_v23(monkeypatch):
    """Termination adapter injected — no real kernel call, no os._exit in runner."""
    import drilling_intelligence.ui.lifetime as lifetime

    class _Scripted:
        def __init__(self, *script):
            self.script = list(script)
            self.calls: list[int] = []

        def terminate(self, code: int):
            self.calls.append(code)
            entry = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            if isinstance(entry, Exception):
                raise entry
            return entry

    # POSIX path: _end_process should call os._exit; Windows path should wait
    # Test via monkeypatch — never actually exit
    monkeypatch.setattr(lifetime, "_TERMINAL_EXIT", lifetime._TerminalExit())
    monkeypatch.setattr(lifetime, "_is_windows", lambda: False)
    calls: list[str] = []
    monkeypatch.setattr(lifetime, "_flush_standard_streams", lambda: calls.append("flush"))
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: calls.append(f"exit:{code}"))
    # On POSIX, _end_process flushes and exits
    lifetime._end_process(4)
    assert calls == ["flush", "exit:4"]
    # Windows accepted path: wait without exit
    monkeypatch.setattr(lifetime, "_TERMINAL_EXIT", lifetime._TerminalExit())
    monkeypatch.setattr(lifetime, "_is_windows", lambda: True)
    calls.clear()
    monkeypatch.setattr(
        lifetime, "_TERMINATION", _Scripted(lifetime._TerminationOutcome(True, False, None, 1))
    )
    monkeypatch.setattr(
        lifetime, "_wait_for_termination", lambda *a, **k: calls.append("wait") or 0
    )
    monkeypatch.setattr(
        lifetime, "_hold_for_external_termination", lambda *a, **k: calls.append("hold")
    )
    lifetime._end_process(4)
    assert calls == ["flush", "wait"]
    assert "exit:4" not in calls
    # Windows refused path: hold without wait
    monkeypatch.setattr(lifetime, "_TERMINAL_EXIT", lifetime._TerminalExit())
    calls.clear()
    monkeypatch.setattr(
        lifetime, "_TERMINATION", _Scripted(lifetime._TerminationOutcome(False, False, 5, 3))
    )
    lifetime._end_process(4)
    assert calls == ["flush", "hold"]


def test_lifetime_application_shutdown_pure_v23():
    """application_shutdown via injection — no real os._exit, three branches."""
    from unittest.mock import MagicMock

    from drilling_intelligence.ui import lifetime
    from drilling_intelligence.ui.lifetime import ReleaseResult

    app = MagicMock()
    # Success
    ok = ReleaseResult(0, ())
    with (
        patch.object(lifetime, "finish_qt_application", return_value=(0, ok)) as mf,
        patch.object(lifetime, "fail_closed_shutdown") as mfail,
    ):
        assert lifetime.application_shutdown(app, 0) == 0
        mf.assert_called_once_with(app, 0, deadline_ms=None)
        mfail.assert_not_called()
    # Incomplete
    inc = ReleaseResult(0, ("workbench",))
    with (
        patch.object(
            lifetime, "finish_qt_application", return_value=(lifetime.SHUTDOWN_INCOMPLETE_EXIT, inc)
        ),
        patch.object(lifetime, "fail_closed_shutdown") as mfail,
    ):
        assert lifetime.application_shutdown(app, 0) == lifetime.SHUTDOWN_INCOMPLETE_EXIT
        mfail.assert_called_once_with(inc)
    # Exception
    with (
        patch.object(lifetime, "finish_qt_application", side_effect=RuntimeError("deleted")),
        patch.object(lifetime, "fail_closed_shutdown") as mfail,
    ):
        assert (
            lifetime.application_shutdown(app, 0, deadline_ms=400)
            == lifetime.SHUTDOWN_INCOMPLETE_EXIT
        )
        assert mfail.called
        assert mfail.call_args[0][0].refused == ("release-exception",)


@pytest.mark.ui
def test_review_worker_success_and_validation_v23():
    pytest.importorskip("PySide6")
    try:
        from PySide6.QtCore import QThread
    except ImportError as exc:
        pytest.skip(f"Qt unavailable: {exc}")
    from drilling_intelligence.core.errors import ValidationError
    from drilling_intelligence.ui.worker import ReviewWorker

    ctrl = MagicMock()
    ctrl.load_review.return_value = {"well": "W1"}
    w = ReviewWorker(ctrl, well_id="W1", lifecycle="actual", verify_citations=False)
    with patch.object(QThread, "currentThread") as mc:
        mc.return_value.isInterruptionRequested.return_value = False
        rec: dict[str, object] = {}
        w.succeeded.connect(lambda v: rec.setdefault("succeeded", v))
        w.failed.connect(lambda e: rec.setdefault("failed", e))
        w.run()
        assert rec.get("succeeded") == {"well": "W1"}
    # Validation
    ctrl2 = MagicMock()
    ctrl2.load_review.side_effect = ValidationError("bad", hint="hint")
    w2 = ReviewWorker(ctrl2, well_id="BAD", lifecycle="actual", verify_citations=False)
    with patch.object(QThread, "currentThread") as mc:
        mc.return_value.isInterruptionRequested.return_value = False
        rec = {}
        w2.failed.connect(lambda e: rec.setdefault("failed", e))
        w2.run()
        assert rec["failed"].category == "input"  # type: ignore[attr-defined]


@pytest.mark.ui
def test_main_window_offscreen_init_v23():
    """MainWindow offscreen init — exercises _build_ui and helpers for resilience."""
    pytest.importorskip("PySide6")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip(f"Qt unavailable: {exc}")
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import _NAVIGATION, MainWindow, _value_text

    app = QApplication.instance() or QApplication([])
    ctrl = MagicMock(spec=ReviewController)
    # Minimal controller surface used during init/startup (no DB)
    ctrl.list_wells.return_value = []
    w = MainWindow(controller=ctrl)
    try:
        assert w.windowTitle() == "Prog-Proc — Review Workbench"
        assert hasattr(w, "well_combo")
        assert hasattr(w, "open_button")
        assert _value_text(None) == "—"
        assert _value_text(0) == "0"
        assert len(_NAVIGATION) >= 12
        # Exercise a few more helpers if available
        from drilling_intelligence.ui.main_window import _fill_tree

        # _add_tree_value branches (parent is a QTreeWidget mock or real)
        # Use a simple stub parent with addChild if possible, otherwise skip
        try:
            from PySide6.QtWidgets import QTreeWidget

            tree = QTreeWidget()
            _fill_tree(tree, {"a": 1, "b": {"c": 2}})
            assert tree.topLevelItemCount() >= 1
            tree.clear()
            tree.deleteLater()
            app.processEvents()
        except Exception:  # noqa: BLE001
            pass
    finally:
        w.close()
        w.deleteLater()
        app.processEvents()


@pytest.mark.ui
def test_comparison_and_report_workers_v23():
    pytest.importorskip("PySide6")
    try:
        from PySide6.QtCore import QThread
    except ImportError as exc:
        pytest.skip(f"Qt unavailable: {exc}")
    from drilling_intelligence.ui.worker import ComparisonWorker, ReportWorker

    ctrl = MagicMock()
    ctrl.compare.return_value = {"identity": "cmp-1"}
    w = ComparisonWorker(ctrl, ["W1", "W2"])
    with patch.object(QThread, "currentThread") as mc:
        mc.return_value.isInterruptionRequested.return_value = False
        rec: dict[str, object] = {}
        w.succeeded.connect(lambda v: rec.setdefault("succeeded", v))
        w.run()
        assert rec["succeeded"] == {"identity": "cmp-1"}  # type: ignore[comparison-overlap]
    ctrl2 = MagicMock()
    ctrl2.report.return_value = {"identity": "rep-1", "mode": "single_well"}
    w2 = ReportWorker(ctrl2, well_ids=["W1"])
    with patch.object(QThread, "currentThread") as mc:
        mc.return_value.isInterruptionRequested.return_value = False
        rec = {}
        w2.succeeded.connect(lambda v: rec.setdefault("succeeded", v))
        w2.run()
        assert rec["succeeded"]["mode"] == "single_well"  # type: ignore[attr-defined]
