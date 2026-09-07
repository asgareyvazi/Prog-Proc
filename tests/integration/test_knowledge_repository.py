"""Storing knowledge: version-aware rows, edges, and the guarantees the queries lean on.

These run against a real SQLite database built from the models (no mocks, no fake repositories),
because the three properties that matter here are properties of *storage*:

*   a fact is one row per (version, property, source wording), so re-deriving the same knowledge is
    a no-op rather than a duplicate - the difference between a safe repair command and one that
    doubles the corpus;
*   superseding a document revision moves the *answer* while keeping the history;
*   every write path refuses a value that cannot say where it came from.

The inverted-provenance-guard regression is pinned here as well: for an hour this layer rejected the
facts that had citations and stored the ones that did not, and only a database test could tell.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select
from tests.fixtures.knowledge import artefact_field, register_artefact

from drilling_intelligence.core.enums import KnowledgeOrigin, KnowledgeRelationType, KnowledgeStatus
from drilling_intelligence.database.models import KnowledgeItem, KnowledgeRelation, Source
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.knowledge.entities import EntityRef, KnowledgeError, ensure_placeholder
from drilling_intelligence.knowledge.facts import KnowledgeFact
from drilling_intelligence.knowledge.repository import KnowledgeRepository, fact_id_for
from drilling_intelligence.knowledge.service import KnowledgeExtractionService


def mud_weight_fact(
    *,
    document_id: str,
    version_id: str,
    subject: EntityRef,
    value: str = "10.2",
    unit: str = "ppg",
    origin: str = KnowledgeOrigin.EXTRACTED.value,
) -> KnowledgeFact:
    return KnowledgeFact.from_field(
        artefact_field("mud_weight", value, unit, document_id=document_id, version_id=version_id),
        subject=subject,
        origin=origin,
        document_id=document_id,
        document_version_id=version_id,
    )


def test_a_stored_fact_carries_both_the_wording_and_the_comparable_number(session, well) -> None:
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    row, action = repository.put_fact(
        mud_weight_fact(
            document_id=document.id,
            version_id=version.id,
            subject=EntityRef("well", well.id, label="A-3"),
        )
    )
    assert action == "CREATED"
    assert row.original_value == "10.2"
    assert row.original_unit == "ppg"
    assert row.normalized_value == pytest.approx(10.2)
    assert row.normalized_unit == "ppg"
    assert row.status == KnowledgeStatus.ACTIVE.value
    assert row.origin == KnowledgeOrigin.EXTRACTED.value
    assert row.entity_type == "well" and row.entity_id == well.id
    assert row.well_id == well.id, "the well-centric query reads this column"
    assert row.value == pytest.approx(10.2)
    assert row.unit == "ppg"
    assert row.revision == 1
    assert row.lookup_key and "mud_weight" in row.lookup_key
    assert (row.provenance or [{}])[0]["locator"]["cell"] == "B9"
    assert row.source_id, "a stored fact also has a registry source row"


def test_the_source_row_cites_the_version_not_just_the_filename(session) -> None:
    document, version, _payload = register_artefact(session)
    repository = KnowledgeRepository(session)
    repository.put_fact(
        mud_weight_fact(
            document_id=document.id,
            version_id=version.id,
            subject=EntityRef("document_version", version.id, label=document.filename),
        )
    )
    row = repository.facts_for_document(document.id)[0]
    source = session.get(Source, str(repository.fact_row(row.item_id).source_id))
    assert source.reference == f"version:{version.id}"
    assert source.authority_tier == document.source_authority
    assert source.document_version_id == version.id


def test_re_deriving_the_same_fact_changes_nothing(session, well) -> None:
    """Idempotence, measured in rows rather than in hopes.

    ``knowledge rebuild`` and "ingest the same folder twice" both depend on this: a fact's identity
    is (version, subject + property + record state, source wording), so a repeat run reports
    ``UNCHANGED`` and the table does not grow.
    """
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    subject = EntityRef("well", well.id, label="A-3")
    fact = mud_weight_fact(document_id=document.id, version_id=version.id, subject=subject)
    first, _ = repository.put_fact(fact)
    second, action = repository.put_fact(fact)
    assert action == "UNCHANGED"
    assert first.id == second.id
    assert int(session.execute(select(func.count()).select_from(KnowledgeItem)).scalar_one()) == 1
    assert second.revision == 1, "an unchanged row is not a new revision of anything"


def test_evidence_changing_under_the_same_key_rewrites_the_row(session, well) -> None:
    """Same statement, better evidence: the row is rewritten, and its ``revision`` stays the file's.

    ``revision`` on a knowledge row is the *document* revision the fact cites, not an edit counter -
    conflating the two would make a fact's revision say something about the database's history that
    the reader would take as history of the well.  What changed here is the evidence, and that is
    what the row reports.
    """
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    subject = EntityRef("well", well.id, label="A-3")
    repository.put_fact(
        mud_weight_fact(document_id=document.id, version_id=version.id, subject=subject)
    )
    moved = KnowledgeFact.from_field(
        artefact_field("mud_weight", "10.2", "ppg", cell="C12", sheet="Details"),
        subject=subject,
        document_id=document.id,
        document_version_id=version.id,
    )
    row, action = repository.put_fact(moved)
    assert action == "UPDATED"
    assert (row.provenance or [{}])[0]["locator"]["cell"] == "C12"
    assert (row.provenance or [{}])[0]["locator"]["sheet"] == "Details"
    assert int(row.revision) == 1, "the revision names the document revision, not an edit count"


def test_two_values_for_one_property_are_two_rows_not_one_overwrite(session, well) -> None:
    """Both sides of a disagreement survive - the storage half of "never silently choose"."""
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    subject = EntityRef("well", well.id, label="A-3")
    repository.put_fact(
        mud_weight_fact(
            document_id=document.id, version_id=version.id, subject=subject, value="10.2"
        )
    )
    repository.put_fact(
        mud_weight_fact(
            document_id=document.id, version_id=version.id, subject=subject, value="10.4"
        )
    )
    rows = repository.facts_for_well(well.id)
    assert sorted(row.original_value for row in rows) == ["10.2", "10.4"]
    assert len({row.item_id for row in rows}) == 2
    assert set(repository.lookup_keys()) == {rows[0].lookup_key()}
    assert len(repository.lookup_keys()) == 1, "two answers, one question being asked"


def test_a_fact_id_is_deterministic_and_value_specific() -> None:
    one = fact_id_for(
        version_id="ver-1",
        lookup_key="well:well-1|property:mud_weight|state:ACTUAL",
        original_value="10.2",
    )
    same = fact_id_for(
        version_id="ver-1",
        lookup_key="well:well-1|property:mud_weight|state:ACTUAL",
        original_value="10.2",
    )
    other = fact_id_for(
        version_id="ver-1",
        lookup_key="well:well-1|property:mud_weight|state:ACTUAL",
        original_value="10.4",
    )
    assert one == same and one != other
    assert one.startswith("ki-") and len(one) <= 36, "it is a knowledge_item primary key"


def test_an_extracted_value_without_provenance_is_refused_by_the_store(session, well) -> None:
    """The guard is checked twice on purpose: once built, once stored.

    A fact can be assembled from JSON by a caller who never went through ``from_field``, and the
    authoritative table is the last place that can still say no.
    """
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    uncited = KnowledgeFact(
        subject=EntityRef("well", well.id, label="A-3"),
        predicate="mud_weight",
        value_type="quantity",
        value=10.2,
        unit="ppg",
        original_value="10.2",
        original_unit="ppg",
        document_id=document.id,
        document_version_id=version.id,
    )
    with pytest.raises(KnowledgeError, match="needs provenance"):
        repository.put_fact(uncited)


def test_a_fact_citing_a_version_that_is_not_in_the_registry_is_refused(session, well) -> None:
    repository = KnowledgeRepository(session)
    with pytest.raises(KnowledgeError, match="not in the registry"):
        repository.put_fact(
            mud_weight_fact(
                document_id="doc-nope",
                version_id="ver-nope",
                subject=EntityRef("well", well.id, label="A-3"),
            )
        )


def test_a_manual_note_is_kept_when_extraction_is_rebuilt(session, well) -> None:
    """``origin`` is the difference between a repair command and a data-loss command.

    ``rebuild`` throws away what extraction produced so it can be produced again.  A person's note
    cannot be re-derived from anything, so it must not be in the set the command clears - and that
    is decided by data, not by a code path a future feature could forget.
    """
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    subject = EntityRef("well", well.id, label="A-3")
    repository.put_fact(
        mud_weight_fact(document_id=document.id, version_id=version.id, subject=subject)
    )
    note = repository.manual_fact(
        KnowledgeFact(
            subject=subject,
            predicate="mud_weight",
            value_type="text",
            original_value="verified against the pressure chart at the rig site",
            text="verified against the pressure chart at the rig site",
        )
    )
    assert note.origin == KnowledgeOrigin.MANUAL.value
    removed = repository.delete_derived(workspace_id=None)
    assert removed == 1, "only the extracted row goes"
    assert session.get(KnowledgeItem, str(note.id)) is not None
    kept = repository.facts_for_well(well.id)
    assert [fact.origin for fact in kept] == [KnowledgeOrigin.MANUAL.value], (
        "a note is found by the well query it belongs to"
    )


def test_superseding_a_revision_moves_the_answer_and_keeps_the_history(session, well, db) -> None:
    """The version-aware part of the brief, end to end through the service.

    ``session=session`` is the path the ingestion pipeline uses - facts are written into the
    caller's transaction, which is also what lets this test see them without a second connection.
    """
    service = KnowledgeExtractionService(database=db, index=None, refresh_index=False)
    subject_fields = (artefact_field("mud_weight", "10.2", "ppg"),)
    document, first_version, _payload = register_artefact(
        session, well_id=well.id, fields=subject_fields
    )
    sync = service.sync_version(document.id, first_version.id, session=session)
    assert sync.facts["created"] >= 1
    assert sync.fact_count == 1
    second_version = register_artefact(
        session,
        well_id=well.id,
        fields=(artefact_field("mud_weight", "10.6", "ppg"),),
        version_number=2,
        supersedes=first_version,
        revision="Rev 2",
    )[1]
    again = service.sync_version(document.id, second_version.id, session=session)
    assert again.superseded >= 1, "the older statement is marked, not deleted"
    repository = KnowledgeRepository(session)
    current = repository.facts_for_well(well.id)
    assert {fact.original_value for fact in current} == {"10.6"}, (
        "the answer is the newest statement"
    )
    history = repository.facts_for_well(well.id, include_superseded=True)
    assert {fact.original_value for fact in history} == {"10.2", "10.6"}
    statuses = {fact.original_value: fact.status for fact in history}
    assert statuses["10.2"] == KnowledgeStatus.SUPERSEDED.value
    assert statuses["10.6"] == KnowledgeStatus.ACTIVE.value
    assert sync.conflicts["conflicts"] == 0, "one version at a time is not an argument"


def test_edges_are_written_and_are_valid_against_the_registry(session, well) -> None:
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    subject = EntityRef("well", well.id, label="A-3")
    row, _ = repository.put_fact(
        mud_weight_fact(document_id=document.id, version_id=version.id, subject=subject)
    )

    repository.link(
        source=EntityRef("document_version", version.id, label=document.filename),
        relation=KnowledgeRelationType.VERSION_CONTAINS_KNOWLEDGE.value,
        target=EntityRef("engineering_fact", str(row.id)),
        note="derived during the test",
    )
    assert repository.relation_exists(
        source=EntityRef("document_version", version.id),
        relation=KnowledgeRelationType.VERSION_CONTAINS_KNOWLEDGE.value,
        target=EntityRef("engineering_fact", str(row.id)),
    )
    edges = list(session.execute(select(KnowledgeRelation)).scalars())
    assert len(edges) == 1
    assert edges[0].note == "derived during the test"
    assert repository.relations_for_entity(EntityRef("engineering_fact", str(row.id)))
    # Linking twice must not double the edge: an upsert, with the note of whoever wrote it last.
    repository.link(
        source=EntityRef("document_version", version.id, label=document.filename),
        relation=KnowledgeRelationType.VERSION_CONTAINS_KNOWLEDGE.value,
        target=EntityRef("engineering_fact", str(row.id)),
        note="written again",
    )
    assert len(list(session.execute(select(KnowledgeRelation)).scalars())) == 1


def test_a_dangling_edge_is_refused_rather_than_stored(session, well) -> None:

    repository = KnowledgeRepository(session)
    with pytest.raises(Exception, match="does not exist"):
        repository.link(
            source=EntityRef("well", well.id),
            relation=KnowledgeRelationType.WELL_HAS_DOCUMENT.value,
            target=EntityRef("document", "doc-does-not-exist"),
        )


def test_reads_filter_by_predicate_status_and_document(session, well) -> None:
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    subject = EntityRef("well", well.id, label="A-3")
    repository.put_fact(
        mud_weight_fact(document_id=document.id, version_id=version.id, subject=subject)
    )
    repository.put_fact(
        KnowledgeFact.from_field(
            artefact_field("rpm", "120", "rpm", document_id=document.id, version_id=version.id),
            subject=subject,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    assert {fact.predicate for fact in repository.facts_for_well(well.id)} == {"mud_weight", "rpm"}
    # ``limit`` defaults to 200 and ordering is newest-first; sorting here keeps the assertion
    # about *which* facts were selected rather than about the order they came back in.
    assert sorted(
        fact.predicate for fact in repository.facts_for_well(well.id, predicate="rpm")
    ) == ["rpm"]
    assert repository.facts_for_document(document.id) and repository.facts_for_version(version.id)
    assert repository.facts_for_entity(subject)
    # Read back by row id, the way a UI would when it follows a hit to the fact behind it.
    mud = next(
        fact
        for fact in repository.facts_for_document(document.id)
        if fact.predicate == "mud_weight"
    )
    assert repository.get_fact(mud.item_id).original_value == "10.2"
    assert repository.get_fact("ki-not-a-row") is None, "a missing row is None, not an exception"
    counts = repository.counts()
    assert counts["facts"] == 2
    assert counts["by_status"][KnowledgeStatus.ACTIVE.value] == 2
    assert counts["by_entity_type"]["well"] == 2


def test_counts_separates_facts_from_the_entity_records_pointed_at(session, well) -> None:
    """``knowledge_item`` holds two populations, and a total that adds them up describes neither.

    A derived subject with no table of its own - a lesson, a mud system - gets a placeholder row so
    the edges have something to reference.  It asserts nothing, so it must not inflate the number a
    reader takes to mean "what does this workspace know"; quietly leaving it out would be just as
    wrong, so it is reported under its own key.
    """
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    repository.put_fact(
        mud_weight_fact(
            document_id=document.id,
            version_id=version.id,
            subject=EntityRef("well", well.id, label="A-3"),
        )
    )
    lesson = ensure_placeholder(
        session, entity_type="lesson_learned", label="do not force the pipe"
    )

    counts = repository.counts()
    assert counts["facts"] == 1, counts
    assert counts["entity_records"] == 1, counts
    assert counts["by_origin"] == {KnowledgeOrigin.EXTRACTED.value: 1}, (
        "a placeholder is not evidence of an extraction either"
    )
    # The table really does hold both rows, which is what the split is protecting.
    assert session.execute(select(func.count()).select_from(KnowledgeItem)).scalar_one() == 2
    assert lesson.entity_type == "lesson_learned" and lesson.entity_id


def test_set_status_validates_the_vocabulary(session, well) -> None:
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    row, _ = repository.put_fact(
        mud_weight_fact(
            document_id=document.id,
            version_id=version.id,
            subject=EntityRef("well", well.id, label="A-3"),
        )
    )
    assert repository.set_status(
        str(row.id), status=KnowledgeStatus.RETIRED.value, note="superseded by a later survey"
    )
    assert row.status == KnowledgeStatus.RETIRED.value
    assert "later survey" in str((row.payload or {}).get("status_note"))
    with pytest.raises(KnowledgeError, match="unknown knowledge status"):
        repository.set_status(str(row.id), status="SETTLED_BY_GUT_FEEL")
    assert repository.set_status("ki-does-not-exist", status=KnowledgeStatus.ACTIVE.value) is False


def test_documents_repository_sees_the_same_rows(session, well) -> None:
    """One store, two readers: the audit/document repository must not need its own copy."""
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    repository.put_fact(
        mud_weight_fact(
            document_id=document.id,
            version_id=version.id,
            subject=EntityRef("well", well.id, label="A-3"),
        )
    )
    session.commit()
    documents = DocumentRepository(session)
    assert documents.get(document.id) is not None
    trail = documents.audit_trail("document", document.id)
    assert isinstance(trail, list)


def test_a_lifecycle_change_is_visible_through_the_read_paths(session, well) -> None:
    """A fact that has been marked, superseded or retired must *say* so when it is read back.

    The payload keeps what the source said and the columns keep what the registry decided, so a read
    path that prefers the payload would print ``ACTIVE`` next to a value that is in dispute, and
    would offer a retired value as an answer.  This is the test that stops that from coming back.
    """
    document, version, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    subject = EntityRef("well", well.id, label="A-3")
    row, _action = repository.put_fact(
        mud_weight_fact(document_id=document.id, version_id=version.id, subject=subject)
    )
    repository.set_status(
        row.id,
        status=KnowledgeStatus.CONFLICTED.value,
        note="another source states a different value",
    )
    [fact] = repository.facts_for_well(well.id)
    assert fact.item_id == row.id
    assert fact.status == KnowledgeStatus.CONFLICTED.value
    assert "another source states" in fact.status_reason()
    assert fact.to_dict()["status"] == KnowledgeStatus.CONFLICTED.value, (
        "the listing and the object must not disagree"
    )
    repository.set_status(
        row.id,
        status=KnowledgeStatus.RETIRED.value,
        superseded_by="",
        note="not selected by the reviewer",
    )
    assert repository.facts_for_well(well.id) == [], "a retired value is not the platform's answer"
    [history] = repository.facts_for_well(well.id, include_superseded=True)
    assert history.status == KnowledgeStatus.RETIRED.value
    assert history.status_reason() == "not selected by the reviewer"
    assert history.superseded_by == ""


def test_superseding_a_version_moves_the_fact_it_answered(session, well) -> None:
    """The column and the read path agree about which revision is current."""
    document, first, _payload = register_artefact(session, well_id=well.id)
    repository = KnowledgeRepository(session)
    subject = EntityRef("well", well.id, label="A-3")
    old_row, _ = repository.put_fact(
        mud_weight_fact(document_id=document.id, version_id=first.id, subject=subject)
    )
    second = register_artefact(
        session,
        well_id=well.id,
        fields=(artefact_field("mud_weight", "10.4", "ppg"),),
        version_number=2,
        supersedes=first,
        revision="Rev 2",
    )[1]
    newer = replace(
        KnowledgeFact.from_field(
            artefact_field(
                "mud_weight", "10.4", "ppg", document_id=document.id, version_id=second.id
            ),
            subject=subject,
            document_id=document.id,
            document_version_id=second.id,
        ),
        revision=2,
    )
    new_row, action = repository.put_fact(newer)
    assert action == "CREATED", "a new revision is a new statement, not an edit of the old one"
    repository.supersede_previous_versions(document_id=document.id, version_id=second.id)
    current = repository.facts_for_well(well.id)
    assert [fact.item_id for fact in current] == [new_row.id]
    assert current[0].revision == 2
    history = repository.facts_for_well(well.id, include_superseded=True)
    assert [fact.item_id for fact in history] == [new_row.id, old_row.id], (
        "the newer revision is listed first, and the older one is still readable"
    )
    # ``facts_for_version`` holds retired history back by default like every other read, so the
    # history flag is part of asking for it - and the row is still there when you do.
    superseded = [
        fact
        for fact in repository.facts_for_version(first.id, include_superseded=True)
        if fact.status == KnowledgeStatus.SUPERSEDED.value
    ]
    assert [fact.item_id for fact in superseded] == [old_row.id], (
        "history stays readable, with its reason"
    )
    # The pointer is the *version* that replaced it, not a row: a new revision may drop a property or
    # split it across two, and a row-to-row pointer would be a guess dressed up as a fact.
    assert superseded[0].superseded_by == ""
    assert superseded[0].superseded_by_version_id == second.id
    assert second.id in superseded[0].status_reason()
