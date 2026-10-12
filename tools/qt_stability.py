#!/usr/bin/env python3
"""No-retry qualification of the historically aborting workbench node.

This is not the CI runner. A failed iteration fails the experiment even if a
later iteration passes. Fifteen passes are evidence of improved reliability,
not proof that a native fault can never recur.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path

HISTORIC_NODE = "tests/ui/test_workbench.py::test_real_fixture_workbench_current_history_evidence_plan_actual_and_detail"


def _workbench():
    spec = importlib.util.spec_from_file_location(
        "qt_workbench", Path(__file__).resolve().with_name("qt_workbench.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("qt_workbench.py is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--node", default=HISTORIC_NODE)
    parser.add_argument("--log-dir", type=Path, default=Path("qt-logs") / "stability")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        print("stability repeats must be at least 1", file=sys.stderr)
        return 2
    workbench = _workbench()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    records: list[tuple[int, int, str]] = []
    for ordinal in range(1, args.repeats + 1):
        log_path = args.log_dir / f"attempt-{ordinal:02d}.txt"
        started = time.monotonic()
        code, output = workbench._run_node(args.node, log_path)
        duration = time.monotonic() - started
        reason = workbench.stability_failure(code, output)
        header = (
            f"ordinal={ordinal} node={args.node} exit={code} "
            f"duration_s={duration:.3f} reason={reason or '-'}\n"
        )
        log_path.write_text(header + output, encoding="utf-8")
        records.append((ordinal, code, reason))
        print(header.strip())
        if reason:
            print(f"::error::stability attempt {ordinal} failed: {reason}")
    status, summary = workbench.qualify_stability(records)
    (args.log_dir / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary, end="")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write("### Qt stability qualification\n\n```\n")
            handle.write(summary)
            handle.write("```\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
