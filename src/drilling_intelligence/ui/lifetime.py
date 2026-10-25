"""Destroy Qt windows while QApplication is still alive.

PySide 6.12 aborts in ``destroyQCoreApplication`` when a top-level widget is
still alive at interpreter shutdown. ``close()`` only hides a window, and a
refused close means a worker is still using it. A refused close stays valid.
The caller must not quit or return while that widget is a live Qt object.

One monotonic budget covers close, worker join, polling, and ``processEvents``.
The wait between attempts is ``QThread.msleep`` of the time still left, so a
quiet loop cannot block past the deadline. There is no local ``QEventLoop``
whose deadline is checked only after it returns. ``processEvents`` can still
run one already-queued callback for longer than the allowance it was given.
A daemon thread waiting on the same monotonic deadline is the process-exit
ceiling for that case. Completion is accepted only while ``now < deadline``,
and that decision shares one lock with the monitor, so a late ``disarm`` cannot
turn an expired cleanup into success. The monitor does not call Qt, delete a
widget, or stop a worker. Exit 4 from that thread is a failed shutdown, not a
clean lifecycle. It is a practical ceiling under ordinary scheduling, not a
hard real-time guarantee: a callback that holds the GIL, or a native call that
stops Python threads, can delay the monitor until that call returns. Widget destruction uses
``shiboken6.delete``, not ``deleteLater``, so cleanup does not depend on a
deferred-delete event. Worker completion is observed by ``QThread.wait``.

Exactly one thread ends the process. The coordinator that polls the release
budget and the deadline monitor that guarantees the ceiling can both observe
the same expiry, and two threads ending the process at the same time is what
produced the Windows access violation.

The mechanism is the way the process was ended, not the number of ends. On
Windows ``os._exit`` is the C runtime's immediate exit, which ends the
process through ``ExitProcess``; ``ExitProcess`` calls the entry point of
every loaded DLL with ``DLL_PROCESS_DETACH`` while the other threads are
still running. The Qt and shiboken teardown then ran against a thread that
was still inside them - the faulting thread was inside the Qt event
filter, a Python callback invoked from C++ - and the result was
``0xC0000005`` instead of the contracted exit 4. ``TerminateProcess`` stops
the threads without notifying the loaded libraries, so the teardown never
runs at all. Microsoft says the same thing about ``ExitProcess``: if the
state of all the threads is unknown, call ``TerminateProcess`` rather than
``ExitProcess``.

The terminal exit is arbitrated as well: the first thread to claim it owns
the process, and the other stands down instead of starting a second
teardown. A refusal to terminate is reported with its error code and the
process waits for something else to end it; it is never turned into a
success and never ends the process through ``ExitProcess`` to reach the
exit code.

``aboutToQuit`` is not a safe place to join a worker or delete a window. That
signal runs inside the quit sequence, and a blocking join there can deadlock
if the worker still needs events. Supported routes are the ones that return
from ``QApplication.exec()`` — ``quit()``, ``exit()``, or the last window
closing — and then ``application_shutdown``. A platform or library path that
destroys the process without returning from ``exec()`` is not intercepted.
A ``quit()`` issued before ``exec()`` has started has no effect and is not
evidence of an external shutdown. ``QApplication`` also ignores a quit that
has started when a visible window refuses ``close()``, so ``exec()`` would
never return and this budget would never start. ``create_application`` accepts
that quit without closing windows inside the quit sequence.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

# One monotonic origin for the shutdown transcript. Every diagnostic line is
# stamped against it, so two threads that both reach a terminal decision can
# be ordered by the order they were written, not by the order they were
# discovered in a log.
_PROCESS_START = time.monotonic()

# Harness-only failure. The fixture uses it after a bounded wait still finds a
# live widget, so Py_Finalize does not turn that failure into the historical
# native abort. It is not a successful teardown.
FIXTURE_INCOMPLETE_EXIT = 3
SHUTDOWN_INCOMPLETE_EXIT = 4


def is_fail_closed_shutdown_exit(code: int) -> bool:
    """True only for the production fail-closed process code.

    Windows ``STATUS_ACCESS_VIOLATION`` (``0xC0000005``) is not this code.
    Python has reported that fault as ``3221225477`` and can also report the
    signed 32-bit form ``-1073741819``. Neither is exit 4.
    """
    return type(code) is int and code == SHUTDOWN_INCOMPLETE_EXIT


# Ceiling for a refused close after exec() has already returned. Distinct from
# the 10s worker join inside MainWindow.closeEvent. The join is not restarted,
# and this ceiling is not restarted, on each poll.
PRODUCTION_SHUTDOWN_WAIT_MS = 120_000
_POLL_SLICE_MS = 50


def remaining_budget_ms(deadline_ms: int, elapsed_ms: int) -> int:
    """Time left on the one shutdown clock. Negative means the budget is spent."""
    return deadline_ms - elapsed_ms


def worker_join_limit_ms(configured_ms: int, remaining_ms: int) -> int:
    """Keep the configured join when the shutdown budget still covers it.

    The last attempt must not start a join longer than the time still left.
    ``wait(0)`` does not block, so a worker that has already finished can still
    be released on the deadline without extending it.
    """
    return min(configured_ms, max(0, remaining_ms))


def wait_slice_ms(remaining_ms: int, *, slice_ms: int = _POLL_SLICE_MS) -> int:
    """A poll interval that cannot outlive the remaining budget."""
    return min(slice_ms, max(0, remaining_ms))


class _TerminalExit:
    """Exactly one thread may end the process.

    The coordinator that polls the release budget and the deadline monitor
    that guarantees the ceiling observe the same expiry from two different
    threads. Only the first claimant owns the process; the second stands
    down. Two concurrent terminations are what produced the Windows access
    violation: the coordinator ended the process through ``os._exit``, which
    on Windows is ``ExitProcess`` and hands every loaded DLL a
    ``DLL_PROCESS_DETACH`` notification while the monitor was still
    running, while the monitor ran ``TerminateProcess`` on a process that
    was already tearing down. The observed result was ``0xC0000005``
    instead of the contracted exit 4.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: str | None = None

    def claim(self, owner: str) -> bool:
        """Take ownership of the terminal exit. False when it is already owned."""
        with self._lock:
            if self._owner is not None:
                return False
            self._owner = owner
            return True

    def owner(self) -> str | None:
        """The thread label that owns the terminal exit, if any."""
        with self._lock:
            return self._owner


