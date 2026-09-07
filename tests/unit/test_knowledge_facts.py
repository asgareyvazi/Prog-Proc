"""What a document is allowed to become knowledge, and what it is not.

The knowledge layer's whole claim is that a value keeps its meaning *and* its source on the way from
a cell in a spreadsheet to an answer about a well.  That claim lives here, in two registries and one
dataclass, and all of it is testable without a database:

*   the entity vocabulary (what a fact can be about) stays aligned with the rest of the platform
    instead of drifting into a second taxonomy;
*   field names map onto predicates, and predicates onto typed values - the original wording kept,
    the normalised number computed through :mod:`drilling_intelligence.core.units`, never guessed;
*   provenance is a precondition of storage for anything claiming to have been read from a file.

The regressions this file pins are the two bugs found while building the layer, because both failed
*silently*: a ``str`` is also a ``Sequence``, so a value type-checked in the wrong order came out
empty for every text field; and a provenance guard written with an inverted condition rejected the
facts that had citations and accepted the ones that did not.
"""

from __future__ import annotations

import pytest

from drilling_intelligence.core.enums import DataQuality, KnowledgeOrigin
from drilling_intelligence.core.provenance import Provenance
from drilling_intelligence.database.models import KnowledgeItem, Well
from drilling_intelligence.knowledge.entities import (
    ENTITY_ALIASES,
    ENTITY_TYPES,
    EntityRef,
    KnowledgeError,
    entity_spec,
    normalise_entity_type,
    placeholder_id,
    ref_for_row,
    subject_type_for_classification,
)
from drilling_intelligence.knowledge.facts import (
    PREDICATE_BY_FIELD,
    PREDICATES,
    KnowledgeFact,
    predicate_for_field,
    render_value,
)

#: The provenance every test below attaches, in the shape the extractors store it.
PROVENANCE = {
    "document_id": "doc-1",
    "document_version_id": "ver-1",
    "filename": "mud.xlsx",
    "parser": "excel/1",
    "excerpt": "Mud weight (ppg) | 10.2",
    "source_sha256": "b" * 64,
    "confidence": 0.95,
    "locator": {
        "locator_kind": "excel",
        "sheet": "Summary",
        "cell": "B9",
        "range_": "A1:D16",
        "read": "10.2",
        "row": 9,
        "column": 2,
    },
}

WELL = EntityRef("well", "well-1", label="A-3")


def field(name: str, value, unit: str = "", **overrides) -> dict:
    entry = {
        "name": name,
        "value": value,
        "unit": unit,
        "quality": DataQuality.VALID.value,
        "confidence": 0.9,
        "method": "test",
        "note": "",
        "provenance": dict(PROVENANCE),
    }
    entry.update(overrides)
    return entry


def build(name: str, value, unit: str = "", **overrides) -> KnowledgeFact:
    return KnowledgeFact.from_field(
        field(name, value, unit, **overrides),
        subject=WELL,
        document_id="doc-1",
        document_version_id="ver-1",
    )


# --------------------------------------------------------------------------- entities
def test_the_briefs_entities_are_all_known_to_the_layer() -> None:
    """The list in the brief, checked against the registry - so it cannot stay aspirational.

    Some of them arrive as aliases rather than names, because the platform already had a word for
    the thing (a hole section is a ``section`` row in the well registry), and reusing it is the
    point: two vocabularies for one concept is how a graph gets cut in half.
    """
    for wanted in (
        "company",
        "project",
        "well",
        "section",
        "formation",
        "hole_section",
        "bha",
        "bit",
        "mud",
        "casing",
        "trajectory",
        "npt_event",
        "service",
        "rig",
        "equipment",
        "safety_event",
        "problem",
        "lesson_learned",
        "engineering_fact",
        "document",
        "document_version",
    ):
        assert wanted in ENTITY_TYPES, wanted
    assert normalise_entity_type("well_section") == "section"
    assert normalise_entity_type("lesson") == "lesson_learned"


