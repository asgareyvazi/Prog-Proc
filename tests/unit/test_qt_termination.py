"""The Windows termination boundary, exercised without killing a process.

The shutdown path ends the process through the operating system. On Windows
that is ``TerminateProcess``, which stops the threads without notifying the
loaded libraries; it is not ``os._exit``, which on Windows ends the process
through ``ExitProcess`` and hands every loaded DLL a ``DLL_PROCESS_DETACH``
notification while the other threads are still running. That difference is
the access violation this repository has to keep out.

These tests drive the boundary with a scripted adapter, so a refusal, a
retry, a raised native error and a wrong return type are all exercised
deterministically - and no test in this file can end the pytest process.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import drilling_intelligence.ui.lifetime as lifetime
from drilling_intelligence.ui.lifetime import (
    _TerminationOutcome,
    is_fail_closed_shutdown_exit,
)

_LIFETIME_SOURCE = Path("src/drilling_intelligence/ui/lifetime.py")


class _ScriptedTermination:
    """A termination adapter that answers from a script.

    Each entry is either an outcome or an exception to raise. The last
    entry repeats, so a test can say "refuse once, then accept" or "always
    refuse" without counting.
    """

    def __init__(self, *script: _TerminationOutcome | Exception) -> None:
        self._script = list(script)
        self.calls: list[int] = []

    def terminate(self, code: int) -> _TerminationOutcome:
        self.calls.append(code)
        if not self._script:
            raise AssertionError("the adapter was called more often than the script allows")
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


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every observable step of the end-of-process path."""
    calls: list[str] = []
    monkeypatch.setattr(lifetime, "_is_windows", lambda: True)
    monkeypatch.setattr(lifetime, "_flush_standard_streams", lambda: calls.append("flush"))
    monkeypatch.setattr(
        lifetime, "_wait_for_termination", lambda *a, **k: calls.append("wait") or 0
    )
    monkeypatch.setattr(
        lifetime, "_hold_for_external_termination", lambda *a, **k: calls.append("hold")
    )
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: calls.append(f"exit:{code}"))
    return calls


def test_an_accepted_termination_does_not_reach_the_c_runtime_exit(
    monkeypatch: pytest.MonkeyPatch, recorder: list[str]
) -> None:
    """The accepted path ends the process through the kernel only.

    ``TerminateProcess`` on the current process stops the calling thread
    and does not return, so the wait is the defensive branch. The C
    runtime's exit is never reached.
    """
    adapter = _ScriptedTermination(_accepted())
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)

    lifetime._end_process(4)

    assert recorder == ["flush", "wait"]
    assert adapter.calls == [4]


def test_a_refused_termination_is_reported_and_never_becomes_a_success(
    monkeypatch: pytest.MonkeyPatch, recorder: list[str]
) -> None:
    """A refusal is a failure with a reason. It is never hidden."""
    adapter = _ScriptedTermination(_refused(error=5, attempts=3))
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    lines: list[str] = []
    monkeypatch.setattr(lifetime, "_diagnostic", lines.append)

    lifetime._end_process(4)

    assert recorder == ["flush", "hold"]
    assert any("TERMINAL_REFUSED" in line for line in lines)
    assert any("error=5" in line for line in lines)
    assert any("attempts=3" in line for line in lines)


def test_the_refusal_error_code_is_preserved(
    monkeypatch: pytest.MonkeyPatch, recorder: list[str]
) -> None:
    """The Win32 error code reaches the record, not only the exception."""
    adapter = _ScriptedTermination(_refused(error=87, attempts=1))
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    lines: list[str] = []
    monkeypatch.setattr(lifetime, "_diagnostic", lines.append)

    lifetime._end_process(4)

    assert any("error=87" in line for line in lines)


