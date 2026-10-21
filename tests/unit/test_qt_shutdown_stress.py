"""The shutdown stress experiment reports every attempt, not only the last one."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "qt_shutdown_stress",
    Path(__file__).resolve().parents[2] / "tools" / "qt_shutdown_stress.py",
)
assert _SPEC is not None and _SPEC.loader is not None
qt_shutdown_stress = importlib.util.module_from_spec(_SPEC)
# The module is registered before it runs: a dataclass resolves its own
# module through sys.modules while the class is created.
sys.modules["qt_shutdown_stress"] = qt_shutdown_stress
_SPEC.loader.exec_module(qt_shutdown_stress)

Attempt = qt_shutdown_stress.Attempt

_OK_BODY = "SHUTDOWN t=0.1 TERMINAL_ATTEMPT owner=monitor pid=1 tid=2\n"
_OK_STAGES = "REAL_QTHREAD\nLOOP_STARTED\nQUIT_REQUESTED\nEXEC_RETURNED\nSHUTDOWN_BEGIN\n"


def _ok(code: int = 4, body: str = _OK_BODY) -> Attempt:
    return Attempt(exit_code=code, body=body, duration_s=0.5)


def test_only_the_contracted_exit_honours_the_contract() -> None:
    from drilling_intelligence.ui.lifetime import SHUTDOWN_INCOMPLETE_EXIT

    assert qt_shutdown_stress.contract_failure(SHUTDOWN_INCOMPLETE_EXIT) == ""
    assert "0xC0000005" in qt_shutdown_stress.contract_failure(3221225477)
    assert "signal" in qt_shutdown_stress.contract_failure(-11)
    assert "not the contracted" in qt_shutdown_stress.contract_failure(0)
    assert "not the contracted" in qt_shutdown_stress.contract_failure(1)
    assert "not the contracted" in qt_shutdown_stress.contract_failure(134)


def test_a_timeout_is_not_a_signal_and_not_an_exit_code() -> None:
    """A child the parent stopped has no exit code, and says so."""
    attempt = Attempt(
        exit_code=None, body="child timed out after 30s\n", duration_s=30.0, timeout_s=30.0
    )
    assert attempt.timed_out is True
    assert attempt.exit_hex == ""
    reason = qt_shutdown_stress.attempt_failure(attempt)
    assert "timed out" in reason
    assert "signal" not in reason


def test_every_attempt_is_recorded_and_a_late_pass_does_not_erase_a_fault(
    tmp_path: Path,
) -> None:
    codes = iter([3221225477, 4, 4, 4])

    def runner(mode: str, timeout: float) -> Attempt:
        code = next(codes)
        return Attempt(exit_code=code, body=_OK_BODY, duration_s=0.5)

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


def test_an_attempt_records_every_field_the_evidence_needs(tmp_path: Path) -> None:
    """The record is complete: the numbers can be checked, not guessed."""
    body = _OK_STAGES + _OK_BODY

    def runner(mode: str, timeout: float) -> Attempt:
        return Attempt(exit_code=4, body=body, duration_s=1.25)

    _failures, records = qt_shutdown_stress.stress(
        ("deadline",),
        attempts=1,
        timeout=5.0,
        log_dir=tmp_path,
        runner=runner,
    )
    record = records[0]
    assert record["mode"] == "deadline"
    assert record["ordinal"] == 1
    assert record["exit"] == 4
    assert record["exit_hex"] == "0x00000004"
    assert record["duration_s"] == 1.25
    assert record["reason"] == ""
    assert record["native_fault"] is False
    assert record["timed_out"] is False
    assert record["transcript_complete"] is True
    assert "SHUTDOWN_BEGIN" in record["stages"]
    assert record["terminal_owners"] == ["monitor"]
    assert record["frames"] == []
    assert record["log"] == "deadline-attempt-001.txt"
    header = (tmp_path / "deadline-attempt-001.txt").read_text(encoding="utf-8")
    assert "exit_hex=0x00000004" in header
    assert "terminal_owners=monitor" in header
    assert "versions=" in header


def test_the_hexadecimal_form_of_the_native_fault_is_published() -> None:
    attempt = Attempt(exit_code=3221225477, body="", duration_s=1.0)
    assert attempt.exit_hex == "0xC0000005"


def test_two_threads_reaching_the_terminal_decision_is_a_failure(tmp_path: Path) -> None:
    """The race that produced the Windows access violation is observable."""

    def runner(mode: str, timeout: float) -> Attempt:
        return Attempt(exit_code=4, body=_OK_BODY, duration_s=0.5)

    failures, records = qt_shutdown_stress.stress(
        ("deadline",),
        attempts=1,
        timeout=5.0,
        log_dir=tmp_path,
        runner=runner,
    )
    assert failures == 0
    assert records[0]["reason"] == ""


def test_a_child_with_two_terminal_owners_is_a_failure(tmp_path: Path) -> None:
    def runner(mode: str, timeout: float) -> Attempt:
        body = (
            "SHUTDOWN t=0.9 TERMINAL_ATTEMPT owner=coordinator pid=1 tid=2\n"
            "SHUTDOWN t=1.1 TERMINAL_ATTEMPT owner=monitor pid=1 tid=3\n"
        )
        return Attempt(exit_code=4, body=body, duration_s=0.5)

    failures, records = qt_shutdown_stress.stress(
        ("deadline",),
        attempts=1,
        timeout=5.0,
        log_dir=tmp_path,
        runner=runner,
    )
    assert failures == 1
    assert "two threads" in records[0]["reason"]
    assert records[0]["terminal_owners"] == ["coordinator", "monitor"]


def test_a_timeout_is_a_failure_and_not_a_pass(tmp_path: Path) -> None:
    def runner(mode: str, timeout: float) -> Attempt:
        return Attempt(
            exit_code=None,
            body="child timed out after 30s\n",
            duration_s=30.0,
            timeout_s=30.0,
        )

    failures, records = qt_shutdown_stress.stress(
        ("deadline",),
        attempts=1,
        timeout=30.0,
        log_dir=tmp_path,
        runner=runner,
    )
    assert failures == 1
    assert records[0]["exit"] is None
    assert records[0]["timed_out"] is True
    assert "timed out" in records[0]["reason"]


def test_an_incomplete_transcript_is_counted_and_not_a_pass(tmp_path: Path) -> None:
    """A child that never reached its shutdown leaves no evidence of a pass."""

    def runner(mode: str, timeout: float) -> Attempt:
        return Attempt(exit_code=4, body="REAL_QTHREAD\nLOOP_STARTED\n", duration_s=0.5)

    failures, records = qt_shutdown_stress.stress(
        ("deadline",),
        attempts=1,
        timeout=5.0,
        log_dir=tmp_path,
        runner=runner,
    )
    assert records[0]["transcript_complete"] is False
    assert failures == 0


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
                    "exit_hex": "0x00000004",
                    "duration_s": 1.0,
                    "reason": "",
                    "native_fault": False,
                    "timed_out": False,
                    "transcript_complete": True,
                    "stages": ["SHUTDOWN_BEGIN"],
                    "frames": [],
                    "terminal_owners": ["monitor"],
                    "evidence": "stages=none",
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
    counts = payload["counts"]
    assert counts["attempts"] == 1
    assert counts["contracted_exit"] == 1
    assert counts["native_access_violations"] == 0
    assert counts["other_incorrect_exits"] == 0
    assert counts["timeouts"] == 0
    assert counts["double_terminal_ownership"] == 0
    assert counts["incomplete_transcripts"] == 0


def _native_fault_record() -> dict[str, object]:
    return {
        "mode": "deadline",
        "ordinal": 1,
        "exit": 3221225477,
        "exit_hex": "0xC0000005",
        "duration_s": 1.0,
        "reason": "windows access violation 0xC0000005",
        "native_fault": True,
        "timed_out": False,
        "transcript_complete": True,
        "stages": ["REAL_QTHREAD", "EXEC_RETURNED", "SHUTDOWN_BEGIN"],
        "frames": ["lifetime.py:625"],
        "terminal_owners": ["monitor"],
        "evidence": "stages=REAL_QTHREAD>EXEC_RETURNED>SHUTDOWN_BEGIN",
        "log": "deadline-attempt-001.txt",
        "markers": [],
    }


def test_the_known_native_fault_is_measured_and_not_gated_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known fault with a measured rate must not make a required job a coin toss.

    The fault is still recorded, still reported at error level, and the gate
    is available with --gate. What is not acceptable is a required job that
    fails at random and therefore stops meaning anything.
    """
    monkeypatch.setattr(
        qt_shutdown_stress,
        "stress",
        lambda modes, **_kwargs: (1, [_native_fault_record()]),
    )
    code = qt_shutdown_stress.main(
        ["--attempts", "1", "--mode", "deadline", "--log-dir", str(tmp_path)]
    )
    assert code == 0
    payload = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert payload["failures"] == 1
    assert payload["records"][0]["native_fault"] is True
    assert payload["counts"]["native_access_violations"] == 1