def test_the_vocabulary_is_one_lookup_away_from_loose_names() -> None:
    assert normalise_entity_type("Hole Section") == "hole_section"
    assert normalise_entity_type("  bha ") == "bha"
    assert normalise_entity_type("WBHA") == "bha"
    # Every alias resolves, or it is a trap for the next person who trusts the table.
    for alias, target in ENTITY_ALIASES.items():
        assert normalise_entity_type(alias) == target, alias
        assert target in ENTITY_TYPES, target


def test_an_unknown_entity_is_rejected_with_the_alternatives_listed() -> None:
    with pytest.raises(KnowledgeError) as excinfo:
        normalise_entity_type("drilling_fluid_program_v2")
    assert "unknown entity type" in str(excinfo.value)
    assert "known" in str(excinfo.value)


def test_a_reference_without_an_id_is_an_error_not_an_empty_pointer() -> None:
    """Empty ids are refused at construction.

    A fact "about nothing in particular" is how a knowledge base accumulates rows nobody can follow
    up, and an edge to a dangling subject looks exactly like a real relationship in a UI.
    """
    with pytest.raises(KnowledgeError, match="needs an id"):
        EntityRef("engineering_fact", "")


def test_a_reference_knows_how_it_is_stored() -> None:
    assert EntityRef("well", "well-1").endpoint_type == "well"
    assert EntityRef("bit", "ki-1").endpoint_type == "knowledge_item"
    assert EntityRef("engineering_fact", "ki-2").key() == "engineering_fact:ki-2"
    assert (
        EntityRef.from_dict({"entity_type": "mud", "entity_id": "ki-3", "label": "WBM"}).label
        == "WBM"
    )


def test_a_row_becomes_the_reference_it_stands_for() -> None:
    """References come from the mapped table, so they cannot drift from the model."""
    well = Well(id="well-9", name="A-3")
    assert ref_for_row(well).key() == "well:well-9"
    assert ref_for_row(well).label == "A-3"
    item = KnowledgeItem(
        id="ki-1",
        item_type="EQUIPMENT",
        title="BHA-07",
        content="",
        domain="bha",
        payload={"entity_type": "bha"},
    )
    assert ref_for_row(item).entity_type == "bha"
    with pytest.raises(KnowledgeError, match="nothing"):
        ref_for_row(None)


def test_a_document_type_decides_what_its_facts_are_about() -> None:
    """The mapping is *derived* from each spec's ``described_by``, so there is one table, not two."""
    assert subject_type_for_classification("MUD_REPORT") == "mud"
    assert subject_type_for_classification("DDR") == "drilling_parameter"
    assert subject_type_for_classification("DIRECTIONAL_SURVEY") == "trajectory"
    assert subject_type_for_classification("LESSON_LEARNED") == "lesson_learned"
    assert subject_type_for_classification("OTHER") == "document_version"
    assert subject_type_for_classification(None) == "document_version"
    for name, spec in ENTITY_TYPES.items():
        for classification in spec.described_by:
            assert subject_type_for_classification(classification) == name, classification


def test_no_two_entity_types_claim_the_same_document_type() -> None:
    """One classification, one subject - enforced at import, asserted here so it stays honest.

    Two claimants would mean whichever spec happened to be defined last decided what every fact of
    that kind is about, and nothing in the file or the data would say so.
    """
    claimed: dict[str, str] = {}
    for name, spec in ENTITY_TYPES.items():
        for classification in spec.described_by:
            assert classification not in claimed, (
                f"{classification}: {claimed.get(classification)} vs {name}"
            )
            claimed[classification] = name


def test_an_unattributable_value_is_attributed_to_the_file_that_stated_it() -> None:
    """``document_version`` is the fallback, and it is honest rather than vague.

    Saying "revision 3 of mud.xlsx states 10.2 ppg" is true; saying "well A-3's mud weight is 10.2"
    when the document never named a well is not, and no folder layout changes that.
    """
    assert subject_type_for_classification("SURVEY_REPORT") in {"trajectory", "document_version"}
    assert entity_spec("document_version").table == "document_version", "a version always exists"


