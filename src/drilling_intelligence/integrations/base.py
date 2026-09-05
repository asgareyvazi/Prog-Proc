"""Shared plumbing for external components (master spec section 54).

Two rules drive this module:

1.  **Process boundaries.**  External engines (MinerU, Ollama) are reached over a
    CLI/HTTP contract, never imported into the domain layer.  That keeps their
    licenses, dependency trees and Python-version constraints off our runtime and
    keeps a crashing parser from crashing the desktop application.
2.  **Honest availability.**  An integration reports exactly what it found -
    version, binary path, endpoint, why it is unusable - and the platform then
    either uses it or records the fallback.  Never "assumed present".
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class IntegrationStatus:
    """What we know about an external component in *this* environment."""

    component: str
    available: bool = False
    mode: str = ""  # cli | http | library | disabled
    version: str = ""
    location: str = ""
    reason: str = ""
    #: Commands/requests attempted, so a failure can be diagnosed from the report.
    checks: list[dict[str, Any]] = field(default_factory=list)
    #: Anything a human must know before trusting the integration (limits, RAM).
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        if self.available:
            return f"{self.component} available ({self.mode}){' v' + self.version if self.version else ''} @ {self.location}"
        return f"{self.component} unavailable: {self.reason}"


def which(binary: str) -> str:
    """Absolute path of an executable, or empty string."""
    if not binary:
        return ""
    found = shutil.which(binary)
    if found:
        return str(Path(found).resolve())
    candidate = Path(binary).expanduser()
    return str(candidate) if candidate.exists() else ""


def run_command(
    argv: list[str],
    *,
    timeout: float = 60.0,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
) -> dict[str, Any]:
    """Run a subprocess with a hard timeout and captured output.

    Returns a dict (never raises on non-zero exit) so callers can decide whether a
    failure is fatal - which is what a *document converter* is allowed to be.
    """
    import os

    started: list[float] = []
    try:
        import time

        started = [time.perf_counter()]
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env={**os.environ, **(env or {})},
            input=stdin,
            check=False,
        )
        duration = (time.perf_counter() - started[0]) * 1000 if started else 0.0
        return {
            "argv": argv,
            "returncode": completed.returncode,
            "stdout": completed.stdout.decode("utf-8", errors="replace"),
            "stderr": completed.stderr.decode("utf-8", errors="replace"),
            "duration_ms": duration,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "returncode": -1,
            "stdout": (exc.stdout or b"").decode("utf-8", errors="replace") if exc.stdout else "",
            "stderr": f"timed out after {timeout}s",
            "duration_ms": timeout * 1000,
            "timeout": True,
        }
    except FileNotFoundError as exc:
        return {"argv": argv, "returncode": -1, "stdout": "", "stderr": f"executable not found: {exc}", "duration_ms": 0.0, "timeout": False}
    except Exception as exc:  # noqa: BLE001 - subprocess boundary
        return {"argv": argv, "returncode": -1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}", "duration_ms": 0.0, "timeout": False}


def parse_json_loose(text: str) -> Any:
    """Parse JSON from noisy tool output (logs written to stdout by some CLIs)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def http_json(method: str, url: str, *, json_body: Any = None, params: dict[str, Any] | None = None, timeout: float = 30.0, headers: dict[str, str] | None = None, files: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Small HTTP helper used by the Ollama/MinerU clients (httpx under the hood).

    Kept in one place so timeout/retry/redaction behaviour is identical for every
    external service the platform talks to.
    """
    import httpx

    try:
        response = httpx.request(
            method,
            url,
            json=json_body,
            params=params,
            timeout=timeout,
            headers=headers or {},
            files=files,
            data=data,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 0, "error": f"{type(exc).__name__}: {exc}", "url": url}
    payload: Any = None
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = None
    return {
        "ok": response.is_success,
        "status_code": response.status_code,
        "json": payload,
        "text": response.text if payload is None else "",
        "url": url,
        "error": "" if response.is_success else f"HTTP {response.status_code}",
    }


__all__ = ["IntegrationStatus", "http_json", "parse_json_loose", "run_command", "which"]
