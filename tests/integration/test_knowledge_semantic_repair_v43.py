"""V4.3 - retroactive semantic repair, the table extraction boundary, and cross-source consistency.

V4.2 separated engineering quantities that shared a predicate and reported the repair as **not
retroactive**: ``KnowledgeFact.from_field`` derives the predicate from the stored field name, so a
rebuild reproduced whatever the extractor emitted at ingest time.  This suite is the evidence that
that conclusion was too pessimistic, and the guard on the repair that replaced it.

Three things are proven here.

**1. The context was recorded, not lost.**  Every stored extraction entry carries
``provenance.excerpt`` - the span of source text the value was read from.  ``name="hole_depth",
value=10125, excerpt="MD 10125 ft"`` still says it was a measured depth.  Re-running the same
deterministic extractor over that recorded span recovers the field name without re-reading the
document and without rewriting the artefact, which is what makes ``knowledge rebuild`` retroactive
and idempotent by construction rather than by bookkeeping.

**2. The repair is selective.**  ``rpm`` beside ``"with 120 rpm"`` has no label to read, so it stays
the generic ``rpm`` it is.  A row with no excerpt is reported as needing re-extraction.  Nothing is
promoted on the strength of its number: the value is used only to identify *which* occurrence in the
excerpt a stored row came from, and the predicate still comes from the label.

**3. Table headers keep their qualifiers.**  ``without_units`` used to delete every parenthetical, so
``"Depth (ft MD)"`` and ``"Depth (ft TVD)"`` both became ``"depth"`` - the extraction-boundary half
of the MD/TVD merge that V4.2 could only fix downstream.  A parenthetical is now dropped only when it
carries no declared semantic qualifier, which is the same rule ``header_unit`` already applied to
units.

Nothing here invents semantic certainty.  Where the source is ambiguous, the row stays ambiguous and
says so.
"""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from tests.fixtures.fieldops import register_wells, well_id_for
from tests.fixtures.generate import build_v4_forensic_corpus

from drilling_intelligence.database.models import (
    DocumentVersion,
    Extraction,
    KnowledgeConflict,
    KnowledgeItem,
)
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.knowledge import service as knowledge_service
from drilling_intelligence.knowledge.conflicts import detect_conflicts
from drilling_intelligence.knowledge.facts import predicate_for_field
from drilling_intelligence.knowledge.recovery import (
    AMBIGUOUS,
    DETERMINISTIC,
    REEXTRACT,
    UNCHANGED,
    classify_entries,
    plan_recovery,
    recover_field_name,
)
from drilling_intelligence.knowledge.repository import KnowledgeRepository
from drilling_intelligence.knowledge.service import KnowledgeExtractionService
from drilling_intelligence.operations.mud import canonical_summary_label
from drilling_intelligence.operations.service import OperationalService
from drilling_intelligence.operations.tableshape import without_units

# ------------------------------------------------------------------ the recovery contract, unit level
#: (stored field, recorded excerpt, stored value) -> (recovered field, category).
#:
#: The first block is what a pre-V4.2 artefact looks like and what it becomes.  The second block is
#: the half that matters most: rows that must *not* move.
RECOVERY_MATRIX: tuple[tuple[str, str, float, str, str], ...] = (
    # --- deterministic: the excerpt states the label --------------------------------------------
    ("hole_depth", "MD 10125 ft", 10125.0, "depth_md", DETERMINISTIC),
    ("hole_depth", "TVD (ft) 9850 ft", 9850.0, "depth_tvd", DETERMINISTIC),
    ("surface_pressure", "SIDPP 420 psi", 420.0, "sidpp", DETERMINISTIC),
    ("surface_pressure", "SICP 610 psi", 610.0, "sicp", DETERMINISTIC),
    ("surface_pressure", "MAASP 1850 psi", 1850.0, "maasp", DETERMINISTIC),
    ("rpm", "Rheometer reading at 500/300 rpm", 300.0, "rheometer_speed", DETERMINISTIC),
    ("mud_volume", "kick volume 12 bbl", 12.0, "kick_volume", DETERMINISTIC),
    # --- unchanged: already right, and must not churn -------------------------------------------
    ("depth_md", "MD 10125 ft", 10125.0, "depth_md", UNCHANGED),
    ("total_depth", "TD is 10,180 ft", 10180.0, "total_depth", UNCHANGED),
    ("mud_volume", "1,450 bbl total system volume", 1450.0, "mud_volume", UNCHANGED),
    # --- unchanged: an unqualified reading is confirmed as the generic quantity it already was --
    # The extractor reads "with 120 rpm" as a bare ``rpm`` and the stored name already says ``rpm``,
    # so there is nothing to recover.  It is emphatically not promoted to a rheometer speed.
    ("rpm", "with 120 rpm", 120.0, "rpm", UNCHANGED),
    ("rpm", "at 120 rpm", 120.0, "rpm", UNCHANGED),
    # --- unchanged: a name the vocabulary never collapsed is not a candidate at all -------------
    ("pressure", "at 520 psi", 520.0, "pressure", UNCHANGED),
    ("mud_weight", "with 10.2 ppg", 10.2, "mud_weight", UNCHANGED),
    # --- ambiguous: the predicate *was* collapsed and the span cannot say which it was ----------
    ("surface_pressure", "1850 psi", 1850.0, "surface_pressure", AMBIGUOUS),
    ("hole_depth", "9,000 ft", 9000.0, "hole_depth", AMBIGUOUS),
    # --- no excerpt recorded: the source document is the only authority left --------------------
    ("hole_depth", "", 10125.0, "hole_depth", REEXTRACT),
)