def test_table_backed_types_have_tables_and_the_rest_have_rows() -> None:
    assert entity_spec("well").table == "well"
    assert not entity_spec("bit").table, "an item-backed type has no table of its own"
    assert entity_spec("bit").item_type
    assert entity_spec("well").item_type == ""


def test_a_derived_subject_id_is_reproducible() -> None:
    first = placeholder_id(scope_id="ver-1", entity_type="drilling_parameter", label="mud.xlsx")
    assert first == placeholder_id(
        scope_id="ver-1", entity_type="drilling_parameter", label="MUD.XLSX"
    )
    assert first != placeholder_id(
        scope_id="ver-2", entity_type="drilling_parameter", label="mud.xlsx"
    )
    assert first.startswith("ki-") and len(first) <= 36, "it lands in knowledge_item.id"


# --------------------------------------------------------------------------- predicates
def test_a_registered_field_name_maps_onto_its_predicate() -> None:
    assert predicate_for_field("Mud Weight (ppg)")[0] == "mud_weight"
    assert predicate_for_field("mud_weight")[0] == "mud_weight"
    assert predicate_for_field("MW")[0] == "mud_weight"
    assert predicate_for_field("hole size")[0] == "hole_section_size"
    assert predicate_for_field("Bit Size (in)")[0] == "hole_section_size"
    for field_name, predicate in PREDICATE_BY_FIELD.items():
        assert predicate_for_field(field_name)[0] == predicate, field_name


def test_a_unit_in_the_header_does_not_create_a_new_property() -> None:
    """ "Hole size, in" and "hole size" are one property; a suffix that *is* a predicate is not.

    ``mud_weight_in`` (inlet density) is a different measurement from ``mud_weight``, and the
    longest registered name wins precisely so that the two do not merge.
    """
    assert predicate_for_field("Hole Size, in")[0] == "hole_section_size"
    assert predicate_for_field("hole_size_in")[0] == "hole_section_size"
    assert predicate_for_field("mud_weight_in")[0] == "mud_weight_in"


def test_an_unknown_field_keeps_its_own_name() -> None:
    """The vocabulary of a real report is not closed, and inventing a nearby predicate is how
    wrong data becomes confident data."""
    assert predicate_for_field("Some New Parser Field")[0] == "some_new_parser_field"
    assert predicate_for_field("Some New Parser Field")[1] is None
    with pytest.raises(KnowledgeError, match="field name"):
        predicate_for_field("   ")


def test_each_predicate_declares_its_kind() -> None:
    """A declared dimension decides the outcome; ``auto`` means "read the value and see".

    Both are promises the typing code has to keep, and the failure directions matter more than the
    successes: a dimensioned predicate that is handed nothing usable becomes a *text* fact with a
    note, never a quantity invented from an empty cell.
    """
    assert PREDICATES["mud_weight"].dimension is not None
    assert build("mud_weight", "10.2", "ppg").value_type == "quantity"
    empty = build("mud_weight", "", quality=DataQuality.MISSING.value)
    assert empty.value_type == "text"
    assert "needs a number" in empty.note
    assert PREDICATES["temperature"].value_type == "auto"
    assert build("temperature", "85", "degF").value_type == "quantity"
    assert PREDICATES["report_date"].value_type == "date"
    assert PREDICATES["event_count"].value_type == "ratio"


# --------------------------------------------------------------------------- provenance
def test_a_value_without_a_source_is_refused() -> None:
    entry = field("mud_weight", "10.2", "ppg")
    entry.pop("provenance")
    with pytest.raises(KnowledgeError, match="no provenance"):
        KnowledgeFact.from_field(
            entry, subject=WELL, document_id="doc-1", document_version_id="ver-1"
        )


def test_a_note_a_person_typed_does_not_cite_a_document() -> None:
    """``MANUAL`` is the one origin with no locator, and that is stated rather than faked."""
    fact = KnowledgeFact(
        subject=WELL,
        predicate="mud_weight",
        value_type="quantity",
        value=10.2,
        unit="ppg",
        original_value="10.2",
        original_unit="ppg",
        origin=KnowledgeOrigin.MANUAL.value,
    )
    assert fact.provenance is None
    assert not fact.is_source_derived
    assert fact.quality == DataQuality.UNVERIFIED.value


