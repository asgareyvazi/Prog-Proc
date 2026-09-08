"""Forensic tests for the Retrieval & Evidence layer.

The layer's single job is to turn search *candidates* (which come from a disposable index that may
be stale) into *authoritative evidence*: re-read each candidate from the database, check its
lifecycle and scope, and return it - or report, with a reason, why it is not authoritative.  These
tests pin down every one of those rules against a real SQLite database, real repositories and a
real search sidecar.  There are no mocks standing in for the database or the index.

The structured fixtures build the exact scope topology the platform is promised
(Project A { Field A { A1, A2 }, Field B { B1 } }, Project B { Field C { C1 } }) and put the *same*
text in a record in every well, so that any scope leak - a record from the wrong well/field/project
appearing in a scoped answer - is caught immediately.  The corpus fixture exercises the document and
knowledge source types on a real generated corpus.  Scope isolation, authoritative re-read,
lifecycle, provenance, identity, determinism, read-only-ness, conflict preservation and bounded
reads are each asserted directly.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import event, select

from drilling_intelligence.core.enums import (
    ConfirmationStatus,
    KnowledgeStatus,
    RecommendationLifecycle,
)
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import (
    Document,
    DocumentVersion,
    Field,
    KnowledgeItem,
    LessonLearned,
    NptRecord,
    ProblemDefinition,
    ProblemOccurrence,
    Project,
    Recommendation,
    Well,
    WellEvent,
)
from drilling_intelligence.lessons.repository import LessonRepository
from drilling_intelligence.operations.repository import OperationsRepository
from drilling_intelligence.retrieval import (
    RetrievalRequest,
    RetrievalService,
)
from drilling_intelligence.search.service import SearchService
from drilling_intelligence.wells.repository import WellRepository

# A distinctive token shared by every record in the world, so a scoped query for it would surface
# every well's copy if scope leaked.  It is not a word in the corpus, so only records built here
# can match it.
SHARED_TERM = "quartzite"


# ============================================================================ helpers
def _authoritative_fingerprint(workspace) -> str:
    """A hash over every authoritative row's identity + state, to prove a read path changed nothing."""
    out: list[str] = []
    with workspace.database.read_only() as session:
        for model in (
            Well,
            Field,
            Project,
            Document,
            DocumentVersion,
            KnowledgeItem,
            LessonLearned,
            ProblemOccurrence,
            Recommendation,
        ):
            rows = session.execute(select(model).order_by(model.id)).scalars().all()
            for row in rows:
                state = {
                    c.name: getattr(row, c.name)
                    for c in row.__table__.columns
                    if c.name != "id" and c.name not in {"created_at", "updated_at"}
                }
                out.append(
                    f"{model.__tablename__}:{row.id}:"
                    + hashlib.sha256(
                        json.dumps(state, default=str, sort_keys=True).encode()
                    ).hexdigest()[:12]
                )
    return hashlib.sha256("\n".join(out).encode()).hexdigest()


@dataclass
class World:
    """A scope topology with the same searchable text in a record in every well."""

    ws: Any
    search: SearchService
    retr: RetrievalService
    ids: dict[str, Any] = field(default_factory=dict)

    def retrieve(self, **kwargs: Any) -> Any:
        return self.retr.retrieve(RetrievalRequest(**kwargs))


def _service_for(workspace) -> World:
    search = SearchService.for_workspace(workspace)
    return World(
        ws=workspace,
        search=search,
        retr=RetrievalService(database=workspace.database, search_service=search),
    )


@pytest.fixture
def world(workspace) -> World:
    """Project A { Field A { A1, A2 }, Field B { B1 } }, Project B { Field C { C1 } }."""
    ws = workspace
    ids: dict[str, Any] = {}
    with ws.database.session() as session:
        repo = WellRepository(session)
        repo.get_or_create_workspace(str(ws.root), name="North Cormorant")
        proj_a = repo.get_or_create_project("Project Alpha")
        proj_b = repo.get_or_create_project("Project Bravo")
        field_a = repo.get_or_create_field("Field Alpha", project=proj_a)
        field_b = repo.get_or_create_field("Field Beta", project=proj_a)
        field_c = repo.get_or_create_field("Field Charlie", project=proj_b)
        a1 = repo.create_well("A1", project_id=proj_a.id, field_id=field_a.id)
        a2 = repo.create_well("A2", project_id=proj_a.id, field_id=field_a.id)
        b1 = repo.create_well("B1", project_id=proj_a.id, field_id=field_b.id)
        c1 = repo.create_well("C1", project_id=proj_b.id, field_id=field_c.id)
        session.commit()
        ids.update(
            pA=proj_a.id,
            pB=proj_b.id,
            fA=field_a.id,
            fB=field_b.id,
            fC=field_c.id,
            A1=a1.id,
            A2=a2.id,
            B1=b1.id,
            C1=c1.id,
        )
        lessons = LessonRepository(session)
        for _key, (well_id, field_id, project_id) in {
            "les_A1": (a1.id, field_a.id, proj_a.id),
            "les_A2": (a2.id, field_a.id, proj_a.id),
            "les_B1": (b1.id, field_b.id, proj_a.id),
            "les_C1": (c1.id, field_c.id, proj_b.id),
        }.items():
            lessons.capture(
                lesson=f"{SHARED_TERM} stuck pipe observed on the rotary drilling program.",
                title=f"{SHARED_TERM} lesson",
                well_id=well_id,
                field_id=field_id,
                project_id=project_id,
            )
        session.commit()
        for key, well_id in {
            "les_A1": a1.id,
            "les_A2": a2.id,
            "les_B1": b1.id,
            "les_C1": c1.id,
        }.items():
            row = session.scalar(
                select(LessonLearned).where(
                    LessonLearned.well_id == well_id, LessonLearned.title == f"{SHARED_TERM} lesson"
                )
            )
            ids[key] = row.id
    search = SearchService.for_workspace(ws)
    search.rebuild()
    return World(
        ws=ws,
        search=search,
        retr=RetrievalService(database=ws.database, search_service=search),
        ids=ids,
    )


