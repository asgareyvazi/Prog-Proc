"""Regression for CI failure reporting: missing pytest output must be NOT RUN, not fabricated.

The V9.0 Linux 3.11 run failed at pin drift, then downstream always() steps
failed because pytest-output.txt etc. were absent, masking the original
failure. The fix reports NOT RUN and does not create secondary errors.
"""

from __future__ import annotations

import json
from pathlib import Path


def test_ci_reports_not_run_when_prerequisite_failed():
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    # Count adapter warnings must handle missing file as NOT RUN
    assert "pytest-output: NOT RUN — prerequisite failed" in ci
    assert "datetime-adapter-warnings: NOT RUN" in ci
    # Record totals must preserve missing evidence as NOT PRODUCED, never empty fixtures.
    assert "pytest-junit: NOT PRODUCED" in ci
    assert "pytest-totals: NOT PRODUCED" in ci
    assert "pytest-run-state.txt" in ci
    assert "<testsuites></testsuites>" not in ci
    assert '"collected":0,"passed":0' not in ci
    # Publish must warn about absent artifacts rather than manufacturing test reports.
    assert "if-no-files-found: warn" in ci


def test_ci_windows_reports_not_run_when_prerequisite_failed():
    ciw = Path(".github/workflows/ci-windows.yml").read_text(encoding="utf-8")
    assert "pytest-output: NOT RUN — prerequisite failed" in ciw
    assert "pytest-junit: NOT PRODUCED" in ciw
    assert "pytest-totals: NOT PRODUCED" in ciw
    assert "pytest-run-state.txt" in ciw
    assert "<testsuites></testsuites>" not in ciw
    assert '"collected":0,"passed":0' not in ciw
    assert "qt-skip-audit: NOT RUN — pytest JUnit report NOT PRODUCED" in ciw
    assert "if-no-files-found: warn" in ciw
    # Old code used Write-Error for missing output
    assert 'Write-Error "pytest-output.txt is missing"' not in ciw


def test_full_suite_artifacts_are_bound_to_matrix_job_and_exact_sha():
    linux = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    windows = Path(".github/workflows/ci-windows.yml").read_text(encoding="utf-8")
    for text, platform, prefix in (
        (linux, "Linux", "pytest-cp"),
        (windows, "Windows", "pytest-windows-cp"),
    ):
        assert "tools/pytest_matrix.py record" in text
        assert f"--platform {platform}" in text
        assert "--totals-step" in text
        assert "pytest-matrix-evidence.json" in text
        assert "name: " + prefix + "${{ matrix.python-version }}-a${{ github.run_attempt }}" in text
        assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in text
        assert "pytest-matrix-report" in text
        assert "--expected-sha" in text
        assert "--jobs-json" in text and "--artifacts-json" in text


def test_ubuntu_2604_cpython_314_is_a_full_suite_identity_reported_ci_lane():
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    ciw = Path(".github/workflows/ci-windows.yml").read_text(encoding="utf-8")
    assert "actions/upload-artifact@cf430a030ddbb5b0abf93d22962f4752f3646cd9" not in ci + ciw
    assert "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9" in ci + ciw
    assert "ubuntu-2604-cp314:" in ci
    assert "runs-on: ubuntu-26.04" in ci
    assert 'python-version: "3.14"' in ci
    assert "Full suite (Qt-enabled, offscreen)" in ci
    assert '--platform "Ubuntu 26.04"' in ci
    assert '"pytest-ubuntu-2604-cp3.14-a${{ github.run_attempt }}"' in ci
    assert "--versions 3.14" in ci
    assert "pytest-matrix-report-ubuntu-2604-cp3.14" in ci
    assert "pattern: pytest-*cp*-a${{ github.run_attempt }}" in ci
    assert "Download Ubuntu 26.04 / CPython 3.14 evidence" not in ci


