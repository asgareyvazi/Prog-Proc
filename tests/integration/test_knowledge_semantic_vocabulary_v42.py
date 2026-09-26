"""V4.2 - the semantic vocabulary: predicate identity, collision matrix, false-conflict elimination.

The V4 audit found five knowledge conflicts in the forensic corpus.  Four of them were not
disagreements.  Different documents had reported *different engineering quantities*, and the pipeline
had collapsed them onto one predicate before the context that distinguished them was gone::

    SIDPP 420 psi | SICP 610 psi | MAASP 1850 psi  ->  surface_pressure   (3 quantities, 1 name)
    active 1450 bbl | pill 12 bbl                  ->  mud_volume         (2 quantities, 1 name)
    rotary 120 rpm | rheometer 300 rpm             ->  rpm                (2 quantities, 1 name)
    MD 9940 ft | TVD 9850 ft                       ->  hole_depth         (2 quantities, 1 name)

They look like conflicts because they *are* conflicts once the distinction is gone.  A kick sheet's
12 bbl pill and a mud report's 1,450 bbl active system share a unit and a dimension and nothing
else.  Merging them is not a reporting bug to be suppressed; it is a lost distinction, and the only
sound repair is to keep the distinction.

The rule these tests hold the layer to:

**A predicate represents an engineering assertion, not a field name, a unit dimension or a numeric
type.**  Two values may share a unit and a dimension and still be different quantities.  Context must
not be discarded before semantic identity is established - and where the source genuinely gives no
context, the answer stays generic and is *not* guessed.

Identity is decided in two layers, and the two are tested separately because they fail differently:

1.  **extraction** - a phrase in a source becomes a canonical field name (``SIDPP: 420 psi`` ->
    ``sidpp``).  Losing the context happens here, so this is where it is preserved.
2.  **vocabulary** - a field name becomes a predicate (``sidpp`` -> the ``sidpp`` assertion).  This
    layer is deliberately conservative: a name it has never seen keeps its own name rather than
    borrowing a nearby predicate.

``docs/KNOWLEDGE_SEMANTIC_VOCABULARY.md`` states the rule; this file is its executable form.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.fixtures.fieldops import register_wells, well_id_for
from tests.fixtures.generate import build_v4_forensic_corpus

from drilling_intelligence.database.models import KnowledgeConflict, KnowledgeItem
from drilling_intelligence.extraction.fields import FieldExtractor
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.knowledge import facts as facts_module
from drilling_intelligence.knowledge.conflicts import detect_conflicts
from drilling_intelligence.knowledge.facts import (
    PREDICATE_BY_FIELD,
    PREDICATES,
    UNIT_SUFFIX_TOKENS,
    predicate_for_field,
)
from drilling_intelligence.knowledge.repository import KnowledgeRepository
from drilling_intelligence.operations.service import OperationalService
from drilling_intelligence.search.chunking import KIND_KNOWLEDGE
from drilling_intelligence.search.service import SearchService

# --------------------------------------------------------------- layer 2: field name -> predicate
#: The collision matrix.  One row per registered field name, the right-hand column the engineering
#: assertion it denotes.  The grouping is the point: names that denote the *same* assertion share a
#: value, and names that denote *different* assertions must not.
FIELD_TO_PREDICATE: dict[str, str] = {
    # --- surface pressure: four distinct assertions, historically one -------------------------------
    "sidpp": "sidpp",
    "shut_in_drillpipe_pressure": "sidpp",
    "shut_in_drill_pipe_pressure": "sidpp",
    "sidpp_psi": "sidpp",
    "sicp": "sicp",
    "shut_in_casing_pressure": "sicp",
    "shut_in_annulus_pressure": "sicp",
    "maasp": "maasp",
    "masp": "maasp",
    "maximum_allowable_annular_surface_pressure": "maasp",
    "maximum_allowable_surface_pressure": "maasp",
    # deliberately *not* any of the three above
    "surface_pressure": "surface_pressure",
    "surface_pressure_psi": "surface_pressure",
    "spp": "standpipe_pressure",
    # --- volume: system, pill, kick and trip tank are different things -------------------------------
    "mud_volume": "mud_volume",
    "mud_volume_bbl": "mud_volume",
    "pit_volume": "mud_volume",
    "pill_volume": "pill_volume",
    "pill_volume_bbl": "pill_volume",
    "kick_volume": "kick_volume",
    "kick_volume_bbl": "kick_volume",
    "gain_volume": "kick_volume",
    "trip_tank_volume": "trip_tank_volume",
    # --- rotational speed: the string and the instrument --------------------------------------------
    "rpm": "rpm",
    "rheometer_speed": "rheometer_speed",
    "rheometer_rpm": "rheometer_speed",
    "viscometer_dial_speed": "rheometer_speed",
    # --- depth: measured and vertical are different measurements ------------------------------------
    "md": "measured_depth",
    "measured_depth": "measured_depth",
    "depth_md": "measured_depth",
    "depth_md_ft": "measured_depth",
    "tvd": "true_vertical_depth",
    "true_vertical_depth": "true_vertical_depth",
    "depth_tvd": "true_vertical_depth",
    "depth_tvd_ft": "true_vertical_depth",
    "depth": "hole_depth",
    "hole_depth": "hole_depth",
    "td_md": "hole_depth",
    # --- diameter: the bit is not the hole it drills ------------------------------------------------
    "bit_size": "bit_size",
    "bit_size_in": "bit_size",
    "bit_gauge": "bit_size",
    "bit_diameter": "bit_size",
    "hole_size": "hole_section_size",
    "section_size": "hole_section_size",
    "hole_size_in": "hole_section_size",
    "casing_size": "casing_size",
    # --- legitimate aliasing must still merge -------------------------------------------------------
    "mud_weight": "mud_weight",
    "mud_weight_ppg": "mud_weight",
    "mw": "mud_weight",
    "mw_out": "mud_weight",
    # --- and a suffix that is itself a predicate must not be swallowed ------------------------------
    "mw_in": "mud_weight_in",
}

#: Layer 1: what the extractor must name a phrase, so the vocabulary layer has a chance to separate
#: it.  This is the layer where the context used to be thrown away.
PHRASE_TO_FIELD: dict[str, str] = {
    "SIDPP: 420 psi": "sidpp",
    "Shut-in drillpipe pressure: 420 psi": "sidpp",
    "SICP: 610 psi": "sicp",
    "Shut-in casing pressure: 610 psi": "sicp",
    "MAASP: 1850 psi": "maasp",
    "MASP: 1850 psi": "maasp",
    "Maximum allowable surface pressure: 1850 psi": "maasp",
    "Maximum allowable annular surface pressure: 1850 psi": "maasp",
    "Surface pressure: 900 psi": "surface_pressure",
    "Active mud volume: 1450 bbl": "mud_volume_bbl",
    "Total system volume: 1450 bbl": "mud_volume_bbl",
    "Pit volume: 1450 bbl": "mud_volume_bbl",
    "Pill volume: 12 bbl": "pill_volume",
    "Kick volume: 20 bbl": "kick_volume",
    "Trip tank volume: 85 bbl": "trip_tank_volume",
    "Rotary speed: 120 rpm": "rpm",
    "Rheometer speed: 300 rpm": "rheometer_speed",
    "MD: 9940 ft": "depth_md",
    "TVD: 9850 ft": "depth_tvd",
}


def test_the_collision_matrix_holds() -> None:
    """Every registered field name resolves to exactly the assertion it denotes."""
    failures = {
        field: (expected, predicate_for_field(field)[0])
        for field, expected in FIELD_TO_PREDICATE.items()
        if predicate_for_field(field)[0] != expected
    }
    assert not failures, failures


def test_the_extractor_keeps_the_context_that_separates_the_quantities() -> None:
    """Layer 1: a labelled phrase becomes a field name that still says which quantity it is."""
    extractor = FieldExtractor()
    failures = {}
    for phrase, expected in PHRASE_TO_FIELD.items():
        hits = extractor.scan_text(phrase)
        got = [hit.name for hit in hits]
        if got != [expected]:
            failures[phrase] = (expected, got)
    assert not failures, failures


def test_the_quantities_that_used_to_collide_are_now_separate_predicates() -> None:
    """End to end, phrase in and predicate out: each historical collision resolves to several."""
    extractor = FieldExtractor()

    def predicates(*phrases: str) -> set[str]:
        out: set[str] = set()
        for phrase in phrases:
            for hit in extractor.scan_text(phrase):
                out.add(predicate_for_field(hit.name)[0])
        return out

    assert len(
        predicates("SIDPP: 420 psi", "SICP: 610 psi", "MAASP: 1850 psi", "Surface pressure: 900 psi")
    ) == 4
    assert len(
        predicates(
            "Active mud volume: 1450 bbl", "Pill volume: 12 bbl", "Kick volume: 20 bbl",
            "Trip tank volume: 85 bbl",
        )
    ) == 4
    assert len(predicates("Rotary speed: 120 rpm", "Rheometer speed: 300 rpm")) == 2
    # both of these used to be ``hole_depth``
    assert len(predicates("MD: 9940 ft", "TVD: 9850 ft")) == 2


def test_no_depth_role_is_registered_as_a_unit() -> None:
    """The bug that merged MD with TVD, pinned so it cannot return.

    ``md`` and ``tvd`` are depth *roles*, not units.  Listed as units, the unit-suffix rule stripped
    them off ``depth_md``/``depth_tvd`` and both fell through to ``hole_depth`` - which is how a
    9,850 ft true vertical depth came to sit among measured depths and look like a conflict.
    """
    assert "md" not in UNIT_SUFFIX_TOKENS, "a measured-depth role is not a unit"
    assert "tvd" not in UNIT_SUFFIX_TOKENS, "a true-vertical-depth role is not a unit"
    assert predicate_for_field("depth_md")[0] == "measured_depth"
    assert predicate_for_field("depth_tvd")[0] == "true_vertical_depth"


# ---------------------------------------------------------------------- category A / B / C conflicts
#: Three prose reports about the same well, arranged so the three conflict categories can be told
#: apart.  The numbers are deliberately adversarial: SIDPP, SICP and MAASP all read 420 psi, and the
#: kick volume and the pill volume are both 12 bbl.  Category B is the one that matters - the same
#: number for two different quantities is *agreement*, and reporting it as a dispute is the failure
#: mode this mission repairs.
CATEGORY_CORPUS: dict[str, str] = {
    "kick_sheet_alpha.txt": (
        "Well: A-3\n\nKick sheet - initial circulation\n\n"
        "SIDPP: 420 psi\n"
        "SICP: 420 psi\n"
        "Kick volume: 12 bbl\n"
        "Rotary speed: 120 rpm\n"
        "MD: 9940 ft\n"
    ),
    "kick_sheet_beta.txt": (
        "Well: A-3\n\nKick sheet - revised after circulation\n\n"
        "SIDPP: 380 psi\n"
        "Pill volume: 12 bbl\n"
    ),
    "well_control_limits.txt": (
        "Well: A-3\n\nWell control limits\n\n"
        "MAASP: 420 psi\n"
        "Rheometer speed: 300 rpm\n"
        "TVD: 9850 ft\n"
    ),
}


def _ingest(workspace, root_name: str, documents: dict[str, str]) -> None:
    register_wells(workspace)
    _ingest_into(workspace, root_name, documents)


def _ingest_into(workspace, root_name: str, documents: dict[str, str]) -> None:
    """Ingest without registering wells again - ``register_wells`` is not idempotent."""
    root = workspace.root / root_name
    root.mkdir(parents=True, exist_ok=True)
    for name, text in documents.items():
        (root / name).write_text(text, encoding="utf-8")
    _run(workspace, root)


def _ingest_v4_corpus(workspace) -> None:
    """The existing 14-file forensic corpus, unchanged - this suite adds to it, never replaces it."""
    register_wells(workspace)
    root = workspace.root / "corpus"
    build_v4_forensic_corpus(root)
    _run(workspace, root)


def _run(workspace, root) -> None:
    result = IngestionPipeline(
        settings=workspace.settings, workspace_root=workspace.root, database=workspace.database
    ).run(root=root, well_id=well_id_for(workspace, "A-3"))
    assert result.ok, result.error
    OperationalService.for_workspace(workspace).promote_workspace(include_unsupported=True)


def _conflicts(workspace) -> list[KnowledgeConflict]:
    with workspace.database.read_only() as session:
        rows = list(session.execute(select(KnowledgeConflict)).scalars())
    return sorted(rows, key=lambda row: row.property_name)


def _report(workspace):
    with workspace.database.read_only() as session:
        return detect_conflicts(KnowledgeRepository(session))


def _items(workspace) -> list[KnowledgeItem]:
    with workspace.database.read_only() as session:
        return list(session.execute(select(KnowledgeItem)).scalars())


def _predicate_values(workspace, predicates) -> dict[str, list[float]]:
    items = _items(workspace)
    return {
        name: sorted(float(item.value) for item in items if item.predicate == name)
        for name in predicates
    }


def test_category_a_same_quantity_different_values_is_exactly_one_conflict(workspace) -> None:
    """Two kick sheets state two different shut-in drillpipe pressures.  That is one disagreement."""
    _ingest(workspace, "categories", CATEGORY_CORPUS)

    rows = _conflicts(workspace)
    assert [row.property_name for row in rows] == ["sidpp"], [r.property_name for r in rows]
    sidpp = rows[0]
    assert {candidate["text"] for candidate in sidpp.candidates} == {"420 psi", "380 psi"}
    assert {candidate["source"] for candidate in sidpp.candidates} == {
        "kick_sheet_alpha.txt",
        "kick_sheet_beta.txt",
    }
    assert sidpp.note == "2 different values stated by 2 sources", sidpp.note


def test_category_b_different_quantities_same_number_is_not_a_conflict(workspace) -> None:
    """SIDPP, SICP and MAASP all read 420 psi.  Nobody disagrees; they are different gauges.

    This is the case the old vocabulary got wrong.  Sharing a number proves nothing about sharing a
    meaning, and reporting agreement as a dispute trains an engineer to dismiss the panel.
    """
    _ingest(workspace, "categories", CATEGORY_CORPUS)

    disputed = {row.property_name for row in _conflicts(workspace)}
    assert disputed == {"sidpp"}, disputed

    # the three quantities really do hold the same number, so this is agreement and not an absence
    values = _predicate_values(
        workspace, ("sidpp", "sicp", "maasp", "kick_volume", "pill_volume")
    )
    assert values["sicp"] == [420.0], values
    assert values["maasp"] == [420.0], values
    assert 420.0 in values["sidpp"], values
    assert values["kick_volume"] == [12.0] and values["pill_volume"] == [12.0], values


def test_category_c_different_quantities_different_values_is_not_a_conflict(workspace) -> None:
    """A 120 rpm string beside a 300 rpm rheometer, an MD beside a TVD.

    Different assertions never conflict, whatever their numbers do.  The values are deliberately
    different so the absence of a conflict cannot be misread as agreement.
    """
    _ingest(workspace, "categories", CATEGORY_CORPUS)

    disputed = {row.property_name for row in _conflicts(workspace)}
    for predicate in (
        "rpm",
        "rheometer_speed",
        "measured_depth",
        "true_vertical_depth",
        "kick_volume",
        "pill_volume",
    ):
        assert predicate not in disputed, (predicate, disputed)

    assert _predicate_values(
        workspace, ("rpm", "rheometer_speed", "measured_depth", "true_vertical_depth")
    ) == {
        "rpm": [120.0],
        "rheometer_speed": [300.0],
        "measured_depth": [9940.0],
        "true_vertical_depth": [9850.0],
    }


# --------------------------------------------------------------------- no silent merging, end to end
def test_folding_two_quantities_back_together_restores_a_phantom_conflict(
    workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation proof: the separation is load-bearing, not decorative.

    Folding ``rheometer_speed`` back onto ``rpm`` - and ``true_vertical_depth`` back onto
    ``measured_depth`` - is a one-line vocabulary change.  Run the *whole* pipeline under it and two
    conflicts appear between documents that never disagreed: a 300 rpm rheometer setting against a
    120 rpm rotary speed, and a 9,850 ft TVD against a 9,940 ft MD.  A test that only asserted the
    current output would not notice; this one fails the moment the distinction is removed.
    """
    _ingest(workspace, "categories", CATEGORY_CORPUS)
    assert {row.property_name for row in _conflicts(workspace)} == {"sidpp"}

    folded = {"rheometer_speed": "rpm", "true_vertical_depth": "measured_depth"}
    original = predicate_for_field

    def merged(name: str):
        predicate, spec = original(name)
        target = folded.get(predicate)
        return original(target) if target else (predicate, spec)

    # ``KnowledgeFact.from_field`` resolves this name through the module globals, so patching it
    # changes what the real promotion path writes - there is no stand-in implementation here.
    monkeypatch.setattr(facts_module, "predicate_for_field", merged)

    _ingest_into(workspace, "categories_folded", CATEGORY_CORPUS)

    rows = {row.property_name: row for row in _conflicts(workspace)}
    # Neither of these was disputed before the fold.  Both are now, purely because two quantities
    # were given one name - the values in the sources never changed.
    assert {"rpm", "measured_depth"} <= set(rows), sorted(rows)
    assert {c["text"] for c in rows["rpm"].candidates} == {"120 rpm", "300 rpm"}, rows["rpm"].candidates
    assert {c["text"] for c in rows["measured_depth"].candidates} == {
        "9940 ft",
        "9850 ft",
    }, rows["measured_depth"].candidates


