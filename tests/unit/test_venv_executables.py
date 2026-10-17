"""Virtualenv launchers keep the suffix the host process can execute."""

from __future__ import annotations

from pathlib import Path

from tools.venv_executables import venv_bindir, venv_executable


def test_posix_programs_live_in_bin_without_a_suffix() -> None:
    root = Path("/opt/drillintel venv")
    assert venv_bindir(root, platform_name="posix") == root / "bin"
    assert venv_executable(root, "python", platform_name="posix") == root / "bin" / "python"
    assert venv_executable(root, "pip", platform_name="posix") == root / "bin" / "pip"
    assert venv_executable(root, "drillintel", platform_name="posix") == root / "bin" / "drillintel"


def test_windows_programs_live_in_scripts_and_keep_spaces() -> None:
    root = Path("C:/Program Files/drillintel venv")
    python = venv_executable(root, "python", platform_name="nt")
    pip = venv_executable(root, "pip", platform_name="nt")
    cli = venv_executable(root, "drillintel", platform_name="nt")
    assert python == root / "Scripts" / "python.exe"
    assert pip == root / "Scripts" / "pip.exe"
    assert cli == root / "Scripts" / "drillintel.exe"
    assert " " in str(python)
    assert python.name == "python.exe"
    assert venv_executable(root, "python.exe", platform_name="nt").name == "python.exe"
