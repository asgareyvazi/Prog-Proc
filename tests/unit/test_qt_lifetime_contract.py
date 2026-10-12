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
    assert "finish_qt_application" in app
    assert "fail_closed_shutdown" in app
    assert "fixture_teardown" in fixture
    assert "application.quit()" not in fixture.split("yield application", 1)[1]
    assert "shiboken6.delete" in lifetime
    assert "if widget.close() is False" in lifetime
    assert "shiboken6.isValid" in lifetime
    assert "PRODUCTION_SHUTDOWN_WAIT_MS = 120_000" in lifetime
    assert "os._exit" not in app
    teardown = lifetime.split("def fixture_teardown", 1)[1]
    assert teardown.index("os._exit") < teardown.index("application.quit()")
    window = Path("src/drilling_intelligence/ui/main_window.py").read_text(encoding="utf-8")
    assert "self._worker_join_timeout_ms = 10_000" in window
    assert "thread.wait(self._worker_join_timeout_ms)" in window