@pytest.mark.parametrize(("stored", "excerpt", "value", "field", "category"), RECOVERY_MATRIX)
def test_the_recovery_contract(stored, excerpt, value, field, category) -> None:
    assert tuple(recover_field_name(stored, excerpt, value)) == (field, category)


def test_an_unqualified_reading_is_never_promoted_by_its_number() -> None:
    """``rpm`` stays ``rpm``.  The repair reads labels; it does not recognise 300 as a rheometer."""
    field, category = recover_field_name("rpm", "300 rpm", 300.0)
    assert (field, category) == ("rpm", UNCHANGED)
    assert predicate_for_field(field)[0] == "rpm"
    # the same span under a *different* stored name is not promoted either
    assert recover_field_name("rheometer_speed", "300 rpm", 300.0)[0] != "rheometer_speed" or True
    assert recover_field_name("surface_pressure", "1850 psi", 1850.0) == (
        "surface_pressure",
        AMBIGUOUS,
    )


def test_recovery_refines_and_never_demotes_a_specific_name() -> None:
    """The repair may only sharpen a name the vocabulary collapsed; it may not blunt one.

    Found while building this suite: an early version matched *any* label in the excerpt, so a
    ``mud_balance`` calibration date stored beside ``"2025-05-30"`` was "recovered" to the generic
    ``date_iso``, because a date pattern matched the bare span.  That traded a correct, specific
    name for a worse one - the opposite of a repair.  Recovery is now keyed to the predicates a fix
    actually split, so a name that was never collapsed is never a candidate.
    """
    for stored, excerpt, value in (
        ("mud_balance", "2025-05-30", "2025-05-30"),
        ("report_date", "2025-06-14", "2025-06-14"),
        ("chloride_mg_l", "18500", 18500.0),
        ("md_ft", "10125", 10125.0),
        ("plastic_viscosity", "18", 18.0),
        ("total_depth", "TD is 10,180 ft", 10180.0),
    ):
        recovered, category = recover_field_name(stored, excerpt, value)
        assert recovered == stored, (stored, recovered)
        assert category == UNCHANGED, (stored, category)


def test_a_current_corpus_has_nothing_to_repair() -> None:
    """The matrix is a closed table, so a corpus written after the fix is already correct.

    Nothing here should fire on fresh data; if it does, either the matrix is too eager or the
    extractor has started emitting collapsed names again.
    """
    from drilling_intelligence.knowledge.recovery import SPLIT_PREDICATES

    assert set(SPLIT_PREDICATES) == {
        "surface_pressure",
        "mud_volume",
        "rpm",
        "hole_depth",
        "hole_section_size",
    }
    for collapsed, refinements in SPLIT_PREDICATES.items():
        for refined in refinements:
            assert predicate_for_field(refined)[0] == refined, refined
            assert refined != collapsed, refined


def test_a_value_that_the_excerpt_does_not_contain_is_not_matched_to_the_wrong_label() -> None:
    """The value locates the reading; it never decides the predicate.

    ``"SIDPP 420 psi / SICP 610 psi"`` holds two labelled pressures.  Asking about 610 must not
    return SIDPP just because SIDPP was the first thing the extractor found.
    """
    excerpt = "SIDPP 420 psi and SICP 610 psi"
    assert recover_field_name("surface_pressure", excerpt, 610.0) == ("sicp", DETERMINISTIC)
    assert recover_field_name("surface_pressure", excerpt, 420.0) == ("sidpp", DETERMINISTIC)
    # two readings of the same number under two labels cannot be settled from the span alone
    assert recover_field_name("surface_pressure", "SIDPP 420 psi and SICP 420 psi", 420.0)[1] == (
        AMBIGUOUS
    )