def test_the_v4_corpus_reports_two_genuine_disagreements(workspace) -> None:
    """The forensic corpus, end to end: five reported conflicts become two, and both are real.

    ``hole_section_size`` is an 8 1/2 in section beside a 12 1/4 in section.  ``measured_depth`` is
    five sources stating five different measured depths.  Neither is a vocabulary artefact and
    neither is resolved by this change - they are handed to a human, which is the correct outcome
    for a real disagreement.
    """
    _ingest_v4_corpus(workspace)

    assert {row.property_name for row in _conflicts(workspace)} == {
        "hole_section_size",
        "measured_depth",
    }

    # the true vertical depth is no longer counted as a disagreeing measured depth
    items = _items(workspace)
    assert 9850.0 not in {
        float(item.value) for item in items if item.predicate == "measured_depth"
    }, "a TVD is not a measured depth"
    assert any(item.predicate == "true_vertical_depth" for item in items)


def test_both_sides_of_a_real_disagreement_stay_retrievable(workspace) -> None:
    """Separating quantities must not cost evidence: both measured depths remain findable."""
    _ingest_v4_corpus(workspace)
    search = SearchService.for_workspace(workspace)
    search.rebuild()

    conflict = next(row for row in _conflicts(workspace) if row.property_name == "measured_depth")
    values = {candidate["text"] for candidate in conflict.candidates}
    assert len(values) >= 2, values

    response = search.search("measured depth ft", kinds=[KIND_KNOWLEDGE], limit=50)
    found = {result.text for result in response.results}
    for text in values:
        assert any(text in hit for hit in found), f"{text!r} disappeared from search"


