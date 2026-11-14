"""Adversarial checks for deterministic pytest sharding and GitHub evidence."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import urllib.error
import zipfile
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from tools.pytest_shard import (
    _canonical,
    _manifest_payload,
    assigned_nodeids,
    shard_for,
    validate_manifest,
)
from tools.pytest_shard_artifacts import FetchError, _download_zip, _json_request
from tools.pytest_shard_report import AggregateError, aggregate, main
from tools.pytest_totals import _XPASS_MESSAGE

_REPOSITORY = "asgareyvazi/Prog-Proc"
_WORKFLOW = "CI"
_SHA = "a" * 40
_RUN_ID = "42"
_ATTEMPT = "1"
_SHARDS = 2


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _zip(folder: Path, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for item in sorted(folder.iterdir()):
            if item.is_file():
                output.write(item, item.name)


def _job(name: str, job_id: int, steps: list[str], conclusion: str = "success") -> dict[str, Any]:
    return {
        "id": job_id,
        "run_id": int(_RUN_ID),
        "run_attempt": int(_ATTEMPT),
        "name": name,
        "workflow_name": _WORKFLOW,
        "head_sha": _SHA,
        "status": "completed",
        "conclusion": conclusion,
        "labels": ["ubuntu-26.04", "Linux", "X64"],
        "steps": [
            {"name": step, "status": "completed", "conclusion": conclusion} for step in steps
        ],
    }


def _fixture(tmp_path: Path, shard_count: int = _SHARDS) -> dict[str, Any]:
    nodeids = [f"tests/unit/test_{i}.py::test_case_{i}" for i in range(16)]
    manifest = _manifest_payload(
        nodeids,
        head_sha=_SHA,
        run_id=_RUN_ID,
        run_attempt=_ATTEMPT,
        repository=_REPOSITORY,
        workflow=_WORKFLOW,
        workflow_ref="asgareyvazi/Prog-Proc/.github/workflows/ci.yml@refs/heads/arena/5957b76d-prog-proc",
        pytest_config_sha256="c" * 64,
    )
    manifest.update(
        platform="linux", platform_system="Linux", implementation="CPython", python_version="3.14.1"
    )
    manifest.pop("manifest_sha256")
    manifest["manifest_sha256"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    payload_root = tmp_path / "payloads"
    collection_name = f"ubuntu2604-qt-collection-a{_ATTEMPT}"
    _write(payload_root / collection_name / "pytest-collection.json", manifest)
    jobs = [_job("Ubuntu 26.04 Qt full-suite collection", 10, ["Collect complete pytest universe"])]
    artifacts: list[dict[str, Any]] = []
    archive_root = tmp_path / "archives"
    specifications = [(collection_name, 101)]
    for index in range(shard_count):
        name = f"ubuntu2604-qt-shard-{index:02d}-of-{shard_count:02d}-a{_ATTEMPT}"
        assigned = assigned_nodeids(nodeids, index, shard_count)
        jobs.append(
            _job(
                f"Ubuntu 26.04 Qt full-suite shard {index} of {shard_count}",
                20 + index,
                ["Run sharded Qt-enabled full suite", "Validate shard pytest totals"],
            )
        )
        selection = {
            "schema_version": 2,
            "head_sha": _SHA,
            "repository": _REPOSITORY,
            "workflow": _WORKFLOW,
            "run_id": _RUN_ID,
            "run_attempt": _ATTEMPT,
            "manifest_sha256": manifest["manifest_sha256"],
            "assignment_algorithm": manifest["assignment_algorithm"],
            "shard_index": index,
            "shard_count": shard_count,
            "collection_count": len(nodeids),
            "assigned_count": len(assigned),
            "assigned_nodeids": assigned,
            "assigned_sha256": hashlib.sha256(_canonical({"nodeids": assigned})).hexdigest(),
            "deselected_count": len(nodeids) - len(assigned),
        }
        execution = {
            "schema_version": 2,
            "head_sha": _SHA,
            "repository": _REPOSITORY,
            "workflow": _WORKFLOW,
            "workflow_ref": manifest["workflow_ref"],
            "run_id": _RUN_ID,
            "run_attempt": _ATTEMPT,
            "manifest_sha256": manifest["manifest_sha256"],
            "assignment_algorithm": manifest["assignment_algorithm"],
            "shard_index": index,
            "shard_count": shard_count,
            "assigned_nodeids": assigned,
            "pytest_exit_status": 0,
            "reports": {
                nodeid: [
                    {"when": "setup", "outcome": "passed"},
                    {"when": "call", "outcome": "passed"},
                    {"when": "teardown", "outcome": "passed"},
                ]
                for nodeid in assigned
            },
        }
        folder = payload_root / name
        _write(folder / "observed-collection.json", manifest)
        _write(folder / "shard-selection.json", selection)
        _write(folder / "shard-execution.json", execution)
        _write(
            folder / "pytest-totals.json",
            {
                "collected": len(assigned),
                "passed": len(assigned),
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
                "exit": 0,
                "runtime": 3.25,
            },
        )
        (folder / "pytest-exit.txt").write_text("0\n", encoding="utf-8")
        (folder / "pytest-run-state.txt").write_text("COMPLETED exit=0\n", encoding="utf-8")
        (folder / "pytest-output.txt").write_text("synthetic fixture\n", encoding="utf-8")
        cases = "".join(
            f'<testcase classname="tests.unit.test_{i}" name="test_case_{i}" />'
            for i in range(len(nodeids))
            if nodeids[i] in assigned
        )
        (folder / "pytest-junit.xml").write_text(
            f'<testsuite tests="{len(assigned)}" failures="0" errors="0" skipped="0" time="3.25">{cases}</testsuite>',
            encoding="utf-8",
        )
        specifications.append((name, 201 + index))
    for name, artifact_id in specifications:
        archive = archive_root / f"{artifact_id}.zip"
        _zip(payload_root / name, archive)
        artifacts.append(
            {
                "id": artifact_id,
                "name": name,
                "digest": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
                "size_in_bytes": archive.stat().st_size,
                "expired": False,
                "workflow_run": {"id": int(_RUN_ID), "head_sha": _SHA, "repository_id": 99},
            }
        )
    run_path, jobs_path, artifacts_path = (
        tmp_path / "run.json",
        tmp_path / "jobs.json",
        tmp_path / "artifacts.json",
    )
    _write(
        run_path,
        {
            "id": int(_RUN_ID),
            "run_attempt": int(_ATTEMPT),
            "head_sha": _SHA,
            "name": _WORKFLOW,
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "repository": {"id": 99, "full_name": _REPOSITORY},
        },
    )
    _write(jobs_path, {"total_count": len(jobs), "jobs": jobs})
    _write(artifacts_path, {"total_count": len(artifacts), "artifacts": artifacts})
    return {
        "manifest_path": tmp_path / "input-manifest.json",
        "manifest": manifest,
        "run_path": run_path,
        "jobs_path": jobs_path,
        "artifacts_path": artifacts_path,
        "payloads": payload_root,
        "archive_root": archive_root,
        "artifacts": artifacts,
        "jobs": jobs,
        "sha": _SHA,
        "run_id": _RUN_ID,
        "attempt": _ATTEMPT,
        "repository": _REPOSITORY,
        "workflow": _WORKFLOW,
        "shard_count": shard_count,
    }


def _run(
    tmp_path: Path, fixture: dict[str, Any], *, allow_in_progress_run: bool = False
) -> dict[str, Any]:
    return aggregate(
        manifest_path=None,
        shard_root=tmp_path / "unused",
        archive_root=fixture["archive_root"],
        extracted_root=tmp_path / "extracted",
        run_path=fixture["run_path"],
        jobs_path=fixture["jobs_path"],
        artifacts_path=fixture["artifacts_path"],
        expected_sha=fixture["sha"],
        repository=fixture["repository"],
        workflow=fixture["workflow"],
        run_id=fixture["run_id"],
        attempt=fixture["attempt"],
        shard_count=fixture["shard_count"],
        output_json=tmp_path / "report.json",
        output_markdown=tmp_path / "report.md",
        combined_junit=tmp_path / "combined.xml",
        combined_stats=tmp_path / "combined-stats.json",
        combined_output=tmp_path / "combined-output.txt",
        combined_exit=tmp_path / "combined-exit.txt",
        combined_state=tmp_path / "combined-state.txt",
        allow_in_progress_run=allow_in_progress_run,
    )


def _artifact(fixture: dict[str, Any], name: str) -> dict[str, Any]:
    return next(row for row in fixture["artifacts"] if row["name"] == name)


def _folder(fixture: dict[str, Any], index: int) -> Path:
    return (
        fixture["payloads"]
        / f"ubuntu2604-qt-shard-{index:02d}-of-{fixture['shard_count']:02d}-a{_ATTEMPT}"
    )


def _refresh(fixture: dict[str, Any], index: int) -> None:
    name = f"ubuntu2604-qt-shard-{index:02d}-of-{fixture['shard_count']:02d}-a{_ATTEMPT}"
    artifact = _artifact(fixture, name)
    archive = fixture["archive_root"] / f"{artifact['id']}.zip"
    _zip(fixture["payloads"] / name, archive)
    artifact["size_in_bytes"] = archive.stat().st_size
    artifact["digest"] = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    _write(fixture["artifacts_path"], {"artifacts": fixture["artifacts"]})


def test_sharding_is_stable_complete_disjoint_and_balanced_for_2860_ids() -> None:
    ids = [f"tests/test_{i}.py::test_case[{i}]" for i in range(2860)]
    partitions = [assigned_nodeids(ids, i, 8) for i in range(8)]
    reversed_partitions = [assigned_nodeids(list(reversed(ids)), i, 8) for i in range(8)]
    assert partitions == [list(reversed(part)) for part in reversed_partitions]
    flattened = [nodeid for part in partitions for nodeid in part]
    assert len(flattened) == len(set(flattened)) == len(ids)
    assert set(flattened) == set(ids)
    assert max(map(len, partitions)) - min(map(len, partitions)) < 100


def test_partition_edges_invalid_values_and_unicode() -> None:
    assert [assigned_nodeids([], i, 4) for i in range(4)] == [[], [], [], []]
    assert sum(bool(assigned_nodeids(["only"], i, 8)) for i in range(8)) == 1
    ids = ["tests/نامۀ_آزمون.py::test_کابل[δ]", "x" * 10000]
    assert sorted(n for i in range(3) for n in assigned_nodeids(ids, i, 3)) == sorted(ids)
    with pytest.raises(ValueError, match="positive integer"):
        shard_for("x", True)
    with pytest.raises(TypeError, match="integer"):
        assigned_nodeids([], False, 2)
    with pytest.raises(ValueError, match="within the shard count"):
        assigned_nodeids([], 2, 2)
    with pytest.raises(ValueError, match="non-empty"):
        shard_for("", 1)


def test_manifest_digest_identity_and_boolean_node_count() -> None:
    manifest = _manifest_payload(["one"], head_sha="sha", run_id="7", run_attempt="1")
    assert validate_manifest(manifest, expected_sha="sha", expected_run_id="7") == manifest
    bad = dict(manifest, node_count=True)
    bad.pop("manifest_sha256")
    bad["manifest_sha256"] = hashlib.sha256(_canonical(bad)).hexdigest()
    with pytest.raises(ValueError, match="node count"):
        validate_manifest(bad)
    with pytest.raises(ValueError, match="digest"):
        validate_manifest(dict(manifest, node_count=2))
    with pytest.raises(ValueError, match="repository"):
        validate_manifest(manifest, expected_repository="other/repo")
    with pytest.raises(ValueError, match="duplicate"):
        _manifest_payload(["same", "same"], head_sha="x", run_id="1", run_attempt="1")


def test_real_api_metadata_archives_and_exact_once_coverage_pass(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    result = _run(tmp_path, fixture)
    assert result["status"] == "PASS"
    assert result["complete"] is True
    assert result["collected_nodeids"] == result["covered_nodeids"] == 16
    assert result["totals"]["passed"] == 16
    assert len(result["shards"]) == 2
    assert all(s["artifact_digest"].startswith("sha256:") for s in result["shards"])
    assert (tmp_path / "combined-exit.txt").read_text().strip() == "0"


def test_in_progress_is_incomplete_unless_report_job_explicitly_allows_pending_run(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    run = json.loads(fixture["run_path"].read_text())
    run.update(status="in_progress", conclusion=None)
    _write(fixture["run_path"], run)
    incomplete = _run(tmp_path, fixture)
    assert incomplete["status"] == "INCOMPLETE"
    assert incomplete["complete"] is False
    other = tmp_path / "allowed"
    pending = _fixture(other)
    run = json.loads(pending["run_path"].read_text())
    run.update(status="in_progress", conclusion=None)
    _write(pending["run_path"], run)
    result = _run(other, pending, allow_in_progress_run=True)
    assert result["status"] == "PASS"
    assert result["workflow_conclusion_verified"] is False
    assert result["workflow_run_completion_pending"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "b" * 40),
        ("run_attempt", 2),
        ("name", "other"),
        ("path", ".github/workflows/ci-windows.yml"),
        ("status", "cancelled"),
    ],
)
def test_wrong_or_cancelled_run_identity_never_qualifies(
    tmp_path: Path, field: str, value: Any
) -> None:
    fixture = _fixture(tmp_path)
    run = json.loads(fixture["run_path"].read_text())
    run[field] = value
    _write(fixture["run_path"], run)
    with pytest.raises(AggregateError):
        _run(tmp_path, fixture)


def test_job_and_artifact_identity_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["jobs"][1]["head_sha"] = "b" * 40
    _write(fixture["jobs_path"], {"jobs": fixture["jobs"]})
    with pytest.raises(AggregateError, match="another workflow run"):
        _run(tmp_path, fixture)


def test_missing_jobs_artifacts_and_expired_artifact_are_incomplete(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _write(fixture["jobs_path"], {"jobs": fixture["jobs"][:2]})
    with pytest.raises(AggregateError) as missing_job:
        _run(tmp_path, fixture)
    assert missing_job.value.status == "INCOMPLETE"
    other = tmp_path / "artifact"
    fixture2 = _fixture(other)
    fixture2["artifacts"][:] = [
        a for a in fixture2["artifacts"] if a["name"] != "ubuntu2604-qt-shard-01-of-02-a1"
    ]
    _write(fixture2["artifacts_path"], {"artifacts": fixture2["artifacts"]})
    with pytest.raises(AggregateError) as missing_artifact:
        _run(other, fixture2)
    assert missing_artifact.value.status == "INCOMPLETE"
    other2 = tmp_path / "expired"
    fixture3 = _fixture(other2)
    _artifact(fixture3, "ubuntu2604-qt-shard-00-of-02-a1")["expired"] = True
    _write(fixture3["artifacts_path"], {"artifacts": fixture3["artifacts"]})
    with pytest.raises(AggregateError, match="expired"):
        _run(other2, fixture3)


def test_archive_digest_size_and_foreign_run_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _artifact(fixture, "ubuntu2604-qt-shard-00-of-02-a1")["digest"] = "sha256:" + "0" * 64
    _write(fixture["artifacts_path"], {"artifacts": fixture["artifacts"]})
    with pytest.raises(AggregateError, match="SHA-256"):
        _run(tmp_path, fixture)
    size_dir = tmp_path / "wrong-size"
    size_fixture = _fixture(size_dir)
    _artifact(size_fixture, "ubuntu2604-qt-shard-00-of-02-a1")["size_in_bytes"] += 1
    _write(size_fixture["artifacts_path"], {"artifacts": size_fixture["artifacts"]})
    with pytest.raises(AggregateError, match="size differs"):
        _run(size_dir, size_fixture)
    other = tmp_path / "foreign"
    fixture2 = _fixture(other)
    _artifact(fixture2, "ubuntu2604-qt-shard-00-of-02-a1")["workflow_run"]["head_sha"] = "b" * 40
    _write(fixture2["artifacts_path"], {"artifacts": fixture2["artifacts"]})
    with pytest.raises(AggregateError, match="another run or SHA"):
        _run(other, fixture2)


def test_duplicate_ids_and_unexpected_attempt_artifacts_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _artifact(fixture, "ubuntu2604-qt-shard-00-of-02-a1")["id"] = 101
    _write(fixture["artifacts_path"], {"artifacts": fixture["artifacts"]})
    with pytest.raises(AggregateError, match="duplicate API IDs"):
        _run(tmp_path, fixture)

    other = tmp_path / "unexpected"
    fixture2 = _fixture(other)
    fixture2["artifacts"].append(
        {
            **_artifact(fixture2, "ubuntu2604-qt-shard-00-of-02-a1"),
            "id": 999,
            "name": "ubuntu2604-qt-shard-99-of-02-a1",
        }
    )
    _write(fixture2["artifacts_path"], {"artifacts": fixture2["artifacts"]})
    with pytest.raises(AggregateError, match="unexpected shard artifacts"):
        _run(other, fixture2)


def test_cancelled_shard_is_incomplete_and_failed_test_is_fail(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["jobs"][1]["conclusion"] = "cancelled"
    _write(fixture["jobs_path"], {"jobs": fixture["jobs"]})
    result = _run(tmp_path, fixture)
    assert result["status"] == "INCOMPLETE"
    other = tmp_path / "failed"
    fixture2 = _fixture(other)
    job = fixture2["jobs"][1]
    job["conclusion"] = "failure"
    job["steps"][-1]["conclusion"] = "failure"
    _write(fixture2["jobs_path"], {"jobs": fixture2["jobs"]})
    folder = _folder(fixture2, 0)
    execution = json.loads((folder / "shard-execution.json").read_text())
    nodeid = next(iter(execution["reports"]))
    execution["pytest_exit_status"] = 1
    execution["reports"][nodeid] = [
        {"when": "setup", "outcome": "passed"},
        {"when": "call", "outcome": "failed"},
    ]
    _write(folder / "shard-execution.json", execution)
    stats = json.loads((folder / "pytest-totals.json").read_text())
    stats.update(passed=stats["passed"] - 1, failed=1, exit=1)
    _write(folder / "pytest-totals.json", stats)
    (folder / "pytest-exit.txt").write_text("1\n")
    (folder / "pytest-run-state.txt").write_text("COMPLETED exit=1\n")
    junit = (folder / "pytest-junit.xml").read_text()
    junit = junit.replace('failures="0"', 'failures="1"', 1)
    case = f'<testcase classname="tests.unit.test_{nodeid.split("test_case_")[1].split(".py")[0]}" name="{nodeid.split("::")[-1]}" />'
    junit = junit.replace(case, case[:-3] + '><failure message="synthetic" /></testcase>')
    (folder / "pytest-junit.xml").write_text(junit)
    _refresh(fixture2, 0)
    failed = _run(other, fixture2)
    assert failed["status"] == "FAIL"
    assert failed["totals"]["failed"] == 1


def test_any_skip_or_xfail_prevents_pass(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    folder = _folder(fixture, 0)
    execution = json.loads((folder / "shard-execution.json").read_text())
    nodeid = next(iter(execution["reports"]))
    execution["reports"][nodeid] = [
        {"when": "setup", "outcome": "passed"},
        {"when": "call", "outcome": "skipped", "skip_reason": "platform"},
    ]
    _write(folder / "shard-execution.json", execution)
    stats = json.loads((folder / "pytest-totals.json").read_text())
    stats.update(passed=stats["passed"] - 1, skipped=1)
    _write(folder / "pytest-totals.json", stats)
    junit = (folder / "pytest-junit.xml").read_text().replace('skipped="0"', 'skipped="1"', 1)
    index = int(nodeid.split("test_case_")[1].split(".py")[0])
    case = f'<testcase classname="tests.unit.test_{index}" name="test_case_{index}" />'
    junit = junit.replace(case, case[:-3] + '><skipped message="platform" /></testcase>')
    (folder / "pytest-junit.xml").write_text(junit)
    _refresh(fixture, 0)
    result = _run(tmp_path, fixture)
    assert result["status"] == "FAIL"
    assert result["totals"]["skipped"] == 1


def test_wrong_partition_missing_reports_and_bad_process_state_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    folder = _folder(fixture, 0)
    selection = json.loads((folder / "shard-selection.json").read_text())
    selection["assigned_nodeids"].pop()
    selection["assigned_count"] -= 1
    selection["assigned_sha256"] = hashlib.sha256(
        _canonical({"nodeids": selection["assigned_nodeids"]})
    ).hexdigest()
    _write(folder / "shard-selection.json", selection)
    _refresh(fixture, 0)
    with pytest.raises(AggregateError, match="deterministic assignment"):
        _run(tmp_path, fixture)
    other = tmp_path / "missing"
    fixture2 = _fixture(other)
    folder2 = _folder(fixture2, 0)
    (folder2 / "pytest-exit.txt").write_text("", encoding="utf-8")
    _refresh(fixture2, 0)
    with pytest.raises(AggregateError) as incomplete:
        _run(other, fixture2)
    assert incomplete.value.status == "INCOMPLETE"


def test_zip_path_traversal_and_symlinks_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    artifact = _artifact(fixture, "ubuntu2604-qt-shard-00-of-02-a1")
    archive = fixture["archive_root"] / f"{artifact['id']}.zip"
    folder = fixture["payloads"] / artifact["name"]
    with zipfile.ZipFile(archive, "w") as out:
        for item in folder.iterdir():
            out.write(item, item.name)
        out.writestr("../escape", "no")
    artifact["size_in_bytes"] = archive.stat().st_size
    artifact["digest"] = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    _write(fixture["artifacts_path"], {"artifacts": fixture["artifacts"]})
    with pytest.raises(AggregateError, match="traversal"):
        _run(tmp_path, fixture)
    other = tmp_path / "symlink"
    fixture2 = _fixture(other)
    artifact2 = _artifact(fixture2, "ubuntu2604-qt-shard-00-of-02-a1")
    archive2 = fixture2["archive_root"] / f"{artifact2['id']}.zip"
    folder2 = fixture2["payloads"] / artifact2["name"]
    with zipfile.ZipFile(archive2, "w") as out:
        for item in folder2.iterdir():
            out.write(item, item.name)
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        out.writestr(info, "pytest-junit.xml")
    artifact2["size_in_bytes"] = archive2.stat().st_size
    artifact2["digest"] = "sha256:" + hashlib.sha256(archive2.read_bytes()).hexdigest()
    _write(fixture2["artifacts_path"], {"artifacts": fixture2["artifacts"]})
    with pytest.raises(AggregateError, match="symlink"):
        _run(other, fixture2)


def test_pytest_plugin_emits_exact_once_real_execution_records(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_smoke.py").write_text(
        "def test_one(): assert True\ndef test_two(): assert True\ndef test_three(): assert True\ndef test_four(): assert True\n"
    )
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    base = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-o",
        "addopts=",
        "--rootdir",
        str(tmp_path),
        "-p",
        "tools.pytest_shard",
    ]
    canonical = tmp_path / "manifest.json"
    first = subprocess.run(
        [
            *base,
            "--progproc-collection-manifest",
            str(canonical),
            "--progproc-head-sha",
            "LOCAL",
            "--progproc-run-id",
            "LOCAL",
            "--progproc-run-attempt",
            "1",
            "--progproc-repository",
            "LOCAL",
            "--progproc-workflow",
            "LOCAL",
            "--progproc-workflow-ref",
            "LOCAL",
            str(suite),
        ],
        cwd=root,
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    manifest = json.loads(canonical.read_text())
    observed: list[str] = []
    for index in range(2):
        selection, result = tmp_path / f"selection-{index}.json", tmp_path / f"result-{index}.json"
        cmd = [
            *base,
            "--progproc-collection-manifest",
            str(tmp_path / f"observed-{index}.json"),
            "--progproc-expected-manifest",
            str(canonical),
            "--progproc-selection-result",
            str(selection),
            "--progproc-shard-result",
            str(result),
            "--progproc-shard-index",
            str(index),
            "--progproc-shard-count",
            "2",
            "--progproc-head-sha",
            "LOCAL",
            "--progproc-run-id",
            "LOCAL",
            "--progproc-run-attempt",
            "1",
            "--progproc-repository",
            "LOCAL",
            "--progproc-workflow",
            "LOCAL",
            "--progproc-workflow-ref",
            "LOCAL",
            str(suite),
        ]
        completed = subprocess.run(
            cmd, cwd=root, check=False, env=env, capture_output=True, text=True
        )
        assert completed.returncode == 0, completed.stderr
        selected, executed = json.loads(selection.read_text()), json.loads(result.read_text())
        assert selected["assigned_nodeids"] == executed["assigned_nodeids"]
        assert set(executed["reports"]) == set(executed["assigned_nodeids"])
        observed.extend(executed["assigned_nodeids"])
    assert len(observed) == len(set(observed)) == manifest["node_count"]
    assert set(observed) == set(manifest["nodeids"])


def test_github_json_api_host_guard_precedes_network() -> None:
    with patch("tools.pytest_shard_artifacts.urllib.request.build_opener") as opener:
        with pytest.raises(FetchError, match="non-GitHub API URL"):
            _json_request("https://attacker.example/api", "secret")
        opener.assert_not_called()


def test_artifact_redirect_does_not_forward_token_and_requires_https(tmp_path: Path) -> None:
    headers = Message()
    headers["Location"] = "https://signed.example/artifact.zip?sig=opaque"
    redirect = urllib.error.HTTPError(
        "https://api.github.com/artifact", 302, "Found", headers, io.BytesIO()
    )

    class Redirecting:
        def open(self, request: Any, timeout: int) -> Any:
            assert request.get_header("Authorization") == "Bearer token"
            raise redirect

    response = io.BytesIO(b"synthetic zip bytes")
    captured: dict[str, Any] = {}

    def download(request: Any, timeout: int) -> io.BytesIO:
        captured["request"] = request
        return response

    dest = tmp_path / "artifact.zip"
    with (
        patch(
            "tools.pytest_shard_artifacts.urllib.request.build_opener", return_value=Redirecting()
        ),
        patch("tools.pytest_shard_artifacts.urllib.request.urlopen", side_effect=download),
    ):
        size, digest = _download_zip(
            "https://api.github.com/repos/a/b/actions/artifacts/1/zip", "token", dest
        )
    assert size == len(b"synthetic zip bytes")
    assert digest == hashlib.sha256(dest.read_bytes()).hexdigest()
    assert captured["request"].get_header("Authorization") is None
    bad_headers = Message()
    bad_headers["Location"] = "http://bad.example/artifact"
    bad = urllib.error.HTTPError(
        "https://api.github.com/artifact", 302, "Found", bad_headers, io.BytesIO()
    )

    class BadRedirect:
        def open(self, request: Any, timeout: int) -> Any:
            raise bad

    with (
        patch(
            "tools.pytest_shard_artifacts.urllib.request.build_opener", return_value=BadRedirect()
        ),
        pytest.raises(FetchError, match="insecure or malformed"),
    ):
        _download_zip(
            "https://api.github.com/repos/a/b/actions/artifacts/2/zip",
            "token",
            tmp_path / "bad.zip",
        )


def test_cli_creates_classified_failure_report_for_missing_run_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_json, out_md = tmp_path / "failure.json", tmp_path / "failure.md"
    args = [
        "--shard-root",
        str(tmp_path),
        "--archive-root",
        str(tmp_path),
        "--extracted-root",
        str(tmp_path / "extract"),
        "--run",
        str(tmp_path / "absent-run.json"),
        "--jobs",
        str(tmp_path / "jobs.json"),
        "--artifacts",
        str(tmp_path / "artifacts.json"),
        "--sha",
        _SHA,
        "--repository",
        _REPOSITORY,
        "--workflow",
        _WORKFLOW,
        "--run-id",
        _RUN_ID,
        "--attempt",
        _ATTEMPT,
        "--shard-count",
        "2",
        "--output-json",
        str(out_json),
        "--output-markdown",
        str(out_md),
        "--combined-junit",
        str(tmp_path / "combined.xml"),
        "--combined-stats",
        str(tmp_path / "stats.json"),
        "--combined-output",
        str(tmp_path / "output.txt"),
        "--combined-exit",
        str(tmp_path / "exit.txt"),
        "--combined-state",
        str(tmp_path / "state.txt"),
    ]
    assert main(args) == 1
    report = json.loads(out_json.read_text())
    assert report["status"] == "INCOMPLETE" and report["complete"] is False
    assert "INCOMPLETE" in capsys.readouterr().err


def test_xpass_is_not_qualified(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    folder = _folder(fixture, 0)
    execution = json.loads((folder / "shard-execution.json").read_text())
    nodeid = next(iter(execution["reports"]))
    execution["reports"][nodeid] = [
        {"when": "setup", "outcome": "passed"},
        {"when": "call", "outcome": "passed", "wasxfail": "expected failure"},
    ]
    _write(folder / "shard-execution.json", execution)
    stats = json.loads((folder / "pytest-totals.json").read_text())
    stats.update(passed=stats["passed"] - 1, xpassed=1)
    _write(folder / "pytest-totals.json", stats)
    junit = (folder / "pytest-junit.xml").read_text().replace('skipped="0"', 'skipped="1"', 1)
    index = int(nodeid.split("test_case_")[1].split(".py")[0])
    case = f'<testcase classname="tests.unit.test_{index}" name="test_case_{index}" />'
    junit = junit.replace(case, case[:-3] + f'><skipped message="{_XPASS_MESSAGE}" /></testcase>')
    (folder / "pytest-junit.xml").write_text(junit)
    _refresh(fixture, 0)
    assert _run(tmp_path, fixture)["status"] == "FAIL"
