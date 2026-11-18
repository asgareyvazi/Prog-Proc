"""Pure, deterministic contract for pytest shard manifests and assignments.

This module intentionally has no dependency on pytest or the application runtime.
The pytest plugin and the evidence aggregator share these functions so that
reporting can run in a minimal installation.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from collections import Counter
from typing import Any

SCHEMA_VERSION = 2
ASSIGNMENT_ALGORITHM = "sha256-nodeid-modulo-v1"


def canonical(payload: dict[str, Any]) -> bytes:
    """Serialize a contract object using the canonical UTF-8 JSON encoding."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def manifest_payload(
    nodeids: list[str],
    *,
    head_sha: str,
    run_id: str,
    run_attempt: str,
    pytest_version: str,
    pytest_config_sha256: str,
    repository: str = "LOCAL",
    workflow: str = "LOCAL",
    workflow_ref: str = "LOCAL",
) -> dict[str, Any]:
    """Build a manifest using values supplied by the pytest-aware caller."""
    if any(not isinstance(nodeid, str) or not nodeid for nodeid in nodeids):
        raise ValueError("collection contains an empty or non-string node id")
    duplicates = sorted(nodeid for nodeid, count in Counter(nodeids).items() if count > 1)
    if duplicates:
        raise ValueError(f"collection contains duplicate node IDs: {duplicates[:5]}")
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "workflow": workflow,
        "workflow_ref": workflow_ref,
        "head_sha": head_sha,
        "run_id": str(run_id),
        "run_attempt": str(run_attempt),
        "platform": sys.platform,
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "pytest_version": pytest_version,
        "pytest_config_sha256": pytest_config_sha256,
        "assignment_algorithm": ASSIGNMENT_ALGORITHM,
        "node_count": len(nodeids),
        "nodeids_sha256": hashlib.sha256(canonical({"nodeids": nodeids})).hexdigest(),
        "nodeids": nodeids,
    }
    body["manifest_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    return body


def validate_manifest(
    manifest: object,
    *,
    expected_sha: str | None = None,
    expected_run_id: str | None = None,
    expected_attempt: str | None = None,
    expected_repository: str | None = None,
    expected_workflow: str | None = None,
) -> dict[str, Any]:
    """Validate manifest schema, digests, unique node IDs, and optional identity."""
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        raise ValueError("collection manifest has an unsupported schema")
    body = dict(manifest)
    claimed = body.pop("manifest_sha256", None)
    if not isinstance(claimed, str) or hashlib.sha256(canonical(body)).hexdigest() != claimed:
        raise ValueError("collection manifest digest mismatch")
    strings = (
        "repository",
        "workflow",
        "workflow_ref",
        "head_sha",
        "run_id",
        "run_attempt",
        "platform",
        "platform_system",
        "python_version",
        "implementation",
        "pytest_version",
        "pytest_config_sha256",
        "assignment_algorithm",
        "nodeids_sha256",
    )
    if any(not isinstance(body.get(key), str) or not body[key] for key in strings):
        raise ValueError("collection manifest identity fields are malformed")
    if body["assignment_algorithm"] != ASSIGNMENT_ALGORITHM:
        raise ValueError("collection manifest assignment algorithm is unsupported")
    nodeids = body.get("nodeids")
    if not isinstance(nodeids, list) or any(
        not isinstance(item, str) or not item for item in nodeids
    ):
        raise ValueError("collection manifest node IDs are malformed")
    if len(nodeids) != len(set(nodeids)):
        raise ValueError("collection manifest contains duplicate node IDs")
    count = body.get("node_count")
    if type(count) is not int or count != len(nodeids):
        raise ValueError("collection manifest node count mismatch")
    if hashlib.sha256(canonical({"nodeids": nodeids})).hexdigest() != body["nodeids_sha256"]:
        raise ValueError("collection node-ID digest mismatch")
    for field, expected, label in (
        ("head_sha", expected_sha, "SHA"),
        ("run_id", expected_run_id, "run ID"),
        ("run_attempt", expected_attempt, "attempt"),
        ("repository", expected_repository, "repository"),
        ("workflow", expected_workflow, "workflow"),
    ):
        if expected is not None and str(body.get(field)) != str(expected):
            raise ValueError(f"collection manifest {label} does not match this run")
    return dict(manifest)


def shard_for(nodeid: str, shard_count: int) -> int:
    """Return deterministic modulo shard assignment for one node ID."""
    if not isinstance(nodeid, str) or not nodeid:
        raise ValueError("node ID must be a non-empty string")
    if isinstance(shard_count, bool) or not isinstance(shard_count, int) or shard_count < 1:
        raise ValueError("shard count must be a positive integer")
    try:
        digest = hashlib.sha256(nodeid.encode("utf-8")).digest()
    except UnicodeEncodeError as exc:
        raise ValueError("node ID cannot be encoded as UTF-8") from exc
    return int.from_bytes(digest, "big") % shard_count


def assigned_nodeids(nodeids: list[str], shard_index: int, shard_count: int) -> list[str]:
    """Keep collection order while selecting IDs assigned to a valid shard."""
    if isinstance(shard_index, bool) or not isinstance(shard_index, int):
        raise TypeError("shard index must be an integer")
    if isinstance(shard_count, bool) or not isinstance(shard_count, int) or shard_count < 1:
        raise ValueError("shard count must be a positive integer")
    if not 0 <= shard_index < shard_count:
        raise ValueError("shard index must be within the shard count")
    return [nodeid for nodeid in nodeids if shard_for(nodeid, shard_count) == shard_index]
