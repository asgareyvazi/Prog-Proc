"""A fact: one assertion about one entity, with the source wording kept next to it.

``Well A-3 -> mud_weight -> 10.2 ppg`` is a *fact*, and the platform already had the two halves -
extraction produced typed :class:`~drilling_intelligence.core.results.DataField` values with
provenance, and ``knowledge_item`` stored structured objects - but nothing between them.  This
module is that in-between: a small immutable value object whose job is to make four things
impossible.

1. **A source-derived fact without provenance.**  The constructor refuses it; a fact marked
   ``MANUAL``/``DERIVED`` is allowed but carries ``KnowledgeStatus.UNVERIFIED`` unless a source is
   attached, so "nobody can show where this came from" is a queryable state instead of an absence.
2. **Normalisation destroying evidence.**  Every fact keeps three representations side by side:
   what the source wrote (``original_value``/``original_unit``), what it parses to in the unit the
   source used (``value``/``unit``), and what it is in the field's canonical unit
   (``normalized_value``/``normalized_unit``).  A conversion is never the only copy of a number.
3. **A second unit system.**  There is none here: all conversion goes through
   :mod:`drilling_intelligence.core.units`, which is the authority (docs/DECISIONS.md ADR-0002).
   The field default unit for a dimension is the normalisation target, so ``10.2 ppg`` stays
   ``10.2 ppg`` and ``85 degF`` becomes ``29.4444 degC`` - deterministically, at 6 significant
   digits, because :func:`core.units.format_number` decides that and nothing overrides it.
4. **Inventing an engineering vocabulary.**  :data:`PREDICATES` names the assertions the extractors
   can already produce, and any other field becomes a fact under its own name rather than being
   dropped or, worse, mapped onto a nearby predicate that happens to exist.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from ..core.enums import (
    DataQuality,
    KnowledgeItemType,
    KnowledgeOrigin,
    KnowledgeStatus,
    RecordState,
)
from ..core.ids import subject_key
from ..core.provenance import Provenance
from ..core.results import DataField
from ..core.units import Dimension, Quantity, convertible, format_number
from .entities import EntityRef, KnowledgeError, entity_spec

__all__ = [
    "PREDICATES",
    "PREDICATE_BY_FIELD",
    "VALUE_TYPES",
    "KnowledgeFact",
    "PredicateSpec",
    "predicate_for_field",
    "render_value",
]

#: The typed shapes a value can take.  ``ratio`` is a dimensionless number: a YAML mud reading or
#: a rig utilisation is numeric and quotable without a unit, and calling it ``text`` would throw
#: away the comparison a user wants, while calling it ``quantity`` would invent a unit for it.
VALUE_TYPES: tuple[str, ...] = ("quantity", "ratio", "text", "date", "boolean")


@dataclass(frozen=True)
class PredicateSpec:
    """What the platform means by one assertion, and how to compare its values."""

    name: str
    label: str
    #: The dimension a quantity value must have.  ``None`` accepts whatever unit the source gave.
    dimension: Dimension | None = None
    value_type: str = "auto"
    #: Extractor field names that *are* this assertion.  Kept explicit: the DDR says ``mw_out``
    #: and the mud report says ``mud_weight``, and a reader querying one wants both.
    fields: tuple[str, ...] = ()
    #: Whether a document of this kind states an intention rather than an observation, so the
    #: planned and the actual value of the same property never overwrite each other.
    planned_by_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["dimension"] = self.dimension.value if self.dimension else ""
        return payload


def _predicate(
    name: str,
    label: str,
    *,
    dimension: str = "",
    value_type: str = "auto",
    fields: tuple[str, ...] = (),
) -> PredicateSpec:
    return PredicateSpec(
        name=name,
        label=label,
        dimension=Dimension(dimension) if dimension else None,
        value_type=value_type,
        fields=tuple(fields),
    )


#: The drilling assertions this build knows how to compare.  Everything a user reads as "the same
#: property under two spellings" is one predicate here; everything else keeps its field name.
PREDICATES: dict[str, PredicateSpec] = {
    spec.name: spec
    for spec in (
        _predicate(
            "mud_weight",
            "Mud weight",
            dimension="MUD_WEIGHT",
            fields=("mud_weight", "mw", "mw_out", "MW (ppg)"),
        ),
        _predicate("mud_weight_in", "Mud weight in", dimension="MUD_WEIGHT", fields=("mw_in",)),
        _predicate(
            "equivalent_mud_weight",
            "Equivalent mud weight",
            dimension="MUD_WEIGHT",
            fields=("emw", "emw_ppg"),
        ),
        _predicate(
            "fracture_gradient",
            "Fracture gradient",
            dimension="PRESSURE_GRADIENT",
            fields=("frac_gradient", "fg"),
        ),
        _predicate(
            "hole_depth", "Hole depth", dimension="LENGTH", fields=("depth", "hole_depth", "td_md")
        ),
        _predicate(
            "measured_depth", "Measured depth", dimension="LENGTH", fields=("md", "measured_depth")
        ),
        _predicate(
            "true_vertical_depth",
            "True vertical depth",
            dimension="LENGTH",
            fields=("tvd", "true_vertical_depth"),
        ),
        _predicate("casing_size", "Casing size", dimension="LENGTH", fields=("casing_size",)),
        _predicate(
            "hole_section_size",
            "Hole section size",
            dimension="LENGTH",
            fields=("hole_size", "bit_size", "section_size"),
        ),
        _predicate("string", "Pipe body", dimension="LENGTH", fields=("string", "pipe_body")),
        _predicate(
            "mud_volume", "Mud volume", dimension="VOLUME", fields=("mud_volume", "pit_volume")
        ),
        _predicate(
            "flow_rate", "Flow rate", dimension="FLOW_RATE", fields=("flow_rate", "pump_rate")
        ),
        _predicate("rpm", "Rotary speed", dimension="ROTARY_SPEED", fields=("rpm",)),
        _predicate(
            "weight_on_bit", "Weight on bit", dimension="FORCE", fields=("wob", "weight_on_bit")
        ),
        _predicate("torque", "Torque", dimension="TORQUE", fields=("torque",)),
        _predicate(
            "standpipe_pressure",
            "Standpipe pressure",
            dimension="PRESSURE",
            fields=("spp", "standpipe_pressure"),
        ),
        _predicate(
            "temperature",
            "Temperature",
            dimension="TEMPERATURE",
            fields=("temperature", "bhp_temperature"),
        ),
        _predicate(
            "rheometer_reading",
            "Rheometer reading",
            dimension="MUD_WEIGHT",
            fields=("mud_weight_600",),
        ),
        _predicate(
            "viscosity",
            "Viscosity",
            value_type="ratio",
            fields=("visc", "viscosity", "funnel_viscosity"),
        ),
        _predicate(
            "sand_content", "Sand content", value_type="ratio", fields=("sand", "sand_content")
        ),
        _predicate(
            "npt_hours",
            "Non-productive time",
            dimension="TIME",
            fields=("npt", "npt_hours", "lost_time"),
        ),
        _predicate("bit_run", "Bit run", value_type="text", fields=("bit_run",)),
        _predicate(
            "safety_classification",
            "Safety classification",
            value_type="text",
            fields=("classification",),
        ),
        _predicate(
            "report_date",
            "Report date",
            value_type="date",
            fields=("report_date", "date", "well_date"),
        ),
        _predicate(
            "event_count",
            "Event count",
            value_type="ratio",
            fields=("events", "npt_events", "event_count"),
        ),
    )
}


def _field_keys(spec: PredicateSpec) -> tuple[str, ...]:
    keys = {spec.name, *spec.fields}
    return tuple(key.casefold().strip() for key in keys if str(key).strip())


#: Extractor field name -> predicate.  Built once from :data:`PREDICATES` so the two cannot
#: disagree, which is the same discipline that keeps the chunk kinds and the ranking weights in
#: step (see ``tests/unit/test_search_ranking.py``).
PREDICATE_BY_FIELD: dict[str, str] = {
    key: spec.name for spec in PREDICATES.values() for key in _field_keys(spec)
}


#: Words that appear in a spreadsheet header *after* the property it measures.
#:
#: ``Mud weight (ppg)`` and ``Hole size, in`` are the same two properties as ``mud_weight`` and
#: ``hole_size``; a real corpus writes them this way constantly, and treating them as new
#: predicates would split one property across three names.
UNIT_SUFFIX_TOKENS = frozenset(
    {
        "bbl",
        "degc",
        "degf",
        "ft",
        "gpm",
        "h",
        "hr",
        "in",
        "kgm3",
        "kips",
        "lbf",
        "m",
        "min",
        "pct",
        "percent",
        "ppg",
        "psi",
        "rpm",
        "sg",
        "tvd",
        "md",
    }
)

_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*")


def predicate_for_field(name: str) -> tuple[str, PredicateSpec | None]:
    """The predicate for extractor field ``name``, and its spec when there is a registered one.

    An unregistered field keeps its own name (normalised to a snake_case token) instead of being
    dropped: the vocabulary of a real mud report is not closed, and inventing a nearby predicate
    for an unknown value is how wrong data becomes confident data.

    Three lookups happen, in this order, and each is exact rather than fuzzy:

    1. the field name as written, lower-cased (what the extractors emit);
    2. the same with a parenthesised unit removed (``Mud weight (ppg)``);
    3. the longest registered predicate that the name *starts* with, when what follows is a unit
       word (``mud_weight_ppg`` is ``mud_weight``; ``mud_weight_in`` - inlet density, a different
       property - is matched by its own registered name first, because the longest match wins).
    """
    token = str(name or "").strip().casefold()
    if not token:
        raise KnowledgeError("a fact needs a field name to become a predicate", field=name)
    for candidate in (token, _PARENTHETICAL.sub(" ", token).strip()):
        known = PREDICATE_BY_FIELD.get(candidate)
        if known is not None:
            return known, PREDICATES[known]
    snake = _snake_token(_PARENTHETICAL.sub(" ", token).strip())
    known_under_scores = PREDICATE_BY_FIELD.get(snake)
    if known_under_scores is not None:
        return known_under_scores, PREDICATES[known_under_scores]
    best = _longest_unit_suffixed_prefix(snake)
    if best is not None:
        return best, PREDICATES[best]
    return snake, None


def _longest_unit_suffixed_prefix(token: str) -> str | None:
    """``mud_weight_ppg`` -> ``mud_weight``, when the tail is a unit and the head is registered.

    A field alias counts as registered here (``hole_size_in`` reaches ``hole_section_size`` through
    the alias ``hole_size``), because the alias table is where a corpus's actual spellings live.
    Scanning from the longest head down is what keeps a real predicate - ``mud_weight_in``, inlet
    density, a different property from mud weight - from being reduced to a nearby one.
    """
    parts = token.split("_")
    for cut in range(len(parts) - 1, 0, -1):
        head = "_".join(parts[:cut])
        tail = parts[cut:]
        if not all(part in UNIT_SUFFIX_TOKENS for part in tail):
            continue
        if head in PREDICATES:
            return head
        aliased = PREDICATE_BY_FIELD.get(head)
        if aliased is not None:
            return aliased
    return None


def render_value(value: float | None, unit: str, *, text: str = "") -> str:
    """How a value reads to a human: ``10.2 ppg``, ``12 1/4 in``, ``A-3``.

    Numbers are formatted through :func:`core.units.format_number` - the same renderer the
    engineering side uses - so the same value never appears with two different precisions in two
    screens of one product.
    """
    if value is None:
        return text
    return f"{format_number(value)} {unit}".strip()


def _snake_token(value: str) -> str:
    cleaned = "".join(
        character.lower() if character.isalnum() else "_" for character in str(value).strip()
    )
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "unnamed"


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%Y/%m/%d",
)


def _parse_date(text: str) -> _dt.datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(raw)
    except ValueError:
        pass
    for pattern in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(raw, pattern)
        except ValueError:
            continue
    return None


#: Entity types whose row is a hole section, so a fact about one belongs to that section too.
SECTION_SUBJECT_TYPES = frozenset({"section", "hole_section"})


@dataclass(frozen=True)
class KnowledgeFact:
    """One citable assertion.

    Frozen on purpose: a fact that has been written to the database is a record, and a record that
    can be edited in place is how a conflict disappears without anyone deciding to resolve it.
    Changing a fact means writing a new one and superseding the old (see
    :class:`~drilling_intelligence.knowledge.repository.KnowledgeRepository`).
    """

    subject: EntityRef
    predicate: str
    #: The row this fact was read from, when it came out of the database.  Deliberately *not*
    #: part of :meth:`to_dict`: identity belongs to the stored row, not to the assertion, so two
    #: facts derived from the same source compare equal whatever table they came from.
    item_id: str = ""
    value_type: str = "text"
    #: Exactly what the source said, as a string, unit and all.  Never rewritten.
    original_value: str = ""
    original_unit: str = ""
    #: The parsed value in the unit the source used.
    value: float | None = None
    unit: str = ""
    #: The parsed value in the field's canonical unit for its dimension.
    normalized_value: float | None = None
    normalized_unit: str = ""
    #: Text/date/boolean payloads, and the display form of any value.
    text: str = ""
    quality: str = DataQuality.UNVERIFIED.value
    confidence: float | None = None
    #: PLANNED / ACTUAL / ... - ``RecordState``.  A program's number and a report's number are
    #: different facts about the same property, and both survive.
    record_state: str = RecordState.ACTUAL.value
    status: str = KnowledgeStatus.CANDIDATE.value
    origin: str = KnowledgeOrigin.EXTRACTED.value
    valid_from: _dt.datetime | None = None
    valid_to: _dt.datetime | None = None
    provenance: Provenance | None = None
    #: The extraction record this was read from, kept whole (method, note, field name) so the
    #: normalisation can always be argued back to the parser's own output.
    evidence: tuple[Mapping[str, Any], ...] = ()
    method: str = ""
    note: str = ""
    #: Why the *registry* changed the status after the fact was written - a conflict recorded
    #: against it, a revision that replaced it, a side retired by a decision.  Kept apart from
    #: ``note``, which is what the extractor said about the value: the two are asked at different
    #: times by different parties, and letting one overwrite the other loses an explanation.
    status_note: str = ""
    #: The subject's scope, copied out of the ref for the queries that filter on it.
    well_id: str = ""
    section_id: str = ""
    project_id: str = ""
    document_id: str = ""
    document_version_id: str = ""
    #: ``1`` for the newest statement, higher when a stronger source replaces it - mirrors
    #: document versioning rather than inventing its own history.
    revision: int = 1
    superseded_by: str = ""
    #: Which *revision* replaced this statement, recorded by the supersede rule.  Not a fact id: a
    #: revision may drop a property or split it into two, so a row-to-row pointer would be a guess.
    superseded_by_version_id: str = ""

    # -- construction -------------------------------------------------------
    @classmethod
    def from_field(
        cls,
        payload: Mapping[str, Any] | DataField,
        *,
        subject: EntityRef,
        origin: str = KnowledgeOrigin.EXTRACTED.value,
        record_state: str | None = None,
        document_id: str = "",
        document_version_id: str = "",
        project_id: str = "",
        valid_from: _dt.datetime | None = None,
        valid_to: _dt.datetime | None = None,
        require_provenance: bool = True,
    ) -> KnowledgeFact:
        """Build a fact from one entry of ``extracted_fields`` in a stored artefact.

        The mapping - not the live extractor - is the input, so a rebuild reads what was recorded
        and gets the same answer years later, and so re-deriving knowledge never re-runs a
        parser.
        """
        data = _field_mapping(payload)
        name = str(data.get("name") or "").strip()
        predicate, spec = predicate_for_field(name)
        evidence = {key: value for key, value in data.items() if key != "provenance"}
        provenance = _provenance_of(data)
        if provenance is None and require_provenance and origin == KnowledgeOrigin.EXTRACTED.value:
            raise KnowledgeError(
                f"the extracted field {name!r} carries no provenance, so it cannot become an engineering fact",
                hint="the extractor lost the location for this value; re-extract the document rather than accepting it",
                field=name,
                subject=subject.key(),
            )
        quality = str(data.get("quality") or DataQuality.UNVERIFIED.value).upper()
        confidence = _as_float(data.get("confidence"))
        text_value = _raw_text(data.get("value"))
        unit_hint = str(data.get("unit") or "").strip()

        fact = cls(
            subject=subject,
            predicate=predicate,
            value_type="text",
            original_value=text_value,
            original_unit=_unit_of(text_value, unit_hint),
            text=text_value,
            quality=quality,
            confidence=confidence,
            record_state=record_state
            or (
                RecordState.PLANNED.value
                if spec and spec.planned_by_default
                else RecordState.ACTUAL.value
            ),
            origin=origin,
            valid_from=valid_from,
            valid_to=valid_to,
            provenance=provenance,
            evidence=(evidence,),
            method=str(data.get("method") or ""),
            note=str(data.get("note") or ""),
            document_id=document_id,
            document_version_id=document_version_id,
            project_id=project_id,
        )
        typed = fact._typed(spec)
        return replace(typed, status=typed._initial_status(spec))

    def _typed(self, spec: PredicateSpec | None) -> KnowledgeFact:
        """Assign the value type and the numeric pair, using only ``core.units``.

        A value that refuses to be typed stays a text fact with a note saying so.  That is the
        deliberate failure direction: dropping it would lose the fact, and forcing it into a unit
        would fabricate one.
        """
        want = (spec.value_type if spec else "auto") or "auto"
        raw_value = self.value_from_original()
        expected = spec.dimension if spec else None

        if want == "date" or (
            want == "auto" and not _is_number(raw_value) and _parse_date(raw_value) is not None
        ):
            moment = _parse_date(raw_value)
            if moment is None:
                # A date-shaped value that will not parse stays a text fact with the reason: the
                # date is still evidence of what the document said.
                return replace(
                    self,
                    value_type="text",
                    note=_append(self.note, "a date was expected but the value would not parse"),
                )
            # ``valid_from`` carries the sortable moment; no epoch number is invented as a "unit",
            # because ``core.units`` has no unit for it and a fake one would be exactly the sort
            # of confident nonsense this layer exists to prevent.
            return replace(
                self,
                value_type="date",
                text=moment.date().isoformat(),
                valid_from=self.valid_from or moment,
            )
        if want == "boolean" or (
            want == "auto" and raw_value.strip().lower() in {"true", "false", "yes", "no"}
        ):
            truth = raw_value.strip().lower() in {"true", "yes"}
            return replace(
                self,
                value_type="boolean",
                text="true" if truth else "false",
                value=1.0 if truth else 0.0,
                normalized_value=1.0 if truth else 0.0,
                normalized_unit="bool",
            )

        if expected is not None and not raw_value:
            return replace(
                self,
                value_type="text",
                note=_append(
                    self.note,
                    f"{self.predicate} needs a number, the source gave {self.original_value!r}",
                ),
            )

        try:
            quantity = Quantity.parse(raw_value, self.original_unit or None)
        except Exception as exc:  # noqa: BLE001 - any unit/refusal becomes a text fact, recorded
            if expected is not None or want == "quantity":
                return replace(
                    self,
                    value_type="text",
                    note=_append(
                        self.note, f"not interpretable as a value ({type(exc).__name__}: {exc})"
                    ),
                )
            return replace(
                self,
                value_type="ratio" if _is_number(raw_value) else "text",
                value=_as_float(raw_value) if _is_number(raw_value) else None,
                text=raw_value,
                normalized_value=_as_float(raw_value) if _is_number(raw_value) else None,
            )

        if expected is not None and not convertible(quantity.dimension, expected):
            return replace(
                self,
                value_type="text",
                note=_append(
                    self.note,
                    f"unit {quantity.unit.symbol!r} is {quantity.dimension.value}, not the {expected.value} this predicate expects",
                ),
            )
        canonical = quantity.as_dimension(expected or quantity.dimension)
        return replace(
            self,
            value_type="quantity",
            value=quantity.value,
            unit=quantity.unit.symbol,
            normalized_value=canonical.value,
            normalized_unit=canonical.unit.symbol,
            text=render_value(quantity.value, quantity.unit.symbol),
        )

    def _initial_status(self, spec: PredicateSpec | None) -> str:
        """ACTIVE, UNVERIFIED or CANDIDATE - decided by the evidence, never by optimism.

        An ``UNVERIFIED``/``INFERRED``/``CONFLICT`` field quality is the extractor's own admission
        that it read a value without confirming it (a cell whose formula had no cached result, a
        phrase matched by a heuristic, two readings of one cell), and a fact inherits that.  Conflict
        detection still compares them; they just never get presented as settled.
        """
        if self.origin != KnowledgeOrigin.EXTRACTED.value and self.provenance is None:
            return KnowledgeStatus.UNVERIFIED.value
        if self.provenance is None:
            return KnowledgeStatus.UNVERIFIED.value
        if not self.original_value.strip():
            # A field the extractor found but could not read a value out of: the location is real,
            # the number is not, and "ACTIVE" would let a search result present it as an answer.
            return KnowledgeStatus.CANDIDATE.value
        if self.quality == DataQuality.INVALID.value:
            return KnowledgeStatus.CANDIDATE.value
        if self.quality != DataQuality.VALID.value:
            # Anything the extractor did not call a clean read is doubt it wrote down: an inferred
            # phrase, an unverified formula cell, a field whose two readings conflicted
            # (``CONFLICT``), or a quality token from an artefact this version of the platform does
            # not know.  ``ACTIVE`` is reserved for the case where the extractor said the value is
            # good - which is the whole reason a user can act on it.
            return KnowledgeStatus.UNVERIFIED.value
        return KnowledgeStatus.ACTIVE.value

    def value_from_original(self) -> str:
        """The source text with the value alone in it, for the parser to work on."""
        return str(self.original_value or "").strip()

    # -- keys ---------------------------------------------------------------
    def lookup_key(self) -> str:
        """The canonical grouping key: same subject + predicate + record state = same question.

        Built from :func:`core.ids.subject_key`, which is the format the registry has always used
        for "which statements are about this exact thing" - values are *not* part of the key, so
        two sources that disagree collide here instead of silently coexisting as unrelated rows.
        """
        parts = [
            subject_key(
                well_id=self.resolved_well_id,
                section_id=self.resolved_section_id,
                property_name=self.predicate,
                record_state=self.record_state,
            )
        ]
        if not self.resolved_well_id and not self.resolved_section_id:
            parts.insert(0, self.subject.key())
        if self.project_id:
            parts.append(f"project:{self.project_id}")
        return "|".join(part for part in parts if part)

    @property
    def resolved_well_id(self) -> str:
        """The well this fact belongs to: set explicitly, or inherited from the subject.

        One rule for both the grouping key and the column, because the two disagreeing is how a
        manual note about a well ends up invisible to the well query that should have found it -
        and how a conflict gets missed.
        """
        return self.well_id or (
            self.subject.entity_id if self.subject.entity_type == "well" else ""
        )

    @property
    def resolved_section_id(self) -> str:
        """The hole section a fact belongs to, on the same rule (:data:`SECTION_SUBJECT_TYPES`)."""
        return self.section_id or (
            self.subject.entity_id if self.subject.entity_type in SECTION_SUBJECT_TYPES else ""
        )

    @property
    def is_source_derived(self) -> bool:
        return self.provenance is not None

    @property
    def comparable_value(self) -> float | None:
        """The number conflicts are judged on: the normalised value when there is one."""
        if self.normalized_value is not None:
            return self.normalized_value
        return self.value

    def citation(self) -> str:
        """Where this came from, in one line, without pretending to be a page number."""
        if self.provenance is None:
            return "no recorded source"
        locator = (
            self.provenance.locator.ref() if getattr(self.provenance.locator, "kind", "") else ""
        )
        filename = self.provenance.filename or "source"
        return f"{filename} > {locator}" if locator else filename

    # -- persistence --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        payload = {
            "subject": self.subject.to_dict(),
            "predicate": self.predicate,
            "value_type": self.value_type,
            "original_value": self.original_value,
            "original_unit": self.original_unit,
            "value": self.value,
            "unit": self.unit,
            "normalized_value": self.normalized_value,
            "normalized_unit": self.normalized_unit,
            "text": self.text,
            "quality": self.quality,
            "confidence": self.confidence,
            "record_state": self.record_state,
            "status": self.status,
            "origin": self.origin,
            "valid_from": _iso(self.valid_from),
            "valid_to": _iso(self.valid_to),
            "provenance": self.provenance.to_dict() if self.provenance is not None else None,
            "evidence": [dict(entry) for entry in self.evidence],
            "method": self.method,
            "note": self.note,
            "status_note": self.status_note,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "well_id": self.well_id,
            "section_id": self.section_id,
            "project_id": self.project_id,
            "revision": self.revision,
            "superseded_by": self.superseded_by,
            "superseded_by_version_id": self.superseded_by_version_id,
        }
        return payload

    def item_payload(self) -> dict[str, Any]:
        """The full fact as it is stored in ``knowledge_item.payload``.

        Everything is in here, including the fields that have no column, because the columns are
        the *index* of a fact and the payload is the fact: a rebuild from a stored item alone has
        to reproduce it exactly.
        """
        return self.to_dict()

    def to_item(self, *, item_id: str = "", source_id: str = "") -> dict[str, Any]:
        """Keyword arguments for a ``knowledge_item`` row.

        Returning kwargs instead of writing the row itself keeps the ORM out of this module: the
        fact is a value object, and the repository decides how it lands.
        """
        from ..core.ids import new_id

        spec = entity_spec(self.subject.entity_type)
        title = f"{self.subject.label or self.subject.key()} · {PREDICATES[self.predicate].label if self.predicate in PREDICATES else self.predicate.replace('_', ' ')} = {self.text}"[
            :400
        ]
        return {
            "id": item_id or new_id("ki"),
            "item_type": _item_type_for(self.predicate),
            "title": title,
            "content": self.text or self.original_value,
            "domain": spec.name,
            "payload": self.item_payload(),
            "lookup_key": self.lookup_key()[:300],
            "value": self.value,
            "unit": self.unit,
            "normalized_value": self.normalized_value,
            "normalized_unit": self.normalized_unit,
            "entity_type": self.subject.entity_type,
            "entity_id": self.subject.entity_id,
            "predicate": self.predicate,
            "value_type": self.value_type,
            "original_value": self.original_value,
            "original_unit": self.original_unit,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "origin": self.origin,
            "record_state": self.record_state,
            "status": self.status,
            "confidence": self.confidence,
            "revision": self.revision,
            "well_id": self.resolved_well_id or None,
            "section_id": self.resolved_section_id or None,
            "project_id": self.project_id or None,
            "document_id": self.document_id or None,
            "document_version_id": self.document_version_id or None,
            "source_id": source_id or None,
            "provenance": [self.provenance.to_dict()] if self.provenance is not None else [],
            "evidence": [dict(entry) for entry in self.evidence],
            "superseded_by": self.superseded_by or None,
            "created_by": "knowledge",
            "assumptions": [self.note] if self.note else [],
            "applicability": f"entity_type={self.subject.entity_type}",
        }

    @classmethod
    def from_item(cls, row: Any) -> KnowledgeFact:
        """Rebuild the fact a ``knowledge_item`` row stores.

        What the source said is read from the payload; what the registry has decided since is read
        from the columns (see :func:`column` inside), so a fact that has been marked, superseded or
        retired reports the state it is actually in rather than the state it was written in.
        """
        payload = dict(getattr(row, "payload", None) or {})

        def column(name: str, fallback: Any = "") -> Any:
            """A field the registry owns: the column is the answer, the payload is history.

            ``set_status`` and ``supersede_previous_versions`` write columns, not the payload - the
            payload is what the source said, kept immutable so a rebuild reproduces it exactly.
            Reading the payload first here would print a fact as ``ACTIVE`` after a conflict had
            been recorded against it, and would offer the retired side of a settled argument as the
            answer.
            """
            value = getattr(row, name, None)
            if value in (None, ""):
                return payload.get(name, fallback)
            return value

        subject = EntityRef.from_dict(
            payload.get("subject")
            or {
                "entity_type": getattr(row, "entity_type", "") or "engineering_fact",
                "entity_id": getattr(row, "entity_id", "") or "",
                "label": getattr(row, "title", ""),
            }
        )
        recorded = list(getattr(row, "provenance", None) or [])
        provenance_payload = payload.get("provenance") or (recorded[0] if recorded else None)
        return cls(
            subject=subject,
            item_id=str(getattr(row, "id", "") or ""),
            predicate=str(
                payload.get("predicate") or getattr(row, "predicate", "") or "unspecified"
            ),
            value_type=str(payload.get("value_type") or getattr(row, "value_type", "") or "text"),
            original_value=str(
                payload.get("original_value")
                if payload.get("original_value") is not None
                else (getattr(row, "original_value", "") or "")
            ),
            original_unit=str(
                payload.get("original_unit") or getattr(row, "original_unit", "") or ""
            ),
            value=_as_float(payload.get("value", getattr(row, "value", None))),
            unit=str(payload.get("unit") or getattr(row, "unit", "") or ""),
            normalized_value=_as_float(
                payload.get("normalized_value", getattr(row, "normalized_value", None))
            ),
            normalized_unit=str(
                payload.get("normalized_unit") or getattr(row, "normalized_unit", "") or ""
            ),
            text=str(payload.get("text") or getattr(row, "content", "") or ""),
            quality=str(payload.get("quality") or DataQuality.UNVERIFIED.value),
            confidence=_as_float(payload.get("confidence", getattr(row, "confidence", None))),
            record_state=str(
                payload.get("record_state")
                or getattr(row, "record_state", "")
                or RecordState.ACTUAL.value
            ),
            status=str(column("status") or KnowledgeStatus.CANDIDATE.value),
            origin=str(column("origin") or KnowledgeOrigin.EXTRACTED.value),
            valid_from=_parse_date(str(payload.get("valid_from") or ""))
            or getattr(row, "valid_from", None),
            valid_to=_parse_date(str(payload.get("valid_to") or ""))
            or getattr(row, "valid_to", None),
            provenance=Provenance.from_dict(dict(provenance_payload))
            if provenance_payload
            else None,
            evidence=tuple(
                dict(entry)
                for entry in payload.get("evidence") or getattr(row, "evidence", None) or ()
            ),
            method=str(payload.get("method") or ""),
            note=str(payload.get("note") or ""),
            document_id=str(column("document_id")),
            document_version_id=str(column("document_version_id")),
            well_id=str(column("well_id")),
            section_id=str(column("section_id")),
            project_id=str(column("project_id")),
            revision=int(column("revision", 1) or 1),
            superseded_by=str(column("superseded_by")),
            superseded_by_version_id=str(payload.get("superseded_by_version_id") or ""),
            # The registry's own explanation is not a column, so it travels in the payload, which is
            # where ``set_status`` appends it.
            status_note=str(payload.get("status_note") or ""),
        )

    # -- search -------------------------------------------------------------
    def search_text(self) -> str:
        """The sentence a query runs against.

        A fact must be findable by the property's English name as well as by its token: "mud
        weight" is what a user types, ``mud_weight`` is what the row is keyed on, and the number
        has to appear with its unit because the unit is what makes the number answerable.
        """
        label = (
            PREDICATES[self.predicate].label
            if self.predicate in PREDICATES
            else self.predicate.replace("_", " ")
        )
        parts = [
            label,
            f"{self.subject.label or self.subject.entity_id} {self.predicate}".strip(),
            f"{self.text}".strip(),
        ]
        if self.original_value and self.original_value != self.text:
            parts.append(f"source wording: {self.original_value}")
        if self.unit and self.unit != self.normalized_unit:
            parts.append(f"as {self.normalized_value_text()} {self.normalized_unit}")
        return "  ".join(part for part in parts if part)

    def normalized_value_text(self) -> str:
        return format_number(self.normalized_value) if self.normalized_value is not None else ""

    def status_reason(self) -> str:
        """Why this fact has the status it has - shown wherever the status is."""
        if self.status == KnowledgeStatus.UNVERIFIED.value:
            return self.note or "the extractor recorded this value without verifying it"
        if self.status == KnowledgeStatus.CONFLICTED.value:
            return (
                self.status_note
                or "at least one other source states a different value for the same property"
            )
        if self.status == KnowledgeStatus.SUPERSEDED.value:
            if self.status_note:
                return self.status_note
            if self.superseded_by_version_id:
                return f"a later revision of the same source states otherwise ({self.superseded_by_version_id})"
            return "a newer revision of the same source states otherwise"
        if self.status == KnowledgeStatus.RETIRED.value:
            # The status a person caused carries the most interesting reason, and a UI that shows
            # "RETIRED" without "not selected by X" reads like a data-quality problem.
            return self.status_note or "a reviewer settled the argument the other way"
        if self.status == KnowledgeStatus.CANDIDATE.value:
            return self.note or "read from the source but not accepted as usable"
        return ""


# --------------------------------------------------------------------------- helpers
def _item_type_for(predicate: str) -> str:
    """Which ``KnowledgeItemType`` a fact is filed under."""
    if predicate in {"npt_hours", "event_count"}:
        return KnowledgeItemType.OBSERVATION.value
    if predicate.endswith("_size"):
        return KnowledgeItemType.SPECIFICATION.value
    if predicate in {"safety_classification", "lesson"}:
        return KnowledgeItemType.LESSON.value
    return (
        KnowledgeItemType.CONSTANT.value
        if predicate in PREDICATES
        else KnowledgeItemType.OBSERVATION.value
    )


def _field_mapping(payload: Mapping[str, Any] | DataField) -> dict[str, Any]:
    if isinstance(payload, DataField):
        return {
            "name": payload.name,
            "value": payload.value,
            "unit": payload.unit,
            "dimension": payload.dimension,
            "quality": payload.quality.value
            if isinstance(payload.quality, DataQuality)
            else str(payload.quality),
            "confidence": payload.confidence,
            "method": payload.method,
            "note": payload.note,
            "provenance": payload.provenance.to_dict() if payload.provenance is not None else None,
        }
    return dict(payload)


def _provenance_of(data: Mapping[str, Any]) -> Provenance | None:
    raw = data.get("provenance")
    if not raw:
        return None
    if isinstance(raw, Provenance):
        return raw
    try:
        return Provenance.from_dict(dict(raw))
    except Exception as exc:
        raise KnowledgeError(
            f"the recorded provenance cannot be read: {exc}", hint="re-extract the document"
        ) from exc


def _raw_text(value: Any) -> str:
    """The value as one plain string, without deciding anything about it.

    ``str`` is a :class:`~collections.abc.Sequence`, so the sequence branch has to come after the
    string branch - a parser that checked them in the other order returned "" for every text field
    it was given, which quietly turned every well name and bit run into an empty fact.  A real
    sequence (a value read out of several cells) is joined with ``" | "`` rather than dropped: the
    source wrote all of it, and the fact keeps all of it.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return format_number(float(value))
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return " | ".join(f"{key}={_raw_text(item)}" for key, item in value.items())
    if isinstance(value, Sequence):
        return " | ".join(_raw_text(item) for item in value if _raw_text(item))
    return str(value).strip()


def _unit_of(text: str, hint: str) -> str:
    """The unit the source itself wrote.

    The extractor's ``unit`` field is sometimes empty while the value string carries the unit
    (``"10.2 ppg"``), so the unit is read off the text when the hint is missing - but only as a
    trailing token, never by stripping digits off the front, which is how ``"12 1/4 in"`` would
    lose its fraction.
    """
    if hint:
        return str(hint).strip()
    parts = str(text or "").rsplit(" ", 1)
    if (
        len(parts) == 2
        and parts[1]
        and not _is_number(parts[1])
        and any(character.isalpha() for character in parts[1])
    ):
        return parts[1].strip()
    return ""


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _is_number(value: Any) -> bool:
    return _as_float(value) is not None and bool(
        re.fullmatch(r"[-+]?[\d.,]+([eE][-+]?\d+)?", str(value).strip())
    )


def _append(note: str, extra: str) -> str:
    return f"{note}; {extra}" if note else extra


def _iso(value: _dt.datetime | None) -> str:
    return value.isoformat() if isinstance(value, _dt.datetime) else ""
