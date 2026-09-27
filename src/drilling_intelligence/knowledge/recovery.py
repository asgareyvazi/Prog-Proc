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
    "CLEAN",
    "CONFLICTS_PRESENT",
    "DERIVED_DRIFT_STATES",
    "DETERMINISTIC",
    "INDEX_STALE",
    "KNOWLEDGE_AND_INDEX_STALE",
    "KNOWLEDGE_STALE",
    "REEXTRACT",
    "SCHEMA_OUT_OF_DATE",
    "SPLIT_PREDICATES",
    "STRUCTURED_INDEX_STALE",
    "UNCHANGED",
    "UNRECOVERABLE",
    "Recovery",
    "assess_recovery",
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
        field, category = recover_field_name(name, excerpt, entry.get("value"), extractor=extractor)
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
    counts: dict[str, int] = dict.fromkeys((UNCHANGED, DETERMINISTIC, AMBIGUOUS, REEXTRACT), 0)
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return {
        "scanned": len(rows),
        "counts": counts,
        "rows": rows,
    }


# --------------------------------------------------------------------------- workspace recovery
#
# The categories above classify one *stored field*.  What an operator actually asks is a different
# question - "is this workspace sound, and if not what is the smallest safe sequence of commands
# that makes it sound?" - and the signals for that answer already exist: ``doctor`` runs the
# registry invariants, ``KnowledgeExtractionService.status`` reports ``needs_rebuild`` and
# ``detached_facts``, and ``SearchService.stats`` reports the index drift.  Nothing below re-derives
# any of it; :func:`assess_recovery` is a pure function over those numbers so that the planner, the
# CLI and a test all read the same state the same way.

#: Nothing derived is behind the registry.
CLEAN = "clean"
#: Derived knowledge is behind the registry: facts point at versions that are no longer current, or
#: an artefact exists that was never derived.  This is ``status()``'s own ``needs_rebuild``.
KNOWLEDGE_STALE = "knowledge_stale"
#: The search index disagrees with the registry about documents and chunks.
INDEX_STALE = "index_stale"
#: Promoted structured rows (a lesson, a problem, an NPT record) the index has not seen.
STRUCTURED_INDEX_STALE = "structured_index_stale"
#: Both halves of the derived state are behind, so one command will not finish the job.
KNOWLEDGE_AND_INDEX_STALE = "knowledge_and_index_stale"
#: Two sources disagree and nobody has decided.  Evidence, not damage - see the module note.
CONFLICTS_PRESENT = "conflicts_present"
#: The schema is behind the migration head.
SCHEMA_OUT_OF_DATE = "schema_out_of_date"
#: A registry invariant is actually broken.  The only state that means corruption.
UNRECOVERABLE = "unrecoverable"

#: The states that mean "derived state has drifted", as opposed to "something is broken" or
#: "two people disagree".  ``RECOVERY_REQUIRED`` from the design vocabulary is the union of these
#: and is exposed as the ``recovery_required`` flag rather than as a separate state, because a
#: planner that says only "recovery required" has thrown away the one thing that decides which
#: command to run.
DERIVED_DRIFT_STATES: tuple[str, ...] = (
    KNOWLEDGE_STALE,
    INDEX_STALE,
    STRUCTURED_INDEX_STALE,
    KNOWLEDGE_AND_INDEX_STALE,
)


