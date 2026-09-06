"""The entity registry: what a knowledge fact can be *about*.

The platform already stores wells, documents, versions and sections in their own tables.  What it
did not have is a way to say "this value belongs to the 12 1/4\" hole section of Well A-3" without
deciding, up front, whether a hole section gets a table.  This module answers that with a registry
rather than a schema: an entity type names a label, the table that backs it (possibly
``knowledge_item``, which is what an entity type gets *before* it has a model of its own), and the
endpoint token a ``knowledge_relation`` edge uses to point at it.

Nothing here pretends to be an engineering model.  ``Bit`` and ``BHA`` are addressable, linkable
and citable; they are not yet described by fields, and that is the point - a later phase adds the
fields to a type without changing how facts or edges refer to it.

Two rules make the registry safe:

1. a type with a backing table must also be an endpoint the integrity layer accepts
   (:data:`drilling_intelligence.database.integrity.RELATION_ENDPOINT_MODELS`), checked at import
   and by a test, so "you can store it" and "you can link it" can never disagree; and
2. an entity that has no row is *rejected*, not quietly created - except through
   :func:`ensure_entity`, which creates a placeholder item that is explicitly marked as
   extracted-but-unattributed rather than invented detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ..core.enums import DocumentClassification, KnowledgeItemType, KnowledgeOrigin, KnowledgeStatus
from ..core.errors import DrillingIntelligenceError
from ..database.integrity import RELATION_ENDPOINT_MODELS
from ..database.models import KnowledgeItem


class KnowledgeError(DrillingIntelligenceError):
    """A knowledge object that cannot be written because it would not be trustworthy."""

    code = "KNOWLEDGE"


@dataclass(frozen=True)
class EntitySpec:
    """One addressable thing a fact can belong to."""

    #: Registry token, snake_case: used in ``EntityRef.entity_type`` and in stored rows.
    name: str
    #: What a user reads in a table header or a citation.
    label: str
    #: The table whose rows this entity *is*.  ``None`` means it has no table yet and is stored
    #: as a ``knowledge_item`` row of the matching ``item_type``.
    table: str | None
    #: The endpoint token a relation edge uses for this entity: its own table when it has one,
    #: ``knowledge_item`` when it is represented by one.
    endpoint_type: str
    #: The item_type used when the entity has no table of its own ("" when it does).
    item_type: str = ""
    #: Document classifications whose facts are primarily *about* this entity.  This is what lets
    #: an extractor attribute a value without a human mapping every document type by hand.
    described_by: tuple[str, ...] = ()

    @property
    def has_table(self) -> bool:
        return bool(self.table)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "table": self.table,
            "endpoint_type": self.endpoint_type,
            "item_type": self.item_type,
            "described_by": list(self.described_by),
        }


def _entity(
    name: str,
    label: str,
    *,
    table: str | None = None,
    item_type: KnowledgeItemType | None = None,
    described_by: tuple[DocumentClassification, ...] = (),
) -> EntitySpec:
    """Register an entity type, deriving the parts that must not be typed by hand."""
    endpoint = table or "knowledge_item"
    return EntitySpec(
        name=name,
        label=label,
        table=table,
        endpoint_type=endpoint,
        item_type="" if table else (item_type.value if item_type else "CONCEPT"),
        described_by=tuple(entry.value for entry in described_by),
    )


#: The Phase-0 vocabulary.  Everything the brief lists as "must be supported", in one table, so
#: adding an entity type is one line plus (later) the fields that describe it.
ENTITY_TYPES: dict[str, EntitySpec] = {
    spec.name: spec
    for spec in (
        _entity("company", "Company", table="company"),
        _entity("project", "Project", table="project"),
        _entity("well", "Well", table="well"),
        _entity(
            "document",
            "Document",
            table="document",
            described_by=(
                DocumentClassification.PROCEDURE,
                DocumentClassification.STANDARD,
                DocumentClassification.BOOK,
            ),
        ),
        _entity("document_version", "Document version", table="document_version"),
        _entity(
            "section",
            "Hole section",
            table="well_section",
            # No ``described_by``: a program or a DDR is not *about* a section row - sections hang
            # off the well, and a report's own measurements arrive as parameters.  Claiming the
            # classification here would fight with ``drilling_parameter`` over what every fact of
            # that kind is about.
            described_by=(),
        ),
        _entity("formation", "Formation", item_type=KnowledgeItemType.CONCEPT),
        # ``hole_section`` is the drilling word for the same row ``section`` names; two tokens,
        # one table, because a reader searching for either must find the same facts.
        _entity("hole_section", "Hole section", table="well_section"),
        _entity(
            "bha",
            "BHA assembly",
            item_type=KnowledgeItemType.EQUIPMENT,
            described_by=(DocumentClassification.BHA_REPORT,),
        ),
        _entity(
            "bit",
            "Drill bit",
            item_type=KnowledgeItemType.EQUIPMENT,
            described_by=(DocumentClassification.BIT_RECORD,),
        ),
        _entity(
            "mud",
            "Mud system",
            item_type=KnowledgeItemType.CONCEPT,
            described_by=(DocumentClassification.MUD_REPORT,),
        ),
        _entity(
            "casing",
            "Casing string",
            item_type=KnowledgeItemType.CONCEPT,
            described_by=(DocumentClassification.CASING_REPORT,),
        ),
        _entity(
            "cement",
            "Cement job",
            item_type=KnowledgeItemType.CONCEPT,
            described_by=(DocumentClassification.CEMENT_REPORT,),
        ),
        _entity(
            "trajectory",
            "Well trajectory",
            item_type=KnowledgeItemType.CONCEPT,
            described_by=(DocumentClassification.DIRECTIONAL_SURVEY,),
        ),
        _entity(
            "survey",
            "Directional survey",
            item_type=KnowledgeItemType.OBSERVATION,
            # A survey report *is* trajectory data, so the classification belongs to
            # ``trajectory``; the survey entity stays linkable as a set of measurements.
            described_by=(),
        ),
        _entity(
            "drilling_parameter",
            "Drilling parameter",
            item_type=KnowledgeItemType.VARIABLE,
            described_by=(
                DocumentClassification.DDR,
                DocumentClassification.DRILLING_PROGRAM,
            ),
        ),
        _entity(
            "npt_event",
            "NPT event",
            item_type=KnowledgeItemType.EVENT,
            described_by=(DocumentClassification.NPT, DocumentClassification.TIME_BREAKDOWN),
        ),
        _entity(
            "service",
            "Service job",
            item_type=KnowledgeItemType.PROCEDURE,
            described_by=(DocumentClassification.SERVICE_REPORT,),
        ),
        _entity("rig", "Rig", item_type=KnowledgeItemType.EQUIPMENT),
        _entity("equipment", "Equipment", item_type=KnowledgeItemType.EQUIPMENT),
        _entity(
            "safety_event",
            "Safety event",
            item_type=KnowledgeItemType.EVENT,
            described_by=(DocumentClassification.HSE, DocumentClassification.WELL_CONTROL),
        ),
        _entity(
            "problem",
            "Problem",
            item_type=KnowledgeItemType.OBSERVATION,
            # Neither NPT codes nor well-control reports are *about* a "problem": the problem is
            # something a report describes (and ``DOCUMENT_MENTIONS_*`` edges carry that), while
            # the classification itself belongs to the event type it names.
            described_by=(),
        ),
        _entity(
            "lesson_learned",
            "Lesson learned",
            item_type=KnowledgeItemType.LESSON,
            described_by=(DocumentClassification.LESSON_LEARNED,),
        ),
        _entity("engineering_fact", "Engineering fact", item_type=KnowledgeItemType.CONSTANT),
    )
}

#: Aliases a reader or an older row may use for a registered name.
ENTITY_ALIASES: dict[str, str] = {
    "well_section": "section",
    "holesection": "hole_section",
    "npt": "npt_event",
    "bit_run": "bit",
    "mud_system": "mud",
    "safety": "safety_event",
    "lesson": "lesson_learned",
    "wbha": "bha",
}


#: The reverse of :attr:`EntitySpec.described_by`, built once: classification -> the entity its
#: facts are about.  A report about the well's mud system still yields *well* facts, because that
#: is the subject a reader queries by; the mud system itself becomes the object of the value.
def _build_subject_table() -> dict[str, str]:
    """``classification -> the entity type its documents describe``, with no room for a tie.

    The table is built from each :class:`EntitySpec`'s ``described_by`` rather than maintained
    alongside it, and a classification claimed twice is an error at import.  That is not
    fastidiousness: with two claimants, whichever dict entry was written last would decide what
    every fact of that kind is about, and the choice would be invisible in the code and in the
    data.
    """
    table: dict[str, str] = {}
    for name, spec in ENTITY_TYPES.items():
        for classification in spec.described_by:
            claimed = table.get(classification)
            if claimed is not None and claimed != name:
                raise KnowledgeError(
                    f"entity types {claimed!r} and {name!r} both claim classification {classification!r}",
                    hint="a classification must describe one entity type; drop the weaker claim",
                )
            table[classification] = name
    return table


_SUBJECT_BY_CLASSIFICATION: dict[str, str] = _build_subject_table()


def normalise_entity_type(value: str) -> str:
    """The registered name for ``value``, or a :class:`KnowledgeError`.

    Case and separator tolerant (``HoleSection``, ``hole-section`` and ``hole_section`` are the
    same entity), because the tokens arrive from JSON payloads and from hand-written configs as
    often as from code.
    """
    token = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if not token:
        raise KnowledgeError("an entity type must not be empty", value=value)
    if token in ENTITY_TYPES:
        return token
    if token in ENTITY_ALIASES:
        return ENTITY_ALIASES[token]
    raise KnowledgeError(f"unknown entity type {value!r}", known=sorted(ENTITY_TYPES))


def entity_spec(value: str) -> EntitySpec:
    """The :class:`EntitySpec` behind any accepted spelling of an entity type."""
    return ENTITY_TYPES[normalise_entity_type(value)]


def subject_type_for_classification(classification: str | None) -> str:
    """What a document of ``classification`` is a report *about*.

    Falls back to ``document_version``: an unattributable value is still a real value with a
    source, and saying "this is what revision 3 of this file states" is honest while "this is the
    well's mud weight" would not be.
    """
    token = str(classification or "").strip().upper()
    return _SUBJECT_BY_CLASSIFICATION.get(token, "document_version")


# --------------------------------------------------------------------------- references
@dataclass(frozen=True)
class EntityRef:
    """A typed pointer: ``("well", "well-1f4a…")``, plus the label a UI shows.

    The label is carried rather than always looked up because a fact keeps citing "Well A-3"
    after the row is renamed or deleted - the citation is about what was read, not about the
    current state of a lookup table.
    """

    entity_type: str
    entity_id: str
    label: str = ""

    def __post_init__(self) -> None:
        # Validation in the constructor, not at the write path: a half-formed ref that can sit in
        # a dataclass is a half-formed ref that reaches the database.
        object.__setattr__(self, "entity_type", normalise_entity_type(self.entity_type))
        if not str(self.entity_id or "").strip():
            raise KnowledgeError(
                f"a {self.entity_type} reference needs an id", entity_type=self.entity_type
            )
        object.__setattr__(self, "entity_id", str(self.entity_id).strip())

    @property
    def endpoint_type(self) -> str:
        """What a ``knowledge_relation`` names this by."""
        return entity_spec(self.entity_type).endpoint_type

    def key(self) -> str:
        """The canonical short form used in lookup keys and citations."""
        return f"{self.entity_type}:{self.entity_id}"

    def to_dict(self) -> dict[str, str]:
        return {"entity_type": self.entity_type, "entity_id": self.entity_id, "label": self.label}

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> EntityRef:
        data = dict(payload or {})
        return cls(
            entity_type=str(data.get("entity_type") or data.get("type") or ""),
            entity_id=str(data.get("entity_id") or data.get("id") or ""),
            label=str(data.get("label") or ""),
        )

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{entity_spec(self.entity_type).label} {self.label or self.entity_id}"


def ref_for_row(row: Any) -> EntityRef:
    """The reference to an ORM row the registry knows about.

    Derived from the mapped table name, so it cannot drift from the model: a new entity type
    whose table is registered gets references for free.
    """
    if row is None:
        raise KnowledgeError("cannot build an entity reference from nothing")
    table = getattr(type(row), "__tablename__", "")
    if table == "knowledge_item":
        # An item-backed entity type is decided by the row, not guessed from its table: a
        # placeholder for a bit and a lesson learned are both knowledge_item rows.
        return EntityRef(_item_entity_type(row), str(row.id), label=_row_label(row))
    for spec in ENTITY_TYPES.values():
        if spec.table == table:
            return EntityRef(spec.name, str(row.id), label=_row_label(row))
    raise KnowledgeError(f"the registry has no entity type for a row of table {table!r}")


def _row_label(row: Any) -> str:
    for attribute in ("name", "title", "filename", "identity_path", "id"):
        value = getattr(row, attribute, None)
        if value:
            return str(value)
    return ""


def _item_entity_type(row: Any) -> str:
    """Which entity a ``knowledge_item`` row stands for.

    An item carries the entity type it was created as in ``payload['entity_type']``; when it does
    not (a row from before this layer existed) its ``item_type`` is the best answer available.
    """
    payload = dict(getattr(row, "payload", None) or {})
    token = str(payload.get("entity_type") or getattr(row, "item_type", "") or "").strip()
    if token:
        try:
            return normalise_entity_type(token)
        except KnowledgeError:
            return "engineering_fact"
    return "engineering_fact"


def resolve(session: Any, ref: EntityRef) -> Any | None:
    """The row ``ref`` points at, or ``None`` when the pointer is dangling.

    Never raises: the caller deciding whether a missing row is an error is a better split than a
    lookup function with an opinion.  :func:`require` is the raising form.
    """
    spec = entity_spec(ref.entity_type)
    if spec.table:
        model = RELATION_ENDPOINT_MODELS.get(spec.table)
        return session.get(model, ref.entity_id) if model is not None else None
    return session.get(KnowledgeItem, ref.entity_id)


def require(session: Any, ref: EntityRef) -> Any:
    """The row ``ref`` points at, or a :class:`KnowledgeError` naming the dangling reference."""
    row = resolve(session, ref)
    if row is None:
        raise KnowledgeError(
            f"{entity_spec(ref.entity_type).label} {ref.entity_id!r} does not exist",
            hint="link a fact only to an entity that is registered; `drillintel knowledge status` lists what is",
            entity_type=ref.entity_type,
            entity_id=ref.entity_id,
        )
    return row


def find_well_ref(session: Any, name_or_id: str) -> EntityRef | None:
    """Resolve ``--well A-3`` (name) or a well id into a reference."""
    from ..wells.repository import WellRepository

    repository = WellRepository(session)
    for token in (token.strip() for token in str(name_or_id or "").split(",") if token.strip()):
        well = repository.find_well(token) or repository.get_well(token)
        if well is not None:
            return EntityRef("well", str(well.id), label=str(well.name))
    return None


def placeholder_id(*, scope_id: str, entity_type: str, label: str) -> str:
    """A stable id for an entity the knowledge layer has to stand up for itself.

    Derived from what created it (the version), what it is and what it is called - so re-deriving
    the same knowledge produces the same row instead of a fresh one each time.  A random id here
    would make every rebuild orphan the previous one's edges.
    """
    from ..core.hashing import sha256_text

    digest = sha256_text(f"{scope_id}|{entity_type}|{str(label).strip().casefold()}")
    return f"ki-{digest[:24]}"


def ensure_placeholder(
    session: Any,
    *,
    entity_type: str,
    label: str,
    entity_id: str = "",
    origin: str = KnowledgeOrigin.DERIVED.value,
    status: str = KnowledgeStatus.ACTIVE.value,
) -> EntityRef:
    """Stand up (or reuse) the ``knowledge_item`` row an item-backed entity needs.

    With ``entity_id`` the row is keyed on that id, which is what makes a derived subject
    reproducible; without it, the label is the key, so "BHA-07" is one entity rather than two.
    Table-backed types are refused: a well is created by the well registry, and letting the
    knowledge layer invent one would put two sources of truth in front of the same name.
    """
    spec = entity_spec(entity_type)
    if spec.table:
        raise KnowledgeError(
            f"{spec.label} rows are created by their own registry, not by a knowledge reference",
            hint=f"link to an existing {entity_type} id, or register it through its own command first",
        )
    wanted = str(label or "").strip()
    if not wanted:
        raise KnowledgeError(f"a {spec.label} needs a label before it can be referenced")
    if entity_id:
        existing = session.get(KnowledgeItem, str(entity_id))
        if existing is not None:
            if str(existing.item_type) != spec.item_type:
                raise KnowledgeError(
                    f"{entity_id!r} is a {existing.item_type} row, not a {spec.label}",
                    hint="a derived subject id must not be reused across entity types",
                )
            return EntityRef(spec.name, str(existing.id), label=str(existing.title))
        row = KnowledgeItem(
            id=str(entity_id),
            item_type=spec.item_type,
            title=wanted,
            content="",
            domain=spec.name,
            status=status,
            origin=origin,
            payload={"entity_type": spec.name, "placeholder": True},
            created_by="knowledge",
        )
        session.add(row)
        session.flush()
        return EntityRef(spec.name, str(row.id), label=str(row.title))
    found = session.execute(
        select(KnowledgeItem).where(
            KnowledgeItem.item_type == spec.item_type, KnowledgeItem.title == wanted
        )
    ).scalar_one_or_none()
    if found is not None:
        return EntityRef(spec.name, str(found.id), label=str(found.title))
    return ensure_placeholder(
        session,
        entity_type=spec.name,
        label=wanted,
        entity_id=placeholder_id(scope_id="label", entity_type=spec.name, label=wanted),
        origin=origin,
        status=status,
    )


def _new_item_id() -> str:
    from ..core.ids import new_id

    return new_id("ki")


def _looks_like_item_id(value: str) -> bool:
    return str(value or "").startswith("ki-") and len(str(value)) <= 36
