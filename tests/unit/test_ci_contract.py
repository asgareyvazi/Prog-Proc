"""Phase B contract tests: collection and shard jobs must share one install command."""

from __future__ import annotations

import re
from pathlib import Path


def _workflow() -> str:
    return Path(".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_collection_and_shard_jobs_use_the_same_shared_install_script() -> None:
    wf = _workflow()
    refs = re.findall(r"run: bash (tools/ci_install_qt\.sh)", wf)
    assert len(refs) >= 2, "collection and shard jobs must both call tools/ci_install_qt.sh"
    # no floating Qt install may remain anywhere in ci.yml
    assert 'pip install -e ".[dev,ui]"' not in wf


def test_shared_install_script_pins_ui_and_build_constraints() -> None:
    script = Path("tools/ci_install_qt.sh").read_text(encoding="utf-8")
    assert "-c constraints/ui.txt" in script
    assert "-c constraints/build.txt" in script
    assert '".[dev,ui]"' in script


def test_no_floating_qt_install_in_windows_workflow() -> None:
    wf = Path(".github/workflows/ci-windows.yml").read_text(encoding="utf-8")
    assert 'pip install -e ".[dev,ui]"' not in wf