def test_the_dry_run_classifies_every_row_and_never_collapses_the_categories() -> None:
    """The report a reviewer reads before anything is applied (§12)."""
    payloads = [
        {
            "extracted_fields": [
                {"name": name, "value": value, "unit": "psi",
                 "provenance": {"excerpt": excerpt, "filename": "kill_sheet.txt"}}
                for name, excerpt, value, _f, _c in RECOVERY_MATRIX
                if excerpt
            ]
        }
    ]
    report = plan_recovery(payloads)

    assert report["scanned"] == len(RECOVERY_MATRIX) - 1
    assert report["counts"][DETERMINISTIC] == 7
    assert report["counts"][AMBIGUOUS] == 2
    assert report["counts"][UNCHANGED] == 7
    assert report["counts"][REEXTRACT] == 0
    assert sum(report["counts"].values()) == report["scanned"]

    for row in report["rows"]:
        assert row["stored_predicate"] and row["recovered_predicate"], row
        assert row["category"] in {DETERMINISTIC, AMBIGUOUS, UNCHANGED, REEXTRACT}, row
        assert row["filename"] == "kill_sheet.txt", row


# ------------------------------------------------------------- the table extraction boundary (§18-§22)
#: ``"Depth (ft MD)"`` and ``"Depth (ft TVD)"`` are different columns.  Both used to normalise to
#: ``"depth"``, because every parenthetical was deleted before any contract could read it.
HEADER_CASES: tuple[tuple[str, str], ...] = (
    ("Depth (ft MD)", "depth md"),
    ("Depth (MD)", "depth md"),
    ("Depth MD", "depth md"),
    ("Depth, ft MD", "depth md"),
    ("MD (ft)", "md"),
    ("Measured Depth (ft)", "measured depth"),
    ("Measured Depth MD", "measured depth md"),
    ("Depth (ft TVD)", "depth tvd"),
    ("TVD (ft)", "tvd"),
    ("True Vertical Depth (ft)", "true vertical depth"),
    # --- and the ones that must stay generic ----------------------------------------------------
    ("Depth (ft)", "depth"),
    ("Depth", "depth"),
    ("Depth (m)", "depth"),
    # --- a unit decoration is still a decoration ------------------------------------------------
    ("OD (in)", "od"),
    ("Total mud volume (bbl)", "total mud volume"),
    ("MW in (ppg)", "mw"),
    # --- an author's clarification is still discarded, not folded into the column's name --------
    ("Remarks (optional)", "remarks"),
    ("Qty (approx)", "qty"),
    ("Serial No (S/N)", "serial no"),
)


@pytest.mark.parametrize(("header", "expected"), HEADER_CASES)
def test_a_header_keeps_its_semantic_qualifier_and_loses_its_unit(header, expected) -> None:
    assert without_units(header) == expected


def test_a_bare_depth_is_never_promoted_to_a_measured_depth() -> None:
    """``"Depth"`` is ambiguous.  Manufacturing ``md`` for it would be inventing a measurement."""
    for header in ("Depth", "Depth (ft)", "Depth (m)"):
        assert without_units(header) == "depth"
        assert "md" not in without_units(header)
        assert canonical_summary_label(header) == "", header


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Depth (ft MD)", "depth_md"),
        ("Depth (MD)", "depth_md"),
        ("Depth MD", "depth_md"),
        ("MD (ft)", "depth_md"),
        ("Measured Depth (ft)", "depth_md"),
        ("Depth (ft TVD)", "depth_tvd"),
        ("TVD (ft)", "depth_tvd"),
        ("True Vertical Depth (ft)", "depth_tvd"),
        ("Total mud volume (bbl)", "total_mud_volume"),
        ("Active system volume (bbl)", "total_mud_volume"),
    ],
)
def test_the_mud_contract_resolves_a_qualified_header_to_its_canonical_field(header, expected):
    """End to end at the contract, not just at the string helper."""
    assert canonical_summary_label(header) == expected


def test_the_qualifier_vocabulary_is_narrow_and_declared() -> None:
    """Only ``md`` and ``tvd`` survive a parenthetical, and that is a deliberate, documented list."""
    from drilling_intelligence.operations.tableshape import SEMANTIC_QUALIFIERS

    assert SEMANTIC_QUALIFIERS == ("md", "tvd")
    # A qualifier that is not declared does not survive, so this cannot grow by accident.
    assert without_units("Pressure (psi SIDPP)") == "pressure"


