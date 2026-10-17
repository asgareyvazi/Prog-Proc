#!/usr/bin/env python3
"""Run pytest and keep a UTF-8 log plus a one-integer exit file.

PowerShell redirection can write UTF-16 and a pipeline can replace the native
exit code. This runner does neither. It does not treat a non-zero pytest exit
as success, and it does not add retries.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def record_process(
    command: list[str],
    *,
    output: Path,
    exit_file: Path,
) -> int:
    """Run ``command``, store combined output as UTF-8, and return its exit code."""
    completed = subprocess.run(command, capture_output=True, shell=False, check=False)
    text = completed.stdout.decode("utf-8", "replace") + completed.stderr.decode("utf-8", "replace")
    output.write_text(text, encoding="utf-8", newline="\n")
    exit_file.write_text(f"{completed.returncode}\n", encoding="utf-8", newline="\n")
    sys.stdout.buffer.write(text.encode("utf-8", "replace"))
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("pytest-output.txt"))
    parser.add_argument("--exit-file", type=Path, default=Path("pytest-exit.txt"))
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    extra = list(args.pytest_args)
    if extra[:1] == ["--"]:
        extra = extra[1:]
    command = [sys.executable, "-m", "pytest", *extra]
    return record_process(command, output=args.output, exit_file=args.exit_file)


if __name__ == "__main__":
    raise SystemExit(main())
