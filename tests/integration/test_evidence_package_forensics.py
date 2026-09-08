"""P10 forensic verification of the Evidence Query & Package layer.

The layer under test sits strictly above the certified chain - search discovers, retrieval
verifies, and the package composes what retrieval verified.  These tests therefore attack the
layer's own promises:

*   **addressing** - the package's content identity is the same for the same database state and
    the same question, whatever the topic order, the insertion order or the display order;
*   **composition** - an item two topics find is one item carrying both topics; coverage says,
    per topic, what was returned, what was dropped and why, and whether discovery broadened;
*   **freshness** - a package stores its own query; staleness is a named diff (added / removed /
    changed) against a re-read of the authoritative database, not a timestamp;
*   **safety** - scope isolation, the read-only promise, bounded reads, and the refusal to run
    at all without the retrieval service (evidence is only ever produced by retrieval).

No mocks anywhere: real workspace, real SQLite, real repositories, real sidecar - the same rules
the retrieval forensics (P9) apply, applied to the layer that composes its answers.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from io import StringIO
from typing import Any

import pytest
from sqlalchemy import event, select, text

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import (
    Document,
    DocumentVersion,
    KnowledgeItem,
    LessonLearned,
    NptRecord,
    ProblemDefinition,
    ProblemOccurrence,
    Recommendation,
    WellEvent,
)
from drilling_intelligence.evidence import (
    EvidencePackage,
    EvidenceQuery,
    EvidenceQueryService,
)
from drilling_intelligence.lessons.repository import LessonRepository
from drilling_intelligence.retrieval.service import RetrievalService
from drilling_intelligence.search.service import SearchService
from drilling_intelligence.wells.repository import WellRepository

# Two words that occur in the same record, so a two-topic query can meet on one item.
SHARED_TERM = "quartzite"
SECOND_TERM = "wiper"


# ============================================================================ helpers
def _authoritative_fingerprint(workspace) -> str:
    """A hash over every table of the authoritative database - a write anywhere moves it."""
    out: list[str] = []
    with workspace.database.engine.connect() as conn:
        tables = [
            row[0]
            for row in conn.execute(
                text(
                    "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                )
            ).fetchall()
        ]
        for table in sorted(tables):
            # The names come from sqlite_master itself (never a caller), but a name that is
            # not a plain identifier is corruption, and corruption is not interpolated into SQL.
            assert re.fullmatch(r"[a-z_][a-z0-9_]*", table), f"unexpected table name {table!r}"
            rows = conn.execute(text(f'select rowid, * from "{table}" order by rowid')).fetchall()  # noqa: S608
            out.append(
                f"{table}:{len(rows)}:" + hashlib.sha256(repr(rows).encode()).hexdigest()[:16]
            )
    return hashlib.sha256("\n".join(out).encode()).hexdigest()


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


@dataclass
class World:
    """A scope topology with the same searchable text in a record in every well."""

    ws: Any
    search: SearchService
    retr: RetrievalService
    evq: EvidenceQueryService
    ids: dict[str, Any] = field(default_factory=dict)

    def query(self, **kwargs: Any) -> EvidencePackage:
        return self.evq.query(EvidenceQuery(**kwargs))


def _service_for(workspace) -> World:
    search = SearchService.for_workspace(workspace)
    retrieval = RetrievalService(database=workspace.database, search_service=search)
    return World(
        ws=workspace,
        search=search,
        retr=retrieval,
        evq=EvidenceQueryService(retrieval=retrieval),
    )


@pytest.fixture
def world(workspace) -> World:
    """Project Alpha { Field Alpha { A1, A2 }, Field Beta { B1 } }, Project Bravo { Field Charlie { C1 } }.

    Every well carries one lesson stating both topics' words, so a scoped package has exactly one
    item per well in scope - the shape a leak test needs.
    """
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
        for well_id, field_id, project_id in (
            (a1.id, field_a.id, proj_a.id),
            (a2.id, field_a.id, proj_a.id),
            (b1.id, field_b.id, proj_a.id),
            (c1.id, field_c.id, proj_b.id),
        ):
            lessons.capture(
                lesson=f"{SHARED_TERM} {SECOND_TERM} trip body stuck on the rotary program.",
                title=f"{SHARED_TERM} lesson",
                well_id=well_id,
                field_id=field_id,
                project_id=project_id,
            )
        session.commit()
        for key, well_id in (
            ("les_A1", a1.id),
            ("les_A2", a2.id),
            ("les_B1", b1.id),
            ("les_C1", c1.id),
        ):
            row = session.scalar(
                select(LessonLearned).where(
                    LessonLearned.well_id == well_id,
                    LessonLearned.title == f"{SHARED_TERM} lesson",
                )
            )
            ids[key] = row.id
    service = _service_for(ws)
    service.ids = ids
    service.search.rebuild()
    return service


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


def _fixed_lesson(session, lesson_id: str, *, well_id: str, field_id: str, project_id: str) -> None:
    """One lesson with a fixed id: insertion-order tests need rows that do not earn new ids."""
    session.add(
        LessonLearned(
            id=lesson_id,
            title=f"{SHARED_TERM} lesson",
            lesson=f"{SHARED_TERM} {SECOND_TERM} trip body stuck on the rotary program.",
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
            status="DRAFT",
            is_current=True,
        )
    )


def _current_pairs(session) -> list[tuple[str, str]]:
    """Every (document, current version) pair, deterministic order, for a knowledge sync."""
    rows = list(
        session.execute(
            select(DocumentVersion.document_id, DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentVersion.is_current.is_(True))
            .order_by(Document.filename, DocumentVersion.version_number)
        ).all()
    )
    return [(str(document_id), str(version_id)) for document_id, version_id in rows]


# ============================================================================ identity
class TestIdentityAndDeterminism:
    def test_package_identity_is_stable_for_same_state_and_request(self, world) -> None:
        """Two fresh services, one database state, one question: the same address, byte for byte."""
        first = world.query(topics=(SHARED_TERM,), well_id=world.ids["A1"])
        other_service = EvidenceQueryService(
            retrieval=RetrievalService(database=world.ws.database, search_service=world.search)
        )
        second = other_service.query(EvidenceQuery(topics=(SHARED_TERM,), well_id=world.ids["A1"]))
        assert first.identity == second.identity
        assert re.fullmatch(r"evpkg:[0-9a-f]{64}", first.identity)
        assert first.to_dict() == second.to_dict()
        json.dumps(first.to_dict())  # the whole package serialises without an ORM default

    def test_identity_is_invariant_to_topic_order(self, world) -> None:
        """The address names the evidence, not the order the topics were asked in."""
        ab = world.query(topics=(SHARED_TERM, SECOND_TERM), field_id=world.ids["fA"])
        ba = world.query(topics=(SECOND_TERM, SHARED_TERM), field_id=world.ids["fA"])
        assert ab.identity == ba.identity
        assert {e.item.identity for e in ab.items} == {e.item.identity for e in ba.items}
        # Display order does follow the topics: the first item was found by the first topic.
        assert ab.items[0].found_by[0] == SHARED_TERM
        assert ba.items[0].found_by[0] == SECOND_TERM

    def test_identity_is_insertion_order_independent(self, world) -> None:
        """The same rows written in a different order earn the same address for the same question."""
        with world.ws.database.session() as session:
            session.query(LessonLearned).delete()
            _fixed_lesson(
                session,
                "les-X",
                well_id=world.ids["A1"],
                field_id=world.ids["fA"],
                project_id=world.ids["pA"],
            )
            _fixed_lesson(
                session,
                "les-Y",
                well_id=world.ids["A2"],
                field_id=world.ids["fA"],
                project_id=world.ids["pA"],
            )
            session.commit()
        world.search.rebuild()
        first = world.query(topics=(SHARED_TERM,), field_id=world.ids["fA"])
        with world.ws.database.session() as session:
            session.query(LessonLearned).delete()
            _fixed_lesson(
                session,
                "les-Y",
                well_id=world.ids["A2"],
                field_id=world.ids["fA"],
                project_id=world.ids["pA"],
            )
            _fixed_lesson(
                session,
                "les-X",
                well_id=world.ids["A1"],
                field_id=world.ids["fA"],
                project_id=world.ids["pA"],
            )
            session.commit()
        world.search.rebuild()
        second = world.query(topics=(SHARED_TERM,), field_id=world.ids["fA"])
        assert first.identity == second.identity
        assert {e.item.source_id for e in first.items} == {"les-X", "les-Y"}

    def test_no_volatile_data_in_the_package(self, world) -> None:
        blob = json.dumps(world.query(topics=(SHARED_TERM,), limit=0).to_dict(), default=str)
        assert not re.search(r"0x[0-9a-f]{6,}", blob), "no memory addresses"
        assert "<" not in blob, "no Python object reprs"


# ============================================================================ composition
class TestCompositionAndCoverage:
    def test_an_item_found_by_two_topics_appears_once_with_both_topics(self, world) -> None:
        pkg = world.query(topics=(SHARED_TERM, SECOND_TERM), well_id=world.ids["A1"])
        assert pkg.count == 1
        entry = pkg.items[0]
        assert entry.item.source_id == world.ids["les_A1"]
        assert entry.found_by == (SHARED_TERM, SECOND_TERM)
        # Coverage still counts the hit under both topics - the item is once, the evidence twice.
        assert [c.returned for c in pkg.coverage] == [1, 1]

    def test_coverage_accounts_each_topic_separately(self, world) -> None:
        """An empty answer is data, not an error: the topic says 0, the package stands."""
        pkg = world.query(
            topics=(SHARED_TERM, "a word no one wrote zzzqqq"), well_id=world.ids["A1"]
        )
        assert pkg.count == 1
        first, second = pkg.coverage
        assert first.topic == SHARED_TERM and first.returned == 1
        assert second.returned == 0 and second.dropped == 0 and second.drop_reasons == ()

    def test_a_broadened_topic_is_labelled_in_its_coverage(self, world) -> None:
        """Search's any-of fallback is inherited, but the label travels to the coverage."""
        exact = world.query(topics=(SHARED_TERM,), well_id=world.ids["A1"])
        broadened = world.query(topics=(f"{SHARED_TERM} zzzqqq",), well_id=world.ids["A1"])
        assert exact.coverage[0].broadened is False
        assert broadened.coverage[0].broadened is True
        assert broadened.count == 1, "the broadened topic still verifies its one hit"

    def test_items_are_ordered_deterministically(self, world) -> None:
        """A1 and A2 carry identical text (equal score): the order is fixed, not row order."""
        first = [
            e.item.identity
            for e in world.query(topics=(SHARED_TERM,), field_id=world.ids["fA"]).items
        ]
        again = [
            e.item.identity
            for e in world.query(topics=(SHARED_TERM,), field_id=world.ids["fA"]).items
        ]
        assert first == again and len(first) == 2
        # Equal scores break ties on identity, so the order is the same the hash order.
        assert first == sorted(first)

    def test_every_item_in_the_package_resolves_in_the_database(self, world) -> None:
        """Package-wide citation integrity: every item points at a real authoritative row."""
        models = {
            "problem_definition": ProblemDefinition,
            "problem_occurrence": ProblemOccurrence,
            "npt_record": NptRecord,
            "well_event": WellEvent,
            "lesson_learned": LessonLearned,
            "recommendation": Recommendation,
        }
        pkg = world.query(topics=(SHARED_TERM,), limit=0)
        assert pkg.count > 0
        with world.ws.database.read_only() as session:
            for entry in pkg.items:
                item = entry.item
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

    def test_a_conflicted_knowledge_item_is_carried_not_resolved(self, corpus_world) -> None:
        """Two sources disagree, the knowledge layer marks both CONFLICTED - the package carries
        that state instead of hiding it or picking a winner."""
        from tests.fixtures.knowledge import artefact_field, register_artefact

        from drilling_intelligence.knowledge.service import KnowledgeExtractionService

        world = corpus_world
        with world.ws.database.session() as session:
            register_artefact(
                session,
                filename="quartz-1.xlsx",
                identity_path="docs/quartz-1.xlsx",
                well_id=world.ids["A3"],
                fields=(
                    artefact_field("quartzite_reading", "7.5", "ppg", filename="quartz-1.xlsx"),
                ),
            )
            register_artefact(
                session,
                filename="quartz-2.xlsx",
                identity_path="docs/quartz-2.xlsx",
                well_id=world.ids["A3"],
                fields=(
                    artefact_field("quartzite_reading", "7.9", "ppg", filename="quartz-2.xlsx"),
                ),
            )
            service = KnowledgeExtractionService(
                database=world.ws.database, index=None, refresh_index=False
            )
            for document_id, version_id in _current_pairs(session):
                service.sync_version(document_id, version_id, session=session)
            session.commit()
            conflicted = list(
                session.execute(
                    select(KnowledgeItem).where(
                        KnowledgeItem.predicate == "quartzite_reading",
                        KnowledgeItem.status == "CONFLICTED",
                    )
                ).scalars()
            )
        assert len(conflicted) == 2, "the disagreement must mark both sides, not settle one"
        world.search.rebuild()
        pkg = world.query(
            topics=("quartzite reading",),  # the words, not the predicate token
            source_types=("knowledge",),
            well_id=world.ids["A3"],
        )
        assert pkg.count == 2
        for entry in pkg.items:
            assert entry.item.status == "CONFLICTED"
            assert entry.item.current is True, (
                "a conflict is a state of the evidence, not a reason to hide it"
            )