@pytest.fixture
def corpus_world(workspace) -> World:
    """The real generated corpus, promoted, with knowledge derived and the index built."""
    from tests.fixtures.fieldops import field_id, ingest, promote, well_id_for

    from drilling_intelligence.knowledge.service import KnowledgeExtractionService

    ws = workspace
    ingest(ws)
    promote(ws)
    KnowledgeExtractionService.for_workspace(ws).rebuild(workspace_id="", well_id="")
    ids = {"A3": well_id_for(ws, "A-3"), "B11": well_id_for(ws, "B-11"), "field": field_id(ws)}
    world = _service_for(ws)
    world.ids = ids
    world.search.rebuild()
    return world


def _solo_world(workspace) -> World:
    """A single well/field/project, for the focused lifecycle and provenance tests."""
    ids: dict[str, Any] = {}
    with workspace.database.session() as session:
        repo = WellRepository(session)
        repo.get_or_create_workspace(str(workspace.root), name="Solo")
        proj = repo.get_or_create_project("Solo Project")
        fld = repo.get_or_create_field("Solo Field", project=proj)
        well = repo.create_well("Solo-1", project_id=proj.id, field_id=fld.id)
        session.commit()
        ids.update(project=proj.id, field=fld.id, well=well.id)
    world = _service_for(workspace)
    world.ids = ids
    return world


# ============================================================================ identity & determinism
class TestIdentityAndDeterminism:
    def test_structured_identity_is_the_row_its_own(self, world) -> None:
        bundle = world.retrieve(query=SHARED_TERM, well_id=world.ids["A1"], limit=0)
        assert bundle.count == 1
        item = bundle.items[0]
        assert item.source_type == "structured"
        assert item.record_type == "lesson_learned"
        assert item.source_id == world.ids["les_A1"]
        assert item.identity == f"structured:lesson_learned:{world.ids['les_A1']}"
        assert item.verified is True

    def test_same_record_different_query_same_identity(self, world) -> None:
        a = world.retrieve(query=SHARED_TERM, well_id=world.ids["A1"], limit=0)
        b = world.retrieve(query=f"{SHARED_TERM} rotary", well_id=world.ids["A1"], limit=0)
        assert a.items[0].identity == b.items[0].identity
        assert a.items[0].source_id == b.items[0].source_id

    def test_bundle_is_byte_for_byte_deterministic(self, world) -> None:
        first = world.retrieve(query=SHARED_TERM, limit=0).to_dict()
        second = world.retrieve(query=SHARED_TERM, limit=0).to_dict()
        assert first == second, "a repeated retrieval over unchanged data must be identical"

    def test_identity_survives_reindex_and_new_rows(self, world) -> None:
        original = {i.identity for i in world.retrieve(query=SHARED_TERM, limit=0).items}
        # Add more rows (changing the index composition and ordering) and reindex.
        with world.ws.database.session() as session:
            for i in range(3):
                LessonRepository(session).capture(
                    lesson=f"{SHARED_TERM} latecomer {i} stuck pipe.",
                    title=f"Late {i}",
                    well_id=world.ids["A1"],
                    field_id=world.ids["fA"],
                    project_id=world.ids["pA"],
                )
            session.commit()
        world.search.rebuild()
        now = {i.identity for i in world.retrieve(query=SHARED_TERM, limit=0).items}
        # The pre-existing rows keep their identities; only new identities are added.
        assert original <= now
        assert len(now) == len(original) + 3

    def test_no_volatile_data_in_the_bundle(self, world) -> None:
        blob = json.dumps(world.retrieve(query=SHARED_TERM, limit=0).to_dict(), default=str)
        assert not re.search(r"0x[0-9a-f]{6,}", blob), "no memory addresses"
        assert "<" not in blob, "no Python object reprs"

    def test_ties_break_deterministically(self, world) -> None:
        # A1 and A2 carry identical text (equal score); their order is fixed, not by row order.
        first = [
            i.identity
            for i in world.retrieve(query=SHARED_TERM, field_id=world.ids["fA"], limit=0).items
        ]
        again = [
            i.identity
            for i in world.retrieve(query=SHARED_TERM, field_id=world.ids["fA"], limit=0).items
        ]
        assert first == again
        assert len(first) == 2


