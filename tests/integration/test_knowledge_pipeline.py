"""The derivation pipeline: artefact in, cited knowledge out - and nothing else.

These tests run the service against real documents in a real database, which is the only way to
pin the properties the layer is defined by:

*   knowledge is derived from the *stored artefact*, so a rebuild years later answers the same
    question the same way and never re-runs a parser;
*   a value that cannot cite its source is reported, not stored and not swallowed;
*   what a document asserts belongs to the entity it describes - and an entity that needs a row of
    its own is never invented to hold it;
*   the search sidecar is written from the same facts and is disposable.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from tests.fixtures.knowledge import artefact_field, register_artefact

from drilling_intelligence.core.enums import KnowledgeOrigin, KnowledgeStatus
from drilling_intelligence.database.models import KnowledgeItem, Well
from drilling_intelligence.knowledge.entities import EntityRef
from drilling_intelligence.knowledge.facts import KnowledgeFact
from drilling_intelligence.knowledge.repository import KnowledgeRepository
from drilling_intelligence.knowledge.service import KnowledgeExtractionService
from drilling_intelligence.search.chunking import KIND_KNOWLEDGE


def service_for(db) -> KnowledgeExtractionService:
    """A service with no index; every write below borrows the test's uncommitted session.

    ``index=None`` is deliberate for most of these tests: the derivation and the sidecar are
    separate promises, and a failure to index must never look like a failure to know.
    """
    return KnowledgeExtractionService(database=db, index=None, refresh_index=False)


def test_knowledge_is_derived_from_the_stored_artefact(db, session, well) -> None:
    document, version, _payload = register_artefact(
        session,
        well_id=well.id,
        fields=(
            artefact_field("mud_weight", "10.2", "ppg", cell="B9"),
            artefact_field("plastic_viscosity", "18", "cP", cell="B10"),
            artefact_field("report_date", "2025-06-14", "", cell="B4"),
        ),
    )
    from drilling_intelligence.documents.repository import DocumentRepository

    before = DocumentRepository(session).extraction_for_version(version.id)
    artefact_before = json.dumps(before.document_json, sort_keys=True)
    service = service_for(db)
    result = service.sync_version(document.id, version.id, session=session)
    assert result.facts["created"] == 3
    assert result.fields_seen == 3
    assert result.skipped == []
    assert result.warnings == []
    repository = KnowledgeRepository(session)
    facts = repository.facts_for_version(version.id)
    assert {fact.predicate for fact in facts} == {"mud_weight", "plastic_viscosity", "report_date"}
    for fact in facts:
        assert fact.provenance is not None, "a derived fact without provenance is a bug, not a gap"
        assert fact.document_id == document.id
        assert fact.document_version_id == version.id
        assert fact.origin == KnowledgeOrigin.EXTRACTED.value
        # The subject is the well the document is filed under - not the file, not the workspace.
        assert fact.subject.entity_type == "well"
        assert fact.subject.entity_id == well.id
    mud = next(fact for fact in facts if fact.predicate == "mud_weight")
    assert mud.citation().endswith("Cell: B9")
    assert mud.original_value == "10.2"
    assert mud.status == KnowledgeStatus.ACTIVE.value
    # A date field is knowledge about when the measurement was valid, not a quantity.
    report_date = next(fact for fact in facts if fact.predicate == "report_date")
    assert report_date.value_type == "date"
    assert report_date.normalized_unit == ""
    assert report_date.valid_from is not None
    # Nothing about the artefact was rewritten: derivation reads it, it does not re-extract.
    after = DocumentRepository(session).extraction_for_version(version.id)
    assert json.dumps(after.document_json, sort_keys=True) == artefact_before
    assert after.id == before.id


def test_the_revision_a_fact_came_from_is_the_registrys_number(db, session, well) -> None:
    """``revision`` decides listing order, so it is taken from the version row, not the payload.

    Two statements of the same property from two revisions must sort newest-first even when the
    older one was written last (a back-fill, a repair run) - trusting the timestamp for that would
    make the answer depend on when someone happened to re-run a command.
    """
    document, first, _payload = register_artefact(
        session, well_id=well.id, fields=(artefact_field("mud_weight", "10.2", "ppg", cell="B9"),)
    )
    second = register_artefact(
        session,
        well_id=well.id,
        fields=(artefact_field("mud_weight", "10.4", "ppg", cell="B9"),),
        version_number=2,
        supersedes=first,
        revision="Rev 2",
    )[1]
    service = service_for(db)
    service.sync_version(document.id, second.id, session=session)
    service.sync_version(document.id, first.id, session=session)  # deliberately the older one last
    repository = KnowledgeRepository(session)
    by_version = {
        fact.document_version_id: fact
        for fact in repository.facts_for_well(well.id, include_superseded=True)
    }
    assert by_version[second.id].revision == 2
    assert by_version[first.id].revision == 1
    assert [fact.item_id for fact in repository.facts_for_well(well.id)] == [
        by_version[second.id].item_id
    ]
    assert repository.facts_for_well(well.id)[0].original_value == "10.4"


def test_a_value_that_cannot_be_read_is_kept_and_explained(db, session, well) -> None:
    """A number that is not a number becomes a candidate with a reason, never an error and never a zero.

    The extractor is allowed to be wrong about a cell; the knowledge layer is not allowed to make
    that wrongness invisible, and it is not allowed to abort the whole document because of it.
    """
    document, version, _payload = register_artefact(
        session,
        well_id=well.id,
        fields=(
            artefact_field("mud_weight", "as received", "ppg", cell="B9", quality="SUSPECT"),
            artefact_field("plastic_viscosity", "", "", cell="B10", quality="MISSING"),
        ),
    )
    result = service_for(db).sync_version(document.id, version.id, session=session)
    assert result.facts["created"] == 1, "the doubtful value is stored, marked"
    assert {entry["field"] for entry in result.skipped} == {"plastic_viscosity"}
    assert any("missing" in entry["reason"] for entry in result.skipped)
    repository = KnowledgeRepository(session)
    [fact] = repository.facts_for_version(version.id)
    assert fact.original_value == "as received", "the wording the source used is preserved"
    assert fact.normalized_value is None, "no number was invented for it"
    assert fact.citation().endswith("Cell: B9")
    # A value the extractor flagged as doubtful is stored, cited and *not* presented as settled;
    # a field it could not read at all is a candidate, which is a different kind of not-known.
    assert fact.status == KnowledgeStatus.UNVERIFIED.value
    assert "not interpretable as a value" in fact.status_reason(), fact.status_reason()
    assert fact.note, "the parse failure is recorded as the fact's own note, not swallowed"
    candidates = repository.facts_for_version(version.id, status=KnowledgeStatus.CANDIDATE.value)
    assert candidates == [], "nothing here is unreadable - it is readable and doubted"


def test_a_field_without_provenance_is_reported_and_left_out(db, session, well) -> None:
    """The one thing this layer will not store, it says out loud instead of dropping quietly.

    ``extraction`` may hand back a field whose provenance was lost; storing it would put a number
    into the authoritative table that nobody can trace, and dropping it in silence would lose the
    only signal that the extractor has a bug.
    """
    document, version, payload = register_artefact(
        session,
        well_id=well.id,
        fields=(
            artefact_field("mud_weight", "10.2", "ppg", cell="B9"),
            artefact_field("yield_point", "12", "lb/100ft2", cell="B11"),
        ),
    )
    # Lose the provenance of the second field, the way a hand-edited artefact would.
    entries = [dict(entry) for entry in payload["extracted_fields"]]
    for entry in entries:
        if entry["name"] == "yield_point":
            entry.pop("provenance", None)
    payload["extracted_fields"] = entries
    from drilling_intelligence.documents.repository import DocumentRepository

    # Rewrite the stored artefact the way a hand-edited one would look: the field is there, its
    # provenance is not.
    DocumentRepository(session).extraction_for_version(version.id).document_json = payload

    result = service_for(db).sync_version(document.id, version.id, session=session)
    assert result.facts["created"] == 1
    assert any(
        "yield_point" in warning and "no provenance" in warning for warning in result.warnings
    ), result.warnings
    assert {
        "field": "yield_point",
        "reason": "no provenance recorded for this field",
    } in result.skipped
    repository = KnowledgeRepository(session)
    assert [fact.predicate for fact in repository.facts_for_version(version.id)] == ["mud_weight"]
    assert session.get(Well, well.id) is not None, "the well link is untouched by the rejection"


def test_a_document_that_names_no_well_becomes_knowledge_about_what_it_describes(
    db, session, well
) -> None:
    """No well, no problem: the facts belong to the entity the document's kind is about.

    A lessons-learned note is not about a well just because one exists in the workspace, and it is
    not about "nothing" either.  The subject type comes from the classification table, the row is
    created on the write path, and the answer is findable by entity.
    """
    document, version, _payload = register_artefact(
        session,
        classification="LESSON_LEARNED",
        fields=(
            artefact_field("lesson", "the crew re-primed the pump", "", excerpt="...re-primed..."),
        ),
    )
    result = service_for(db).sync_version(document.id, version.id, session=session)
    assert result.facts["created"] == 1
    repository = KnowledgeRepository(session)
    [fact] = repository.facts_for_document(document.id)
    assert fact.subject.entity_type == "lesson_learned", fact.subject
    assert fact.subject.entity_id, "a subject must have an id to point at"
    row = session.get(KnowledgeItem, fact.subject.entity_id)
    assert row is not None, "the subject row the fact points at must exist"
    from drilling_intelligence.knowledge.entities import entity_spec

    # The placeholder is an item-backed entity: it lives in ``knowledge_item`` under the item type
    # its entity spec names, which is what makes it findable by either vocabulary.
    assert row.item_type == entity_spec("lesson_learned").item_type
    assert row.title, "the placeholder carries the label it was named by"
    assert [item.item_id for item in repository.facts_for_entity(fact.subject)] == [fact.item_id]
    # Keyed deterministically to the version, so a rebuild reuses the same subject row.
    again = service_for(db).sync_version(document.id, version.id, session=session)
    assert again.facts == {"created": 0, "updated": 0, "unchanged": 1}
    assert (
        session.query(KnowledgeItem).filter(KnowledgeItem.id == fact.subject.entity_id).count() == 1
    )


def test_a_subject_with_a_table_of_its_own_is_never_invented(db, session, well) -> None:
    """A procedure document is about the document, because there is no procedure *row* to attach to.

    ``document`` is a table-backed entity type, so a placeholder cannot be conjured for it: the
    facts go under the version, which is a real row and a citable one.  Inventing a subject here
    would put a second, fake copy of the document into the entity table.
    """
    document, version, _payload = register_artefact(
        session,
        classification="PROCEDURE",
        identity_path="docs/procedure.pdf",
        filename="procedure.pdf",
        fields=(artefact_field("procedure_id", "PROC-7", "", excerpt="PROC-7"),),
    )
    service_for(db).sync_version(document.id, version.id, session=session)
    repository = KnowledgeRepository(session)
    [fact] = repository.facts_for_document(document.id)
    assert fact.subject.entity_type == "document_version"
    assert fact.subject.entity_id == version.id
    assert repository.facts_for_well(well.id) == [], (
        "nothing was attached to a well that never held it"
    )
    assert session.execute(select(func.count(KnowledgeItem.id))).scalar() == 1


def test_document_level_properties_stay_with_the_document(db, session, well) -> None:
    """ "Revision 3" and "written 2025-06-14" are facts about a file, not about the hole.

    Filed under the well, a workbook still reports its own revision label; if that became a property
    of the well, two documents about one well would argue with each other about a thing they are not
    describing.
    """
    document, version, _payload = register_artefact(
        session,
        well_id=well.id,
        fields=(
            artefact_field("revision", "Rev 3", "", cell="B2"),
            artefact_field("mud_weight", "10.2", "ppg", cell="B9"),
        ),
    )
    service_for(db).sync_version(document.id, version.id, session=session)
    repository = KnowledgeRepository(session)
    subjects = {
        fact.predicate: fact.subject.entity_type
        for fact in repository.facts_for_document(document.id)
    }
    assert subjects["revision"] == "document_version"
    assert subjects["mud_weight"] == "well"


def test_rebuild_replaces_derived_rows_and_keeps_what_a_person_typed(db, session, well) -> None:
    """The repair command is safe to run twice, and it never destroys a human note.

    ``rebuild`` deletes what extraction produced and re-derives it from the stored artefacts; a
    manual note - typed by someone who read the file - is not extraction's output, so it stays, and
    the ids of the derived rows must come out the same so edges and citations do not drift.
    """
    document, version, _payload = register_artefact(
        session, well_id=well.id, fields=(artefact_field("mud_weight", "10.2", "ppg", cell="B9"),)
    )
    service = service_for(db)
    service.sync_version(document.id, version.id, session=session)
    repository = KnowledgeRepository(session)
    note = KnowledgeFact(
        subject=EntityRef("well", well.id, label="A-3"),
        predicate="mud_weight",
        original_value="10.9",
        original_unit="ppg",
        value=10.9,
        unit="ppg",
        normalized_value=10.9,
        normalized_unit="ppg",
        text="10.9 ppg",
        value_type="quantity",
        status=KnowledgeStatus.ACTIVE.value,
        origin=KnowledgeOrigin.MANUAL.value,
        document_id=document.id,
        well_id=well.id,
        note="read off the density gauge the same evening",
    )
    manual = repository.manual_fact(note)
    # Typed against the file, so it is an argument the moment it exists - not one that waits for the
    # next ingest or rebuild to notice.
    assert len(repository.conflicts()) == 1
    assert [item.status for item in repository.facts_for_well(well.id)] == [
        KnowledgeStatus.CONFLICTED.value,
        KnowledgeStatus.CONFLICTED.value,
    ]
    before = [
        row.id
        for row in session.execute(select(KnowledgeItem).order_by(KnowledgeItem.id)).scalars()
    ]

    report = service.rebuild(session=session)
    assert report["removed"] == 1, "only the derived row was removed"
    assert report["versions"] == 1
    after = [
        row.id
        for row in session.execute(select(KnowledgeItem).order_by(KnowledgeItem.id)).scalars()
    ]
    assert sorted(after) == sorted(before), "the derived row came back with the same id"
    manual_row = session.get(KnowledgeItem, manual.id)
    assert manual_row is not None, "a note a person typed survives the rebuild"
    assert manual_row.origin == KnowledgeOrigin.MANUAL.value
    assert manual_row.status == KnowledgeStatus.CONFLICTED.value, (
        "the note and the file disagree, and a rebuild records that argument instead of ending it"
    )
    values = {
        fact.original_value for fact in repository.facts_for_well(well.id, include_superseded=True)
    }
    assert values == {"10.2", "10.9"}
    assert len(repository.conflicts()) == 1, "the dispute is still on the table after the rebuild"
    # Running it again is a no-op at the row level: same ids, same payload, no duplicates.
    second = service.rebuild(session=session)
    assert second["removed"] == 1
    third = [
        row.id
        for row in session.execute(select(KnowledgeItem).order_by(KnowledgeItem.id)).scalars()
    ]
    assert sorted(third) == sorted(after)


def test_status_notices_when_the_registry_moves_ahead_of_the_knowledge(db, session, well) -> None:
    """The one place where "how out of date am I?" is answered, and it must be answered exactly.

    A workspace where a new revision exists but has not been derived is not broken - it is behind,
    and the difference matters: "rebuild" fixes it, so the status says so; a fact still citing a
    version that is no longer current is *detached*, and that is printed as its own number rather
    than folded into the count of facts.
    """
    document, first, _payload = register_artefact(
        session, well_id=well.id, fields=(artefact_field("mud_weight", "10.2", "ppg", cell="B9"),)
    )
    service = service_for(db)
    service.sync_version(document.id, first.id, session=session)
    repository = KnowledgeRepository(session)
    clean = repository.counts()
    assert clean["facts"] == 1
    status = service.status(session=session)
    assert status["needs_rebuild"] is False
    assert status["versions_without_knowledge"] == 0
    assert status["detached_facts"] == 0

    second = register_artefact(
        session,
        well_id=well.id,
        fields=(artefact_field("mud_weight", "10.4", "ppg", cell="B9"),),
        version_number=2,
        supersedes=first,
    )[1]
    behind = service.status(session=session)
    assert behind["versions_without_knowledge"] == 1, "revision 2 has no facts of its own yet"
    assert behind["detached_facts"] == 1, "the one fact cites a version that is no longer current"
    assert behind["needs_rebuild"] is True
    service.sync_version(document.id, second.id, session=session)
    repository.supersede_previous_versions(document_id=document.id, version_id=second.id)
    fixed = service.status(session=session)
    assert fixed["versions_without_knowledge"] == 0
    assert fixed["detached_facts"] == 0, (
        "the superseded fact is still stored, and no longer detached"
    )
    assert fixed["needs_rebuild"] is False


def test_a_synced_fact_is_searchable_and_cites_the_cell_it_came_from(
    workspace, session, well
) -> None:
    """The sidecar is written from the same facts, and a hit is readable on its own.

    The search text deliberately carries no lifecycle status: the index is not rewritten when a
    marking pass moves a fact from ``ACTIVE`` to ``CONFLICTED``, and text that disagrees with the
    registry is worse than text that says less.
    """
    document, version, _payload = register_artefact(
        session,
        well_id=well.id,
        filename="mud_synthetic.xlsx",
        identity_path="docs/mud_synthetic.xlsx",
        fields=(
            artefact_field("mud_weight", "10.2", "ppg", cell="B9", filename="mud_synthetic.xlsx"),
        ),
    )
    session.commit()  # the index is written through the workspace's own session
    service = KnowledgeExtractionService.for_workspace(workspace)
    result = service.sync_version(document.id, version.id)
    assert result.index_chunks > 0, "the version's facts must reach the index in the same pass"
    search = workspace.search_service()
    try:
        response = search.search("mud weight 10.2 ppg", kinds=[KIND_KNOWLEDGE], limit=20)
        hits = [hit for hit in response.results if hit.kind == KIND_KNOWLEDGE]
        assert hits, "a fact the platform knows must be findable"
        hit = hits[0]
        assert hit.version_id == version.id
        assert hit.locator_ref.endswith("Cell: B9"), hit.locator_ref
        assert "10.2" in hit.text and "mud weight" in hit.text
        assert "status:" not in hit.text, "lifecycle state is not part of what is indexed"
    finally:
        workspace.close()
