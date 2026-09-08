"""P6 forensic verification of the knowledge & evidence graph.

The knowledge layer is already exercised for *behaviour* by ``test_knowledge_repository``,
``test_knowledge_conflicts``, ``test_knowledge_pipeline`` and ``test_knowledge_integrity``.  This file
is the *forensic* half: it re-derives the contract from the authoritative rows and pins the properties
a future retrieval/RAG/UI layer will lean on, without mocks and through the public repository/service
boundaries:

*   **identity** - a fact's id is content-addressed over (version, lookup key, source wording), so the
    same bytes are the same row and two machines agree without talking; provenance/confidence changes
    rewrite the row rather than spawning a twin, and a *different* value is never an update;
*   **provenance** - a MANUAL fact fabricates no document/version/evidence; an EXTRACTED fact is refused
    without provenance and keeps its document chain and locator; the stored row is a snapshot, not a
    live view of a mutable source;
*   **relations** - an edge is one row per (source, relation, target); re-asserting strengthens, a
    reversed edge is distinct, a dangling endpoint is refused, and a self-reference is refused at the
    write path (the regression for the write/check drift);
*   **conflicts** - a disagreement is recorded, never settled, both candidates stay recoverable, a
    resolution records who decided and cannot pick a non-candidate, and re-running detection converges
    instead of piling up;
*   **cross-domain lineage** - a fact answers FROM WHERE from its own columns; a calculation input's
    ``subject_key`` is the soft link change-impact analysis walks; the record tables carry real foreign
    keys and the relation vocabulary carries the hard edges;
*   **scope isolation** - a well-scoped query never returns another well's facts or conflicts;
*   **lifecycle / search / determinism / cost / immutability** - read paths never write, listing issues
    a constant number of queries, and the search projection is a deterministic, provenance-preserving,
    disposable view of the authoritative rows.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import event, func, select
from tests.fixtures.knowledge import artefact_field, register_artefact

from drilling_intelligence.core.enums import (
    ConflictResolution,
    KnowledgeOrigin,
    KnowledgeRelationType,
    KnowledgeStatus,
)
from drilling_intelligence.database.integrity import (
    KnowledgeIntegrityError,
    check_knowledge_relations,
)
from drilling_intelligence.database.models import (
    Document,
    DocumentVersion,
    KnowledgeConflict,
    KnowledgeItem,
    KnowledgeRelation,
    Recommendation,
    Source,
)
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.knowledge.conflicts import detect_conflicts, resolve_conflict
from drilling_intelligence.knowledge.entities import EntityRef, KnowledgeError
from drilling_intelligence.knowledge.facts import KnowledgeFact
from drilling_intelligence.knowledge.repository import KnowledgeRepository, fact_id_for
from drilling_intelligence.search.structured import (
    is_searchable,
    structured_record_id,
    structured_records,
)


def _fact(
    session,
    well,
    *,
    value: str = "10.2",
    unit: str = "ppg",
    predicate: str = "mud_weight",
    filename: str = "mud.xlsx",
    origin: str = KnowledgeOrigin.EXTRACTED.value,
) -> tuple[Document, DocumentVersion, KnowledgeItem]:
    document, version, _payload = register_artefact(
        session, well_id=well.id, filename=filename, identity_path=f"docs/{filename}"
    )
    repository = KnowledgeRepository(session)
    row, _action = repository.put_fact(
        KnowledgeFact.from_field(
            artefact_field(predicate, value, unit),
            subject=EntityRef("well", well.id, label=str(well.name)),
            origin=origin,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    return document, version, row


def _select_count(engine, fn) -> int:
    count = {"value": 0}

    def before(conn, cursor, statement, params, context, executemany):
        if str(statement).lstrip().upper().startswith("SELECT"):
            count["value"] += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return count["value"]


def _fingerprint(session) -> str:
    """A content fingerprint of the three knowledge tables (the authoritative record)."""
    parts: list[str] = []
    for model in (KnowledgeItem, KnowledgeRelation, KnowledgeConflict):
        rows = [
            tuple(
                sorted(
                    (str(k), str(v)) for k, v in row.__dict__.items() if k != "_sa_instance_state"
                )
            )
            for row in session.execute(select(model).order_by(model.id)).scalars()
        ]
        parts.append(f"{model.__tablename__}::{rows}")
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- identity
class TestFactIdentity:
    def test_identity_is_deterministic_and_value_specific(self) -> None:
        """Same (version, key, wording) is the same id; a different value is a different id."""
        base = {
            "version_id": "ver-1",
            "lookup_key": "well:well-1|property:mud_weight|state:ACTUAL",
        }
        assert fact_id_for(original_value="10.2", **base) == fact_id_for(
            original_value="10.2", **base
        )
        assert fact_id_for(original_value="10.2", **base) != fact_id_for(
            original_value="10.4", **base
        )
        # A different version of the same source is a different row.
        assert fact_id_for(original_value="10.2", **base) != fact_id_for(
            original_value="10.2", version_id="ver-2", lookup_key=base["lookup_key"]
        )

    def test_the_same_fact_twice_is_one_row_not_two(self, session, well) -> None:
        """Duplicate prevention: two independently-built but identical facts are one row."""
        document, version, _payload = register_artefact(session, well_id=well.id)
        subject = EntityRef("well", well.id, label=str(well.name))
        repository = KnowledgeRepository(session)

        def build() -> KnowledgeFact:
            return KnowledgeFact.from_field(
                artefact_field("mud_weight", "10.2", "ppg"),
                subject=subject,
                document_id=document.id,
                document_version_id=version.id,
            )

        first, action_first = repository.put_fact(build())
        again, action_again = repository.put_fact(build())
        assert action_first == "CREATED" and action_again == "UNCHANGED"
        assert first.id == again.id
        assert session.scalar(select(func.count()).select_from(KnowledgeItem)) == 1

    def test_a_different_value_is_a_new_row_never_an_update(self, session, well) -> None:
        """Two sources disagreeing about one property are two rows, not a silent overwrite."""
        _fact(session, well, value="10.2", filename="a.xlsx")
        _fact(session, well, value="10.4", filename="b.xlsx")
        repository = KnowledgeRepository(session)
        rows = repository.facts_by_key(
            repository.facts_for_well(well.id)[0].lookup_key(), include_superseded=True
        )
        assert {str(row.original_value) for row in rows} == {"10.2", "10.4"}

    def test_provenance_and_confidence_change_under_the_same_key_is_a_rewrite(
        self, session, well
    ) -> None:
        """The identity is the *statement*, not its metadata: better evidence rewrites, not twins.

        This is the documented counterpart of P5's calculation identity: a fact groups by
        (version, subject+property+state, source wording), so a confidence or locator change
        reports ``UPDATED`` and keeps one row - it never silently accumulates duplicates.
        """
        repository = KnowledgeRepository(session)
        document, version, _payload = register_artefact(session, well_id=well.id)
        subject = EntityRef("well", well.id, label=str(well.name))
        first = KnowledgeFact.from_field(
            artefact_field("mud_weight", "10.2", "ppg", confidence=0.7),
            subject=subject,
            document_id=document.id,
            document_version_id=version.id,
        )
        row, _ = repository.put_fact(first)
        moved = KnowledgeFact.from_field(
            artefact_field("mud_weight", "10.2", "ppg", confidence=0.9, cell="C12"),
            subject=subject,
            document_id=document.id,
            document_version_id=version.id,
        )
        again, action = repository.put_fact(moved)
        assert action == "UPDATED" and again.id == row.id
        assert session.scalar(select(func.count()).select_from(KnowledgeItem)) == 1
        assert again.confidence == pytest.approx(0.9)

    def test_identity_is_content_addressed_not_sequence_derived(self, session, well) -> None:
        """Ids are content hashes, not auto-increment counters, so insertion order cannot matter."""
        document, version, _payload = register_artefact(session, well_id=well.id)
        subject = EntityRef("well", well.id, label=str(well.name))
        repository = KnowledgeRepository(session)
        facts = [
            KnowledgeFact.from_field(
                artefact_field(predicate, value, unit),
                subject=subject,
                document_id=document.id,
                document_version_id=version.id,
            )
            for predicate, value, unit in (("mud_weight", "10.2", "ppg"), ("rpm", "120", "rpm"))
        ]
        expected = {
            fact_id_for(
                version_id=version.id,
                lookup_key=fact.lookup_key(),
                original_value=fact.original_value,
            )
            for fact in facts
        }
        for fact in reversed(facts):  # write in the reverse of the natural order
            repository.put_fact(fact)
        stored = {row.id for row in session.execute(select(KnowledgeItem)).scalars()}
        assert stored == expected, "each row's id is its content hash, independent of write order"


# --------------------------------------------------------------------------- provenance
class TestProvenanceContract:
    def test_a_manual_fact_fabricates_no_document_evidence(self, session, well) -> None:
        """A note a person typed carries no document_id/version_id/source/evidence."""
        repository = KnowledgeRepository(session)
        note = repository.manual_fact(
            KnowledgeFact(
                subject=EntityRef("well", well.id, label=str(well.name)),
                predicate="mud_weight",
                value_type="text",
                original_value="verified against the rig chart",
                text="verified against the rig chart",
            )
        )
        assert note.origin == KnowledgeOrigin.MANUAL.value
        assert note.document_id is None and note.document_version_id is None
        assert note.source_id is None and note.provenance == []
        # The status is whatever the writer asserted (here the dataclass default CANDIDATE); a person
        # can assert ACTIVE for a value they read off the gauge - the point is that nothing *pretends*
        # a document backs it.
        assert note.status == KnowledgeStatus.CANDIDATE.value

    def test_a_document_derived_fact_preserves_the_chain_and_locator(self, session, well) -> None:
        document, version, row = _fact(session, well)
        assert row.origin == KnowledgeOrigin.EXTRACTED.value
        assert row.document_id == document.id and row.document_version_id == version.id
        assert row.provenance and row.provenance[0]["locator"]["cell"] == "B9"
        source = session.get(Source, str(row.source_id))
        assert source is not None and source.reference == f"version:{version.id}"

    def test_an_extracted_fact_without_provenance_is_refused(self, session, well) -> None:
        repository = KnowledgeRepository(session)
        document, version, _payload = register_artefact(session, well_id=well.id)
        uncited = KnowledgeFact(
            subject=EntityRef("well", well.id, label=str(well.name)),
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

    def test_the_stored_row_is_a_snapshot_not_a_live_view(self, session, well) -> None:
        """Mutating the source mapping after the write does not rewrite the fact."""
        repository = KnowledgeRepository(session)
        document, version, _payload = register_artefact(session, well_id=well.id)
        subject = EntityRef("well", well.id, label=str(well.name))
        field = artefact_field("mud_weight", "10.2", "ppg")
        fact = KnowledgeFact.from_field(
            field,
            subject=subject,
            document_id=document.id,
            document_version_id=version.id,
        )
        row, _ = repository.put_fact(fact)
        field["value"] = "99.9"
        field["provenance"]["locator"]["cell"] = "ZZZ"
        assert row.original_value == "10.2"
        assert row.provenance[0]["locator"]["cell"] == "B9"


# --------------------------------------------------------------------------- relations
class TestRelationContract:
    def test_re_asserting_the_same_edge_strengthens_not_duplicates(self, session, well) -> None:
        document, version, row = _fact(session, well)
        repository = KnowledgeRepository(session)
        source = EntityRef("document_version", version.id, label=document.filename)
        target = EntityRef("engineering_fact", str(row.id))
        relation = KnowledgeRelationType.VERSION_CONTAINS_KNOWLEDGE.value
        repository.link(source=source, relation=relation, target=target, weight=0.5)
        repository.link(source=source, relation=relation, target=target, weight=0.9)
        edges = list(session.execute(select(KnowledgeRelation)).scalars())
        assert len(edges) == 1 and edges[0].weight == pytest.approx(0.9)

    def test_a_reversed_edge_is_a_distinct_relationship(self, session, well) -> None:
        """Directed edges: A->B and B->A are two different claims, both stored."""
        document, version, row = _fact(session, well)
        repository = KnowledgeRepository(session)
        source = EntityRef("document_version", version.id, label=document.filename)
        target = EntityRef("engineering_fact", str(row.id))
        repository.link(source=source, relation="ITEM_SUPPORTS", target=target)
        repository.link(source=target, relation="ITEM_SUPPORTS", target=source)
        edges = list(session.execute(select(KnowledgeRelation)).scalars())
        assert len(edges) == 2
        assert {(e.source_id, e.target_id) for e in edges} == {
            (version.id, str(row.id)),
            (str(row.id), version.id),
        }

    def test_a_dangling_endpoint_is_refused(self, session, well) -> None:
        repository = KnowledgeRepository(session)
        with pytest.raises(Exception, match="does not exist"):
            repository.link(
                source=EntityRef("well", well.id),
                relation=KnowledgeRelationType.WELL_HAS_DOCUMENT.value,
                target=EntityRef("document", "doc-does-not-exist"),
            )

    def test_a_self_reference_is_refused_at_the_write_path(self, session, well) -> None:
        """The regression: the write path used to accept a ``well -> well`` edge that its own
        integrity checker then reported as ``SELF_REFERENCE``.  The two halves now agree."""
        repository = KnowledgeRepository(session)
        with pytest.raises(KnowledgeIntegrityError, match="must not point at its own source"):
            repository.link(
                source=EntityRef("well", well.id),
                relation=KnowledgeRelationType.WELL_HAS_DOCUMENT.value,
                target=EntityRef("well", well.id),
            )
        assert session.scalar(select(func.count()).select_from(KnowledgeRelation)) == 0

    def test_an_existing_self_reference_is_still_reported(self, session, well) -> None:
        """The post-hoc half still finds self-edges that predate the write-path guard."""
        session.execute(
            KnowledgeRelation.__table__.insert().values(
                id="rel-self",
                source_type="well",
                source_id=well.id,
                relation="ITEM_SUPPORTS",
                target_type="well",
                target_id=well.id,
                weight=1.0,
                provenance=[],
            )
        )
        session.flush()
        assert [p.problem for p in check_knowledge_relations(session)] == ["SELF_REFERENCE"]


# --------------------------------------------------------------------------- conflicts
class TestConflictContract:
    def _competing_facts(self, session, well):
        """Two documents, two values for the same property, both stored."""
        _fact(session, well, value="10.2", filename="a.xlsx")
        _fact(session, well, value="10.4", filename="b.xlsx")
        return KnowledgeRepository(session)

    def test_a_disagreement_is_recorded_and_both_sides_survive(self, session, well) -> None:
        repository = self._competing_facts(session, well)
        report = detect_conflicts(repository)
        assert report.conflicts == 1 and report.items_marked == 2
        conflicts = list(session.execute(select(KnowledgeConflict)).scalars())
        assert len(conflicts) == 1
        candidates = conflicts[0].candidates
        assert {str(c["value"]) for c in candidates} == {"10.2", "10.4"}
        # Both facts are demoted to CONFLICTED, neither is deleted.
        rows = repository.facts_by_key(conflicts[0].lookup_key, include_superseded=True)
        assert {str(r.status) for r in rows} == {KnowledgeStatus.CONFLICTED.value}

    def test_re_running_detection_converges_instead_of_piling_up(self, session, well) -> None:
        repository = self._competing_facts(session, well)
        detect_conflicts(repository)
        detect_conflicts(repository)
        assert session.scalar(select(func.count()).select_from(KnowledgeConflict)) == 1

    def test_resolution_keeps_both_values_and_records_who_decided(self, session, well) -> None:
        repository = self._competing_facts(session, well)
        detect_conflicts(repository)
        conflict = session.scalar(select(KnowledgeConflict))
        chosen = str(conflict.candidates[0]["item_id"])
        resolve_conflict(
            repository, conflict.id, chosen_item_id=chosen, by="well engineer", note="program wins"
        )
        session.flush()
        assert conflict.status == ConflictResolution.RESOLVED_MANUALLY.value
        assert conflict.resolution["by"] == "well engineer"
        assert conflict.resolution["candidates_at_resolution"], "the losing side is preserved"
        rows = repository.facts_by_key(conflict.lookup_key, include_superseded=True)
        statuses = {str(r.status) for r in rows}
        assert KnowledgeStatus.ACTIVE.value in statuses
        assert KnowledgeStatus.RETIRED.value in statuses

    def test_resolution_cannot_pick_a_non_candidate(self, session, well) -> None:
        repository = self._competing_facts(session, well)
        detect_conflicts(repository)
        conflict = session.scalar(select(KnowledgeConflict))
        with pytest.raises(ValueError, match="not one of this conflict"):
            resolve_conflict(repository, conflict.id, chosen_item_id="ki-not-a-candidate")

    def test_a_superseded_statement_leaves_the_argument(self, session, well) -> None:
        """A newer revision replaces an older one, and the older one stops arguing."""
        from dataclasses import replace

        document, first, _payload = register_artefact(
            session, well_id=well.id, fields=(artefact_field("mud_weight", "10.2", "ppg"),)
        )
        repository = KnowledgeRepository(session)
        subject = EntityRef("well", well.id, label=str(well.name))
        repository.put_fact(
            KnowledgeFact.from_field(
                artefact_field("mud_weight", "10.2", "ppg"),
                subject=subject,
                document_id=document.id,
                document_version_id=first.id,
            )
        )
        _fact(session, well, value="10.4", filename="b.xlsx")
        detect_conflicts(repository)
        assert session.scalar(select(func.count()).select_from(KnowledgeConflict)) == 1
        # Supersede the older document, then re-detect: 10.2 is history, not a voice.
        second = register_artefact(
            session,
            well_id=well.id,
            fields=(artefact_field("mud_weight", "10.5", "ppg"),),
            version_number=2,
            supersedes=first,
        )[1]
        repository.put_fact(
            replace(
                KnowledgeFact.from_field(
                    artefact_field("mud_weight", "10.5", "ppg"),
                    subject=subject,
                    document_id=document.id,
                    document_version_id=second.id,
                ),
                revision=2,
            )
        )
        repository.supersede_previous_versions(document_id=document.id, version_id=second.id)
        rows = repository.facts_by_key(
            repository.facts_for_well(well.id)[0].lookup_key(), include_superseded=True
        )
        by_value = {str(r.original_value): str(r.status) for r in rows}
        assert by_value["10.2"] == KnowledgeStatus.SUPERSEDED.value
        assert KnowledgeStatus.CONFLICTED.value in by_value.values(), (
            "the still-live 10.4 and 10.5 keep arguing"
        )


# --------------------------------------------------------------------------- cross-domain lineage
class TestCrossDomainLineage:
    def test_a_fact_answers_from_where_from_its_own_columns(self, session, well) -> None:
        """A fact's persisted columns alone trace it back to its document version and document."""
        document, version, row = _fact(session, well)
        assert row.document_version_id == version.id
        assert version.document_id == document.id == row.document_id
        assert row.provenance and row.provenance[0]["locator"]
        assert row.original_value == "10.2" and row.unit == "ppg"

    def test_a_calculation_input_links_to_its_domain_source(self, session, well) -> None:
        """``subject_key`` is the soft link change-impact analysis walks from a calculation back
        to the knowledge fact (or well/section attribute) it consumed."""
        _document, _version, row = _fact(session, well)
        lookup_key = str(row.lookup_key)
        engineering = EngineeringRepository(session)
        calculation, created = engineering.record_calculation(
            method_id="hydraulics.ecd",
            inputs={"mw": {"value": "10.2 ppg", "subject_key": lookup_key}},
            outputs={"ecd": 11.4},
        )
        session.flush()
        assert created
        [item] = engineering.calculation_inputs(calculation.id)
        assert item.subject_key == lookup_key
        assert engineering.calculations_using(lookup_key) == [calculation]

    def test_the_record_tables_carry_real_foreign_keys_for_lineage(self, session) -> None:
        """Hard links (not strings) exist where the schema chose them: recommendation -> lesson,
        lesson -> well, and every scoped record -> its well."""
        from drilling_intelligence.database.models import LessonLearned

        rec_columns = {c.name: c for c in Recommendation.__table__.columns}
        for name in ("lesson_id", "pattern_id", "problem_id", "well_id", "project_id"):
            assert name in rec_columns and rec_columns[name].foreign_keys, name
        lesson_columns = {c.name: c for c in LessonLearned.__table__.columns}
        for name in ("well_id", "field_id", "project_id"):
            assert name in lesson_columns and lesson_columns[name].foreign_keys, name

    def test_the_relation_vocabulary_carries_the_lineage_edges(self) -> None:
        """The hard graph edges for lesson/procedure/calculation lineage already exist as tokens."""
        for edge in (
            "LESSON_DERIVED_FROM_WELL",
            "LESSON_CITES_EVIDENCE",
            "PROBLEM_CITED_BY_LESSON",
            "REPORT_CONTAINS_KNOWLEDGE",
            "PROGRAM_USES_CALCULATION",
        ):
            assert hasattr(KnowledgeRelationType, edge), edge


