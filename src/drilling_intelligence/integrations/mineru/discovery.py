"""MinerU runtime discovery.

What we check, and why (the current runtime boundary is recorded in
``docs/PRODUCT_CAPABILITY.md`` and the active mission report):

*   ``[mineru].mode`` may pin the integration (``cli``/``http``), ask for auto
    detection (``auto``) or turn it off (``disabled``);
*   CLI mode: the supported ``mineru-kit`` entry point (or a compatible legacy
    CLI) must exist **and** answer a version query. MinerU stays external so its
    models and dependency tree do not enter the application runtime.
*   HTTP mode: OpenAPI must declare the configured legacy parse route with a
    JSON response. A reachable docs page or MinerU 4 attachment endpoint is not
    sufficient. A configured but incompatible endpoint fails closed.

A probe result is cached per process; long ingestion runs must not pay for it
per document.
"""

from __future__ import annotations

import re
import threading
from urllib.parse import urlsplit

from ...core.logging import get_logger
from ..base import IntegrationStatus, http_json, run_command, which
from .cli import is_mineru_kit, resolve_cli_binary

log = get_logger("integrations.mineru.discovery")

_VERSION_RE = re.compile(r"(?:v|version)?\s*(\d+\.\d+(?:\.\d+)?)")


def http_endpoint_problem(endpoint: str) -> str:
    """Return a safe reason when an HTTP endpoint could expose credentials or URL secrets."""
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        # Accessing .port also validates malformed port syntax.
        _ = parsed.port
    except ValueError:
        return "HTTP endpoint must be a valid http(s) URL without credentials or URL parameters"
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(
            ord(character) < 32 or character.isspace() or character == "\\"
            for character in endpoint
        )
    ):
        return "HTTP endpoint must be a valid http(s) URL without credentials or URL parameters"
    return ""


def _safe_endpoint_label(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme.lower()}://{hostname}{port}"
    except ValueError:
        return "<configured-http-endpoint>"


def _resolve_local_schema(document: dict[str, object], schema: object) -> dict[str, object] | None:
    """Resolve only bounded local OpenAPI schema refs; never fetch external refs."""
    current = schema
    seen: set[str] = set()
    while isinstance(current, dict) and "$ref" in current:
        reference = current.get("$ref")
        if (
            not isinstance(reference, str)
            or not reference.startswith("#/components/schemas/")
            or reference in seen
        ):
            return None
        seen.add(reference)
        key = reference.removeprefix("#/components/schemas/").replace("~1", "/").replace("~0", "~")
        components = document.get("components")
        schemas = components.get("schemas") if isinstance(components, dict) else None
        current = schemas.get(key) if isinstance(schemas, dict) else None
    return current if isinstance(current, dict) else None


def _is_upload_schema(document: dict[str, object], schema: object, *, depth: int = 0) -> bool:
    if depth >= 16:
        return False
    resolved = _resolve_local_schema(document, schema)
    if not resolved:
        return False
    if resolved.get("type") == "string":
        return resolved.get("format") == "binary"
    if resolved.get("type") == "array":
        return _is_upload_schema(document, resolved.get("items"), depth=depth + 1)
    return False