# ============================================================================ scope
class TestScopeAndLifecycle:
    def test_well_scope_is_isolated(self, world) -> None:
        pkg = world.query(topics=(SHARED_TERM,), well_id=world.ids["A1"])
        assert pkg.count == 1
        assert pkg.items[0].item.well_id == world.ids["A1"]
        assert pkg.scope["level"] == "well"

    def test_field_scope_covers_only_its_wells(self, world) -> None:
        pkg = world.query(topics=(SHARED_TERM,), field_id=world.ids["fA"])
        wells = {e.item.well_id for e in pkg.items}
        assert wells == {world.ids["A1"], world.ids["A2"]}

    def test_project_scope_covers_only_its_wells(self, world) -> None:
        pkg = world.query(topics=(SHARED_TERM,), project_id=world.ids["pA"])
        wells = {e.item.well_id for e in pkg.items}
        assert wells == {world.ids["A1"], world.ids["A2"], world.ids["B1"]}
        assert world.ids["C1"] not in wells, "Project Bravo's well must not leak into Alpha"

    def test_unknown_scope_is_rejected_not_silently_empty(self, world) -> None:
        with pytest.raises(ValidationError):
            world.query(topics=(SHARED_TERM,), well_id="no-such-well")

    def test_superseded_lesson_is_history_not_current_under_a_stale_sidecar(self, world) -> None:
        """Revise without a rebuild: CURRENT holds the stale row back, HISTORY surfaces it."""
        lesson_id = world.ids["les_A1"]
        current = world.query(topics=(SHARED_TERM,), well_id=world.ids["A1"])
        assert current.count == 1
        with world.ws.database.session() as session:
            LessonRepository(session).revise(
                lesson_id, by="engineer", changes={"lesson": "tightened wording."}
            )
            session.commit()
        after_current = world.query(topics=(SHARED_TERM,), well_id=world.ids["A1"])
        assert all(e.item.source_id != lesson_id for e in after_current.items)
        assert any(
            d["reason"] == "not current (status SUPERSEDED)" and d["identity"].endswith(lesson_id)
            for d in after_current.coverage[0].drop_reasons
        )
        after_history = world.query(
            topics=(SHARED_TERM,), well_id=world.ids["A1"], lifecycle="history"
        )
        stale = next((e for e in after_history.items if e.item.source_id == lesson_id), None)
        assert stale is not None
        assert stale.item.current is False
        assert stale.item.status == "SUPERSEDED"
        assert after_history.policy == "history"


