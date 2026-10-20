"""The Qt CI retry is a policy, not a blanket exit-code pardon."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

#: The v7 release of each GitHub Action this repository uses, verified against
#: the upstream tag on 2026-10-20. The workflows pin the commit SHA; the
#: comment beside each pin names the tag the SHA was taken from.
ACTION_PINS_V7 = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
}

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
    collecting = "Fatal Python error: Aborted\nERROR collecting tests/ui/test_workbench.py"
    assert qt_workbench.qualifies_for_abort_retry(134, collecting) is False
    assert qt_workbench.qualifies_for_abort_retry(2, "ERROR collecting tests/ui") is False
    assert qt_workbench.qualifies_for_abort_retry(1, "") is False


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


def test_collection_accounts_for_class_async_and_parametrized_tests(tmp_path: Path):
    ui = tmp_path / "tests" / "ui"
    ui.mkdir(parents=True)
    (ui / "test_one.py").write_text(
        "class TestSuite:\n"
        "    def test_alpha(self):\n"
        "        pass\n\n"
        "async def test_beta():\n"
        "    pass\n\n"
        "def test_gamma(n):\n"
        "    pass\n",
        encoding="utf-8",
    )
    expected = qt_workbench.declared_stems(ui)
    assert expected == [
        "tests/ui/test_one.py::TestSuite::test_alpha",
        "tests/ui/test_one.py::test_beta",
        "tests/ui/test_one.py::test_gamma",
    ]
    assert (
        qt_workbench.account_for_collection(expected, []) == "pytest collected no workbench tests"
    )
    assert qt_workbench.account_for_collection(expected, ["tests/ui/test_one.py::test_alpha"]) != ""
    collected = [
        "tests/ui/test_one.py::TestSuite::test_alpha",
        "tests/ui/test_one.py::test_beta",
        "tests/ui/test_one.py::test_gamma[1]",
        "tests/ui/test_one.py::test_gamma[2]",
    ]
    assert qt_workbench.account_for_collection(expected, collected) == ""
    assert qt_workbench.account_for_collection(expected, collected + collected[:1]) != ""
    hidden = [*expected, "tests/ui/test_one.py::test_beta"]
    assert "duplicate declared" in qt_workbench.account_for_collection(hidden, collected)


def test_attempt_result_distinguishes_a_retry_pass():
    assert qt_workbench.attempt_result([(1, 0)]) == "FIRST_PASS"
    assert qt_workbench.attempt_result([(1, 134), (2, 0)]) == "RETRY_PASS"
    assert qt_workbench.attempt_result([(1, 1)]) == "FAILED"
    assert qt_workbench.attempt_result([(1, 134), (2, 134)]) == "FAILED"


def test_stability_qualification_keeps_every_failed_attempt():
    warning = (
        "QObject: shared QObject was deleted directly. The program is malformed and may crash."
    )
    assert "shared QObject" in qt_workbench.stability_failure(0, warning)
    assert qt_workbench.stability_failure(0, "loaded 4 records") == ""
    assert "exit 134" in qt_workbench.stability_failure(134, "Fatal Python error: Aborted")
    assert "assertion" in qt_workbench.stability_failure(
        1, "AssertionError: chart\nFAILED tests/ui/x"
    )
    # A later pass does not erase the first abort. Retry policy is not applied.
    status, summary = qt_workbench.qualify_stability(
        [(1, 134, "exit 134; abort signature"), (2, 0, "")]
    )
    assert status == 1
    assert "ATTEMPT 1 EXIT 134 FAIL" in summary
    assert "ATTEMPT 2 EXIT 0 PASS" in summary
    assert "stability failed: 1" in summary
    clean, clean_summary = qt_workbench.qualify_stability([(1, 0, ""), (2, 0, "")])
    assert clean == 0
    assert "stability result: PASS" in clean_summary


def test_required_jobs_stay_on_ubuntu_24_04_and_actions_v7() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "runs-on: ubuntu-latest" not in workflow
    assert workflow.count("runs-on: ubuntu-24.04") == 3
    assert "runs-on: ubuntu-26.04" in workflow
    assert "qt-ubuntu-2604-logs" in workflow
    assert "tools/qt_evidence.py" in workflow
    assert "continue-on-error" not in workflow
    for action, sha in ACTION_PINS_V7.items():
        assert f"{action}@{sha}" in workflow, action


def test_every_action_in_every_workflow_is_pinned_to_a_commit_sha() -> None:
    """A tag is a name that can move. A commit SHA cannot.

    The v7 release of each action is recorded here so the pin and the tag it
    came from can be compared by reading this file, without a network call.
    """
    for name in ("ci.yml", "ci-windows.yml"):
        workflow = Path(".github/workflows") / name
        text = workflow.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("uses:"):
                continue
            reference = stripped.removeprefix("uses:").split("#", 1)[0].strip()
            action, _, pin = reference.partition("@")
            assert action.startswith("actions/"), reference
            assert re.fullmatch(r"[0-9a-f]{40}", pin), reference
            assert pin in ACTION_PINS_V7.values(), reference


def test_stability_does_not_consult_the_retry_policy():
    source = Path("tools/qt_stability.py").read_text(encoding="utf-8")
    assert "qualifies_for_abort_retry" not in source
    assert "write_evidence_manifest" in source
    assert "test_real_fixture_workbench_current_history_evidence_plan_actual_and_detail" in source
    assert "test_presentation_filter_conflict_detail_and_explicit_error_states" in source


def test_an_attempt_after_ordinal_9_still_fails_the_qualification():
    attempts = [(ordinal, 0, "") for ordinal in range(1, 10)]
    attempts.append((10, 134, "exit 134"))
    status, summary = qt_workbench.qualify_stability(attempts)
    assert status == 1
    assert "ATTEMPT 10 EXIT 134 FAIL" in summary
    assert "ATTEMPT 9 EXIT 0 PASS" in summary


def test_the_normal_runner_still_retries_only_a_qualifying_abort():
    assert (
        qt_workbench.qualifies_for_abort_retry(134, qt_workbench.QOBJECT_DELETED_DIRECTLY) is False
    )
    assert (
        qt_workbench.qualifies_for_abort_retry(
            134, "malloc_consolidate(): unaligned fastbin chunk detected"
        )
        is True
    )


def test_collected_node_parser_ignores_the_summary_line():
    text = "tests/ui/test_workbench.py::test_alpha\n\n2 tests collected in 0.01s\n"
    assert qt_workbench.parse_collected_nodes(text) == ["tests/ui/test_workbench.py::test_alpha"]