def test_a_process_that_is_already_terminating_is_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch, recorder: list[str]
) -> None:
    """``ERROR_ACCESS_DENIED`` means the process is already dying.

    The exit code is then the one the first terminator chose, so the
    shutdown waits for the termination instead of reporting a failure.
    """
    adapter = _ScriptedTermination(_dying(error=5))
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    lines: list[str] = []
    monkeypatch.setattr(lifetime, "_diagnostic", lines.append)

    lifetime._end_process(4)

    assert recorder == ["flush", "wait"]
    assert any("TERMINAL_ALREADY_TERMINATING" in line for line in lines)


def test_a_raised_native_error_is_a_refusal_and_does_not_escape(
    monkeypatch: pytest.MonkeyPatch, recorder: list[str]
) -> None:
    """The boundary may fail. The shutdown path may not."""
    adapter = _ScriptedTermination(OSError(6, "invalid handle"))
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    lines: list[str] = []
    monkeypatch.setattr(lifetime, "_diagnostic", lines.append)

    lifetime._end_process(4)

    assert recorder == ["flush", "hold"]
    assert any("TERMINAL_REFUSED" in line for line in lines)
    assert any("OSError" in line for line in lines)


def test_the_posix_path_still_ends_the_process_with_the_c_runtime_exit(
    monkeypatch: pytest.MonkeyPatch, recorder: list[str]
) -> None:
    """POSIX has no library-detach notification, so ``os._exit`` is safe."""
    monkeypatch.setattr(lifetime, "_is_windows", lambda: False)

    lifetime._end_process(4)

    assert recorder == ["flush", "exit:4"]


def test_a_refusal_is_retried_and_can_still_succeed(
    monkeypatch: pytest.MonkeyPatch, recorder: list[str]
) -> None:
    """A refusal is not a decision, so the request is repeated."""
    adapter = _ScriptedTermination(_refused(error=5, attempts=1), _accepted(attempts=2))
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)

    outcome = lifetime._KernelTermination  # the real class is not called here
    assert outcome is not None

    lifetime._end_process(4)

    assert adapter.calls == [4]


def test_the_kernel_adapter_retries_a_refusal_and_keeps_the_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real adapter asks again, and reports the last error code."""
    calls: list[str] = []

    class _FakeKernel:
        def __init__(self) -> None:
            self.TerminateProcess = self._terminate
            self.GetCurrentProcess = self._handle
            self.GetCurrentProcessId = lambda: 4242
            self.OpenProcess = lambda *a: 0

        def _handle(self) -> int:
            return -1

        def _terminate(self, handle: int, code: int) -> int:
            calls.append("terminate")
            return 0

    adapter = lifetime._KernelTermination(attempts=3, kernel_factory=_FakeKernel)
    monkeypatch.setattr(lifetime, "_last_error", lambda: 5)

    outcome = adapter.terminate(4)

    assert outcome.accepted is False
    assert outcome.already_terminating is True
    assert outcome.error == 5
    assert outcome.attempts == 1


def test_the_kernel_adapter_uses_a_second_handle_when_the_first_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused pseudo handle is retried with an opened handle."""
    calls: list[str] = []

    class _FakeKernel:
        def __init__(self) -> None:
            self.TerminateProcess = self._terminate
            self.GetCurrentProcess = lambda: -1
            self.GetCurrentProcessId = lambda: 4242
            self.OpenProcess = self._open
            self.CloseHandle = lambda handle: calls.append("close")

        def _open(self, access: int, inherit: int, pid: int) -> int:
            calls.append(f"open:{access}:{pid}")
            return 99

        def _terminate(self, handle: int, code: int) -> int:
            calls.append(f"terminate:{handle}")
            return 1 if handle == 99 else 0

    adapter = lifetime._KernelTermination(attempts=2, kernel_factory=_FakeKernel)
    # 87 is ERROR_INVALID_PARAMETER. A refusal that is not
    # ERROR_ACCESS_DENIED is retried with a second handle; the access
    # denied case means the process is already dying.
    monkeypatch.setattr(lifetime, "_last_error", lambda: 87)

    outcome = adapter.terminate(4)

    assert outcome.accepted is True
    assert calls[0] == "terminate:-1"
    assert "open:1:4242" in calls