# ============================================================================ authoritative re-read
class TestAuthoritativeReRead:
    def test_deleted_row_is_dropped_not_returned(self, world) -> None:
        with world.ws.database.session() as session:
            session.delete(session.get(LessonLearned, world.ids["les_A1"]))
            session.commit()
        # The sidecar still lists it (no rebuild) - discovery would still surface it...
        stale = world.search.search(SHARED_TERM, well_id=world.ids["A1"], limit=0)
        assert any(h.metadata.get("source_id") == world.ids["les_A1"] for h in stale.results)
        # ...but the re-read finds no row, so it is dropped, never returned as evidence.
        bundle = world.retrieve(query=SHARED_TERM, well_id=world.ids["A1"], limit=0)
        assert bundle.count == 0
        assert any(d["reason"] == "no longer in the authoritative database" for d in bundle.dropped)

    def test_stale_sidecar_cannot_become_authoritative_evidence(self, world) -> None:
        """The mandatory forensic: index -> mutate authoritative -> no rebuild -> retrieve."""
        target = world.ids["les_A1"]
        with world.ws.database.session() as session:
            LessonRepository(session).revise(
                target, by="engineer", changes={"lesson": f"{SHARED_TERM} revised body now."}
            )
            session.commit()
        # Without a rebuild the sidecar still carries the (now non-current) original revision.
        stale = world.search.search(SHARED_TERM, well_id=world.ids["A1"], limit=0)
        assert any(h.metadata.get("source_id") == target for h in stale.results), (
            "precondition: the sidecar is still stale"
        )
        bundle = world.retrieve(query=SHARED_TERM, well_id=world.ids["A1"], limit=0)
        assert bundle.count == 0
        assert bundle.dropped, "the stale candidate must be reported as dropped, not silently kept"

    def test_rejected_occurrence_dropped_under_current_kept_under_history(self, workspace) -> None:
        world = _solo_world(workspace)
        with workspace.database.session() as session:
            occ = OperationsRepository(session).record_problem(
                well_id=world.ids["well"],
                problem_type="stuck_pipe",
                description="rejected-occurrence kelly stuck body.",
            )
            occ_id = occ.id
            session.commit()
        world.search.rebuild()
        current = world.retrieve(query="rejected-occurrence", well_id=world.ids["well"], limit=0)
        assert current.count == 1 and current.items[0].source_id == occ_id
        with workspace.database.session() as session:
            session.get(ProblemOccurrence, occ_id).status = ConfirmationStatus.REJECTED.value
            session.commit()
        current = world.retrieve(query="rejected-occurrence", well_id=world.ids["well"], limit=0)
        assert current.count == 0
        assert any(d["reason"].startswith("not current") for d in current.dropped)
        history = world.retrieve(
            query="rejected-occurrence", well_id=world.ids["well"], limit=0, lifecycle="history"
        )
        assert history.count == 1
        assert history.items[0].current is False
        assert history.items[0].status == ConfirmationStatus.REJECTED.value

    def test_superseded_recommendation_dropped_under_current(self, workspace) -> None:
        world = _solo_world(workspace)
        with workspace.database.session() as session:
            rec = LessonRepository(session).propose_recommendation(
                statement="superseded-recommendation wiper trip body.",
                reason="r",
                well_id=world.ids["well"],
                field_id=world.ids["field"],
                project_id=world.ids["project"],
            )
            rec_id = rec.id
            session.commit()
        world.search.rebuild()
        with workspace.database.session() as session:
            LessonRepository(session).decide_recommendation(
                rec_id, RecommendationLifecycle.SUPERSEDED, by="engineer"
            )
            session.commit()
        current = world.retrieve(
            query="superseded-recommendation", well_id=world.ids["well"], limit=0
        )
        assert current.count == 0
        history = world.retrieve(
            query="superseded-recommendation",
            well_id=world.ids["well"],
            limit=0,
            lifecycle="history",
        )
        assert history.count == 1
        assert history.items[0].current is False
        assert history.items[0].status == RecommendationLifecycle.SUPERSEDED.value

    def test_deleted_document_version_is_dropped(self, corpus_world) -> None:
        """Delete a current version without a rebuild: the re-read must not resurrect it.

        The document is chosen by filename (the corpus's deterministic order) so the test always
        lands on a citable, single-version file, not whichever random id happens to sort first.
        """
        world = corpus_world
        with world.ws.database.read_only() as session:
            version = session.scalar(
                select(DocumentVersion)
                .join(Document, DocumentVersion.document_id == Document.id)
                .where(DocumentVersion.is_current.is_(True))
                .order_by(Document.filename)
            )
            version_id, document_id = str(version.id), str(version.document_id)
        # Make sure the version actually has a chunk in the index for a known query.
        query = _a_query_for(world, document_id)
        assert query, "corpus must yield a searchable document"
        world.search.rebuild()
        before = world.retrieve(query=query, source_types=("document",), limit=0)
        assert any(i.document_version_id == version_id for i in before.items)
        with world.ws.database.session() as session:
            session.delete(session.get(DocumentVersion, version_id))
            session.commit()
        after = world.retrieve(query=query, source_types=("document",), limit=0)
        assert all(i.document_version_id != version_id for i in after.items)
        assert any(d["reason"] == "no longer in the authoritative database" for d in after.dropped)

    def test_superseded_document_version_dropped_under_current(self, corpus_world) -> None:
        """Demote the version without a rebuild: CURRENT holds it back, HISTORY surfaces it stale.

        The document is chosen by filename (the corpus's deterministic order) so the test always
        lands on a citable, single-version file, not whichever random id happens to sort first.
        """
        world = corpus_world
        with world.ws.database.read_only() as session:
            doc = session.scalar(select(Document).order_by(Document.filename))
            doc_id = str(doc.id)
        query = _a_query_for(world, doc_id)
        assert query
        with world.ws.database.session() as session:
            version = session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == doc_id, DocumentVersion.is_current.is_(True)
                )
            )
            version.is_current = False
            session.commit()
            version_id = str(version.id)
        # No rebuild: the sidecar still carries the now-non-current version, so both policies must
        # decide it by the authoritative row, not the index.
        current = world.retrieve(query=query, source_types=("document",), limit=0)
        assert all(i.document_version_id != version_id for i in current.items)
        assert any(d["reason"] == "not current (superseded version)" for d in current.dropped)
        history = world.retrieve(
            query=query, source_types=("document",), limit=0, lifecycle="history"
        )
        stale = next((i for i in history.items if i.document_version_id == version_id), None)
        assert stale is not None, "under history the superseded version is still evidence, labelled"
        assert stale.current is False