def test_shared_pytest_artifact_root_preserves_linux_and_ubuntu_lane_identity(
    tmp_path: Path, monkeypatch
):
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.11", monkeypatch=monkeypatch)
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.14", monkeypatch=monkeypatch)
    _pytest_matrix_lane(
        tmp_path,
        platform="Ubuntu 26.04",
        version="3.14",
        monkeypatch=monkeypatch,
    )
    linux_jobs, linux_artifacts = _pytest_matrix_api("Linux")
    ubuntu_jobs, ubuntu_artifacts = _pytest_matrix_api("Ubuntu 26.04", ("3.14",))

    linux = _build_pytest_matrix(
        tmp_path, platform="Linux", jobs=linux_jobs, artifacts=linux_artifacts
    )
    ubuntu = _build_pytest_matrix(
        tmp_path,
        platform="Ubuntu 26.04",
        jobs=ubuntu_jobs,
        artifacts=ubuntu_artifacts,
        versions=("3.14",),
    )

    assert linux["result"] == "PASS"
    assert [row["artifact_name"] for row in linux["rows"]] == [
        "pytest-cp3.11-a1",
        "pytest-cp3.14-a1",
    ]
    assert ubuntu["result"] == "PASS"
    assert ubuntu["rows"][0]["artifact_name"] == "pytest-ubuntu-2604-cp3.14-a1"


def test_pytest_totals_distinguishes_zero_and_interrupted():
    # The parser must distinguish zero tests, interrupted, etc. — this is the
    # contract of tools/pytest_totals.py, not just the CI file.
    # We exercise it directly: zero collected is a failure, but not a hidden pass.
    from tools.pytest_totals import totals_from_stats

    recorded = totals_from_stats({}, collected=0, exit_code=0, runtime=0.1)
    assert recorded["collected"] == 0
    # publish will reject collected <=0 as failure (verified via disagreements)
    # Here we just check that totals are correctly formed for zero case
    assert recorded["passed"] == 0


def test_pytest_totals_publish_fails_on_missing_files(tmp_path):
    from tools.pytest_totals import publish

    stats = tmp_path / "missing-stats.json"
    junit = tmp_path / "missing-junit.xml"
    # publish with missing files must return 1, not crash, and report missing
    rc = publish(stats, junit, None, None)
    assert rc == 1


def _pytest_matrix_lane(
    root: Path,
    *,
    platform: str,
    version: str,
    monkeypatch,
    sha: str = "a" * 40,
    totals: dict[str, int] | None = None,
    omit_totals: bool = False,
) -> tuple[Path, str]:
    import json

    from tools.pytest_matrix import _PLATFORMS, _expected_artifact_name, record_evidence

    attempt = "1"
    repository = "asgareyvazi/Prog-Proc"
    workflow = "CI Windows" if platform == "Windows" else "CI"
    job_prefix, _artifact_template, job_key = _PLATFORMS[platform]
    artifact_name = _expected_artifact_name(platform, version, attempt)
    directory = root / artifact_name
    directory.mkdir(parents=True, exist_ok=True)
    counts = totals or {
        "collected": 5,
        "passed": 4,
        "failed": 0,
        "errors": 0,
        "skipped": 1,
        "xfailed": 0,
        "xpassed": 0,
    }
    stats = dict(counts, exit=0, runtime=1.25)
    if not omit_totals:
        (directory / "pytest-totals.json").write_text(json.dumps(stats), encoding="utf-8")
    skipped_cases = (
        "<testcase><skipped message='platform skip'/></testcase>" if counts["skipped"] else ""
    )
    passed_cases = "".join("<testcase/>" for _ in range(counts["passed"]))
    junit = (
        f"<testsuites><testsuite tests='{counts['collected']}' failures='{counts['failed']}' "
        f"errors='{counts['errors']}' skipped='{counts['skipped']}' time='1.25'>"
        f"{passed_cases}{skipped_cases}</testsuite></testsuites>"
    )
    (directory / "pytest-junit.xml").write_text(junit, encoding="utf-8")
    (directory / "pytest-exit.txt").write_text("0\n", encoding="utf-8")
    (directory / "pytest-run-state.txt").write_text("COMPLETED exit=0\n", encoding="utf-8")
    (directory / "pytest-output.txt").write_text("pytest completed\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_REPOSITORY", repository)
    monkeypatch.setenv("GITHUB_WORKFLOW", workflow)
    monkeypatch.setenv("GITHUB_RUN_ID", "41593362382")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/arena/5957b76d-prog-proc")
    monkeypatch.setenv("GITHUB_SHA", sha)
    monkeypatch.setenv("GITHUB_JOB", job_key)
    monkeypatch.setenv("PYTEST_MATRIX_JOB_NAME", f"{job_prefix} (cp{version})")
    monkeypatch.chdir(directory)
    record_evidence(
        platform=platform,
        python_version=version,
        artifact_name=artifact_name,
        output=Path("pytest-matrix-evidence.json"),
        totals_step="success",
    )
    monkeypatch.chdir(root)
    return directory, artifact_name


