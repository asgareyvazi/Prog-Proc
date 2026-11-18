#!/usr/bin/env python3
"""Audit the pinned dependency set for known vulnerabilities.

This is a gate, not a report. It has three outcomes and only one of them
fails the build:

``clean``
    The advisory service answered and reported no known vulnerability in the
    audited set.
``vulnerable``
    The advisory service answered and reported a known vulnerability. The
    build fails. A vulnerability is never suppressed.
``unavailable``
    The advisory service could not be reached. The build passes with a
    warning and the outage is recorded. A transient outage of a third-party
    service is not a defect in this repository and must not be reported as
    one, but it must also not be mistaken for a clean audit.

Anything else - a missing ``pip-audit``, a malformed report, an unexpected
error - is a failure. A gate that cannot decide must not say "pass".

The audited set is the repository's own pinned constraints, not the ambient
environment. The environment of a CI runner also contains the installer and
whatever the runner image happened to seed, and a gate whose verdict depends
on the image is not a gate on this repository.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

CLEAN = "clean"
VULNERABLE = "vulnerable"
UNAVAILABLE = "unavailable"
FAILURE = "failure"

# The markers of an advisory service that could not be reached. They are
# deliberately narrow: an unexpected error must be a failure, not an excuse.
_OUTAGE_MARKERS = (
    "connectionerror",
    "temporary failure in name resolution",
    "failed to establish a new connection",
    "max retries exceeded",
    "readtimeout",
    "connecttimeout",
    "ssLError".lower(),
    "unable to reach",
    "network is unreachable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)

# The sets this repository proves. ``ui.txt`` is a superset of
# ``development.txt``, which is a superset of ``runtime.txt``.
DEFAULT_REQUIREMENTS = (
    Path("constraints") / "runtime.txt",
    Path("constraints") / "ui.txt",
)


def _looks_like_an_outage(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _OUTAGE_MARKERS)


def _records(payload: object) -> list[dict[str, object]]:
    """The dependency records of a pip-audit JSON report."""
    if isinstance(payload, dict):
        payload = payload.get("dependencies")
    if not isinstance(payload, list):
        return []
    return [record for record in payload if isinstance(record, dict)]


def classify(returncode: int, stdout: str, stderr: str) -> tuple[str, list[dict[str, object]]]:
    """Decide the outcome from what ``pip-audit`` produced."""
    findings: list[dict[str, object]] = []
    if stdout.strip():
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return FAILURE, findings
        records = _records(payload)
        if not records and not isinstance(payload, (list, dict)):
            return FAILURE, findings
        for record in records:
            vulns = record.get("vulns")
            if not isinstance(vulns, list):
                continue
            for vuln in vulns:
                if isinstance(vuln, dict):
                    findings.append(
                        {
                            "name": record.get("name"),
                            "version": record.get("version"),
                            "id": vuln.get("id"),
                            "fix_versions": vuln.get("fix_versions"),
                        }
                    )
        if findings:
            return VULNERABLE, findings
        if returncode == 0:
            return CLEAN, findings
    if returncode == 0:
        return CLEAN, findings
    if _looks_like_an_outage(stderr) or _looks_like_an_outage(stdout):
        return UNAVAILABLE, findings
    return FAILURE, findings


def run_audit(python_executable: str, requirements: Path) -> tuple[int, str, str]:
    """Audit one pinned requirement file.

    ``--no-deps`` makes pip-audit read the pins from the file instead of
    resolving them, so the audited set is exactly the file and the check does
    not depend on what the runner has installed.
    """
    command = [
        python_executable,
        "-m",
        "pip_audit",
        "--requirement",
        str(requirements),
        "--no-deps",
        "--progress-spinner=off",
        "--format=json",
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    return completed.returncode, completed.stdout, completed.stderr


def _summary(outcome: str, findings: list[dict[str, object]], audited: list[str]) -> str:
    lines = ["dependency audit", f"outcome: {outcome}", "audited: " + ", ".join(audited)]
    lines.append(f"findings: {len(findings)}")
    for finding in findings:
        lines.append(
            "vulnerability: "
            f"{finding.get('name')} {finding.get('version')} {finding.get('id')} "
            f"fix={finding.get('fix_versions')}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dependency-audit.json"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--requirements",
        action="append",
        type=Path,
        default=None,
        help="a pinned requirement file; repeatable. Defaults to the repository sets.",
    )
    args = parser.parse_args(argv)
    requirements = tuple(args.requirements) if args.requirements else DEFAULT_REQUIREMENTS
    findings: list[dict[str, object]] = []
    outcome = CLEAN
    audited: list[str] = []
    for path in requirements:
        if not path.is_file():
            print(f"::error::pinned requirement file is missing: {path}")
            return 1
        returncode, stdout, stderr = run_audit(args.python, path)
        audited.append(str(path))
        current, found = classify(returncode, stdout, stderr)
        findings.extend(found)
        if current == VULNERABLE:
            outcome = VULNERABLE
        elif current == FAILURE and outcome != VULNERABLE:
            outcome = FAILURE
        elif current == UNAVAILABLE and outcome == CLEAN:
            outcome = UNAVAILABLE
    record = {
        "outcome": outcome,
        "findings": findings,
        "tool": "pip-audit",
        "scope": "the pinned requirement files this repository ships",
        "audited": audited,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(_summary(outcome, findings, audited))
    if outcome == VULNERABLE:
        for finding in findings:
            print(
                "::error::known vulnerability in "
                f"{finding.get('name')} {finding.get('version')}: {finding.get('id')}"
            )
        return 1
    if outcome == UNAVAILABLE:
        print("::warning::the advisory service could not be reached; this run is not a clean audit")
        return 0
    if outcome == FAILURE:
        print("::error::the dependency audit could not decide")
        return 1
    print("::notice::dependency audit: no known vulnerabilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