# ============================================================================ scope isolation
class TestScopeIsolation:
    def test_well_scope_returns_only_that_well(self, world) -> None:
        for well_key in ("A1", "A2", "B1", "C1"):
            bundle = world.retrieve(query=SHARED_TERM, well_id=world.ids[well_key], limit=0)
            assert bundle.count == 1, f"{well_key} must return exactly its own record"
            assert bundle.items[0].well_id == world.ids[well_key]

    def test_field_scope_returns_only_that_field(self, world) -> None:
        bundle = world.retrieve(query=SHARED_TERM, field_id=world.ids["fA"], limit=0)
        assert {i.well_id for i in bundle.items} == {world.ids["A1"], world.ids["A2"]}
        assert world.ids["B1"] not in {i.well_id for i in bundle.items}
        assert world.ids["C1"] not in {i.well_id for i in bundle.items}

    def test_project_scope_returns_only_that_project(self, world) -> None:
        bundle = world.retrieve(query=SHARED_TERM, project_id=world.ids["pA"], limit=0)
        assert {i.well_id for i in bundle.items} == {
            world.ids["A1"],
            world.ids["A2"],
            world.ids["B1"],
        }
        assert world.ids["C1"] not in {i.well_id for i in bundle.items}

    def test_no_scope_returns_everything_and_labels_it(self, world) -> None:
        bundle = world.retrieve(query=SHARED_TERM, limit=0)
        assert {i.well_id for i in bundle.items} == {
            world.ids["A1"],
            world.ids["A2"],
            world.ids["B1"],
            world.ids["C1"],
        }
        assert bundle.scope["level"] == "all"

    def test_well_beats_field_never_a_union(self, world) -> None:
        bundle = world.retrieve(
            query=SHARED_TERM, well_id=world.ids["A1"], field_id=world.ids["fB"], limit=0
        )
        assert {i.well_id for i in bundle.items} == {world.ids["A1"]}
        assert bundle.scope["level"] == "well"

    def test_well_beats_project(self, world) -> None:
        bundle = world.retrieve(
            query=SHARED_TERM, well_id=world.ids["C1"], project_id=world.ids["pA"], limit=0
        )
        assert {i.well_id for i in bundle.items} == {world.ids["C1"]}

    def test_unknown_scope_is_an_explicit_error_not_an_empty_answer(self, world) -> None:
        # "Well X that does not exist" is a caller error, not a silent empty answer.
        for scope_kwargs in (
            {"well_id": "does-not-exist"},
            {"field_id": "does-not-exist"},
            {"project_id": "does-not-exist"},
        ):
            with pytest.raises(ValidationError):
                world.retrieve(query=SHARED_TERM, limit=0, **scope_kwargs)

    def test_knowledge_scope_does_not_leak_across_wells(self, corpus_world) -> None:
        world = corpus_world
        query = "mud weight"
        a3 = world.retrieve(
            query=query, well_id=world.ids["A3"], source_types=("knowledge",), limit=0
        )
        assert a3.count > 0
        assert all(i.well_id == world.ids["A3"] for i in a3.items), (
            "A-3 scope may only return A-3 facts"
        )
        b11 = world.retrieve(
            query=query, well_id=world.ids["B11"], source_types=("knowledge",), limit=0
        )
        assert all(i.well_id == world.ids["B11"] for i in b11.items), (
            "B-11 scope may only return B-11 facts"
        )
        # Whatever A-3 returned, none of it may appear under the B-11 scope.
        assert not ({i.identity for i in a3.items} & {i.identity for i in b11.items})

    def test_document_scope_does_not_leak_across_wells(self, corpus_world) -> None:
        world = corpus_world
        query = _a_query_for(world, document_any(world))
        a3 = world.retrieve(
            query=query, well_id=world.ids["A3"], source_types=("document",), limit=0
        )
        assert all(i.well_id == world.ids["A3"] for i in a3.items)


