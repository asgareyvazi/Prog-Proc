"""The workbench must not leave Qt objects for interpreter shutdown."""

from __future__ import annotations

import ast
from pathlib import Path


def _tree(relative: str) -> ast.AST:
    return ast.parse(Path(relative).read_text(encoding="utf-8"), filename=relative)


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


def test_application_and_fixture_release_widgets_before_shutdown() -> None:
    app = Path("src/drilling_intelligence/ui/app.py").read_text(encoding="utf-8")
    fixture = Path("tests/ui/test_workbench.py").read_text(encoding="utf-8")
    lifetime = Path("src/drilling_intelligence/ui/lifetime.py").read_text(encoding="utf-8")
    assert "release_top_level_widgets" in app
    assert "release_top_level_widgets" in fixture
    assert "shiboken6.delete" in lifetime
    assert "destroyQCoreApplication" in lifetime
