#!/usr/bin/env python3
"""Run the real Qt workbench tests with a narrow, recorded abort retry.

Exit 134 by itself is not evidence of a transient Qt/glibc abort. A retry is
allowed only when the attempt output carries that abort signature and does not
carry a test assertion or collection failure. The first attempt log is kept.
"""

from __future__ import annotations

import ast
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
# The warning is the lifetime fault. It is not, by itself, a retry qualifier.
QOBJECT_DELETED_DIRECTLY = "QObject: shared QObject was deleted directly"
PRODUCT_FAILURE_MARKERS = (
    "AssertionError",
    "FAILED tests/",
    "ERROR tests/",
    "ERROR collecting",
)
_SKIPPED = re.compile(r"\d+ skipped")


def _declared_in(body: list[ast.stmt], rel: str, prefix: list[str]) -> list[str]:
    stems: list[str] = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            stems.extend(_declared_in(node.body, rel, [*prefix, node.name]))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            stems.append("::".join([rel, *prefix, node.name]))
    return stems


def declared_stems(ui_dir: Path) -> list[str]:
    """AST inventory of tests/ui, including async tests and class methods.

    Parametrized cases are not expanded here. Pytest collection is the authority
    for those node ids; each collected case must still name one of these stems.
    """
    stems: list[str] = []
    for path in sorted(ui_dir.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        stems.extend(_declared_in(tree.body, f"tests/ui/{path.name}", []))
    return stems


def expected_test_names(ui_dir: Path) -> list[str]:
    """Bare test names. Prefer ``declared_stems`` for collection accounting."""
    return [stem.rsplit("::", 1)[-1] for stem in declared_stems(ui_dir)]


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


def _matches_stem(node: str, stem: str) -> bool:
    return node == stem or node.startswith(stem + "[")


def account_for_collection(expected: list[str], nodes: list[str]) -> str:
    """Return an error when collected nodes do not account for the declared stems.

    ``expected`` is a list of node stems (``tests/ui/test_x.py::TestA::test_b``).
    A parametrized case matches its stem. Duplicate node ids, a missing stem, an
    undeclared node, and an empty collection are all failures.
    """
    if not expected:
        return "tests/ui declares no workbench tests"
    if not nodes:
        return "pytest collected no workbench tests"
    if len(expected) != len(set(expected)):
        duplicated = sorted({stem for stem in expected if expected.count(stem) > 1})
        return f"duplicate declared workbench tests: {duplicated}"
    if len(nodes) != len(set(nodes)):
        return "duplicate collected workbench node ids"
    missing = [stem for stem in expected if not any(_matches_stem(node, stem) for node in nodes)]
    extra = [node for node in nodes if not any(_matches_stem(node, stem) for stem in expected)]
    if missing or extra:
        return (
            "workbench collection does not match tests/ui: "
            f"missing={missing or '-'} extra={extra or '-'}"
        )
    return ""


def stability_failure(exit_code: int, output: str) -> str:
    """Why one no-retry qualification attempt failed, or an empty string.

    A later passing iteration must not erase this. The normal one-retry policy
    is not consulted.
    """
    text = output or ""
    reasons: list[str] = []
    if exit_code != 0:
        reasons.append(f"exit {exit_code}")
    if QOBJECT_DELETED_DIRECTLY in text:
        reasons.append("shared QObject deleted directly")
    if any(marker in text for marker in ABORT_SIGNATURES):
        reasons.append("abort signature")
    if any(marker in text for marker in PRODUCT_FAILURE_MARKERS):
        reasons.append("assertion or collection failure")
    if _SKIPPED.search(text):
        reasons.append("skipped")
    return "; ".join(reasons)


def qualify_stability(attempts: list[tuple[int, int, str]]) -> tuple[int, str]:
    """Return (exit, summary). Any failed attempt fails the qualification."""
    lines = [f"stability attempts: {len(attempts)}"]
    failed = 0
    for ordinal, code, reason in attempts:
        state = "FAIL" if reason else "PASS"
        if reason:
            failed += 1
        lines.append(f"ATTEMPT {ordinal} EXIT {code} {state} {reason}".rstrip())
    lines.append(f"stability failed: {failed}")
    lines.append("stability result: " + ("FAIL" if failed else "PASS"))
    return (1 if failed else 0), "\n".join(lines) + "\n"


def attempt_result(attempts: list[tuple[int, int]]) -> str:
    """Distinguish a first-attempt pass from a recorded retry pass."""
    if attempts == [(1, 0)]:
        return "FIRST_PASS"
    if len(attempts) == 2 and attempts[-1] == (2, 0):
        return "RETRY_PASS"
    return "FAILED"


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
    log_dir = root / "qt-logs"
    log_dir.mkdir(exist_ok=True)
    expected = declared_stems(ui_dir)
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "--no-header", "tests/ui"],
        capture_output=True,
        text=True,
        check=False,
    )
    collect_text = (collected.stdout or "") + "\n" + (collected.stderr or "")
    (log_dir / "collection.txt").write_text(collect_text, encoding="utf-8")
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
    summary: list[str] = [f"collected {len(nodes)} workbench tests"]
    status = 0
    for node in nodes:
        code = 1
        output = ""
        attempts: list[tuple[int, int]] = []
        for attempt in (1, 2):
            log_path = log_dir / f"{_safe(node)}.attempt-{attempt}.txt"
            code, output = _run_node(node, log_path)
            attempts.append((attempt, code))
            line = f"NODE {node} ATTEMPT {attempt} EXIT {code}"
            summary.append(line)
            print(f"::notice::{line}")
            if code == 0:
                break
            if not qualifies_for_abort_retry(code, output):
                break
        outcome = attempt_result(attempts)
        summary.append(f"NODE {node} RESULT {outcome}")
        print(f"::notice::NODE {node} RESULT {outcome}")
        skipped = bool(_SKIPPED.search(output))
        if code != 0 or skipped:
            status = 1
            print(f"::error::NODE {node} EXIT {code}")
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            interesting = [
                line
                for line in lines
                if any(
                    token in line
                    for token in ("AssertionError", "FAIL ", "Error", "Traceback", "E   ")
                )
            ]
            chosen = interesting[-1] if interesting else (lines[-1] if lines else "")
            if chosen:
                print(f"::error::{node}: {chosen[:500]}")
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