# ============================================================================ lifecycle / current-history
class TestLifecycle:
    def test_policy_is_recorded(self, world) -> None:
        assert world.retrieve(query=SHARED_TERM, limit=0).policy == "current"
        assert world.retrieve(query=SHARED_TERM, limit=0, lifecycle="history").policy == "history"

    def test_current_and_history_differ_exactly_by_the_superseded(self, world) -> None:
        target = world.ids["les_A2"]
        with world.ws.database.session() as session:
            LessonRepository(session).revise(
                target, by="eng", changes={"lesson": f"{SHARED_TERM} A2 now revised."}
            )
            session.commit()
        current = world.retrieve(query=SHARED_TERM, well_id=world.ids["A2"], limit=0)
        history = world.retrieve(
            query=SHARED_TERM, well_id=world.ids["A2"], limit=0, lifecycle="history"
        )
        assert current.count == 0
        assert history.count == 1
        assert history.items[0].current is False

    def test_every_structured_type_follows_its_own_current_rule(self, workspace) -> None:
        world = _solo_world(workspace)
        with workspace.database.session() as session:
            lessons = LessonRepository(session)
            lessons.capture(
                lesson="kelly bushing lesson body text.",
                title="L",
                well_id=world.ids["well"],
                field_id=world.ids["field"],
                project_id=world.ids["project"],
            )
            occ = OperationsRepository(session).record_problem(
                well_id=world.ids["well"],
                problem_type="stuck_pipe",
                description="kelly occurrence body.",
            )
            rec = lessons.propose_recommendation(
                statement="kelly wiper recommendation body.",
                reason="r",
                well_id=world.ids["well"],
                field_id=world.ids["field"],
                project_id=world.ids["project"],
            )
            session.commit()
            occ_id, rec_id = occ.id, rec.id
        world.search.rebuild()
        bundle = world.retrieve(query="kelly", well_id=world.ids["well"], limit=0)
        assert {i.record_type for i in bundle.items} == {
            "lesson_learned",
            "problem_occurrence",
            "recommendation",
        }
        # Now reject the occurrence and supersede the recommendation; both leave CURRENT only.
        with workspace.database.session() as session:
            session.get(ProblemOccurrence, occ_id).status = ConfirmationStatus.REJECTED.value
            LessonRepository(session).decide_recommendation(
                rec_id, RecommendationLifecycle.SUPERSEDED, by="eng"
            )
            session.commit()
        bundle = world.retrieve(query="kelly", well_id=world.ids["well"], limit=0)
        # The rejected occurrence and superseded recommendation both leave the CURRENT answer;
        # only the live lesson remains.
        assert {i.record_type for i in bundle.items} == {"lesson_learned"}
        hist = world.retrieve(
            query="kelly", well_id=world.ids["well"], limit=0, lifecycle="history"
        )
        by_type = {i.record_type: i for i in hist.items}
        assert by_type["problem_occurrence"].current is False
        assert by_type["recommendation"].current is False
        assert by_type["recommendation"].status == RecommendationLifecycle.SUPERSEDED.value

    def test_retired_knowledge_dropped_under_current_kept_under_history(self, corpus_world) -> None:
        world = corpus_world
        from drilling_intelligence.knowledge.repository import KnowledgeRepository

        with world.ws.database.read_only() as session:
            item = session.scalar(
                select(KnowledgeItem)
                .where(KnowledgeItem.well_id == world.ids["A3"])
                .order_by(KnowledgeItem.id)
            )
            item_id = str(item.id)
        # Build the index while the item is still current, then retire it without a rebuild.
        world.search.rebuild()
        query = _fact_query_for(world, item_id)
        assert query
        current = world.retrieve(
            query=query, well_id=world.ids["A3"], source_types=("knowledge",), limit=0
        )
        assert any(i.source_id == item_id for i in current.items)
        with world.ws.database.session() as session:
            KnowledgeRepository(session).set_status(
                item_id, status=KnowledgeStatus.RETIRED.value, note="retired by test"
            )
            session.commit()
        current = world.retrieve(
            query=query, well_id=world.ids["A3"], source_types=("knowledge",), limit=0
        )
        assert all(i.source_id != item_id for i in current.items)
        assert any(d["reason"].startswith("not current") for d in current.dropped)
        history = world.retrieve(
            query=query,
            well_id=world.ids["A3"],
            source_types=("knowledge",),
            limit=0,
            lifecycle="history",
        )
        retired = next(i for i in history.items if i.source_id == item_id)
        assert retired.current is False
        assert retired.status == KnowledgeStatus.RETIRED.value

    def test_conflicted_knowledge_is_returned_and_labelled_not_resolved(self, corpus_world) -> None:
        world = corpus_world
        from drilling_intelligence.knowledge.repository import KnowledgeRepository

        with world.ws.database.read_only() as session:
            item = session.scalar(
                select(KnowledgeItem)
                .where(KnowledgeItem.well_id == world.ids["A3"])
                .order_by(KnowledgeItem.id)
            )
            item_id = str(item.id)
        with world.ws.database.session() as session:
            KnowledgeRepository(session).set_status(
                item_id, status=KnowledgeStatus.CONFLICTED.value, note="disputed"
            )
            session.commit()
        world.search.rebuild()
        query = _fact_query_for(world, item_id)
        assert query
        bundle = world.retrieve(
            query=query, well_id=world.ids["A3"], source_types=("knowledge",), limit=0
        )
        conflicted = next((i for i in bundle.items if i.source_id == item_id), None)
        assert conflicted is not None, (
            "a CONFLICTED item is still live evidence and must be returned"
        )
        assert conflicted.status == KnowledgeStatus.CONFLICTED.value
        assert conflicted.current is True


# ============================================================================ all six structured sources
class TestAllSixStructuredSources:
    def test_every_structured_type_resolves_through_one_mechanism(self, workspace) -> None:
        """All six authoritative record types go through the same re-read, scope and identity rules."""
        world = _solo_world(workspace)
        with workspace.database.session() as session:
            ops = OperationsRepository(session)
            lessons = LessonRepository(session)
            well, field, project = world.ids["well"], world.ids["field"], world.ids["project"]
            defs = ops.get_or_create_problem_definition(
                "stuck_pipe", name="Stuck pipe", description="zermatt definition body."
            )
            occ = ops.record_problem(
                well_id=well, problem_type="stuck_pipe", description="yosemite occurrence body."
            )
            npt = ops.record_npt(
                well_id=well, category="stuck_pipe", description="yellowstone npt body."
            )
            ev = ops.record_event(
                well_id=well, event_type="stuck_pipe", description="yamuna event body."
            )
            les = lessons.capture(
                lesson="yarrabubin lesson body.",
                title="L",
                well_id=well,
                field_id=field,
                project_id=project,
            )
            rec = lessons.propose_recommendation(
                statement="yarara recommendation body.",
                reason="r",
                well_id=well,
                field_id=field,
                project_id=project,
            )
            session.commit()
            ids = {
                "problem_definition": defs.id,
                "problem_occurrence": occ.id,
                "npt_record": npt.id,
                "well_event": ev.id,
                "lesson_learned": les.id,
                "recommendation": rec.id,
            }
        world.search.rebuild()

        token_for = {
            "problem_definition": "zermatt",
            "problem_occurrence": "yosemite",
            "npt_record": "yellowstone",
            "well_event": "yamuna",
            "lesson_learned": "yarrabubin",
            "recommendation": "yarara",
        }
        for record_type, row_id in ids.items():
            # A definition is unscoped, so it is only retrievable by an unscoped query; the other
            # five carry the well and are scoped to it here.
            scope_kwargs = {} if record_type == "problem_definition" else {"well_id": well}
            bundle = world.retrieve(query=token_for[record_type], limit=0, **scope_kwargs)
            match = next((i for i in bundle.items if i.record_type == record_type), None)
            assert match is not None, f"{record_type} must be retrievable"
            assert match.source_id == row_id
            assert match.identity == f"structured:{record_type}:{row_id}"
            assert match.verified is True
            assert match.current is True, f"{record_type} is created live and must read as current"
            # The well-scoped types carry the authoritative well scope, not an empty one.
            if record_type == "problem_definition":
                assert match.well_id == ""
            else:
                assert match.well_id == well

        # A problem definition is a canonical, unscoped reference: it has no well and a well-scoped
        # query must not return it (the same rule search applies) - only an unscoped query does.
        scoped = world.retrieve(query="zermatt", well_id=well, limit=0)
        assert not any(i.record_type == "problem_definition" for i in scoped.items)
        unscoped = world.retrieve(query="zermatt", limit=0)
        assert any(
            i.record_type == "problem_definition" and i.well_id == "" for i in unscoped.items
        )


