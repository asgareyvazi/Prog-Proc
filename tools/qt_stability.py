#!/usr/bin/env python3
"""No-retry qualification of the historically aborting workbench node.

This is not the CI runner. A failed iteration fails the experiment even if a
later iteration passes. Fifteen passes are evidence of improved reliability,
not proof that a native fault can never recur.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
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


def runtime_versions() -> dict[str, str]:
    """Qt and Python versions. A missing toolkit is recorded, not invented."""
    versions = {"python": platform.python_version()}
    try:
        import PySide6
        from PySide6.QtCore import qVersion
    except (ImportError, OSError, RuntimeError):
        versions["pyside"] = "unavailable"
        versions["qt"] = "unavailable"
        return versions
    versions["pyside"] = str(PySide6.__version__)
    versions["qt"] = str(qVersion())
    return versions


def write_evidence_manifest(log_dir: Path, versions: dict[str, str]) -> Path:
    """Hash every evidence file already written. Relative names only."""
    files: list[dict[str, int | str]] = []
    for path in sorted(log_dir.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", "manifest.sha256"}:
            continue
        files.append(
            {
                "name": path.relative_to(log_dir).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    destination = log_dir / "manifest.json"
    attempts = len(list(log_dir.glob("attempt-*.txt")))
    destination.write_text(
        json.dumps(
            {"attempts": attempts, "files": files, "versions": versions},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    (log_dir / "manifest.sha256").write_text(f"{digest}  manifest.json\n", encoding="utf-8")
    return destination


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
    manifest = write_evidence_manifest(args.log_dir, runtime_versions())
    manifest_line = (args.log_dir / "manifest.sha256").read_text(encoding="utf-8").strip()
    summary += f"stability manifest: {manifest.name}\nstability manifest {manifest_line}\n"
    print(summary, end="")
    for line in summary.splitlines():
        if line.startswith("ATTEMPT ") or line.startswith("stability "):
            print(f"::notice::{line}")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write("### Qt stability qualification\n\n```\n")
            handle.write(summary)
            handle.write("```\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