# ============================================================================ freshness
class TestFreshnessAndStaleness:
    def test_a_package_is_fresh_over_unchanged_data(self, world) -> None:
        pkg = world.query(topics=(SHARED_TERM,), field_id=world.ids["fA"])
        report = world.evq.check_freshness(pkg)
        assert report.fresh
        assert report.stored_identity == report.current_identity
        assert report.added == () and report.removed == () and report.changed == ()

    def test_a_stale_sidecar_mutation_is_detected_with_a_named_diff(self, world) -> None:
        """The mandated forensic at the package level: index -> mutate -> no rebuild -> check."""
        pkg = world.query(topics=(SHARED_TERM,), well_id=world.ids["A1"])
        lesson_id = world.ids["les_A1"]
        with world.ws.database.session() as session:
            LessonRepository(session).revise(
                lesson_id, by="engineer", changes={"lesson": "tightened wording."}
            )
            session.commit()
        report = world.evq.check_freshness(pkg)
        assert report.fresh is False
        assert report.removed == (f"structured:lesson_learned:{lesson_id}",)
        # The new revision is not yet in the disposable index, so it is not (yet) evidence:
        # discovery, not the package, is where new rows appear.
        assert report.added == ()

    def test_a_rebuild_moves_the_package_to_the_new_revision(self, world) -> None:
        pkg = world.query(topics=(SHARED_TERM,), well_id=world.ids["A1"])
        old_id = world.ids["les_A1"]
        with world.ws.database.session() as session:
            LessonRepository(session).revise(
                old_id, by="engineer", changes={"lesson": "tightened wording."}
            )
            session.commit()
        world.search.rebuild()
        report = world.evq.check_freshness(pkg)
        assert report.fresh is False
        assert report.removed == (f"structured:lesson_learned:{old_id}",)
        assert len(report.added) == 1
        assert report.added[0].startswith("structured:lesson_learned:")
        assert report.added[0] != report.removed[0]

    def test_a_new_authoritative_row_is_not_evidence_until_the_index_holds_it(self, world) -> None:
        """Freshness compares discovery-plus-verification; an unindexed row cannot move a package.

        This is the honest boundary the sidecar makes (ADR-0013): the package is fresh because
        nothing it could discover moved, and the new row becomes evidence after a rebuild - never
        before, never silently.
        """
        pkg = world.query(topics=(SHARED_TERM,), well_id=world.ids["A1"])
        with world.ws.database.session() as session:
            LessonRepository(session).capture(
                lesson=f"{SHARED_TERM} latecomer body stuck.",
                title="Latecomer",
                well_id=world.ids["A1"],
                field_id=world.ids["fA"],
                project_id=world.ids["pA"],
            )
            session.commit()
        assert world.evq.check_freshness(pkg).fresh
        world.search.rebuild()
        after = world.evq.check_freshness(pkg)
        assert after.fresh is False
        assert len(after.added) == 1, "the rebuild is what brings the new row into evidence"

    def test_a_status_move_on_a_current_row_is_reported_as_changed(self, world) -> None:
        """Same identity, same index - but the row's authoritative state moved: that is 'changed'."""
        pkg = world.query(topics=(SHARED_TERM,), well_id=world.ids["A1"])
        lesson_id = world.ids["les_A1"]
        with world.ws.database.session() as session:
            LessonRepository(session).submit_for_review(lesson_id, by="engineer")
            session.commit()
        report = world.evq.check_freshness(pkg)
        assert report.fresh is False
        assert report.added == () and report.removed == ()
        assert len(report.changed) == 1
        changed = report.changed[0]
        assert changed["identity"] == f"structured:lesson_learned:{lesson_id}"
        assert changed["status"]["from"] == "DRAFT"
        assert changed["status"]["to"] == "REVIEW"


