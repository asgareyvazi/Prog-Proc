"""Qt-independent structured error for worker boundaries.

This module has no Qt dependency so that redaction and categorisation can be
exercised in headless environments without importing ``PySide6``.  The worker
layer (``drilling_intelligence.ui.worker``) re-exports this symbol so that
``from drilling_intelligence.ui.worker import WorkerError`` continues to work.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.redact import safe_detail


@dataclass(frozen=True)
class WorkerError:
    """Structured UI error without exposing a traceback to the operator."""

    category: str
    message: str
    hint: str = ""
    exception_type: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", " ".join(safe_detail(self.message).split()))
        object.__setattr__(self, "hint", " ".join(safe_detail(self.hint).split()))


__all__ = ["WorkerError"]
