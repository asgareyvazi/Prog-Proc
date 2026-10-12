"""The workbench must not leave Qt objects for interpreter shutdown."""

from __future__ import annotations

import ast
from pathlib import Path


def _tree(relative: str) -> ast.AST:
    return ast.parse(Path(relative).read_text(encoding="utf-8"), filename=relative)


def test_release_result_is_validity_not_a_count() -> None:
    from drilling_intelligence.ui.lifetime import ReleaseResult

    assert ReleaseResult(3, ()).complete is True
    refused = ReleaseResult(3, ("workbench",))
    assert refused.complete is False
    assert refused.released == 3


def test_close_waits_for_every_worker_thread_attribute() -> None:
    tree = _tree("src/drilling_intelligence/ui/main_window.py")
    constant = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_WORKER_THREAD_ATTRS":
                constant = node
    assert constant is not None
    declared = {elt.value for elt in constant.value.elts if isinstance(elt, ast.Constant)}
    assigned: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr.endswith("_thread"):
                assigned.add(target.attr)
    assert assigned <= declared
    assert "_decision_thread" in declared
    assert "_comparison_thread" in declared
    source = Path("src/drilling_intelligence/ui/main_window.py").read_text(encoding="utf-8")
    assert "lambda:" not in source


def test_application_and_fixture_use_the_same_shutdown_contract() -> None:
    app = Path("src/drilling_intelligence/ui/app.py").read_text(encoding="utf-8")
    fixture = Path("tests/ui/test_workbench.py").read_text(encoding="utf-8")
    lifetime = Path("src/drilling_intelligence/ui/lifetime.py").read_text(encoding="utf-8")
    assert "application_shutdown" in app
    assert "os._exit" not in app
    shutdown = lifetime.split("def application_shutdown", 1)[1].split("def fixture_teardown", 1)[0]
    assert "finish_qt_application" in shutdown
    assert "fail_closed_shutdown" in shutdown
    assert "deadline_ms=deadline_ms" in shutdown
    assert "fixture_teardown" in fixture
    assert "application.quit()" not in fixture.split("yield application", 1)[1]
    assert "shiboken6.delete" in lifetime
    assert "widget.close() is not False" in lifetime
    assert "shiboken6.isValid" in lifetime
    assert "PRODUCTION_SHUTDOWN_WAIT_MS = 120_000" in lifetime
    coordinate = lifetime.split("def _coordinate", 1)[1].split("def finish_qt_application", 1)[0]
    assert ".exec()" not in coordinate
    assert "QEventLoop()" not in coordinate
    assert coordinate.count("elapsed.start()") == 1
    assert "PRODUCTION_SHUTDOWN_WAIT_MS" not in coordinate
    teardown = lifetime.split("def fixture_teardown", 1)[1]
    assert teardown.index("os._exit") < teardown.index("application.quit()")
    window = Path("src/drilling_intelligence/ui/main_window.py").read_text(encoding="utf-8")
    assert "self._worker_join_timeout_ms = 10_000" in window
    assert "_shutdown_join_budget_ms" in window
    assert "thread.wait(limit)" in window
    assert "def owned_workers_running" in window
    assert "owned_workers_running" in lifetime


def test_shutdown_child_cannot_disarm_the_parent_watchdog() -> None:
    child = Path("tests/ui/qt_shutdown_child.py").read_text(encoding="utf-8")
    parent = Path("tests/ui/test_qt_shutdown.py").read_text(encoding="utf-8")
    assert "SIG_IGN" not in child
    assert "signal.signal" not in child
    assert "application_shutdown" in child
    deadline = child.split("def production_deadline", 1)[1].split("def bounded_incomplete", 1)[0]
    assert "deadline_ms=400" in deadline
    assert "max_wait_ms" not in deadline
    assert "os._exit" not in deadline
    active = child.split("def quit_active", 1)[1].split("def quit_several", 1)[0]
    assert active.index("_exec(app, probe)") < active.index("_after_exec")
    assert "quit()" not in active
    sleeper = child.split("def _sleeper", 1)[1].split("def _window", 1)[0]
    assert "time.sleep" in sleeper
    assert "timeout=" in parent
    assert "timeout=8" in parent


def test_one_shutdown_budget_is_not_restarted_and_does_not_shrink_a_covered_join() -> None:
    from drilling_intelligence.ui.lifetime import (
        remaining_budget_ms,
        wait_slice_ms,
        worker_join_limit_ms,
    )

    assert remaining_budget_ms(120_000, 10_000) == 110_000
    assert remaining_budget_ms(250, 250) == 0
    assert remaining_budget_ms(250, 300) < 0
    # The production 10s join is unchanged while the 120s ceiling still covers it.
    assert worker_join_limit_ms(10_000, 120_000) == 10_000
    assert worker_join_limit_ms(10_000, 50) == 50
    assert worker_join_limit_ms(40, 0) == 0
    assert wait_slice_ms(5) == 5
    assert wait_slice_ms(0) == 0