def legacy_http_parse_contract(endpoint: str, parse_path: str) -> tuple[bool, str, str]:
    """Require the multipart request and JSON result envelope this client consumes.

    A reachable docs page is not enough: MinerU 4's standalone file-parse API
    returns attachments, while this integration accepts a legacy JSON ``results``
    envelope from a multipart ``files`` request. The actual response is validated
    again after every parse; OpenAPI is only a compatibility declaration.
    """
    if not endpoint:
        return False, "HTTP endpoint is not configured", ""
    problem = http_endpoint_problem(endpoint)
    if problem:
        return False, problem, ""
    response = http_json(
        "GET",
        endpoint.rstrip("/") + "/openapi.json",
        timeout=4.0,
        follow_redirects=False,
    )
    status_code = response.get("status_code")
    if not response.get("ok") or not isinstance(status_code, int) or not 200 <= status_code < 300:
        return False, "OpenAPI description is unavailable", ""
    payload = response.get("json")
    paths = payload.get("paths") if isinstance(payload, dict) else None
    operation = paths.get(parse_path) if isinstance(paths, dict) else None
    post = operation.get("post") if isinstance(operation, dict) else None
    if not isinstance(post, dict):
        return False, "OpenAPI does not expose the configured POST parse route", ""

    request_body = post.get("requestBody")
    request_content = request_body.get("content") if isinstance(request_body, dict) else None
    multipart = (
        next(
            (
                value
                for media_type, value in request_content.items()
                if str(media_type).split(";", 1)[0].strip().lower() == "multipart/form-data"
                and isinstance(value, dict)
            ),
            None,
        )
        if isinstance(request_content, dict)
        else None
    )
    schema = multipart.get("schema") if isinstance(multipart, dict) else None
    resolved_schema = _resolve_local_schema(payload, schema) if isinstance(payload, dict) else None
    properties = resolved_schema.get("properties") if resolved_schema else None
    if not isinstance(properties, dict) or "files" not in properties:
        return False, "parse route does not declare the multipart 'files' field", ""
    if not _is_upload_schema(payload, properties.get("files")):
        return False, "multipart 'files' field is not declared as a binary upload", ""

    responses = post.get("responses")
    if not isinstance(responses, dict):
        return False, "OpenAPI does not declare parse responses", ""
    json_success = any(
        isinstance(response_spec, dict)
        and str(code).startswith("2")
        and isinstance(response_spec.get("content"), dict)
        and any(
            str(media_type).split(";", 1)[0].strip().lower() == "application/json"
            or str(media_type).split(";", 1)[0].strip().lower().endswith("+json")
            for media_type in response_spec["content"]
        )
        for code, response_spec in responses.items()
    )
    if not json_success:
        return False, "parse route does not declare a successful JSON response", ""
    info = payload.get("info") if isinstance(payload, dict) else None
    raw_version = str(info.get("version", "")) if isinstance(info, dict) else ""
    match = _VERSION_RE.search(raw_version)
    version = match.group(1) if match else "unknown"
    return True, "", version


