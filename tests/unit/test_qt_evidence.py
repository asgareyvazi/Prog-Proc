"""The Qt release archive is hashed from the logs, not rebuilt from a summary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path


def _evidence():
    path = Path("tools/qt_evidence.py")
    spec = importlib.util.spec_from_file_location("qt_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stability():
    path = Path("tools/qt_stability.py")
    spec = importlib.util.spec_from_file_location("qt_stability", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _archive(tmp_path: Path, *, repeats: int = 2, poison: str = "") -> Path:
    stability = _stability()
    root = tmp_path / "qt-logs"
    root.mkdir(parents=True)
    (root / "collection.txt").write_text("tests/ui/test_workbench.py::test_one\n", encoding="utf-8")
    (root / "versions.txt").write_text("qt-platform offscreen\n", encoding="utf-8")
    node = "tests/ui/test_workbench.py::test_one"
    (root / "summary.txt").write_text(
        "collected 1 workbench tests\n"
        f"NODE {node} ATTEMPT 1 EXIT 0\n"
        f"NODE {node} RESULT FIRST_PASS\n"
        "workbench tests executed: 1\n",
        encoding="utf-8",
    )
    (root / "tests_ui_test_workbench.py_test_one.attempt-1.txt").write_text(
        "passed\n",
        encoding="utf-8",
    )
    stability_root = root / "stability"
    blocks = []
    for historic in stability.HISTORIC_NODES:
        node_dir = stability_root / stability.stability_dirname(historic)
        node_dir.mkdir(parents=True)
        for ordinal in range(1, repeats + 1):
            body = f"ordinal={ordinal} node={historic} exit=0 duration_s=0.1 reason=-\n"
            if poison and ordinal == repeats:
                body += poison + "\n"
            (node_dir / f"attempt-{ordinal:02d}.txt").write_text(body, encoding="utf-8")
        blocks.append(f"NODE {historic}\nATTEMPT 1 exit=0 reason=-\n")
    blocks.append("stability nodes: 2")
    blocks.append("stability nodes failed: 0")
    blocks.append("stability result: PASS")
    (stability_root / "summary.txt").write_text("\n".join(blocks) + "\n", encoding="utf-8")
    nodes = [
        {
            "node": historic,
            "attempts": repeats,
            "failed": 0,
            "records": [
                {"ordinal": ordinal, "exit": 0, "reason": ""} for ordinal in range(1, repeats + 1)
            ],
        }
        for historic in stability.HISTORIC_NODES
    ]
    stability.write_evidence_manifest(stability_root, {"python": "3.11"}, nodes=nodes)
    return root


def test_archive_hashes_match_and_a_tampered_log_fails(tmp_path: Path) -> None:
    evidence = _evidence()
    root = _archive(tmp_path)
    evidence.write_manifest(root)
    assert evidence.verify_archive(root, repeats=2) == []
    checksum = (root / "evidence.sha256").read_text(encoding="utf-8")
    assert "evidence.json" in checksum
    listed = json.loads((root / "evidence.json").read_text(encoding="utf-8"))["files"]
    names = [item["path"] for item in listed]
    assert "evidence.json" not in names
    assert "evidence.sha256" not in names
    assert "stability/manifest.json" in names
    assert "stability/manifest.sha256" in names
    log = next(root.rglob("attempt-02.txt"))
    log.write_text(log.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    problems = evidence.verify_archive(root, repeats=2)
    assert any("hash or size mismatch" in problem for problem in problems)


def test_a_success_claim_with_an_abort_signature_or_a_missing_attempt_fails(
    tmp_path: Path,
) -> None:
    evidence = _evidence()
    poisoned = _archive(tmp_path / "poison", poison="Fatal Python error: Aborted")
    evidence.write_manifest(poisoned)
    problems = evidence.verify_archive(poisoned, repeats=2)
    assert any("abort signature" in problem for problem in problems)
    missing = _archive(tmp_path / "missing")
    stability = _stability()
    first = stability.HISTORIC_NODES[0]
    attempt = missing / "stability" / stability.stability_dirname(first) / "attempt-02.txt"
    attempt.unlink()
    evidence.write_manifest(missing)
    problems = evidence.verify_archive(missing, repeats=2)
    assert any("expected" in problem for problem in problems)
    escaped = _archive(tmp_path / "escape")
    evidence.write_manifest(escaped)
    document = json.loads((escaped / "evidence.json").read_text(encoding="utf-8"))
    document["files"].append({"path": "../secret.txt", "bytes": 1, "sha256": "0" * 64})
    (escaped / "evidence.json").write_text(json.dumps(document), encoding="utf-8")
    digest = hashlib.sha256((escaped / "evidence.json").read_bytes()).hexdigest()
    (escaped / "evidence.sha256").write_text(f"{digest}  evidence.json\n", encoding="utf-8")
    problems = evidence.verify_archive(escaped, repeats=2)
    assert any("unsafe" in problem or "does not match" in problem for problem in problems)


def test_a_self_consistent_false_nested_manifest_still_fails(tmp_path: Path) -> None:
    evidence = _evidence()
    root = _archive(tmp_path)
    nested = root / "stability" / "manifest.json"
    document = json.loads(nested.read_text(encoding="utf-8"))
    document["files"][0]["sha256"] = "0" * 64
    nested.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(nested.read_bytes()).hexdigest()
    checksum = root / "stability" / "manifest.sha256"
    checksum.write_text(f"{digest}  manifest.json\n", encoding="utf-8")
    before = nested.read_bytes()
    evidence.write_manifest(root)
    problems = evidence.verify_archive(root, repeats=2)
    assert any("does not match file" in problem for problem in problems)
    again = evidence.main([str(root), "--repeats", "2"])
    assert again == 1
    assert nested.read_bytes() == before


def test_malformed_or_incomplete_nested_manifests_fail_without_a_traceback(
    tmp_path: Path,
) -> None:
    evidence = _evidence()
    broken = _archive(tmp_path / "broken")
    nested = broken / "stability" / "manifest.json"
    nested.write_text("{", encoding="utf-8")
    digest = hashlib.sha256(nested.read_bytes()).hexdigest()
    (broken / "stability" / "manifest.sha256").write_text(
        f"{digest}  manifest.json\n", encoding="utf-8"
    )
    evidence.write_manifest(broken)
    problems = evidence.verify_archive(broken, repeats=2)
    assert any("not json" in problem for problem in problems)
    assert "Traceback" not in "\n".join(problems)
    typed = _archive(tmp_path / "typed")
    document = json.loads((typed / "stability" / "manifest.json").read_text(encoding="utf-8"))
    document["attempts"] = True
    document["files"].append(document["files"][0])
    (typed / "stability" / "manifest.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256((typed / "stability" / "manifest.json").read_bytes()).hexdigest()
    (typed / "stability" / "manifest.sha256").write_text(
        f"{digest}  manifest.json\n", encoding="utf-8"
    )
    evidence.write_manifest(typed)
    problems = evidence.verify_archive(typed, repeats=2)
    assert any("attempt count" in problem or "duplicate" in problem for problem in problems)
    absent = _archive(tmp_path / "absent")
    (absent / "stability" / "manifest.json").unlink()
    evidence.write_manifest(absent)
    problems = evidence.verify_archive(absent, repeats=2)
    assert any("missing" in problem for problem in problems)


def test_a_tampered_shutdown_transcript_fails_and_a_valid_one_is_hashed(tmp_path: Path) -> None:
    evidence = _evidence()
    root = _archive(tmp_path)
    transcript = root / "shutdown" / "real-qthread.json"
    transcript.parent.mkdir()
    transcript.write_text(
        json.dumps(
            {
                "mode": "real-qthread",
                "exit_code": 0,
                "markers": [
                    "LOOP_STARTED",
                    "QUIT_REQUESTED",
                    "EXEC_RETURNED",
                    "SHUTDOWN_CODE 0",
                ],
                "result": "observed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    evidence.write_manifest(root)
    assert evidence.verify_archive(root, repeats=2) == []
    listed = json.loads((root / "evidence.json").read_text(encoding="utf-8"))["files"]
    assert any(item["path"] == "shutdown/real-qthread.json" for item in listed)
    transcript.write_text(transcript.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    problems = evidence.verify_archive(root, repeats=2)
    assert any("hash or size mismatch" in problem for problem in problems)