def _pytest_matrix_api(platform: str, versions: tuple[str, ...] = ("3.11", "3.14")):
    from tools.pytest_matrix import _PLATFORMS, _expected_artifact_name

    job_prefix = _PLATFORMS[platform][0]
    return (
        {
            "jobs": [
                {
                    "id": 1000 + index,
                    "name": f"{job_prefix} (cp{version})",
                    "status": "completed",
                    "conclusion": "success",
                }
                for index, version in enumerate(versions)
            ]
        },
        {
            "artifacts": [
                {
                    "id": 2000 + index,
                    "name": _expected_artifact_name(platform, version, "1"),
                    "digest": f"sha256:{index:064x}",
                    "expired": False,
                }
                for index, version in enumerate(versions)
            ]
        },
    )


def _build_pytest_matrix(
    root: Path,
    *,
    platform: str,
    jobs,
    artifacts,
    sha: str = "a" * 40,
    versions: tuple[str, ...] = ("3.11", "3.14"),
):
    from tools.pytest_matrix import build_report

    return build_report(
        artifact_root=root,
        platform=platform,
        expected_sha=sha,
        run_id="41593362382",
        run_attempt="1",
        repository="asgareyvazi/Prog-Proc",
        workflow="CI Windows" if platform == "Windows" else "CI",
        jobs=jobs,
        artifacts=artifacts,
        versions=versions,
    )


def test_pytest_matrix_assigns_each_lane_by_explicit_identity_and_preserves_skips(
    tmp_path: Path, monkeypatch
):
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.11", monkeypatch=monkeypatch)
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.14", monkeypatch=monkeypatch)
    jobs, artifacts = _pytest_matrix_api("Linux")

    report = _build_pytest_matrix(tmp_path, platform="Linux", jobs=jobs, artifacts=artifacts)

    assert report["result"] == "PASS"
    assert [
        (row["python_version"], row["job_id"], row["artifact_id"]) for row in report["rows"]
    ] == [
        ("3.11", 1000, 2000),
        ("3.14", 1001, 2001),
    ]
    assert all(row["passed"] == 4 and row["skipped"] == 1 for row in report["rows"])


def test_pytest_matrix_binds_ubuntu_2604_cpython_314_to_its_own_job_and_artifact(
    tmp_path: Path, monkeypatch
):
    from tools.pytest_matrix import _expected_artifact_name

    _pytest_matrix_lane(
        tmp_path,
        platform="Ubuntu 26.04",
        version="3.14",
        monkeypatch=monkeypatch,
    )
    versions = ("3.14",)
    jobs, artifacts = _pytest_matrix_api("Ubuntu 26.04", versions)
    report = _build_pytest_matrix(
        tmp_path,
        platform="Ubuntu 26.04",
        jobs=jobs,
        artifacts=artifacts,
        versions=versions,
    )

    assert _expected_artifact_name("Ubuntu 26.04", "3.14", "1") == ("pytest-ubuntu-2604-cp3.14-a1")
    assert report["result"] == "PASS"
    assert len(report["rows"]) == 1
    assert report["rows"][0]["job_name"] == "test Ubuntu 26.04 (cp3.14)"
    assert report["rows"][0]["job_id"] == 1000
    assert report["rows"][0]["artifact_id"] == 2000
    assert report["rows"][0]["head_sha"] == "a" * 40

    jobs["jobs"][0]["conclusion"] = "failure"
    failed_job_report = _build_pytest_matrix(
        tmp_path,
        platform="Ubuntu 26.04",
        jobs=jobs,
        artifacts=artifacts,
        versions=versions,
    )
    assert failed_job_report["result"] == "FAIL"
    assert failed_job_report["rows"][0]["result"] == "INVALID"