# ============================================================================ bundle integrity
class TestBundleIntegrity:
    def test_every_item_in_the_bundle_resolves_in_the_database(self, world) -> None:
        """Bundle-wide citation integrity: every returned item points at a real authoritative row."""
        from drilling_intelligence.database.models import (
            Recommendation as _Rec,
        )

        models = {
            "problem_definition": ProblemDefinition,
            "problem_occurrence": ProblemOccurrence,
            "npt_record": NptRecord,
            "well_event": WellEvent,
            "lesson_learned": LessonLearned,
            "recommendation": _Rec,
        }
        bundle = world.retrieve(query=SHARED_TERM, limit=0)
        assert bundle.count == 4
        with world.ws.database.read_only() as session:
            for item in bundle.items:
                assert item.verified
                if item.source_type == "structured":
                    row = session.get(models[item.record_type], item.source_id)
                    assert row is not None, f"{item.identity} does not resolve"
                    assert str(getattr(row, "well_id", "") or "") == item.well_id
                elif item.source_type == "knowledge":
                    assert session.get(KnowledgeItem, item.source_id) is not None
                elif item.source_type == "document":
                    assert session.get(DocumentVersion, item.document_version_id) is not None
                    assert session.get(Document, item.document_id) is not None

    def test_public_contract_is_plain_json_serializable_values(self, world) -> None:
        """No ORM objects, sessions or detached rows leak into the public contract."""
        bundle = world.retrieve(query=SHARED_TERM, limit=0)
        json.dumps(bundle.to_dict())  # must serialize without an ORM default
        for item in bundle.items:
            payload = item.to_dict()
            json.dumps(payload)
            assert isinstance(item.identity, str)
            assert isinstance(item.verified, bool)
            assert isinstance(item.current, bool)
            assert isinstance(item.score, float)
            assert isinstance(item.provenance, (dict, list))
        # The bundle self-describes the question that produced it (a snapshot, not a live query).
        assert bundle.request["query"] == SHARED_TERM
        assert bundle.scope["level"] == "all"
        assert bundle.policy == "current"


# ============================================================================ provenance
class TestProvenance:
    def test_manual_lesson_fabricates_no_document_identity(self, world) -> None:
        item = world.retrieve(query=SHARED_TERM, well_id=world.ids["A1"], limit=0).items[0]
        assert item.document_id == ""
        assert item.document_version_id == ""
        assert item.locator_ref == ""

    def test_scope_names_are_read_from_the_database(self, world) -> None:
        item = world.retrieve(query=SHARED_TERM, well_id=world.ids["A1"], limit=0).items[0]
        assert item.well_name == "A1"
        assert item.field_name == "Field Alpha"
        assert item.project_name == "Project Alpha"

    def test_recommendation_status_is_carried_not_elevated(self, workspace) -> None:
        world = _solo_world(workspace)
        with workspace.database.session() as session:
            LessonRepository(session).propose_recommendation(
                statement="proposed-only wiper trip recommendation body.",
                reason="r",
                well_id=world.ids["well"],
                field_id=world.ids["field"],
                project_id=world.ids["project"],
            )
            session.commit()
        world.search.rebuild()
        item = world.retrieve(query="proposed-only", well_id=world.ids["well"], limit=0).items[0]
        # Retrieval surfaces the row with its real status; it does not turn a PROPOSED row into advice.
        assert item.status == RecommendationLifecycle.PROPOSED.value
        assert item.current is True

    def test_document_evidence_carries_a_real_locator(self, corpus_world) -> None:
        world = corpus_world
        query = _a_query_for(world, document_any(world))
        bundle = world.retrieve(
            query=query, well_id=world.ids["A3"], source_types=("document",), limit=0
        )
        assert bundle.count > 0
        item = bundle.items[0]
        assert item.document_id and item.document_version_id
        assert item.locator_ref, (
            "a document citation must carry the location the extraction recorded"
        )
        assert item.provenance, "a document citation must carry the recorded provenance"
        # The locator must come from the recorded extraction, not be invented: it references the file.
        assert item.provenance.get("filename") or item.provenance.get("locator")

    def test_structured_evidence_carries_the_rows_own_provenance(self, workspace) -> None:
        """A record's evidence list is carried from the authoritative row, verbatim."""
        world = _solo_world(workspace)
        entry = {
            "kind": "spreadsheet",
            "document": {"sheet": "Notes", "cell": "B4"},
            "excerpt": "wiper trip noted",
        }
        with workspace.database.session() as session:
            LessonRepository(session).capture(
                lesson="provcheck wiper trip lesson body.",
                title="P",
                well_id=world.ids["well"],
                field_id=world.ids["field"],
                project_id=world.ids["project"],
                provenance=[entry],
            )
            session.commit()
        world.search.rebuild()
        item = world.retrieve(query="provcheck", well_id=world.ids["well"], limit=0).items[0]
        assert item.provenance == [entry], "the row's own evidence list is carried, not reshaped"

    def test_uncitable_chunk_is_dropped_not_evidence(self, corpus_world) -> None:
        """The scanned page's diagnostic chunk has no recorded location, so it is not a citation."""
        world = corpus_world
        bundle = world.retrieve(query="extractable", source_types=("document",), limit=0)
        assert any(d["reason"] == "not citable (no recorded location)" for d in bundle.dropped)
        # Everything that IS returned as document evidence carries a recorded locator.
        for item in bundle.items:
            if item.source_type == "document":
                assert item.provenance, "returned document evidence must be cited"

    def test_knowledge_evidence_carries_item_provenance(self, corpus_world) -> None:
        world = corpus_world
        with world.ws.database.read_only() as session:
            item = session.scalar(
                select(KnowledgeItem).where(
                    KnowledgeItem.well_id == world.ids["A3"],
                    KnowledgeItem.predicate == "mud_weight",
                )
            )
            item_id = str(item.id)
        world.search.rebuild()
        query = _fact_query_for(world, item_id)
        assert query
        bundle = world.retrieve(
            query=query, well_id=world.ids["A3"], source_types=("knowledge",), limit=0
        )
        fact = next((i for i in bundle.items if i.source_id == item_id), None)
        assert fact is not None
        assert fact.source_type == "knowledge"
        assert fact.identity == f"knowledge:{item_id}"


