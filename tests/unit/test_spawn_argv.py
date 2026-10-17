"""A configured program is started without a shell, on both path styles."""

from __future__ import annotations

from drilling_intelligence.integrations.base import spawn_argv


def test_windows_python_script_is_started_by_the_interpreter() -> None:
    argv = spawn_argv(
        [r"C:\Program Files\mineru\mineru_crash.py", "-o", r"C:\out dir"],
        platform_name="nt",
        interpreter=r"C:\Python311\python.exe",
    )
    assert argv == [
        r"C:\Python311\python.exe",
        r"C:\Program Files\mineru\mineru_crash.py",
        "-o",
        r"C:\out dir",
    ]


def test_posix_and_windows_executables_are_not_rewritten() -> None:
    assert spawn_argv(["/opt/mineru", "-p", "a"], platform_name="posix") == [
        "/opt/mineru",
        "-p",
        "a",
    ]
    assert spawn_argv(
        [r"C:\tools\mineru.exe", "-p", "a"],
        platform_name="nt",
        interpreter="python",
    ) == [r"C:\tools\mineru.exe", "-p", "a"]
    assert spawn_argv([], platform_name="nt") == []
