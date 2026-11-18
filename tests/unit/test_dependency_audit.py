"""The dependency audit must fail on a vulnerability and not on an outage."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "dependency_audit",
    Path(__file__).resolve().parents[2] / "tools" / "dependency_audit.py",
)
assert _SPEC is not None and _SPEC.loader is not None
dependency_audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dependency_audit)


def _report(*records: dict[str, object]) -> str:
    return json.dumps({"dependencies": list(records)})


def test_a_clean_report_is_clean() -> None:
    outcome, findings = dependency_audit.classify(0, _report(), "")
    assert outcome == dependency_audit.CLEAN
    assert findings == []


def test_a_known_vulnerability_is_a_failure() -> None:
    payload = _report(
        {
            "name": "urllib3",
            "version": "2.0.0",
            "vulns": [{"id": "PYSEC-2024-1", "fix_versions": ["2.2.0"]}],
        }
    )
    outcome, findings = dependency_audit.classify(1, payload, "")
    assert outcome == dependency_audit.VULNERABLE
    assert findings[0]["name"] == "urllib3"
    assert findings[0]["id"] == "PYSEC-2024-1"


def test_an_advisory_service_outage_is_not_a_vulnerability() -> None:
    outcome, findings = dependency_audit.classify(
        1,
        "",
        "requests.exceptions.ConnectionError: Max retries exceeded",
    )
    assert outcome == dependency_audit.UNAVAILABLE
    assert findings == []


def test_an_unexpected_error_is_a_failure_not_an_outage() -> None:
    outcome, _ = dependency_audit.classify(1, "", "pip-audit is not installed")
    assert outcome == dependency_audit.FAILURE


def test_a_malformed_report_is_a_failure() -> None:
    outcome, _ = dependency_audit.classify(0, "not json at all", "")
    assert outcome == dependency_audit.FAILURE


def test_the_tool_records_the_outcome_and_fails_on_a_vulnerability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _report(
        {
            "name": "urllib3",
            "version": "2.0.0",
            "vulns": [{"id": "PYSEC-2024-1", "fix_versions": ["2.2.0"]}],
        }
    )
    monkeypatch.setattr(dependency_audit, "run_audit", lambda _python, _path: (1, payload, ""))
    output = tmp_path / "dependency-audit.json"
    code = dependency_audit.main(["--output", str(output)])
    assert code == 1
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["outcome"] == dependency_audit.VULNERABLE
    assert record["findings"][0]["id"] == "PYSEC-2024-1"


def test_the_tool_records_an_outage_without_failing_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dependency_audit,
        "run_audit",
        lambda _python, _path: (1, "", "requests.exceptions.ConnectionError: timed out"),
    )
    output = tmp_path / "dependency-audit.json"
    code = dependency_audit.main(["--output", str(output)])
    assert code == 0
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["outcome"] == dependency_audit.UNAVAILABLE


def test_the_tool_fails_when_it_cannot_decide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dependency_audit, "run_audit", lambda _python, _path: (2, "", "boom"))
    output = tmp_path / "dependency-audit.json"
    code = dependency_audit.main(["--output", str(output)])
    assert code == 1
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["outcome"] == dependency_audit.FAILURE
