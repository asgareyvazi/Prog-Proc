"""The shutdown stress experiment reports every attempt, not only the last one."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "qt_shutdown_stress",
    Path(__file__).resolve().parents[2] / "tools" / "qt_shutdown_stress.py",
)
assert _SPEC is not None and _SPEC.loader is not None
qt_shutdown_stress = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(qt_shutdown_stress)


def test_only_the_contracted_exit_honours_the_contract() -> None:
    from drilling_intelligence.ui.lifetime import SHUTDOWN_INCOMPLETE_EXIT

    assert qt_shutdown_stress.contract_failure(SHUTDOWN_INCOMPLETE_EXIT) == ""
    assert "0xC0000005" in qt_shutdown_stress.contract_failure(3221225477)
    assert "signal" in qt_shutdown_stress.contract_failure(-11)
    assert "not the contracted" in qt_shutdown_stress.contract_failure(0)
    assert "not the contracted" in qt_shutdown_stress.contract_failure(1)
    assert "not the contracted" in qt_shutdown_stress.contract_failure(134)


def test_every_attempt_is_recorded_and_a_late_pass_does_not_erase_a_fault(
    tmp_path: Path,
) -> None:
    codes = iter([3221225477, 4, 4, 4])

    def runner(mode: str, timeout: float) -> tuple[int, str, float]:
        code = next(codes)
        return code, "SHUTDOWN t=0.1 TERMINAL_ATTEMPT owner=monitor pid=1 tid=2\n", 0.5

    failures, records = qt_shutdown_stress.stress(
        ("deadline",),
        attempts=4,
        timeout=5.0,
        log_dir=tmp_path,
        runner=runner,
    )
    assert failures == 1
    assert len(records) == 4
    assert [record["exit"] for record in records] == [3221225477, 4, 4, 4]
    assert records[0]["reason"]
    assert not records[1]["reason"]
    written = sorted(path.name for path in tmp_path.iterdir())
    assert written == [f"deadline-attempt-{n:03d}.txt" for n in range(1, 5)]
    body = (tmp_path / "deadline-attempt-001.txt").read_text(encoding="utf-8")
    assert "exit=3221225477" in body
    assert "TERMINAL_ATTEMPT" in body


def test_a_timeout_is_a_failure_and_not_a_pass(tmp_path: Path) -> None:
    def runner(mode: str, timeout: float) -> tuple[int, str, float]:
        return -1, "child timed out after 30s\n", 30.0

    failures, records = qt_shutdown_stress.stress(
        ("deadline",),
        attempts=1,
        timeout=30.0,
        log_dir=tmp_path,
        runner=runner,
    )
    assert failures == 1
    assert records[0]["exit"] == -1
    assert "signal" in records[0]["reason"]


def test_the_experiment_writes_a_summary_that_names_every_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        qt_shutdown_stress,
        "stress",
        lambda modes, **_kwargs: (
            0,
            [
                {
                    "mode": "deadline",
                    "ordinal": 1,
                    "exit": 4,
                    "duration_s": 1.0,
                    "reason": "",
                    "log": "deadline-attempt-001.txt",
                    "markers": [],
                }
            ],
        ),
    )
    code = qt_shutdown_stress.main(
        ["--attempts", "1", "--mode", "deadline", "--log-dir", str(tmp_path)]
    )
    assert code == 0
    payload = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert payload["attempts"] == 1
    assert payload["failures"] == 0
    assert payload["contracted_exit"] == 4
    assert payload["records"][0]["exit"] == 4
    assert "python" in payload["versions"]


def test_a_failed_attempt_makes_the_experiment_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        qt_shutdown_stress,
        "stress",
        lambda modes, **_kwargs: (
            1,
            [
                {
                    "mode": "deadline",
                    "ordinal": 1,
                    "exit": 3221225477,
                    "duration_s": 1.0,
                    "reason": "windows access violation 0xC0000005",
                    "log": "deadline-attempt-001.txt",
                    "markers": [],
                }
            ],
        ),
    )
    code = qt_shutdown_stress.main(
        ["--attempts", "1", "--mode", "deadline", "--log-dir", str(tmp_path)]
    )
    assert code == 1
    payload = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert payload["failures"] == 1