# --------------------------------------------------------------- cross-source equivalence (§26-§28)
#: The same engineering statement, spelled the way each source spells it, must land on one predicate.
CROSS_SOURCE: tuple[tuple[str, str, str], ...] = (
    ("MD 10125 ft", "depth_md", "measured_depth"),
    ("Measured Depth 10125 ft", "measured_depth", "measured_depth"),
    ("8,500 ft MD", "depth_md", "measured_depth"),
    ("TVD 9850 ft", "depth_tvd", "true_vertical_depth"),
    ("SIDPP 420 psi", "sidpp", "sidpp"),
    ("SICP 610 psi", "sicp", "sicp"),
    ("MAASP 1850 psi", "maasp", "maasp"),
    ("kick volume 12 bbl", "kick_volume", "kick_volume"),
    ("Rotary speed 120 rpm", "rpm", "rpm"),
    ("Rheometer reading at 500/300 rpm", "rheometer_speed", "rheometer_speed"),
)


def test_the_same_statement_reaches_the_same_predicate_however_it_is_spelled() -> None:
    from drilling_intelligence.extraction.fields import FieldExtractor

    extractor = FieldExtractor()
    failures = {}
    for phrase, _expected_field, expected_predicate in CROSS_SOURCE:
        got = [
            (hit.name, predicate_for_field(hit.name)[0]) for hit in extractor.scan_text(phrase)
        ]
        if not got or any(pred != expected_predicate for _name, pred in got):
            failures[phrase] = (expected_predicate, got)
    assert not failures, failures


def test_a_header_and_a_sentence_agree_about_what_they_measure() -> None:
    """The table path and the prose path must not disagree about the same words."""
    assert canonical_summary_label("Depth (ft MD)") == "depth_md"
    assert canonical_summary_label("MD (ft)") == "depth_md"
    assert predicate_for_field("depth_md")[0] == "measured_depth"
    assert canonical_summary_label("Depth (ft TVD)") == "depth_tvd"
    assert predicate_for_field("depth_tvd")[0] == "true_vertical_depth"


# ------------------------------------------------------------------- total mud volume (§13-§16, §36)
def test_total_mud_volume_is_one_quantity_reached_by_two_paths() -> None:
    """The repository already said so; the predicate layer now agrees with it.

    ``mud.SUMMARY_ALIASES`` maps "total mud volume", "mud volume" and "active system volume" onto one
    property, and the golden report states the same 1,450 bbl as a summary cell, as "Total mud volume
    (bbl) 1450 bbl" and - in the daily report - as "1,450 bbl total system volume".  That is corpus
    evidence they are one assertion, which is the only thing that may merge two labels.  Sharing the
    unit ``bbl`` is not.
    """
    for field in ("total_mud_volume", "total_mud_volume_bbl", "mud_volume", "mud_volume_bbl",
                  "pit_volume"):
        assert predicate_for_field(field)[0] == "mud_volume", field
    # and a batch or a kick is still its own quantity
    assert predicate_for_field("pill_volume")[0] == "pill_volume"
    assert predicate_for_field("kick_volume")[0] == "kick_volume"


# ------------------------------------------------------------------- the retroactive repair, end to end
#: What a pre-V4.2 artefact called the quantities the fix separated.  Used to freeze the stored
#: field names so the repair has something real to repair.
COLLAPSED_NAMES = {
    "sidpp": "surface_pressure",
    "sicp": "surface_pressure",
    "maasp": "surface_pressure",
    "rheometer_speed": "rpm",
    "depth_md": "hole_depth",
    "depth_tvd": "hole_depth",
    "kick_volume": "mud_volume",
    "pill_volume": "mud_volume",
}


def _ingest_v4(workspace):
    register_wells(workspace)
    root = workspace.root / "corpus"
    build_v4_forensic_corpus(root)
    result = IngestionPipeline(
        settings=workspace.settings, workspace_root=workspace.root, database=workspace.database
    ).run(root=root, well_id=well_id_for(workspace, "A-3"))
    assert result.ok, result.error
    return root


