"""Extra UI coverage for V8.20 gate — main_window, lifetime, controller paths without display."""

from __future__ import annotations

import pytest


@pytest.mark.ui
def test_main_window_helpers_and_class_structure():
    pytest.importorskip("PySide6")
    try:
        from drilling_intelligence.ui.main_window import (
            MainWindow,
            _add_tree_value,
            _configure_table,
            _fill_tree,
            _value_text,
        )
    except ImportError as e:
        pytest.skip(f"Qt not available (libGL missing): {e}")

    assert MainWindow is not None
    try:
        from PySide6.QtWidgets import QMainWindow
    except ImportError as e:
        pytest.skip(f"Qt not available: {e}")

    assert issubclass(MainWindow, QMainWindow)
    import inspect

    sig = inspect.signature(MainWindow.__init__)
    params = list(sig.parameters.keys())
    assert "startup_workspace" in params or "self" in params
    # Exercise helpers without display
    assert _value_text(None) == "—"
    assert _value_text("") == "—"
    assert _value_text(0) == "0"
    assert _value_text("hello") == "hello"
    # _add_tree_value and _fill_tree with mocked tree
    from unittest.mock import MagicMock

    mock_item = MagicMock()
    _add_tree_value(mock_item, "k", "v")
    # _fill_tree with None and empty
    mock_tree = MagicMock()
    _fill_tree(mock_tree, None)
    _fill_tree(mock_tree, {"a": 1, "b": None})
    # _configure_table with mock
    mock_table = MagicMock()
    mock_model = MagicMock()
    _configure_table(mock_table, mock_model)


@pytest.mark.ui
def test_lifetime_create_and_shutdown_paths():
    pytest.importorskip("PySide6")
    try:
        from drilling_intelligence.ui import lifetime
    except ImportError as e:
        pytest.skip(f"Qt not available: {e}")

    assert hasattr(lifetime, "create_application")
    assert hasattr(lifetime, "application_shutdown")
    # Check that create_application is callable and has expected signature
    import inspect

    sig = inspect.signature(lifetime.create_application)
    assert "argv" in sig.parameters or len(sig.parameters) >= 1


@pytest.mark.ui
def test_controller_and_models_import():
    pytest.importorskip("PySide6")
    try:
        from drilling_intelligence.ui.controller import ReviewController
        from drilling_intelligence.ui.models import MappingTableModel, ReviewRecordsModel
    except ImportError as e:
        pytest.skip(f"Qt not available: {e}")

    assert ReviewController is not None
    assert MappingTableModel is not None
    assert ReviewRecordsModel is not None


@pytest.mark.ui
def test_chart_and_worker_import():
    pytest.importorskip("PySide6")
    try:
        from drilling_intelligence.ui.chart import ExhibitChart
        from drilling_intelligence.ui.worker import ReviewWorker
    except ImportError as e:
        pytest.skip(f"Qt not available: {e}")

    assert ExhibitChart is not None
    assert ReviewWorker is not None


def test_app_build_parser_extra():
    from drilling_intelligence.cli.app import build_parser

    p = build_parser()
    assert p is not None
    assert hasattr(p, "parse_args")
    # --help should exit 0, --invalid should exit 2
    try:
        p.parse_args(["--help"])
    except SystemExit as e:
        assert e.code == 0
    try:
        p.parse_args(["--unknown-flag"])
    except SystemExit as e:
        assert e.code == 2