def _count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def assess_recovery(signals: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Name the workspace's recovery state and the minimum safe sequence that fixes it.

    ``signals`` is the numbers the existing surfaces already produce - ``integrity_problems`` from
    :func:`~drilling_intelligence.database.integrity.check_current_version_invariants` and friends,
    ``schema`` from the migration state, ``knowledge`` from
    :meth:`KnowledgeExtractionService.status`, ``index`` from ``SearchService.stats`` and
    ``conflicts`` from the conflict report.  Passing them in rather than reading the database here
    is what keeps this deterministic and testable: the same numbers always give the same state.

    The severity order is the important part.  Four conditions are kept apart because they have
    four different remedies, and collapsing them is how a repair tool ends up "fixing" things that
    were never broken:

    1.  :data:`UNRECOVERABLE` - a registry invariant is broken.  Nothing may be rebuilt on top of
        it, because a rebuild derives from a registry that is currently lying.
    2.  :data:`SCHEMA_OUT_OF_DATE` - the schema is behind head.  Same reason: migrate first.
    3.  the derived-drift states - the data is fine, the *projections* of it are behind.
    4.  :data:`CONFLICTS_PRESENT` - two sources disagree.  This is the state that must never be
        reported as corruption and never "recovered" automatically, so it is listed under
        ``not_performed`` with the command a person uses to review it, and on its own it leaves the
        workspace :data:`CLEAN` as far as recovery is concerned.
    """
    signals = signals or {}

    def section(name: str) -> Mapping[str, Any]:
        value = signals.get(name)
        return value if isinstance(value, Mapping) else {}

    integrity = list(signals.get("integrity_problems") or [])
    schema = section("schema")
    knowledge = section("knowledge")
    index = section("index")
    conflicts = section("conflicts")

    # ``status()`` already decides this, and re-deciding it differently here would give the operator
    # two answers to the same question.  The fallback recomputes the same rule from raw counts for
    # callers that hand over the pieces instead of the verdict.
    knowledge_stale = bool(knowledge.get("needs_rebuild")) or (
        "needs_rebuild" not in knowledge
        and (
            _count(knowledge.get("detached_facts")) > 0
            or (
                _count(knowledge.get("facts")) == 0
                and _count(knowledge.get("versions_with_artefacts")) > 0
            )
        )
    )
    index_broken = bool(index.get("error"))
    index_stale = index_broken or any(
        _count(index.get(key)) > 0 for key in ("stale_versions", "orphaned", "missing_versions")
    )
    structured_stale = any(
        _count(index.get(key)) > 0
        for key in ("structured_missing", "structured_stale", "structured_orphaned")
    )
    open_conflicts = _count(conflicts.get("open"))
    ambiguous = _count(conflicts.get("ambiguous"))
    schema_behind = bool(schema) and schema.get("up_to_date") is False

    conditions: list[str] = []
    explanation: list[str] = []
    if integrity:
        conditions.append(UNRECOVERABLE)
        explanation.append(
            f"{len(integrity)} registry invariant(s) are violated; a rebuild derives from the "
            "registry, so it cannot be trusted until these are fixed"
        )
    if schema_behind:
        conditions.append(SCHEMA_OUT_OF_DATE)
        explanation.append(
            f"schema is at {schema.get('current')!r} while head is {schema.get('head')!r}"
        )
    if knowledge_stale:
        conditions.append(KNOWLEDGE_STALE)
        explanation.append(
            f"{_count(knowledge.get('detached_facts'))} fact(s) point at versions the registry no "
            f"longer considers current; {_count(knowledge.get('versions_without_knowledge'))} "
            "current version(s) with an artefact have no derived facts"
        )
    if index_stale:
        conditions.append(INDEX_STALE)
        explanation.append(
            "the search index disagrees with the registry"
            + (f" (index reported an error: {index.get('error')})" if index_broken else "")
        )
    if structured_stale:
        conditions.append(STRUCTURED_INDEX_STALE)
        explanation.append(
            f"{_count(index.get('structured_missing'))} structured row(s) are not in the index, "
            f"{_count(index.get('structured_stale'))} are no longer searchable, "
            f"{_count(index.get('structured_orphaned'))} are orphaned"
        )
    if open_conflicts:
        conditions.append(CONFLICTS_PRESENT)
        explanation.append(
            f"{open_conflicts} unresolved engineering conflict(s): two sources state different "
            "values for the same quantity, which is a disagreement to review, not a defect"
        )
    if ambiguous:
        explanation.append(
            f"{ambiguous} key(s) are ambiguous within one source; extraction, not recovery, decides "
            "those"
        )

    if UNRECOVERABLE in conditions:
        state = UNRECOVERABLE
    elif schema_behind:
        state = SCHEMA_OUT_OF_DATE
    elif knowledge_stale and (index_stale or structured_stale):
        state = KNOWLEDGE_AND_INDEX_STALE
    elif knowledge_stale:
        state = KNOWLEDGE_STALE
    elif structured_stale and not index_stale:
        state = STRUCTURED_INDEX_STALE
    elif index_stale:
        state = INDEX_STALE
    elif open_conflicts:
        state = CONFLICTS_PRESENT
    else:
        state = CLEAN

    recommended: list[dict[str, Any]] = []
    not_performed: list[dict[str, Any]] = []
    if state is UNRECOVERABLE:
        not_performed.append(
            {
                "item": "knowledge rebuild",
                "reason": "the registry's own invariants are violated; fix those first, because a "
                "rebuild takes the registry's word for what is current",
                "command": "drillintel doctor",
            }
        )
    elif state is SCHEMA_OUT_OF_DATE:
        recommended.append(
            {
                "step": 1,
                "operation": "schema upgrade",
                "command": "alembic upgrade head",
                "reason": "derived state must not be rebuilt onto a schema behind head",
            }
        )
    else:
        step = 0
        if knowledge_stale:
            step += 1
            recommended.append(
                {
                    "step": step,
                    "operation": "knowledge rebuild",
                    "command": "drillintel knowledge rebuild",
                    "reason": "re-derives facts from the stored artefacts; manual notes survive",
                }
            )
        if index_stale or structured_stale:
            step += 1
            recommended.append(
                {
                    "step": step,
                    "operation": "index rebuild",
                    "command": "drillintel index rebuild",
                    "reason": (
                        "re-projects documents, knowledge chunks and structured rows into the "
                        "search sidecar"
                        if step == 1
                        else "a knowledge rebuild rewrites knowledge chunks but not the structured "
                        "rows, so the sidecar still needs its own pass"
                    ),
                }
            )
        if open_conflicts:
            not_performed.append(
                {
                    "item": f"{open_conflicts} unresolved engineering conflict(s)",
                    "reason": "a disagreement between two sources is evidence; no rebuild may "
                    "settle it, and none does",
                    "command": "drillintel knowledge conflicts",
                }
            )

    return {
        "state": state,
        "conditions": conditions,
        "corrupt": state is UNRECOVERABLE,
        "recovery_required": state in DERIVED_DRIFT_STATES
        or state in (UNRECOVERABLE, SCHEMA_OUT_OF_DATE),
        "recommended": recommended,
        "not_performed": not_performed,
        "explanation": explanation,
    }