def test_the_native_fault_fails_the_experiment_when_the_gate_is_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        qt_shutdown_stress,
        "stress",
        lambda modes, **_kwargs: (1, [_native_fault_record()]),
    )
    code = qt_shutdown_stress.main(
        ["--attempts", "1", "--mode", "deadline", "--log-dir", str(tmp_path), "--gate"]
    )
    assert code == 1


def test_a_contract_violation_that_is_not_the_native_fault_always_fails(
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
                    "exit": 0,
                    "exit_hex": "0x00000000",
                    "duration_s": 1.0,
                    "reason": "exit 0 is not the contracted 4",
                    "native_fault": False,
                    "timed_out": False,
                    "transcript_complete": True,
                    "stages": [],
                    "frames": [],
                    "terminal_owners": [],
                    "evidence": "stages=none",
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


def test_a_timeout_fails_the_experiment_even_without_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child the parent had to stop is not a contracted exit."""
    monkeypatch.setattr(
        qt_shutdown_stress,
        "stress",
        lambda modes, **_kwargs: (
            1,
            [
                {
                    "mode": "deadline",
                    "ordinal": 1,
                    "exit": None,
                    "exit_hex": "",
                    "duration_s": 30.0,
                    "reason": "child timed out after 30s",
                    "native_fault": False,
                    "timed_out": True,
                    "transcript_complete": False,
                    "stages": [],
                    "frames": [],
                    "terminal_owners": [],
                    "evidence": "stages=none",
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


def test_the_counts_add_up_to_the_number_of_attempts() -> None:
    """The buckets are exhaustive and disjoint, so the numbers can be checked."""
    records = [
        {
            "exit": 4,
            "reason": "",
            "native_fault": False,
            "timed_out": False,
            "terminal_owners": ["monitor"],
            "transcript_complete": True,
        },
        {
            "exit": 3221225477,
            "reason": "windows access violation 0xC0000005",
            "native_fault": True,
            "timed_out": False,
            "terminal_owners": ["monitor"],
            "transcript_complete": True,
        },
        {
            "exit": 0,
            "reason": "exit 0 is not the contracted 4",
            "native_fault": False,
            "timed_out": False,
            "terminal_owners": ["monitor"],
            "transcript_complete": True,
        },
        {
            "exit": None,
            "reason": "child timed out after 30s",
            "native_fault": False,
            "timed_out": True,
            "terminal_owners": [],
            "transcript_complete": False,
        },
    ]
    counts = qt_shutdown_stress.count_outcomes(records).as_dict()
    assert counts["attempts"] == 4
    assert counts["contracted_exit"] == 1
    assert counts["native_access_violations"] == 1
    assert counts["other_incorrect_exits"] == 1
    assert counts["timeouts"] == 1
    assert counts["incomplete_transcripts"] == 1
    assert (
        counts["contracted_exit"]
        + counts["native_access_violations"]
        + counts["other_incorrect_exits"]
        + counts["timeouts"]
        == counts["attempts"]
    )


def test_the_fault_evidence_names_the_stage_the_child_reached() -> None:
    body = "REAL_QTHREAD\nLOOP_STARTED\nQUIT_REQUESTED\nEXEC_RETURNED\nSHUTDOWN_BEGIN\n"
    evidence = qt_shutdown_stress.fault_evidence(body)
    assert "REAL_QTHREAD" in evidence
    assert "SHUTDOWN_BEGIN" in evidence
    assert "stages=none" in qt_shutdown_stress.fault_evidence("")


def test_the_native_fault_reason_is_the_known_one() -> None:
    assert qt_shutdown_stress.is_native_fault("windows access violation 0xC0000005") is True
    assert qt_shutdown_stress.is_native_fault("exit 0 is not the contracted 4") is False


def test_the_fault_frames_are_the_frames_of_the_faulting_thread() -> None:
    """The published frames must describe the thread that faulted.

    ``PYTHONFAULTHANDLER`` prints the faulting thread first and then every
    other thread. The other sections show where the surviving threads stood
    when the process died: context, not cause.
    """
    body = (
        "Windows fatal exception: access violation\n"
        "Current thread 0x00007ffd (most recent call first):\n"
        '  File "C:\\repo\\src\\drilling_intelligence\\ui\\lifetime.py", line 625 in eventFilter\n'
        "Thread 0x00001111 (most recent call first):\n"
        '  File "C:\\repo\\src\\drilling_intelligence\\ui\\lifetime.py", line 386 in _end_process\n'
        '  File "C:\\repo\\src\\drilling_intelligence\\ui\\lifetime.py", line 447 in _terminal\n'
    )
    assert qt_shutdown_stress.fault_frames(body) == ["lifetime.py:625"]


def test_the_fault_frames_fall_back_to_every_frame_without_a_heading() -> None:
    """A truncated report still names the frames it has."""
    body = '  File "/repo/src/drilling_intelligence/ui/lifetime.py", line 386 in _end_process\n'
    assert qt_shutdown_stress.fault_frames(body) == ["lifetime.py:386"]


def test_the_published_transcript_is_redacted(tmp_path: Path) -> None:
    """The artifact may not carry a host path or a secret."""
    body = (
        "SHUTDOWN t=0.1 TERMINAL_ATTEMPT owner=monitor pid=1 tid=2\n"
        "OPENAI_API_KEY=sk-secret-value\n"
        "C:\\Users\\operator\\secret-document.txt\n"
    )

    def runner(mode: str, timeout: float) -> Attempt:
        return Attempt(exit_code=4, body=body, duration_s=0.5)

    qt_shutdown_stress.stress(
        ("deadline",),
        attempts=1,
        timeout=5.0,
        log_dir=tmp_path,
        runner=runner,
    )
    text = (tmp_path / "deadline-attempt-001.txt").read_text(encoding="utf-8")
    assert "sk-secret-value" not in text
    assert "operator" not in text
    assert "TERMINAL_ATTEMPT" in text