# ---------------------------------------------------------------------------- registry integrity
def test_every_predicate_is_well_formed() -> None:
    for name, spec in PREDICATES.items():
        assert name == spec.name, (name, spec.name)
        assert spec.label.strip(), name
        assert spec.fields, f"{name} declares no field aliases"
        assert spec.value_type, name
        for field in spec.fields:
            assert field.strip() == field, (name, field)


def test_the_separated_quantities_carry_a_dimension_so_their_units_can_be_checked() -> None:
    """A predicate with no dimension accepts any unit, which is how a bbl slips in beside a psi."""
    for name, expected in (
        ("sidpp", "PRESSURE"),
        ("sicp", "PRESSURE"),
        ("maasp", "PRESSURE"),
        ("surface_pressure", "PRESSURE"),
        ("pill_volume", "VOLUME"),
        ("kick_volume", "VOLUME"),
        ("trip_tank_volume", "VOLUME"),
        ("rheometer_speed", "ROTARY_SPEED"),
    ):
        assert PREDICATES[name].dimension is not None, name
        assert PREDICATES[name].dimension.value == expected, (name, PREDICATES[name].dimension)


def test_two_predicates_never_claim_the_same_field_alias() -> None:
    """A shared alias is a silent merge waiting to happen; the registry must make it impossible."""
    claims: dict[str, list[str]] = {}
    for name, spec in PREDICATES.items():
        for field in spec.fields:
            claims.setdefault(field.lower(), []).append(name)
    assert not {f: n for f, n in claims.items() if len(n) > 1}