def test_provenance_round_trips_through_the_model() -> None:
    fact = build("mud_weight", "10.2", "ppg")
    assert isinstance(fact.provenance, Provenance)
    assert fact.provenance.locator.sheet == "Summary"
    assert fact.citation() == "mud.xlsx > Sheet: Summary > Cell: B9"


# --------------------------------------------------------------------------- the row contract
def test_a_fact_survives_the_trip_into_and_out_of_a_row() -> None:
    fact = build("mud_weight", "10.2", "ppg", note="from the mud log")
    payload = fact.to_item(item_id="ki-1", source_id="src-1")
    row = type("Row", (), payload)()
    restored = KnowledgeFact.from_item(row)
    assert restored.item_id == "ki-1"
    assert (
        restored == KnowledgeFact.from_item(KnowledgeItem(**payload))
        or restored.to_dict() == fact.to_dict()
    )
    for attribute in (
        "predicate",
        "original_value",
        "original_unit",
        "normalized_unit",
        "subject",
        "document_version_id",
        "record_state",
    ):
        assert getattr(restored, attribute) == getattr(fact, attribute), attribute


def test_the_columns_and_the_payload_agree() -> None:
    """The payload is a convenience; queries filter on columns, so the two must not disagree."""
    fact = build("mud_weight", "10.2", "ppg")
    item = fact.to_item(item_id="ki-1")
    payload = item["payload"]
    assert item["entity_type"] == payload["subject"]["entity_type"]
    assert item["entity_id"] == payload["subject"]["entity_id"]
    for key in (
        "predicate",
        "value_type",
        "record_state",
        "status",
        "origin",
        "original_value",
        "original_unit",
    ):
        assert str(item[key]) == str(payload[key]), key
    # ``content`` is the row's readable body - the value as it reads - while the wider descriptive
    # text belongs to the index.  Two different jobs, and mixing them would put a paragraph where a
    # UI expects a quantity.
    assert item["content"] == "10.2 ppg"
    assert "mud_weight" in fact.search_text()
    assert "10.2" in fact.search_text()
    assert item["title"] == "A-3 · Mud weight = 10.2 ppg"
    assert item["item_type"] in {"OBSERVATION", "CONSTANT", "SPECIFICATION"}


def test_the_lookup_key_groups_statements_about_the_same_property() -> None:
    first = build("mud_weight", "10.2", "ppg")
    second = build("mud_weight", "10.4", "ppg")
    other_well = KnowledgeFact.from_field(
        field("mud_weight", "9.8", "ppg"),
        subject=EntityRef("well", "well-2", label="B-11"),
        document_id="doc-1",
        document_version_id="ver-1",
    )
    assert first.lookup_key() == second.lookup_key(), "the value must not be part of the key"
    assert first.lookup_key() != other_well.lookup_key()
    assert "mud_weight" in first.lookup_key()
    assert first.lookup_key().startswith("well:well-1|")


def test_planned_and_actual_statements_do_not_share_a_key() -> None:
    """A program's intention and a report's observation are different facts (architecture §11).

    Without this, every drilling program would "conflict" with every report about the same hole,
    and the conflict list would be noise nobody reads.
    """
    planned = KnowledgeFact.from_field(
        field("mud_weight", "12.0", "ppg"),
        subject=WELL,
        record_state="PLANNED",
        document_id="doc-1",
        document_version_id="ver-1",
    )
    actual = build("mud_weight", "12.0", "ppg")
    assert planned.lookup_key() != actual.lookup_key()
    assert "PLANNED" in planned.lookup_key()


def test_rendering_a_value_formats_numbers_through_the_shared_unit_code() -> None:
    assert render_value(10.2, "ppg") == "10.2 ppg"
    assert render_value(None, "ft", text="13 3/8 in") == "13 3/8 in"
    assert render_value(None, "", text="") == ""
    assert PREDICATES["mud_weight"].label == "Mud weight"
