#!/usr/bin/env python3
"""No-retry qualification of the historically aborting workbench nodes.

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
import re
import sys
import time
from pathlib import Path

HISTORIC_NODES = (
    "tests/ui/test_workbench.py::test_real_fixture_workbench_current_history_evidence_plan_actual_and_detail",
    "tests/ui/test_workbench.py::test_presentation_filter_conflict_detail_and_explicit_error_states",
)


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


def qualify_nodes(
    groups: list[tuple[str, list[tuple[int, int, str]]]],
) -> tuple[int, str]:
    """Qualify every historic node. One failed attempt fails the whole gate."""
    workbench = _workbench()
    blocks: list[str] = []
    failed_nodes = 0
    for node, attempts in groups:
        status, summary = workbench.qualify_stability(attempts)
        blocks.append(f"NODE {node}\n{summary.rstrip()}")
        if status:
            failed_nodes += 1
    blocks.append(f"stability nodes: {len(groups)}")
    blocks.append(f"stability nodes failed: {failed_nodes}")
    blocks.append("stability result: " + ("FAIL" if failed_nodes else "PASS"))
    return (1 if failed_nodes else 0), "\n".join(blocks) + "\n"


def write_evidence_manifest(
    log_dir: Path,
    versions: dict[str, str],
    *,
    nodes: list[dict[str, object]] | None = None,
) -> Path:
    """Hash every evidence file already written. Relative names only."""
    files: list[dict[str, int | str]] = []
    for path in sorted(log_dir.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", "manifest.sha256"}:
            continue
        relative = path.relative_to(log_dir).as_posix()
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise RuntimeError("stability evidence path escaped the log directory")
        files.append(
            {
                "name": relative,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    destination = log_dir / "manifest.json"
    attempts = len(list(log_dir.rglob("attempt-*.txt")))
    destination.write_text(
        json.dumps(
            {
                "attempts": attempts,
                "files": files,
                "nodes": nodes or [],
                "versions": versions,
            },
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
    parser.add_argument("--node", action="append", default=None)
    parser.add_argument("--log-dir", type=Path, default=Path("qt-logs") / "stability")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        print("stability repeats must be at least 1", file=sys.stderr)
        return 2
    selected = tuple(args.node) if args.node else HISTORIC_NODES
    workbench = _workbench()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    groups: list[tuple[str, list[tuple[int, int, str]]]] = []
    node_records: list[dict[str, object]] = []
    for node in selected:
        digest = hashlib.sha256(node.encode("utf-8")).hexdigest()[:8]
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", node.rsplit("::", 1)[-1])[:48]
        node_dir = args.log_dir / f"{stem}-{digest}"
        node_dir.mkdir(parents=True, exist_ok=True)
        records: list[tuple[int, int, str]] = []
        written: list[dict[str, object]] = []
        for ordinal in range(1, args.repeats + 1):
            log_path = node_dir / f"attempt-{ordinal:02d}.txt"
            started = time.monotonic()
            code, output = workbench._run_node(node, log_path)
            duration = time.monotonic() - started
            reason = workbench.stability_failure(code, output)
            header = (
                f"ordinal={ordinal} node={node} exit={code} "
                f"duration_s={duration:.3f} reason={reason or '-'}\n"
            )
            log_path.write_text(header + output, encoding="utf-8")
            records.append((ordinal, code, reason))
            relative = log_path.relative_to(args.log_dir).as_posix()
            written.append(
                {
                    "ordinal": ordinal,
                    "exit": code,
                    "duration_s": round(duration, 3),
                    "reason": reason,
                    "log": relative,
                    "sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
                }
            )
            print(header.strip())
            if reason:
                print(f"::error::stability attempt {ordinal} failed: {reason}")
        groups.append((node, records))
        node_records.append(
            {
                "node": node,
                "attempts": len(written),
                "failed": sum(1 for item in written if item["reason"]),
                "records": written,
            }
        )
    status, summary = qualify_nodes(groups)
    (args.log_dir / "summary.txt").write_text(summary, encoding="utf-8")
    manifest = write_evidence_manifest(args.log_dir, runtime_versions(), nodes=node_records)
    manifest_line = (args.log_dir / "manifest.sha256").read_text(encoding="utf-8").strip()
    summary += f"stability manifest: {manifest.name}\nstability manifest {manifest_line}\n"
    print(summary, end="")
    for line in summary.splitlines():
        if line.startswith(("ATTEMPT ", "NODE ", "stability ")):
            print(f"::notice::{line[:200]}")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write("### Qt stability qualification\n\n```\n")
            handle.write(summary)
            handle.write("```\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