# --------------------------------------------------------------------------- scope isolation
class TestScopeIsolation:
    def test_facts_for_well_returns_only_that_well(self, session, well) -> None:
        from drilling_intelligence.wells.repository import WellRepository

        other = WellRepository(session).create_well("B-11", project_id=well.project_id)
        _fact(session, well, value="10.2", filename="a.xlsx")
        _fact(session, other, value="11.0", filename="b.xlsx")
        repository = KnowledgeRepository(session)
        mine = {f.original_value for f in repository.facts_for_well(well.id)}
        assert mine == {"10.2"}, "a well query must not leak another well's facts"
        assert {f.original_value for f in repository.facts_for_well(other.id)} == {"11.0"}

    def test_conflicts_do_not_leak_across_wells(self, session, well) -> None:
        from drilling_intelligence.wells.repository import WellRepository

        other = WellRepository(session).create_well("B-11", project_id=well.project_id)
        _fact(session, well, value="10.2", filename="a.xlsx")
        _fact(session, well, value="10.4", filename="b.xlsx")
        _fact(session, other, value="11.0", filename="c.xlsx")
        _fact(session, other, value="11.2", filename="d.xlsx")
        repository = KnowledgeRepository(session)
        detect_conflicts(repository)
        assert repository.conflicts(well_id=well.id), "well A has its own argument"
        assert repository.conflicts(well_id=other.id), "well B has its own argument"
        for conflict in repository.conflicts(well_id=well.id):
            assert conflict.well_id == well.id
        for conflict in repository.conflicts(well_id=other.id):
            assert conflict.well_id == other.id

    def test_relations_for_entity_is_scoped_to_its_endpoints(self, session, well) -> None:
        from drilling_intelligence.wells.repository import WellRepository

        other = WellRepository(session).create_well("B-11", project_id=well.project_id)
        document, version, row = _fact(session, well)
        repository = KnowledgeRepository(session)
        repository.link(
            source=EntityRef("document_version", version.id, label=document.filename),
            relation=KnowledgeRelationType.VERSION_CONTAINS_KNOWLEDGE.value,
            target=EntityRef("engineering_fact", str(row.id)),
        )
        edges = repository.relations_for_entity(EntityRef("engineering_fact", str(row.id)))
        assert len(edges) == 1
        assert all(e.source_id != other.id and e.target_id != other.id for e in edges), (
            "an entity's edges must not touch a foreign well"
        )