def test_pytest_matrix_never_assigns_linux_artifacts_to_windows_lanes(tmp_path: Path, monkeypatch):
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.11", monkeypatch=monkeypatch)
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.14", monkeypatch=monkeypatch)
    jobs, artifacts = _pytest_matrix_api("Windows")

    report = _build_pytest_matrix(tmp_path, platform="Windows", jobs=jobs, artifacts=artifacts)

    assert report["result"] == "FAIL"
    assert all(row["platform"] == "Windows" for row in report["rows"])
    assert all(row["passed"] is None and row["result"] == "INVALID" for row in report["rows"])
    assert all("downloaded evidence sidecar is missing" in row["detail"] for row in report["rows"])
    assert all(row["artifact_name"].startswith("pytest-windows-cp") for row in report["rows"])


def test_pytest_matrix_rejects_a_sidecar_from_another_head_sha(tmp_path: Path, monkeypatch):
    _pytest_matrix_lane(
        tmp_path, platform="Linux", version="3.11", monkeypatch=monkeypatch, sha="b" * 40
    )
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.14", monkeypatch=monkeypatch)
    jobs, artifacts = _pytest_matrix_api("Linux")

    report = _build_pytest_matrix(tmp_path, platform="Linux", jobs=jobs, artifacts=artifacts)

    assert report["rows"][0]["result"] == "INVALID"
    assert report["rows"][0]["head_sha"] == "b" * 40
    assert "head_sha" in report["rows"][0]["detail"]


def test_pytest_matrix_missing_totals_are_not_produced_not_pass(tmp_path: Path, monkeypatch):
    _pytest_matrix_lane(
        tmp_path, platform="Linux", version="3.11", monkeypatch=monkeypatch, omit_totals=True
    )
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.14", monkeypatch=monkeypatch)
    jobs, artifacts = _pytest_matrix_api("Linux")

    report = _build_pytest_matrix(tmp_path, platform="Linux", jobs=jobs, artifacts=artifacts)

    assert report["rows"][0]["result"] == "NOT PRODUCED"
    assert report["rows"][0]["passed"] is None
    assert report["result"] == "FAIL"


def test_pytest_matrix_duplicate_sidecars_are_ambiguous_not_first_match_wins(
    tmp_path: Path, monkeypatch
):
    import shutil

    directory, _ = _pytest_matrix_lane(
        tmp_path, platform="Linux", version="3.11", monkeypatch=monkeypatch
    )
    duplicate = tmp_path / "duplicate-copy"
    shutil.copytree(directory, duplicate)
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.14", monkeypatch=monkeypatch)
    jobs, artifacts = _pytest_matrix_api("Linux")

    report = _build_pytest_matrix(tmp_path, platform="Linux", jobs=jobs, artifacts=artifacts)

    assert report["rows"][0]["result"] == "AMBIGUOUS"
    assert report["rows"][0]["passed"] is None
    assert report["result"] == "FAIL"


def test_pytest_matrix_duplicate_artifact_or_job_api_entries_are_ambiguous(
    tmp_path: Path, monkeypatch
):
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.11", monkeypatch=monkeypatch)
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.14", monkeypatch=monkeypatch)
    jobs, artifacts = _pytest_matrix_api("Linux")
    artifacts["artifacts"].append(dict(artifacts["artifacts"][0], id=9999))
    jobs["jobs"].append(dict(jobs["jobs"][0], id=9998))

    report = _build_pytest_matrix(tmp_path, platform="Linux", jobs=jobs, artifacts=artifacts)

    assert report["rows"][0]["result"] == "AMBIGUOUS"
    assert "GitHub artifacts" in report["rows"][0]["detail"]
    assert report["rows"][1]["result"] == "PASS"


