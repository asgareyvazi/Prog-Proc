"""The Qt CI retry is a policy, not a blanket exit-code pardon."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

#: The release of each GitHub Action this repository uses, verified against the
#: upstream repository on 2026-10-24 by reading the tag list of the action's own
#: repository: each pin below is the commit the named upstream tag points at.
#: The workflows pin the commit SHA; the comment beside each pin names the tag.
#: The moving ``v7`` tag is recorded too, because a tag that moves is the reason
#: a pin exists.
ACTION_PINS = {
    "actions/checkout": {
        "sha": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "tag": "v7.0.1",
        "moving_tag": "v7",
        "moving_tag_sha": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    },
    "actions/setup-python": {
        "sha": "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0",
        "tag": "v7.1.0",
        "moving_tag": "v7",
        "moving_tag_sha": "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0",
    },
    "actions/upload-artifact": {
        "sha": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
        "tag": "v7.0.2",
        "moving_tag": "v7",
        "moving_tag_sha": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    },
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
    for action, pin in ACTION_PINS.items():
        assert f"{action}@{pin['sha']}" in workflow, action


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
            assert pin in {entry["sha"] for entry in ACTION_PINS.values()}, reference


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


def _workbench_test_source() -> str:
    path = Path(__file__).resolve().parents[2] / "tests" / "ui" / "test_workbench.py"
    return path.read_text(encoding="utf-8")


def test_the_workbench_tests_release_their_windows_and_not_only_close_them():
    """A closed window is not a destroyed window.

    The ``qt_app`` fixture is session-scoped, so a window that a test only
    closes stays alive until the end of the session - long after the
    per-test workspace it reads has been deleted.  The native abort that
    follows used to surface in a full ``tests/ui`` run, far away from the
    test that caused it.  The tests must use the production release path.
    """
    source = _workbench_test_source()

    assert "window.close()" not in source, (
        "a workbench test closes its window instead of releasing it; "
        "use release_top_level_widgets(qt_app)"
    )
    assert source.count("release_top_level_widgets(qt_app)") == 6
    assert "release_top_level_widgets" in source


def test_the_session_fixture_releases_and_then_asserts_that_nothing_survived():
    source = _workbench_test_source()

    assert '@pytest.fixture(scope="session")' in source
    assert "fixture_teardown(application)" in source
    assert "top-level widgets survived the fixture teardown" in source


def test_the_release_path_used_by_the_tests_is_the_production_path():
    """The test must not grow a private teardown of its own."""
    from drilling_intelligence.ui import lifetime

    assert callable(lifetime.release_top_level_widgets)
    source = _workbench_test_source()
    assert "from drilling_intelligence.ui.lifetime import release_top_level_widgets" in source
