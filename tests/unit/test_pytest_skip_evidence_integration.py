"""Real-pytest regressions for process-sidecar/JUnit skip-reason agreement."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tools.pytest_shard import _skip_reason
from tools.pytest_shard_report import _is_single_approved_backend_skip, _junit_counts

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_REPOSITORY = "asgareyvazi/Prog-Proc"
_SHA = "a" * 40
_RUN_ID = "424242"
_ATTEMPT = "1"
_WORKFLOW = "CI"
_BRANCH = "arena/5957b76d-prog-proc"
_APPROVED_NODEID = (
    "tests/integration/test_phase1_finalization.py::test_postgresql_claim_is_qualified_or_withdrawn"
)
_APPROVED_CLASS = "tests.integration.test_phase1_finalization"
_APPROVED_NAME = "test_postgresql_claim_is_qualified_or_withdrawn"
_APPROVED_REASON = (
    "PostgreSQL is NOT QUALIFIED: SQLite is the only currently qualified backend; "
    "no disposable PostgreSQL service job exists."
)


def _run_real_pytest(project: Path, test_path: Path) -> tuple[dict[str, Any], Path]:
    """Collect then execute one real pytest shard using the production plugin."""
    project.mkdir(parents=True, exist_ok=True)
    manifest = project / "canonical-collection.json"
    identity = [
        "--progproc-head-sha",
        _SHA,
        "--progproc-run-id",
        _RUN_ID,
        "--progproc-run-attempt",
        _ATTEMPT,
        "--progproc-repository",
        _REPOSITORY,
        "--progproc-workflow",
        _WORKFLOW,
        "--progproc-workflow-ref",
        f"{_REPOSITORY}/.github/workflows/ci.yml@refs/heads/{_BRANCH}",
    ]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            (
                str(_REPOSITORY_ROOT),
                str(_REPOSITORY_ROOT / "src"),
                os.environ.get("PYTHONPATH", ""),
            )
        ),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    collect = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tools.pytest_shard",
            *identity,
            "--progproc-collection-manifest",
            str(manifest),
            "--collect-only",
            str(test_path.relative_to(project)),
        ],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert collect.returncode == 0, collect.stdout + collect.stderr
    assert manifest.is_file()

    sidecar = project / "shard-execution.json"
    observed = project / "observed-collection.json"
    selection = project / "shard-selection.json"
    junit = project / "pytest-junit.xml"
    execute = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tools.pytest_shard",
            *identity,
            "--progproc-collection-manifest",
            str(observed),
            "--progproc-expected-manifest",
            str(manifest),
            "--progproc-selection-result",
            str(selection),
            "--progproc-shard-result",
            str(sidecar),
            "--progproc-shard-index",
            "0",
            "--progproc-shard-count",
            "1",
            f"--junitxml={junit}",
            str(test_path.relative_to(project)),
        ],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert execute.returncode == 0, execute.stdout + execute.stderr
    assert sidecar.is_file() and junit.is_file() and selection.is_file()
    return json.loads(sidecar.read_text(encoding="utf-8")), junit


def test_real_pytest_postgresql_skip_matches_junit_after_exact_wrapper_normalization(
    tmp_path: Path,
) -> None:
    """This fails against the original bug: sidecar said ``Skipped: reason``."""
    project = tmp_path / "project"
    test_file = project / "tests" / "integration" / "test_phase1_finalization.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "import pytest\n"
        f"def test_postgresql_claim_is_qualified_or_withdrawn(): pytest.skip({_APPROVED_REASON!r})\n",
        encoding="utf-8",
    )

    execution, junit_path = _run_real_pytest(project, test_file)
    node_phases = execution["reports"][_APPROVED_NODEID]
    process_skips = [phase for phase in node_phases if phase.get("outcome") == "skipped"]
    counts, _root, junit_skips = _junit_counts(junit_path)

    assert len(process_skips) == 1
    assert process_skips[0]["when"] == "call"
    assert process_skips[0]["skip_reason"] == _APPROVED_REASON
    assert counts["skipped"] == 1
    assert counts["passed"] == 0
    assert junit_skips == [
        {
            "classname": _APPROVED_CLASS,
            "name": _APPROVED_NAME,
            "reason": _APPROVED_REASON,
            "type": "pytest.skip",
        }
    ]
    assert _is_single_approved_backend_skip(execution["reports"], junit_skips)


def test_policy_skip_rejects_identity_reason_phase_and_shape_mismatches() -> None:
    reports = {
        _APPROVED_NODEID: [
            {"when": "setup", "outcome": "passed"},
            {"when": "call", "outcome": "skipped", "skip_reason": _APPROVED_REASON},
            {"when": "teardown", "outcome": "passed"},
        ]
    }
    junit = {
        "classname": _APPROVED_CLASS,
        "name": _APPROVED_NAME,
        "reason": _APPROVED_REASON,
        "type": "pytest.skip",
    }
    assert _is_single_approved_backend_skip(reports, [junit])

    bad_junit_cases = []
    for key, value in (
        ("classname", "tests.integration.other_module"),
        ("name", "test_other_name"),
        ("reason", _APPROVED_REASON + " extra"),
        ("type", "pytest.xfail"),
    ):
        changed = dict(junit)
        changed[key] = value
        bad_junit_cases.append(changed)
    assert all(not _is_single_approved_backend_skip(reports, [case]) for case in bad_junit_cases)

    bad_report_shapes = [
        {
            _APPROVED_NODEID.replace("test_phase1_finalization.py", "other.py"): reports[
                _APPROVED_NODEID
            ]
        },
        {
            _APPROVED_NODEID: [
                {"when": "setup", "outcome": "passed"},
                {"when": "setup", "outcome": "skipped", "skip_reason": _APPROVED_REASON},
            ]
        },
        {
            _APPROVED_NODEID: [
                {"when": "setup", "outcome": "passed"},
                {"when": "call", "outcome": "skipped"},
            ]
        },
        {
            _APPROVED_NODEID: [
                {"when": "setup", "outcome": "passed"},
                {"when": "call", "outcome": "skipped", "skip_reason": _APPROVED_REASON},
                {"when": "call", "outcome": "passed"},
            ]
        },
        {
            _APPROVED_NODEID: [
                {"when": "setup", "outcome": "passed"},
                {"when": "call", "outcome": "skipped", "skip_reason": _APPROVED_REASON},
                {"when": "teardown", "outcome": "failed"},
            ]
        },
        {
            _APPROVED_NODEID: [
                {"when": "setup", "outcome": "passed"},
                {"when": "call", "outcome": "skipped", "skip_reason": _APPROVED_REASON},
            ],
            "tests/integration/other.py::test_other": [
                {"when": "setup", "outcome": "passed"},
                {"when": "call", "outcome": "skipped", "skip_reason": "unexpected"},
            ],
        },
    ]
    assert all(not _is_single_approved_backend_skip(shape, [junit]) for shape in bad_report_shapes)
    assert not _is_single_approved_backend_skip(None, [junit])
    assert not _is_single_approved_backend_skip(reports, [])


def test_real_pytest_removes_only_its_outer_prefix_and_preserves_authored_prefix(
    tmp_path: Path,
) -> None:
    authored = "Skipped: authored text deliberately starts with the wrapper spelling"
    project = tmp_path / "project"
    test_file = project / "tests" / "integration" / "test_phase1_finalization.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "import pytest\n"
        f"def test_postgresql_claim_is_qualified_or_withdrawn(): pytest.skip({authored!r})\n",
        encoding="utf-8",
    )

    execution, junit_path = _run_real_pytest(project, test_file)
    phases = execution["reports"][_APPROVED_NODEID]
    process_skip = next(phase for phase in phases if phase.get("outcome") == "skipped")
    _counts, _root, junit_skips = _junit_counts(junit_path)

    assert process_skip["skip_reason"] == authored
    assert junit_skips[0]["reason"] == authored
    assert not _is_single_approved_backend_skip(execution["reports"], junit_skips)


def test_real_pytest_skip_phase_variants_and_xfail_xpass_are_not_misclassified(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    test_file = project / "tests" / "unit" / "test_skip_variants.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def setup_skip(): pytest.skip('setup reason')\n"
        "@pytest.fixture\n"
        "def teardown_skip():\n"
        "    yield\n"
        "    pytest.skip('teardown reason')\n"
        "def test_normal_skip(): pytest.skip('ordinary reason')\n"
        "def test_setup_skip(setup_skip): pass\n"
        "def test_teardown_skip(teardown_skip): pass\n"
        "@pytest.mark.xfail(reason='expected failure')\n"
        "def test_xfail(): assert False\n"
        "@pytest.mark.xfail(reason='unexpected pass')\n"
        "def test_xpass(): pass\n",
        encoding="utf-8",
    )

    execution, junit_path = _run_real_pytest(project, test_file)
    counts, _root, junit_skips = _junit_counts(junit_path)
    reports = execution["reports"]

    # The ordinary call skip and setup skip have matching authored reasons in
    # the process report and JUnit. The teardown action is recorded as teardown
    # evidence and can never satisfy the exact call-phase allowlist.
    by_name = {row["name"]: row for row in junit_skips}
    for name, reason in (
        ("test_normal_skip", "ordinary reason"),
        ("test_setup_skip", "setup reason"),
    ):
        _nodeid, phases = next(
            (nodeid, phases) for nodeid, phases in reports.items() if nodeid.endswith(f"::{name}")
        )
        phase = next(phase for phase in phases if phase.get("outcome") == "skipped")
        assert phase["skip_reason"] == by_name[name]["reason"] == reason
        if name == "test_setup_skip":
            assert phase["when"] == "setup"
        else:
            assert phase["when"] == "call"

    teardown_id, teardown_phases = next(
        (nodeid, phases)
        for nodeid, phases in reports.items()
        if nodeid.endswith("::test_teardown_skip")
    )
    assert any(
        phase.get("when") == "teardown" and phase.get("outcome") == "skipped"
        for phase in teardown_phases
    )
    assert counts["xfailed"] == 1
    # Raw pytest JUnit records XPASS as an ordinary testcase; the production
    # totals plugin annotates it before reconciliation. The process sidecar
    # must still preserve pytest's wasxfail marker for this case.
    assert counts["xpassed"] == 0
    assert counts["skipped"] == 3
    assert counts["passed"] == 1
    xpass_phases = next(
        phases for nodeid, phases in reports.items() if nodeid.endswith("::test_xpass")
    )
    assert any(phase.get("wasxfail") == "unexpected pass" for phase in xpass_phases)
    assert not _is_single_approved_backend_skip(reports, junit_skips)
    assert teardown_id.endswith("::test_teardown_skip")


@pytest.mark.parametrize(
    ("longrepr", "expected"),
    [
        (("file.py", 1, "Skipped: authored"), "authored"),
        (("file.py", 1, "Skipped: Skipped: authored"), "Skipped: authored"),
        (("file.py", 1, "not a pytest skip wrapper"), "not a pytest skip wrapper"),
        (("too", "short"), "('too', 'short')"),
        (None, ""),
    ],
)
def test_skip_reason_normalizer_is_exact_and_fail_closed(longrepr: object, expected: str) -> None:
    class Report:
        pass

    report = Report()
    report.longrepr = longrepr
    assert _skip_reason(report) == expected
