"""MinerU as a :class:`DocumentExtractor` (master spec section 7).

The adapter is where the external engine becomes a platform citizen: it produces
the same normalised document as the built-in extractors, keeps page+bbox
provenance, and - crucially - reports its own limitations in ``diagnostics`` so a
reviewer can see what kind of extraction produced a fact.
"""

from __future__ import annotations

from typing import Any

from ... import EXTRACTION_ENGINE_VERSION
from ...core.errors import ExtractionError, ParserUnavailableError
from ...core.logging import get_logger
from ...core.redact import safe_detail
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


def _safe_run_text(value: str, run: Any, context: ExtractionContext) -> str:
    text = str(value or "")
    paths = ((context.path, "<document>"), (run.output_dir, "<temporary-output>"))
    for path, replacement in paths:
        if path is None:
            continue
        candidates = {str(path), path.as_posix()}
        for candidate in candidates:
            if candidate:
                text = text.replace(candidate, replacement)
    return safe_detail(text)


def _command_summary(run: Any, client: MinerUClient) -> str:
    """Redact source/scratch paths from MinerU command/error metadata.

    Generic ``NormalizedDocument.metadata.path`` remains product provenance and is separate from
    these sanitized MinerU-specific command and error fields.
    """
    if run.mode != "cli":
        return "HTTP multipart/JSON parse"
    if "--format" in run.command:
        return (
            "mineru-kit parse <document> --output <temporary-output> "
            f"--format middle_json --tier {client.tier}"
        )
    if "-o" in run.command:
        return (
            f"mineru -p <document> -o <temporary-output> -b {client.backend} -l {client.language}"
        )
    return "MinerU CLI (command details omitted)"


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
            run = self.client.parse(
                context.path,
                filename_hint=context.filename,
                selected_mode=status.mode,
                contract_verified=status.mode == "http",
            )
        except ParserUnavailableError:
            raise
        except Exception as exc:
            detail = safe_detail(f"{type(exc).__name__}: {exc}")
            raise ExtractionError(f"MinerU run failed: {detail}") from exc
        document: NormalizedDocument | None = None
        try:
            artefacts = run.artefacts
            if not run.ok or artefacts is None:
                raise ExtractionError(
                    f"MinerU returned no usable output: {run.error or 'unknown error'}",
                    mode=run.mode,
                )
            if (
                not artefacts.middle
                and not artefacts.content_list
                and not (artefacts.markdown or "").strip()
            ):
                raise ExtractionError(
                    "MinerU returned a well-formed response containing no middle.json, content_list "
                    "or markdown; refusing to record an empty document as an extraction",
                    mode=run.mode,
                )

            document, diagnostics = self._normalize(artefacts, context)
            if not document.text.strip() and not document.tables and not document.figures:
                raise ExtractionError(
                    "MinerU output contained no normalized text, tables, or figures; "
                    "refusing to record an empty document",
                    mode=run.mode,
                )
            document.metadata.engine = (document.metadata.engine or "") or f"MinerU via {run.mode}"
            document.metadata.extra["mineru"] = {
                "mode": run.mode,
                "model_source": self.client.model_source,
                "duration_ms": round(run.duration_ms, 1),
                "artefact": artefacts.best,
                "command": _command_summary(run, self.client),
                "stderr_tail": _safe_run_text(run.stderr, run, context)[-600:],
                "version": status.version,
                "returncode": run.returncode,
                "partial": run.partial,
                "timed_out": run.timed_out,
                "error": _safe_run_text(run.error, run, context),
                "scratch_cleanup": "pending" if run.output_dir else "not_applicable",
            }
            document.metadata.extra["provenance_source"] = artefacts.best
            document.diagnostics.extend(diagnostics)
            document.diagnostics.append(
                f"extracted by MinerU ({run.mode}, artefact={artefacts.best}) in {run.duration_ms / 1000:.1f}s"
            )
            if run.returncode != 0:
                document.diagnostics.append(
                    f"MinerU exited with code {run.returncode}; output was used but may be incomplete"
                )
            if run.partial:
                document.diagnostics.append(
                    "MinerU produced usable output but exited abnormally; this extraction is partial "
                    "and must not be read as a complete parse"
                )
            return document
        finally:
            # Scratch is always transient. The cleanup status is recorded without persisting a path.
            had_scratch = run.output_dir is not None
            cleaned = run.cleanup()
            if document is not None:
                mineru_meta = document.metadata.extra.get("mineru")
                if isinstance(mineru_meta, dict):
                    mineru_meta["scratch_cleanup"] = (
                        (
                            "removed"
                            if had_scratch and cleaned and run.output_dir is None
                            else "not_applicable"
                        )
                        if cleaned
                        else "failed"
                    )
                    if run.cleanup_error:
                        mineru_meta["scratch_cleanup_error"] = _safe_run_text(
                            run.cleanup_error, run, context
                        )
                if not cleaned:
                    document.diagnostics.append(
                        "MinerU temporary output cleanup failed; transient parser files may remain"
                    )

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