def test_every_registered_alias_resolves_back_to_its_own_predicate() -> None:
    for field, predicate in PREDICATE_BY_FIELD.items():
        assert predicate_for_field(field)[0] == predicate, field


def test_every_predicate_is_reachable_from_at_least_one_alias() -> None:
    unreachable = set(PREDICATES) - set(PREDICATE_BY_FIELD.values())
    assert not unreachable, unreachable


# ------------------------------------------------------------------- unknown-field safety
#: Field names the vocabulary has never seen.  None of them may be forced onto a nearby predicate.
#:
#: ``total_mud_volume_bbl`` used to be in this list and is not any more.  V4.3 registered it as an
#: alias of ``mud_volume`` on corpus evidence - the mud workbook's own ``SUMMARY_ALIASES`` already
#: treats "total mud volume", "mud volume" and "active system volume" as one property, and the
#: golden report states the same 1,450 bbl both ways.  It was a genuine cross-source split, not an
#: unknown field; see ``test_knowledge_semantic_repair_v43.py``.
UNREGISTERED_FIELDS = (
    "torque_on_bit",
    "bit_ny",
    "shoe_test_pressure",
    "annular_velocity",
    "yield_point",
    "plastic_viscosity",
    "gel_strength_10s",
    "bit_size_nominal",
    "surface_pressure_reading",
    "pill_volume_remaining",
)


