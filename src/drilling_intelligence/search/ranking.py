"""Candidate selection and ranking - the part of search that must not depend on the backend.

The index stores, for every chunk, its term counts and its length.  That is enough to answer
three questions in pure Python, and it is answered identically whether the candidate rows
came from SQLite's FTS5 or from an in-memory dict:

1.  *does this chunk actually match?*  every query term must be present (AND), with a
    documented fallback to "any term" when AND finds nothing, and quoted phrases must appear
    in order in the text;
2.  *how good is it?*  Okapi BM25 (``k1=1.2``, ``b=0.75``) over the corpus statistics the
    backend supplies, times a small kind weight;
3.  *what wins a tie?*  the lowest ``document_id`` then the lowest chunk index, so two runs
    over the same data produce the same list in the same order.

Why BM25 rather than FTS5's own ``bm25()``: SQLite's function is a column-weighted occurrence
score with no access to corpus-wide inverse document frequency, and it cannot be reproduced
without it.  A ranking we can explain line by line - and show in the UI next to a result - is
worth more here than a few milliseconds, especially when the number decides which revision of
a report an engineer reads first.

The kind weights are the one judgement call.  A ``field`` chunk is an extractor's assertion
("mud_weight = 10.2 ppg"), a ``table_row`` is a cell range, a ``paragraph`` is prose; if a
query matches an explicit field, that is the better answer.  They are multipliers, not a
filter, and the corpus-wide ranking is untouched.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .tokenize import tokenize

__all__ = ["IndexStatistics", "MatchedChunk", "candidate_matches", "rank_chunks"]

#: Okapi BM25 parameters.  ``k1`` controls term-frequency saturation (a chunk mentioning
#: "mud" forty times is not forty times better than one that mentions it four times) and
#: ``b`` controls length normalisation.  These are the standard values; search quality is
#: asserted in tests by *relative* order, not by an absolute score.
K1 = 1.2
B = 0.75

#: Multipliers applied to the BM25 score, by chunk kind.
DEFAULT_KIND_WEIGHTS: dict[str, float] = {
    "field": 1.35,
    "table_row": 1.15,
    "heading": 1.1,
    "paragraph": 1.0,
    "page": 0.9,
    "diagnostic": 0.5,
}


@dataclass(frozen=True)
class IndexStatistics:
    """Corpus-wide numbers BM25 needs, read from the index (never from the query)."""

    total_chunks: int = 0
    total_length: int = 0
    #: ``term -> number of chunks containing it``.  Missing means zero.
    document_frequency: Mapping[str, int] = field(default_factory=dict)

    @property
    def average_length(self) -> float:
        if self.total_chunks <= 0:
            return 0.0
        return self.total_length / self.total_chunks

    def idf(self, term: str, *, df: int | None = None) -> float:
        """``ln(1 + (N - df + 0.5) / (df + 0.5))`` - always positive, so ranking never flips."""
        total = max(0, int(self.total_chunks))
        frequency = int(self.document_frequency.get(term, 0) if df is None else df)
        return math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))


@dataclass(frozen=True)
class MatchedChunk:
    """One ranked hit, with the numbers that explain it."""

    row: Any
    score: float
    matched_terms: tuple[str, ...]
    #: Per-term contributions, for the UI's "why this result" disclosure.
    term_scores: Mapping[str, float] = field(default_factory=dict)

    def explain(self) -> str:  # pragma: no cover - debugging aid
        parts = ", ".join(f"{term}={value:.3f}" for term, value in sorted(self.term_scores.items()))
        return f"{self.score:.4f} [{parts}]"


def candidate_matches(
    counts: Mapping[str, int],
    text: str,
    *,
    terms: Sequence[str],
    phrases: Sequence[str],
    require_all: bool = True,
) -> bool:
    """Does a chunk satisfy the query?  Terms are AND-ed; phrases must appear in order.

    ``require_all=False`` is the documented fallback: "mud weight loss circulation" finds
    nothing as an AND over four terms in a mud report corpus, and an empty result page is a
    worse user experience than a broader one that is *labelled* as broadened (the service
    reports it).
    """
    if not terms and not phrases:
        return False
    hit = 0
    for term in terms:
        if term in counts:
            hit += 1
        elif require_all:
            return False
    if require_all and terms and hit != len(terms):
        return False
    if not require_all and terms and hit == 0:
        return False
    return all(phrase_present(text, phrase) for phrase in phrases)


def phrase_present(text: str, phrase: str) -> bool:
    """Order-sensitive substring test, on terms rather than characters.

    "10.2 ppg" must match "…10.2 ppg measured…" but not "…ppg … 10.2 …", which an
    AND-of-terms check would happily allow.  Comparing token sequences (not raw
    characters) is what makes it survive the case, accent and punctuation folding the
    index applies.
    """
    wanted = tokenize(phrase)
    if not wanted:
        return False
    haystack = tokenize(text)
    span = len(wanted)
    for start in range(0, max(0, len(haystack) - span + 1)):
        if haystack[start : start + span] == wanted:
            return True
    return False


def rank_chunks(
    chunks: Iterable[tuple[Any, Mapping[str, int], int, str, str]],
    *,
    terms: Sequence[str],
    phrases: Sequence[str] = (),
    statistics: IndexStatistics,
    require_all: bool = True,
    kind_weights: Mapping[str, float] = DEFAULT_KIND_WEIGHTS,
    limit: int = 20,
) -> list[MatchedChunk]:
    """Filter, score and order.  ``chunks`` items are ``(row, term_counts, length, kind, text)``.

    Sorting is ``score`` descending, then ``document_id`` then ``chunk_index`` ascending, so
    a repeated search over unchanged data returns the identical list.
    """
    scored: list[MatchedChunk] = []
    average_length = statistics.average_length or 1.0
    for row, counts, length, kind, text in chunks:
        if not candidate_matches(counts, text, terms=terms, phrases=phrases, require_all=require_all):
            continue
        contribution: dict[str, float] = {}
        for term in terms:
            frequency = int(counts.get(term, 0))
            if frequency <= 0:
                continue
            denominator = frequency + K1 * (1.0 - B + B * (length / average_length if average_length else 1.0))
            contribution[term] = statistics.idf(term) * (frequency * (K1 + 1.0)) / denominator
        if not contribution and not phrases:
            continue
        weight = float(kind_weights.get(kind, 1.0))
        total = sum(contribution.values()) * weight
        if total <= 0.0:
            # A phrase-only hit with no scoring terms still deserves to be returned.
            total = weight * sum(statistics.idf(term) for term in tokenize(phrase_join(phrases)))
        scored.append(
            MatchedChunk(
                row=row,
                score=round(total, 9),
                matched_terms=tuple(sorted(contribution)),
                term_scores={term: round(value, 9) for term, value in contribution.items()},
            )
        )
    scored.sort(key=lambda item: (-item.score, _document_id(item.row), _chunk_index(item.row)))
    return scored[:limit] if limit > 0 else scored


def phrase_join(phrases: Iterable[str]) -> str:
    return " ".join(phrases)


def _document_id(row: Any) -> str:
    return str(getattr(row, "document_id", "") or (row.get("document_id") if isinstance(row, Mapping) else "") or "")


def _chunk_index(row: Any) -> int:
    value = getattr(row, "chunk_index", None)
    if value is None and isinstance(row, Mapping):
        value = row.get("chunk_index", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):  # pragma: no cover - defensive: rows come from our schema
        return 0
