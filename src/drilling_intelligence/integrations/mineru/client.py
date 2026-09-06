"""MinerU client: run the parser as an external process or service.

Two transports, chosen by configuration and by what discovery found:

``cli``
    ``mineru -p <file> -o <outdir> -b <backend> -l <lang>`` in a temporary
    output directory.  Works with a per-machine MinerU install (its own venv),
    which is the recommended deployment for offline desktop use.

``http``
    ``POST {endpoint}/file_parse`` with the file and the same options, reading
    ``md_content`` / ``middle_json`` / ``content_list`` from the response.  This
    is what a shared GPU host or ``mineru-router`` deployment exposes.

Why a process boundary rather than ``import mineru``:
MinerU pins its own dependency stack (models, torch, its own Python range:
PyPI metadata for 3.4.x requires ``<3.14``) and its licence is Apache-2.0 with
additional conditions.  Keeping it outside the application keeps our runtime,
our licence and our upgrade schedule independent, and keeps a segfault in an OCR
stack from taking down a desktop session with unsaved review state.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core.errors import ExtractionError, IntegrationUnavailableError
from ...core.hashing import short_hash
from ...core.logging import get_logger
from ..base import http_json, run_command
from .normalize import MinerURawOutput

log = get_logger("integrations.mineru.client")


@dataclass
class MinerURun:
    """One parsed document plus everything needed to audit the run."""

    ok: bool
    mode: str
    artefacts: MinerURawOutput | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    command: list[str] = field(default_factory=list)
    output_dir: Path | None = None
    error: str = ""
    #: Keep artefacts for debugging when this is set (config: keep_artifacts).
    keep: bool = False

    def cleanup(self) -> None:
        if self.keep or self.output_dir is None:
            return
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "duration_ms": round(self.duration_ms, 1),
            "command": " ".join(self.command) if self.command else "",
            "artefact": self.artefacts.best if self.artefacts else "",
            "error": self.error,
        }


class MinerUClient:
    """Thin, explicit client for MinerU.  No silent retries, no magic."""

    def __init__(self, settings: Any) -> None:
        mineru = getattr(settings, "mineru", None)
        self.mode = str(getattr(mineru, "mode", "auto") or "auto").lower()
        self.binary = str(getattr(mineru, "binary", "mineru") or "mineru")
        self.backend = str(getattr(mineru, "backend", "pipeline") or "pipeline")
        self.endpoint = str(getattr(mineru, "endpoint", "") or "").rstrip("/")
        self.timeout = float(getattr(mineru, "timeout_seconds", 900.0) or 900.0)
        self.extra_env: dict[str, str] = dict(getattr(mineru, "env", {}) or {})
        self.parse_path = str(getattr(mineru, "http_parse_path", "/file_parse") or "/file_parse")
        self.language = str(getattr(mineru, "language", "en") or "en")
        self.keep_artifacts = bool(getattr(mineru, "keep_artifacts", False))

    # -- public -------------------------------------------------------------
    def parse(self, source: Path, *, filename_hint: str = "") -> MinerURun:
        if self.mode in ("disabled", "off", "none"):
            raise IntegrationUnavailableError("MinerU", "disabled in configuration")
        mode = self.mode
        if mode == "auto":
            # Prefer HTTP when an endpoint answers, else CLI.  The prober has the
            # authoritative view; here we only pick the transport.
            mode = (
                "http"
                if (
                    self.endpoint
                    and http_json("GET", self.endpoint + "/docs", timeout=3.0).get("ok")
                )
                else "cli"
            )
        if mode == "http":
            if not self.endpoint:
                raise IntegrationUnavailableError(
                    "MinerU", "[mineru].endpoint is required for http mode"
                )
            return self._parse_http(Path(source))
        return self._parse_cli(Path(source), filename_hint)

    # -- transports ---------------------------------------------------------
    def _parse_cli(self, source: Path, filename_hint: str = "") -> MinerURun:
        if not shutil.which(self.binary) and not Path(self.binary).expanduser().exists():
            raise IntegrationUnavailableError(
                "MinerU", f"executable {self.binary!r} not found on PATH"
            )
        workdir = Path(tempfile.mkdtemp(prefix=f"mineru-{short_hash(uuid.uuid4().hex, 8)}-"))
        outdir = workdir / "out"
        outdir.mkdir(parents=True, exist_ok=True)
        command = [
            self.binary or "mineru",
            "-p",
            str(source),
            "-o",
            str(outdir),
            "-b",
            self.backend,
            "-l",
            self.language,
        ]
        run = run_command(command, timeout=self.timeout, cwd=workdir, env=self.extra_env)
        duration = float(run.get("duration_ms") or 0.0)
        code = int(run.get("returncode", -1) or -1)
        stdout = str(run.get("stdout") or "")
        stderr = str(run.get("stderr") or "")
        artefacts: MinerURawOutput | None = None
        error = ""
        ok = False
        if code == 0 or any(outdir.rglob("*.json")) or any(outdir.rglob("*.md")):
            try:
                # One discovery implementation, shared with the adapter path.
                from .normalize import load_mineru_outputs

                artefacts = load_mineru_outputs(outdir, source.stem or filename_hint)
                ok = True
            except ExtractionError as exc:
                error = str(exc)
        if not ok and not error:
            error = (
                f"mineru exited with code {code}: {stderr.strip()[:400] or stdout.strip()[:400]}"
            )
        return MinerURun(
            ok=ok,
            mode="cli",
            artefacts=artefacts,
            stdout=stdout[-4000:],
            stderr=stderr[-4000:],
            duration_ms=duration,
            command=command,
            output_dir=artefacts.directory
            if (artefacts and self.keep_artifacts)
            else (outdir if self.keep_artifacts else None),
            error=error,
            keep=self.keep_artifacts,
        )

    def _parse_http(self, source: Path) -> MinerURun:
        url = self.endpoint + self.parse_path
        data = {
            "backend": self.backend,
            "lang_list": self.language,
            "return_md": "true",
            "return_middle_json": "true",
            "return_content_list": "true",
        }
        with source.open("rb") as handle:
            response = http_json(
                "POST",
                url,
                files={"files": (source.name, handle, "application/pdf")},
                data=data,
                timeout=self.timeout,
            )
        payload = response.get("json") or {}
        if not response.get("ok"):
            raise IntegrationUnavailableError(
                "MinerU", f"HTTP {response.get('status_code')}: {str(response.get('error'))[:200]}"
            )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, dict) or not results:
            raise IntegrationUnavailableError(
                "MinerU", f"unexpected /file_parse response: {str(payload)[:200]}"
            )
        entry = next(iter(results.values()))
        if not isinstance(entry, dict):
            raise IntegrationUnavailableError("MinerU", "malformed result entry")
        middle = _maybe_json(entry.get("middle_json"))
        content_list = _maybe_json(entry.get("content_list") or entry.get("content_list_json"))
        markdown = str(entry.get("md_content") or entry.get("markdown") or "")
        artefacts = MinerURawOutput(
            middle=middle if isinstance(middle, dict) else None,
            content_list=content_list,
            markdown=markdown,
        )
        return MinerURun(
            ok=True,
            mode="http",
            artefacts=artefacts,
            duration_ms=float(response.get("duration_ms") or 0.0),
            error="",
        )


def _maybe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        from ..base import parse_json_loose

        return parse_json_loose(value)
    return None


__all__ = ["MinerUClient", "MinerURun"]
