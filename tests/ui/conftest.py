"""Shared real-Qt fixture for offscreen workbench tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except (ImportError, OSError) as exc:
        pytest.skip(f"QtWidgets unavailable for UI tests: {exc}")
    application = QApplication.instance() or QApplication(["pytest-review-workbench"])
    yield application
    from drilling_intelligence.ui.lifetime import fixture_teardown

    fixture_teardown(application)
    import shiboken6
    from PySide6.QtWidgets import QApplication as _Application

    alive = [
        widget
        for widget in _Application.topLevelWidgets()
        if shiboken6.isValid(widget) and widget is not application
    ]
    assert not alive, f"top-level widgets survived the fixture teardown: {alive}"