# One arbiter for the process. The invariant is about the process, not about
# one shutdown: a second end of the process is a defect wherever it starts.
_TERMINAL_EXIT = _TerminalExit()

# How long a thread that lost the race waits for the owner to end the
# process. The owner always ends it, so this is a backstop for a broken
# owner, not a normal wait.
_TERMINAL_STANDDOWN_TIMEOUT_S = 3.0

# How long a thread waits for a termination request the platform accepted.
# The process is already marked for termination, so this thread is normally
# killed inside the wait. The wait is a backstop, not a normal wait.
_TERMINATION_WAIT_S = 2.0

# Win32 WaitForSingleObject results. WAIT_OBJECT_0 (0) means the process
# handle is signaled — the process is terminating. WAIT_TIMEOUT (258) means
# the wait expired while the process is still alive. WAIT_FAILED
# (0xFFFFFFFF) means the wait itself failed; GetLastError carries the
# reason. Any other value is unexpected and must not be treated as success.
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF


@dataclass(frozen=True)
class ReleaseResult:
    """Outcome of one release pass. ``complete`` is validity, not a call count."""

    released: int
    refused: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.refused


def _apply_join_budget(widget: Any, remaining_ms: int) -> None:
    configured = getattr(widget, "_worker_join_timeout_ms", None)
    if not isinstance(configured, int):
        return
    widget._shutdown_join_budget_ms = worker_join_limit_ms(configured, remaining_ms)


def _clear_join_budget(widget: Any) -> None:
    import shiboken6

    if shiboken6.isValid(widget) and hasattr(widget, "_shutdown_join_budget_ms"):
        del widget._shutdown_join_budget_ms


def _pump(application: Any, budget_ms: int | None) -> None:
    """Deliver pending events without opening a loop that can block past the budget.

    A bare ``processEvents(int)`` would be read as flags, not as a time limit.
    The timed overload is the one that returns when the allowance is spent.
    """
    if budget_ms is not None and budget_ms <= 0:
        return
    if budget_ms is None:
        application.processEvents()
        return
    from PySide6.QtCore import QEventLoop

    application.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, budget_ms)


def release_top_level_widgets(
    application: Any,
    *,
    remaining_ms: int | None = None,
) -> ReleaseResult:
    """Close and delete widgets whose close was accepted.

    A widget that ignores ``close()`` is left valid. The returned names are the
    widgets that are still valid after the pass, checked with the wrapper, not
    the widgets we hoped to close. ``remaining_ms`` caps a worker join so the
    close cannot outlive the shutdown budget. ``None`` keeps the widget's own
    join, which is the direct-release test path.
    """
    import shiboken6
    from PySide6.QtWidgets import QApplication

    released = 0
    deferred = False
    for widget in list(QApplication.topLevelWidgets()):
        if not shiboken6.isValid(widget):
            continue
        # After exec() has returned, joining a still-running worker from close()
        # can fail to return on this host. Poll isRunning() instead, and close
        # only once the worker has finished. A user close while the loop is
        # still running still uses the 10s join inside closeEvent.
        running = getattr(widget, "owned_workers_running", None)
        if remaining_ms is not None and callable(running) and running():
            deferred = True
            continue
        if remaining_ms is not None:
            _apply_join_budget(widget, remaining_ms)
        accepted = widget.close() is not False
        _clear_join_budget(widget)
        if not accepted:
            continue
        if shiboken6.isValid(widget):
            shiboken6.delete(widget)
            released += 1
    # A full-budget pump would restart the allowance already spent inside close().
    # Deletion is shiboken6.delete, so a short slice is enough to deliver
    # bookkeeping. Do not pump while this pass only waited for a worker.
    # processEvents can deliver the close that was just refused. A Windows
    # child access-violated inside this coordinator while that pump still ran.
    # Skipping it is not proof the fault cannot recur.
    if not (deferred and released == 0):
        _pump(application, None if remaining_ms is None else wait_slice_ms(remaining_ms))
    refused: list[str] = []
    for widget in list(QApplication.topLevelWidgets()):
        if not shiboken6.isValid(widget):
            continue
        refused.append(widget.objectName() or type(widget).__name__)
    return ReleaseResult(released, tuple(refused))


