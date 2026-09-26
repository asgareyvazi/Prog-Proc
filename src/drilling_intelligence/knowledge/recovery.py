"""Recovering the canonical field name of an already-stored extraction entry.

V4.2 separated engineering quantities that had shared a predicate, and reported the repair as *not
retroactive*: :meth:`KnowledgeFact.from_field` derives the predicate from the stored field name, so
a rebuild faithfully reproduced whatever the extractor emitted at ingest time - including the
collapsed names that predate the fix.

That conclusion was too pessimistic, and this module is why.  A stored entry is not just a name and
a number; it also carries ``provenance.excerpt``, the span of source text the value was read from::

    name="hole_depth"  value=10125  unit=ft  excerpt="MD 10125 ft"
    name="surface_pressure"  value=420  unit=psi  excerpt="SIDPP 420 psi"

The context was therefore never destroyed - it was recorded and then not consulted.  Re-running the
same deterministic extractor over that recorded span recovers the field name the entry would have
today, **without re-reading the source document** and without inventing anything: the excerpt is the
source's own text, and the extractor is the same pure function that ran at ingest.

Two boundaries are respected, and both are the point:

*   **The artefact is not rewritten.**  ``document_json`` stays a record of what the extractor
    produced at the time, so "a rebuild reads what was recorded" keeps holding.  The recovery happens
    on the way *out*, in the derivation, which is what makes it idempotent by construction rather
    than by bookkeeping.
*   **The vocabulary layer does not guess.**  Nothing here maps ``surface_pressure`` to ``sidpp``
    because psi suggests it.  The predicate is whatever the label in the excerpt says, and when the
    excerpt has no label the entry is reported as ambiguous and left alone.

The value is used for one narrow purpose: identifying *which* occurrence in the excerpt the stored
row came from, so a span containing two numbers cannot be attributed to the wrong one.  The
predicate still comes from the label, never from the number.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from drilling_intelligence.extraction.fields import FieldExtractor
from drilling_intelligence.knowledge.facts import predicate_for_field

__all__ = [
    "AMBIGUOUS",
    "DETERMINISTIC",
    "REEXTRACT",
    "SPLIT_PREDICATES",
    "UNCHANGED",
    "Recovery",
    "classify_entries",
    "plan_recovery",
    "recover_field_name",
]

#: The stored name already carries as much context as the excerpt can give.  Nothing to do.
UNCHANGED = "unchanged"
#: The excerpt states a label that resolves to a different predicate.  Safe to recover.
DETERMINISTIC = "deterministic"
#: The excerpt has no label, or supports more than one reading.  Reported, never guessed at.
AMBIGUOUS = "ambiguous"
#: No excerpt was recorded, so the source itself is the only remaining authority.
REEXTRACT = "requires_reextraction"


class Recovery(tuple[str, str]):
    """``(field_name, category)`` - what the entry should be called, and how sure we are."""

    @property
    def field(self) -> str:
        return self[0]

    @property
    def category(self) -> str:
        return self[1]


def _number(value: Any) -> float | None:
    """The stored value as a float, or ``None`` when it is not one.  Never converts a unit."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


#: The predicates a vocabulary fix split, and the specific assertions each one used to swallow.
#:
#: This is the repair matrix, and it is deliberately a closed table rather than a heuristic.  Two
#: reasons, both load-bearing:
#:
#: *   **Recovery may only refine, never demote.**  A stored ``mud_balance`` beside the excerpt
#:     ``"2025-05-30"`` must not become ``date_iso`` just because a date pattern matched the span -
#:     that would trade a specific, correct name for a generic one.  Keying on the predicates that
#:     were actually split makes that impossible: a name the vocabulary never collapsed is never a
#:     candidate.
#: *   **A remap must be auditable.**  Every row here is a distinction V4.2 introduced, and a row can
#:     only be added by pointing at the fix that made it.  Nothing in this module invents a mapping
#:     from a unit, a dimension or a numeric value.
#:
#: Adding a row is the whole contract: name the collapsed predicate, list what it may really have
#: been, and the excerpt decides which.
SPLIT_PREDICATES: dict[str, tuple[str, ...]] = {
    # SIDPP and SICP are shut-in observations on opposite sides of the string; MAASP is a limit.
    "surface_pressure": ("sidpp", "sicp", "maasp"),
    # The circulating system, a batch pumped on purpose, and what the well gave back.
    "mud_volume": ("pill_volume", "kick_volume", "trip_tank_volume"),
    # The string turning, and a laboratory instrument's setting.
    "rpm": ("rheometer_speed",),
    # Along-hole distance and vertical distance - different by design in any deviated well.
    "hole_depth": ("measured_depth", "true_vertical_depth"),
    # The bit, and the hole it drills.
    "hole_section_size": ("bit_size",),
}


