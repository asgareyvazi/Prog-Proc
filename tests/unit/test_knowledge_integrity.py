"""Knowledge edges stay pointed at real rows - without a foreign key per table.

The graph is deliberately polymorphic (ADR-0006): ``source_type``/``source_id`` and
``target_type``/``target_id`` instead of twenty optional foreign keys, because an edge can
join a document to a knowledge item, a section to a survey, a lesson to a well, and Phase
2 will add endpoint types without a schema migration.  The price of that design is that
nothing in the database can refuse an edge pointing at a row that does not exist - and a
dangling edge in a knowledge graph is worse than a missing one, because the UI and every
traversal will present it as a real relationship.

So the guarantee is enforced in two places, and both are tested here:
:func:`create_knowledge_relation` refuses before persistence, and
:func:`check_knowledge_relations` reports edges that are already broken (deleted rows,
hand-edited files, interrupted imports).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from drilling_intelligence.core.ids import new_id
from drilling_intelligence.database.integrity import (
    RELATION_ENDPOINT_MODELS,
    IntegrityProblem,
    KnowledgeIntegrityError,
    check_knowledge_relations,
    create_knowledge_relation,
    describe_problems,
    find_knowledge_relation,
    validate_knowledge_relation,
)
from drilling_intelligence.database.models import Document, KnowledgeItem, KnowledgeRelation
from drilling_intelligence.documents.repository import DocumentRepository


@pytest.fixture
def knowledge_item(session) -> KnowledgeItem:
    row = KnowledgeItem(
        id=new_id("ki"),
        item_type="LESSON_LEARNED",
        title="Kick off margin on A-3 was too tight",
        content="Observed 2025-06-14.",
        domain="drilling",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def document_row(session) -> Document:
    document = DocumentRepository(session).create_document(
        workspace_id=None,
        identity_path="docs/lesson.txt",
        filename="lesson.txt",
        extension=".txt",
        mime_type="text/plain",
        size_bytes=12,
        sha256="a" * 64,
    )
    session.flush()
    return document


# --------------------------------------------------------------------------- the accepted shape
def test_the_endpoints_stay_polymorphic_and_unconstrained() -> None:
    """Guard against a future "improvement" that fans the columns out into foreign keys."""
    columns = KnowledgeRelation.__table__.columns
    for name in ("source_type", "source_id", "target_type", "target_id"):
        assert name in columns, f"{name} must stay: the edge is polymorphic by design"
    for column in columns:
        assert not column.foreign_keys, (
            f"knowledge_relation.{column.name} must not grow a foreign key; the endpoint "
            "types are open, so referential integrity is enforced in code (see this module's tests)"
        )
    assert set(RELATION_ENDPOINT_MODELS) >= {"document", "knowledge_item", "well", "source"}


def test_a_supported_edge_between_two_real_rows_is_written(session, document_row, knowledge_item) -> None:
    row = create_knowledge_relation(
        session,
        source_type="document",
        source_id=document_row.id,
        relation="DOCUMENT_CONTAINS_KNOWLEDGE",
        target_type="knowledge_item",
        target_id=knowledge_item.id,
        weight=0.8,
        provenance=[{"kind": "text", "page": 1}],
        note="from the mud report revision 1",
    )
    session.commit()
    assert row.source_id == document_row.id and row.target_id == knowledge_item.id
    assert row.weight == pytest.approx(0.8)
    assert row.provenance == [{"kind": "text", "page": 1}]
    assert check_knowledge_relations(session) == []


def test_re_asserting_the_same_edge_strengthens_it_instead_of_duplicating(session, document_row, knowledge_item) -> None:
    create_knowledge_relation(
        session,
        source_type="document",
        source_id=document_row.id,
        relation="ITEM_SUPPORTS",
        target_type="knowledge_item",
        target_id=knowledge_item.id,
        weight=0.4,
    )
    session.flush()
    again = create_knowledge_relation(
        session,
        source_type="document",
        source_id=document_row.id,
        relation="ITEM_SUPPORTS",
        target_type="knowledge_item",
        target_id=knowledge_item.id,
        weight=0.9,
        provenance=[{"kind": "text", "page": 3}],
    )
    session.flush()
    assert session.scalar(select(func.count()).select_from(KnowledgeRelation)) == 1
    assert again.weight == pytest.approx(0.9), "the stronger assertion wins, per ADR-0006"
    assert again.provenance and len(again.provenance) == 1
    assert find_knowledge_relation(
        session,
        source_type="document",
        source_id=document_row.id,
        relation="ITEM_SUPPORTS",
        target_type="knowledge_item",
        target_id=knowledge_item.id,
    ).id == again.id


# --------------------------------------------------------------------------- refused writes
def test_an_unsupported_endpoint_type_is_refused(session, document_row, knowledge_item) -> None:
    with pytest.raises(KnowledgeIntegrityError, match="not a supported endpoint"):
        validate_knowledge_relation(
            session,
            source_type="black_hole",
            source_id=knowledge_item.id,
            target_type="knowledge_item",
            target_id=knowledge_item.id,
            relation="ITEM_SUPPORTS",
        )
    with pytest.raises(KnowledgeIntegrityError, match="not a supported endpoint"):
        create_knowledge_relation(
            session,
            source_type="document",
            source_id=document_row.id,
            relation="ITEM_SUPPORTS",
            target_type="rumour",
            target_id="whatever",
        )


def test_a_dangling_endpoint_is_refused_before_persistence(session, document_row, knowledge_item) -> None:
    missing = new_id("ki")
    with pytest.raises(KnowledgeIntegrityError, match="does not exist") as caught:
        create_knowledge_relation(
            session,
            source_type="document",
            source_id=document_row.id,
            relation="DOCUMENT_CONTAINS_KNOWLEDGE",
            target_type="knowledge_item",
            target_id=missing,
        )
    assert caught.value.context["endpoint_id"] == missing
    session.flush()
    assert session.scalar(select(func.count()).select_from(KnowledgeRelation)) == 0, "a refused edge leaves nothing behind"


@pytest.mark.parametrize(
    ("source_type", "source_id", "relation", "target_type", "target_id"),
    [
        ("", "doc-1", "ITEM_SUPPORTS", "knowledge_item", "ki-1"),
        ("document", "", "ITEM_SUPPORTS", "knowledge_item", "ki-1"),
        ("document", "doc-1", "", "knowledge_item", "ki-1"),
        ("document", "doc-1", "   ", "knowledge_item", "ki-1"),
        ("document", "doc-1", "item supports", "knowledge_item", "ki-1"),
        ("document", "doc-1", "ITEM_SUPPORTS", "", "ki-1"),
        ("document", "doc-1", "ITEM_SUPPORTS", "knowledge_item", "  "),
        ("document", "doc-1", "ITEM_SUPPORTS", "knowledge_item", "doc-1"),
    ],
)
def test_malformed_edges_are_refused(session, source_type: str, source_id: str, relation: str, target_type: str, target_id: str) -> None:
    with pytest.raises(KnowledgeIntegrityError):
        validate_knowledge_relation(
            session,
            source_type=source_type,
            source_id=source_id,
            relation=relation,
            target_type=target_type,
            target_id=target_id,
        )


def test_a_weight_outside_the_unit_interval_is_refused(session, document_row, knowledge_item) -> None:
    for weight in (-0.1, 1.5):
        with pytest.raises(KnowledgeIntegrityError, match="between 0 and 1"):
            create_knowledge_relation(
                session,
                source_type="document",
                source_id=document_row.id,
                relation="ITEM_SUPPORTS",
                target_type="knowledge_item",
                target_id=knowledge_item.id,
                weight=weight,
            )


# --------------------------------------------------------------------------- reporting old damage
def _insert_edge_raw(session, *, source_type: str, source_id: str, target_type: str, target_id: str, relation: str = "ITEM_SUPPORTS") -> str:
    row_id = new_id("rel")
    session.execute(
        text(
            "insert into knowledge_relation (id, source_type, source_id, relation, target_type, target_id, weight,"
            " provenance, note, created_at, updated_at) values (:id, :st, :si, :rel, :tt, :ti, 1.0, '[]', NULL,"
            " '2026-01-01', '2026-01-01')"
        ),
        {"id": row_id, "st": source_type, "si": source_id, "rel": relation, "tt": target_type, "ti": target_id},
    )
    return row_id


def test_an_edge_broken_by_a_delete_is_reported(session, document_row, knowledge_item) -> None:
    edge_id = _insert_edge_raw(
        session,
        source_type="document",
        source_id=document_row.id,
        target_type="knowledge_item",
        target_id=knowledge_item.id,
        relation="DOCUMENT_CONTAINS_KNOWLEDGE",
    )
    session.expunge_all()
    assert check_knowledge_relations(session) == []

    session.execute(text("delete from knowledge_item where id = :id"), {"id": knowledge_item.id})
    session.expire_all()
    problems = check_knowledge_relations(session)
    assert [problem.problem for problem in problems] == ["DANGLING_REFERENCE"], [problem.to_dict() for problem in problems]
    assert problems[0].row_id == edge_id
    assert problems[0].detail["target"].startswith("knowledge_item(")
    assert isinstance(problems[0], IntegrityProblem)
    assert "knowledge_relation" in describe_problems(problems)


def test_an_edge_naming_an_unknown_endpoint_type_is_reported(session) -> None:
    edge_id = _insert_edge_raw(session, source_type="well", source_id="well-1", target_type="rumour", target_id="x")
    problems = check_knowledge_relations(session)
    assert [(problem.problem, problem.row_id) for problem in problems] == [
        # Both halves of the edge are wrong and both are reported: "unsupported type" does
        # not excuse the checker from saying the source row is missing too.
        ("DANGLING_REFERENCE", edge_id),
        ("UNSUPPORTED_ENDPOINT_TYPE", edge_id),
    ]
    assert problems[1].detail["target"] == "rumour(x)"
    assert describe_problems(problems).startswith("knowledge_relation(")


def test_self_reference_is_reported(session, knowledge_item) -> None:
    _insert_edge_raw(
        session,
        source_type="knowledge_item",
        source_id=knowledge_item.id,
        target_type="knowledge_item",
        target_id=knowledge_item.id,
    )
    assert [problem.problem for problem in check_knowledge_relations(session)] == ["SELF_REFERENCE"]


def test_a_clean_database_reports_nothing(session) -> None:
    assert check_knowledge_relations(session) == []
    assert describe_problems([]) == "no problems"
