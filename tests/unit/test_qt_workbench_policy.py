"""The Qt CI retry is a policy, not a blanket exit-code pardon."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "qt_workbench", Path(__file__).resolve().parents[2] / "tools" / "qt_workbench.py"
)
assert _SPEC is not None and _SPEC.loader is not None
qt_workbench = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(qt_workbench)


def test_exit_134_without_an_abort_signature_is_not_retried():
    assert qt_workbench.qualifies_for_abort_retry(134, "") is False
    assert qt_workbench.qualifies_for_abort_retry(134, "pytest crashed for another reason") is False
    assert qt_workbench.qualifies_for_abort_retry(1, "Fatal Python error: Aborted") is False


def test_an_assertion_failure_is_never_retried_even_if_the_process_aborted():
    output = "Fatal Python error: Aborted\nAssertionError: chart state\nFAILED tests/ui/test_workbench.py::test_x"
    assert qt_workbench.qualifies_for_abort_retry(134, output) is False


def test_the_recorded_runner_abort_may_be_retried_once():
    assert (
        qt_workbench.qualifies_for_abort_retry(
            134, "malloc_consolidate(): unaligned fastbin chunk detected\nAborted (core dumped)"
        )
        is True
    )
    assert (
        qt_workbench.qualifies_for_abort_retry(134, "terminate called without an active exception")
        is True
    )


def test_collection_must_account_for_every_declared_test(tmp_path: Path):
    ui = tmp_path / "tests" / "ui"
    ui.mkdir(parents=True)
    (ui / "test_one.py").write_text(
        "def test_alpha():\n    pass\n\ndef test_beta():\n    pass\n", encoding="utf-8"
    )
    expected = qt_workbench.expected_test_names(ui)
    assert expected == ["test_alpha", "test_beta"]
    assert (
        qt_workbench.account_for_collection(expected, []) == "pytest collected no workbench tests"
    )
    assert qt_workbench.account_for_collection(expected, ["tests/ui/test_one.py::test_alpha"]) != ""
    assert (
        qt_workbench.account_for_collection(
            expected,
            ["tests/ui/test_one.py::test_alpha", "tests/ui/test_one.py::test_beta"],
        )
        == ""
    )


def test_collected_node_parser_ignores_the_summary_line():
    text = "tests/ui/test_workbench.py::test_alpha\n\n2 tests collected in 0.01s\n"
    assert qt_workbench.parse_collected_nodes(text) == ["tests/ui/test_workbench.py::test_alpha"]
