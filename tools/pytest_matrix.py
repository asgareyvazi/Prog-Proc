#!/usr/bin/env python3
"""Bind full-suite pytest totals to their platform, job, artifact, and exact commit.

The ``record`` subcommand runs in each matrix job and writes an identity sidecar next to the raw
pytest files. The ``report`` subcommand runs after the matrix, validates downloaded sidecars against
the workflow/job/artifact API records, and emits a table without positional row assumptions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_TOTAL_KEYS = ("collected", "passed", "failed", "errors", "skipped", "xfailed", "xpassed")
_REQUIRED_FILES = {
    "stats": "pytest-totals.json",
    "junit": "pytest-junit.xml",
    "exit": "pytest-exit.txt",
    "state": "pytest-run-state.txt",
    "output": "pytest-output.txt",
}
_PLATFORMS = {
    "Linux": ("test", "pytest-cp{python}-a{attempt}", "test"),
    "Windows": ("windows test", "pytest-windows-cp{python}-a{attempt}", "test"),
    "Ubuntu 26.04": (
        "test Ubuntu 26.04",
        "pytest-ubuntu-2604-cp{python}-a{attempt}",
        "ubuntu-2604-cp314",
    ),
}


class EvidenceError(ValueError):
    """A matrix evidence record is missing, ambiguous, mismatched, or malformed."""


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceError(f"{label} must be a non-negative integer")
    return value


def _read_totals(path: Path) -> dict[str, int | float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("pytest totals are missing or unreadable") from exc
    if not isinstance(raw, dict):
        raise EvidenceError("pytest totals root must be an object")
    totals: dict[str, int | float] = {}
    for key in (*_TOTAL_KEYS, "exit"):
        totals[key] = _nonnegative_int(raw.get(key), f"pytest totals {key}")
    runtime = raw.get("runtime")
    if isinstance(runtime, bool) or not isinstance(runtime, (int, float)):
        raise EvidenceError("pytest totals runtime must be a finite non-negative number")
    runtime = float(runtime)
    if not math.isfinite(runtime) or runtime < 0:
        raise EvidenceError("pytest totals runtime must be a finite non-negative number")
    totals["runtime"] = runtime
    if totals["collected"] <= 0:
        raise EvidenceError("pytest collected no tests")
    accounted = sum(int(totals[key]) for key in _TOTAL_KEYS[1:])
    if accounted != totals["collected"]:
        raise EvidenceError("pytest outcome counts do not equal collected")
    return totals


def _junit_totals(path: Path) -> dict[str, int]:
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - generated in the same job
    except (OSError, ET.ParseError) as exc:
        raise EvidenceError("pytest JUnit is missing or unreadable") from exc
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.findall("testsuite"))
    else:
        raise EvidenceError("pytest JUnit has an unexpected root")
    if not suites:
        raise EvidenceError("pytest JUnit has no test suites")
    tests = failures = errors = skipped_attr = xfailed = xpassed = 0
    for suite in suites:
        for key, attribute in (
            ("tests", "tests"),
            ("failures", "failures"),
            ("errors", "errors"),
            ("skipped", "skipped"),
        ):
            raw = suite.attrib.get(attribute, "0")
            if re.fullmatch(r"[0-9]+", raw) is None:
                raise EvidenceError(f"pytest JUnit {attribute} is malformed")
            value = int(raw)
            if key == "tests":
                tests += value
            elif key == "failures":
                failures += value
            elif key == "errors":
                errors += value
            else:
                skipped_attr += value
        for skipped in suite.iter("skipped"):
            kind = skipped.attrib.get("type") or ""
            message = skipped.attrib.get("message") or ""
            if kind == "pytest.xfail":
                xfailed += 1
            elif message.startswith("xfail-marked test passes unexpectedly"):
                xpassed += 1
    skipped = skipped_attr - xfailed - xpassed
    if skipped < 0:
        raise EvidenceError("pytest JUnit skip counts are inconsistent")
    passed = tests - failures - errors - skipped_attr
    if passed < 0:
        raise EvidenceError("pytest JUnit outcome counts are inconsistent")
    return {
        "collected": tests,
        "passed": passed,
        "failed": failures,
        "errors": errors,
        "skipped": skipped,
        "xfailed": xfailed,
        "xpassed": xpassed,
    }


def _compare_totals(stats: dict[str, int | float], junit: dict[str, int]) -> None:
    for key in ("collected", "failed", "errors", "skipped", "xfailed"):
        if stats[key] != junit[key]:
            raise EvidenceError(f"pytest stats and JUnit disagree on {key}")
    if stats["passed"] + stats["xpassed"] != junit["passed"] + junit["xpassed"]:
        raise EvidenceError("pytest stats and JUnit disagree on passed plus xpassed")


def _exit_code(path: Path) -> int:
    value = _safe_read(path)
    if value is None or re.fullmatch(r"-?[0-9]+", value) is None:
        raise EvidenceError("pytest shell exit record is missing or malformed")
    return int(value)


def record_evidence(
    *, platform: str, python_version: str, artifact_name: str, output: Path, totals_step: str
) -> str:
    if platform not in _PLATFORMS:
        raise EvidenceError("unsupported platform")
    if not re.fullmatch(r"3\.[0-9]+", python_version):
        raise EvidenceError("python version must be major.minor")
    identity = {
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "workflow": os.environ.get("GITHUB_WORKFLOW", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "ref": os.environ.get("GITHUB_REF", ""),
        "head_sha": os.environ.get("GITHUB_SHA", ""),
        "job_key": os.environ.get("GITHUB_JOB", ""),
        "job_name": os.environ.get("PYTEST_MATRIX_JOB_NAME", ""),
        "platform": platform,
        "python_version": python_version,
        "artifact_name": artifact_name,
    }
    files = {
        label: {
            "path": filename,
            "state": "PRESENT" if Path(filename).is_file() else "NOT PRODUCED",
        }
        for label, filename in _REQUIRED_FILES.items()
    }
    for item in files.values():
        file_path = Path(item["path"])
        if file_path.is_file():
            try:
                item["sha256"] = _sha256(file_path)
                item["size_bytes"] = file_path.stat().st_size
            except OSError:
                item["state"] = "UNREADABLE"
    totals: dict[str, int | float] | None = None
    result = "INVALID"
    reason = ""
    state = _safe_read(Path(_REQUIRED_FILES["state"]))
    state_exit: int | None = None
    if state is not None:
        state_match = re.fullmatch(r"COMPLETED exit=(-?[0-9]+)", state)
        if state_match is not None:
            state_exit = int(state_match.group(1))
    try:
        if state is None or state.startswith("NOT RUN") or state == "RUNNING":
            result = "NOT RUN"
            reason = state or "pytest state file was not produced"
        elif state_exit is None:
            result = "INVALID"
            reason = "pytest run-state file is malformed"
        elif any(item["state"] != "PRESENT" for item in files.values()):
            result = "NOT PRODUCED"
            reason = "one or more required pytest evidence files are missing or unreadable"
        else:
            totals = _read_totals(Path(_REQUIRED_FILES["stats"]))
            junit = _junit_totals(Path(_REQUIRED_FILES["junit"]))
            _compare_totals(totals, junit)
            shell_exit = _exit_code(Path(_REQUIRED_FILES["exit"]))
            if shell_exit != state_exit:
                raise EvidenceError("pytest run-state and shell exit disagree")
            if shell_exit != totals["exit"]:
                raise EvidenceError("pytest shell exit and session exit disagree")
            if totals_step != "success" or shell_exit != 0:
                result = "FAIL"
                reason = f"pytest publisher outcome={totals_step}; exit={shell_exit}"
            elif totals["failed"] or totals["errors"] or totals["xpassed"]:
                result = "FAIL"
                reason = "pytest has failures, errors, or unexpected passes"
            elif totals["passed"] + totals["xpassed"] == 0:
                result = "FAIL"
                reason = "no tests passed; all collected tests were skipped or xfailed"
            else:
                result = "PASS"
                reason = "pytest totals, JUnit, shell exit, and publisher agree"
    except EvidenceError as exc:
        result = "INVALID"
        reason = str(exc)

    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "identity": identity,
        "result": result,
        "reason": reason,
        "totals_step_outcome": totals_step,
        "run_state": state or "NOT PRODUCED",
        "totals": totals,
        "files": files,
    }
    payload["evidence_sha256"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".partial")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return result


def _json_file(path: Path, expected_key: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{expected_key} API evidence is missing or malformed") from exc
    if not isinstance(value, dict) or not isinstance(value.get(expected_key), list):
        raise EvidenceError(f"{expected_key} API evidence has the wrong shape")
    return value


def _read_evidence(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("matrix evidence sidecar is missing or malformed") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
        raise EvidenceError("matrix evidence sidecar has an unsupported schema")
    claimed = payload.get("evidence_sha256")
    check = dict(payload)
    check.pop("evidence_sha256", None)
    if (
        not isinstance(claimed, str)
        or hashlib.sha256(_canonical_bytes(check)).hexdigest() != claimed
    ):
        raise EvidenceError("matrix evidence sidecar hash mismatch")
    return payload


def _validate_sidecar_files(directory: Path, evidence: dict[str, Any]) -> None:
    files = evidence.get("files")
    if not isinstance(files, dict):
        raise EvidenceError("matrix evidence file manifest is missing")
    for label, filename in _REQUIRED_FILES.items():
        item = files.get(label)
        if not isinstance(item, dict) or item.get("path") != filename:
            raise EvidenceError(f"matrix evidence {label} file identity mismatch")
        if item.get("state") != "PRESENT":
            if evidence.get("result") in {"NOT RUN", "NOT PRODUCED"}:
                continue
            raise EvidenceError(f"matrix evidence {label} is not present")
        target = directory / filename
        if not target.is_file() or _sha256(target) != item.get("sha256"):
            raise EvidenceError(f"downloaded {label} file does not match its SHA-256")
        if target.stat().st_size != item.get("size_bytes"):
            raise EvidenceError(f"downloaded {label} file size does not match its record")
    if evidence.get("result") == "PASS":
        totals = evidence.get("totals")
        if not isinstance(totals, dict):
            raise EvidenceError("PASS evidence has no totals")
        stats = _read_totals(directory / _REQUIRED_FILES["stats"])
        junit = _junit_totals(directory / _REQUIRED_FILES["junit"])
        _compare_totals(stats, junit)
        if totals != stats:
            raise EvidenceError("sidecar totals differ from pytest-totals.json")
        if _exit_code(directory / _REQUIRED_FILES["exit"]) != stats["exit"]:
            raise EvidenceError("sidecar exit does not match pytest-exit.txt")
        if evidence.get("totals_step_outcome") != "success" or stats["exit"] != 0:
            raise EvidenceError("PASS evidence has a non-success publisher or process exit")


def _api_match(items: list[dict[str, Any]], key: str, value: str) -> list[dict[str, Any]]:
    return [item for item in items if str(item.get(key, "")) == value]


def _expected_artifact_name(platform: str, python_version: str, attempt: str) -> str:
    _job_prefix, template, _job_key = _PLATFORMS[platform]
    return template.format(python=python_version, attempt=attempt)


def build_report(
    *,
    artifact_root: Path,
    platform: str,
    expected_sha: str,
    run_id: str,
    run_attempt: str,
    repository: str,
    workflow: str,
    jobs: dict[str, Any],
    artifacts: dict[str, Any],
    versions: tuple[str, ...] = ("3.11", "3.14"),
) -> dict[str, Any]:
    if platform not in _PLATFORMS:
        raise EvidenceError("unsupported platform")
    job_items = [item for item in jobs["jobs"] if isinstance(item, dict)]
    artifact_items = [item for item in artifacts["artifacts"] if isinstance(item, dict)]
    expected_names = {
        _expected_artifact_name(platform, version, run_attempt) for version in versions
    }
    directories = list(artifact_root.iterdir()) if artifact_root.exists() else []
    sidecar_paths = (
        list(artifact_root.rglob("pytest-matrix-evidence.json")) if artifact_root.exists() else []
    )
    entries: list[tuple[Path, dict[str, Any] | None, str]] = []
    for path in sidecar_paths:
        try:
            entries.append((path.parent, _read_evidence(path), ""))
        except EvidenceError as exc:
            entries.append((path.parent, None, str(exc)))

    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    artifact_prefix = _PLATFORMS[platform][1].split("{python}", 1)[0]
    for directory in directories:
        if (
            directory.is_dir()
            and directory.name.startswith(artifact_prefix)
            and directory.name not in expected_names
        ):
            issues.append(f"unexpected artifact directory for {platform}: {directory.name}")
    for version in versions:
        artifact_name = _expected_artifact_name(platform, version, run_attempt)
        matching = [
            (directory, evidence, error)
            for directory, evidence, error in entries
            if directory.name == artifact_name
            or (
                evidence is not None
                and isinstance(evidence.get("identity"), dict)
                and evidence["identity"].get("platform") == platform
                and evidence["identity"].get("python_version") == version
                and evidence["identity"].get("artifact_name") == artifact_name
            )
        ]
        row: dict[str, Any] = {
            "platform": platform,
            "python_version": version,
            "job_name": f"{_PLATFORMS[platform][0]} (cp{version})",
            "job_id": None,
            "job_conclusion": "NOT REPORTED",
            "artifact_name": artifact_name,
            "artifact_id": None,
            "artifact_digest": "NOT REPORTED",
            "totals_sha256": None,
            "head_sha": None,
            "collected": None,
            "passed": None,
            "skipped": None,
            "failed": None,
            "errors": None,
            "result": "NOT PRODUCED",
            "detail": "expected artifact/evidence was not produced",
        }
        if len(matching) > 1:
            row["result"] = "AMBIGUOUS"
            row["detail"] = f"{len(matching)} sidecars claim the same platform/job/artifact lane"
            issues.append(f"{platform} CPython {version}: duplicate sidecars")
            rows.append(row)
            continue
        api_artifacts = _api_match(artifact_items, "name", artifact_name)
        if len(api_artifacts) > 1:
            row["result"] = "AMBIGUOUS"
            row["detail"] = f"{len(api_artifacts)} GitHub artifacts have the expected name"
            issues.append(f"{platform} CPython {version}: duplicate GitHub artifacts")
            rows.append(row)
            continue
        if len(api_artifacts) == 1:
            artifact = api_artifacts[0]
            row["artifact_id"] = artifact.get("id")
            row["artifact_digest"] = artifact.get("digest") or "NOT REPORTED"
            if artifact.get("expired") is True:
                row["result"] = "NOT PRODUCED"
                row["detail"] = "GitHub artifact is expired"
                issues.append(f"{platform} CPython {version}: artifact expired")
                rows.append(row)
                continue
        expected_job_name = row["job_name"]
        api_jobs = _api_match(job_items, "name", expected_job_name)
        if len(api_jobs) == 1:
            row["job_id"] = api_jobs[0].get("id")
            row["job_conclusion"] = (
                api_jobs[0].get("conclusion") or api_jobs[0].get("status") or "UNKNOWN"
            )
        elif len(api_jobs) > 1:
            row["result"] = "AMBIGUOUS"
            row["detail"] = f"{len(api_jobs)} GitHub jobs have the expected matrix name"
            issues.append(f"{platform} CPython {version}: duplicate GitHub jobs")
            rows.append(row)
            continue
        if not matching:
            if len(api_jobs) > 1:
                row["result"] = "AMBIGUOUS"
            elif len(api_jobs) == 1 and row["job_conclusion"] in {
                "cancelled",
                "skipped",
                "in_progress",
                "queued",
            }:
                row["result"] = "NOT RUN"
                row["detail"] = f"job conclusion={row['job_conclusion']}; no test artifact"
            elif len(api_artifacts) == 1:
                row["result"] = "INVALID"
                row["detail"] = "GitHub artifact exists but downloaded evidence sidecar is missing"
                issues.append(f"{platform} CPython {version}: artifact missing sidecar")
            else:
                issues.append(f"{platform} CPython {version}: required artifact not produced")
            rows.append(row)
            continue
        directory, evidence, error = matching[0]
        if evidence is None:
            row["result"] = "INVALID"
            row["detail"] = error
            issues.append(f"{platform} CPython {version}: invalid sidecar: {error}")
            rows.append(row)
            continue
        identity = evidence.get("identity")
        if not isinstance(identity, dict):
            row["result"] = "INVALID"
            row["detail"] = "sidecar identity is missing"
            issues.append(f"{platform} CPython {version}: identity missing")
            rows.append(row)
            continue
        row["head_sha"] = identity.get("head_sha")
        expected_identity = {
            "repository": repository,
            "workflow": workflow,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "head_sha": expected_sha,
            "job_name": row["job_name"],
            "job_key": _PLATFORMS[platform][2],
            "platform": platform,
            "python_version": version,
            "artifact_name": artifact_name,
        }
        mismatches = [
            key for key, value in expected_identity.items() if str(identity.get(key, "")) != value
        ]
        if mismatches:
            row["result"] = "INVALID"
            row["detail"] = "sidecar identity mismatch: " + ", ".join(mismatches)
            issues.append(
                f"{platform} CPython {version}: identity mismatch ({', '.join(mismatches)})"
            )
            rows.append(row)
            continue
        if row["job_id"] is None:
            row["result"] = "INVALID"
            row["detail"] = "GitHub job identity was not uniquely resolved"
            issues.append(f"{platform} CPython {version}: job id missing or ambiguous")
            rows.append(row)
            continue
        if row["job_conclusion"] != "success":
            row["result"] = "INVALID"
            row["detail"] = f"GitHub job conclusion is {row['job_conclusion']}, not success"
            issues.append(f"{platform} CPython {version}: job conclusion is not success")
            rows.append(row)
            continue
        if row["artifact_id"] is None:
            row["result"] = "INVALID"
            row["detail"] = "GitHub artifact identity was not uniquely resolved"
            issues.append(f"{platform} CPython {version}: artifact id missing")
            rows.append(row)
            continue
        try:
            _validate_sidecar_files(directory, evidence)
        except EvidenceError as exc:
            row["result"] = "INVALID"
            row["detail"] = str(exc)
            issues.append(f"{platform} CPython {version}: {exc}")
            rows.append(row)
            continue
        file_manifest = evidence.get("files")
        stats_manifest = file_manifest.get("stats") if isinstance(file_manifest, dict) else None
        if isinstance(stats_manifest, dict):
            row["totals_sha256"] = stats_manifest.get("sha256")
        totals = evidence.get("totals")
        if isinstance(totals, dict):
            for key in ("collected", "passed", "skipped", "failed", "errors"):
                row[key] = totals.get(key)
        row["result"] = evidence.get("result", "INVALID")
        row["detail"] = evidence.get("reason", "")
        if row["result"] not in {"PASS", "FAIL", "NOT RUN", "NOT PRODUCED"}:
            row["result"] = "INVALID"
            issues.append(f"{platform} CPython {version}: unsupported result state")
        if row["result"] != "PASS":
            issues.append(f"{platform} CPython {version}: pytest result={row['result']}")
        rows.append(row)

    report = {
        "schema_version": _SCHEMA_VERSION,
        "platform": platform,
        "repository": repository,
        "workflow": workflow,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "expected_sha": expected_sha,
        "rows": rows,
        "issues": issues,
        "result": "PASS" if not issues and all(row["result"] == "PASS" for row in rows) else "FAIL",
    }
    return report


def report_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['platform']} full-suite pytest matrix",
        "",
        f"- Workflow: `{report['workflow']}`; run: `{report['run_id']}` attempt `{report['run_attempt']}`",
        f"- Expected commit: `{report['expected_sha']}`",
        f"- Matrix result: **{report['result']}**",
        "",
        "| Platform | Python | Job ID | Job conclusion | Artifact | Artifact ID | Artifact digest | Head SHA | Totals SHA-256 | Collected | Passed | Skipped | Failed | Errors | Pytest result |",
        "|---|---:|---:|---|---|---:|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in report["rows"]:
        lines.append(
            "| {platform} | {python_version} | {job_id} | {job_conclusion} | `{artifact_name}` | {artifact_id} | `{artifact_digest}` | `{head_sha}` | `{totals_sha256}` | {collected} | {passed} | {skipped} | {failed} | {errors} | **{result}** |".format(
                **{key: ("NOT PRODUCED" if value is None else value) for key, value in row.items()}
            )
        )
    if report["issues"]:
        lines += ["", "## Integrity issues", ""]
        lines.extend(f"- {issue}" for issue in report["issues"])
    lines.append("")
    return "\n".join(lines)


def _record_command(args: argparse.Namespace) -> int:
    if args.job_name:
        os.environ["PYTEST_MATRIX_JOB_NAME"] = args.job_name
    result = record_evidence(
        platform=args.platform,
        python_version=args.python_version,
        artifact_name=args.artifact_name,
        output=args.output,
        totals_step=args.totals_step,
    )
    print(f"pytest matrix evidence: {result} — {args.output}")
    return 1 if result in {"FAIL", "INVALID", "NOT PRODUCED"} else 0


def _report_command(args: argparse.Namespace) -> int:
    try:
        jobs = _json_file(args.jobs_json, "jobs")
        artifacts = _json_file(args.artifacts_json, "artifacts")
        report = build_report(
            artifact_root=args.artifact_root,
            platform=args.platform,
            expected_sha=args.expected_sha,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            repository=args.repository,
            workflow=args.workflow,
            jobs=jobs,
            artifacts=artifacts,
            versions=tuple(args.versions),
        )
    except EvidenceError as exc:
        print(f"pytest matrix report: INVALID — {exc}", file=sys.stderr)
        return 1
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    markdown = report_markdown(report)
    args.output_markdown.write_text(markdown, encoding="utf-8")
    print(markdown)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(markdown)
    return 0 if report["result"] == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("--platform", choices=sorted(_PLATFORMS), required=True)
    record.add_argument("--python-version", required=True)
    record.add_argument("--artifact-name", required=True)
    record.add_argument("--job-name", required=True)
    record.add_argument("--totals-step", required=True)
    record.add_argument("--output", type=Path, default=Path("pytest-matrix-evidence.json"))
    record.set_defaults(handler=_record_command)

    report = commands.add_parser("report")
    report.add_argument("--artifact-root", type=Path, required=True)
    report.add_argument("--platform", choices=sorted(_PLATFORMS), required=True)
    report.add_argument("--expected-sha", required=True)
    report.add_argument("--run-id", required=True)
    report.add_argument("--run-attempt", required=True)
    report.add_argument("--repository", required=True)
    report.add_argument("--workflow", required=True)
    report.add_argument("--jobs-json", type=Path, required=True)
    report.add_argument("--artifacts-json", type=Path, required=True)
    report.add_argument("--versions", nargs="+", default=("3.11", "3.14"))
    report.add_argument("--output-json", type=Path, required=True)
    report.add_argument("--output-markdown", type=Path, required=True)
    report.set_defaults(handler=_report_command)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError) as exc:
        print(f"pytest matrix evidence: INVALID — {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