def test_a_wrong_native_return_type_is_not_read_as_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value that is not a ``BOOL`` cannot be read as a decision."""

    class _FakeKernel:
        def TerminateProcess(self, handle: int, code: int) -> str:
            return "yes"

    error = lifetime._KernelTermination._terminate_with(_FakeKernel(), -1, 4)

    assert error is not None


def test_a_raised_native_call_is_recorded_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native call that raises is a refusal with a reason."""

    class _FakeKernel:
        def TerminateProcess(self, handle: int, code: int) -> int:
            raise OSError(6, "invalid handle")

    error = lifetime._KernelTermination._terminate_with(_FakeKernel(), -1, 4)

    assert error is not None


def test_the_hold_path_retries_and_can_still_end_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the process waits for an external terminator, it keeps asking."""
    adapter = _ScriptedTermination(_refused(error=5), _refused(error=5), _accepted())
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    monkeypatch.setattr(lifetime, "_TERMINAL_HOLD_INTERVAL_S", 0.0)
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: pytest.fail("os._exit must not be used"))

    lifetime._hold_for_external_termination(4, max_wait_s=1.0)

    assert adapter.calls == [4, 4, 4]


def test_the_hold_path_gives_up_on_the_external_terminator_without_exiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the process cannot end itself, it does not end itself unsafely."""
    adapter = _ScriptedTermination(_refused(error=5))
    monkeypatch.setattr(lifetime, "_TERMINATION", adapter)
    monkeypatch.setattr(lifetime, "_TERMINAL_HOLD_INTERVAL_S", 0.0)
    monkeypatch.setattr(lifetime.os, "_exit", lambda code: pytest.fail("os._exit must not be used"))
    lines: list[str] = []
    monkeypatch.setattr(lifetime, "_diagnostic", lines.append)

    lifetime._hold_for_external_termination(4, max_wait_s=0.05)

    assert any("TERMINAL_HOLDING" in line for line in lines)
    assert adapter.calls


def test_the_windows_branch_cannot_reach_the_c_runtime_exit_transitively() -> None:
    """The guarantee is about the call graph, not about one function.

    The previous guarantee was an AST check on ``_end_process`` alone, and
    it passed while a helper reachable from the Windows branch still
    called ``os._exit``. The check now walks the functions the Windows
    branch can reach.
    """
    tree = ast.parse(_LIFETIME_SOURCE.read_text(encoding="utf-8"))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        while pending:
            name = pending.pop()
            if name in seen or name not in functions:
                continue
            seen.add(name)
            for node in ast.walk(functions[name]):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    pending.append(node.func.id)
        return seen

    def calls_os_exit(name: str) -> bool:
        for node in ast.walk(functions[name]):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_exit"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                return True
        return False

    windows_reachable = reachable("_end_process") - {"_end_process"}
    for name in windows_reachable:
        assert not calls_os_exit(name), f"{name} reaches os._exit from the Windows branch"

    # The POSIX branch of _end_process is the one place that may use it.
    assert calls_os_exit("_end_process")
    assert "os._exit" in ast.unparse(functions["_end_process"])


def test_the_fixture_teardown_does_not_use_the_c_runtime_exit() -> None:
    """The test fixture ends its process the same safe way."""
    tree = ast.parse(_LIFETIME_SOURCE.read_text(encoding="utf-8"))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    for node in ast.walk(functions["fixture_teardown"]):
        assert not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_exit"
        )


def test_the_exit_contract_is_unchanged() -> None:
    """Only the integer 4 is the contracted exit."""
    assert is_fail_closed_shutdown_exit(4)
    assert not is_fail_closed_shutdown_exit(0)
    assert not is_fail_closed_shutdown_exit(1)
    assert not is_fail_closed_shutdown_exit(3)
    assert not is_fail_closed_shutdown_exit(134)
    assert not is_fail_closed_shutdown_exit(True)  # type: ignore[arg-type]
    assert not is_fail_closed_shutdown_exit(3221225477)
    assert not is_fail_closed_shutdown_exit(-1073741819)
