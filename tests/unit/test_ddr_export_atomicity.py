"""Cross-platform adversarial tests for DDR comparison export destinations."""

from __future__ import annotations

import concurrent.futures
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from drilling_intelligence.operations.ddr_comparison import write_ddr_comparison_export


def _payload(label: str) -> dict[str, Any]:
    return {
        "schema": "prog-proc.ddr-comparison/v1",
        "read_only": True,
        "approval_evaluated": False,
        "predecessor": {"report_id": label, "provenance": []},
        "successor": {"report_id": label, "provenance": []},
        "comparison": {
            "status": "OK",
            "conclusive": True,
            "has_changes": False,
            "fields": [],
            "activities": {"status": "OK", "items": [], "counts": {}},
            "issues": [],
        },
        "truncation": {
            "truncated": False,
            "included_activity_items": 0,
            "omitted_activity_items": 0,
        },
    }


def test_existing_file_symlink_and_directory_are_never_clobbered_by_default(tmp_path: Path) -> None:
    destination = tmp_path / "existing.json"
    destination.write_text("owner data", encoding="utf-8")
    with pytest.raises(FileExistsError, match="explicitly allow overwrite"):
        write_ddr_comparison_export(_payload("replacement"), destination, output_format="json")
    assert destination.read_text(encoding="utf-8") == "owner data"

    target = tmp_path / "symlink-target.json"
    target.write_text("symlink target data", encoding="utf-8")
    link = tmp_path / "linked.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable on this host: {exc}")
    with pytest.raises(FileExistsError, match="explicitly allow overwrite"):
        write_ddr_comparison_export(_payload("replacement"), link, output_format="json")
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "symlink target data"

    directory = tmp_path / "destination-directory"
    directory.mkdir()
    with pytest.raises((FileExistsError, IsADirectoryError, PermissionError)):
        write_ddr_comparison_export(_payload("replacement"), directory, output_format="json")
    assert directory.is_dir()
    assert list(tmp_path.glob("*.partial")) == []


def test_overwrite_replaces_symlink_not_its_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("target remains", encoding="utf-8")
    link = tmp_path / "export.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable on this host: {exc}")

    write_ddr_comparison_export(_payload("replaced"), link, output_format="json", overwrite=True)

    assert not link.is_symlink()
    assert "replaced" in link.read_text(encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "target remains"


def test_interrupted_temporary_write_cleans_up_and_leaves_no_final_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "interrupted.json"
    original_fsync = os.fsync

    def fail_fsync(_fd: int) -> None:
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="simulated interrupted write"):
        write_ddr_comparison_export(_payload("never-visible"), destination, output_format="json")
    monkeypatch.setattr(os, "fsync", original_fsync)
    assert not destination.exists()
    assert list(tmp_path.glob(".*.partial")) == []


def test_no_clobber_concurrent_writers_have_one_atomic_winner(tmp_path: Path) -> None:
    destination = tmp_path / "race.json"
    barrier = threading.Barrier(3)

    def write(label: str) -> str:
        barrier.wait(timeout=5)
        try:
            write_ddr_comparison_export(_payload(label), destination, output_format="json")
            return "written"
        except FileExistsError:
            return "exists"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, value) for value in ("writer-one", "writer-two")]
        barrier.wait(timeout=5)
        results = [future.result(timeout=10) for future in futures]

    assert sorted(results) == ["exists", "written"]
    final = destination.read_text(encoding="utf-8")
    assert ("writer-one" in final) ^ ("writer-two" in final)
    assert list(tmp_path.glob(".*.partial")) == []


def test_overwrite_race_never_exposes_partial_or_mixed_output(tmp_path: Path) -> None:
    destination = tmp_path / "overwrite-race.json"
    barrier = threading.Barrier(3)

    def write(label: str) -> None:
        barrier.wait(timeout=5)
        write_ddr_comparison_export(
            _payload(label), destination, output_format="json", overwrite=True
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, value) for value in ("writer-one", "writer-two")]
        barrier.wait(timeout=5)
        for future in futures:
            future.result(timeout=10)

    final = destination.read_text(encoding="utf-8")
    assert ("writer-one" in final) ^ ("writer-two" in final)
    assert final.endswith("\n")
    assert list(tmp_path.glob(".*.partial")) == []


def test_permission_failure_does_not_leave_final_or_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "permission.json"
    original_mkstemp = __import__("tempfile").mkstemp

    def fail_mkstemp(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
        raise PermissionError("simulated destination permission failure")

    monkeypatch.setattr(
        "drilling_intelligence.operations.ddr_comparison.tempfile.mkstemp", fail_mkstemp
    )
    with pytest.raises(PermissionError, match="simulated destination permission failure"):
        write_ddr_comparison_export(_payload("not-written"), destination, output_format="json")
    monkeypatch.setattr(
        "drilling_intelligence.operations.ddr_comparison.tempfile.mkstemp", original_mkstemp
    )
    assert not destination.exists()
    assert list(tmp_path.glob(".*.partial")) == []
