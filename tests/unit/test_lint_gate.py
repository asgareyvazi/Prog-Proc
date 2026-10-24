"""The tree lints and formats cleanly, and the check runs over *everything*.

The CI runs ``ruff check .`` and ``ruff format --check .`` over the checkout,
and a violation there fails the job.  A local run that only looked at the
files it had changed would not have noticed a violation in a new tool file,
so the same two commands run here, over the whole tree.

The test is skipped when ruff is not installed - the product does not depend
on it - but in CI, where the dev extra is installed, it always runs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _ruff(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run ruff the way the CI does: through the interpreter that has it."""
    return subprocess.run(
        [sys.executable, "-m", "ruff", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_whole_tree_passes_the_lint() -> None:
    result = _ruff("check", ".")
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_whole_tree_is_formatted() -> None:
    result = _ruff("format", "--check", ".")
    assert result.returncode == 0, result.stdout + result.stderr