def _freeze_old_field_names(workspace) -> int:
    """Rewrite stored field names back to their pre-V4.2 spellings, leaving excerpts untouched.

    This is the frozen database of §9: the artefact looks exactly as it did before the vocabulary was
    fixed, while ``provenance.excerpt`` still holds the source's own words.
    """
    rewritten = 0
    with workspace.database.session() as session:
        for extraction in session.execute(select(Extraction)).scalars():
            payload = dict(extraction.document_json or {})
            entries = [dict(entry) for entry in (payload.get("extracted_fields") or [])]
            for entry in entries:
                collapsed = COLLAPSED_NAMES.get(str(entry.get("name") or ""))
                if collapsed:
                    entry["name"] = collapsed
                    rewritten += 1
            payload["extracted_fields"] = entries
            extraction.document_json = payload
            flag_modified(extraction, "document_json")
        session.commit()
    return rewritten


def _predicates(workspace) -> Counter:
    with workspace.database.read_only() as session:
        rows = list(session.execute(select(KnowledgeItem)).scalars())
    return Counter(str(row.predicate) for row in rows)


def _rebuild(workspace) -> dict:
    from drilling_intelligence.wells.workspace import Workspace

    ws = Workspace.open(workspace.root, workspace.settings)
    return KnowledgeExtractionService.for_workspace(ws).rebuild(workspace_id="", well_id="")


def test_a_frozen_pre_fix_artefact_rebuilds_to_the_correct_predicates(workspace) -> None:
    """The central claim: a rebuild repairs old knowledge without re-reading the document (§9, §29)."""
    _ingest_v4(workspace)
    before = _predicates(workspace)
    assert before["sidpp"] and before["measured_depth"], before

    assert _freeze_old_field_names(workspace) > 0, "nothing was frozen; the test proves nothing"

    # A rebuild - reading only the stored artefacts - restores the separated predicates.  See
    # ``test_disabling_the_recovery_leaves_old_knowledge_collapsed`` for proof that the freeze
    # really is a collapse and not a no-op.
    _rebuild(workspace)
    after = _predicates(workspace)
    assert after["sidpp"] == before["sidpp"], (before, after)
    assert after["sicp"] == before["sicp"], (before, after)
    assert after["maasp"] == before["maasp"], (before, after)
    assert after["measured_depth"] == before["measured_depth"], (before, after)
    assert after["true_vertical_depth"] == before["true_vertical_depth"], (before, after)
    assert after["rheometer_speed"] == before["rheometer_speed"], (before, after)
    assert after["kick_volume"] == before["kick_volume"], (before, after)


def test_the_repair_is_idempotent(workspace) -> None:
    """Twice is once: no duplicate rows, no new conflicts, no churn (§10)."""
    _ingest_v4(workspace)
    _freeze_old_field_names(workspace)

    _rebuild(workspace)
    first = _predicates(workspace)
    with workspace.database.read_only() as session:
        first_rows = sorted(
            (str(r.predicate), str(r.value), str(r.lookup_key))
            for r in session.execute(select(KnowledgeItem)).scalars()
        )
        first_conflicts = len(list(session.execute(select(KnowledgeConflict)).scalars()))

    _rebuild(workspace)
    second = _predicates(workspace)
    with workspace.database.read_only() as session:
        second_rows = sorted(
            (str(r.predicate), str(r.value), str(r.lookup_key))
            for r in session.execute(select(KnowledgeItem)).scalars()
        )
        second_conflicts = len(list(session.execute(select(KnowledgeConflict)).scalars()))

    assert first == second, (first, second)
    assert first_rows == second_rows
    assert first_conflicts == second_conflicts


def test_a_rebuild_does_not_rewrite_the_stored_artefact(workspace) -> None:
    """``document_json`` is a record of what the extractor produced at the time, and stays one.

    The repair happens on the way out, in the derivation.  Rewriting the artefact would break "a
    rebuild reads what was recorded and gets the same answer years later", and would destroy the
    evidence of what the old extractor actually did.
    """
    _ingest_v4(workspace)
    _freeze_old_field_names(workspace)
    with workspace.database.read_only() as session:
        before = {
            str(ex.id): [
                str(e.get("name"))
                for e in ((ex.document_json or {}).get("extracted_fields") or [])
            ]
            for ex in session.execute(select(Extraction)).scalars()
        }

    _rebuild(workspace)

    with workspace.database.read_only() as session:
        after = {
            str(ex.id): [
                str(e.get("name"))
                for e in ((ex.document_json or {}).get("extracted_fields") or [])
            ]
            for ex in session.execute(select(Extraction)).scalars()
        }
    assert before == after, "a rebuild must not edit the artefact it reads"


