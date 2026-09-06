"""Normalising the words a report uses into the tokens the database aggregates by.

Operations, events, NPT, problems and cost categories are all *open* vocabularies in the field:
every operator's reporting system has its own codes, and a fixed enum either loses the ones it does
not know or forces a migration for every new one.  So the schema stores a snake_case token and this
module owns the mapping from source wording to token, in one place, with two rules:

*   **Two spellings of one thing must reach one token.**  ``Tripping``, ``trip out`` and
    ``TRIPPING_OUT`` are the same operation, and an aggregation that grouped them apart would
    under-report exactly the wells that use the other spelling.
*   **A token nobody recognises is kept, never renamed.**  Folding an unknown code into ``other``
    is how a field's most interesting problem disappears into a bucket.  The raw wording travels
    with the token (in the record's ``attributes``), so a reader can always see what the source
    actually said and correct the mapping here rather than edit rows.

This is deliberately not the knowledge layer's predicate registry
(:mod:`drilling_intelligence.knowledge.facts`).  That one *must* land on a registered predicate,
because a predicate decides how a value is parsed, normalised and compared; these decide only how a
row is grouped, and an unregistered value is a normal outcome here and an error there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .enums import (
    COST_CATEGORIES,
    COST_CATEGORY_ALIASES,
    KNOWN_EVENT_CATEGORIES,
    KNOWN_OPERATION_TYPES,
    OPERATION_ALIASES,
    PROBLEM_CODE_ALIASES,
    PROBLEM_TYPES,
    SEVERITY_ALIASES,
    SeverityLevel,
)

#: What to call a value the vocabulary does not know when the column may not be empty.
FALLBACK_VALUE = "other"

_SEPARATOR = re.compile(r"[^0-9a-z]+")


def snake_token(value: object, *, fallback: str = "") -> str:
    """``'Trip Out (2nd)'`` -> ``'trip_out_2nd'``: lowercase, single separators, nothing invented.

    Empty in, ``fallback`` out - never a guess.  A caller that must store something uses
    :data:`FALLBACK_VALUE` and says so in the record's attributes, which keeps "the source was
    blank" distinguishable from "the source said other".
    """
    text = str(value or "").strip().lower().replace("_", " ")
    token = _SEPARATOR.sub("_", text).strip("_")
    return token or fallback


@dataclass(frozen=True)
class VocabMatch:
    """A token, plus whether the vocabulary recognised it and what the source wrote.

    ``recognised`` is the part a reader needs: an aggregation over a hundred rows where thirty of
    them matched nothing is a report on sixty, and saying so is the difference between a tool that
    admits its coverage and one that quietly averages what it happened to understand.
    """

    token: str
    #: The wording exactly as the source had it (never normalised away).
    raw: str
    #: True when ``token`` came from the registered vocabulary rather than the source's own wording.
    recognised: bool

    @property
    def key(self) -> str:
        """The attributes key that records an unrecognised code, when there is one to record."""
        return "" if self.recognised else "source_wording"

    def to_dict(self) -> dict[str, str]:
        return {"token": self.token, "raw": self.raw, "recognised": self.recognised}

    def note(self) -> str:
        """What to append to a record's ``attributes`` when the mapping did not fire."""
        return "" if self.recognised else f"{self.raw!r} is not a registered token"


def _match(
    value: object,
    *,
    known: frozenset[str],
    aliases: dict[str, str],
    fallback: str = FALLBACK_VALUE,
) -> VocabMatch:
    raw = str(value or "").strip()
    if not raw:
        return VocabMatch(fallback, raw, False)
    folded = raw.lower()
    if folded in known:
        return VocabMatch(folded, raw, True)
    # An alias may name a known token or another alias; one hop is enough, and more than one hop
    # would be a lookup table nobody can read.
    candidate = aliases.get(folded) or aliases.get(snake_token(folded))
    if candidate is None:
        candidate = aliases.get(snake_token(folded, fallback=folded))
    if candidate is not None:
        return VocabMatch(str(candidate), raw, str(candidate) in known)
    token = snake_token(folded, fallback=fallback)
    if token in known:
        return VocabMatch(token, raw, True)
    return VocabMatch(token, raw, False)


def operation_type(value: object) -> VocabMatch:
    """The operation a report was describing: ``'Tripping out'`` -> ``tripping``."""
    return _match(value, known=KNOWN_OPERATION_TYPES, aliases=OPERATION_ALIASES)


#: Event categories have no aliases of their own beyond the snake_case fold: they are named after
#: the department that owns them, and each operator spells those differently on purpose.
def event_category(value: object) -> VocabMatch:
    return _match(value, known=KNOWN_EVENT_CATEGORIES, aliases={})


def problem_type(value: object) -> VocabMatch:
    """A problem code as written on a report -> the token the field aggregates by.

    ``NPT-STUCK`` becomes ``stuck_pipe`` and is recognised; ``NPT-HOLE-ANGULAR`` becomes
    ``npt_hole_angular`` and is not, so it stays visible as its own group until somebody decides
    whether it belongs with ``poor_hole_cleaning`` or deserves a type of its own.
    """
    return _match(value, known=PROBLEM_TYPES, aliases=PROBLEM_CODE_ALIASES)


def cost_category(value: object) -> VocabMatch:
    return _match(value, known=COST_CATEGORIES, aliases=COST_CATEGORY_ALIASES)


def severity(value: object) -> SeverityLevel | None:
    """The four severity words, or ``None``.

    ``None`` is the answer more often than not, and it is the right one: a report that says
    "bit was pulled" does not tell anybody how bad it was, and a severity invented here would be
    counted in every average that follows.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw in SEVERITY_ALIASES:
        return SEVERITY_ALIASES[raw]
    return SEVERITY_ALIASES.get(snake_token(raw, fallback=raw).replace("_", " "))


__all__ = [
    "FALLBACK_VALUE",
    "VocabMatch",
    "cost_category",
    "event_category",
    "operation_type",
    "problem_type",
    "severity",
    "snake_token",
]
