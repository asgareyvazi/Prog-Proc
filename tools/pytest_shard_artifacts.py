#!/usr/bin/env python3
"""Fetch read-only GitHub Actions run/job/artifact evidence for pytest shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_API = "https://api.github.com"
_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


class FetchError(ValueError):
    """GitHub API data or artifact retrieval was incomplete or unsafe."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _assert_api_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.port is not None
        or parsed.username
        or parsed.password
    ):
        raise FetchError("refusing a non-GitHub API URL")


def _assert_artifact_storage_url(url: str) -> None:
    """Allow only GitHub Actions' signed Azure artifact-storage redirect hosts."""
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise FetchError("refusing a malformed artifact storage URL") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or port not in {None, 443}
        or parsed.username
        or parsed.password
        or re.fullmatch(r"productionresultssa[a-z0-9-]+[.]blob[.]core[.]windows[.]net", host)
        is None
    ):
        raise FetchError("refusing an untrusted artifact storage redirect")


def _json_request(url: str, token: str) -> tuple[dict[str, Any], Any]:
    _assert_api_url(url)
    request = urllib.request.Request(  # noqa: S310 - scheme and api.github.com host validated above
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Prog-Proc-pytest-shard-evidence",
        },
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=60) as response:
            raw = response.read(_MAX_JSON_BYTES + 1)
            headers = response.headers
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError("GitHub API request failed") from exc
    if len(raw) > _MAX_JSON_BYTES:
        raise FetchError("GitHub API response exceeds the bounded size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise FetchError("GitHub API returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise FetchError("GitHub API response root must be an object")
    return payload, headers


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


def _paginated_list(url: str, key: str, token: str) -> dict[str, Any]:
    items: list[Any] = []
    page = 1
    first: dict[str, Any] | None = None
    total: int | None = None
    while True:
        page_url = f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}"
        payload, _ = _json_request(page_url, token)
        if first is None:
            first = payload
            raw_total = payload.get("total_count")
            if isinstance(raw_total, bool) or not isinstance(raw_total, int) or raw_total < 0:
                raise FetchError(f"GitHub {key} response has malformed total_count")
            total = raw_total
        batch = payload.get(key)
        if not isinstance(batch, list):
            raise FetchError(f"GitHub {key} response has the wrong shape")
        items.extend(batch)
        if len(items) >= total:
            break
        if not batch or page >= 100:
            raise FetchError(f"GitHub {key} pagination ended before total_count")
        page += 1
    if len(items) != total:
        raise FetchError(f"GitHub {key} item count does not equal total_count")
    if first is None:
        raise FetchError(f"GitHub {key} response was not retrieved")
    return {**first, key: items}