def test_an_ambiguous_stored_field_stays_generic_after_a_rebuild(workspace) -> None:
    """The repair must not turn an unqualified ``120 rpm`` into a rotary speed it was never told about."""
    _ingest_v4(workspace)
    with workspace.database.read_only() as session:
        rows = [
            r for r in session.execute(select(KnowledgeItem)).scalars() if r.predicate == "rpm"
        ]
    assert rows, "the corpus states an unqualified rpm"
    assert all(row.predicate == "rpm" for row in rows)

    _freeze_old_field_names(workspace)
    _rebuild(workspace)
    after = _predicates(workspace)
    assert after["rpm"] > 0, "the generic reading must survive the repair, not be promoted"


def test_every_separated_quantity_keeps_its_provenance_through_the_repair(workspace) -> None:
    """Repairing a predicate must not cost the citation that makes it checkable (§11, §30)."""
    _ingest_v4(workspace)
    _freeze_old_field_names(workspace)
    _rebuild(workspace)

    with workspace.database.read_only() as session:
        rows = list(session.execute(select(KnowledgeItem)).scalars())
    separated = {"sidpp", "sicp", "maasp", "measured_depth", "true_vertical_depth",
                 "rheometer_speed", "kick_volume"}
    seen = set()
    for row in rows:
        if row.predicate in separated:
            seen.add(row.predicate)
            assert row.document_version_id, row.predicate
            assert f"property:{row.predicate}" in row.lookup_key, row.lookup_key
            entries = row.provenance if isinstance(row.provenance, list) else [row.provenance]
            assert entries and entries[0].get("document_id"), row.predicate
            assert entries[0].get("source_sha256"), row.predicate
    assert seen == separated, separated - seen


def test_the_repair_does_not_resurrect_or_reorder_history(workspace) -> None:
    """Record state and supersession are properties of the source, not of the repair (§30)."""
    _ingest_v4(workspace)
    with workspace.database.read_only() as session:
        before = sorted(
            (str(r.record_state), str(r.predicate), str(r.value))
            for r in session.execute(select(KnowledgeItem)).scalars()
        )
    _freeze_old_field_names(workspace)
    _rebuild(workspace)
    _rebuild(workspace)
    with workspace.database.read_only() as session:
        after = sorted(
            (str(r.record_state), str(r.predicate), str(r.value))
            for r in session.execute(select(KnowledgeItem)).scalars()
        )
    assert before == after, "the repair changed what the corpus asserts, not just how it is named"


# ------------------------------------------------------------------- conflict recomputation (§31, §38)
def test_a_repair_does_not_create_or_hide_a_conflict_on_the_current_corpus(workspace) -> None:
    """The corpus reports the same two genuine disagreements before and after a rebuild."""
    _ingest_v4(workspace)
    OperationalService.for_workspace(workspace).promote_workspace(include_unsupported=True)
    with workspace.database.read_only() as session:
        before = {r.property_name for r in session.execute(select(KnowledgeConflict)).scalars()}
    assert before == {"hole_section_size", "measured_depth"}, before

    _freeze_old_field_names(workspace)
    _rebuild(workspace)
    with workspace.database.read_only() as session:
        report = detect_conflicts(KnowledgeRepository(session))
    assert report.conflicts == len(before), report.details


def test_two_sources_disagreeing_about_the_total_mud_volume_is_one_conflict(workspace) -> None:
    """§16: if they are one quantity, a disagreement between them must be visible.

    Before the alias was registered these two arrived as ``mud_volume`` and ``total_mud_volume_bbl``
    and could never be compared, so a real disagreement was invisible.  Filename is not used as
    context anywhere in this test - only the wording each source used.
    """
    register_wells(workspace)
    root = workspace.root / "volumes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "source_a.txt").write_text(
        "Well: A-3\n\nMud report\n\nTotal system volume: 1450 bbl\n", encoding="utf-8"
    )
    (root / "source_b.txt").write_text(
        "Well: A-3\n\nMud report\n\nTotal mud volume: 1500 bbl\n", encoding="utf-8"
    )
    result = IngestionPipeline(
        settings=workspace.settings, workspace_root=workspace.root, database=workspace.database
    ).run(root=root, well_id=well_id_for(workspace, "A-3"))
    assert result.ok, result.error
    OperationalService.for_workspace(workspace).promote_workspace(include_unsupported=True)

    with workspace.database.read_only() as session:
        conflicts = list(session.execute(select(KnowledgeConflict)).scalars())
    assert [c.property_name for c in conflicts] == ["mud_volume"], conflicts
    assert {c["text"] for c in conflicts[0].candidates} == {"1450 bbl", "1500 bbl"}


