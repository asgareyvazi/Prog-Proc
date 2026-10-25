"""Windows termination edge cases that the stress experiment cannot cover safely.

The normal shutdown path has passed a 50-attempt post-fix stress run on Windows.
That experiment proves the contracted exit 4 when the kernel accepts the request.
These tests prove the contract when the kernel does not: a refusal is never
reported as success, a malformed return type is never read as a decision,
a handle opened for the fallback is always closed, and a thread that lost
the terminal race stands down instead of starting a second teardown.

All tests run through the injectable termination adapter. No test in this
file may end the pytest process with a real ``TerminateProcess`` or
``os._exit``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import drilling_intelligence.ui.lifetime as lifetime
from drilling_intelligence.ui.lifetime import (
    _TerminationOutcome,
    is_fail_closed_shutdown_exit,
)


class _ScriptedTermination:
    def __init__(self, *script: _TerminationOutcome | Exception) -> None:
        self._script = list(script)
        self.calls: list[int] = []

    def terminate(self, code: int) -> _TerminationOutcome:
        self.calls.append(code)
        if not self._script:
            raise AssertionError("adapter called more often than script allows")
        entry = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(entry, Exception):
            raise entry
        return entry


def _accepted(attempts: int = 1) -> _TerminationOutcome:
    return _TerminationOutcome(
        accepted=True, already_terminating=False, error=None, attempts=attempts
    )


def _refused(error: int | None = 5, attempts: int = 1) -> _TerminationOutcome:
    return _TerminationOutcome(
        accepted=False, already_terminating=False, error=error, attempts=attempts
    )


def _dying(error: int = 5, attempts: int = 1) -> _TerminationOutcome:
    return _TerminationOutcome(
        accepted=False, already_terminating=True, error=error, attempts=attempts
    )


def test_1_termination_succeeds_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lifetime, "_is_windows", lambda: True)
    monkeypatch.setattr(lifetime, "_flush_standard_streams", lambda: calls.append("flush"))
    monkeypatch.setattr(lifetime, "_wait_for_termination", lambda *a, **k: calls.append("wait") or 0)
    monkeypatch.setattr(
        lifetime, "_hold_for_external_termination", lambda *a, **k: calls.append("hold")
    )
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: calls.append(f"exit:{code}"))
    adapter = _ScriptedTermination(_accepted())
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    lifetime._end_process(4)
    assert calls == ["flush", "wait"]
    assert adapter.calls == [4]


def test_2_first_call_fails_and_later_retry_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifetime, "_TERMINAL_HOLD_INTERVAL_S", 0.0)
    # _hold_for_external_termination retries while waiting for an external terminator
    adapter = _ScriptedTermination(_refused(error=87), _refused(error=87), _accepted())
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    lifetime._hold_for_external_termination(4, max_wait_s=1.0)
    assert adapter.calls == [4, 4, 4]


def test_3_pseudo_handle_fails_and_openprocess_fallback_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class _FakeKernel:
        def __init__(self) -> None:
            self.GetCurrentProcess = lambda: -1
            self.GetCurrentProcessId = lambda: 4242
            self.OpenProcess = self._open
            self.CloseHandle = lambda h: closed.append(f"close:{h}")
            self.TerminateProcess = self._terminate

        def _open(self, access: int, inherit: bool, pid: int) -> int:
            return 99

        def _terminate(self, handle: int, code: int) -> int:
            # pseudo fails, opened succeeds
            return 0 if handle == -1 else 1

    adapter = lifetime._KernelTermination(attempts=2, kernel_factory=_FakeKernel)
    monkeypatch.setattr(lifetime, "_last_error", lambda: 87)
    outcome = adapter.terminate(4)
    assert outcome.accepted is True
    # opened handle succeeded, it must be closed (our fix closes even on success)
    assert "close:99" in closed


def test_4_all_retries_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lifetime, "_is_windows", lambda: True)
    monkeypatch.setattr(lifetime, "_flush_standard_streams", lambda: calls.append("flush"))
    monkeypatch.setattr(lifetime, "_wait_for_termination", lambda *a, **k: calls.append("wait") or 0)
    monkeypatch.setattr(
        lifetime, "_hold_for_external_termination", lambda *a, **k: calls.append("hold")
    )
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: calls.append(f"exit:{code}"))
    adapter = _ScriptedTermination(_refused(error=5, attempts=3))
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    lines: list[str] = []
    monkeypatch.setattr(lifetime, "_diagnostic", lines.append)
    lifetime._end_process(4)
    assert calls == ["flush", "hold"]
    assert any("TERMINAL_REFUSED" in line for line in lines)


def test_5_native_call_raises_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lifetime, "_is_windows", lambda: True)
    monkeypatch.setattr(lifetime, "_flush_standard_streams", lambda: calls.append("flush"))
    monkeypatch.setattr(lifetime, "_wait_for_termination", lambda *a, **k: calls.append("wait") or 0)
    monkeypatch.setattr(
        lifetime, "_hold_for_external_termination", lambda *a, **k: calls.append("hold")
    )
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: calls.append(f"exit:{code}"))
    adapter = _ScriptedTermination(OSError(6, "invalid handle"))
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    lines: list[str] = []
    monkeypatch.setattr(lifetime, "_diagnostic", lines.append)
    lifetime._end_process(4)
    assert calls == ["flush", "hold"]
    assert any("OSError" in line for line in lines)


def test_6_malformed_return_type_is_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeKernel:
        def TerminateProcess(self, handle: int, code: int) -> str:  # type: ignore[override]
            return "yes"  # type: ignore[return-value]

    error = lifetime._KernelTermination._terminate_with(_FakeKernel(), -1, 4)
    assert error is not None
    assert error == 1

    class _FakeKernelNone:
        def TerminateProcess(self, handle: int, code: int) -> None:  # type: ignore[override]
            return None  # type: ignore[return-value]

    error2 = lifetime._KernelTermination._terminate_with(_FakeKernelNone(), -1, 4)
    assert error2 is not None


def test_7_getlasterror_unavailable_zero_and_meaningful(monkeypatch: pytest.MonkeyPatch) -> None:
    # unavailable: ctypes.get_last_error missing -> _last_error returns 0
    monkeypatch.setattr(lifetime, "_last_error", lambda: 0)

    class _FakeKernelZero:
        def TerminateProcess(self, handle: int, code: int) -> int:
            return 0

    err = lifetime._KernelTermination._terminate_with(_FakeKernelZero(), -1, 4)
    # when GetLastError is 0, _terminate_with returns 1 as generic error
    assert err == 1

    # meaningful code
    monkeypatch.setattr(lifetime, "_last_error", lambda: 87)
    err2 = lifetime._KernelTermination._terminate_with(_FakeKernelZero(), -1, 4)
    assert err2 == 87

    # unavailable case already covered by _last_error returning 0
    # Also test that exception with winerror is preferred
    class _FakeKernelRaise:
        def TerminateProcess(self, handle: int, code: int) -> int:
            exc = OSError(2, "no such file")
            exc.winerror = 5  # type: ignore[attr-defined]
            raise exc

    err3 = lifetime._KernelTermination._terminate_with(_FakeKernelRaise(), -1, 4)
    assert err3 == 5


def test_8_closehandle_required_after_opened_handle_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []

    class _FakeKernel:
        def __init__(self) -> None:
            self.GetCurrentProcess = lambda: -1
            self.GetCurrentProcessId = lambda: 4242
            self.OpenProcess = lambda a, b, c: 99
            self.CloseHandle = closed.append
            self.calls = 0

        def TerminateProcess(self, handle: int, code: int) -> int:
            self.calls += 1
            return 0

    monkeypatch.setattr(lifetime, "_last_error", lambda: 87)
    adapter = lifetime._KernelTermination(attempts=1, kernel_factory=_FakeKernel)
    outcome = adapter.terminate(4)
    assert outcome.accepted is False
    # opened handle failed with 87 (not ACCESS_DENIED), so it should be closed
    assert closed == [99]

    # ACCESS_DENIED via pseudo handle does not open a second handle, so no close
    closed2: list[int] = []

    class _FakeKernel2:
        def __init__(self) -> None:
            self.GetCurrentProcess = lambda: -1
            self.GetCurrentProcessId = lambda: 4242
            self.OpenProcess = lambda a, b, c: 77
            self.CloseHandle = closed2.append

        def TerminateProcess(self, handle: int, code: int) -> int:
            return 0

    monkeypatch.setattr(lifetime, "_last_error", lambda: 5)
    adapter2 = lifetime._KernelTermination(attempts=1, kernel_factory=_FakeKernel2)
    outcome2 = adapter2.terminate(4)
    assert outcome2.already_terminating is True
    assert closed2 == []

    # ACCESS_DENIED via opened handle (pseudo failed with 87, opened failed with 5) must close
    closed3: list[int] = []

    class _FakeKernel3:
        def __init__(self) -> None:
            self.GetCurrentProcess = lambda: -1
            self.GetCurrentProcessId = lambda: 4242
            self.OpenProcess = lambda a, b, c: 77
            self.CloseHandle = closed3.append
            self.call_count = 0

        def TerminateProcess(self, handle: int, code: int) -> int:
            self.call_count += 1
            if self.call_count == 1:
                # pseudo handle: fail with 87
                lifetime._last_error = lambda: 87
                return 0
            # opened handle: fail with ACCESS_DENIED
            lifetime._last_error = lambda: 5
            return 0

    monkeypatch.setattr(lifetime, "_last_error", lambda: 87)
    adapter3 = lifetime._KernelTermination(attempts=1, kernel_factory=_FakeKernel3)
    outcome3 = adapter3.terminate(4)
    assert outcome3.already_terminating is True
    assert closed3 == [77]


def test_9_already_terminating_distinguished_from_live_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lifetime, "_is_windows", lambda: True)
    monkeypatch.setattr(lifetime, "_flush_standard_streams", lambda: calls.append("flush"))
    monkeypatch.setattr(lifetime, "_wait_for_termination", lambda *a, **k: calls.append("wait") or 0)
    monkeypatch.setattr(
        lifetime, "_hold_for_external_termination", lambda *a, **k: calls.append("hold")
    )
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: calls.append(f"exit:{code}"))

    adapter = _ScriptedTermination(_dying(error=5))
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    lifetime._end_process(4)
    assert calls == ["flush", "wait"]
    assert "hold" not in calls

    calls.clear()
    adapter2 = _ScriptedTermination(_refused(error=5))
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter2)
    lifetime._end_process(4)
    assert calls == ["flush", "hold"]


def test_10_parent_watchdog_and_child_deadline_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    # The deadline monitor and a parent watchdog both bound the process.
    # The monitor owns the terminal exit; the parent would kill the child
    # from outside. Verify the monitor still honors the deadline even when
    # the parent timeout is shorter than the child deadline.
    import subprocess
    import sys

    # Child that starts a deadline monitor and then waits
    script = (
        "from drilling_intelligence.ui.lifetime import _ShutdownDeadline\n"
        "import time\n"
        "_ShutdownDeadline(400)\n"
        "time.sleep(5)\n"
    )
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    elapsed = time.monotonic() - started
    # With watchdog timeout 2s, process should have been killed by monitor at 0.4s -> exit 4
    # But subprocess.run with timeout 2 will either see exit 4 or timeout?
    # Instead test that monitor fires quickly even under watchdog
    assert proc.returncode == 4 or proc.returncode is None
    assert elapsed < 1.5


def test_11_standdown_thread_encounters_deadline_backstop(monkeypatch: pytest.MonkeyPatch) -> None:
    # _stand_down_for_terminal_exit waits TERMINAL_STANDDOWN_TIMEOUT_S then ends process
    calls: list[str] = []

    monkeypatch.setattr(lifetime, "_TERMINAL_STANDDOWN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(lifetime, "_end_process", lambda code: calls.append(f"end:{code}"))
    # claim first
    import drilling_intelligence.ui.lifetime as lt

    monkeypatch.setattr(lt, "_TERMINAL_EXIT", lt._TerminalExit())
    assert lt._TERMINAL_EXIT.claim("monitor") is True
    # Now a second thread tries to claim and should stand down, then backstop
    start = time.monotonic()
    lt._stand_down_for_terminal_exit()
    elapsed = time.monotonic() - start
    assert elapsed < 1.0
    assert calls == ["end:4"]


def test_12_termination_adapter_contradicts_native_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Adapter says accepted but native semantics say it should have been refused
    # The caller must trust the adapter's classification: accepted means wait,
    # refused means hold, already_terminating means wait. Contradictory
    # adapter still drives correct branch.
    calls: list[str] = []
    monkeypatch.setattr(lifetime, "_is_windows", lambda: True)
    monkeypatch.setattr(lifetime, "_flush_standard_streams", lambda: calls.append("flush"))
    monkeypatch.setattr(lifetime, "_wait_for_termination", lambda *a, **k: calls.append("wait") or 0)
    monkeypatch.setattr(
        lifetime, "_hold_for_external_termination", lambda *a, **k: calls.append("hold")
    )
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: calls.append(f"exit:{code}"))

    # Contradictory: says accepted but error code is ACCESS_DENIED (which normally means already_terminating)
    contradictory = _TerminationOutcome(
        accepted=True, already_terminating=True, error=5, attempts=1
    )
    monkeypatch.setattr(lifetime, "_TERMINATION", _ScriptedTermination(contradictory))
    lifetime._end_process(4)
    # accepted takes precedence -> wait
    assert calls == ["flush", "wait"]


def test_exit_contract_rejects_all_non_contract_codes() -> None:
    assert is_fail_closed_shutdown_exit(4)
    assert not is_fail_closed_shutdown_exit(0)
    assert not is_fail_closed_shutdown_exit(3221225477)
    assert not is_fail_closed_shutdown_exit(-1073741819)
    assert not is_fail_closed_shutdown_exit(True)  # type: ignore[arg-type]
    assert not is_fail_closed_shutdown_exit(0xC0000005)


def test_refusal_never_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lifetime, "_is_windows", lambda: True)
    monkeypatch.setattr(lifetime, "_flush_standard_streams", lambda: calls.append("flush"))
    monkeypatch.setattr(lifetime, "_wait_for_termination", lambda *a, **k: calls.append("wait") or 0)
    monkeypatch.setattr(
        lifetime, "_hold_for_external_termination", lambda *a, **k: calls.append("hold")
    )
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: calls.append(f"exit:{code}"))
    for code in [5, 87, 1, 0]:
        calls.clear()
        adapter = _ScriptedTermination(_refused(error=code))
        monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
        lifetime._end_process(4)
        assert "wait" not in calls, f"refused {code} must not wait"
        assert "hold" in calls
        assert "exit" not in calls


def test_malformed_return_type_not_accepted_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeKernel:
        def TerminateProcess(self, handle: int, code: int) -> object:
            return object()

    err = lifetime._KernelTermination._terminate_with(_FakeKernel(), -1, 4)
    assert err == 1


def test_exactly_one_terminal_owner_can_end_process() -> None:
    arbiter = lifetime._TerminalExit()
    assert arbiter.claim("coordinator") is True
    assert arbiter.claim("monitor") is False
    assert arbiter.owner() == "coordinator"


def test_shutdown_child_exposes_unexpected_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate child that would exit with wrong code -> parent watchdog sees it
    calls: list[str] = []
    monkeypatch.setattr(lifetime, "_is_windows", lambda: True)
    monkeypatch.setattr(lifetime, "_flush_standard_streams", lambda: calls.append("flush"))
    # _wait_for_termination is defensive; it should not hide a wrong exit
    monkeypatch.setattr(lifetime, "_wait_for_termination", lambda *a, **k: calls.append("wait") or 0)
    monkeypatch.setattr(
        lifetime, "_hold_for_external_termination", lambda *a, **k: calls.append("hold")
    )
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: calls.append(f"exit:{code}"))
    adapter = _ScriptedTermination(_refused(error=87))
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    lines: list[str] = []
    monkeypatch.setattr(lifetime, "_diagnostic", lines.append)
    lifetime._end_process(4)
    # refusal is recorded, not hidden
    assert any("TERMINAL_REFUSED" in line for line in lines)
    assert any("error=87" in line for line in lines)


def test_windows_api_semantics_documented() -> None:
    source = Path("src/drilling_intelligence/ui/lifetime.py").read_text(encoding="utf-8")
    # Official semantics must be cited
    assert "TerminateProcess" in source
    assert "ExitProcess" in source
    assert "DLL_PROCESS_DETACH" in source
    assert "does not return" in source
    # The fix must be TerminateProcess, not ExitProcess/os._exit on Windows
    windows_branch = (
        source.split("def _end_process", 1)[1]
        .split("def _end_windows_process", 1)[1]
        .split("def _flush", 1)[0]
    )
    assert "TerminateProcess" in windows_branch or "_TERMINATION.terminate" in windows_branch
    assert "os._exit" not in windows_branch


def test_defensive_wait_is_kernel_wait_not_python_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path("src/drilling_intelligence/ui/lifetime.py").read_text(encoding="utf-8")
    wait_fn = source.split("def _wait_for_termination", 1)[1].split("def _kernel_wait", 1)[0]
    assert "_is_windows" in wait_fn
    assert "_kernel_wait_for_termination" in wait_fn
    kernel_wait = source.split("def _kernel_wait_for_termination", 1)[1].split(
        "def _hold_for_external", 1
    )[0]
    assert "WaitForSingleObject" in kernel_wait
    assert "GetCurrentProcess" in kernel_wait
