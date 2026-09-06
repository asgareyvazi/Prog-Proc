"""The one tokenizer search depends on.

Two backends read the same text - SQLite's FTS5 for candidate retrieval and this module for
counting, filtering and ranking - so the definition of a *term* has to live in exactly one
place.  If it did not, the same query would find different chunks depending on whether an
optional SQLite extension happened to be compiled in, which is the sort of
capability-dependent-answers bug this project refuses to have.

What counts as a term, and why it looks like this:

*   ``12.5`` stays one term.  Splitting on the decimal point would make ``5`` a match for
    ``12.5`` and turn every depth in a field note into noise.
*   ``well-a3`` is indexed as ``well-a3``, ``well``, ``a3`` and ``wella3``: operators write
    ``WELL A-3``, ``Well_A3`` and ``well a3`` in the same folder, and a search that cannot
    bridge those spellings is not useful.  The raw form is kept as well, so a quoted phrase
    still matches exactly.
*   Case and accents are folded (``NFKC`` + ``casefold``), so ``PPG`` and ``ppg`` are the same
    term, and a dotted abbreviation (``p.p.g.``) is indexed under both spellings.
*   ``500/300`` (rpm) and ``10.2`` keep their internal punctuation: a ratio and a decimal are
    one idea each, not two.
*   A short, explicit stopword list is applied to *both* the index and the query.  Removing
    them from the query only would leave ``the`` required in the stored counts and quietly
    change results.

Numbers are not parsed or converted here: the platform never guesses a unit
(:mod:`drilling_intelligence.core.units` owns that), so ``12.5 ppg`` and ``12.5 bar`` are
simply different term sets.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

__all__ = ["PHRASE_MARKERS", "STOPWORDS", "term_counts", "tokenize", "tokens_of_query"]

#: A token starts with a letter or digit and may continue with the punctuation that appears
#: *inside* identifiers and decimals.  Trailing punctuation is not part of a token.
_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z\u00c0-\uffff](?:[\w.\-/+*#%']*[0-9A-Za-z\u00c0-\uffff])?")
#: Separators that are spelling variation rather than meaning.  Deliberately *not* the
#: decimal point: ``10.2`` must not produce ``2`` (which would match every second row of a
#: table), and ``/`` stays inside a ratio such as ``500/300`` rpm.
_SEPARATOR_RUN = re.compile(r"[-_]+")
#: ``p.p.g.`` / ``N.A.P.`` - a dotted abbreviation is also its undotted form.
_DOTTED_ABBREVIATION = re.compile(r"(?:[a-z]\.)+[a-z]?$")

#: English connectives that carry no retrieval value in drilling prose.  Deliberately small:
#: every word here is one less term a chunk must contain, and a long list starts deleting
#: meaning ("not", "no", "after" are all load-bearing in a report).
STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)

#: Quoted spans are phrase queries: the tokens must appear next to each other, in order.
PHRASE_MARKERS = ('"', "\u201c", "\u201d")


def _candidate_terms(word: str) -> list[str]:
    """All the forms one recognised word is indexed under, in a fixed order."""
    terms = [word]
    if word.endswith("'s"):
        # "the operator's MW" has to answer a search for "operator mw".
        stem = word[:-2]
        if len(stem) > 1:
            terms.append(stem)
    if _DOTTED_ABBREVIATION.match(word):
        undotted = word.replace(".", "")
        if len(undotted) > 1:
            terms.append(undotted)
    if _SEPARATOR_RUN.search(word):
        parts = [part for part in _SEPARATOR_RUN.split(word) if part]
        # Only substantial parts become terms: a lone "3" from "A-3" would match every
        # third row in a table and is not what a person meant when they typed "3".
        terms.extend(part for part in parts if len(part) > 1)
        joined = _SEPARATOR_RUN.sub("", word)
        if len(joined) > 1 and joined != word:
            terms.append(joined)
    if "." in word and not word[0].isdigit():
        # "rev.12" and "no.3" are two words written with a dot; "10.2" is one number, and
        # the leading-digit test is what keeps decimals whole.
        head, _, tail = word.partition(".")
        if head.isalpha() and len(head) > 2:
            terms.append(head)
            if len(tail) > 1:
                terms.append(tail)
    return terms


def tokenize(text: str, *, keep_stopwords: bool = False) -> list[str]:
    """Return the indexable terms of ``text``, in order, with duplicates kept.

    Order and duplicates matter: they are what a phrase check and a term frequency are
    computed from, and both have to be reproducible from the artefact alone.
    """
    if not text:
        return []
    normalised = unicodedata.normalize("NFKC", str(text)).casefold()
    out: list[str] = []
    for match in _TOKEN_PATTERN.findall(normalised):
        for term in _candidate_terms(match):
            if not keep_stopwords and term in STOPWORDS:
                continue
            if len(term) == 1 and not term.isdigit():
                continue  # a lone letter is noise: "A" matches everywhere and means nothing
            out.append(term)
    return out


def term_counts(text: str) -> dict[str, int]:
    """``{term: occurrences}`` for a chunk - the statistic BM25 and ``matches`` both read.

    Stored per chunk instead of being recomputed at query time, so a rebuild and a query
    cannot drift apart, and so the index stays searchable without re-parsing documents.
    """
    counts: dict[str, int] = {}
    for term in tokenize(text):
        counts[term] = counts.get(term, 0) + 1
    return counts


def parse_query(query: str) -> tuple[list[str], list[str]]:
    """Split a query into ``(terms, phrases)``.

    Quoted runs become phrases (matched against the chunk text, order sensitive) and are
    removed from the bag of required terms; their words still count towards ranking, which
    is what makes a phrase hit rank above a scattered one.
    """
    text = str(query or "")
    phrases: list[str] = []
    for opener, closer in (('"', '"'), ("\u201c", "\u201d")):
        while opener in text:
            start = text.index(opener)
            try:
                end = text.index(closer, start + 1)
            except ValueError:  # unbalanced quote: treat the rest as a phrase
                end = len(text)
            phrases.append(text[start + 1 : end].strip())
            text = text[:start] + " " + text[min(end + 1, len(text)) :]
    phrase_terms = [term for phrase in phrases for term in tokenize(phrase)]
    terms = tokenize(text)
    # Every term the query mentions, in first-occurrence order, deduplicated.
    ordered = list(dict.fromkeys([*phrase_terms, *terms]))
    return ordered, [phrase for phrase in phrases if phrase]


def tokens_of_query(query: str, *, extra: Iterable[str] = ()) -> list[str]:
    """Convenience for callers that only need the required terms (filters, highlights)."""
    terms, phrases = parse_query(query)
    out = [*terms, *(term for phrase in phrases for term in tokenize(phrase))]
    out.extend(term for item in extra for term in tokenize(item))
    return list(dict.fromkeys(out))


def highlight(
    text: str, terms: Iterable[str], *, context: int = 90
) -> tuple[str, list[tuple[int, int]]]:
    """A snippet around the matched region, plus the spans to emphasise.

    Returned as offsets rather than markup because the consumer decides how to show it (Qt
    rich text, a terminal, JSON for an API) - and because a snippet that rewrote the
    document text would break the "click through to the source" contract.  The spans are
    offsets *into the returned snippet*, which is what a renderer needs.
    """
    wanted = [term for term in dict.fromkeys(terms) if term]
    if not text:
        return "", []
    spans: list[tuple[int, int]] = []
    for term in wanted:
        # A word-boundary test on both sides: highlighting "well" inside "wellsite" would
        # tell the reader the chunk matched something it did not.
        pattern = re.compile(rf"(?<![\w.\-]){re.escape(term)}(?![\w.\-])", re.IGNORECASE)
        spans.extend((match.start(), match.end()) for match in pattern.finditer(text))
    spans.sort()
    if not spans:
        head = text[: context * 2]
        return head + ("\u2026" if len(text) > len(head) else ""), []
    start = max(0, spans[0][0] - context)
    stop = min(len(text), spans[-1][1] + context)
    prefix = "\u2026" if start > 0 else ""
    suffix = "\u2026" if stop < len(text) else ""
    offset = len(prefix) - start
    return (
        f"{prefix}{text[start:stop]}{suffix}",
        [(begin + offset, end + offset) for begin, end in spans if start <= begin < stop],
    )
