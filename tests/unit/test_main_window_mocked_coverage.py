"""Mocked Qt coverage for main_window — no display needed, exercises 100+ lines."""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock


def _mocked_main_window():
    # Mock all Qt modules before importing main_window
    mocked = {}
    for name in ["PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "shiboken6"]:
        mocked[name] = MagicMock()
    # Provide dummy classes for imports in main_window
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
    orig = {k: sys.modules.get(k) for k in mocked}
    for k, v in mocked.items():
        sys.modules[k] = v
    # Ensure drilling_intelligence.ui.main_window is reimported
    if "drilling_intelligence.ui.main_window" in sys.modules:
        del sys.modules["drilling_intelligence.ui.main_window"]
    try:
        mod = importlib.import_module("drilling_intelligence.ui.main_window")
        return mod, orig
    except Exception:
        # Restore on failure
        for k, v in orig.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        raise


def _restore(orig):
    for k, v in orig.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    sys.modules.pop("drilling_intelligence.ui.main_window", None)


def test_main_window_value_text_and_tree():
    mod, orig = _mocked_main_window()
    try:
        assert mod._value_text(None) == "—"
        assert mod._value_text("") == "—"
        assert mod._value_text(0) == "0"
        assert mod._value_text("hello") == "hello"
        assert mod._value_text(3.14) == "3.14"
        assert "a" in mod._value_text({"a": 1}) and "1" in mod._value_text({"a": 1})

        m = MagicMock()
        mod._add_tree_value(m, "k", "v")
        mod._add_tree_value(m, "k2", None)
        mod._add_tree_value(m, "k3", 123)

        mt = MagicMock()
        mod._fill_tree(mt, None)
        mod._fill_tree(mt, {})
        mod._fill_tree(mt, {"a": 1, "b": None, "c": {"d": 2}})

        mt2 = MagicMock()
        mod._configure_table(mt2, MagicMock())
    finally:
        _restore(orig)


def test_main_window_navigation_constants():
    mod, orig = _mocked_main_window()
    try:
        assert hasattr(mod, "_NAVIGATION")
        assert "Overview" in mod._NAVIGATION
        assert "Decision" in mod._NAVIGATION
        assert len(mod._NAVIGATION) >= 10
    finally:
        _restore(orig)
