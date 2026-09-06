"""MinerU as a :class:`DocumentExtractor` (master spec section 7).

The adapter is where the external engine becomes a platform citizen: it produces
the same normalised document as the built-in extractors, keeps page+bbox
provenance, and - crucially - reports its own limitations in ``diagnostics`` so a
reviewer can see what kind of extraction produced a fact.
"""

from __future__ import annotations

from typing import Any

from ...__init__ import EXTRACTION_ENGINE_VERSION
from ...core.errors import ExtractionError, ParserUnavailableError
from ...core.logging import get_logger
from ...extraction.interfaces import DocumentComplexity, ExtractionContext, ProvenanceBuilder
from ...extraction.normalized import NormalizedDocument, clean_text
from .client import MinerUClient
from .discovery import MinerUProber
from .normalize import (
    load_mineru_outputs,
    normalize_content_list,
    normalize_markdown,
    normalize_middle_json,
)

log = get_logger("integrations.mineru.adapter")


class MinerUExtractor:
    """Preferred extractor for complex, table-heavy or scanned PDFs."""

    name = "mineru"
    version = EXTRACTION_ENGINE_VERSION
    description = (
        "MinerU document parser (external CLI/HTTP engine): layout, tables, OCR, formulas."
    )

    def __init__(self, settings: Any, prober: MinerUProber | None = None) -> None:
        self.settings = settings
        self.prober = prober or MinerUProber(settings)
        self.client = MinerUClient(settings)

    # -- DocumentExtractor protocol ----------------------------------------
    def supports(self, context: ExtractionContext) -> tuple[bool, str]:
        status = self.prober.status()
        if not status.available:
            return False, f"runtime unavailable: {status.reason}"
        extension = context.extension.lower()
        if extension in (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"):
            return True, f"MinerU {status.version or '?'} via {status.mode} handles {extension}"
        if extension in (".docx", ".pptx", ".xlsx"):
            # MinerU can parse these, but native extractors keep richer
            # cell/structure provenance, so we deliberately decline.
            return False, f"{extension}: native extractor preserves more provenance than MinerU"
        return False, f"unsupported input type {extension or '(none)'}"

    def probe(self, context: ExtractionContext) -> DocumentComplexity:
        status = self.prober.status()
        return DocumentComplexity(
            pages=context.complexity.pages,
            has_text_layer=context.complexity.has_text_layer,
            text_chars_per_page=context.complexity.text_chars_per_page,
            table_count=context.complexity.table_count,
            is_scanned=context.complexity.is_scanned,
            multi_column=context.complexity.multi_column,
            reasons=[f"MinerU runtime: {status.summary()}"],
        )

    def extract(
        self, context: ExtractionContext, provenance: ProvenanceBuilder
    ) -> NormalizedDocument:
        status = self.prober.status()
        if not status.available:
            raise ParserUnavailableError(
                f"MinerU is not usable here: {status.reason}",
                component="MinerU",
                hint="install MinerU separately, or set [mineru].mode = 'disabled' to silence the fallback",
            )
        try:
            run = self.client.parse(context.path, filename_hint=context.filename)
        except ParserUnavailableError:
            raise
        except Exception as exc:
            raise ExtractionError(f"MinerU run failed: {type(exc).__name__}: {exc}") from exc
        artefacts = run.artefacts
        if not run.ok or artefacts is None:
            raise ExtractionError(
                f"MinerU returned no usable output: {run.error or 'unknown error'}", mode=run.mode
            )

        document, diagnostics = self._normalize(artefacts, context)
        document.metadata.engine = (document.metadata.engine or "") or f"MinerU via {run.mode}"
        document.metadata.extra["mineru"] = {
            "mode": run.mode,
            "duration_ms": round(run.duration_ms, 1),
            "artefact": artefacts.best,
            "command": " ".join(run.command),
            "stderr_tail": (run.stderr or "")[-600:],
            "version": status.version,
        }
        document.metadata.extra["provenance_source"] = artefacts.best
        document.diagnostics.extend(diagnostics)
        document.diagnostics.append(
            f"extracted by MinerU ({run.mode}, artefact={artefacts.best}) in {run.duration_ms / 1000:.1f}s"
        )
        if artefacts.layout_pdf and artefacts.layout_pdf.exists():
            document.metadata.extra["mineru"]["layout_pdf"] = str(artefacts.layout_pdf)
        run.cleanup()
        return document

    # -- internals ----------------------------------------------------------
    def _normalize(
        self, artefacts: Any, context: ExtractionContext
    ) -> tuple[NormalizedDocument, list[str]]:
        kwargs = {
            "filename": context.filename,
            "document_id": context.document_id,
            "version_id": context.document_version_id,
            "sha256": context.sha256,
        }
        if artefacts.middle:
            document, diagnostics = normalize_middle_json(artefacts.middle, **kwargs)
        elif artefacts.content_list:
            document, diagnostics = normalize_content_list(artefacts.content_list, **kwargs)
        else:
            document, diagnostics = normalize_markdown(artefacts.markdown or "", **kwargs)
        document.metadata.filename = context.filename
        document.metadata.path = str(context.path)
        document.metadata.size_bytes = context.size_bytes
        document.metadata.extension = context.extension
        document.metadata.parser = self.name
        document.metadata.parser_version = self.version
        if not document.text and artefacts.markdown:
            document.text = clean_text(artefacts.markdown)
        return document, diagnostics


__all__ = ["MinerUExtractor", "load_mineru_outputs"]