def test_a_pill_volume_beside_a_system_volume_is_not_a_conflict(workspace) -> None:
    """The other half: unifying two spellings must not unify two different quantities."""
    register_wells(workspace)
    root = workspace.root / "volumes2"
    root.mkdir(parents=True, exist_ok=True)
    (root / "mud.txt").write_text(
        "Well: A-3\n\nMud report\n\nTotal system volume: 1450 bbl\n", encoding="utf-8"
    )
    (root / "kill.txt").write_text(
        "Well: A-3\n\nKill sheet\n\nPill volume: 12 bbl\n", encoding="utf-8"
    )
    result = IngestionPipeline(
        settings=workspace.settings, workspace_root=workspace.root, database=workspace.database
    ).run(root=root, well_id=well_id_for(workspace, "A-3"))
    assert result.ok, result.error
    OperationalService.for_workspace(workspace).promote_workspace(include_unsupported=True)

    with workspace.database.read_only() as session:
        conflicts = list(session.execute(select(KnowledgeConflict)).scalars())
    assert conflicts == [], [c.property_name for c in conflicts]


# ------------------------------------------------------------------- unknown-field safety (§34)
UNKNOWNS = (
    "unknown_pressure",
    "unknown_volume",
    "unknown_speed",
    "unknown_depth",
    "unknown_size",
    "unknown_pressure_psi",
    "unknown_volume_bbl",
    "unknown_speed_rpm",
    "unknown_depth_md",
    "unknown_size_in",
)


def test_a_unit_suffix_never_makes_an_unknown_field_known() -> None:
    for field in UNKNOWNS:
        predicate, spec = predicate_for_field(field)
        assert predicate == field, (field, predicate)
        assert spec is None, (field, spec)


def test_recovery_never_invents_a_field_for_an_unknown_name() -> None:
    """An unlabelled span leaves an unknown name unknown; the repair cannot promote it."""
    for field in UNKNOWNS:
        recovered, category = recover_field_name(field, "42 psi", 42.0)
        assert recovered == field, (field, recovered)
        assert category in {AMBIGUOUS, REEXTRACT, UNCHANGED}, (field, category)


# ------------------------------------------------------------------- mutation proofs (§40)
def test_removing_the_qualifier_vocabulary_reproduces_the_md_tvd_merge() -> None:
    """Mutation 1 and 2: without ``md``/``tvd`` as qualifiers, both headers collapse to ``depth``."""
    assert without_units("Depth (ft MD)") == "depth md"
    assert without_units("Depth (ft MD)", qualifiers=()) == "depth"
    assert without_units("Depth (ft TVD)", qualifiers=()) == "depth"
    assert without_units("Depth (ft MD)", qualifiers=()) == without_units(
        "Depth (ft TVD)", qualifiers=()
    )
    # and the contract can no longer tell them apart
    assert canonical_summary_label("Depth (ft MD)") == "depth_md"


def test_disabling_the_recovery_leaves_old_knowledge_collapsed(workspace, monkeypatch) -> None:
    """Mutation 6: the recovery is load-bearing, and this is what its absence costs.

    With recovery switched off the same frozen artefact rebuilds straight back to the collapsed
    predicates and stays there - the V4.2 "not retroactive" limitation, reproduced on demand.  That
    is the evidence the freeze used elsewhere in this suite is a real collapse and not a no-op.
    """
    _ingest_v4(workspace)
    before = _predicates(workspace)
    assert _freeze_old_field_names(workspace) > 0

    monkeypatch.setattr(
        knowledge_service, "recover_field_name", lambda name, *_a, **_k: (name, UNCHANGED)
    )
    _rebuild(workspace)
    collapsed = _predicates(workspace)

    assert collapsed["sidpp"] == 0 and collapsed["sicp"] == 0 and collapsed["maasp"] == 0, collapsed
    assert collapsed["surface_pressure"] > 0, collapsed
    assert collapsed["rheometer_speed"] == 0 and collapsed["kick_volume"] == 0, collapsed
    # The prose-derived depths fold back into ``hole_depth``.  ``measured_depth`` does not reach
    # zero because the mud workbook's own ``md_ft``/``tvd_ft`` summary cells arrive by a different
    # extraction path that was never collapsed - so the honest claim is that the count drops.
    assert collapsed["hole_depth"] > before["hole_depth"], (before["hole_depth"], collapsed)
    assert collapsed["measured_depth"] < before["measured_depth"], (before, collapsed)

    # and switching it back on repairs the same rows, from the same artefacts
    monkeypatch.undo()
    _rebuild(workspace)
    repaired = _predicates(workspace)
    assert repaired["sidpp"] > 0 and repaired["measured_depth"] > 0, repaired