# --------------------------------------------------------------------------- lifecycle
class TestLifecycle:
    def test_active_superseded_and_retired_are_distinct_through_reads(self, session, well) -> None:
        repository = KnowledgeRepository(session)
        _document, _version, row = _fact(session, well)
        assert [f.status for f in repository.facts_for_well(well.id)] == [
            KnowledgeStatus.ACTIVE.value
        ]
        repository.set_status(str(row.id), status=KnowledgeStatus.RETIRED.value, note="not chosen")
        assert repository.facts_for_well(well.id) == [], "a retired value is not the answer"
        history = repository.facts_for_well(well.id, include_superseded=True)
        assert [f.status for f in history] == [KnowledgeStatus.RETIRED.value]

    def test_superseding_moves_the_answer_and_keeps_history(self, session, well) -> None:
        from dataclasses import replace

        document, first, _payload = register_artefact(
            session, well_id=well.id, fields=(artefact_field("mud_weight", "10.2", "ppg"),)
        )
        repository = KnowledgeRepository(session)
        subject = EntityRef("well", well.id, label=str(well.name))
        repository.put_fact(
            KnowledgeFact.from_field(
                artefact_field("mud_weight", "10.2", "ppg"),
                subject=subject,
                document_id=document.id,
                document_version_id=first.id,
            )
        )
        second = register_artefact(
            session,
            well_id=well.id,
            fields=(artefact_field("mud_weight", "10.6", "ppg"),),
            version_number=2,
            supersedes=first,
        )[1]
        newer = replace(
            KnowledgeFact.from_field(
                artefact_field("mud_weight", "10.6", "ppg"),
                subject=subject,
                document_id=document.id,
                document_version_id=second.id,
            ),
            revision=2,
        )
        repository.put_fact(newer)
        repository.supersede_previous_versions(document_id=document.id, version_id=second.id)
        assert {f.original_value for f in repository.facts_for_well(well.id)} == {"10.6"}
        history = repository.facts_for_well(well.id, include_superseded=True)
        assert {f.original_value for f in history} == {"10.2", "10.6"}
        assert {f.original_value: f.status for f in history}[
            "10.2"
        ] == KnowledgeStatus.SUPERSEDED.value