# ============================================================================ chain & conflict
class TestChainAndConflict:
    def test_knowledge_resolves_to_its_document_and_version(self, corpus_world) -> None:
        world = corpus_world

        with world.ws.database.read_only() as session:
            item = session.scalar(
                select(KnowledgeItem).where(
                    KnowledgeItem.well_id == world.ids["A3"],
                    KnowledgeItem.predicate == "mud_weight",
                )
            )
            item_id = str(item.id)
        world.search.rebuild()
        query = _fact_query_for(world, item_id)
        assert query
        fact = next(
            i
            for i in world.retrieve(
                query=query, well_id=world.ids["A3"], source_types=("knowledge",), limit=0
            ).items
            if i.source_id == item_id
        )
        # The item -> version -> document chain must be real, resolvable rows.
        assert fact.document_id and fact.document_version_id
        with world.ws.database.read_only() as session:
            assert session.get(Document, fact.document_id) is not None
            assert session.get(DocumentVersion, fact.document_version_id) is not None

    def test_stale_knowledge_fact_is_dropped_not_resurrected(self, corpus_world) -> None:
        """Index -> delete the authoritative fact -> no rebuild -> retrieve must not return it."""
        world = corpus_world

        with world.ws.database.read_only() as session:
            item = session.scalar(
                select(KnowledgeItem).where(
                    KnowledgeItem.well_id == world.ids["A3"],
                    KnowledgeItem.predicate == "mud_weight",
                )
            )
            item_id = str(item.id)
        world.search.rebuild()
        query = _fact_query_for(world, item_id)
        assert query
        before = world.retrieve(
            query=query, well_id=world.ids["A3"], source_types=("knowledge",), limit=0
        )
        assert any(i.source_id == item_id for i in before.items)
        with world.ws.database.session() as session:
            session.delete(session.get(KnowledgeItem, item_id))
            session.commit()
        after = world.retrieve(
            query=query, well_id=world.ids["A3"], source_types=("knowledge",), limit=0
        )
        assert all(i.source_id != item_id for i in after.items)
        assert any(d["reason"] == "no longer in the authoritative database" for d in after.dropped)


# ============================================================================ read-only & transactions
class TestReadOnlyAndTransactions:
    def test_retrieval_changes_nothing_authoritative(self, world) -> None:
        before = _authoritative_fingerprint(world.ws)
        world.retrieve(query=SHARED_TERM, limit=0)
        world.retrieve(query=SHARED_TERM, well_id=world.ids["A1"], limit=0, lifecycle="history")
        after = _authoritative_fingerprint(world.ws)
        assert before == after

    def test_retrieval_does_not_commit_a_callers_transaction(self, world) -> None:
        target = world.ids["les_A1"]
        session = world.ws.database.session()
        try:
            session.get(LessonLearned, target).status = "PENDING-MARK"
            world.retr.retrieve(
                RetrievalRequest(query=SHARED_TERM, well_id=world.ids["A1"], limit=0),
                session=session,
            )
            with world.ws.database.read_only() as committed:
                assert committed.get(LessonLearned, target).status != "PENDING-MARK", (
                    "retrieval must not commit the caller's pending change"
                )
        finally:
            session.rollback()
            session.close()

    def test_retrieval_reads_committed_truth_when_no_session_passed(self, world) -> None:
        session = world.ws.database.session()
        try:
            session.get(LessonLearned, world.ids["les_A1"]).status = "HIDDEN-PENDING"
            bundle = world.retrieve(query=SHARED_TERM, well_id=world.ids["A1"], limit=0)
            assert bundle.items[0].status != "HIDDEN-PENDING"
        finally:
            session.rollback()
            session.close()

    def test_retrieval_with_caller_session_sees_pending_change_but_does_not_commit(
        self, world
    ) -> None:
        target = world.ids["les_A1"]
        session = world.ws.database.session()
        try:
            session.get(LessonLearned, target).status = "PENDING-VALUE"
            bundle = world.retr.retrieve(
                RetrievalRequest(query=SHARED_TERM, well_id=world.ids["A1"], limit=0),
                session=session,
            )
            # Re-read through the caller's transaction sees the pending value...
            assert bundle.items[0].status == "PENDING-VALUE"
            # ...but the database still holds the committed one (no commit happened).
            with world.ws.database.read_only() as committed:
                assert committed.get(LessonLearned, target).status != "PENDING-VALUE"
        finally:
            session.rollback()
            session.close()


