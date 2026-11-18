#!/usr/bin/env python3
"""Run pytest while preserving its output incrementally and its exact exit code.

PowerShell redirection can write UTF-16 and a pipeline can replace a native
exit code. This runner merges the child's streams in arrival order, flushes each
chunk to a UTF-8 log immediately, and does not retry or reinterpret a non-zero
exit. If pytest aborts natively, the partial transcript survives and the
process exit record is written after the child has ended.
"""

from __future__ import annotations

import argparse
import codecs
import subprocess
import sys
from pathlib import Path


def record_process(
    command: list[str],
    *,
    output: Path,
    exit_file: Path,
    state_file: Path | None = None,
) -> int:
    """Stream combined child output to disk and stdout, then record its exit code."""
    output.parent.mkdir(parents=True, exist_ok=True)
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    if state_file is not None:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("RUNNING\n", encoding="utf-8", newline="\n")
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    with (
        output.open("w", encoding="utf-8", newline="\n") as log,
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            bufsize=0,
        ) as process,
    ):
        if process.stdout is None:  # pragma: no cover - PIPE is required above
            raise RuntimeError("pytest stdout pipe was not created")
        while chunk := process.stdout.read(8192):
            text = decoder.decode(chunk)
            if text:
                log.write(text)
                log.flush()
                sys.stdout.write(text)
                sys.stdout.flush()
        tail = decoder.decode(b"", final=True)
        if tail:
            log.write(tail)
            log.flush()
            sys.stdout.write(tail)
            sys.stdout.flush()
        return_code = process.wait()

    temporary = exit_file.with_name(exit_file.name + ".partial")
    temporary.write_text(f"{return_code}\n", encoding="utf-8", newline="\n")
    temporary.replace(exit_file)
    if state_file is not None:
        state_file.write_text(f"COMPLETED exit={return_code}\n", encoding="utf-8", newline="\n")
    return return_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("pytest-output.txt"))
    parser.add_argument("--exit-file", type=Path, default=Path("pytest-exit.txt"))
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    extra = list(args.pytest_args)
    if extra[:1] == ["--"]:
        extra = extra[1:]
    command = [sys.executable, "-m", "pytest", *extra]
    return record_process(
        command,
        output=args.output,
        exit_file=args.exit_file,
        state_file=args.state_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