def test_unregistered_fields_keep_their_own_name_and_never_borrow_a_predicate() -> None:
    for field in UNREGISTERED_FIELDS:
        predicate, spec = predicate_for_field(field)
        assert predicate == field, (field, predicate)
        assert spec is None, (field, spec)
        assert field not in PREDICATE_BY_FIELD, field


def test_no_unregistered_field_reaches_a_predicate_by_losing_a_trailing_token() -> None:
    """The trap that swallowed MD and TVD.

    A name that differs from a registered predicate by one trailing token must not be reduced to it:
    stripping the token invents a property the source never stated.  ``surface_pressure_reading`` is
    not ``surface_pressure`` and ``bit_size_nominal`` is not ``bit_size`` - and appending a unit to
    an unknown name must leave it unknown rather than lending it somebody else's meaning.
    """
    for field in UNREGISTERED_FIELDS:
        assert predicate_for_field(field)[0] == field, field
        for token in ("ft", "psi", "bbl", "in", "ppg"):
            combined = f"{field}_{token}"
            assert predicate_for_field(combined)[1] is None, (field, token)
            assert predicate_for_field(combined)[0] == combined, (field, token)


def test_a_header_that_strips_its_unit_still_resolves_conservatively() -> None:
    """A documented gap, pinned so it stays known rather than becoming a surprise.

    ``predicate_for_field`` strips a parenthesised unit, so a header literally spelled
    ``Depth (ft MD)`` reduces to ``depth`` and lands on ``hole_depth``.  That is the general rule
    working as designed - and it is why the *extractor* emits ``depth_md``/``depth_tvd`` rather than
    relying on a header to carry the role.  A table whose only depth header is ``Depth (ft MD)``
    stays generic; widening this would mean guessing from a unit, which is prohibited.
    """
    assert predicate_for_field("Depth (ft MD)")[0] == "hole_depth"
    assert predicate_for_field("depth_md")[0] == "measured_depth"
    assert predicate_for_field("TD")[1] is None, "an abbreviation nobody registered stays unknown"


