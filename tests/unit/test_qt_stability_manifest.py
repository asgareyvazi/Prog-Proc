"""Stability evidence is hashed. A later pass must not replace a failed attempt."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "qt_stability", Path(__file__).resolve().parents[2] / "tools" / "qt_stability.py"
)
assert _SPEC is not None and _SPEC.loader is not None
qt_stability = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(qt_stability)


def test_manifest_hashes_every_attempt_and_records_versions(tmp_path: Path) -> None:
    log_dir = tmp_path / "stability"
    log_dir.mkdir()
    (log_dir / "attempt-01.txt").write_text("ATTEMPT 1 EXIT 134 FAIL\n", encoding="utf-8")
    (log_dir / "attempt-02.txt").write_text("ATTEMPT 2 EXIT 0 PASS\n", encoding="utf-8")
    (log_dir / "summary.txt").write_text("stability result: FAIL\n", encoding="utf-8")
    versions = {"python": "3.11.0", "pyside": "6.6.0", "qt": "6.6.0"}
    manifest = qt_stability.write_evidence_manifest(log_dir, versions)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["attempts"] == 2
    assert payload["versions"] == versions
    assert [item["name"] for item in payload["files"]] == [
        "attempt-01.txt",
        "attempt-02.txt",
        "summary.txt",
    ]
    for item in payload["files"]:
        body = (log_dir / str(item["name"])).read_bytes()
        assert item["bytes"] == len(body)
        assert item["sha256"] == hashlib.sha256(body).hexdigest()
        assert "/" not in str(item["name"])
    recorded = (log_dir / "manifest.sha256").read_text(encoding="utf-8").split()[0]
    assert recorded == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert payload["nodes"] == []


def test_a_late_failure_on_one_historic_node_fails_the_gate(tmp_path: Path) -> None:
    history = "tests/ui/test_workbench.py::test_real_fixture_workbench_current_history_evidence_plan_actual_and_detail"
    presentation = "tests/ui/test_workbench.py::test_presentation_filter_conflict_detail_and_explicit_error_states"
    history_attempts = [(ordinal, 0, "") for ordinal in range(1, 16)]
    presentation_attempts = [(ordinal, 0, "") for ordinal in range(1, 15)]
    presentation_attempts.append((15, 1, "assertion"))
    status, summary = qt_stability.qualify_nodes(
        [(history, history_attempts), (presentation, presentation_attempts)]
    )
    assert status == 1
    assert "ATTEMPT 15 EXIT 1 FAIL assertion" in summary
    assert history in summary
    assert presentation in summary
    assert "stability nodes failed: 1" in summary
    log_dir = tmp_path / "stability"
    nested = log_dir / "presentation-deadbeef"
    nested.mkdir(parents=True)
    (nested / "attempt-15.txt").write_text("ATTEMPT 15 EXIT 1 FAIL\n", encoding="utf-8")
    manifest = qt_stability.write_evidence_manifest(log_dir, {"python": "3.11.0"})
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["files"][0]["name"] == "presentation-deadbeef/attempt-15.txt"
    assert not payload["files"][0]["name"].startswith("/")
    assert ".." not in payload["files"][0]["name"]
    (log_dir / "attempt-01.txt").write_text("changed\n", encoding="utf-8")
    changed = hashlib.sha256((log_dir / "attempt-01.txt").read_bytes()).hexdigest()
    assert changed != payload["files"][0]["sha256"]


def test_missing_qt_is_recorded_rather_than_invented() -> None:
    versions = qt_stability.runtime_versions()
    assert versions["python"]
    assert versions["pyside"]
    assert versions["qt"]
    assert "Traceback" not in versions["pyside"]
    assert "/" not in versions["pyside"]
