"""When two sources disagree: keep both, mark the argument, decide nothing in secret.

The whole feature lives or dies here.  A mud report says 10.2 ppg, the drilling program says
10.4 ppg, and any "last write wins" rule - the easiest possible implementation - quietly turns a
safety-relevant discrepancy into a stale row nobody can find.  So this module's job is:

*   **group** every statement about the same subject, property and record state
    (``KnowledgeFact.lookup_key`` is exactly that grouping key);
*   **compare** the values in canonical units through :mod:`drilling_intelligence.core.units`,
    so 10.2 ppg and 1222 kg/m3 are the same number and 10.2 ppg and 10.4 ppg are not;
*   **record** the disagreement, with every candidate kept and the ranking basis written down;
*   **demote, never delete**: the facts involved become ``CONFLICTED``, and a ``UNVERIFIED`` fact
    stays ``UNVERIFIED`` - a value nobody could confirm does not become a real point of
    contention by being wrong in good company.

The ranking is computed and *reported*, never applied: which source wins is an engineering
decision (or, much later, a reviewer's), and the platform's job is to make the choice informed.
:meth:`resolve_conflict` is the only path that picks a side, and it records who did it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..core.enums import ConflictResolution, KnowledgeStatus, SourceAuthority
from ..core.units import Quantity, resolve_unit
from ..database.models import Document, DocumentVersion, KnowledgeConflict, KnowledgeItem

__all__ = [
    "AUTHORITY_RANK",
    "ConflictCandidate",
    "ConflictReport",
    "detect_conflicts",
    "resolve_conflict",
    "values_agree",
]


#: The ladder :data:`~drilling_intelligence.core.enums.SourceAuthority` defines, most authoritative
#: first.  Order matters and is not a guess: an approved program outranks a report about a shift.
AUTHORITY_RANK: tuple[str, ...] = (
    SourceAuthority.APPROVED_DRILLING_PROGRAM.value,
    SourceAuthority.APPROVED_ENGINEERING_DOCUMENT.value,
    SourceAuthority.CURRENT_PROGRAM_REVISION.value,
    SourceAuthority.CURRENT_OPERATIONAL_REPORT.value,
    SourceAuthority.TECHNICAL_REFERENCE.value,
    SourceAuthority.PREVIOUS_REVISION.value,
    SourceAuthority.HISTORICAL_REPORT.value,
    SourceAuthority.GENERAL_KNOWLEDGE.value,
)

REL_TOLERANCE = 1e-9
ABS_TOLERANCE = 1e-12
#: When two sources state a number in *different* units, floating-point noise is not the only
#: difference to allow for: a metric sheet writes "1222 kg/m3" and a mud log writes "10.2 ppg", and
#: those are the same mud.  So a cross-unit comparison is judged at the precision each side actually
#: stated - half of its last written decimal place - and the ceiling stops "10 ppg" from swallowing
#: a 3 % difference just because it was written with two significant figures.  Same-unit comparisons
#: stay tight: two sources writing the same unit with different digits really do disagree.
CROSS_UNIT_MAX_TOLERANCE = 0.02


def values_agree(left: KnowledgeItem, right: KnowledgeItem) -> bool:
    """Do two stored facts state the same thing?

    Quantities compare in the canonical base unit of their dimension, which is the only way
    "10.2 ppg" and "1.222 kg/l" can be recognised as agreement at all.  Anything without a unit
    (a text value, a date rendered as text, a dimensionless ratio) compares as normalised text.
    Tolerances are the ones :class:`~drilling_intelligence.core.units.Quantity` uses for equality,
    deliberately not tightened here: two representations of one number differ by float noise, and
    inventing a stricter rule would invent conflicts.
    """
    left_number, right_number = _base_value(left), _base_value(right)
    if left_number is not None and right_number is not None:
        return math.isclose(
            left_number,
            right_number,
            rel_tol=_relative_tolerance(left, right),
            abs_tol=ABS_TOLERANCE,
        )
    left_text, right_text = _comparable_text(left), _comparable_text(right)
    if left_text and right_text:
        return left_text == right_text
    # One side is a number, the other is not: they are different statements, and treating that as
    # agreement would hide a unit error behind a formatting accident.
    return (
        left_number is None and right_number is None and (left.value or 0.0) == (right.value or 0.0)
    )


def _relative_tolerance(left: KnowledgeItem, right: KnowledgeItem) -> float:
    """How far apart two numbers may be and still be the same statement.

    Same unit: :data:`REL_TOLERANCE`, i.e. only float noise - there is no excuse for two sources
    writing different digits in the same unit.  Different units: the coarser side's stated
    precision, because converting "1222 kg/m3" into ppg yields 10.198... and a rule that demanded
    exactness would report a conflict for every metric sheet in the corpus.
    """
    if _units_match(left, right):
        return REL_TOLERANCE
    return max(
        _stated_relative_uncertainty(left), _stated_relative_uncertainty(right), REL_TOLERANCE
    )


def _units_match(left: KnowledgeItem, right: KnowledgeItem) -> bool:
    """Do both rows state their number in the same unit, allowing for spelling?"""
    resolved: list[str | None] = []
    for row in (left, right):
        token = str(row.unit or "").strip()
        if not token:
            resolved.append(None)
            continue
        try:
            resolved.append(resolve_unit(token).symbol)
        except Exception:  # noqa: BLE001 - an unresolvable unit is not a match for anything
            resolved.append(token.casefold())
    if resolved[0] is None or resolved[1] is None:
        return resolved[0] is resolved[1]
    return resolved[0] == resolved[1]


def _stated_relative_uncertainty(row: KnowledgeItem) -> float:
    """Half of the last written decimal place of this value, as a fraction of the value.

    ``original_value`` is what the source wrote, which is the only place the *stated precision*
    survives: ``10.2`` was written to a tenth and ``10.20`` to a hundredth, and after conversion
    both are floats.  A value with no digits at all (or a zero) gets the ceiling, so the comparison
    is decided by the other side rather than by an absent one.
    """
    magnitude = abs(float(row.value)) if row.value else 0.0
    match = re.search(r"\d+(?:[.,]\d+)?", str(row.original_value or ""))
    if match is None or not magnitude:
        return CROSS_UNIT_MAX_TOLERANCE
    decimals = (
        len(re.split(r"[.,]", match.group(0))[1]) if re.search(r"[.,]\d", match.group(0)) else 0
    )
    absolute = 0.5 * 10.0 ** (-decimals)
    return min(absolute / magnitude, CROSS_UNIT_MAX_TOLERANCE)


def _base_value(row: KnowledgeItem) -> float | None:
    if row.value is None or not str(row.unit or "").strip():
        return None
    try:
        return Quantity.of(float(row.value), resolve_unit(str(row.unit))).base_value
    except Exception:  # noqa: BLE001 - an unresolvable unit makes this not a quantity
        return None


def _comparable_text(row: KnowledgeItem) -> str:
    text = str(row.content or row.original_value or "").strip()
    return " ".join(text.split()).casefold()


def _authority_rank(source_id: str | None, source_authority: str | None) -> int:
    tier = str(source_authority or "").strip()
    try:
        return AUTHORITY_RANK.index(tier)
    except ValueError:
        return len(AUTHORITY_RANK)


@dataclass(frozen=True)
class ConflictCandidate:
    """One side of an argument, with everything a reviewer needs to pick a side."""

    item_id: str
    value: float | None
    unit: str
    text: str
    source: str
    authority_tier: str
    authority_rank: int
    document_status: str
    revision: str
    version_number: int
    document_date: str
    confidence: float | None
    fact_status: str
    locator_ref: str
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


@dataclass(frozen=True)
class ConflictReport:
    """What a detection pass found and did."""

    keys_examined: int = 0
    conflicts: int = 0
    agreements: int = 0
    items_marked: int = 0
    cleared: int = 0
    #: Keys where one source states several values.  Reported rather than marked as a conflict -
    #: see the note in :func:`detect_conflicts` for why an intra-file disagreement is a different
    #: problem from a cross-source one.
    ambiguous: int = 0
    details: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "keys_examined": self.keys_examined,
            "conflicts": self.conflicts,
            "agreements": self.agreements,
            "items_marked": self.items_marked,
            "cleared": self.cleared,
            "ambiguous_within_source": self.ambiguous,
            "details": list(self.details)[:50],
        }


def _candidates(repository: Any, rows: Sequence[KnowledgeItem]) -> list[ConflictCandidate]:
    """Rank the parties, most authoritative first, with a stable tie-break.

    The ordering is documented *in the conflict row*, which is the difference between "the system
    prefers the program revision" and an unexplained answer: a reader can see the basis and
    overrule it.
    """
    built: list[ConflictCandidate] = []
    for row in rows:
        document = repository.session.get(Document, row.document_id) if row.document_id else None
        version = (
            repository.session.get(DocumentVersion, row.document_version_id)
            if row.document_version_id
            else None
        )
        provenance_rows = list(row.provenance or [])
        provenance = dict(provenance_rows[0]) if provenance_rows else {}
        payload = dict(row.payload or {})
        built.append(
            ConflictCandidate(
                item_id=str(row.id),
                value=None if row.value is None else float(row.value),
                unit=str(row.unit or ""),
                text=str(row.content or ""),
                source=str(
                    (document.filename if document else None) or row.document_id or "unknown"
                ),
                authority_tier=str(
                    (document.source_authority if document else None) or "general_knowledge"
                ),
                authority_rank=_authority_rank(
                    row.source_id, document.source_authority if document else None
                ),
                document_status=str(document.status if document else "UNKNOWN"),
                revision=str(
                    (document.revision if document else None)
                    or (f"v{version.version_number}" if version else "")
                ),
                version_number=int(version.version_number if version else 0),
                document_date=document.document_date.date().isoformat()
                if document is not None and document.document_date
                else "",
                confidence=None if row.confidence is None else float(row.confidence),
                fact_status=str(row.status or ""),
                locator_ref=str(payload.get("locator_ref") or _locator_ref(provenance)),
                provenance=provenance,
            )
        )
    # One ordering, stated once: newest document date, then most authoritative, then newest
    # version, then id so that a tie never depends on dictionary order.  A reader's first question
    # about a disagreement is "which file said this most recently", and the tie-break below that
    # is the authority ladder the classification already assigned.
    built.sort(
        key=lambda item: (
            item.document_date,
            -item.authority_rank,
            item.version_number,
            item.item_id,
        ),
        reverse=True,
    )
    return built


def _locator_ref(provenance: dict[str, Any]) -> str:
    from ..core.provenance import Provenance

    try:
        return str(Provenance.from_dict(dict(provenance)).locator.ref())
    except Exception:  # noqa: BLE001 - a candidate with unreadable provenance is still a candidate
        return ""


def detect_conflicts(
    repository: Any, *, keys: Sequence[str] | None = None, include_manual: bool = True
) -> ConflictReport:
    """Compare every group of statements and record the ones that disagree.

    ``keys`` limits the pass (one document's worth of facts after an ingest); the default is
    everything, which is what ``knowledge rebuild`` runs.  Superseded and retired rows are excluded
    from the comparison - they are history, and a newer revision or a human decision already answered
    them - but they are never deleted, so a reviewer can still see what revision 11 said.

    A conflict needs two *sources*.  Two values inside one revision of one file are counted as
    ``ambiguous`` instead, because the knowledge layer cannot adjudicate what a table meant and
    must not pretend that a document contradicts itself in the sense an engineer would mean.
    """
    lookup_keys = list(keys) if keys is not None else repository.lookup_keys()
    examined = 0
    conflicts = 0
    agreements = 0
    marked = 0
    cleared = 0
    ambiguous = 0
    details: list[dict[str, Any]] = []

    for lookup_key in lookup_keys:
        rows = [
            row
            for row in repository.facts_by_key(lookup_key, include_superseded=False)
            if _eligible(row, include_manual=include_manual)
        ]
        versions = {str(row.document_version_id or "") for row in rows}
        if len(rows) < 2 or len(versions) < 2:
            if rows:
                # One voice is not an argument; make sure no stale conflict is left claiming one.
                cleared += repository.clear_conflict(
                    lookup_key, record_state=str(rows[0].record_state or "")
                )
                agreements += 1
            if len(versions) < 2 and len(rows) > 1:
                # Two values from *one* revision of one file are not a dispute between sources -
                # they are one source saying a thing twice (a table with a depth per row, a field
                # name that maps onto the same predicate from two cells).  Calling that a conflict
                # would teach the user to ignore the conflict list, so it is counted separately and
                # named: the fix belongs to the extraction, and "ambiguous within one source" is the
                # honest description of what the knowledge layer can see.
                ambiguous += 1
                details.append(
                    {
                        "lookup_key": lookup_key,
                        "property": _split_key(lookup_key)[0],
                        "ambiguous_within_source": len(rows),
                        "values": [_render(row) for row in rows],
                    }
                )
            continue
        examined += 1
        groups = _distinct_values(rows)
        if len(groups) == 1:
            agreements += 1
            cleared += repository.clear_conflict(
                lookup_key, record_state=str(rows[0].record_state or "")
            )
            for row in rows:
                if row.status == KnowledgeStatus.CONFLICTED.value:
                    repository.set_status(
                        str(row.id), status=KnowledgeStatus.ACTIVE.value, note="sources agree"
                    )
                    marked += 1
            continue
        property_name, record_state = _split_key(lookup_key)
        if _sources_state_the_same_set(rows, groups):
            # Every source states the *same set* of values for this property: each of them quotes
            # the same range (a table with a measured depth per row, a bit list, a stand-off that
            # was written twice).  No source says anything the others do not, so there is nothing
            # for a reviewer to choose between - this is ambiguity in how the document was read, and
            # naming it that way is what tells the next person where to look.
            ambiguous += 1
            cleared += repository.clear_conflict(lookup_key, record_state=record_state)
            for row in rows:
                if row.status == KnowledgeStatus.CONFLICTED.value:
                    repository.set_status(
                        str(row.id),
                        status=KnowledgeStatus.ACTIVE.value,
                        note="every source states the same values for this property",
                    )
                    marked += 1
            details.append(
                {
                    "lookup_key": lookup_key,
                    "property": property_name,
                    "same_values_in_every_source": len(rows),
                    "values": [_render(row) for row in rows],
                }
            )
            continue
        conflicts += 1
        candidates = _candidates(repository, rows)
        # Every voice in the argument is marked, not just the minority one: "what is the mud weight
        # here?" has no settled answer, and flagging only the odd value out would imply the
        # majority value *is* the answer - which is choosing a side by counting instead of by
        # deciding.  The tally itself is in the conflict row, where a reviewer can read it.
        for row in rows:
            if row.status == KnowledgeStatus.ACTIVE.value:
                repository.set_status(
                    str(row.id),
                    status=KnowledgeStatus.CONFLICTED.value,
                    note="another source states a different value",
                )
                marked += 1
        repository.record_conflict(
            lookup_key=lookup_key,
            property_name=property_name,
            record_state=record_state,
            well_id=str(next((row.well_id for row in rows if row.well_id), "") or ""),
            compare_unit=_compare_unit(rows),
            candidates=[candidate.to_dict() for candidate in candidates],
            # "sources", not "rows": several statements from one file are one voice saying a thing
            # more than once, and a reviewer deciding how many arguments are open counts sources.
            note=f"{len(groups)} different values stated by {len(versions)} sources",
        )
        details.append(
            {
                "lookup_key": lookup_key,
                "property": property_name,
                "values": [_render(row) for row in rows],
                "ranking": [
                    {
                        "item_id": candidate.item_id,
                        "source": candidate.source,
                        "authority": candidate.authority_tier,
                        "date": candidate.document_date,
                    }
                    for candidate in candidates
                ],
            }
        )
    return ConflictReport(
        keys_examined=examined,
        conflicts=conflicts,
        agreements=agreements,
        items_marked=marked,
        cleared=cleared,
        ambiguous=ambiguous,
        details=tuple(details),
    )


def _eligible(row: KnowledgeItem, *, include_manual: bool) -> bool:
    from ..core.enums import KnowledgeOrigin

    if row.status in {KnowledgeStatus.SUPERSEDED.value, KnowledgeStatus.RETIRED.value}:
        # Neither is a voice in a current argument: one was replaced by a newer revision, the
        # other was judged not to be the answer.  Including retired rows would resurrect the
        # conflict a person just closed, on every re-detection, forever.
        return False
    if row.origin == KnowledgeOrigin.MANUAL.value and not include_manual:
        return False
    return bool(row.predicate)


def _distinct_values(rows: Sequence[KnowledgeItem]) -> list[list[str]]:
    """Group row ids by the value they state, so "how many answers are on the table" is one count."""
    groups: list[list[str]] = []
    seen: list[KnowledgeItem] = []
    for row in rows:
        for group, representative in zip(groups, seen, strict=False):
            if values_agree(representative, row):
                group.append(str(row.id))
                break
        else:
            groups.append([str(row.id)])
            seen.append(row)
    return groups


def _sources_state_the_same_set(
    rows: Sequence[KnowledgeItem], groups: Sequence[Sequence[str]]
) -> bool:
    """Do all the sources state the same *set* of values, so nobody disagrees with anybody?

    ``groups`` comes from :func:`_distinct_values`: the value-classes the rows fall into.  Grouping
    those classes per document version answers the only question that matters here - whether some
    source states a value the others do not.  One version alone cannot disagree with anyone, and the
    caller has already required two versions before asking.
    """
    membership = {str(item_id): index for index, group in enumerate(groups) for item_id in group}
    stated: dict[str, set[int]] = {}
    for row in rows:
        stated.setdefault(str(row.document_version_id or ""), set()).add(membership[str(row.id)])
    if len(stated) < 2:
        return False
    sides = [frozenset(side) for side in stated.values()]
    return all(side == sides[0] for side in sides)


def _split_key(lookup_key: str) -> tuple[str, str]:
    property_name = ""
    record_state = ""
    for part in str(lookup_key or "").split("|"):
        head, _, value = part.partition(":")
        if head == "property":
            property_name = value
        elif head == "state":
            record_state = value
    return property_name or str(lookup_key), record_state


def _compare_unit(rows: Sequence[KnowledgeItem]) -> str:
    for row in rows:
        if row.normalized_unit:
            return str(row.normalized_unit)
    return ""


def _render(row: KnowledgeItem) -> dict[str, Any]:
    return {
        "item_id": str(row.id),
        "value": None if row.value is None else float(row.value),
        "unit": str(row.unit or ""),
        "normalized_value": None if row.normalized_value is None else float(row.normalized_value),
        "normalized_unit": str(row.normalized_unit or ""),
        "original_value": str(row.original_value or ""),
        "text": str(row.content or ""),
        "status": str(row.status or ""),
    }


def resolve_conflict(
    repository: Any,
    conflict_id: str,
    *,
    chosen_item_id: str,
    resolution: str = ConflictResolution.RESOLVED_MANUALLY.value,
    by: str = "operator",
    note: str = "",
) -> KnowledgeConflict:
    """Record a human (or, later, a reviewer workflow) picking a side.

    The chosen fact becomes ``ACTIVE`` and the others ``RETIRED`` - still stored, still
    retrievable, explicitly no longer the platform's answer.  There is no delete path here at all:
    an argument that has been settled is history, and "we decided the program was right about the
    mud weight" is a fact worth keeping next to the values it was decided between.
    """
    conflict = repository.session.get(KnowledgeConflict, conflict_id)
    if conflict is None:
        raise LookupError(f"no conflict {conflict_id!r}")
    candidates = [dict(entry) for entry in (conflict.candidates or [])]
    ids = {str(entry.get("item_id") or "") for entry in candidates}
    if chosen_item_id not in ids:
        raise ValueError(
            f"{chosen_item_id!r} is not one of this conflict's candidates: {sorted(ids)}"
        )
    if resolution not in {member.value for member in ConflictResolution}:
        raise ValueError(f"unknown conflict resolution {resolution!r}")

    for entry in candidates:
        item_id = str(entry.get("item_id") or "")
        if item_id == chosen_item_id:
            repository.set_status(
                item_id, status=KnowledgeStatus.ACTIVE.value, note=f"selected by {by}"
            )
        else:
            repository.set_status(
                item_id, status=KnowledgeStatus.RETIRED.value, note=f"not selected by {by}"
            )
    conflict.status = (
        ConflictResolution.RESOLVED_MANUALLY.value
        if resolution == ConflictResolution.OPEN.value
        else resolution
    )
    conflict.resolution = {
        "chosen_item_id": chosen_item_id,
        "by": by,
        "note": note,
        "at": _now_iso(),
        "candidates_at_resolution": candidates,
    }
    repository.session.flush()
    return conflict


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")
