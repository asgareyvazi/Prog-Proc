"""Run-bound, deterministic pytest collection and execution sharding.

Every shard recollects the full suite, validates the canonical collection
manifest, and executes a stable SHA-256 partition. The result sidecar binds
phase-level reports to the exact repository, workflow, run, attempt, and SHA.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from tools.pytest_shard_contract import (
    ASSIGNMENT_ALGORITHM as _ASSIGNMENT_ALGORITHM,
)
from tools.pytest_shard_contract import (
    SCHEMA_VERSION as _SCHEMA_VERSION,
)
from tools.pytest_shard_contract import (
    assigned_nodeids,
    shard_for,
    validate_manifest,
)
from tools.pytest_shard_contract import (
    canonical as _canonical,
)
from tools.pytest_shard_contract import (
    manifest_payload as _contract_manifest_payload,
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _ini_digest(config: pytest.Config | None) -> str:
    ini = {} if config is None else dict(config.inicfg)
    normalized = {
        str(key): value for key, value in sorted(ini.items(), key=lambda pair: str(pair[0]))
    }
    return hashlib.sha256(_canonical({"pytest_ini": normalized})).hexdigest()


def _manifest_payload(
    nodeids: list[str],
    *,
    head_sha: str,
    run_id: str,
    run_attempt: str,
    repository: str = "LOCAL",
    workflow: str = "LOCAL",
    workflow_ref: str = "LOCAL",
    pytest_config_sha256: str | None = None,
) -> dict[str, Any]:
    return _contract_manifest_payload(
        nodeids,
        head_sha=head_sha,
        run_id=run_id,
        run_attempt=run_attempt,
        repository=repository,
        workflow=workflow,
        workflow_ref=workflow_ref,
        pytest_version=pytest.__version__,
        pytest_config_sha256=pytest_config_sha256 or _ini_digest(None),
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"required shard JSON is missing or malformed: {path.name}") from exc


def _skip_reason(report: Any) -> str:
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        return str(longrepr[-1])
    crash = getattr(longrepr, "reprcrash", None)
    if crash is not None:
        return str(getattr(crash, "message", ""))
    return str(longrepr or "")


class _ShardPlugin:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.manifest: dict[str, Any] | None = None
        self.selection: dict[str, Any] | None = None
        self.reports: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def _identity(self, option: str, env: str, default: str = "") -> str:
        return str(self.config.getoption(option) or os.environ.get(env, default))

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, session: pytest.Session, config: pytest.Config, items: list[Any]
    ) -> None:
        nodeids = [str(item.nodeid) for item in items]
        manifest = _manifest_payload(
            nodeids,
            head_sha=self._identity("--progproc-head-sha", "GITHUB_SHA", "LOCAL"),
            run_id=self._identity("--progproc-run-id", "GITHUB_RUN_ID", "LOCAL"),
            run_attempt=self._identity("--progproc-run-attempt", "GITHUB_RUN_ATTEMPT", "1"),
            repository=self._identity("--progproc-repository", "GITHUB_REPOSITORY", "LOCAL"),
            workflow=self._identity("--progproc-workflow", "GITHUB_WORKFLOW", "LOCAL"),
            workflow_ref=self._identity("--progproc-workflow-ref", "GITHUB_WORKFLOW_REF", "LOCAL"),
            pytest_config_sha256=_ini_digest(config),
        )
        self.manifest = manifest
        manifest_path = config.getoption("--progproc-collection-manifest")
        if manifest_path:
            _write_json(Path(manifest_path), manifest)

        expected_path = config.getoption("--progproc-expected-manifest")
        if expected_path is None:
            return
        index = config.getoption("--progproc-shard-index")
        count = config.getoption("--progproc-shard-count")
        try:
            expected = validate_manifest(
                _read_json(Path(expected_path)),
                expected_sha=self._identity("--progproc-head-sha", "GITHUB_SHA"),
                expected_run_id=self._identity("--progproc-run-id", "GITHUB_RUN_ID"),
                expected_attempt=self._identity("--progproc-run-attempt", "GITHUB_RUN_ATTEMPT"),
                expected_repository=self._identity("--progproc-repository", "GITHUB_REPOSITORY"),
                expected_workflow=self._identity("--progproc-workflow", "GITHUB_WORKFLOW"),
            )
            if (
                manifest["manifest_sha256"] != expected["manifest_sha256"]
                or manifest["nodeids"] != expected["nodeids"]
            ):
                raise ValueError("this shard collected a different test universe or configuration")
            assigned = assigned_nodeids(nodeids, index, count)
            selected_set = set(assigned)
            selected = [item for item in items if str(item.nodeid) in selected_set]
            deselected = [item for item in items if str(item.nodeid) not in selected_set]
            self.selection = {
                "schema_version": _SCHEMA_VERSION,
                "head_sha": manifest["head_sha"],
                "repository": manifest["repository"],
                "workflow": manifest["workflow"],
                "run_id": manifest["run_id"],
                "run_attempt": manifest["run_attempt"],
                "manifest_sha256": manifest["manifest_sha256"],
                "assignment_algorithm": _ASSIGNMENT_ALGORITHM,
                "shard_index": index,
                "shard_count": count,
                "collection_count": len(nodeids),
                "assigned_count": len(assigned),
                "assigned_nodeids": assigned,
                "assigned_sha256": hashlib.sha256(_canonical({"nodeids": assigned})).hexdigest(),
                "deselected_count": len(deselected),
            }
            result_path = config.getoption("--progproc-selection-result")
            if result_path:
                _write_json(Path(result_path), self.selection)
            items[:] = selected
            if deselected:
                config.hook.pytest_deselected(items=deselected)
        except (OSError, ValueError) as exc:
            raise pytest.UsageError(str(exc)) from exc

    def pytest_runtest_logreport(self, report: Any) -> None:
        entry: dict[str, Any] = {
            "when": str(report.when),
            "outcome": str(report.outcome),
            "duration": round(float(getattr(report, "duration", 0.0) or 0.0), 6),
        }
        wasxfail = getattr(report, "wasxfail", None)
        if wasxfail:
            entry["wasxfail"] = str(wasxfail)
        if report.outcome == "skipped":
            entry["skip_reason"] = _skip_reason(report)
        self.reports[str(report.nodeid)].append(entry)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        result_path = self.config.getoption("--progproc-shard-result")
        if not result_path or self.selection is None or self.manifest is None:
            return
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "head_sha": self.manifest["head_sha"],
            "repository": self.manifest["repository"],
            "workflow": self.manifest["workflow"],
            "workflow_ref": self.manifest["workflow_ref"],
            "run_id": self.manifest["run_id"],
            "run_attempt": self.manifest["run_attempt"],
            "manifest_sha256": self.manifest["manifest_sha256"],
            "assignment_algorithm": _ASSIGNMENT_ALGORITHM,
            "shard_index": self.selection["shard_index"],
            "shard_count": self.selection["shard_count"],
            "assigned_nodeids": self.selection["assigned_nodeids"],
            "pytest_exit_status": int(exitstatus),
            "reports": dict(sorted(self.reports.items())),
        }
        _write_json(Path(result_path), payload)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("progproc-sharding")
    for name in (
        "collection-manifest",
        "expected-manifest",
        "selection-result",
        "shard-result",
        "head-sha",
        "run-id",
        "run-attempt",
        "repository",
        "workflow",
        "workflow-ref",
    ):
        group.addoption(f"--progproc-{name}", default=None)
    group.addoption("--progproc-shard-index", type=int, default=None)
    group.addoption("--progproc-shard-count", type=int, default=None)


def pytest_configure(config: pytest.Config) -> None:
    index = config.getoption("--progproc-shard-index")
    count = config.getoption("--progproc-shard-count")
    expected = config.getoption("--progproc-expected-manifest")
    selection = config.getoption("--progproc-selection-result")
    result = config.getoption("--progproc-shard-result")
    if (index is None) != (count is None):
        raise pytest.UsageError("both shard index and shard count are required")
    if index is not None:
        if count < 1 or not 0 <= index < count:
            raise pytest.UsageError("shard index/count are invalid")
        if not expected or not selection or not result:
            raise pytest.UsageError(
                "a shard requires expected manifest, selection, and result paths"
            )
    elif expected or selection or result:
        raise pytest.UsageError("shard artifacts require an explicit shard index/count")
    if not config.getoption("--progproc-collection-manifest"):
        raise pytest.UsageError("--progproc-collection-manifest is required")
    config.pluginmanager.register(_ShardPlugin(config), "progproc-shard")


__all__ = ["assigned_nodeids", "shard_for", "validate_manifest"]
