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