def recover_field_name(
    name: str,
    excerpt: str,
    value: Any = None,
    *,
    extractor: FieldExtractor | None = None,
) -> Recovery:
    """Re-derive one stored entry's field name from the source text recorded beside it.

    Returns the name to use and a category:

    *   :data:`DETERMINISTIC` - the excerpt carries a label naming one of the assertions the stored
        predicate used to swallow.  Safe to recover.
    *   :data:`UNCHANGED` - either the stored predicate was never collapsed, or the excerpt confirms
        it as it stands.  Nothing to do, and nothing churned.
    *   :data:`AMBIGUOUS` - the stored predicate *was* collapsed and the excerpt cannot say which
        assertion it was.  Reported, never guessed at.
    *   :data:`REEXTRACT` - no excerpt was recorded, so the source document is the only authority.
    """
    stored = str(name or "").strip()
    if not stored:
        return Recovery((stored, UNCHANGED))

    stored_predicate = predicate_for_field(stored)[0]
    refinements = SPLIT_PREDICATES.get(stored_predicate)
    if not refinements:
        # Not a predicate the vocabulary ever collapsed.  There is nothing to recover, and looking
        # for something anyway is how a specific name gets demoted to a generic one.
        return Recovery((stored, UNCHANGED))

    text = str(excerpt or "").strip()
    if not text:
        # Nothing recorded to re-read.  Saying so is more useful than quietly keeping a name that
        # may be wrong.
        return Recovery((stored, REEXTRACT))

    hits = (extractor or FieldExtractor()).scan_text(text)
    if not hits:
        return Recovery((stored, AMBIGUOUS))

    want = _number(value)
    present: dict[str, str] = {}
    for hit in hits:
        got = _number(hit.value)
        if want is not None and got is not None and got != want:
            continue
        present.setdefault(predicate_for_field(hit.name)[0], hit.name)

    candidates = {pred: field for pred, field in present.items() if pred in refinements}
    if len(candidates) == 1:
        return Recovery((next(iter(candidates.values())), DETERMINISTIC))
    if len(candidates) > 1:
        # Two labels in one span, two different assertions, one stored value: the recorded excerpt
        # cannot settle it and choosing would be a guess.
        return Recovery((stored, AMBIGUOUS))
    # No refinement available.  If the excerpt at least restates the stored assertion, that is
    # confirmation; if it says nothing recognisable, the entry stays ambiguous.
    if stored_predicate in present:
        return Recovery((stored, UNCHANGED))
    return Recovery((stored, AMBIGUOUS))


def classify_entries(
    entries: Iterable[Mapping[str, Any]], *, extractor: FieldExtractor | None = None
) -> list[dict[str, Any]]:
    """Classify every stored extraction entry, changing nothing.

    This is the dry run.  Each result names the field, what it is called today, what it should be
    called, and why - so a repair can be read and argued with before it is applied, and so a row
    that cannot be settled is reported rather than skipped in silence.
    """
    out: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        provenance = entry.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        excerpt = str(provenance.get("excerpt") or "")
        field, category = recover_field_name(
            name, excerpt, entry.get("value"), extractor=extractor
        )
        out.append(
            {
                "stored_field": name,
                "stored_predicate": predicate_for_field(name)[0],
                "recovered_field": field,
                "recovered_predicate": predicate_for_field(field)[0],
                "category": category,
                "value": entry.get("value"),
                "unit": entry.get("unit"),
                "method": entry.get("method"),
                "excerpt": excerpt,
                "filename": provenance.get("filename"),
                "document_version_id": provenance.get("document_version_id"),
            }
        )
    return out


def plan_recovery(payloads: Iterable[Mapping[str, Any] | None]) -> dict[str, Any]:
    """Roll a set of stored artefact payloads up into one dry-run report.

    Counts are returned alongside the rows so a caller can report ``scanned``, ``deterministic``,
    ``ambiguous`` and ``requires_reextraction`` without walking the list twice, and so the three
    non-trivial categories cannot be collapsed into one another by accident.
    """
    rows: list[dict[str, Any]] = []
    for payload in payloads:
        rows.extend(classify_entries((payload or {}).get("extracted_fields") or ()))
    counts: dict[str, int] = dict.fromkeys(
        (UNCHANGED, DETERMINISTIC, AMBIGUOUS, REEXTRACT), 0
    )
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return {
        "scanned": len(rows),
        "counts": counts,
        "rows": rows,
    }
