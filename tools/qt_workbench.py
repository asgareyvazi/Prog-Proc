#!/usr/bin/env python3
"""Run the real Qt workbench tests with a narrow, recorded abort retry.

Exit 134 by itself is not evidence of a transient Qt/glibc abort. A retry is
allowed only when the attempt output carries that abort signature and does not
carry a test assertion or collection failure. The first attempt log is kept.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ABORT_SIGNATURES = (
    "Fatal Python error: Aborted",
    "malloc_consolidate(): unaligned fastbin chunk detected",
    "terminate called without an active exception",
    "Aborted (core dumped)",
)
PRODUCT_FAILURE_MARKERS = (
    "AssertionError",
    "FAILED tests/",
    "ERROR tests/",
)
_TEST_DEF = re.compile(r"^def (test_[A-Za-z0-9_]+)\(", re.M)
_SKIPPED = re.compile(r"\d+ skipped")


def expected_test_names(ui_dir: Path) -> list[str]:
    """Test functions declared under tests/ui. Collection must match this set."""
    names: list[str] = []
    for path in sorted(ui_dir.glob("test_*.py")):
        names.extend(_TEST_DEF.findall(path.read_text(encoding="utf-8")))
    return names


def parse_collected_nodes(text: str) -> list[str]:
    nodes: list[str] = []
    for line in text.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if token.startswith("tests/") and "::" in token:
            nodes.append(token)
    return nodes


def qualifies_for_abort_retry(exit_code: int, output: str) -> bool:
    """True only for the recorded runner abort, never for an assertion failure.

    An empty log does not qualify. Exit 134 without the abort signature does not
    qualify. A log that also contains a pytest assertion or error does not qualify.
    """
    if exit_code != 134:
        return False
    text = output or ""
    if any(marker in text for marker in PRODUCT_FAILURE_MARKERS):
        return False
    return any(marker in text for marker in ABORT_SIGNATURES)


def account_for_collection(expected: list[str], nodes: list[str]) -> str:
    """Return an error string when the collected nodes are not the declared tests."""
    collected = [node.rsplit("::", 1)[-1] for node in nodes]
    missing = [name for name in expected if name not in collected]
    extra = [name for name in collected if name not in expected]
    if not nodes:
        return "pytest collected no workbench tests"
    if missing or extra or len(collected) != len(expected):
        return (
            f"workbench collection does not match tests/ui: "
            f"missing={missing or '-'} extra={extra or '-'} "
            f"collected={len(collected)} declared={len(expected)}"
        )
    return ""


def _safe(node: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", node)


def _run_node(node: str, log_path: Path) -> tuple[int, str]:
    command = [
        "xvfb-run",
        "-a",
        "-s",
        "-screen 0 1280x1024x24",
        sys.executable,
        "-X",
        "faulthandler",
        "-m",
        "pytest",
        "-q",
        node,
        "--tb=line",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    output = (completed.stdout or "") + (completed.stderr or "")
    log_path.write_text(output, encoding="utf-8")
    return completed.returncode, output


def main() -> int:
    root = Path.cwd()
    ui_dir = root / "tests" / "ui"
    expected = expected_test_names(ui_dir)
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "--no-header", "tests/ui"],
        capture_output=True,
        text=True,
        check=False,
    )
    collect_text = (collected.stdout or "") + "\n" + (collected.stderr or "")
    if collected.returncode != 0:
        print(collect_text, file=sys.stderr)
        print("::error::workbench test collection failed", file=sys.stderr)
        return collected.returncode or 2
    nodes = parse_collected_nodes(collect_text)
    mismatch = account_for_collection(expected, nodes)
    if mismatch:
        print(collect_text, file=sys.stderr)
        print(f"::error::{mismatch}", file=sys.stderr)
        return 2
    log_dir = root / "qt-logs"
    log_dir.mkdir(exist_ok=True)
    summary: list[str] = [f"collected {len(nodes)} workbench tests"]
    status = 0
    for node in nodes:
        code = 1
        output = ""
        for attempt in (1, 2):
            log_path = log_dir / f"{_safe(node)}.attempt-{attempt}.txt"
            code, output = _run_node(node, log_path)
            line = f"NODE {node} ATTEMPT {attempt} EXIT {code}"
            summary.append(line)
            print(f"::notice::{line}")
            if code == 0:
                break
            if not qualifies_for_abort_retry(code, output):
                break
        skipped = bool(_SKIPPED.search(output))
        if code != 0 or skipped:
            status = 1
            print(f"::error::NODE {node} EXIT {code}")
            tail = "\n".join(output.splitlines()[-8:])
            if tail:
                print(f"::error::{node}: {tail}")
    summary.append(f"workbench tests executed: {len(nodes)}")
    print(f"::notice::workbench tests executed: {len(nodes)}")
    text = "\n".join(summary) + "\n"
    (log_dir / "summary.txt").write_text(text, encoding="utf-8")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write("### Workbench Qt attempts\n\n```\n")
            handle.write(text)
            handle.write("```\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