# ============================================================================ contract & safety
class TestContractAndSafety:
    def test_malformed_queries_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            EvidenceQuery(topics=())
        with pytest.raises(ValueError):
            EvidenceQuery(topics=("   ",))
        with pytest.raises(ValueError):
            EvidenceQuery(topics=("q",), lifecycle="bogus")
        with pytest.raises(ValueError):
            EvidenceQuery(topics=("q",), limit=-1)
        with pytest.raises(ValueError):
            EvidenceQuery(topics=("q",), source_types=("vector",))
        with pytest.raises(ValueError):
            EvidenceQuery(topics=("q",), date_from="not-a-date")

    def test_the_service_refuses_to_run_without_retrieval(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceQueryService(retrieval=None)

    def test_queries_are_read_only(self, world) -> None:
        before = _authoritative_fingerprint(world.ws)
        pkg = world.query(topics=(SHARED_TERM, SECOND_TERM), limit=0)
        world.evq.check_freshness(pkg)
        assert _authoritative_fingerprint(world.ws) == before

    def test_the_package_issues_bounded_authoritative_queries(self, world) -> None:
        """One retrieval per topic, batched re-reads inside: the count does not grow with items."""
        baseline = _select_count(
            world.ws.database.engine,
            lambda: world.query(topics=(SHARED_TERM, SECOND_TERM), field_id=world.ids["fA"]),
        )
        assert baseline > 0
        with world.ws.database.session() as session:
            for index in range(5):
                LessonRepository(session).capture(
                    lesson=f"{SHARED_TERM} extra body {index} stuck.",
                    title=f"Extra {index}",
                    well_id=world.ids["A2"],
                    field_id=world.ids["fA"],
                    project_id=world.ids["pA"],
                )
            session.commit()
        world.search.rebuild()
        larger = _select_count(
            world.ws.database.engine,
            lambda: world.query(topics=(SHARED_TERM, SECOND_TERM), field_id=world.ids["fA"]),
        )
        assert larger == baseline, "adding rows must not add per-item queries"


# ============================================================================ CLI
class TestCommandLine:
    def _call(self, world, *argv: str) -> tuple[int, str]:
        """One ``--json`` command, stdout captured - the payload is the only thing on the stream."""
        import sys

        from drilling_intelligence.cli.app import main

        out, err = StringIO(), StringIO()
        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = main(["evidence", "query", "--workspace", str(world.ws.root), *argv, "--json"])
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err
        return int(code), out.getvalue()

    def test_the_cli_prints_the_same_document_twice_and_checks_freshness(self, world) -> None:
        argv = ["--topic", SHARED_TERM, "--well", "A1"]
        code_one, out_one = self._call(world, *argv)
        code_two, out_two = self._call(world, *argv)
        assert code_one == 0 and code_two == 0
        doc_one = json.loads(out_one)
        doc_two = json.loads(out_two)
        assert doc_one == doc_two, "the CLI must print the same package for the same state"
        identity = doc_one["identity"]
        assert identity.startswith("evpkg:")
        assert doc_one["count"] == 1
        fresh_code, fresh_out = self._call(world, *argv, "--expect", identity)
        assert fresh_code == 0
        assert json.loads(fresh_out)["fresh"] is True
        stale_code, stale_out = self._call(world, *argv, "--expect", "evpkg:" + "0" * 64)
        assert stale_code == 1
        assert json.loads(stale_out)["fresh"] is False
