"""Fail-closed checks for replaying the historical pytest-shard failure."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from tools.pytest_shard_artifacts import FetchError, collect
from tools.pytest_shard_replay import (
    _HISTORICAL_ARTIFACTS,
    _POSTGRESQL_NODEID,
    _POSTGRESQL_REASON,
    ReplayError,
    _emit_safe_download_diagnostic,
    _emit_safe_replay_diagnostic,
    _emit_safe_sidecar_diagnostic,
    _safe_download_diagnostic,
    _validate_archived_report,
    _validate_replay,
)

_RUN = {
    "id": 43695755434,
    "run_attempt": 1,
    "head_sha": "114173a489e8a0d5b56cdfe6f63efd14701e9d65",
    "status": "completed",
    "conclusion": "failure",
}
_ARTIFACTS = [
    "ubuntu2604-qt-collection-a1",
    "ubuntu2604-qt-shard-00-of-04-a1",
    "ubuntu2604-qt-shard-01-of-04-a1",
    "ubuntu2604-qt-shard-02-of-04-a1",
    "ubuntu2604-qt-shard-03-of-04-a1",
    "ubuntu2604-qt-shard-report-a1",
]
_REASON = (
    "PostgreSQL is NOT QUALIFIED: SQLite is the only currently qualified backend; "
    "no disposable PostgreSQL service job exists."
)


def _evidence() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    report = {
        "status": "FAIL",
        "overall_status": "FAIL",
        "run_completion_verified": True,
        "workflow_conclusion_verified": False,
        "coverage_complete": True,
        "exact_once_assignment": True,
        "exact_once_execution": True,
        "outcome_qualification": False,
        "collected_nodeids": 2893,
        "covered_nodeids": 2893,
        "totals": {
            "collected": 2893,
            "passed": 2892,
            "failed": 0,
            "errors": 0,
            "skipped": 1,
            "xfailed": 0,
            "xpassed": 0,
            "exit": 1,
            "runtime": 1.0,
        },
        "skips": [
            {
                "classname": "tests.integration.test_phase1_finalization",
                "name": "test_postgresql_claim_is_qualified_or_withdrawn",
                "reason": _REASON,
                "type": "pytest.skip",
            }
        ],
        "permitted_skip_policy": {
            "qualified": False,
            "observed_junit_skip_count": 1,
            "process_sidecar_skips": [
                {
                    "nodeid": _POSTGRESQL_NODEID,
                    "when": "call",
                    "reason": f"Skipped: {_REASON}",
                }
            ],
        },
        "issues": [
            "shard 2 did not execute every test normally outside the sole documented PostgreSQL boundary skip",
            "workflow run conclusion is 'failure'",
        ],
    }
    downloads = {
        "failures": [],
        "downloaded": [
            {
                "artifact_id": _HISTORICAL_ARTIFACTS[name][0],
                "artifact_name": name,
                "api_size_bytes": _HISTORICAL_ARTIFACTS[name][1],
                "api_digest": f"sha256:{_HISTORICAL_ARTIFACTS[name][2]}",
                "matches_api_digest": True,
                "matches_api_size": True,
            }
            for name in _ARTIFACTS
        ],
    }
    return report, downloads, deepcopy(_RUN)


def _archived_report() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "status": "FAIL",
        "repository": "asgareyvazi/Prog-Proc",
        "workflow": "CI",
        "branch": "arena/5957b76d-prog-proc",
        "head_sha": _RUN["head_sha"],
        "run_id": str(_RUN["id"]),
        "run_attempt": str(_RUN["run_attempt"]),
        "totals": {
            "collected": 2893,
            "passed": 2892,
            "failed": 0,
            "errors": 0,
            "skipped": 1,
            "xfailed": 0,
            "xpassed": 0,
        },
        "issues": [
            "shard 2 did not execute every test normally outside the sole documented PostgreSQL boundary skip"
        ],
    }


def test_artifact_fetcher_allows_only_the_additional_current_attempt_report(tmp_path: Path) -> None:
    dummy_auth_value = "test-only-credential-placeholder"
    with (
        patch("tools.pytest_shard_artifacts._json_request") as request,
        pytest.raises(FetchError, match="bounded shard-report allowlist"),
    ):
        collect(
            repository="asgareyvazi/Prog-Proc",
            run_id="43695755434",
            attempt="1",
            expected_sha=_RUN["head_sha"],
            workflow="CI",
            shard_count=4,
            output_dir=tmp_path / "invalid",
            token=dummy_auth_value,
            additional_artifact_names=("unrelated-artifact",),
        )
    request.assert_not_called()
    assert not (tmp_path / "invalid").exists()


def test_artifact_fetcher_verifies_and_records_the_optional_failed_report(tmp_path: Path) -> None:
    dummy_auth_value = "test-only-credential-placeholder"
    names = [
        "ubuntu2604-qt-collection-a1",
        *(f"ubuntu2604-qt-shard-{index:02d}-of-04-a1" for index in range(4)),
        "ubuntu2604-qt-shard-report-a1",
    ]
    digest = hashlib.sha256(b"z").hexdigest()
    rows = [
        {
            "id": index + 1,
            "name": name,
            "size_in_bytes": 1,
            "digest": f"sha256:{digest}",
            "expired": False,
        }
        for index, name in enumerate(names)
    ]

    def json_response(url: str, _token: str):
        if "/artifacts?" in url:
            return {"total_count": len(rows), "artifacts": rows}, None
        if "/jobs?" in url:
            return {"total_count": 0, "jobs": []}, None
        return {"id": 43695755434}, None

    def download(_url: str, _token: str, destination: Path):
        destination.write_bytes(b"z")
        return 1, digest

    with (
        patch("tools.pytest_shard_artifacts._json_request", side_effect=json_response),
        patch("tools.pytest_shard_artifacts._download_zip", side_effect=download),
    ):
        result = collect(
            repository="asgareyvazi/Prog-Proc",
            run_id="43695755434",
            attempt="1",
            expected_sha=_RUN["head_sha"],
            workflow="CI",
            shard_count=4,
            output_dir=tmp_path / "valid",
            token=dummy_auth_value,
            additional_artifact_names=("ubuntu2604-qt-shard-report-a1",),
        )

    assert {item["artifact_name"] for item in result["downloaded"]} == set(names)
    saved = json.loads((tmp_path / "valid" / "downloads.json").read_text(encoding="utf-8"))
    assert saved["failures"] == []
    assert all(
        item["matches_api_digest"] and item["matches_api_size"] for item in saved["downloaded"]
    )


def test_safe_artifact_diagnostic_reports_categories_without_echoing_untrusted_text(
    tmp_path: Path,
) -> None:
    report = {
        "downloaded": [],
        "failures": [
            "ubuntu2604-qt-shard-00-of-04-a1: signed artifact download failed",
            "untrusted-name: https://attacker.example/?sig=do-not-echo",
        ],
    }
    (tmp_path / "downloads.json").write_text(json.dumps(report), encoding="utf-8")

    summary = _safe_download_diagnostic(tmp_path)

    assert summary is not None
    assert '"downloaded_count": 0' in summary
    assert '"failure_count": 2' in summary
    assert '"signed artifact download failed": 1' in summary
    assert "attacker.example" not in summary
    assert "do-not-echo" not in summary


def test_safe_artifact_diagnostic_is_a_sanitized_actions_annotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = {
        "downloaded": [],
        "failures": [
            "ubuntu2604-qt-shard-00-of-04-a1: signed artifact download failed",
            "untrusted-name: https://attacker.example/?sig=do-not-echo",
        ],
    }
    (tmp_path / "downloads.json").write_text(json.dumps(evidence), encoding="utf-8")

    annotation = _emit_safe_download_diagnostic(tmp_path)

    output = capsys.readouterr().err
    assert annotation is not None and annotation.startswith(
        "::notice::SAFE ARTIFACT FETCH DIAGNOSTIC:"
    )
    assert output.strip() == annotation
    assert "attacker.example" not in output
    assert "do-not-echo" not in output


def test_replay_failure_diagnostic_only_exposes_internal_error_messages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    annotation = _emit_safe_replay_diagnostic(
        ReplayError("historical shard totals differ from the bounded target")
    )
    assert annotation == (
        "::notice::SAFE REPLAY DIAGNOSTIC: historical shard totals differ from the bounded target"
    )
    assert capsys.readouterr().err.strip() == annotation
    assert _emit_safe_replay_diagnostic(ValueError("token=never-echo-this")) is None
    assert capsys.readouterr().err == ""


def test_sidecar_diagnostic_exposes_only_allowlisted_comparison_facts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = {
        "permitted_skip_policy": {
            "process_sidecar_skips": [
                {
                    "nodeid": _POSTGRESQL_NODEID,
                    "when": "call",
                    "reason": "Skipped: " + _POSTGRESQL_REASON,
                }
            ]
        },
        "skips": [{"reason": _POSTGRESQL_REASON}],
    }
    (tmp_path / "replay-report.json").write_text(json.dumps(report), encoding="utf-8")

    annotation = _emit_safe_sidecar_diagnostic(tmp_path)

    output = capsys.readouterr().err
    assert annotation is not None
    assert '"process_reason_class": "expected_wrapped_reason"' in annotation
    assert '"junit_reason_is_canonical": true' in annotation
    assert _POSTGRESQL_REASON not in output
    assert "Skipped:" not in output


def test_artifact_fetcher_rejects_pinned_metadata_before_downloading_bytes(tmp_path: Path) -> None:
    rows = []
    dummy_auth_value = "test-only-noncredential"
    mismatched_name = "ubuntu2604-qt-shard-00-of-04-a1"
    for name, (artifact_id, size, digest) in _HISTORICAL_ARTIFACTS.items():
        rows.append(
            {
                "id": artifact_id,
                "name": name,
                "size_in_bytes": size,
                "digest": f"sha256:{'0' * 64}" if name == mismatched_name else f"sha256:{digest}",
                "expired": False,
            }
        )

    def json_response(url: str, _token: str):
        if "/artifacts?" in url:
            return {"total_count": len(rows), "artifacts": rows}, None
        if "/jobs?" in url:
            return {"total_count": 0, "jobs": []}, None
        return {"id": 43695755434}, None

    calls: list[int] = []

    def download(url: str, _token: str, destination: Path):
        artifact_id = int(url.rsplit("/", 2)[-2])
        calls.append(artifact_id)
        destination.write_bytes(b"z")
        return 1, hashlib.sha256(b"z").hexdigest()

    with (
        patch("tools.pytest_shard_artifacts._json_request", side_effect=json_response),
        patch("tools.pytest_shard_artifacts._download_zip", side_effect=download),
        pytest.raises(FetchError, match="could not be verified"),
    ):
        collect(
            repository="asgareyvazi/Prog-Proc",
            run_id="43695755434",
            attempt="1",
            expected_sha=_RUN["head_sha"],
            workflow="CI",
            shard_count=4,
            output_dir=tmp_path / "pinned",
            token=dummy_auth_value,
            additional_artifact_names=("ubuntu2604-qt-shard-report-a1",),
            expected_artifact_metadata=_HISTORICAL_ARTIFACTS,
        )

    assert _HISTORICAL_ARTIFACTS[mismatched_name][0] not in calls
    saved = json.loads((tmp_path / "pinned" / "downloads.json").read_text(encoding="utf-8"))
    assert any(mismatched_name in item for item in saved["failures"])


def test_historical_replay_accepts_expected_failure_and_reproduced_wrapper_mismatch() -> None:
    report, downloads, run = _evidence()

    _validate_replay(report=report, downloads=downloads, run=run)


def test_original_failed_report_artifact_is_bound_to_original_run_and_totals() -> None:
    _validate_archived_report(_archived_report(), deepcopy(_RUN))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report, downloads, run: downloads["downloaded"].pop(),
        lambda report, downloads, run: downloads["downloaded"][0].update(matches_api_digest=False),
        lambda report, downloads, run: run.update(head_sha="0" * 40),
        lambda report, downloads, run: run.update(conclusion="success"),
        lambda report, downloads, run: report.update(coverage_complete=False),
        lambda report, downloads, run: report.update(exact_once_execution=False),
        lambda report, downloads, run: report["totals"].update(passed=2891),
        lambda report, downloads, run: report["skips"][0].update(reason="authored text"),
        lambda report, downloads, run: report["permitted_skip_policy"]["process_sidecar_skips"][
            0
        ].update(reason=_REASON),
        lambda report, downloads, run: report["issues"].clear(),
        lambda report, downloads, run: report.update(workflow_conclusion_verified=True),
    ],
    ids=(
        "missing-artifact",
        "digest-mismatch",
        "wrong-sha",
        "wrong-historical-conclusion",
        "incomplete-coverage",
        "duplicate-or-missing-execution-proof",
        "wrong-total",
        "wrong-junit-reason",
        "wrapper-mismatch-not-reproduced",
        "failure-cause-not-reproduced",
        "failed-run-not-kept-separate-from-replay",
    ),
)
def test_historical_replay_rejects_incomplete_or_rewritten_evidence(mutation) -> None:
    report, downloads, run = _evidence()
    mutation(report, downloads, run)

    with pytest.raises(ReplayError):
        _validate_replay(report=report, downloads=downloads, run=run)


@pytest.mark.parametrize("field,value", [("status", "PASS"), ("head_sha", "0" * 40)])
def test_archived_report_rejects_wrong_status_or_identity(field: str, value: str) -> None:
    report = _archived_report()
    report[field] = value

    with pytest.raises(ReplayError):
        _validate_archived_report(report, deepcopy(_RUN))
