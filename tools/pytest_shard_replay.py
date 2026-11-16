#!/usr/bin/env python3
"""Replay a historical pytest-shard run from checksum-verified GitHub artifacts.

The replay preserves the historical workflow outcome: an originally failed run
must still report FAIL. It separately verifies that the old payloads contain
complete exact-once coverage and reproduces the known pytest skip-display-prefix
mismatch, without treating offline replay as a newly passing GitHub workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tools.pytest_shard_artifacts import FetchError, collect
from tools.pytest_shard_report import _extract

_POSTGRESQL_NODEID = (
    "tests.integration.test_phase1_finalization::test_postgresql_claim_is_qualified_or_withdrawn"
)
_POSTGRESQL_REASON = (
    "PostgreSQL is NOT QUALIFIED: SQLite is the only currently qualified backend; "
    "no disposable PostgreSQL service job exists."
)
_HISTORICAL_REPORT_NAME = "ubuntu2604-qt-shard-report-a1"
_HISTORICAL_ARTIFACTS = {
    "ubuntu2604-qt-collection-a1": (
        14153934832,
        55789,
        "57cc861a16b177bddcc52b81e8e78350fd02f7a496751cf9de4f39cf06d2deb9",
    ),
    "ubuntu2604-qt-shard-00-of-04-a1": (
        14154442461,
        139067,
        "f1506bd476e606261892b1bb4d2bf9c97f1f99e9497f5feb6b505d9f802cb36f",
    ),
    "ubuntu2604-qt-shard-01-of-04-a1": (
        14155155156,
        144870,
        "7265b15a3a61557df1bdee2653c8a902828186e6d13a42f44b22860308d2dd27",
    ),
    "ubuntu2604-qt-shard-02-of-04-a1": (
        14154935699,
        143338,
        "4204e0461e2187162e8b19afa7e2f8ef961e3c09d34765fd56f2ff7d1aaec5a3",
    ),
    "ubuntu2604-qt-shard-03-of-04-a1": (
        14154004233,
        142003,
        "062f3f34389f16c1f3341b88f36ce8df2fb97d4492e2e48f4b901a0c434d254a",
    ),
    _HISTORICAL_REPORT_NAME: (
        14154880825,
        81488,
        "7852477463a5425edc4693a1c82da109fa5253823d5f483c559bfd09ef67e5d9",
    ),
}
_HISTORICAL_REPORT_FILES = {
    "pytest-shard-report.json",
    "pytest-shard-report.md",
    "pytest-shard-combined.xml",
    "pytest-shard-combined-stats.json",
    "pytest-shard-combined-output.txt",
    "pytest-shard-combined-exit.txt",
    "pytest-shard-combined-state.txt",
}


class ReplayError(ValueError):
    """Historical artifacts did not match the expected, explicitly bounded replay."""


def _validate_replay(
    *, report: dict[str, Any], downloads: dict[str, Any], run: dict[str, Any]
) -> None:
    """Check payload and API identities, exact-once evidence, and the old defect."""
    expected_names = {
        "ubuntu2604-qt-collection-a1",
        *(f"ubuntu2604-qt-shard-{index:02d}-of-04-a1" for index in range(4)),
        _HISTORICAL_REPORT_NAME,
    }
    downloaded = downloads.get("downloaded")
    if (
        not isinstance(downloaded, list)
        or len(downloaded) != len(expected_names)
        or {item.get("artifact_name") for item in downloaded if isinstance(item, dict)}
        != expected_names
    ):
        raise ReplayError("verified artifact inventory is incomplete or unexpected")
    if any(
        not isinstance(item, dict)
        or item.get("matches_api_digest") is not True
        or item.get("matches_api_size") is not True
        for item in downloaded
    ):
        raise ReplayError("a downloaded artifact did not match GitHub API size and SHA-256")
    for item in downloaded:
        name = item["artifact_name"]
        expected_id, expected_size, expected_digest = _HISTORICAL_ARTIFACTS[name]
        if (
            item.get("artifact_id") != expected_id
            or item.get("api_size_bytes") != expected_size
            or item.get("api_digest") != f"sha256:{expected_digest}"
        ):
            raise ReplayError("historical artifact API metadata differs from the pinned evidence")
    if downloads.get("failures") != []:
        raise ReplayError("artifact downloader reported a failure")
    if (
        run.get("id") != 43695755434
        or run.get("run_attempt") != 1
        or run.get("head_sha") != "114173a489e8a0d5b56cdfe6f63efd14701e9d65"
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
    ):
        raise ReplayError("GitHub run identity or original outcome differs from the replay target")
    expected_totals = {
        "collected": 2893,
        "passed": 2892,
        "failed": 0,
        "errors": 0,
        "skipped": 1,
        "xfailed": 0,
        "xpassed": 0,
    }
    if report.get("status") != "FAIL" or report.get("overall_status") != "FAIL":
        raise ReplayError("replay did not preserve the historical failed workflow outcome")
    if report.get("run_completion_verified") is not True:
        raise ReplayError("historical completed-run evidence was not verified")
    if report.get("workflow_conclusion_verified") is not False:
        raise ReplayError("the failed historical workflow was incorrectly represented as success")
    if report.get("coverage_complete") is not True:
        raise ReplayError("canonical test-universe coverage is incomplete")
    if (
        report.get("exact_once_assignment") is not True
        or report.get("exact_once_execution") is not True
    ):
        raise ReplayError("historical node IDs were not assigned and executed exactly once")
    totals = report.get("totals")
    if not isinstance(totals, dict) or totals != {
        **expected_totals,
        "exit": 1,
        "runtime": totals.get("runtime"),
    }:
        raise ReplayError("historical shard totals differ from the API-bound expected totals")
    if (
        report.get("collected_nodeids") != expected_totals["collected"]
        or report.get("covered_nodeids") != expected_totals["collected"]
    ):
        raise ReplayError("canonical and covered node-ID counts do not match")

    skips = report.get("skips")
    if not isinstance(skips, list) or len(skips) != 1:
        raise ReplayError("historical JUnit evidence does not contain exactly one skip")
    junit_skip = skips[0]
    if (
        junit_skip.get("classname") != "tests.integration.test_phase1_finalization"
        or junit_skip.get("name") != "test_postgresql_claim_is_qualified_or_withdrawn"
        or junit_skip.get("reason") != _POSTGRESQL_REASON
    ):
        raise ReplayError("historical JUnit skip identity/reason differs from the approved policy")
    policy = report.get("permitted_skip_policy")
    if (
        not isinstance(policy, dict)
        or policy.get("observed_junit_skip_count") != 1
        or policy.get("qualified") is not False
        or report.get("outcome_qualification") is not False
    ):
        raise ReplayError("historical skip-policy evidence is malformed or over-qualifies outcomes")
    process_skips = policy.get("process_sidecar_skips")
    if not isinstance(process_skips, list) or len(process_skips) != 1:
        raise ReplayError("historical process sidecar does not contain exactly one skip")
    process_skip = process_skips[0]
    if (
        process_skip.get("nodeid") != _POSTGRESQL_NODEID
        or process_skip.get("when") != "call"
        or process_skip.get("reason") != f"Skipped: {_POSTGRESQL_REASON}"
    ):
        raise ReplayError("historical process sidecar did not reproduce the known wrapper mismatch")
    issues = report.get("issues")
    if not isinstance(issues, list) or not any(
        isinstance(issue, str) and "did not execute every test normally" in issue
        for issue in issues
    ):
        raise ReplayError("the historical reconciliation defect was not reproduced")
    if not any(
        isinstance(issue, str) and "workflow run conclusion is 'failure'" in issue
        for issue in issues
    ):
        raise ReplayError("the historical failing workflow conclusion was not preserved")


def _validate_archived_report(report: dict[str, Any], run: dict[str, Any]) -> None:
    """Validate the original failed reconciliation artifact without trusting it."""
    if not (
        report.get("schema_version") == 2
        and report.get("status") == "FAIL"
        and report.get("repository") == "asgareyvazi/Prog-Proc"
        and report.get("workflow") == "CI"
        and report.get("branch") == "arena/5957b76d-prog-proc"
        and report.get("head_sha") == run.get("head_sha")
        and str(report.get("run_id")) == str(run.get("id"))
        and str(report.get("run_attempt")) == str(run.get("run_attempt"))
    ):
        raise ReplayError(
            "archived failed reconciliation report has the wrong identity/schema/status"
        )
    totals = report.get("totals")
    if not isinstance(totals, dict) or any(
        totals.get(name) != expected
        for name, expected in {
            "collected": 2893,
            "passed": 2892,
            "failed": 0,
            "errors": 0,
            "skipped": 1,
            "xfailed": 0,
            "xpassed": 0,
        }.items()
    ):
        raise ReplayError("archived failed reconciliation report has unexpected aggregate totals")
    issues = report.get("issues")
    if not isinstance(issues, list) or not any(
        isinstance(issue, str) and "did not execute every test normally" in issue
        for issue in issues
    ):
        raise ReplayError(
            "archived report does not preserve the original skip reconciliation defect"
        )


def replay(output_dir: Path, token: str) -> dict[str, Any]:
    """Fetch the historical run, verify it offline, and return a bounded summary."""
    collect(
        repository="asgareyvazi/Prog-Proc",
        run_id="43695755434",
        attempt="1",
        expected_sha="114173a489e8a0d5b56cdfe6f63efd14701e9d65",
        workflow="CI",
        shard_count=4,
        output_dir=output_dir,
        token=token,
        additional_artifact_names=(_HISTORICAL_REPORT_NAME,),
        expected_artifact_metadata=_HISTORICAL_ARTIFACTS,
    )
    report_json = output_dir / "replay-report.json"
    report_md = output_dir / "replay-report.md"
    result_paths = {
        "--shard-root": output_dir,
        "--archive-root": output_dir / "archives",
        "--extracted-root": output_dir / "extracted",
        "--run": output_dir / "run.json",
        "--jobs": output_dir / "jobs.json",
        "--artifacts": output_dir / "artifacts.json",
        "--sha": "114173a489e8a0d5b56cdfe6f63efd14701e9d65",
        "--repository": "asgareyvazi/Prog-Proc",
        "--workflow": "CI",
        "--branch": "arena/5957b76d-prog-proc",
        "--workflow-ref": (
            "asgareyvazi/Prog-Proc/.github/workflows/ci.yml@refs/heads/arena/5957b76d-prog-proc"
        ),
        "--run-id": "43695755434",
        "--attempt": "1",
        "--shard-count": "4",
        "--output-json": report_json,
        "--output-markdown": report_md,
        "--combined-junit": output_dir / "replay-combined.xml",
        "--combined-stats": output_dir / "replay-stats.json",
        "--combined-output": output_dir / "replay-output.txt",
        "--combined-exit": output_dir / "replay-exit.txt",
        "--combined-state": output_dir / "replay-state.txt",
    }
    command = [sys.executable, "-m", "tools.pytest_shard_report"]
    for flag, value in result_paths.items():
        command.extend((flag, str(value)))
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 1:
        raise ReplayError(
            "offline aggregator did not return the expected historical failure status"
        )
    try:
        report = json.loads(report_json.read_text(encoding="utf-8"))
        run = json.loads((output_dir / "run.json").read_text(encoding="utf-8"))
        download_record = json.loads((output_dir / "downloads.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReplayError("offline replay outputs are missing or malformed") from exc
    if (
        not isinstance(report, dict)
        or not isinstance(run, dict)
        or not isinstance(download_record, dict)
    ):
        raise ReplayError("offline replay outputs have the wrong JSON shape")
    _validate_replay(report=report, downloads=download_record, run=run)
    try:
        artifacts_payload = json.loads((output_dir / "artifacts.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReplayError("historical artifact API inventory is missing or malformed") from exc
    artifact_rows = (
        artifacts_payload.get("artifacts") if isinstance(artifacts_payload, dict) else None
    )
    report_artifacts = [
        item
        for item in artifact_rows or []
        if isinstance(item, dict) and item.get("name") == _HISTORICAL_REPORT_NAME
    ]
    if len(report_artifacts) != 1:
        raise ReplayError("historical failed-report artifact is missing or ambiguous")
    report_artifact = report_artifacts[0]
    provenance = report_artifact.get("workflow_run")
    repository = run.get("repository")
    if (
        report_artifact.get("expired") is not False
        or not isinstance(provenance, dict)
        or not isinstance(repository, dict)
        or provenance.get("repository_id") != repository.get("id")
        or str(provenance.get("id")) != str(run.get("id"))
        or provenance.get("head_sha") != run.get("head_sha")
    ):
        raise ReplayError("historical failed-report artifact is not bound to the target run")
    archived_report_dir = _extract(
        report_artifact,
        archive_root=output_dir / "archives",
        extracted_root=output_dir / "extracted",
        allowed=_HISTORICAL_REPORT_FILES,
    )
    try:
        archived_report = json.loads(
            (archived_report_dir / "pytest-shard-report.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReplayError("original failed-report artifact JSON is missing or malformed") from exc
    if not isinstance(archived_report, dict):
        raise ReplayError("original failed-report artifact has the wrong JSON root")
    _validate_archived_report(archived_report, run)
    return {
        "historical_run_id": run["id"],
        "historical_attempt": run["run_attempt"],
        "historical_sha": run["head_sha"],
        "historical_workflow_conclusion": run["conclusion"],
        "artifact_archives_verified": len(download_record["downloaded"]),
        "historical_report_artifact_id": report_artifact["id"],
        "archived_report_payload_verified": True,
        "collected_nodeids": report["collected_nodeids"],
        "covered_nodeids": report["covered_nodeids"],
        "totals": report["totals"],
        "historical_status": report["status"],
        "known_skip_wrapper_mismatch_reproduced": True,
        "workflow_qualification_inferred": False,
    }


def _safe_download_diagnostic(output_dir: Path) -> str | None:
    """Summarize known failure categories without exposing URLs or credentials."""
    path = output_dir / "downloads.json"
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(evidence, dict):
        return None
    downloaded = evidence.get("downloaded")
    failures = evidence.get("failures")
    if not isinstance(downloaded, list) or not isinstance(failures, list):
        return None
    safe_messages = {
        "signed artifact storage returned an HTTP error",
        "artifact redirect chain is missing a target or exceeds its limit",
        "refusing a malformed artifact storage URL",
        "refusing an untrusted artifact storage redirect",
        "GitHub artifact request failed",
        "signed artifact download failed",
        "artifact redirect chain did not produce a response",
        "artifact archive destination already exists",
        "artifact archive exceeds the configured size limit",
        "artifact archive download was empty",
        "missing numeric API artifact ID",
        "expired or expiration is unknown",
        "API artifact size is malformed",
        "API artifact SHA-256 digest is malformed",
        "GitHub API metadata differs from pinned historical evidence",
        "downloaded bytes do not match API digest and size",
    }
    counts: dict[str, int] = {}
    for failure in failures:
        if not isinstance(failure, str):
            category = "malformed failure record"
        else:
            _, separator, detail = failure.partition(": ")
            if not separator:
                category = "unclassified artifact fetch failure"
            elif detail in safe_messages:
                category = detail
            elif detail.startswith("expected one API artifact record, found "):
                suffix = detail.removeprefix("expected one API artifact record, found ")
                category = (
                    f"expected one API artifact record, found {suffix}"
                    if suffix.isdecimal()
                    else "unclassified artifact fetch failure"
                )
            elif detail.startswith("GitHub artifact API returned HTTP "):
                code = detail.removeprefix("GitHub artifact API returned HTTP ")
                category = (
                    f"GitHub artifact API returned HTTP {code}"
                    if code.isdecimal()
                    else ("unclassified artifact fetch failure")
                )
            else:
                category = "unclassified artifact fetch failure"
        counts[category] = counts.get(category, 0) + 1
    return json.dumps(
        {
            "downloaded_count": len(downloaded),
            "failure_count": len(failures),
            "failure_categories": counts,
        },
        sort_keys=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("INCOMPLETE: read-only GitHub Actions token is missing", file=sys.stderr)
        return 1
    try:
        summary = replay(args.output_dir, token)
    except (FetchError, ReplayError, OSError, ValueError) as exc:
        print(
            f"INCOMPLETE: historical replay could not be verified ({type(exc).__name__})",
            file=sys.stderr,
        )
        diagnostic = _safe_download_diagnostic(args.output_dir)
        if diagnostic is not None:
            print(f"SAFE ARTIFACT FETCH DIAGNOSTIC: {diagnostic}", file=sys.stderr)
        return 1
    print(
        "HISTORICAL REPLAY VERIFIED: artifact bytes and identities match GitHub API; "
        "exact-once coverage is complete; the original failed workflow and known skip-wrapper "
        "mismatch are faithfully reproduced. This replay is not a passing workflow."
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