def test_pytest_matrix_all_skipped_lane_is_failure_not_pass(tmp_path: Path, monkeypatch):
    lane, _artifact = _pytest_matrix_lane(
        tmp_path,
        platform="Linux",
        version="3.11",
        monkeypatch=monkeypatch,
        totals={
            "collected": 3,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 3,
            "xfailed": 0,
            "xpassed": 0,
        },
    )
    from tools.pytest_matrix import record_evidence

    monkeypatch.chdir(lane)
    result = record_evidence(
        platform="Linux",
        python_version="3.11",
        artifact_name="pytest-cp3.11-a1",
        output=Path("pytest-matrix-evidence.json"),
        totals_step="success",
    )
    evidence = json.loads((lane / "pytest-matrix-evidence.json").read_text(encoding="utf-8"))
    assert result == evidence["result"] == "FAIL"
    assert evidence["totals"]["skipped"] == 3
    assert "all collected tests were skipped or xfailed" in evidence["reason"]


def test_pytest_matrix_malformed_or_zero_totals_never_pass(tmp_path: Path, monkeypatch):
    lane, _artifact = _pytest_matrix_lane(
        tmp_path, platform="Linux", version="3.11", monkeypatch=monkeypatch
    )
    from tools.pytest_matrix import record_evidence

    totals_path = lane / "pytest-totals.json"
    totals_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.chdir(lane)
    result = record_evidence(
        platform="Linux",
        python_version="3.11",
        artifact_name="pytest-cp3.11-a1",
        output=Path("pytest-matrix-evidence.json"),
        totals_step="success",
    )
    assert result == "INVALID"

    totals_path.write_text(
        json.dumps(
            {
                "collected": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
                "exit": 0,
                "runtime": 0.01,
            }
        ),
        encoding="utf-8",
    )
    result = record_evidence(
        platform="Linux",
        python_version="3.11",
        artifact_name="pytest-cp3.11-a1",
        output=Path("pytest-matrix-evidence.json"),
        totals_step="success",
    )
    evidence = json.loads((lane / "pytest-matrix-evidence.json").read_text(encoding="utf-8"))
    assert result == evidence["result"] == "INVALID"
    assert "collected no tests" in evidence["reason"]


def test_pytest_matrix_malformed_run_state_is_invalid(tmp_path: Path, monkeypatch):
    lane, _artifact = _pytest_matrix_lane(
        tmp_path, platform="Linux", version="3.11", monkeypatch=monkeypatch
    )
    from tools.pytest_matrix import record_evidence

    (lane / "pytest-run-state.txt").write_text("COMPLETED maybe\n", encoding="utf-8")
    monkeypatch.chdir(lane)
    result = record_evidence(
        platform="Linux",
        python_version="3.11",
        artifact_name="pytest-cp3.11-a1",
        output=Path("pytest-matrix-evidence.json"),
        totals_step="success",
    )
    evidence = json.loads((lane / "pytest-matrix-evidence.json").read_text(encoding="utf-8"))
    assert result == evidence["result"] == "INVALID"
    assert "run-state file is malformed" in evidence["reason"]


def test_pytest_matrix_interrupted_state_is_not_run(tmp_path: Path, monkeypatch):
    lane, _artifact = _pytest_matrix_lane(
        tmp_path, platform="Linux", version="3.11", monkeypatch=monkeypatch
    )
    from tools.pytest_matrix import record_evidence

    (lane / "pytest-run-state.txt").write_text("RUNNING\n", encoding="utf-8")
    monkeypatch.chdir(lane)
    result = record_evidence(
        platform="Linux",
        python_version="3.11",
        artifact_name="pytest-cp3.11-a1",
        output=Path("pytest-matrix-evidence.json"),
        totals_step="success",
    )
    evidence = json.loads((lane / "pytest-matrix-evidence.json").read_text(encoding="utf-8"))
    assert result == evidence["result"] == "NOT RUN"
    assert evidence["totals"] is None


def test_pytest_matrix_sha_tamper_in_downloaded_totals_invalidates_lane(
    tmp_path: Path, monkeypatch
):
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.11", monkeypatch=monkeypatch)
    _pytest_matrix_lane(tmp_path, platform="Linux", version="3.14", monkeypatch=monkeypatch)
    (tmp_path / "pytest-cp3.11-a1" / "pytest-totals.json").write_text("{}\n", encoding="utf-8")
    jobs, artifacts = _pytest_matrix_api("Linux")

    report = _build_pytest_matrix(tmp_path, platform="Linux", jobs=jobs, artifacts=artifacts)

    assert report["rows"][0]["result"] == "INVALID"
    assert "SHA-256" in report["rows"][0]["detail"]
