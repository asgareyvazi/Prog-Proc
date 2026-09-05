"""Extractor registry: the one place that decides which extractors exist.

The application, the pipeline and the UI all ask :func:`build_default_router`
for a configured router instead of instantiating extractors themselves, so
adding an extractor (or disabling MinerU) is a one-line change here.
"""

from __future__ import annotations

from typing import Any

from .docx import DocxExtractor
from .excel import ExcelExtractor
from .pdf_text import PdfTextExtractor
from .router import DocumentRouter
from .text import TextExtractor

DEFAULT_EXTRACTOR_ORDER = (PdfTextExtractor, ExcelExtractor, DocxExtractor, TextExtractor)


def default_extractors() -> list[Any]:
    """Built-in extractors in preference order (specific formats first)."""
    return [extractor() for extractor in DEFAULT_EXTRACTOR_ORDER]


def build_default_router(settings: Any, *, mineru: Any = None) -> DocumentRouter:
    """Assemble the router from configuration.

    ``mineru`` is an already-constructed adapter (or ``None``).  When the
    configuration leaves ``[mineru].mode = "auto"`` and no adapter is supplied,
    the MinerU CLI/HTTP surface is probed here, so callers never have to wire
    the integration themselves.  A probed-but-absent MinerU is not an error: the
    router simply keeps the built-in extractors and records why.
    """
    extractors: list[Any] = []
    if mineru is None and settings is not None:
        mode = str(getattr(getattr(settings, "mineru", None), "mode", "auto") or "auto").lower()
        if mode != "disabled":
            try:  # local import: the integration is optional at runtime
                from ..integrations.mineru.adapter import MinerUExtractor
                from ..integrations.mineru.discovery import MinerUProber

                prober = MinerUProber(settings)
                status = prober.status()
                if status.available:
                    mineru = MinerUExtractor(settings=settings, prober=prober)
            except Exception as exc:  # noqa: BLE001 - never let discovery break startup
                from ..core.logging import get_logger

                get_logger("extraction.registry").warning_event("mineru.discovery_failed", error=str(exc))
                mineru = None
    if mineru is not None:
        extractors.append(mineru)
    extractors.extend(default_extractors())
    return DocumentRouter(extractors, mineru_available=mineru is not None, settings=settings)


__all__ = ["DEFAULT_EXTRACTOR_ORDER", "build_default_router", "default_extractors"]
