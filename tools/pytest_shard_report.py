#!/usr/bin/env python3
"""Verify actual GitHub Actions shard jobs, API artifact metadata, ZIPs and tests."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import stat
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from tools.pytest_shard import assigned_nodeids, validate_manifest
from tools.pytest_totals import (
    disagreements,
    read_exit_file,
    totals_from_junit,
    unexpected_skips_qt_enabled,
)

_SCHEMA = 2
_TOTALS = ("collected", "passed", "failed", "errors", "skipped", "xfailed", "xpassed")
_WORKFLOW_PATH = ".github/workflows/ci.yml"
_COLLECTION_JOB = "Ubuntu 26.04 Qt full-suite collection"
_COLLECTION_STEP = "Collect complete pytest universe"
_SHARD_STEP = "Run sharded Qt-enabled full suite"
_TOTALS_STEP = "Validate shard pytest totals"
_COLLECTION_FILES = {"pytest-collection.json"}
_SHARD_FILES = {
    "observed-collection.json",
    "shard-selection.json",
    "shard-execution.json",
    "pytest-totals.json",
    "pytest-junit.xml",
    "pytest-exit.txt",
    "pytest-run-state.txt",
    "pytest-output.txt",
}
_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_UNCOMPRESSED = 512 * 1024 * 1024


class AggregateError(ValueError):
    """Fail-closed evidence issue with a workflow-safe classification."""

    def __init__(self, message: str, *, status: str = "INVALID EVIDENCE") -> None:
        super().__init__(message)
        self.status = status


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AggregateError(f"{label} was not produced", status="INCOMPLETE") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AggregateError(f"{label} is unreadable or malformed") from exc


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_ids(nodeids: list[str]) -> str:
    return hashlib.sha256(_canonical({"nodeids": nodeids})).hexdigest()


def _records(path: Path, key: str, label: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = _read_json(path, label)
    values = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise AggregateError(f"{label} has the wrong API response shape")
    return payload, values


def _run_identity(
    run: dict[str, Any], *, repository: str, workflow: str, sha: str, run_id: str, attempt: str
) -> int:
    repo = run.get("repository")
    if not isinstance(repo, dict) or repo.get("full_name") != repository:
        raise AggregateError("GitHub run repository identity does not match")
    repo_id = repo.get("id")
    if type(repo_id) is not int or repo_id < 1:
        raise AggregateError("GitHub run repository ID is missing or malformed")
    if not (
        str(run.get("id")) == str(run_id)
        and str(run.get("run_attempt")) == str(attempt)
        and run.get("head_sha") == sha
        and run.get("name") == workflow
        and run.get("path") == _WORKFLOW_PATH
    ):
        raise AggregateError(
            "GitHub workflow run identity does not match repository/workflow/SHA/run/attempt"
        )
    if run.get("status") == "cancelled" or run.get("conclusion") == "cancelled":
        raise AggregateError("GitHub workflow run was cancelled", status="INCOMPLETE")
    if not isinstance(run.get("status"), str) or run.get("status") not in {
        "in_progress",
        "completed",
    }:
        raise AggregateError(
            "GitHub workflow run is not in a reportable state", status="INCOMPLETE"
        )
    return repo_id


def _job(
    jobs: list[dict[str, Any]], *, name: str, workflow: str, sha: str, run_id: str, attempt: str
) -> dict[str, Any]:
    matches = [row for row in jobs if row.get("name") == name]
    if len(matches) != 1:
        raise AggregateError(f"expected exactly one GitHub job named {name!r}", status="INCOMPLETE")
    job = matches[0]
    if type(job.get("id")) is not int or job["id"] < 1:
        raise AggregateError(f"GitHub job {name!r} has no valid numeric API ID")
    if not (
        str(job.get("run_id")) == str(run_id)
        and str(job.get("run_attempt")) == str(attempt)
        and job.get("head_sha") == sha
        and job.get("workflow_name") == workflow
    ):
        raise AggregateError(f"GitHub job {name!r} is bound to another workflow run")
    labels = job.get("labels")
    if not isinstance(labels, list) or "ubuntu-26.04" not in labels:
        raise AggregateError(f"GitHub job {name!r} does not prove runner label ubuntu-26.04")
    return job


def _step_issues(
    job: dict[str, Any], required: tuple[str, ...], label: str
) -> tuple[list[str], list[str]]:
    failed: list[str] = []
    incomplete: list[str] = []
    steps = job.get("steps")
    for name in required:
        found = (
            [step for step in steps if isinstance(step, dict) and step.get("name") == name]
            if isinstance(steps, list)
            else []
        )
        if len(found) != 1 or found[0].get("status") != "completed":
            incomplete.append(f"{label}: required step {name!r} did not complete")
        elif found[0].get("conclusion") != "success":
            failed.append(
                f"{label}: required step {name!r} concluded {found[0].get('conclusion')!r}"
            )
    status = job.get("status")
    conclusion = job.get("conclusion")
    if (
        status != "completed"
        or not isinstance(conclusion, str)
        or conclusion in {"cancelled", "skipped", "timed_out"}
    ):
        incomplete.append(f"{label}: job status/conclusion is {status!r}/{conclusion!r}")
    elif conclusion != "success":
        failed.append(f"{label}: job concluded {conclusion!r}")
    return failed, incomplete


def _artifact(
    artifacts: list[dict[str, Any]], *, name: str, repo_id: int, sha: str, run_id: str, attempt: str
) -> dict[str, Any]:
    matches = [row for row in artifacts if row.get("name") == name]
    if len(matches) != 1:
        raise AggregateError(
            f"expected exactly one current-run artifact named {name!r}", status="INCOMPLETE"
        )
    artifact = matches[0]
    if type(artifact.get("id")) is not int or artifact["id"] < 1:
        raise AggregateError(f"artifact {name!r} has no valid numeric API ID")
    if artifact.get("expired") is not False:
        raise AggregateError(
            f"artifact {name!r} is expired or expiration is unknown", status="INCOMPLETE"
        )
    size = artifact.get("size_in_bytes")
    digest = artifact.get("digest")
    if (
        type(size) is not int
        or size < 1
        or not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest) is None
    ):
        raise AggregateError(f"artifact {name!r} has malformed API size or digest")
    provenance = artifact.get("workflow_run")
    if (
        not isinstance(provenance, dict)
        or type(provenance.get("repository_id")) is not int
        or provenance.get("repository_id") != repo_id
    ):
        raise AggregateError(f"artifact {name!r} has invalid repository provenance")
    if str(provenance.get("id")) != str(run_id) or provenance.get("head_sha") != sha:
        raise AggregateError(f"artifact {name!r} belongs to another run or SHA")
    return artifact


def _extract(
    artifact: dict[str, Any], *, archive_root: Path, extracted_root: Path, allowed: set[str]
) -> Path:
    archive = archive_root / f"{artifact['id']}.zip"
    if not archive.is_file():
        raise AggregateError(
            f"downloaded artifact archive {artifact['id']} is missing", status="INCOMPLETE"
        )
    if archive.stat().st_size != artifact["size_in_bytes"]:
        raise AggregateError(f"artifact archive {artifact['id']} size differs from GitHub API")
    if _sha256(archive) != artifact["digest"].split(":", 1)[1].lower():
        raise AggregateError(f"artifact archive {artifact['id']} SHA-256 differs from GitHub API")
    destination = extracted_root / str(artifact["name"])
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise AggregateError(
            f"artifact extraction destination {destination.name!r} already exists"
        ) from exc
    files: dict[str, bytes] = {}
    total = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                member = PurePosixPath(info.filename)
                if info.is_dir():
                    if member.is_absolute() or ".." in member.parts:
                        raise AggregateError("artifact ZIP contains an unsafe directory path")
                    continue
                if member.is_absolute() or ".." in member.parts or "\\" in info.filename:
                    raise AggregateError("artifact ZIP contains path traversal")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise AggregateError("artifact ZIP contains a symlink")
                name = member.name
                if name not in allowed or name in files:
                    raise AggregateError("artifact ZIP contains an unexpected or duplicate file")
                if info.file_size < 0 or info.file_size > _MAX_FILE_BYTES:
                    raise AggregateError("artifact ZIP member exceeds the per-file size limit")
                total += info.file_size
                if total > _MAX_UNCOMPRESSED:
                    raise AggregateError("artifact ZIP exceeds the expanded-size limit")
                files[name] = bundle.read(info)
    except AggregateError:
        raise
    except (OSError, EOFError, zipfile.BadZipFile, RuntimeError, ValueError, zlib.error) as exc:
        raise AggregateError(f"artifact ZIP {artifact['id']} is unreadable") from exc
    if set(files) != allowed:
        raise AggregateError(
            f"artifact ZIP {artifact['id']} lacks required files: {sorted(allowed - set(files))}",
            status="INCOMPLETE",
        )
    for name, data in files.items():
        (destination / name).write_bytes(data)
    return destination


def _stats(path: Path) -> dict[str, int | float]:
    payload = _read_json(path, "pytest totals")
    if not isinstance(payload, dict):
        raise AggregateError("pytest totals root is not an object")
    values: dict[str, int | float] = {}
    for key in (*_TOTALS, "exit"):
        if type(payload.get(key)) is not int or payload[key] < 0:
            raise AggregateError(f"pytest totals {key} is malformed")
        values[key] = payload[key]
    runtime = payload.get("runtime")
    if (
        isinstance(runtime, bool)
        or not isinstance(runtime, (int, float))
        or not math.isfinite(float(runtime))
        or runtime < 0
    ):
        raise AggregateError("pytest totals runtime is malformed")
    values["runtime"] = float(runtime)
    if sum(int(values[key]) for key in _TOTALS[1:]) != values["collected"]:
        raise AggregateError("pytest totals do not account for every collected test")
    return values


def _junit_counts(path: Path) -> tuple[Counter[str], ET.Element, list[dict[str, str]]]:
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - size-bounded CI artifact verified by API digest
    except (OSError, ET.ParseError, RecursionError) as exc:
        raise AggregateError(
            "a shard JUnit file is missing or malformed", status="INCOMPLETE"
        ) from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise AggregateError("a shard JUnit file has an unexpected root")
    cases = list(root.iter("testcase"))
    if not cases:
        raise AggregateError("a shard JUnit file has no testcase records")
    counts: Counter[str] = Counter(collected=len(cases))
    skips: list[dict[str, str]] = []
    for case in cases:
        outcomes = [key for key in ("failure", "error", "skipped") if case.find(key) is not None]
        if len(outcomes) > 1:
            raise AggregateError("JUnit testcase has multiple terminal outcomes")
        if not outcomes:
            counts["passed"] += 1
        elif outcomes[0] == "failure":
            counts["failed"] += 1
        elif outcomes[0] == "error":
            counts["errors"] += 1
        else:
            skipped = case.find("skipped")
            kind = skipped.attrib.get("type", "") if skipped is not None else ""
            reason = skipped.attrib.get("message", "") if skipped is not None else ""
            if kind == "pytest.xfail":
                counts["xfailed"] += 1
            elif reason.startswith("xfail-marked test passes unexpectedly"):
                counts["xpassed"] += 1
            else:
                counts["skipped"] += 1
            skips.append(
                {
                    "classname": case.get("classname", ""),
                    "name": case.get("name", ""),
                    "reason": reason,
                    "type": kind,
                }
            )
    return counts, root, skips


def _classify_reports(reports: object, assigned: list[str]) -> Counter[str]:
    if not isinstance(reports, dict) or set(reports) != set(assigned):
        raise AggregateError("executed node IDs do not exactly match assigned node IDs")
    counts: Counter[str] = Counter()
    for nodeid in assigned:
        phases = reports[nodeid]
        if (
            not isinstance(phases, list)
            or not phases
            or any(not isinstance(p, dict) for p in phases)
        ):
            raise AggregateError(f"test node {nodeid!r} has no valid process-level phase records")
        whens = [p.get("when") for p in phases]
        if any(
            not isinstance(w, str) or w not in {"setup", "call", "teardown"} for w in whens
        ) or len(whens) != len(set(whens)):
            raise AggregateError(f"test node {nodeid!r} has invalid or duplicate phase records")
        if any(
            not isinstance(p.get("outcome"), str)
            or p.get("outcome") not in {"passed", "failed", "skipped"}
            for p in phases
        ):
            raise AggregateError(f"test node {nodeid!r} has an unknown phase outcome")
        failed = [p for p in phases if p["outcome"] == "failed"]
        skipped = [p for p in phases if p["outcome"] == "skipped"]
        passed = [p for p in phases if p["outcome"] == "passed"]
        if failed:
            counts["errors" if any(p["when"] != "call" for p in failed) else "failed"] += 1
        elif skipped:
            counts["xfailed" if any(p.get("wasxfail") for p in skipped) else "skipped"] += 1
        elif any(p.get("wasxfail") for p in passed):
            counts["xpassed"] += 1
        elif any(p["when"] == "call" for p in passed):
            counts["passed"] += 1
        else:
            raise AggregateError(f"test node {nodeid!r} lacks a terminal pytest phase")
    return counts


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _write_junit(path: Path, suites: list[ET.Element], totals: dict[str, int | float]) -> None:
    root = ET.Element(
        "testsuites",
        {
            "tests": str(totals["collected"]),
            "failures": str(totals["failed"]),
            "errors": str(totals["errors"]),
            "skipped": str(totals["skipped"] + totals["xfailed"] + totals["xpassed"]),
            "time": str(round(float(totals["runtime"]), 3)),
        },
    )
    for suite in suites:
        root.append(copy.deepcopy(suite))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    ET.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
    temporary.replace(path)


def aggregate(
    *,
    manifest_path: Path | None,
    shard_root: Path,
    archive_root: Path,
    extracted_root: Path,
    run_path: Path,
    jobs_path: Path,
    artifacts_path: Path,
    expected_sha: str,
    repository: str,
    workflow: str,
    run_id: str,
    attempt: str,
    shard_count: int,
    output_json: Path,
    output_markdown: Path,
    combined_junit: Path,
    combined_stats: Path,
    combined_output: Path,
    combined_exit: Path,
    combined_state: Path,
    allow_in_progress_run: bool = False,
) -> dict[str, Any]:
    run = _read_json(run_path, "GitHub workflow run API response")
    if not isinstance(run, dict):
        raise AggregateError("GitHub workflow run API response has the wrong shape")
    repo_id = _run_identity(
        run,
        repository=repository,
        workflow=workflow,
        sha=expected_sha,
        run_id=run_id,
        attempt=attempt,
    )
    _, jobs = _records(jobs_path, "jobs", "GitHub jobs API response")
    _, artifacts = _records(artifacts_path, "artifacts", "GitHub artifacts API response")
    if shard_count < 2:
        raise AggregateError("the Qt-enabled Ubuntu run must use at least two shards")
    job_ids = [row.get("id") for row in jobs]
    if any(type(value) is not int or value < 1 for value in job_ids):
        raise AggregateError("GitHub jobs API response contains a malformed job ID")
    if len(job_ids) != len(set(job_ids)):
        raise AggregateError("GitHub jobs API response contains duplicate IDs")
    expected_artifact_names = {f"ubuntu2604-qt-collection-a{attempt}"}
    expected_artifact_names.update(
        f"ubuntu2604-qt-shard-{i:02d}-of-{shard_count:02d}-a{attempt}" for i in range(shard_count)
    )
    expected_artifacts = [
        row
        for row in artifacts
        if isinstance(row.get("name"), str) and row["name"] in expected_artifact_names
    ]
    expected_ids = [row.get("id") for row in expected_artifacts]
    if any(type(value) is not int or value < 1 for value in expected_ids):
        raise AggregateError("a required GitHub artifact has no valid numeric API ID")
    if len(expected_ids) != len(set(expected_ids)):
        raise AggregateError("required GitHub artifacts have duplicate API IDs")
    unexpected = [
        row.get("name")
        for row in artifacts
        if isinstance(row.get("name"), str)
        and row["name"].startswith("ubuntu2604-qt-shard-")
        and row["name"].endswith(f"-a{attempt}")
        and row["name"] not in expected_artifact_names
    ]
    if unexpected:
        raise AggregateError(f"unexpected shard artifacts for this attempt: {sorted(unexpected)}")

    collector_name = _COLLECTION_JOB
    collector = _job(
        jobs,
        name=collector_name,
        workflow=workflow,
        sha=expected_sha,
        run_id=run_id,
        attempt=attempt,
    )
    fail_issues: list[str] = []
    incomplete_issues: list[str] = []
    issues, pending = _step_issues(collector, (_COLLECTION_STEP,), collector_name)
    fail_issues.extend(issues)
    incomplete_issues.extend(pending)
    collection_artifact_name = f"ubuntu2604-qt-collection-a{attempt}"
    collection_artifact = _artifact(
        artifacts,
        name=collection_artifact_name,
        repo_id=repo_id,
        sha=expected_sha,
        run_id=run_id,
        attempt=attempt,
    )
    collection_folder = _extract(
        collection_artifact,
        archive_root=archive_root,
        extracted_root=extracted_root,
        allowed=_COLLECTION_FILES,
    )
    try:
        manifest = validate_manifest(
            _read_json(
                collection_folder / "pytest-collection.json", "canonical collection manifest"
            ),
            expected_sha=expected_sha,
            expected_run_id=run_id,
            expected_attempt=attempt,
            expected_repository=repository,
            expected_workflow=workflow,
        )
    except ValueError as exc:
        raise AggregateError(f"canonical collection manifest: {exc}") from exc
    if manifest_path is not None:
        try:
            supplied = validate_manifest(
                _read_json(manifest_path, "input collection manifest"), expected_sha=expected_sha
            )
        except ValueError as exc:
            raise AggregateError(f"input collection manifest: {exc}") from exc
        if supplied["manifest_sha256"] != manifest["manifest_sha256"]:
            raise AggregateError("input manifest differs from verified collection artifact")
    nodeids = manifest["nodeids"]
    if not nodeids:
        raise AggregateError("canonical test collection is empty")
    if (
        manifest.get("implementation") != "CPython"
        or not manifest.get("python_version", "").startswith("3.14.")
        or manifest.get("platform") != "linux"
        or manifest.get("platform_system") != "Linux"
    ):
        raise AggregateError("canonical collection is not Linux CPython 3.14")

    shard_summaries: list[dict[str, Any]] = []
    suites: list[ET.Element] = []
    totals: Counter[str] = Counter()
    runtime_sum = 0.0
    seen: set[str] = set()
    all_skips: list[dict[str, str]] = []
    for index in range(shard_count):
        job_name = f"Ubuntu 26.04 Qt full-suite shard {index} of {shard_count}"
        job = _job(
            jobs, name=job_name, workflow=workflow, sha=expected_sha, run_id=run_id, attempt=attempt
        )
        failed, pending = _step_issues(job, (_SHARD_STEP, _TOTALS_STEP), job_name)
        fail_issues.extend(failed)
        incomplete_issues.extend(pending)
        artifact_name = f"ubuntu2604-qt-shard-{index:02d}-of-{shard_count:02d}-a{attempt}"
        artifact = _artifact(
            artifacts,
            name=artifact_name,
            repo_id=repo_id,
            sha=expected_sha,
            run_id=run_id,
            attempt=attempt,
        )
        folder = _extract(
            artifact, archive_root=archive_root, extracted_root=extracted_root, allowed=_SHARD_FILES
        )
        selection = _read_json(folder / "shard-selection.json", f"shard {index} selection")
        observed = _read_json(folder / "observed-collection.json", f"shard {index} collection")
        execution = _read_json(folder / "shard-execution.json", f"shard {index} execution")
        try:
            observed = validate_manifest(
                observed,
                expected_sha=expected_sha,
                expected_run_id=run_id,
                expected_attempt=attempt,
                expected_repository=repository,
                expected_workflow=workflow,
            )
        except ValueError as exc:
            raise AggregateError(f"shard {index} collection: {exc}") from exc
        if (
            observed["manifest_sha256"] != manifest["manifest_sha256"]
            or observed["nodeids"] != nodeids
        ):
            raise AggregateError(f"shard {index} collected a different test universe")
        assigned = assigned_nodeids(nodeids, index, shard_count)
        if not assigned:
            raise AggregateError(f"shard {index} was assigned no tests")
        if not isinstance(selection, dict) or any(
            type(selection.get(k)) is not int
            for k in (
                "schema_version",
                "shard_index",
                "shard_count",
                "collection_count",
                "assigned_count",
                "deselected_count",
            )
        ):
            raise AggregateError(f"shard {index} selection has malformed integer fields")
        if not (
            selection["schema_version"] == _SCHEMA
            and selection.get("head_sha") == expected_sha
            and selection.get("repository") == repository
            and selection.get("workflow") == workflow
            and str(selection.get("run_id")) == str(run_id)
            and str(selection.get("run_attempt")) == str(attempt)
            and selection.get("manifest_sha256") == manifest["manifest_sha256"]
            and selection.get("assignment_algorithm") == manifest["assignment_algorithm"]
            and selection["shard_index"] == index
            and selection["shard_count"] == shard_count
            and selection["collection_count"] == len(nodeids)
            and selection["assigned_nodeids"] == assigned
            and selection["assigned_count"] == len(assigned)
            and selection.get("assigned_sha256") == _digest_ids(assigned)
            and selection["deselected_count"] == len(nodeids) - len(assigned)
        ):
            raise AggregateError(f"shard {index} selection differs from deterministic assignment")
        if not isinstance(execution, dict) or any(
            type(execution.get(k)) is not int
            for k in ("schema_version", "shard_index", "shard_count", "pytest_exit_status")
        ):
            raise AggregateError(f"shard {index} execution has malformed integer fields")
        if not (
            execution["schema_version"] == _SCHEMA
            and execution.get("head_sha") == expected_sha
            and execution.get("repository") == repository
            and execution.get("workflow") == workflow
            and execution.get("workflow_ref") == manifest["workflow_ref"]
            and str(execution.get("run_id")) == str(run_id)
            and str(execution.get("run_attempt")) == str(attempt)
            and execution.get("manifest_sha256") == manifest["manifest_sha256"]
            and execution.get("assignment_algorithm") == manifest["assignment_algorithm"]
            and execution["shard_index"] == index
            and execution["shard_count"] == shard_count
            and execution.get("assigned_nodeids") == assigned
        ):
            raise AggregateError(f"shard {index} execution identity or assignment is invalid")
        if seen.intersection(assigned):
            raise AggregateError(f"shard {index} overlaps another shard")
        seen.update(assigned)
        stats = _stats(folder / "pytest-totals.json")
        try:
            shell_exit = read_exit_file(folder / "pytest-exit.txt")
        except ValueError as exc:
            raise AggregateError(
                f"shard {index} shell exit is invalid", status="INCOMPLETE"
            ) from exc
        state = (folder / "pytest-run-state.txt").read_text(encoding="utf-8").strip()
        if (
            shell_exit != stats["exit"]
            or str(execution["pytest_exit_status"]) != str(shell_exit)
            or state != f"COMPLETED exit={shell_exit}"
        ):
            raise AggregateError(
                f"shard {index} process-exit evidence is inconsistent", status="INCOMPLETE"
            )
        junit_counts, junit_root, skips = _junit_counts(folder / "pytest-junit.xml")
        if junit_counts["collected"] != len(assigned):
            raise AggregateError(f"shard {index} JUnit count differs from assigned node IDs")
        process_counts = _classify_reports(execution.get("reports"), assigned)
        for key in _TOTALS[1:]:
            if process_counts[key] != stats[key]:
                raise AggregateError(f"shard {index} process reports disagree on {key}")
        for key in ("failed", "errors", "skipped", "xfailed"):
            if junit_counts[key] != stats[key]:
                raise AggregateError(f"shard {index} JUnit outcomes disagree on {key}")
        if junit_counts["passed"] + junit_counts["xpassed"] != stats["passed"] + stats["xpassed"]:
            raise AggregateError(f"shard {index} JUnit passed/xpassed outcomes disagree")
        problems = disagreements(stats, totals_from_junit(folder / "pytest-junit.xml", shell_exit))
        if problems:
            raise AggregateError(
                f"shard {index} stats/JUnit attributes disagree: {'; '.join(problems)}"
            )
        if stats["collected"] != len(assigned):
            raise AggregateError(f"shard {index} totals do not account for all assigned node IDs")
        if stats["skipped"] or stats["xfailed"]:
            fail_issues.append(
                f"shard {index} did not execute every test normally: skipped={stats['skipped']} xfailed={stats['xfailed']}"
            )
        if stats["failed"] or stats["errors"] or stats["xpassed"] or shell_exit != 0:
            fail_issues.append(
                f"shard {index} failed: failed={stats['failed']} errors={stats['errors']} xpassed={stats['xpassed']} exit={shell_exit}"
            )
        unexpected = unexpected_skips_qt_enabled(folder / "pytest-junit.xml")
        if unexpected:
            fail_issues.append(f"shard {index} has unexpected Qt-enabled skips: {unexpected[:10]}")
        for key in _TOTALS:
            totals[key] += int(stats[key])
        runtime_sum += float(stats["runtime"])
        suites.extend(
            junit_root.findall("testsuite") if junit_root.tag == "testsuites" else [junit_root]
        )
        all_skips.extend(skips)
        shard_summaries.append(
            {
                "shard_index": index,
                "shard_count": shard_count,
                "job_id": job["id"],
                "job_name": job_name,
                "job_status": job.get("status"),
                "job_conclusion": job.get("conclusion"),
                "artifact_id": artifact["id"],
                "artifact_name": artifact_name,
                "artifact_digest": artifact["digest"],
                "artifact_size_bytes": artifact["size_in_bytes"],
                "assigned_count": len(assigned),
                "assigned_sha256": _digest_ids(assigned),
                "totals": stats,
            }
        )
    if seen != set(nodeids) or len(seen) != len(nodeids):
        raise AggregateError("shard partition does not cover the collection exactly")
    if totals["collected"] != len(nodeids):
        raise AggregateError("combined totals do not equal canonical collection count")

    run_status = run.get("status")
    if run_status != "completed":
        if not (allow_in_progress_run and run_status == "in_progress"):
            incomplete_issues.append(f"workflow run has not completed (status {run_status!r})")
    elif run.get("conclusion") != "success":
        fail_issues.append(f"workflow run conclusion is {run.get('conclusion')!r}")
    status = "INCOMPLETE" if incomplete_issues else "FAIL" if fail_issues else "PASS"
    runtime_sum = round(runtime_sum, 3)
    combined = {**totals, "exit": 0 if status == "PASS" else 1, "runtime": runtime_sum}
    _write_junit(combined_junit, suites, combined)
    _write_json(combined_stats, combined)
    combined_exit.parent.mkdir(parents=True, exist_ok=True)
    combined_exit.write_text(f"{combined['exit']}\n", encoding="utf-8", newline="\n")
    combined_state.write_text(
        (f"COMPLETED exit={combined['exit']}" if status != "INCOMPLETE" else "INCOMPLETE") + "\n",
        encoding="utf-8",
        newline="\n",
    )
    combined_output.parent.mkdir(parents=True, exist_ok=True)
    combined_output.write_text(
        f"shards: {shard_count}/{shard_count}\ncollection_sha256: {manifest['manifest_sha256']}\nnodeids: {len(nodeids)}\n"
        + "\n".join(f"{k}: {totals[k]}" for k in _TOTALS)
        + f"\nruntime_sum_seconds: {runtime_sum}\n",
        encoding="utf-8",
        newline="\n",
    )
    report = {
        "schema_version": _SCHEMA,
        "status": status,
        "complete": not incomplete_issues,
        "repository": repository,
        "workflow": workflow,
        "workflow_path": run.get("path"),
        "head_sha": expected_sha,
        "run_id": str(run_id),
        "run_attempt": str(attempt),
        "run_status_at_aggregation": run_status,
        "workflow_conclusion_verified": run_status == "completed"
        and run.get("conclusion") == "success",
        "workflow_run_completion_pending": run_status != "completed",
        "platform": "Ubuntu 26.04",
        "python_version": manifest["python_version"],
        "pytest_version": manifest["pytest_version"],
        "qt_enabled": True,
        "shard_count": shard_count,
        "collection_manifest_sha256": manifest["manifest_sha256"],
        "collected_nodeids": len(nodeids),
        "covered_nodeids": len(seen),
        "totals": combined,
        "issues": [*fail_issues, *incomplete_issues],
        "skips": all_skips,
        "collector": {
            "job_id": collector.get("id"),
            "job_name": collector_name,
            "job_status": collector.get("status"),
            "job_conclusion": collector.get("conclusion"),
            "artifact_id": collection_artifact["id"],
            "artifact_name": collection_artifact_name,
            "artifact_digest": collection_artifact["digest"],
            "artifact_size_bytes": collection_artifact["size_in_bytes"],
        },
        "shards": shard_summaries,
    }
    _write_json(output_json, report)
    output_markdown.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Ubuntu 26.04 Qt-enabled pytest shard report",
        "",
        f"- Result: **{status}**",
        f"- Repository: `{repository}`",
        f"- Workflow: `{workflow}`; run `{run_id}` attempt `{attempt}`",
        f"- Exact SHA: `{expected_sha}`",
        f"- Workflow conclusion verified: {'yes' if report['workflow_conclusion_verified'] else 'no — final run status must be checked after this report job'}",
        f"- Python: `{manifest['python_version']}`; pytest: `{manifest['pytest_version']}`; Qt enabled: **yes**",
        f"- Collection manifest SHA-256: `{manifest['manifest_sha256']}`",
        f"- Complete universe: {len(nodeids)} collected; {len(seen)} assigned and executed exactly once",
        f"- Runtime sum: {runtime_sum:.3f}s",
        "",
        "| shard | job ID | conclusion | assigned | passed | failed | errors | skipped | xfailed | xpassed | runtime s | artifact ID |",
        "|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for shard in shard_summaries:
        values = shard["totals"]
        lines.append(
            f"| {shard['shard_index']}/{shard_count} | {shard['job_id']} | {shard['job_conclusion']} | {shard['assigned_count']} | {values['passed']} | {values['failed']} | {values['errors']} | {values['skipped']} | {values['xfailed']} | {values['xpassed']} | {values['runtime']:.3f} | {shard['artifact_id']} |"
        )
    lines += ["", "## Issues", ""]
    lines += [f"- {issue}" for issue in report["issues"]] if report["issues"] else ["- None."]
    lines += ["", "## Skip reasons", ""]
    lines += (
        [f"- `{s['classname']}::{s['name']}` — `{s['type']}`: {s['reason']}" for s in all_skips]
        if all_skips
        else ["- None."]
    )
    output_markdown.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return report


def _failure_report(args: argparse.Namespace, status: str, message: str) -> None:
    payload = {
        "schema_version": _SCHEMA,
        "status": status,
        "complete": False,
        "repository": args.repository,
        "workflow": args.workflow,
        "head_sha": args.sha,
        "run_id": str(args.run_id),
        "run_attempt": str(args.attempt),
        "error": message,
    }
    _write_json(args.output_json, payload)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.write_text(
        f"# Ubuntu 26.04 Qt-enabled pytest shard report\n\n**{status}** — {message}\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--shard-root", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--extracted-root", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--allow-in-progress-run", action="store_true")
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    parser.add_argument("--combined-junit", type=Path, required=True)
    parser.add_argument("--combined-stats", type=Path, required=True)
    parser.add_argument("--combined-output", type=Path, required=True)
    parser.add_argument("--combined-exit", type=Path, required=True)
    parser.add_argument("--combined-state", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = aggregate(
            manifest_path=args.manifest,
            shard_root=args.shard_root,
            archive_root=args.archive_root,
            extracted_root=args.extracted_root,
            run_path=args.run,
            jobs_path=args.jobs,
            artifacts_path=args.artifacts,
            expected_sha=args.sha,
            repository=args.repository,
            workflow=args.workflow,
            run_id=args.run_id,
            attempt=args.attempt,
            shard_count=args.shard_count,
            output_json=args.output_json,
            output_markdown=args.output_markdown,
            combined_junit=args.combined_junit,
            combined_stats=args.combined_stats,
            combined_output=args.combined_output,
            combined_exit=args.combined_exit,
            combined_state=args.combined_state,
            allow_in_progress_run=args.allow_in_progress_run,
        )
    except AggregateError as exc:
        try:
            _failure_report(args, exc.status, str(exc))
        except OSError:
            print(f"{exc.status}: report output could not be written", file=sys.stderr)
            return 1
        print(f"{exc.status}: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        try:
            _failure_report(args, "INVALID EVIDENCE", str(exc))
        except OSError:
            pass
        print("INVALID EVIDENCE: a required record failed validation", file=sys.stderr)
        return 1
    except OSError:
        try:
            _failure_report(args, "INCOMPLETE", "required evidence could not be read or written")
        except OSError:
            pass
        print("INCOMPLETE: required evidence could not be read or written", file=sys.stderr)
        return 1
    print(
        f"{report['status']}: {report['covered_nodeids']} node IDs, {report['shard_count']} shards"
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