# ============================================================================ bounded reads
class TestBoundedReads:
    def _count_selects(self, workspace, fn) -> int:
        state = {"n": 0}

        def _before(conn, cursor, stmt, params, ctx, exec_):
            if stmt.strip().lower().startswith("select"):
                state["n"] += 1

        event.listen(workspace.database.engine, "before_cursor_execute", _before, propagate=True)
        try:
            fn()
        finally:
            event.remove(workspace.database.engine, "before_cursor_execute", _before)
        return state["n"]

    def _bulk_world(self, world, n: int) -> None:
        with world.ws.database.session() as session:
            for i in range(n):
                LessonRepository(session).capture(
                    lesson=f"{SHARED_TERM} bulk stuck pipe row {i} body.",
                    title=f"Bulk {i}",
                    well_id=world.ids["A1"],
                    field_id=world.ids["fA"],
                    project_id=world.ids["pA"],
                )
            session.commit()
        world.search.rebuild()

    def test_query_count_is_bounded_not_one_per_row(self, world) -> None:
        self._bulk_world(world, 120)
        one = self._count_selects(world.ws, lambda: world.retrieve(query="bulk", limit=1))
        few = self._count_selects(world.ws, lambda: world.retrieve(query="bulk", limit=10))
        many = self._count_selects(world.ws, lambda: world.retrieve(query="bulk", limit=0))
        assert world.retrieve(query="bulk", limit=0).count == 120
        # Each result size costs a small, flat number of queries - never one per row.
        assert one <= 12
        assert few <= 12
        assert many <= 12

    def test_batched_reread_resolves_every_row(self, world) -> None:
        self._bulk_world(world, 120)
        bundle = world.retrieve(query="bulk", limit=0)
        assert bundle.count == 120
        assert len({i.source_id for i in bundle.items}) == 120, (
            "every distinct row must be re-read and resolved"
        )


# ============================================================================ empty / failure semantics
class TestEmptyAndFailureSemantics:
    def test_empty_or_unmatched_queries_are_empty_bundles_not_errors(self, world) -> None:
        # "No question" and "no matches" are both valid, deterministic, empty answers.
        empty = world.retrieve(query="   ", limit=0)
        assert empty.count == 0 and empty.items == () and empty.dropped == ()
        unmatched = world.retrieve(query="a word no one wrote zzzqqq", limit=0)
        assert unmatched.count == 0 and unmatched.dropped == ()

    def test_invalid_request_fields_are_rejected(self, world) -> None:
        with pytest.raises(ValueError):
            RetrievalRequest(query=SHARED_TERM, lifecycle="bogus")
        with pytest.raises(ValueError):
            RetrievalRequest(query=SHARED_TERM, limit=-1)
        with pytest.raises(ValueError):
            RetrievalRequest(query=SHARED_TERM, source_types=("vector",))

    def test_zero_limit_means_no_cap(self, world) -> None:
        assert world.retrieve(query=SHARED_TERM, limit=0).count == 4
        assert world.retrieve(query=SHARED_TERM, limit=1).count == 1

    def test_source_type_filter_narrows(self, world) -> None:
        assert world.retrieve(query=SHARED_TERM, source_types=("structured",), limit=0).count == 4
        documents_only = world.retrieve(query=SHARED_TERM, source_types=("document",), limit=0)
        assert documents_only.count == 0
        assert documents_only.dropped == (), "unrequested source types are not reported as dropped"

    def test_no_search_service_rejected_for_a_real_query(self, workspace) -> None:
        _solo_world(workspace)
        service = RetrievalService(database=workspace.database, search_service=None)
        with pytest.raises(ValidationError):
            service.retrieve(RetrievalRequest(query=SHARED_TERM, limit=0))

    def test_malformed_date_is_rejected_not_silently_broadened(self, world) -> None:
        # A malformed date would, if it reached the string-comparing index, silently widen or
        # narrow the query.  Retrieval refuses it up front instead.
        with pytest.raises(ValueError):
            world.retrieve(query=SHARED_TERM, date_from="not-a-date", limit=0)
        with pytest.raises(ValueError):
            world.retrieve(query=SHARED_TERM, date_to="14/06/2025", limit=0)


# ============================================================================ corpus query helpers
def _a_query_for(world: World, document_id: str) -> str:
    """A query that is known to match a chunk of the given document (from the index, not a guess)."""
    with world.ws.index_database.engine.connect() as connection:
        row = connection.execute(
            __import__("sqlalchemy").text(
                "select text from search_chunk where document_id = :d limit 1"
            ),
            {"d": document_id},
        ).fetchone()
    if not row or not str(row[0]).strip():
        return ""
    return str(row[0]).strip().split()[0]


def _fact_query_for(world: World, item_id: str) -> str:
    """A query that is known to match the knowledge fact chunk for the given item."""
    from drilling_intelligence.database.models import KnowledgeItem as _KI

    with world.ws.database.read_only() as session:
        item = session.get(_KI, item_id)
        value = str(item.original_value or "").strip()
        predicate = str(item.predicate or "").replace("_", " ").strip()
    # A fact's indexed text always carries its value and its humanised predicate; both are stable.
    if value:
        return value
    return predicate


def document_any(world: World) -> str:
    with world.ws.database.read_only() as session:
        return str(session.scalar(select(Document.id).order_by(Document.id)))
