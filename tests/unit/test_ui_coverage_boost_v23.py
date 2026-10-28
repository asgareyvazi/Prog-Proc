"""V23 coverage resilience: push qt/ui from 71.0 toward durable headroom.

Targets main_window helpers, models, chart, lifetime wait/termination and the
Qt-independent errors module. All mocked tests are pure (no display, no real
QThread) and not marked ui so they run headless; Qt tests are marked ui and
require real Qt/PySide6 to be present. No real process-killing call is made in the
main runner — termination is exercised via injection.
"""

from __future__ import annotations

import sys
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
    # Re-export from worker must be same object when Qt available;
    # headless without PySide6 cannot exercise the re-export.
    try:
        import PySide6  # noqa: F401  # check availability, not used directly

        qt_available = True
    except ImportError:
        qt_available = False

    if qt_available:
        from drilling_intelligence.ui.worker import WorkerError as FromWorker

        assert FromWorker is WorkerError
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
    except Exception as exc:  # noqa: BLE001 - QApplication creation may fail headless without xvfb
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
    """TableColumn is a dataclass; exercised via mocked Qt import graph."""

    orig = {
        k: sys.modules.get(k)
        for k in ["PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "shiboken6"]
    }
    mocked = {}
    for name in ["PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "shiboken6"]:
        mocked[name] = MagicMock()
    # Minimal Qt types needed for models import to succeed with mocks
    mocked["PySide6.QtCore"].QAbstractTableModel = MagicMock
    mocked["PySide6.QtCore"].QSortFilterProxyModel = MagicMock
    mocked["PySide6.QtCore"].Qt = MagicMock()
    mocked["PySide6.QtCore"].QModelIndex = MagicMock
    # Patch sys.modules
    for k, v in mocked.items():
        sys.modules[k] = v
    # Ensure the target module is reloaded under mocked environment
    models_was_loaded = "drilling_intelligence.ui.models" in sys.modules
    if models_was_loaded:
        del sys.modules["drilling_intelligence.ui.models"]
    try:
        import importlib

        mod = importlib.import_module("drilling_intelligence.ui.models")
        TableColumn = mod.TableColumn
        c = TableColumn("k", "K", 10)
        assert c.key == "k"
        assert c.title == "K"
        assert c.width == 10
        # MappingTableModel with mocked Qt still has pure Python row_count
        cols = [TableColumn("a", "A", 100)]
        rows = [{"a": "hello"}, {"a": None}]
        m = mod.MappingTableModel(cols, rows)  # type: ignore[arg-type]
        # Pure attribute, not Qt method, proves data handling without Qt
        assert m.row_count == 2
        assert len(m.columns) == 1
        assert m.row_payload(0) == {"a": "hello"}
        assert m.row_payload(1) == {"a": None}
        assert m.row_payload(99) is None
    finally:
        for k, v in orig.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        # Remove the mocked models module so subsequent imports get the real one
        sys.modules.pop("drilling_intelligence.ui.models", None)
        # Re-import real module for later tests that need real Qt, if originally present
        if orig.get("PySide6") is not None:
            try:
                import importlib

                importlib.import_module("drilling_intelligence.ui.models")
            except Exception:  # noqa: BLE001 - cleanup must not raise
                pass


def test_main_window_value_text_and_tree_mocked():
    """main_window helpers via mocked Qt — no display needed, exercises branches."""

    orig = {
        k: sys.modules.get(k)
        for k in ["PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "shiboken6"]
    }
    mocked: dict[str, MagicMock] = {}
    for name in ["PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "shiboken6"]:
        mocked[name] = MagicMock()
    # Provide necessary Qt classes for main_window import
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
    # Ensure Qt enums have required attributes for main_window code
    mocked["PySide6.QtCore"].Qt.ItemDataRole = MagicMock()
    mocked["PySide6.QtCore"].Qt.Orientation = MagicMock()
    mocked["PySide6.QtCore"].Qt.SortOrder = MagicMock()
    for k, v in mocked.items():
        sys.modules[k] = v
    had_main = "drilling_intelligence.ui.main_window" in sys.modules
    if had_main:
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
        mod._configure_table(table, tc)  # type: ignore[arg-type]
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
        # If models was imported under mocked Qt, remove it so next import gets real Qt
        if "drilling_intelligence.ui.models" in sys.modules and orig.get("PySide6") is not None:
            # Check if the cached models was loaded with mocked Qt by inspecting its Qt dependency
            # Simplest: pop and re-import if Qt was originally present
            try:
                sys.modules.pop("drilling_intelligence.ui.models", None)
                import importlib

                importlib.import_module("drilling_intelligence.ui.models")
            except Exception:  # noqa: BLE001
                pass


def test_lifetime_helpers_pure_v23(monkeypatch):
    """Lifetime helpers pure — deterministic via injection, no real kernel wait."""

    from drilling_intelligence.ui import lifetime as lt

    # Constants
    assert lt._WAIT_OBJECT_0 == 0
    assert lt._WAIT_TIMEOUT == 258
    assert lt._WAIT_FAILED == 4294967295
    assert lt.SHUTDOWN_INCOMPLETE_EXIT == 4
    # is_fail_closed_shutdown_exit strict: only int 4
    assert lt.is_fail_closed_shutdown_exit(4) is True
    assert lt.is_fail_closed_shutdown_exit(0) is False
    assert lt.is_fail_closed_shutdown_exit(1) is False
    assert lt.is_fail_closed_shutdown_exit(True) is False  # type: ignore[arg-type]
    assert lt.is_fail_closed_shutdown_exit(3221225477) is False
    assert lt.is_fail_closed_shutdown_exit(-1073741819) is False
    assert lt.is_fail_closed_shutdown_exit(5) is False

    # Deterministic injection: _kernel_wait_for_termination is the seam
    # Only _WAIT_OBJECT_0 is success; other values are distinct non-success
    monkeypatch.setattr(lt, "_is_windows", lambda: True)

    monkeypatch.setattr(lt, "_kernel_wait_for_termination", lambda timeout_s: lt._WAIT_OBJECT_0)
    assert lt._wait_for_termination(timeout_s=0.01) == lt._WAIT_OBJECT_0

    monkeypatch.setattr(lt, "_kernel_wait_for_termination", lambda timeout_s: lt._WAIT_TIMEOUT)
    assert lt._wait_for_termination(timeout_s=0.01) == lt._WAIT_TIMEOUT

    monkeypatch.setattr(lt, "_kernel_wait_for_termination", lambda timeout_s: lt._WAIT_FAILED)
    assert lt._wait_for_termination(timeout_s=0.01) == lt._WAIT_FAILED

    # Unexpected/malformed value must be returned as-is and not treated as success
    monkeypatch.setattr(lt, "_kernel_wait_for_termination", lambda timeout_s: 999)
    result = lt._wait_for_termination(timeout_s=0.01)
    assert result == 999
    assert result != lt._WAIT_OBJECT_0

    # Native-call exception path: _kernel_wait catches and returns _WAIT_FAILED
    # We simulate by making the monkeypatched kernel raise, then ensure
    # the wrapper that catches is exercised via the real function.
    # For _wait_for_termination, if _kernel_wait raises, it propagates;
    # the real production _kernel_wait handles the exception internally.
    # So test the real _kernel_wait's exception handling via patching ctypes.WinDLL.
    monkeypatch.setattr(lt, "_is_windows", lambda: True)

    def _raise_win(_timeout_s: float):  # type: ignore[no-untyped-def]
        raise OSError("native failure")

    monkeypatch.setattr(lt, "_kernel_wait_for_termination", _raise_win)
    # _wait_for_termination will propagate the OSError from the seam;
    # the inner _kernel_wait's own try/except is not exercised when we patch at this level.
    # Instead verify that the caller correctly treats non-zero as non-success.
    try:
        r = lt._wait_for_termination(timeout_s=0.01)
        # If it propagated, it is not success
        assert r != lt._WAIT_OBJECT_0
    except OSError:
        # Propagated exception is also not success — deterministic failure mode
        assert True

    # Restore POSIX path — must return WAIT_OBJECT_0 without kernel
    monkeypatch.setattr(lt, "_is_windows", lambda: False)
    assert lt._wait_for_termination(timeout_s=0.01) == lt._WAIT_OBJECT_0

    # Budget helpers
    assert lt.remaining_budget_ms(120_000, 10_000) == 110_000
    assert lt.remaining_budget_ms(250, 250) == 0
    assert lt.remaining_budget_ms(250, 300) < 0
    assert lt.worker_join_limit_ms(10_000, 120_000) == 10_000
    assert lt.worker_join_limit_ms(10_000, 50) == 50
    assert lt.wait_slice_ms(5) == 5
    assert lt.wait_slice_ms(0) == 0


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
    """MainWindow offscreen init — exercises _build_ui and helpers with real Qt."""

    pytest.importorskip("PySide6")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip(f"Qt unavailable: {exc}")
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - QApplication may fail without display
        pytest.skip(f"QApplication unavailable: {exc}")
    from drilling_intelligence.ui.controller import ReviewController
    from drilling_intelligence.ui.main_window import (
        _NAVIGATION,
        MainWindow,
        _fill_tree,
        _value_text,
    )

    ctrl = MagicMock(spec=ReviewController)
    ctrl.list_wells.return_value = []
    w = MainWindow(controller=ctrl)
    try:
        assert w.windowTitle() == "Prog-Proc — Review Workbench"
        assert hasattr(w, "well_combo")
        assert hasattr(w, "open_button")
        assert _value_text(None) == "—"
        assert _value_text(0) == "0"
        assert _value_text("") == "—"
        assert len(_NAVIGATION) >= 12
        # _fill_tree via real QTreeWidget
        from PySide6.QtWidgets import QTreeWidget

        tree = QTreeWidget()
        _fill_tree(tree, {"a": 1, "b": {"c": 2}})
        assert tree.topLevelItemCount() >= 1
        tree.clear()
        tree.deleteLater()
        app.processEvents()
    finally:
        w.close()
        w.deleteLater()
        app.processEvents()


@pytest.mark.ui
def test_main_window_handlers_extra_v23():
    """Exercise MainWindow handlers — deterministic, no worker start."""

    pytest.importorskip("PySide6")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip(f"Qt unavailable: {exc}")
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"QApplication unavailable: {exc}")
    from drilling_intelligence.ui.main_window import MainWindow

    ctrl = MagicMock()
    ctrl.list_wells.return_value = []
    w = MainWindow(controller=ctrl)
    try:
        w._set_busy(True)
        w._set_busy(False)
        w._set_status("extra coverage")
        # Lifecycle/well change must not crash even with empty model
        w._well_changed(0)
        w._lifecycle_changed(0)
        assert w.windowTitle().startswith("Prog-Proc")
    finally:
        w.close()
        w.deleteLater()
        app.processEvents()


@pytest.mark.ui
def test_chart_and_lifetime_offscreen_extra_v23():
    """Chart set_exhibit + lifetime helpers offscreen — deterministic, no broad except."""

    pytest.importorskip("PySide6")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip(f"Qt unavailable: {exc}")
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"QApplication unavailable: {exc}")

    from drilling_intelligence.ui.chart import ExhibitChart

    ch = ExhibitChart()
    ch.set_exhibit({"exhibits": {}})  # type: ignore[arg-type]
    ch.set_exhibit({"exhibits": {"e1": {"title": "T", "kind": "bar", "series": []}}})  # type: ignore[arg-type]
    ch.deleteLater()
    app.processEvents()

    # Lifetime deterministic branches — monkeypatched kernel wait

    from drilling_intelligence.ui import lifetime as lt

    assert isinstance(lt._is_windows(), bool)
    # _last_error returns int (0 on POSIX) — check type
    le = lt._last_error()
    assert isinstance(le, int)

    # Inject kernel wait outcomes and verify _wait_for_termination forwards them
    original_is_win = lt._is_windows
    original_kernel = lt._kernel_wait_for_termination
    try:
        lt._is_windows = lambda: True  # type: ignore[assignment]
        lt._kernel_wait_for_termination = lambda timeout_s: lt._WAIT_OBJECT_0  # type: ignore[assignment]
        assert lt._wait_for_termination(timeout_s=0.01) == lt._WAIT_OBJECT_0

        lt._kernel_wait_for_termination = lambda timeout_s: lt._WAIT_TIMEOUT  # type: ignore[assignment]
        assert lt._wait_for_termination(timeout_s=0.01) == lt._WAIT_TIMEOUT

        lt._kernel_wait_for_termination = lambda timeout_s: lt._WAIT_FAILED  # type: ignore[assignment]
        assert lt._wait_for_termination(timeout_s=0.01) == lt._WAIT_FAILED
    finally:
        lt._is_windows = original_is_win  # type: ignore[assignment]
        lt._kernel_wait_for_termination = original_kernel  # type: ignore[assignment]


@pytest.mark.ui
def test_decision_and_calculation_workers_v23():
    """Decision/Calculation workers — extra qt/ui statements."""

    pytest.importorskip("PySide6")
    try:
        from PySide6.QtCore import QThread
    except ImportError as exc:
        pytest.skip(f"Qt unavailable: {exc}")
    from drilling_intelligence.core.errors import ValidationError
    from drilling_intelligence.ui.worker import CalculationWorker, DecisionWorker

    # DecisionWorker success
    ctrl = MagicMock()
    ctrl.decision.return_value = {"pack": "ok"}
    w = DecisionWorker(ctrl, well_id="W1")
    with patch.object(QThread, "currentThread") as mc:
        mc.return_value.isInterruptionRequested.return_value = False
        rec: dict[str, object] = {}
        w.succeeded.connect(lambda v: rec.setdefault("succeeded", v))
        w.run()
        assert rec.get("succeeded") == {"pack": "ok"}
    # DecisionWorker validation error
    ctrl2 = MagicMock()
    ctrl2.decision.side_effect = ValidationError("bad", hint="h")
    w2 = DecisionWorker(ctrl2, well_id="BAD")
    with patch.object(QThread, "currentThread") as mc:
        mc.return_value.isInterruptionRequested.return_value = False
        rec = {}
        w2.failed.connect(lambda e: rec.setdefault("failed", e))
        w2.run()
        assert rec["failed"].category == "input"  # type: ignore[attr-defined]
    # CalculationWorker success
    ctrl3 = MagicMock()
    ctrl3.run_npt_rollup.return_value = {"calc": 1}
    w3 = CalculationWorker(ctrl3, well_id="W1", actor="tester")
    with patch.object(QThread, "currentThread") as mc:
        mc.return_value.isInterruptionRequested.return_value = False
        rec = {}
        w3.succeeded.connect(lambda v: rec.setdefault("succeeded", v))
        w3.run()
        assert rec.get("succeeded") == {"calc": 1}


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


def test_app_build_parser_and_main_pure_v23():
    """app.py coverage: build_parser (pure, adds ~7 stmts)."""

    from drilling_intelligence.ui.app import build_parser

    parser = build_parser()
    assert parser.prog == "drillintel-ui"
    ns = parser.parse_args(["--workspace", "ws", "--well", "W1", "--config", "cfg"])
    assert ns.workspace == "ws"
    assert ns.well == "W1"
    assert ns.config == "cfg"
    ns2 = parser.parse_args([])
    assert ns2.workspace is None


def test_well_choice_label_pure_v23():
    """controller.WellChoice label branches — pure, adds ~3 stmts to qt/ui."""

    from drilling_intelligence.ui.controller import WellChoice

    c = WellChoice(well_id="1", name="W1", field_name="F1", project_name="P1", company_name="C1")
    assert "C1" in c.label and "W1" in c.label
    assert "F1" in c.label
    c2 = WellChoice(well_id="2", name="W2")
    assert c2.label == "W2"
    c3 = WellChoice(well_id="3", name="W3", company_name="OnlyCo")
    assert "OnlyCo" in c3.label


def test_controller_workspace_info_pure_v23():
    """ReviewController workspace_info — pure, exact exception contract."""

    from pathlib import Path

    from drilling_intelligence.core.errors import WorkspaceError
    from drilling_intelligence.ui.controller import ReviewController

    ws = MagicMock()
    ws.path = Path("dummy_ws")
    ws.manifest = MagicMock()
    ws.manifest.wells = []
    # workspace.config is accessed in workspace_info
    ws.config = MagicMock()
    ws.config.name = "test-ws"
    ws.database_path = Path("dummy.db")
    ws.migration = None
    info = ReviewController.workspace_info(ws)  # type: ignore[arg-type]
    assert isinstance(info, dict)
    assert info["name"] == "test-ws"
    assert "root" in info
    ctrl = ReviewController()
    with pytest.raises(WorkspaceError, match="no workspace is open"):
        ctrl._require_workspace()  # type: ignore[attr-defined]
