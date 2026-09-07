"""Deterministic document classification (master spec section 15).

Why deterministic first: a drilling program is a drilling program because its
filename and body say so.  A rule-based classifier is reproducible (identical
input -> identical output), explainable (every score names the pattern that
fired), and testable against golden fixtures.  An LLM classifier may be added on
top later, but it must return a value from the same taxonomy and it is recorded
as ``method=llm`` so nobody mistakes an inferred label for a verified one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..__init__ import CLASSIFIER_VERSION
from ..core.enums import DocumentClassification
from ..core.provenance import Provenance
from .taxonomy import TAXONOMY, authority_for

#: Weighting of the evidence sources.  Documented here, not buried in code, so a
#: drilling reviewer can challenge the numbers rather than the implementation.
WEIGHTS = {"filename": 0.45, "content": 0.4, "extension": 0.05, "structure": 0.1}
#: Minimum margin over the runner-up for a confident label.
MIN_MARGIN = 0.08
#: Below this confidence the label is reported but flagged as weak.
WEAK_CONFIDENCE = 0.35
#: Absolute score at which the winning rule has earned full confidence.  Below it the
#: confidence is scaled down proportionally: two rules that have both barely matched are
#: not a confident answer, however far apart they are from each other.
STRONG_EVIDENCE = 0.15
#: A best score below this is noise, not a weak label: the document is reported as
#: OTHER rather than as a low-confidence guess at a specific type.
NOISE_SCORE = 0.05


@dataclass
class Evidence:
    """One matched pattern, with the text and location that produced it."""

    source: str  # filename | content | extension | structure
    classification: DocumentClassification
    weight: float
    pattern: str
    matched_text: str
    page: int | None = None
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "classification": self.classification.value,
            "weight": round(self.weight, 4),
            "pattern": self.pattern[:80],
            "matched_text": self.matched_text[:120],
            "page": self.page,
            "provenance_ref": self.provenance.ref if self.provenance else "",
        }


@dataclass
class ClassificationResult:
    """Classification outcome with its full explanation."""

    classification: DocumentClassification
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    method: str = "deterministic"
    version: str = CLASSIFIER_VERSION
    notes: list[str] = field(default_factory=list)
    #: Authority tier assigned from the type + document status (section 83).
    authority_tier: str = "general_knowledge"

    @property
    def runner_up(self) -> tuple[str, float] | None:
        ordered = sorted(self.scores.items(), key=lambda item: item[1], reverse=True)
        if len(ordered) < 2:
            return None
        return ordered[1][0], ordered[1][1]

    @property
    def ambiguous(self) -> bool:
        runner_up = self.runner_up
        if runner_up is None:
            return False
        return (self.scores.get(self.classification.value, 0.0) - runner_up[1]) < MIN_MARGIN

    @property
    def weak(self) -> bool:
        return self.confidence < WEAK_CONFIDENCE

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "confidence": round(self.confidence, 4),
            "method": self.method,
            "version": self.version,
            "authority_tier": self.authority_tier,
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
            "evidence": [e.to_dict() for e in self.evidence[:24]],
            "notes": list(self.notes),
            "ambiguous": self.ambiguous,
            "weak": self.weak,
        }


@dataclass
class DeterministicClassifier:
    """Scores every taxonomy entry against the document and picks the best."""

    weights: dict[str, float] = field(default_factory=lambda: dict(WEIGHTS))
    #: Inspect at most this many paragraphs for content patterns (keeps large PDFs cheap).
    max_paragraphs: int = 800
    #: Only content patterns matching inside this many leading characters count for
    #: the "front matter" bonus (DDR headers, program covers).
    front_matter_chars: int = 6000

    def classify(
        self,
        *,
        filename: str,
        text: str = "",
        extension: str = "",
        document: Any | None = None,
        declared_status: str | None = None,
        is_current: bool = True,
    ) -> ClassificationResult:
        scores: dict[str, float] = {signature.classification.value: 0.0 for signature in TAXONOMY}
        evidence: list[Evidence] = []
        raw_name = (filename or "").lower()
        # Delimiters carry no meaning for a reader: "daily_drilling_report",
        # "daily-drilling-report" and "Daily Drilling Report.pdf" must all score the
        # same, otherwise the strongest evidence a file offers (its name) is lost.
        lowered_name = re.sub(r"[_\-.]+", " ", raw_name)

        for signature in TAXONOMY:
            # --- filename evidence
            for pattern, weight in signature.filename_patterns:
                match = re.search(pattern, lowered_name)
                if match:
                    scores[signature.classification.value] += weight * self.weights["filename"]
                    evidence.append(
                        Evidence(
                            source="filename",
                            classification=signature.classification,
                            weight=weight * self.weights["filename"],
                            pattern=pattern,
                            matched_text=match.group(0),
                        )
                    )
            # --- extension evidence (weak by itself, never decisive)
            if extension and extension.lower().lstrip(".") in {
                ext.lstrip(".") for ext in signature.extensions
            }:
                scores[signature.classification.value] += 0.02 * self.weights["extension"] * 10
                evidence.append(
                    Evidence(
                        source="extension",
                        classification=signature.classification,
                        weight=0.02 * self.weights["extension"] * 10,
                        pattern=f"extension:{extension}",
                        matched_text=extension,
                    )
                )

        body = text or ""
        paragraph_provenance: dict[int, Provenance] = {}
        pages: dict[int, str] = {}
        if document is not None:
            body = document.text or body
            for paragraph in document.paragraphs:
                if paragraph.provenance and paragraph.text:
                    paragraph_provenance[hash(paragraph.text[:80])] = paragraph.provenance
                if paragraph.page:
                    pages.setdefault(paragraph.page, "")
                    pages[paragraph.page] += paragraph.text + "\n"

        if body:
            scan = body[:400_000]
            front = scan[: self.front_matter_chars]
            for signature in TAXONOMY:
                for pattern, weight in signature.content_patterns:
                    match = re.search(pattern, scan, re.IGNORECASE | re.DOTALL)
                    if not match:
                        continue
                    bonus = 1.15 if re.search(pattern, front, re.IGNORECASE | re.DOTALL) else 1.0
                    page = self._page_of(scan, match.start(), pages, document)
                    source_provenance = self._provenance_for(document, scan, match.start())
                    scores[signature.classification.value] += (
                        weight * self.weights["content"] * bonus
                    )
                    evidence.append(
                        Evidence(
                            source="content",
                            classification=signature.classification,
                            weight=weight * self.weights["content"] * bonus,
                            pattern=pattern,
                            matched_text=match.group(0).strip(),
                            page=page,
                            provenance=source_provenance,
                        )
                    )
                for pattern in signature.negative:
                    if re.search(pattern, scan, re.IGNORECASE):
                        scores[signature.classification.value] -= 0.15
                        evidence.append(
                            Evidence(
                                source="content",
                                classification=signature.classification,
                                weight=-0.15,
                                pattern=f"NEGATIVE {pattern}",
                                matched_text="negative evidence",
                            )
                        )

            # Structural evidence: a table-heavy workbook is a report, not a book.
            table_count = len(getattr(document, "tables", []) or [])
            sheets = (
                len(
                    getattr(getattr(document, "metadata", None), "extra", {})
                    .get("workbook", {})
                    .get("sheets", [])
                )
                if document is not None
                else 0
            )
            if table_count >= 3 or sheets >= 3:
                for signature in TAXONOMY:
                    if signature.tabular:
                        scores[signature.classification.value] += (
                            0.06 * self.weights["structure"] * 10
                        )
                        evidence.append(
                            Evidence(
                                source="structure",
                                classification=signature.classification,
                                weight=0.06 * self.weights["structure"] * 10,
                                pattern=f"tables:{table_count},sheets:{sheets}",
                                matched_text="table-heavy document",
                            )
                        )

        # Nothing readable in the document itself: a filename alone must never carry
        # more than a suggestion, and the reason has to be visible in the result.
        content_evidence_missing = not (text or "").strip()

        best = max(scores, key=lambda key: scores[key])
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        raw = max(0.0, scores[best])
        runner = max(0.0, ordered[1][1]) if len(ordered) > 1 else 0.0
        separation = raw / (raw + runner) if raw else 0.0
        confidence = (
            min(0.98, separation * min(1.0, raw / STRONG_EVIDENCE)) if raw > NOISE_SCORE else 0.0
        )
        if content_evidence_missing:
            confidence = min(confidence, 0.45)
        notes: list[str] = []
        if content_evidence_missing:
            # Recorded on both branches: "we could not read it" is the reason, and the
            # UI needs it whether the answer is OTHER or a filename-only hint.
            notes.append(
                "no text content available: classified from filename and extension only (OCR/MinerU required)"
            )
        if raw <= NOISE_SCORE:
            best = DocumentClassification.OTHER.value
            confidence = 0.0
            notes.append(
                "no rule matched: classified as OTHER"
                if raw <= 0.0
                else f"evidence too thin to name a type (best score {raw:.2f} <= {NOISE_SCORE:.2f}): classified as OTHER"
            )
        else:
            if confidence < WEAK_CONFIDENCE:
                notes.append(f"weak confidence ({confidence:.2f}): keep for review")
            if len(ordered) > 1 and (raw - ordered[1][1]) < MIN_MARGIN:
                notes.append(f"ambiguous: {ordered[1][0]} is within {MIN_MARGIN:.2f} of the leader")
        if declared_status:
            notes.append(f"registry status: {declared_status}")

        classification = DocumentClassification(best)
        result = ClassificationResult(
            classification=classification,
            confidence=round(confidence, 4),
            scores=scores,
            evidence=evidence,
            notes=notes,
            authority_tier=authority_for(
                classification, status=declared_status, is_current=is_current
            ),
        )
        return result

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _page_of(scan: str, position: int, pages: dict[int, str], document: Any) -> int | None:
        if document is None or not pages:
            return None
        cumulative = 0
        for page, text in sorted(pages.items()):
            cumulative += len(text)
            if position <= cumulative:
                return page
        return max(pages) if pages else None

    @staticmethod
    def _provenance_for(document: Any, scan: str, position: int) -> Provenance | None:
        """Attach the provenance of the nearest preceding paragraph with a locator.

        Best effort by design: classification evidence needs a *pointer into the
        document*, and the paragraph stream is what carries locators.
        """
        if document is None:
            return None
        best: Provenance | None = None
        best_start = -1
        for paragraph in getattr(document, "paragraphs", []):
            if not paragraph.provenance or not paragraph.text:
                continue
            start = paragraph.char_start or scan.find(paragraph.text[:80])
            if start <= position and start > best_start:
                best, best_start = paragraph.provenance, start
        return best

    @staticmethod
    def validate(value: str | DocumentClassification | None) -> DocumentClassification | None:
        """Accept only taxonomy members - the contract any future LLM must satisfy."""
        if value is None:
            return None
        if isinstance(value, DocumentClassification):
            return value
        # A model answers "Drilling Program" as readily as "DRILLING_PROGRAM", and both
        # are unambiguous once separators are normalised; anything else is rejected
        # rather than approximated.
        token = re.sub(r"[\s\-]+", "_", str(value).strip().upper())
        try:
            return DocumentClassification(token)
        except ValueError:
            return None


__all__ = [
    "MIN_MARGIN",
    "NOISE_SCORE",
    "STRONG_EVIDENCE",
    "WEAK_CONFIDENCE",
    "WEIGHTS",
    "ClassificationResult",
    "DeterministicClassifier",
    "Evidence",
]
