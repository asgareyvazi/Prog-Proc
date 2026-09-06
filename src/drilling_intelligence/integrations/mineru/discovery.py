"""MinerU runtime discovery.

What we check, and why (docs/DEPENDENCIES.md records the outcome for the
environment this was developed in):

*   ``[mineru].mode`` may pin the integration (``cli``/``http``), ask for auto
    detection (``auto``) or turn it off (``disabled``);
*   CLI mode: the ``mineru`` executable must exist **and** answer a version
    query.  MinerU is a heavyweight parser (models, torch) - we never import it;
    we run it as a subprocess so its dependency tree and its Python-version
    constraint stay outside our application runtime.
*   HTTP mode: a reachable MinerU/`mineru-router` service is probed with a short
    timeout.  A configured but unreachable endpoint is a *warning*, not a crash.

A probe result is cached per process; long ingestion runs must not pay for it
per document.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from ...core.logging import get_logger
from ..base import IntegrationStatus, http_json, run_command, which

log = get_logger("integrations.mineru.discovery")

_VERSION_RE = re.compile(r"(?:v|version)?\s*(\d+\.\d+(?:\.\d+)?)")


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
        status.limitations.extend(
            [
                f"backend '{backend}': the pipeline backend runs on CPU and needs ~16 GB RAM (32 GB recommended); "
                "vlm/hybrid backends require a GPU",
                "MinerU's own Python version constraint is independent of the application because it runs as a separate process/service",
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
        binary = str(getattr(mineru, "binary", "mineru") or "mineru")
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
            record["version"] = (
                _VERSION_RE.findall(str(help_result["stdout"]))[0]
                if _VERSION_RE.findall(str(help_result["stdout"]))
                else "unknown"
            )
            record["available"] = True
            record["binary"] = path
            return record
        found = _VERSION_RE.findall(str(result["stdout"]) + str(result["stderr"]))
        record["version"] = (
            found[0]
            if found
            else str(result["stdout"]).strip().splitlines()[0][:32]
            if str(result["stdout"]).strip()
            else "unknown"
        )
        record["available"] = True
        record["binary"] = path
        return record

    def _probe_http(self, mineru: object) -> dict[str, object]:
        endpoint = str(getattr(mineru, "endpoint", "") or "").rstrip("/")
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
        for path in ("/health", "/docs", "/"):
            response = http_json("GET", endpoint + path, timeout=4.0)
            if response.get("ok"):
                record["available"] = True
                record["endpoint"] = endpoint + path
                payload = response.get("json")
                if isinstance(payload, dict):
                    record["version"] = str(
                        payload.get("version") or payload.get("version_name") or ""
                    )
                return record
        record["reason"] = f"no HTTP response from {endpoint} (checked /health, /docs, /)"
        return record


def output_root(data_dir: Path | None = None) -> Path:
    """Directory where MinerU run artefacts are written for inspection."""
    root = Path(data_dir) if data_dir else Path.cwd() / "var"
    root.mkdir(parents=True, exist_ok=True)
    return root


__all__ = ["MinerUProber", "output_root"]