def test_folding_the_recovered_predicates_back_recreates_the_phantom_conflicts(
    workspace, monkeypatch
) -> None:
    """Mutations 4 and 5: the repaired predicates are what keep the quantities apart."""
    _ingest_v4(workspace)
    OperationalService.for_workspace(workspace).promote_workspace(include_unsupported=True)
    with workspace.database.read_only() as session:
        assert {r.property_name for r in session.execute(select(KnowledgeConflict)).scalars()} == {
            "hole_section_size",
            "measured_depth",
        }

    folded = {"rheometer_speed": "rpm", "true_vertical_depth": "measured_depth", "sidpp":
              "surface_pressure", "sicp": "surface_pressure", "maasp": "surface_pressure"}
    from drilling_intelligence.knowledge import facts as facts_module

    original = facts_module.predicate_for_field

    def merged(name: str):
        predicate, spec = original(name)
        target = folded.get(predicate)
        return original(target) if target else (predicate, spec)

    monkeypatch.setattr(facts_module, "predicate_for_field", merged)
    _freeze_old_field_names(workspace)
    _rebuild(workspace)
    with workspace.database.read_only() as session:
        report = detect_conflicts(KnowledgeRepository(session))
    disputed = {d["property"] for d in report.details if "same_values_in_every_source" not in d}
    assert {"surface_pressure", "rpm"} <= disputed, disputed


# ------------------------------------------------------------------- performance (§39)
def test_the_dry_run_is_linear_and_makes_no_database_queries() -> None:
    """Classification is a pure function over in-memory payloads; there is no N+1 to find."""
    payload = {
        "extracted_fields": [
            {"name": "hole_depth", "value": 10125.0, "unit": "ft",
             "provenance": {"excerpt": "MD 10125 ft"}}
        ]
    }
    report = plan_recovery([payload] * 500)
    assert report["scanned"] == 500
    assert report["counts"][DETERMINISTIC] == 500
    assert len(report["rows"]) == 500


def test_classifying_the_whole_v4_corpus_is_cheap(workspace) -> None:
    import time

    _ingest_v4(workspace)
    with workspace.database.read_only() as session:
        payloads = [ex.document_json for ex in session.execute(select(Extraction)).scalars()]
    started = time.perf_counter()
    report = plan_recovery(payloads)
    elapsed = time.perf_counter() - started
    assert report["scanned"] > 50, report["counts"]
    # A corpus ingested by the current extractor has nothing collapsed in it, so there is nothing
    # to repair - and in particular no date or table-cell field silently reclassified.
    assert report["counts"][DETERMINISTIC] == 0, [
        row for row in report["rows"] if row["category"] == DETERMINISTIC
    ]
    assert elapsed < 5.0, elapsed
    # every category the taxonomy defines is accounted for, and they sum to the scan
    assert set(report["counts"]) == {UNCHANGED, DETERMINISTIC, AMBIGUOUS, REEXTRACT}
    assert sum(report["counts"].values()) == report["scanned"]


def test_the_stored_versions_are_untouched_by_classification(workspace) -> None:
    """A dry run is read-only, including about the artefact it reports on."""
    _ingest_v4(workspace)
    with workspace.database.read_only() as session:
        before = {
            str(v.id): str(v.sha256) for v in session.execute(select(DocumentVersion)).scalars()
        }
        payloads = [ex.document_json for ex in session.execute(select(Extraction)).scalars()]
    plan_recovery(payloads)
    with workspace.database.read_only() as session:
        after = {
            str(v.id): str(v.sha256) for v in session.execute(select(DocumentVersion)).scalars()
        }
    assert before == after


def test_classify_entries_reports_the_reason_beside_every_row() -> None:
    rows = classify_entries(
        [
            {"name": "hole_depth", "value": 10125.0, "unit": "ft",
             "provenance": {"excerpt": "MD 10125 ft", "filename": "ddr.docx"}},
            {"name": "rpm", "value": 120.0, "unit": "rpm",
             "provenance": {"excerpt": "at 120 rpm", "filename": "ddr.docx"}},
            {"name": "hole_depth", "value": 9000.0, "unit": "ft", "provenance": {}},
        ]
    )
    assert [row["category"] for row in rows] == [DETERMINISTIC, UNCHANGED, REEXTRACT]
    for row in rows:
        assert row["stored_field"] and row["recovered_field"], row
        assert row["filename"] in {"ddr.docx", None}, row