def fail_closed_shutdown(result: ReleaseResult) -> None:
    """Stop the process without returning into interpreter teardown.

    This is a failed shutdown, not a safe lifecycle. It runs only when the
    production deadline is spent and a widget is still valid. It does not
    delete that widget and it does not kill a worker by ``QThread.terminate``.

    The thread that claims the terminal exit owns the process. A thread that
    finds it already claimed stands down: it must not start a second
    teardown, because two concurrent terminations fault the process on
    Windows.
    """
    if not _TERMINAL_EXIT.claim("coordinator"):
        _diagnostic(
            f"TERMINAL_STANDDOWN owner=coordinator pid={os.getpid()} "
            f"tid={threading.get_ident()} held_by={_TERMINAL_EXIT.owner()}"
        )
        _stand_down_for_terminal_exit()
        return
    names = ", ".join(result.refused) or "unknown"
    print(
        f"Qt shutdown incomplete; live windows remain ({names})",
        file=sys.stderr,
    )
    _diagnostic(f"TERMINAL_ATTEMPT owner=coordinator pid={os.getpid()} tid={threading.get_ident()}")
    _end_process(SHUTDOWN_INCOMPLETE_EXIT)


def _set_nonblocking(fd: int) -> None:
    """Put a POSIX descriptor into non-blocking mode.

    ``fcntl`` is imported here, not at module scope. Windows has no ``fcntl``,
    and a top-level import would stop ``drillintel-ui`` from loading even when
    Qt itself is installed.
    """
    import fcntl

    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _write_deadline_notice(fd: int, message: bytes) -> None:
    """Write the deadline line without holding the process on a full pipe.

    POSIX uses ``O_NONBLOCK``. Windows has no equivalent in the 3.11 floor, so
    the write runs on a daemon thread and this function waits at most 50 ms.
    A late write may be lost. That is a failed-shutdown diagnostic, not a
    record.
    """
    if os.name == "nt":
        done = threading.Event()

        def _attempt() -> None:
            try:
                os.write(fd, message)
            except (AttributeError, OSError, ValueError):
                pass
            finally:
                done.set()

        threading.Thread(target=_attempt, name="qt-deadline-notice", daemon=True).start()
        done.wait(0.05)
        return
    _set_nonblocking(fd)
    os.write(fd, message)


def _is_windows() -> bool:
    return os.name == "nt"


def _last_error() -> int:
    """The last Win32 error code, or 0 where the platform has none.

    ``ctypes.get_last_error`` only exists in a Windows build of ctypes,
    so the lookup is guarded: the module is imported and tested on POSIX
    as well, and a missing error code must not become a crash on the
    shutdown path.
    """
    import ctypes

    getter = getattr(ctypes, "get_last_error", None)
    if getter is None:
        return 0
    try:
        return int(getter())
    except (OSError, ValueError):
        return 0