class MinerUProber:
    """Cached availability probe for the configured MinerU runtime."""

    def __init__(self, settings: object) -> None:
        self.settings = settings  # Settings (typed loosely to avoid an import cycle)
        self._lock = threading.Lock()
        self._cached: IntegrationStatus | None = None

    # -- public -------------------------------------------------------------
    def status(self, *, refresh: bool = False) -> IntegrationStatus:
        with self._lock:
            if self._cached is not None and not refresh:
                return self._cached
            self._cached = self._probe()
            log.info(
                self._cached.summary(),
                extra={"event": "mineru.probe", "available": self._cached.available},
            )
            return self._cached

    def available(self) -> tuple[bool, str]:
        status = self.status()
        return (
            status.available,
            status.reason
            if not status.available
            else f"MinerU {status.version or '?'} via {status.mode}",
        )

    def reset(self) -> None:
        with self._lock:
            self._cached = None

    # -- internals ----------------------------------------------------------
    def _probe(self) -> IntegrationStatus:
        mineru = getattr(self.settings, "mineru", None)
        mode = str(getattr(mineru, "mode", "auto") or "auto").lower()
        status = IntegrationStatus(component="MinerU", mode=mode)
        if mode in ("disabled", "off", "none"):
            status.reason = "disabled in configuration ([mineru].mode = disabled)"
            status.limitations.append("router will use the built-in PDF text extractor instead")
            return status

        backend = str(getattr(mineru, "backend", "pipeline") or "pipeline")
        tier = str(getattr(mineru, "tier", "basic") or "basic")
        model_source = str(getattr(mineru, "model_source", "local") or "local")
        status.limitations.extend(
            [
                f"MinerU 4 mineru-kit uses configured tier '{tier}'; legacy CLI backend '{backend}' applies only to older CLI builds",
                f"model source is configured as '{model_source}'; local prevents implicit downloads, while non-local sources may contact their providers when parsing",
                "model-file readiness is not inspected by this runtime probe; OCR quality is not qualified by a successful CLI check",
                "MinerU's Python and dependency requirements remain outside the application because it runs as a subprocess/service",
                "MinerU output is layout-based; cell-level Excel provenance is *not* available from it, which is why XLSX stays on the native openpyxl extractor",
            ]
        )

        if mode in ("auto", "cli"):
            cli_status = self._probe_cli(mineru, backend)
            status.checks.append(cli_status)
            if cli_status["available"]:
                status.available = True
                status.mode = "cli"
                status.version = str(cli_status["version"])
                status.location = str(cli_status["binary"])
                status.reason = ""
                return status
            if mode == "cli":
                status.reason = str(cli_status["reason"])
                return status

        if mode in ("auto", "http"):
            http_status = self._probe_http(mineru)
            status.checks.append(http_status)
            if http_status["available"]:
                status.available = True
                status.mode = "http"
                status.version = str(http_status["version"])
                status.location = str(http_status["endpoint"])
                status.reason = ""
                return status
            if mode == "http":
                status.reason = str(http_status["reason"])
                return status

        if not status.reason:
            cli_reason = next(
                (c.get("reason") for c in status.checks if c.get("mode") == "cli"), None
            )
            http_reason = next(
                (c.get("reason") for c in status.checks if c.get("mode") == "http"), None
            )
            status.reason = f"no usable runtime found (cli: {cli_reason}; http: {http_reason})"
        return status

    def _probe_cli(self, mineru: object, backend: str) -> dict[str, object]:
        configured = str(getattr(mineru, "binary", "mineru") or "mineru")
        binary = resolve_cli_binary(configured, find_executable=which)
        path = which(binary)
        record: dict[str, object] = {
            "mode": "cli",
            "available": False,
            "version": "",
            "binary": path,
            "reason": "",
        }
        if not path:
            record["reason"] = f"executable {binary!r} not found on PATH"
            return record
        result = run_command([path, "--version"], timeout=60.0)
        if int(result["returncode"]) != 0:
            # Older builds have no --version; fall back to --help presence.
            help_result = run_command([path, "--help"], timeout=60.0)
            if int(help_result["returncode"]) != 0:
                record["reason"] = (
                    "configured CLI --version and --help failed; process output omitted"
                )
                return record
            found = _VERSION_RE.findall(str(help_result["stdout"]))
            version = found[0] if found else "unknown"
        else:
            found = _VERSION_RE.findall(str(result["stdout"]) + str(result["stderr"]))
            version = found[0] if found else "unknown"

        record["version"] = version
        # MinerU 4's `mineru` command is its document-library interface, not the
        # stateless parse command.  If `mineru-kit` was not found, fail closed;
        # invoking it with legacy -p/-o flags would be a false availability pass.
        if not is_mineru_kit(path) and version.startswith("4."):
            record["reason"] = (
                "MinerU 4 was found without the compatible mineru-kit executable; "
                "install the complete MinerU 4 CLI or configure a supported legacy CLI"
            )
            return record
        record["available"] = True
        record["binary"] = path
        return record

    def _probe_http(self, mineru: object) -> dict[str, object]:
        endpoint = str(getattr(mineru, "endpoint", "") or "").rstrip("/")
        parse_path = str(getattr(mineru, "http_parse_path", "/file_parse") or "/file_parse")
        record: dict[str, object] = {
            "mode": "http",
            "available": False,
            "version": "",
            "endpoint": _safe_endpoint_label(endpoint) if endpoint else "",
            "reason": "",
        }
        if not endpoint:
            record["reason"] = "[mineru].endpoint is empty"
            return record
        problem = http_endpoint_problem(endpoint)
        if problem:
            record["reason"] = problem
            return record
        compatible, reason, version = legacy_http_parse_contract(endpoint, parse_path)
        record["contract"] = {
            "path": parse_path,
            "compatible": compatible,
        }
        if not compatible:
            record["reason"] = (
                f"HTTP service does not implement the configured JSON parse contract: {reason}"
            )
            return record
        health = http_json("GET", endpoint + "/health", timeout=4.0, follow_redirects=False)
        payload = health.get("json") if health.get("ok") else None
        if isinstance(payload, dict):
            raw_version = str(payload.get("version") or payload.get("version_name") or version)
            match = _VERSION_RE.search(raw_version)
            if match:
                version = match.group(1)
        record["available"] = True
        record["endpoint"] = _safe_endpoint_label(endpoint)
        record["version"] = version
        return record


__all__ = ["MinerUProber", "legacy_http_parse_contract"]
