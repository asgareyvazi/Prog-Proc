"""The CI pytest runner keeps the child exit code and writes UTF-8."""

from __future__ import annotations

import sys
from pathlib import Path

from tools.ci_pytest import record_process


def test_record_process_keeps_a_failing_exit_and_utf8_text(tmp_path: Path) -> None:
    output = tmp_path / "pytest-output.txt"
    exit_file = tmp_path / "pytest-exit.txt"
    code = record_process(
        [sys.executable, "-c", "import sys; print('caf\u00e9'); sys.exit(3)"],
        output=output,
        exit_file=exit_file,
    )
    assert code == 3
    assert exit_file.read_text(encoding="utf-8") == "3\n"
    assert "caf\u00e9" in output.read_text(encoding="utf-8")
    assert b"\x00" not in output.read_bytes()