def _diagnostic(message: str) -> None:
    """One shutdown transcript line that must survive the process exit.

    The line is written to stderr and flushed. A shutdown diagnostic that is
    still sitting in a buffer when the process is ended is not a record. The
    stamp is monotonic and relative to the import of this module, so the
    transcript of a child process can be read as a timeline.
    """
    try:
        sys.stderr.write(f"SHUTDOWN t={time.monotonic() - _PROCESS_START:.3f} {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


# Win32 error codes that decide what a refusal means.
_WINERROR_ACCESS_DENIED = 5

# How long the process waits between attempts to end itself while an
# external terminator is expected to act. The interval is short enough that
# a retry still happens inside the window a parent watchdog allows.
_TERMINAL_HOLD_INTERVAL_S = 1.0


@dataclass(frozen=True)
class _TerminationOutcome:
    """What the operating system said about a termination request.

    ``accepted`` means the kernel took the request and the process is
    being torn down. ``already_terminating`` means the kernel refused
    because the process is already dying - the request is not a failure,
    the exit code is the one the first terminator chose. ``error`` carries
    the Win32 error code of a refusal, so the reason is a record and not
    a swallowed exception.
    """

    accepted: bool
    already_terminating: bool
    error: int | None
    attempts: int
    detail: str = ""

    @property
    def refused(self) -> bool:
        """True when the kernel refused and the process is not dying."""
        return not self.accepted and not self.already_terminating


class _TerminationAdapter(Protocol):
    """The boundary between the shutdown path and the operating system.

    The real implementation asks the kernel. The tests substitute a
    scripted one, so a refusal can be exercised without a process-killing
    call inside the test runner.
    """

    def terminate(self, code: int) -> _TerminationOutcome: ...


def _load_windows_kernel() -> Any:
    """Load ``kernel32`` with the types the calls need.

    The types are explicit because an untyped call passes the 64-bit
    handle as a 32-bit argument. The loader is a separate function so a
    test can supply a fake kernel on a platform that has no
    ``ctypes.WinDLL``.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetCurrentProcessId.restype = wintypes.DWORD
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    return kernel


class _KernelTermination:
    """End this Windows process through the kernel, without the libraries.

    ``TerminateProcess`` stops every thread of the process, including the
    calling one, and **it does not return** when a process terminates
    itself. It also does *not* deliver ``DLL_PROCESS_DETACH`` to the
    loaded libraries, so the Qt and shiboken teardown never runs against
    a thread that is still inside them.

    That is the difference from ``os._exit``, and it is the whole point of
    this module. On Windows ``os._exit`` is the C runtime's immediate
    exit, which ends the process through ``ExitProcess``; ``ExitProcess``
    calls the entry point of every loaded DLL with ``DLL_PROCESS_DETACH``
    while the other threads are still running. A DLL teardown that runs
    against a live thread is what produced ``0xC0000005`` instead of the
    contracted exit 4 - the faulting thread was inside the Qt event
    filter, a Python callback invoked from C++, when the process died.
    Microsoft's own guidance is the same: if the state of all the threads
    is unknown, call ``TerminateProcess`` rather than ``ExitProcess``.

    Two handle strategies are used, because a refusal is not a decision:
    the pseudo handle from ``GetCurrentProcess`` and, if that is refused,
    a handle opened with ``PROCESS_TERMINATE`` for this process id.
    """

    def __init__(
        self, *, attempts: int = 3, kernel_factory: Callable[[], Any] | None = None
    ) -> None:
        self._attempts = max(1, attempts)
        self._kernel_factory = kernel_factory or _load_windows_kernel

    def terminate(self, code: int) -> _TerminationOutcome:
        """Ask the kernel to end this process. Never raises for a refusal."""
        kernel = self._kernel_factory()
        process_terminate = 0x0001  # PROCESS_TERMINATE

        last_error: int | None = None
        last_detail: str = ""
        attempts = 0
        for _ in range(self._attempts):
            attempts += 1
            handle = kernel.GetCurrentProcess()
            error = self._terminate_with(kernel, handle, code)
            if error is None:
                # In a real Windows process TerminateProcess on the current
                # process does not return — the calling thread is stopped.
                # A fake adapter that returns success is test-only; in
                # production this return is unreachable.
                return _TerminationOutcome(
                    accepted=True, already_terminating=False, error=None, attempts=attempts
                )
            last_error = error
            if error == _WINERROR_ACCESS_DENIED:
                # The process is already being torn down. The exit code is
                # the one the first terminator chose, and this process is
                # not going to survive this call.
                return _TerminationOutcome(
                    accepted=False,
                    already_terminating=True,
                    error=error,
                    attempts=attempts,
                )
            opened = kernel.OpenProcess(process_terminate, False, kernel.GetCurrentProcessId())
            if opened:
                error = self._terminate_with(kernel, opened, code)
                # Close the opened handle on every path that owns it.
                # A successful TerminateProcess on the current process does
                # not return in production, so the close after success is
                # reachable only through a fake adapter; it is still closed
                # to keep the fake leak-free. A failure to close must not
                # turn a refused termination into success, and the original
                # termination error is preserved.
                close_error: int | None = None
                close_detail: str = ""
                try:
                    close_ok = kernel.CloseHandle(opened)
                    if close_ok is False or close_ok == 0:
                        close_error = _last_error() or 1
                        close_detail = f"CloseHandle returned {close_ok!r}"
                except (AttributeError, OSError, TypeError, ValueError) as exc:
                    close_error = getattr(exc, "winerror", None) or _last_error() or 1
                    close_detail = f"{type(exc).__name__}: {exc}"
                if close_error is not None:
                    _diagnostic(
                        f"TERMINAL_CLOSE_FAILED handle={opened} error={close_error} detail={close_detail or '-'}"
                    )
                if error is None:
                    # Success via opened handle — see non-return note above.
                    return _TerminationOutcome(
                        accepted=True,
                        already_terminating=False,
                        error=None,
                        attempts=attempts,
                    )
                last_error = error
                # Preserve close failure as secondary diagnostic, not as primary error.
                if close_error is not None:
                    last_detail = last_detail if last_detail else f"close_failed={close_error}:{close_detail}"
                if error == _WINERROR_ACCESS_DENIED:
                    return _TerminationOutcome(
                        accepted=False,
                        already_terminating=True,
                        error=error,
                        attempts=attempts,
                    )
                # Fall through to next attempt or final refusal; handle already closed.
            else:
                # OpenProcess failed — GetLastError carries reason, but we keep
                # the original TerminateProcess error as the primary refusal.
                open_err = _last_error()
                if open_err:
                    _diagnostic(f"TERMINAL_OPEN_FAILED error={open_err}")
        return _TerminationOutcome(
            accepted=False, already_terminating=False, error=last_error, attempts=attempts, detail=last_detail
        )

    @staticmethod
    def _terminate_with(kernel: Any, handle: Any, code: int) -> int | None:
        """One native call. ``None`` is success; an int is the error code.

        A native call that raises is a refusal with the reason recorded,
        and a return value that is not an integer is a refusal as well: a
        value that is not a ``BOOL`` cannot be read as a decision.
        """
        try:
            result = kernel.TerminateProcess(handle, code)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            return getattr(exc, "winerror", None) or _last_error() or 1
        if isinstance(result, bool):
            return None if result else _last_error() or 1
        if isinstance(result, int):
            return None if result else _last_error() or 1
        return 1


_TERMINATION: _TerminationAdapter = _KernelTermination()


def _wait_for_termination(timeout_s: float = _TERMINATION_WAIT_S) -> int:
    """Wait for a process that has been told to terminate.

    The wait is the defensive branch for a platform that accepted the
    termination request and returned anyway. On Windows the wait is a
    kernel wait on the process object, not a Python sleep loop: the
    process may already be tearing down, and running Python code in a
    process that is being killed is exactly the race this module exists
    to avoid.

    Returns the Win32 wait status (WAIT_OBJECT_0 etc.) on Windows, or
    WAIT_OBJECT_0 on POSIX after the sleep. The caller must inspect the
    result; WAIT_TIMEOUT, WAIT_FAILED, or an unexpected value must not
    be treated as success.
    """
    if _is_windows():
        return _kernel_wait_for_termination(timeout_s)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(0.02)
    return _WAIT_OBJECT_0


def _kernel_wait_for_termination(timeout_s: float) -> int:
    """Block in the kernel until this process is gone, or the wait runs out.

    Returns the Win32 wait status. WAIT_FAILED is returned with GetLastError
    available for diagnostics. An exception from the native layer is caught
    and mapped to WAIT_FAILED.
    """
    import ctypes
    from ctypes import wintypes

    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        milliseconds = max(0, int(timeout_s * 1000))
        result = kernel.WaitForSingleObject(kernel.GetCurrentProcess(), milliseconds)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        _diagnostic(f"TERMINAL_WAIT_FAILED exception={type(exc).__name__}: {exc}")
        return _WAIT_FAILED
    # Normalize: the kernel returns a DWORD; map Python types to int.
    try:
        status = int(result)
    except (TypeError, ValueError):
        _diagnostic(f"TERMINAL_WAIT_MALFORMED result={result!r}")
        return _WAIT_FAILED
    if status == _WAIT_FAILED:
        err = _last_error()
        _diagnostic(f"TERMINAL_WAIT_FAILED status=WAIT_FAILED error={err}")
    elif status not in (_WAIT_OBJECT_0, _WAIT_ABANDONED, _WAIT_TIMEOUT, _WAIT_FAILED):
        _diagnostic(f"TERMINAL_WAIT_UNEXPECTED status={status}")
    return status


def _hold_for_external_termination(code: int, *, max_wait_s: float | None = None) -> None:
    """Hold the process until something else ends it. Never ``os._exit``.

    The kernel refused to end this process. The only remaining way to end
    it from the inside is the C runtime's immediate exit, and on Windows
    that ends the process through ``ExitProcess``, which delivers
    ``DLL_PROCESS_DETACH`` to every loaded library while the other
    threads are still running. That is the access violation, so it is not
    used - not even to reach the exit code.

    So the refusal is reported and the process waits. The watchdog that
    bounds a parent's wait ends it; an operator ends it when there is no
    parent. The cost is stated rather than hidden: the exit code is then
    the terminator's and not the contracted 4, and the shutdown harness
    records that outcome as a timeout instead of a pass.

    The request is retried while waiting, because a refusal can be
    transient and the process may still be able to end itself cleanly.
    """
    _diagnostic(f"TERMINAL_HOLDING code={code}")
    deadline = None if max_wait_s is None else time.monotonic() + max_wait_s
    while deadline is None or time.monotonic() < deadline:
        time.sleep(_TERMINAL_HOLD_INTERVAL_S)
        outcome = _TERMINATION.terminate(code)
        if outcome.accepted:
            status = _wait_for_termination()
            if status == _WAIT_OBJECT_0:
                _diagnostic("TERMINAL_HOLDING accepted and wait signaled")
                return
            _diagnostic(f"TERMINAL_HOLDING wait status={status} after accepted")
            # Wait did not signal — keep holding and retrying.
            continue
        if outcome.already_terminating:
            _diagnostic("TERMINAL_ALREADY_TERMINATING")
            status = _wait_for_termination()
            if status == _WAIT_OBJECT_0:
                return
            _diagnostic(f"TERMINAL_HOLDING wait status={status} after already_terminating")
            continue
        _diagnostic(f"TERMINAL_HOLDING still waiting error={outcome.error}")


def _end_process(code: int) -> None:
    """End this process with ``code``. The caller owns the terminal exit.

    The standard streams are flushed first because a forced end does not
    flush them, and the test contract reads the child's markers. On POSIX
    the process is ended with ``os._exit``: that is the C runtime's
    immediate exit, it skips the interpreter's own teardown, and POSIX
    has no library-detach notification to deliver.

    On Windows the process is ended with ``TerminateProcess``, which
    stops the threads without notifying the loaded libraries. The
    ``os._exit`` path is not reachable there, directly or through a
    helper: on Windows ``os._exit`` ends the process through
    ``ExitProcess``, which calls the entry point of every loaded DLL with
    ``DLL_PROCESS_DETACH`` while the other threads are still running.
    A library teardown that runs against a live thread is what produced
    ``0xC0000005`` instead of the contracted exit 4.

    A refusal is not hidden and is not turned into a success. The refusal
    is reported with its Win32 error code, and the process waits for
    something else to end it.
    """
    _flush_standard_streams()
    if not _is_windows():
        os._exit(code)
        return
    _end_windows_process(code)


def _end_windows_process(code: int) -> None:
    """End this Windows process through the kernel, and report a refusal.

    The Windows half of ``_end_process``. It is a separate function so
    the two platform paths cannot bleed into each other, and so the
    refusal handling can be read - and tested - on its own.
    """
    try:
        outcome = _TERMINATION.terminate(code)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        # The boundary itself failed. That is a refusal with a reason,
        # not an error that may escape the shutdown path.
        outcome = _TerminationOutcome(
            accepted=False,
            already_terminating=False,
            error=getattr(exc, "winerror", None),
            attempts=1,
            detail=f"{type(exc).__name__}: {exc}",
        )
    if outcome.accepted:
        # ``TerminateProcess`` on the current process stops the calling
        # thread and does not return. The wait is the defensive branch
        # for a platform that returns anyway, and it is a kernel wait.
        # Inspect the wait result: only WAIT_OBJECT_0 means the process
        # handle is signaled. WAIT_TIMEOUT, WAIT_FAILED, or any
        # unexpected status must not be treated as success — the process
        # is still alive after claiming a terminal exit, so hold instead
        # of returning to Python/Qt teardown.
        status = _wait_for_termination()
        if status == _WAIT_OBJECT_0:
            return
        if status == _WAIT_TIMEOUT:
            _diagnostic(f"TERMINAL_WAIT_TIMEOUT code={code} after accepted")
        elif status == _WAIT_FAILED:
            _diagnostic(f"TERMINAL_WAIT_FAILED code={code} after accepted")
        else:
            _diagnostic(f"TERMINAL_WAIT_UNEXPECTED code={code} status={status} after accepted")
        _hold_for_external_termination(code)
        return
    if outcome.already_terminating:
        # The process is already being torn down. Its exit code is the
        # one the first terminator chose.
        _diagnostic(f"TERMINAL_ALREADY_TERMINATING code={code}")
        status = _wait_for_termination()
        if status == _WAIT_OBJECT_0:
            return
        if status == _WAIT_TIMEOUT:
            _diagnostic(f"TERMINAL_WAIT_TIMEOUT code={code} after already_terminating")
        elif status == _WAIT_FAILED:
            _diagnostic(f"TERMINAL_WAIT_FAILED code={code} after already_terminating")
        else:
            _diagnostic(f"TERMINAL_WAIT_UNEXPECTED code={code} status={status} after already_terminating")
        _hold_for_external_termination(code)
        return
    _diagnostic(
        f"TERMINAL_REFUSED code={code} error={outcome.error} "
        f"attempts={outcome.attempts} detail={outcome.detail or '-'}"
    )
    _hold_for_external_termination(code)


def _flush_standard_streams() -> None:
    """Flush the diagnostics. A lost shutdown line is not a record."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass


def _stand_down_for_terminal_exit() -> None:
    """Wait for the thread that owns the terminal exit to end the process.

    Returning into interpreter teardown while another thread is ending the
    process is the failure this guards. The owner ends the process and does
    not come back, so this thread is normally killed with it. The bounded
    wait is a backstop for an owner that failed to end the process, and it
    never returns: the backstop ends the process itself.
    """
    deadline = time.monotonic() + _TERMINAL_STANDDOWN_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(0.02)
    _diagnostic("TERMINAL_BACKSTOP owner=standdown")
    _end_process(SHUTDOWN_INCOMPLETE_EXIT)


def _terminal_exit() -> None:
    """Best-effort diagnostic, then exit 4. A blocked write must not delay it.

    The monitor is the backstop for a coordinator that is stuck inside a
    native call. It ends the process only when it owns the terminal exit.
    """
    if not _TERMINAL_EXIT.claim("monitor"):
        _diagnostic(
            f"TERMINAL_STANDDOWN owner=monitor pid={os.getpid()} "
            f"tid={threading.get_ident()} held_by={_TERMINAL_EXIT.owner()}"
        )
        _stand_down_for_terminal_exit()
        return
    message = b"Qt shutdown incomplete; deadline elapsed before release finished\n"
    try:
        _write_deadline_notice(sys.stderr.fileno(), message)
    except (AttributeError, ImportError, OSError, ValueError):
        pass
    _diagnostic(f"TERMINAL_ATTEMPT owner=monitor pid={os.getpid()} tid={threading.get_ident()}")
    _end_process(SHUTDOWN_INCOMPLETE_EXIT)


class _ShutdownDeadline:
    """One absolute monotonic deadline for the coordinator and its monitor.

    ``now < deadline`` may accept a completed cleanup. ``now >= deadline`` is
    expired. ``disarm`` and ``enforce`` share one lock and read the clock inside
    it, so a completion that crosses the boundary cannot disarm an expiry that
    has already been observed, and a late ``disarm`` cannot suppress exit 4.
    A repeated ``disarm`` keeps the first decision. The monitor does not call Qt.
    """

    def __init__(
        self,
        deadline_ms: int,
        *,
        clock: Callable[[], float] | None = None,
        terminal: Callable[[], None] | None = None,
        armed: bool = True,
    ) -> None:
        self._clock = clock or time.monotonic
        self._deadline = self._clock() + max(0, deadline_ms) / 1000
        self._terminal = terminal or _terminal_exit
        self._lock = threading.Lock()
        self._done = False
        self._expired = False
        self._exited = False
        self._wake = threading.Event()
        if armed:
            threading.Thread(
                target=self._run,
                name="qt-shutdown-deadline",
                daemon=True,
            ).start()

    def remaining_ms(self) -> int:
        return int((self._deadline - self._clock()) * 1000)

    def disarm(self) -> bool:
        """Accept completion only if it is still strictly before the deadline."""
        with self._lock:
            if self._done:
                return True
            if self._expired or self._clock() >= self._deadline:
                self._expired = True
                self._wake.set()
                return False
            self._done = True
            self._wake.set()
            return True

    def enforce(self) -> bool:
        """Own the terminal exit if the deadline is spent and completion was not accepted."""
        with self._lock:
            if self._done or self._exited:
                return False
            if self._clock() < self._deadline:
                return False
            self._expired = True
            self._exited = True
        self._terminal()
        return True

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._done:
                    return
                remaining = self._deadline - self._clock()
                due = remaining <= 0
            if due:
                self.enforce()
                return
            self._wake.wait(min(0.05, remaining))


def _coordinate(
    application: Any,
    code: int,
    *,
    deadline_ms: int,
    on_expire: str,
) -> tuple[int, ReleaseResult]:
    """Retry release until the widgets are gone or the one budget is spent.

    The timer is started once. Later iterations subtract from that clock;
    they do not open a fresh production ceiling.
    """
    from PySide6.QtCore import QThread

    # One deadline. The fixture path uses it without arming the process exit.
    guard = _ShutdownDeadline(deadline_ms, armed=on_expire == "fail_closed")

    def _finish(exit_code: int, result: ReleaseResult) -> tuple[int, ReleaseResult]:
        if not guard.disarm():
            return _expired(exit_code, result, on_expire)
        return exit_code, result

    while True:
        remaining = guard.remaining_ms()
        if remaining <= 0:
            result = release_top_level_widgets(application, remaining_ms=0)
            if result.complete:
                return _finish(code, result)
            return _expired(code, result, on_expire)
        result = release_top_level_widgets(application, remaining_ms=remaining)
        if result.complete:
            return _finish(code, result)
        remaining = guard.remaining_ms()
        pause = wait_slice_ms(remaining)
        if pause <= 0:
            return _expired(code, result, on_expire)
        QThread.msleep(pause)
        # Same rule as the release pass: do not deliver events while the only
        # reason the window is still valid is a live worker.
        if result.released:
            _pump(application, guard.remaining_ms())


def _expired(
    code: int,
    result: ReleaseResult,
    on_expire: str,
) -> tuple[int, ReleaseResult]:
    if on_expire == "fail_closed":
        fail_closed_shutdown(result)
    return SHUTDOWN_INCOMPLETE_EXIT, result


def finish_qt_application(
    application: Any,
    code: int = 0,
    *,
    max_wait_ms: int | None = None,
    deadline_ms: int | None = None,
) -> tuple[int, ReleaseResult]:
    """Release every top-level widget before the caller may leave Qt.

    ``deadline_ms`` selects the production terminal: the same state machine as
    an ordinary ``drillintel-ui`` shutdown, with only the ceiling changed. The
    default ceiling is 120s and expiry calls ``fail_closed_shutdown``.
    ``max_wait_ms`` is the fixture path. It uses the same coordinator but
    returns an incomplete result instead of exiting, so the fixture can record
    exit 3 without hanging for the production ceiling. Passing both is rejected
    by using the production terminal only when ``max_wait_ms`` is omitted.
    """
    if max_wait_ms is not None:
        return _coordinate(
            application,
            code,
            deadline_ms=max_wait_ms,
            on_expire="incomplete",
        )
    ceiling = PRODUCTION_SHUTDOWN_WAIT_MS if deadline_ms is None else deadline_ms
    return _coordinate(
        application,
        code,
        deadline_ms=ceiling,
        on_expire="fail_closed",
    )


def create_application(argv: list[str]) -> Any:
    """The ``QApplication`` used by ``drillintel-ui`` and the shutdown proof.

    Qt delivers ``quit()`` as ``QEvent::Quit``. ``QApplication`` closes every
    visible window and ignores that event if one window remains. A worker that
    refuses ``close()`` would then leave ``exec()`` running forever. Accepting
    the quit only leaves the loop. ``application_shutdown`` releases windows
    afterwards and is allowed to fail closed. Closing them in this filter would
    re-enter the path Qt uses to cancel the quit.
    """
    from PySide6.QtCore import QCoreApplication, QEvent, QObject
    from PySide6.QtWidgets import QApplication

    class _AcceptQuit(QObject):
        def eventFilter(self, watched: QObject, event: QEvent) -> bool:
            if event.type() == QEvent.Type.Quit:
                # Do not close windows here. A refused close makes Qt ignore
                # the quit, and the offscreen close path does not return while
                # a worker is still running.
                QCoreApplication.exit(0)
                return True
            return super().eventFilter(watched, event)

    application = QApplication(argv)
    accept_quit = _AcceptQuit(application)
    application.installEventFilter(accept_quit)
    # The Python wrapper must outlive the filter. Parenting alone is not enough
    # if the wrapper is collected and shiboken deletes the C++ object.
    application._accept_quit = accept_quit  # type: ignore[attr-defined]
    return application


def request_quit(application: Any) -> None:
    """Queue ``quit()`` onto the GUI thread.

    A direct quit from another thread asks the platform plugin for synchronous
    termination and then waits for a GUI flush. That wait does not return while
    the GUI thread is already inside quit. ``invokeMethod`` with a queued
    connection runs ``quit()`` on the GUI thread, where the event filter accepts
    it. ``exit(code)`` is a different call and is not rewritten to status 0.
    """
    import shiboken6
    from PySide6.QtCore import QMetaObject, Qt, QThread

    # A new Python wrapper is not the same object as ``application.thread()``.
    # Compare the C++ thread, not wrapper identity.
    current = QThread.currentThread()
    owner = application.thread()
    if shiboken6.getCppPointer(current) == shiboken6.getCppPointer(owner):
        application.quit()
        return
    queued = QMetaObject.invokeMethod(
        application,
        "quit",
        Qt.ConnectionType.QueuedConnection,
    )
    if not queued:
        raise RuntimeError("could not queue application quit onto the GUI thread")


def application_shutdown(
    application: Any,
    code: int = 0,
    *,
    deadline_ms: int | None = None,
) -> int:
    """The production path ``drillintel-ui`` runs after ``QApplication.exec``.

    ``deadline_ms`` changes only the duration. The terminal failure route stays
    process exit 4. A successful release returns ``code`` and does not quit
    again; the caller already left ``exec()``.
    """
    try:
        exit_code, result = finish_qt_application(application, code, deadline_ms=deadline_ms)
    except Exception:  # noqa: BLE001 - any release failure must still exit 4
        # A deleted wrapper or a native error during release must not return
        # into interpreter teardown. The diagnostic stays a fixed token: a
        # traceback would include a local path.
        fail_closed_shutdown(ReleaseResult(0, ("release-exception",)))
        return SHUTDOWN_INCOMPLETE_EXIT
    if not result.complete:
        fail_closed_shutdown(result)
    return exit_code


def fixture_teardown(application: Any, *, max_wait_ms: int = 15_000) -> None:
    """Session teardown. Do not quit while a top-level widget is still valid."""
    _code, result = finish_qt_application(application, 0, max_wait_ms=max_wait_ms)
    if not result.complete:
        print(
            "Qt fixture teardown incomplete; refusing to quit with live windows",
            file=sys.stderr,
        )
        # The same rule as the fail-closed exit: the process is ended
        # through the platform's own termination, never through
        # ``os._exit``, which on Windows ends the process through
        # ``ExitProcess`` and hands every loaded DLL a
        # ``DLL_PROCESS_DETACH`` notification while the other threads are
        # still running.
        _end_process(FIXTURE_INCOMPLETE_EXIT)
    application.quit()
    application.processEvents()
