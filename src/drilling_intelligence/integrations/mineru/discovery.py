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
from pathlib import Path

from ...core.logging import get_logger
from ..base import IntegrationStatus, http_json, run_command, which
from .cli import is_mineru_kit, resolve_cli_binary

log = get_logger("integrations.mineru.discovery")

_VERSION_RE = re.compile(r"(?:v|version)?\s*(\d+\.\d+(?:\.\d+)?)")


def legacy_http_parse_contract(endpoint: str, parse_path: str) -> tuple[bool, str, str]:
    """Require the JSON multipart endpoint this client actually consumes.

    A reachable docs page is not enough: MinerU 4's standalone file-parse API
    returns attachments from ``/v1/file_parse``, while this integration's HTTP
    adapter consumes a JSON ``results`` envelope from a legacy route.
    """
    response = http_json("GET", endpoint.rstrip("/") + "/openapi.json", timeout=4.0)
    payload = response.get("json") if response.get("ok") else None
    paths = payload.get("paths") if isinstance(payload, dict) else None
    operation = paths.get(parse_path) if isinstance(paths, dict) else None
    post = operation.get("post") if isinstance(operation, dict) else None
    responses = post.get("responses") if isinstance(post, dict) else None
    if not isinstance(responses, dict):
        return False, "OpenAPI does not expose the configured parse route", ""
    json_response = any(
        isinstance(value, dict)
        and isinstance(value.get("content"), dict)
        and any("json" in str(media_type).lower() for media_type in value["content"])
        for value in responses.values()
    )
    if not json_response:
        return False, "configured parse route does not declare a JSON response", ""
    info = payload.get("info")
    version = str(info.get("version", "")) if isinstance(info, dict) else ""
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
        status.limitations.extend(
            [
                f"MinerU 4 mineru-kit uses configured tier '{tier}'; legacy CLI backend '{backend}' applies only to older CLI builds",
                "OCR/model availability and resource use are controlled by the separately installed MinerU runtime; an OCR parse may need external model downloads",
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
                    f"{binary} --version failed: {str(result['stderr'])[:160] or str(help_result['stderr'])[:160]}"
                )
                return record
            found = _VERSION_RE.findall(str(help_result["stdout"]))
            version = found[0] if found else "unknown"
        else:
            found = _VERSION_RE.findall(str(result["stdout"]) + str(result["stderr"]))
            version = (
                found[0]
                if found
                else str(result["stdout"]).strip().splitlines()[0][:32]
                if str(result["stdout"]).strip()
                else "unknown"
            )

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
            "endpoint": endpoint,
            "reason": "",
        }
        if not endpoint:
            record["reason"] = "[mineru].endpoint is empty"
            return record
        compatible, reason, version = legacy_http_parse_contract(endpoint, parse_path)
        record["contract"] = {
            "path": parse_path,
            "compatible": compatible,
        }
        if not compatible:
            record["reason"] = (
                f"HTTP service at {endpoint} does not implement the configured JSON parse contract: {reason}"
            )
            return record
        health = http_json("GET", endpoint + "/health", timeout=4.0)
        payload = health.get("json") if health.get("ok") else None
        if isinstance(payload, dict):
            version = str(payload.get("version") or payload.get("version_name") or version)
        record["available"] = True
        record["endpoint"] = endpoint + parse_path
        record["version"] = version
        return record


def output_root(data_dir: Path | None = None) -> Path:
    """Directory where MinerU run artefacts are written for inspection."""
    root = Path(data_dir) if data_dir else Path.cwd() / "var"
    root.mkdir(parents=True, exist_ok=True)
    return root


__all__ = ["MinerUProber", "output_root"]