def test_the_extractor_records_why_a_value_was_typed() -> None:
    """An unqualified number is reported with its reason attached, never presented as certain."""
    hits = FieldExtractor().scan_text("600/300 rpm")
    assert hits
    assert all("inferred from the unit alone" in (hit.note or "") for hit in hits), hits


def test_a_labelled_value_is_not_reported_as_inferred() -> None:
    hits = FieldExtractor().scan_text("Rotary speed: 120 rpm")
    assert hits
    assert all("inferred from the unit alone" not in (hit.note or "") for hit in hits), hits


# ---------------------------------------------------------------------- contextual semantics
def test_the_rheometer_rule_needs_a_label_and_will_not_steal_a_rotary_speed() -> None:
    """The rule is gated on the instrument, and its unit is mandatory.

    Both matter.  An ungated rule would claim any ``300 rpm`` as a rheometer reading, and an optional
    unit lets the engine capture the first number of ``500/300 rpm`` and split one reading across two
    predicates - the mirror image of the bug being fixed.
    """
    extractor = FieldExtractor()

    assert [h.name for h in extractor.scan_text("Rheometer speed: 300 rpm")] == ["rheometer_speed"]
    assert {h.name for h in extractor.scan_text("500/300 rpm")} == {"rpm"}
    assert [h.name for h in extractor.scan_text("300 rpm")] == ["rpm"]


