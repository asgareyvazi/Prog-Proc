"""Document router: chooses an extractor, records *why*, and falls back safely.

Policy (master spec section 7):

*   MinerU is preferred for *suitable* complex PDFs - scans, missing text layer,
    low text density, table-heavy or multi-column layouts - because that is the
    problem it is built for.
*   Excel and DOCX are read natively.  Forcing MinerU on an .xlsx would destroy
    cell-level provenance, which is exactly what a DDR needs.
*   When a preferred extractor is unavailable or fails, the router degrades to a
    simpler parser and records the fallback.  The decision, the candidates and the
    reason are persisted with the extraction so a reviewer can always answer
    "why was this parsed this way?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.errors import ExtractionError, ParserUnavailableError
from ..core.logging import get_logger
from .interfaces import (
    DocumentComplexity,
    DocumentExtractor,
    ExtractionContext,
    ProvenanceBuilder,
    new_provenance_builder,
)
from .normalized import NormalizedDocument

log = get_logger("extraction.router")


@dataclass
class CandidateRecord:
    extractor: str
    supported: bool
    reason: str
    chosen: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"extractor": self.extractor, "supported": self.supported, "reason": self.reason, "chosen": self.chosen, "error": self.error}


@dataclass
class ExtractorChoice:
    """The routing decision, as an auditable object."""

    extractor: str
    extractor_version: str = ""
    reason: str = ""
    considered: list[CandidateRecord] = field(default_factory=list)
    fallback_from: str = ""
    #: True when MinerU was wanted but not usable in this environment.
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "extractor": self.extractor,
            "extractor_version": self.extractor_version,
            "reason": self.reason,
            "considered": [c.to_dict() for c in self.considered],
            "fallback_from": self.fallback_from,
            "degraded": self.degraded,
        }


class DocumentRouter:
    """Selects the extractor for a file and returns normalised content."""

    def __init__(self, extractors: list[DocumentExtractor], *, mineru_available: Any | None = None, settings: Any | None = None) -> None:
        """``mineru_available`` is a callable returning ``(bool, reason)``.

        It is injected (not imported) so that the router stays testable and the
        MinerU runtime probe stays a single place - the integration module.
        """
        self.extractors = list(extractors)
        self._mineru_available = mineru_available
        self.settings = settings

    # -- availability -------------------------------------------------------
    def mineru_available(self) -> tuple[bool, str]:
        if self._mineru_available is None:
            return False, "MinerU not wired into this router"
        if callable(self._mineru_available):
            try:
                result = self._mineru_available()
                if isinstance(result, tuple) and len(result) == 2:
                    return bool(result[0]), str(result[1])
                return bool(result), ""
            except Exception as exc:  # noqa: BLE001 - availability probes never raise
                return False, f"probe error: {type(exc).__name__}: {exc}"
        return bool(self._mineru_available), ""

    def list_extractors(self) -> list[dict[str, str]]:
        return [{"name": e.name, "version": e.version, "description": getattr(e, "description", "")} for e in self.extractors]

    def probe(self, context: ExtractionContext) -> Any:
        """Ask the first extractor that supports the file for a cheap complexity probe.

        "Cheap" is the contract: page/sheet counts and text-layer presence, never a full
        parse.  The numbers are advisory routing input and are also recorded in the
        router decision, so a reviewer can see what the choice was based on.
        """
        from .interfaces import DocumentComplexity

        for extractor in self.extractors:
            try:
                supported, _reason = extractor.supports(context)
            except Exception:  # noqa: BLE001
                continue
            if not supported:
                continue
            probe = getattr(extractor, "probe", None)
            if probe is None:
                continue
            try:
                return probe(context) or DocumentComplexity()
            except Exception as exc:  # noqa: BLE001 - probing is advisory only
                complexity = DocumentComplexity(reasons=[f"probe failed: {type(exc).__name__}: {exc}"])
                return complexity
        return DocumentComplexity()

    # -- selection ----------------------------------------------------------
    def route(self, context: ExtractionContext, *, options: dict[str, Any] | None = None) -> ExtractorChoice:
        """Decide which extractor *would* read this file, without reading it.

        This is the cheap half of :meth:`extract`: an extension check per candidate plus
        one structural probe (page count, text-layer presence, table count, sheet count).
        It costs milliseconds on a 600-page PDF because the probe only touches metadata
        and short text spans, and the result is enough to answer the cache question
        "which artefact would this file produce?" - so a duplicate file never runs a
        parser at all.

        Raises ``ParserUnavailableError`` when no candidate supports the file, which is
        the same decision ``extract()`` would have made, so routing failures are reported
        before any work is spent.
        """
        if options:
            context.options = {**context.options, **options}
        context.complexity = self.ensure_complexity(context)
        return self.select(context)

    def ensure_complexity(self, context: ExtractionContext) -> DocumentComplexity:
        """The file's structural facts, probed once per context (``None`` is tolerated)."""
        complexity = context.complexity
        if complexity is None or not complexity.probed:
            complexity = self.probe(context) or DocumentComplexity()
            complexity.probed = True
            context.complexity = complexity
        return complexity

    def select(self, context: ExtractionContext) -> ExtractorChoice:
        extension = context.extension.lower()
        preferred: list[tuple[DocumentExtractor, str]] = []
        records: list[CandidateRecord] = []

        for extractor in self.extractors:
            try:
                supported, reason = extractor.supports(context)
            except Exception as exc:  # noqa: BLE001
                supported, reason = False, f"support check failed: {type(exc).__name__}: {exc}"
            records.append(CandidateRecord(extractor=extractor.name, supported=supported, reason=reason))

        by_name = {extractor.name: extractor for extractor in self.extractors}
        mineru = by_name.get("mineru")
        if extension == ".pdf" and mineru is not None:
            available, why = self.mineru_available()
            complexity = context.complexity
            wants_mineru, want_reason = self._wants_mineru(complexity, context)
            if available and wants_mineru:
                preferred.append((mineru, f"MinerU selected for complex PDF ({want_reason})"))
            elif not available:
                records.append(CandidateRecord(extractor="mineru", supported=False, reason=f"unavailable: {why}", chosen=False))
            else:
                records.append(CandidateRecord(extractor="mineru", supported=True, reason=f"not required: {want_reason}", chosen=False))

        for extractor in self.extractors:
            if extractor.name == "mineru":
                continue
            record = next((r for r in records if r.extractor == extractor.name), None)
            if record is not None and record.supported:
                preferred.append((extractor, record.reason))

        if not preferred:
            raise ParserUnavailableError(
                f"No extractor can read {context.filename} ({extension or 'unknown extension'})",
                filename=context.filename,
                extension=extension,
                candidates=[r.to_dict() for r in records],
            )

        chosen, reason = preferred[0]
        for record in records:
            if record.extractor == chosen.name:
                record.chosen = True
        return ExtractorChoice(
            extractor=chosen.name,
            extractor_version=chosen.version,
            reason=reason,
            considered=records,
            degraded=extension == ".pdf" and mineru is not None and any(r.extractor == "mineru" and not r.supported for r in records),
        )

    def _wants_mineru(self, complexity: DocumentComplexity, context: ExtractionContext) -> tuple[bool, str]:
        """Routing heuristic for MinerU - thresholds are configuration, not code."""
        settings = self.settings
        pages_threshold = 12
        table_threshold = 25
        density_threshold = 250.0
        if settings is not None:
            pages_threshold = int(settings.mineru.prefer_when_pages_above)
            table_threshold = int(settings.mineru.prefer_when_table_rows_above)
            density_threshold = float(settings.mineru.prefer_when_text_chars_per_page_below)
        mode = "auto"
        if settings is not None:
            mode = str(settings.mineru.mode)
        if mode == "disabled":
            return False, "mineru.mode = disabled"
        if complexity.is_scanned:
            return True, "scanned pages without a text layer"
        if not complexity.has_text_layer:
            return True, "no text layer"
        if complexity.multi_column:
            return True, "multi-column layout"
        if complexity.table_count >= max(1, table_threshold // 8):
            return True, f"{complexity.table_count} table(s) detected"
        if complexity.pages >= pages_threshold and complexity.text_chars_per_page < density_threshold:
            return True, f"{complexity.pages} pages at {complexity.text_chars_per_page:.0f} text chars/page"
        if mode in ("cli", "http") and context.extension.lower() == ".pdf":
            return True, f"mineru.mode = {mode} (forced)"
        return False, f"simple PDF ({complexity.pages} pages, text layer present, {complexity.table_count} tables)"

    # -- extraction ---------------------------------------------------------
    def extract(
        self,
        context: ExtractionContext,
        *,
        options: dict[str, Any] | None = None,
        decision: ExtractorChoice | None = None,
    ) -> tuple[NormalizedDocument, ExtractorChoice, Any]:
        """Return ``(document, decision, extractor)``.

        The extractor is returned too, so the caller can stamp version info and
        so tests can assert which one actually produced the content.

        ``decision`` lets a caller that already routed the file (to look something up in
        the cache, say) pass its choice in instead of paying for the probe and the
        candidate scan twice.
        """
        if options:
            context.options = {**context.options, **options}
        if decision is None:
            context.complexity = self.ensure_complexity(context)
            decision = self.select(context)
        by_name = {extractor.name: extractor for extractor in self.extractors}
        order = [decision.extractor] + [r.extractor for r in decision.considered if r.supported and r.extractor != decision.extractor]
        errors: list[str] = []
        for name in order:
            extractor = by_name.get(name)
            if extractor is None:
                continue
            builder: ProvenanceBuilder = new_provenance_builder(context, extractor.name, extractor.version)
            try:
                document = extractor.extract(context, builder)
            except ExtractionError as exc:
                errors.append(f"{name}: {exc}")
                log.warning_event("extract.failed", extractor=name, file=context.filename, error=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - a third-party parser failing must not kill ingestion
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                log.error_event("extract.crashed", extractor=name, file=context.filename, error=str(exc), exc_info=True)
                continue
            if name != decision.extractor:
                document.diagnostics.append(
                    f"extraction fell back from {decision.extractor} to {name}; original failure: {errors[-1] if errors else 'unknown'}"
                )
                decision = ExtractorChoice(
                    extractor=name,
                    extractor_version=extractor.version,
                    reason=f"fallback after {decision.extractor} failed",
                    considered=decision.considered,
                    fallback_from=decision.extractor,
                    degraded=True,
                )
            document.metadata.parser = name
            document.metadata.parser_version = extractor.version
            document.metadata.extra["routing"] = decision.to_dict()
            document.diagnostics.extend(_complexity_notes(context.complexity))
            return document, decision, extractor
        raise ExtractionError(
            f"All candidate extractors failed for {context.filename}",
            filename=context.filename,
            attempts=errors,
        )


def _complexity_notes(complexity: DocumentComplexity) -> list[str]:
    if complexity is None:
        return []
    notes = []
    if complexity.is_scanned:
        notes.append("document is scanned: text-layer extraction will be poor; MinerU/OCR recommended")
    if complexity.encrypted:
        notes.append("PDF is encrypted")
    return notes


__all__ = ["CandidateRecord", "DocumentRouter", "ExtractorChoice"]