# --------------------------------------------------------------------------- search projection
class TestSearchProjection:
    def test_the_projection_preserves_provenance_and_invents_none(self, session, well) -> None:
        from drilling_intelligence.database.models import LessonLearned

        cited = LessonLearned(
            id="les-cited",
            title="Cited lesson",
            lesson="ream before tripping",
            provenance=[{"kind": "document", "page": 3}],
            origin=KnowledgeOrigin.EXTRACTED.value,
            well_id=well.id,
        )
        manual = LessonLearned(
            id="les-manual",
            title="Manual lesson",
            lesson="a note with no source",
            provenance=[],
            origin=KnowledgeOrigin.MANUAL.value,
        )
        session.add_all([cited, manual])
        session.flush()
        records = {r.source_id: r for r in structured_records(session)}
        cited_provenance = records["les-cited"].provenance
        assert cited_provenance["record_type"] == "lesson_learned"
        assert cited_provenance["evidence"] == [{"kind": "document", "page": 3}]
        manual_provenance = records["les-manual"].provenance
        assert "document_id" not in manual_provenance, "no fabricated document reference"
        assert manual_provenance.get("evidence", []) == []

    def test_projection_identity_is_stable_and_lifecycle_aware(self, session, well) -> None:
        from drilling_intelligence.database.models import LessonLearned

        lesson = LessonLearned(
            id="les-lc", title="Lifecycle lesson", lesson="a statement", is_current=True
        )
        session.add(lesson)
        session.flush()
        assert (
            structured_record_id("lesson_learned", "les-lc") == "structured:lesson_learned:les-lc"
        )
        assert is_searchable(lesson) is True
        lesson.is_current = False
        session.flush()
        assert is_searchable(lesson) is False, "a superseded lesson is not the searchable answer"

    def test_the_projection_writes_nothing_to_the_registry(self, session, well) -> None:
        _fact(session, well)
        baseline = _fingerprint(session)
        structured_records(session)
        structured_records(session)
        assert _fingerprint(session) == baseline, "building the projection is read-only"