def test_an_unqualified_reading_stays_generic() -> None:
    """``600/300 rpm`` with no instrument named is not a rheometer reading, and is not called one.

    It is also not silently split into two assertions.  One reading is reported, as the generic
    ``rpm`` it is, carrying the note that says the label was absent - which is the honest answer, and
    the reason the conservative gap is a documented gap rather than a guess.
    """
    hits = FieldExtractor().scan_text("600/300 rpm")
    assert [hit.name for hit in hits] == ["rpm"], hits
    assert predicate_for_field(hits[0].name)[0] == "rpm"


def test_the_unit_is_never_silently_converted() -> None:
    """Separating quantities must not become a licence to normalise values."""
    hit = FieldExtractor().scan_text("Kick volume: 12 bbl")[0]
    assert (hit.value, hit.unit) == (12.0, "bbl")
    assert predicate_for_field(hit.name)[0] == "kick_volume"


# --------------------------------------------------------------------------- provenance
def test_a_separated_quantity_keeps_its_own_document_and_locator(workspace) -> None:
    """Splitting a predicate must not blur which document said what."""
    _ingest(workspace, "categories", CATEGORY_CORPUS)

    separated = {"sidpp", "sicp", "maasp", "pill_volume", "kick_volume", "rheometer_speed"}
    seen = set()
    for item in _items(workspace):
        if item.predicate in separated:
            seen.add(item.predicate)
            assert item.document_version_id, item.predicate
            assert item.lookup_key, item.predicate
            assert f"property:{item.predicate}" in item.lookup_key, item.lookup_key
            entries = item.provenance if isinstance(item.provenance, list) else [item.provenance]
            assert entries and entries[0].get("document_id"), item.predicate
    assert seen == separated, separated - seen