def _download_zip(api_url: str, token: str, destination: Path) -> tuple[int, str]:
    _assert_api_url(api_url)
    opener = urllib.request.build_opener(_NoRedirect())
    request_url = api_url
    response: Any = None
    for redirect_count in range(3):
        headers = {"User-Agent": "Prog-Proc-pytest-shard-evidence"}
        if request_url == api_url:
            headers.update(
                {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
            )
        else:
            _assert_artifact_storage_url(request_url)
        request = urllib.request.Request(  # noqa: S310 - API/storage URL is validated per hop
            request_url, headers=headers
        )
        try:
            response = opener.open(request, timeout=60 if request_url == api_url else 120)
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                if request_url == api_url:
                    raise FetchError(f"GitHub artifact API returned HTTP {exc.code}") from exc
                raise FetchError("signed artifact storage returned an HTTP error") from exc
            location = exc.headers.get("Location")
            exc.close()
            if not location or redirect_count == 2:
                raise FetchError(
                    "artifact redirect chain is missing a target or exceeds its limit"
                ) from None
            target = urllib.parse.urljoin(request_url, location)
            _assert_artifact_storage_url(target)
            request_url = target
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            message = (
                "GitHub artifact request failed"
                if request_url == api_url
                else "signed artifact download failed"
            )
            raise FetchError(message) from exc
    if response is None:
        raise FetchError("artifact redirect chain did not produce a response")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        response.close()
        raise FetchError("artifact archive destination already exists")
    digest = hashlib.sha256()
    size = 0
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as output, response:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_ARCHIVE_BYTES:
                    raise FetchError("artifact archive exceeds the configured size limit")
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size == 0:
            raise FetchError("artifact archive download was empty")
        Path(temporary).replace(destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return size, digest.hexdigest()


def collect(
    *,
    repository: str,
    run_id: str,
    attempt: str,
    expected_sha: str,
    workflow: str,
    shard_count: int,
    output_dir: Path,
    token: str,
    additional_artifact_names: tuple[str, ...] = (),
    expected_artifact_metadata: Mapping[str, tuple[int, int, str]] | None = None,
) -> dict[str, Any]:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise FetchError("repository must be owner/name")
    if not run_id.isdecimal() or not attempt.isdecimal() or shard_count < 2:
        raise FetchError("run ID, attempt, or shard count is invalid")
    allowed_additional = {f"ubuntu2604-qt-shard-report-a{attempt}"}
    if any(name not in allowed_additional for name in additional_artifact_names):
        raise FetchError("additional artifact name is outside the bounded shard-report allowlist")
    if len(additional_artifact_names) != len(set(additional_artifact_names)):
        raise FetchError("additional artifact names contain duplicates")
    base = f"{_API}/repos/{repository}/actions/runs/{run_id}"
    run, _ = _json_request(base, token)
    jobs = _paginated_list(f"{base}/jobs", "jobs", token)
    artifacts = _paginated_list(f"{base}/artifacts", "artifacts", token)
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(output_dir / "run.json", run)
    _write_json(output_dir / "jobs.json", jobs)
    _write_json(output_dir / "artifacts.json", artifacts)
    archive_root = output_dir / "archives"
    archive_root.mkdir()
    expected_names = {f"ubuntu2604-qt-collection-a{attempt}"}
    expected_names.update(
        f"ubuntu2604-qt-shard-{i:02d}-of-{shard_count:02d}-a{attempt}" for i in range(shard_count)
    )
    expected_names.update(additional_artifact_names)
    pinned_metadata = dict(expected_artifact_metadata or {})
    if not set(pinned_metadata) <= expected_names:
        raise FetchError("pinned artifact metadata names exceed the bounded artifact inventory")
    if any(
        not isinstance(value, tuple)
        or len(value) != 3
        or type(value[0]) is not int
        or value[0] < 1
        or type(value[1]) is not int
        or value[1] < 1
        or not isinstance(value[2], str)
        or re.fullmatch(r"[0-9a-f]{64}", value[2]) is None
        for value in pinned_metadata.values()
    ):
        raise FetchError("pinned artifact metadata is malformed")
    records = artifacts.get("artifacts", [])
    downloads: list[dict[str, Any]] = []
    failures: list[str] = []
    for name in sorted(expected_names):
        matches = [row for row in records if isinstance(row, dict) and row.get("name") == name]
        if len(matches) != 1:
            failures.append(f"{name}: expected one API artifact record, found {len(matches)}")
            continue
        artifact = matches[0]
        artifact_id = artifact.get("id")
        expected_size = artifact.get("size_in_bytes")
        expected_digest = artifact.get("digest")
        if type(artifact_id) is not int or artifact_id < 1:
            failures.append(f"{name}: missing numeric API artifact ID")
            continue
        if artifact.get("expired") is not False:
            failures.append(f"{name}: expired or expiration is unknown")
            continue
        if type(expected_size) is not int or expected_size <= 0:
            failures.append(f"{name}: API artifact size is malformed")
            continue
        if (
            not isinstance(expected_digest, str)
            or re.fullmatch(r"sha256:[0-9a-fA-F]{64}", expected_digest) is None
        ):
            failures.append(f"{name}: API artifact SHA-256 digest is malformed")
            continue
        pinned = pinned_metadata.get(name)
        if pinned is not None and (
            artifact_id != pinned[0]
            or expected_size != pinned[1]
            or expected_digest.lower() != f"sha256:{pinned[2]}"
        ):
            failures.append(f"{name}: GitHub API metadata differs from pinned historical evidence")
            continue
        endpoint = f"{_API}/repos/{repository}/actions/artifacts/{artifact_id}/zip"
        try:
            size, digest = _download_zip(endpoint, token, archive_root / f"{artifact_id}.zip")
        except FetchError as exc:
            failures.append(f"{name}: {exc}")
            continue
        matches_digest = expected_digest.lower() == f"sha256:{digest}"
        matches_size = expected_size == size
        downloads.append(
            {
                "artifact_id": artifact_id,
                "artifact_name": name,
                "download_size_bytes": size,
                "download_sha256": f"sha256:{digest}",
                "api_digest": expected_digest,
                "api_size_bytes": expected_size,
                "matches_api_digest": matches_digest,
                "matches_api_size": matches_size,
            }
        )
        if not matches_digest or not matches_size:
            failures.append(f"{name}: downloaded bytes do not match API digest and size")
    report = {
        "schema_version": 1,
        "repository": repository,
        "workflow": workflow,
        "run_id": run_id,
        "run_attempt": attempt,
        "expected_sha": expected_sha,
        "downloaded": downloads,
        "failures": failures,
    }
    _write_json(output_dir / "downloads.json", report)
    if failures:
        raise FetchError("one or more required current-run artifacts could not be verified")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--additional-artifact-name", action="append", default=[])
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("INCOMPLETE: read-only GH_TOKEN was not provided", file=sys.stderr)
        return 1
    try:
        report = collect(
            repository=args.repository,
            run_id=args.run_id,
            attempt=args.attempt,
            expected_sha=args.sha,
            workflow=args.workflow,
            shard_count=args.shard_count,
            output_dir=args.output_dir,
            token=token,
            additional_artifact_names=tuple(args.additional_artifact_name),
        )
    except FetchError as exc:
        print(f"INCOMPLETE: {exc}", file=sys.stderr)
        return 1
    print(f"verified {len(report['downloaded'])} run-scoped GitHub artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