# --------------------------------------------------------------------------- determinism / cost / immutability
class TestDeterminismAndCost:
    def test_listing_facts_issues_a_constant_number_of_queries(self, db, session, well) -> None:
        repository = KnowledgeRepository(session)
        for index in range(3):
            _fact(session, well, value=f"10.{index}", filename=f"n{index}.xlsx")
        session.flush()
        baseline = _select_count(db.engine, lambda: repository.facts_for_well(well.id))
        assert baseline == 1, baseline
        for index in range(3, 30):
            _fact(session, well, value=f"10.{index}", filename=f"n{index}.xlsx")
        session.flush()
        assert _select_count(db.engine, lambda: repository.facts_for_well(well.id)) == baseline, (
            "a well listing must be one scan, not one query per fact"
        )

    def test_relations_and_conflicts_listing_is_set_based(self, db, session, well) -> None:
        repository = KnowledgeRepository(session)
        document, version, row = _fact(session, well)
        repository.link(
            source=EntityRef("document_version", version.id, label=document.filename),
            relation=KnowledgeRelationType.VERSION_CONTAINS_KNOWLEDGE.value,
            target=EntityRef("engineering_fact", str(row.id)),
        )
        session.flush()
        ref = EntityRef("engineering_fact", str(row.id))
        relation_baseline = _select_count(db.engine, lambda: repository.relations_for_entity(ref))
        conflict_baseline = _select_count(db.engine, repository.conflicts)
        assert relation_baseline <= 2, relation_baseline
        assert conflict_baseline <= 2, conflict_baseline

    def test_read_operations_leave_the_registry_unchanged(self, db, session, well) -> None:
        repository = KnowledgeRepository(session)
        _fact(session, well)
        session.flush()
        baseline = _fingerprint(session)
        repository.facts_for_well(well.id, include_superseded=True)
        repository.lookup_keys()
        repository.counts()
        repository.conflicts()
        repository.get_fact(repository.facts_for_well(well.id)[0].item_id)
        assert _fingerprint(session) == baseline, "reads must not write to the knowledge tables"

    def test_conflict_candidate_ranking_is_deterministic(self, session, well) -> None:
        """The ranking depends on authority/date/version/id, not on insertion order."""
        from drilling_intelligence.knowledge.conflicts import _candidates

        _fact(session, well, value="10.2", filename="a.xlsx")
        _fact(session, well, value="10.4", filename="b.xlsx")
        repository = KnowledgeRepository(session)
        rows = list(session.execute(select(KnowledgeItem).order_by(KnowledgeItem.id)).scalars())
        first = _candidates(repository, rows)
        second = _candidates(repository, list(reversed(rows)))
        assert [c.item_id for c in first] == [c.item_id for c in second], (
            "candidate order must not depend on the order rows were passed in"
        )
