"""Structured logging with secret redaction (master spec sections 59 and 61).

Two formats are supported:

*   ``text`` - readable console output for day-to-day development;
*   ``json`` - one object per line for ingestion runs and CI artefacts.

Every record carries the platform event fields (``event``, ``document_id``,
``well_id``, ``duration_ms`` ...) added via :func:`log_event`.  Keys that look
like credentials are redacted before anything is written, so a config file path
or an HTTP header can never leak a token into a log artefact.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REDACTED = "***REDACTED***"

_DEFAULT_SENSITIVE_KEYS = ("api_key", "token", "password", "secret", "authorization", "cookie")

# A bearer/token-looking string anywhere in a message is masked as well.
_TOKEN_PATTERNS = (
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)\S+"),
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{8,})\b"),
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._\-]{8,}"),
)

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)))


def _mask(value: Any, sensitive: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): (
                REDACTED if any(s in str(k).lower() for s in sensitive) else _mask(v, sensitive)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_mask(v, sensitive) for v in value]
    if isinstance(value, str):
        text = value
        for pattern in _TOKEN_PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + REDACTED, text)
        return text
    return value


class _JsonFormatter(logging.Formatter):
    def __init__(self, sensitive: tuple[str, ...]) -> None:
        super().__init__()
        self.sensitive = sensitive

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": _mask(record.getMessage(), self.sensitive),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = _mask(value, self.sensitive)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _TextFormatter(logging.Formatter):
    def __init__(self, sensitive: tuple[str, ...]) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-28s %(message)s", datefmt="%H:%M:%S")
        self.sensitive = sensitive

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            rendered = " ".join(
                f"{k}={_mask(v, self.sensitive)}" for k, v in sorted(extras.items())
            )
            base = f"{base} | {rendered}"
        return base


_CONFIGURED = False


def configure_logging(
    level: str = "INFO",
    *,
    file: str | Path | None = None,
    format: str = "text",  # noqa: A002 - mirrors the config key name
    sensitive_keys: tuple[str, ...] | list[str] | None = None,
    force: bool = False,
) -> logging.Logger:
    """Attach exactly one handler set to the platform root logger."""
    global _CONFIGURED  # noqa: PLW0603 - idempotence flag for handler installation
    root = logging.getLogger("drilling_intelligence")
    if _CONFIGURED and not force:
        return root
    for handler in list(root.handlers):
        root.removeHandler(handler)
    sensitive = tuple(k.lower() for k in (sensitive_keys or _DEFAULT_SENSITIVE_KEYS))
    formatter: logging.Formatter = (
        _JsonFormatter(sensitive) if format == "json" else _TextFormatter(sensitive)
    )
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if file:
        path = Path(file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.propagate = False
    _CONFIGURED = True
    return root


def get_logger(name: str) -> PlatformLogger:
    return PlatformLogger(logging.getLogger(f"drilling_intelligence.{name}"))


class PlatformLogger(logging.LoggerAdapter[logging.Logger]):
    """``logging`` adapter adding :meth:`event` for structured, redacted events."""

    def event(
        self,
        name: str,
        level: int = logging.INFO,
        duration_ms: float | None = None,
        *,
        exception: bool = False,
        **fields: object,
    ) -> None:
        payload: dict[str, object] = {"event": name, **fields}
        if duration_ms is not None:
            payload["duration_ms"] = round(duration_ms, 1)
        self.log(level, name, extra=payload, exc_info=exception)

    def warning_event(self, name: str, **fields: object) -> None:
        self.event(name, logging.WARNING, **fields)

    def error_event(self, name: str, **fields: object) -> None:
        self.event(
            name,
            logging.ERROR,
            exception=bool(fields.get("exc_info")),
            **{k: v for k, v in fields.items() if k != "exc_info"},
        )

    def process(self, msg: object, kwargs: dict[str, object]) -> tuple[object, dict[str, object]]:  # type: ignore[override]
        kwargs.setdefault("extra", {"component": self.name})
        return msg, kwargs


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    duration_ms: float | None = None,
    **fields: Any,
) -> None:
    """Emit a structured event.  ``event`` is the stable key for log analysis."""
    payload: dict[str, Any] = {"event": event, **fields}
    if duration_ms is not None:
        payload["duration_ms"] = round(duration_ms, 1)
    logger.log(level, event, extra=payload)


class timed:
    """Context manager logging duration of a block under a named event."""

    def __init__(
        self, logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any
    ) -> None:
        self.logger = logger
        self.event = event
        self.level = level
        self.fields = fields
        self.duration_ms = 0.0

    def __enter__(self) -> timed:
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.duration_ms = (time.perf_counter() - self._started) * 1000.0
        if exc_type is None:
            log_event(self.logger, self.level, self.event, self.duration_ms, **self.fields)
        else:
            log_event(
                self.logger,
                logging.ERROR,
                f"{self.event}.failed",
                self.duration_ms,
                error=str(exc),
                **self.fields,
            )
        return False


__all__ = ["REDACTED", "PlatformLogger", "configure_logging", "get_logger", "log_event", "timed"]
