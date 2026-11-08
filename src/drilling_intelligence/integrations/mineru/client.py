"""MinerU client: run the parser as an external process or legacy JSON service.

MinerU 4 is invoked through ``mineru-kit parse`` and writes the versioned
``docvortex.middle`` JSON contract. Older separately managed CLI builds retain
their ``-p/-o/-b/-l`` invocation. HTTP mode deliberately accepts only the
legacy multipart + JSON parse endpoint verified through OpenAPI; current
attachment/ZIP APIs are rejected rather than reinterpreted as JSON.

MinerU stays in a separate environment/process so its heavy, version-specific
runtime and license terms do not enter the application installation, and an OCR
process failure cannot take down a desktop session with unsaved review state.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core.errors import ExtractionError, IntegrationUnavailableError
from ...core.hashing import short_hash
from ...core.logging import get_logger
from ...core.redact import safe_detail
from ..base import http_json, run_command
from .cli import is_mineru_kit, resolve_cli_binary
from .discovery import MinerUProber, http_endpoint_problem, legacy_http_parse_contract
from .normalize import MinerURawOutput

log = get_logger("integrations.mineru.client")
_OWNER_MARKER = ".drillintel-mineru-owner"


def _redact_paths(value: str, source: Path, scratch: Path) -> str:
    text = str(value or "")
    for path, replacement in ((source, "<document>"), (scratch, "<temporary-output>")):
        for candidate in {str(path), path.as_posix()}:
            if candidate:
                text = text.replace(candidate, replacement)
    return text


def _safe_command(command: list[str], source: Path, scratch: Path) -> list[str]:
    safe: list[str] = []
    source_paths = {str(source), source.as_posix()}
    scratch_paths = {str(scratch), scratch.as_posix()}
    for index, argument in enumerate(command):
        if index == 0:
            safe.append(Path(argument).name)
        elif argument in source_paths:
            safe.append("<document>")
        elif any(path and path in argument for path in scratch_paths):
            safe.append("<temporary-output>")
        else:
            safe.append(argument)
    return safe


@dataclass
class MinerURun:
    """One parsed document plus everything needed to audit the run."""

    ok: bool
    mode: str
    artefacts: MinerURawOutput | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    returncode: int = 0
    command: list[str] = field(default_factory=list)
    output_dir: Path | None = None
    error: str = ""
    timed_out: bool = False
    cleanup_error: str = ""
    ownership_marker_value: str = ""

    @property
    def partial(self) -> bool:
        return self.ok and self.returncode != 0

    def cleanup(self) -> bool:
        """Remove only the unique temporary run directory owned by this result.

        Return False (and retain an auditable error) if the directory no longer
        has its expected temp-root/prefix shape or the OS refuses removal. Never
        follow a replaced symlink and never recursively delete an arbitrary path.
        """
        path = self.output_dir
        if path is None:
            return True
        if not path.exists() and not path.is_symlink():
            self.output_dir = None
            self.ownership_marker_value = ""
            return True
        try:
            candidate = path.absolute()
            temp_root = Path(tempfile.gettempdir()).absolute()
            if candidate.parent != temp_root or not candidate.name.startswith("mineru-"):
                raise OSError("MinerU scratch path failed ownership checks")
            if path.is_symlink() or not path.is_dir():
                raise OSError("MinerU scratch path is not a real directory")
            marker = path / _OWNER_MARKER
            if marker.is_symlink() or not marker.is_file():
                raise OSError("MinerU scratch ownership marker is missing or unsafe")
            if (
                not self.ownership_marker_value
                or marker.read_text(encoding="ascii") != self.ownership_marker_value
            ):
                raise OSError("MinerU scratch ownership marker does not match this run")
            shutil.rmtree(candidate)
            self.output_dir = None
            self.ownership_marker_value = ""
            return True
        except (OSError, UnicodeError) as exc:
            detail = str(exc)
            for candidate in {str(path), path.as_posix()}:
                if candidate:
                    detail = detail.replace(candidate, "<temporary-output>")
            self.cleanup_error = safe_detail(f"{type(exc).__name__}: {detail}")
            log.warning(
                "MinerU scratch directory cleanup failed",
                extra={"event": "mineru.cleanup_failed", "error": self.cleanup_error},
            )
            return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "status": "partial" if self.partial else ("ok" if self.ok else "failed"),
            "duration_ms": round(self.duration_ms, 1),
            "command": " ".join(self.command) if self.command else "",
            "artefact": self.artefacts.best if self.artefacts else "",
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "error": self.error,
            "cleanup_error": self.cleanup_error,
        }


class MinerUClient:
    """Thin, explicit client. Transport selection is made once and then pinned."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        mineru = getattr(settings, "mineru", None)
        self.mode = str(getattr(mineru, "mode", "auto") or "auto").lower()
        self.binary = str(getattr(mineru, "binary", "mineru") or "mineru")
        self.backend = str(getattr(mineru, "backend", "pipeline") or "pipeline")
        self.tier = str(getattr(mineru, "tier", "basic") or "basic").lower()
        self.model_source = str(getattr(mineru, "model_source", "local") or "local").lower()
        self.endpoint = str(getattr(mineru, "endpoint", "") or "").rstrip("/")
        self.timeout = float(getattr(mineru, "timeout_seconds", 900.0) or 900.0)
        self.extra_env: dict[str, str] = dict(getattr(mineru, "env", {}) or {})
        self.extra_env["MINERU_MODEL_SOURCE"] = self.model_source
        self.parse_path = str(getattr(mineru, "http_parse_path", "/file_parse") or "/file_parse")
        self.language = str(getattr(mineru, "language", "en") or "en")

    def parse(
        self,
        source: Path,
        *,
        filename_hint: str = "",
        selected_mode: str | None = None,
        contract_verified: bool = False,
    ) -> MinerURun:
        """Parse using a pinned transport; no retry or fallback after selection.

        Adapters pass the cached prober's selected mode so a compatible CLI
        avoids any second probe and an HTTP selection does not re-read OpenAPI.
        Direct auto-mode callers use the same prober once, which short-circuits
        after finding a compatible CLI.
        """
        if self.mode in ("disabled", "off", "none"):
            raise IntegrationUnavailableError("MinerU", "disabled in configuration")
        mode = (selected_mode or "").strip().lower()
        if not mode:
            if self.mode in {"cli", "http"}:
                mode = self.mode
            elif self.mode == "auto":
                status = MinerUProber(self.settings).status()
                if not status.available or status.mode not in {"cli", "http"}:
                    raise IntegrationUnavailableError(
                        "MinerU", status.reason or "no compatible runtime"
                    )
                mode = status.mode
                contract_verified = mode == "http"
            else:
                raise IntegrationUnavailableError("MinerU", f"unsupported mode {self.mode!r}")
        if self.mode in {"cli", "http"} and mode != self.mode:
            raise IntegrationUnavailableError(
                "MinerU",
                f"selected transport {mode!r} conflicts with explicitly configured mode {self.mode!r}",
            )
        if mode == "http":
            if not self.endpoint:
                raise IntegrationUnavailableError(
                    "MinerU", "[mineru].endpoint is required for http mode"
                )
            endpoint_problem = http_endpoint_problem(self.endpoint)
            if endpoint_problem:
                raise IntegrationUnavailableError("MinerU", endpoint_problem)
            if not contract_verified:
                compatible, reason, _version = legacy_http_parse_contract(
                    self.endpoint, self.parse_path
                )
                if not compatible:
                    raise IntegrationUnavailableError(
                        "MinerU",
                        f"HTTP service does not implement the supported JSON contract: {reason}",
                    )
            return self._parse_http(Path(source))
        if mode != "cli":
            raise IntegrationUnavailableError("MinerU", f"unsupported selected transport {mode!r}")
        return self._parse_cli(Path(source), filename_hint)

    def _parse_cli(self, source: Path, filename_hint: str = "") -> MinerURun:
        binary = resolve_cli_binary(self.binary)
        if not shutil.which(binary) and not Path(binary).expanduser().exists():
            raise IntegrationUnavailableError("MinerU", f"executable {binary!r} not found on PATH")
        kit_cli = is_mineru_kit(binary)
        workdir = Path(tempfile.mkdtemp(prefix=f"mineru-{short_hash(uuid.uuid4().hex, 8)}-"))
        ownership_marker_value = uuid.uuid4().hex
        try:
            (workdir / _OWNER_MARKER).write_text(ownership_marker_value, encoding="ascii")
            outdir = workdir / "out"
            outdir.mkdir(parents=True, exist_ok=True)
            if kit_cli:
                output_file = outdir / f"{source.stem or filename_hint or 'document'}_middle.json"
                command = [
                    binary,
                    "parse",
                    str(source),
                    "--output",
                    str(output_file),
                    "--format",
                    "middle_json",
                    "--tier",
                    self.tier,
                ]
            else:
                command = [
                    binary,
                    "-p",
                    str(source),
                    "-o",
                    str(outdir),
                    "-b",
                    self.backend,
                    "-l",
                    self.language,
                ]
            process = run_command(command, timeout=self.timeout, cwd=workdir, env=self.extra_env)
            raw_code = process.get("returncode", -1)
            code = -1 if raw_code is None else int(raw_code)
            stdout = str(process.get("stdout") or "")
            stderr = str(process.get("stderr") or "")
            duration = float(process.get("duration_ms") or 0.0)
            timed_out = bool(process.get("timeout"))
            artefacts: MinerURawOutput | None = None
            error = ""
            ok = False
            if code == 0 or any(outdir.rglob("*.json")) or any(outdir.rglob("*.md")):
                try:
                    from .normalize import load_mineru_outputs

                    artefacts = load_mineru_outputs(outdir, source.stem or filename_hint)
                    ok = True
                except (ExtractionError, OSError) as exc:
                    error = str(exc)
            if not ok and not error:
                reason = stderr.strip()[:400] or stdout.strip()[:400]
                error = f"mineru exited with code {code}: {reason}"
            elif ok and code != 0:
                state = (
                    f"timed out after {self.timeout:g}s"
                    if timed_out
                    else f"exited with code {code}"
                )
                error = (
                    f"mineru {state} but wrote usable output; the extraction may be incomplete: "
                    f"{stderr.strip()[:300]}"
                )
            return MinerURun(
                ok=ok,
                returncode=code,
                mode="cli",
                artefacts=artefacts,
                stdout=stdout[-4000:],
                stderr=safe_detail(_redact_paths(stderr[-4000:], source, workdir)),
                duration_ms=duration,
                command=_safe_command(command, source, workdir),
                output_dir=workdir,
                error=safe_detail(_redact_paths(error, source, workdir)),
                ownership_marker_value=ownership_marker_value,
                timed_out=timed_out,
            )
        except Exception:
            # No run object exists yet, so retain no scratch if command construction or discovery fails.
            scratch = MinerURun(
                ok=False,
                mode="cli",
                output_dir=workdir,
                ownership_marker_value=ownership_marker_value,
            )
            scratch.cleanup()
            raise

    def _parse_http(self, source: Path) -> MinerURun:
        url = self.endpoint + self.parse_path
        data = {
            "backend": self.backend,
            "lang_list": self.language,
            "return_md": "true",
            "return_middle_json": "true",
            "return_content_list": "true",
        }
        started = time.perf_counter()
        try:
            with source.open("rb") as handle:
                response = http_json(
                    "POST",
                    url,
                    files={"files": (source.name, handle, "application/pdf")},
                    data=data,
                    timeout=self.timeout,
                    follow_redirects=False,
                )
        except OSError as exc:
            raise IntegrationUnavailableError(
                "MinerU", f"could not read the source document ({type(exc).__name__})"
            ) from exc
        duration_ms = (time.perf_counter() - started) * 1000
        status_code = response.get("status_code")
        if (
            not response.get("ok")
            or not isinstance(status_code, int)
            or not 200 <= status_code < 300
        ):
            raise IntegrationUnavailableError(
                "MinerU", f"HTTP {status_code}: response details omitted"
            )
        content_type = str(response.get("content_type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json" and not content_type.endswith("+json"):
            raise IntegrationUnavailableError(
                "MinerU",
                f"HTTP parse returned unsupported content type {content_type!r}; JSON required",
            )
        payload = response.get("json")
        if not isinstance(payload, dict):
            raise IntegrationUnavailableError("MinerU", "HTTP parse response is not a JSON object")
        results = payload.get("results")
        if not isinstance(results, dict) or not results:
            raise IntegrationUnavailableError(
                "MinerU", "HTTP parse response has no non-empty 'results' object"
            )
        if set(results) != {source.name}:
            raise IntegrationUnavailableError(
                "MinerU",
                "HTTP parse response is ambiguous or does not match the uploaded filename",
            )
        entry = results[source.name]
        if not isinstance(entry, dict):
            raise IntegrationUnavailableError(
                "MinerU", f"HTTP parse response has no result object for {source.name!r}"
            )

        middle_value = entry.get("middle_json")
        content_value = entry.get("content_list")
        if content_value is None:
            content_value = entry.get("content_list_json")
        middle = _maybe_json(middle_value)
        content_list = _maybe_json(content_value)
        markdown_value = entry.get("md_content")
        if markdown_value is None:
            markdown_value = entry.get("markdown")
        if markdown_value is not None and not isinstance(markdown_value, str):
            raise IntegrationUnavailableError("MinerU", "HTTP markdown result must be a string")
        if middle_value is not None and not isinstance(middle, dict):
            raise IntegrationUnavailableError("MinerU", "HTTP middle_json result is malformed")
        if content_value is not None and not isinstance(content_list, list):
            raise IntegrationUnavailableError(
                "MinerU", "HTTP content_list result must be a JSON array"
            )
        markdown = markdown_value or ""
        if not middle and not content_list and not markdown.strip():
            raise IntegrationUnavailableError(
                "MinerU", "HTTP result contains no readable middle.json, content_list or markdown"
            )
        artefacts = MinerURawOutput(
            middle=middle if isinstance(middle, dict) else None,
            content_list=content_list,
            markdown=markdown,
        )
        return MinerURun(
            ok=True,
            mode="http",
            artefacts=artefacts,
            duration_ms=duration_ms,
        )


def _maybe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        from ..base import parse_json_loose

        return parse_json_loose(value)
    return None


__all__ = ["MinerUClient", "MinerURun"]
