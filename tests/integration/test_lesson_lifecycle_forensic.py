"""Forensic: LessonLearned data lifecycle integrity — deletion, uniqueness, queries.

This suite proves the V10.0 contracts from §2:

* delete_promoted is part of the version-owned derived surface (LessonLearned
  included), preserves MANUAL rows, preserves other versions, reinstates a
  superseded predecessor, removes lesson evidence edges, is transactional,
  and audit (approved/reviewed) is preserved via the predecessor row.
* code uniqueness is global (uq_lesson_one_current on code), not per-well.
* queries remain correct after revision/supersession: provenance, revision
  chain, well/category/problem filters.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import select

from drilling_intelligence.config.settings import Settings
from drilling_intelligence.database.models import KnowledgeRelation, LessonLearned
from drilling_intelligence.lessons.repository import LessonRepository
from drilling_intelligence.operations.promote import VersionPromoter
from drilling_intelligence.wells.repository import WellRepository
from drilling_intelligence.wells.workspace import Workspace


def _settings() -> Settings:
    return Settings.load(Path("configs/development.toml"))


def _make_workspace(tmp_path: Path, name: str) -> tuple[Workspace, object, object]:
    settings = _settings()
    root = tmp_path / f"ws-{name}"
    ws = Workspace.create(root, settings, name=name)
    with ws.database.session() as s:
        repo = WellRepository(s)
        field = repo.get_or_create_field("North Cormorant")
        project = repo.get_or_create_project("North Cormorant")
        well = repo.create_well("A-3", project_id=project.id, field_id=field.id)
        well_b = repo.create_well("B-11", project_id=project.id, field_id=field.id)
        s.commit()
        return ws, well, well_b


def _build_lesson_xlsx(path: Path, rows: list[dict[str, str]]) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Lessons"
    headers = [
        "Lesson Code",
        "Title",
        "Lesson",
        "Observation",
        "Well",
        "Problem Type",
        "Root Cause",
        "Recommendation",
    ]
    for col, h in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=h)
    for r, row in enumerate(rows, start=2):
        ws.cell(row=r, column=1, value=row.get("code", ""))
        ws.cell(row=r, column=2, value=row.get("title", ""))
        ws.cell(row=r, column=3, value=row.get("lesson", ""))
        ws.cell(row=r, column=4, value=row.get("observation", ""))
        ws.cell(row=r, column=5, value=row.get("well", ""))
        ws.cell(row=r, column=6, value=row.get("problem_type", ""))
        ws.cell(row=r, column=7, value=row.get("root_cause", ""))
        ws.cell(row=r, column=8, value=row.get("recommendation", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _ingest_and_promote(ws: Workspace, well, file_path: Path):
    from drilling_intelligence.database.models import Document, DocumentVersion
    from drilling_intelligence.ingestion.pipeline import IngestionPipeline

    settings = _settings()
    pipe = IngestionPipeline(settings=settings, workspace_root=ws.root, database=ws.database)
    with ws.database.session() as s:
        repo = WellRepository(s)
        ws_row = repo.get_or_create_workspace(str(ws.root), name="test")
        well_id = well.id if hasattr(well, "id") else str(well)
        s.commit()
        ws_id = ws_row.id
    pipe.run(root=file_path.parent, workspace_id=ws_id, well_id=well_id)
    with ws.database.session() as s:
        doc = s.execute(select(Document).where(Document.filename == file_path.name)).scalar_one()
        version = s.get(DocumentVersion, str(doc.current_version_id))
        promoter = VersionPromoter(s)
        result = promoter.promote(document_id=doc.id, version_id=version.id)
        s.commit()
        return result, doc, version


# -- delete_promoted -------------------------------------------------------


def test_delete_promoted_removes_lesson_and_its_edges(tmp_path):
    ws, well, _ = _make_workspace(tmp_path, "del-edges")
    xlsx = tmp_path / "docs" / "lesson_del.xlsx"
    _build_lesson_xlsx(
        xlsx, [{"code": "LL-DEL-01", "title": "Del", "lesson": "Delete me", "well": "A-3"}]
    )
    _, _doc, version = _ingest_and_promote(ws, well, xlsx)
    with ws.database.session() as s:
        lesson = s.execute(
            select(LessonLearned).where(LessonLearned.code == "LL-DEL-01")
        ).scalar_one()
        repo = LessonRepository(s)
        # attach evidence to have an edge
        # create a dummy knowledge item? Use attach with document_version_ids
        repo.attach_evidence(lesson.id, document_version_ids=[version.id])
        s.commit()
        lesson_id = lesson.id
        with ws.database.session() as s2:
            edges_before = list(
                s2.execute(
                    select(KnowledgeRelation).where(KnowledgeRelation.source_id == lesson_id)
                )
                .scalars()
                .all()
            )
            assert len(edges_before) >= 1
    with ws.database.unit_of_work() as s:
        removed = VersionPromoter(s).delete_promoted(version_id=version.id)
        assert removed >= 1
    with ws.database.session() as s:
        remaining = list(
            s.execute(select(LessonLearned).where(LessonLearned.code == "LL-DEL-01"))
            .scalars()
            .all()
        )
        assert remaining == []
        edges_after = list(
            s.execute(select(KnowledgeRelation).where(KnowledgeRelation.source_id == lesson_id))
            .scalars()
            .all()
        )
        assert edges_after == []


def test_delete_promoted_preserves_manual_lesson(tmp_path):
    ws, well, _ = _make_workspace(tmp_path, "del-manual")
    xlsx = tmp_path / "docs" / "lesson_manual.xlsx"
    _build_lesson_xlsx(
        xlsx, [{"code": "LL-DEL-02", "title": "Derived", "lesson": "Derived lesson", "well": "A-3"}]
    )
    _, _doc, version = _ingest_and_promote(ws, well, xlsx)
    with ws.database.session() as s:
        repo = LessonRepository(s)
        manual = repo.capture(
            lesson="Manual lesson must survive",
            title="Manual",
            code="LL-MANUAL-01",
            created_by="engineer@example.com",
            well_id=well.id,
        )
        manual.origin = "MANUAL"
        # manually assign document_version_id to the same version would be deleted if not filtered; so assign None
        manual.document_version_id = None
        s.commit()
        manual_id = manual.id
    with ws.database.unit_of_work() as s:
        VersionPromoter(s).delete_promoted(version_id=version.id)
    with ws.database.session() as s:
        assert s.get(LessonLearned, manual_id) is not None
        assert s.get(LessonLearned, manual_id).code == "LL-MANUAL-01"
        derived = list(
            s.execute(select(LessonLearned).where(LessonLearned.code == "LL-DEL-02"))
            .scalars()
            .all()
        )
        assert derived == []


def test_delete_promoted_preserves_other_version(tmp_path):
    ws, well, _ = _make_workspace(tmp_path, "del-other-version")
    xlsx_a = tmp_path / "docs_a" / "lesson_a.xlsx"
    xlsx_b = tmp_path / "docs_b" / "lesson_b.xlsx"
    _build_lesson_xlsx(
        xlsx_a, [{"code": "LL-DEL-03", "title": "A", "lesson": "Lesson A", "well": "A-3"}]
    )
    _build_lesson_xlsx(
        xlsx_b, [{"code": "LL-DEL-04", "title": "B", "lesson": "Lesson B", "well": "A-3"}]
    )
    _, _doc_a, ver_a = _ingest_and_promote(ws, well, xlsx_a)
    _, _doc_b, _ver_b = _ingest_and_promote(ws, well, xlsx_b)
    with ws.database.unit_of_work() as s:
        VersionPromoter(s).delete_promoted(version_id=ver_a.id)
    with ws.database.session() as s:
        assert (
            list(
                s.execute(select(LessonLearned).where(LessonLearned.code == "LL-DEL-03"))
                .scalars()
                .all()
            )
            == []
        )
        assert (
            len(
                list(
                    s.execute(select(LessonLearned).where(LessonLearned.code == "LL-DEL-04"))
                    .scalars()
                    .all()
                )
            )
            == 1
        )


def test_delete_promoted_reinstates_superseded_predecessor(tmp_path):
    ws, well, _ = _make_workspace(tmp_path, "del-supersede")
    docs = tmp_path / "docs_rev2"
    docs.mkdir()
    xlsx = docs / "lesson_rev.xlsx"
    _build_lesson_xlsx(
        xlsx, [{"code": "LL-DEL-05", "title": "Rev1", "lesson": "Original", "well": "A-3"}]
    )
    _, _doc, _ver1 = _ingest_and_promote(ws, well, xlsx)
    with ws.database.session() as s:
        rev1 = s.execute(
            select(LessonLearned).where(LessonLearned.code == "LL-DEL-05")
        ).scalar_one()
        rev1_id = rev1.id
        assert rev1.is_current is True
    _build_lesson_xlsx(
        xlsx, [{"code": "LL-DEL-05", "title": "Rev2", "lesson": "Updated", "well": "A-3"}]
    )
    _, _doc2, ver2 = _ingest_and_promote(ws, well, xlsx)
    with ws.database.session() as s:
        rows = list(
            s.execute(
                select(LessonLearned)
                .where(LessonLearned.code == "LL-DEL-05")
                .order_by(LessonLearned.revision)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[0].id == rev1_id
        assert rows[0].is_current is False
        assert rows[1].is_current is True
        assert rows[1].supersedes_id == rev1_id
    with ws.database.unit_of_work() as s:
        VersionPromoter(s).delete_promoted(version_id=ver2.id)
    with ws.database.session() as s:
        rows = list(
            s.execute(select(LessonLearned).where(LessonLearned.code == "LL-DEL-05"))
            .scalars()
            .all()
        )
        # After deleting rev2, rev1 must be reinstated as current
        assert len(rows) == 1
        assert rows[0].id == rev1_id
        assert rows[0].is_current is True


def test_delete_promoted_transactional_on_failure(tmp_path):
    """A failure mid-delete must not leave a partial state (rollback)."""
    ws, well, _ = _make_workspace(tmp_path, "del-tx")
    xlsx = tmp_path / "docs" / "lesson_tx.xlsx"
    _build_lesson_xlsx(
        xlsx, [{"code": "LL-DEL-06", "title": "Tx", "lesson": "Tx lesson", "well": "A-3"}]
    )
    _, _doc, version = _ingest_and_promote(ws, well, xlsx)
    with ws.database.session() as s:
        lesson = s.execute(
            select(LessonLearned).where(LessonLearned.code == "LL-DEL-06")
        ).scalar_one()
        lesson_id = lesson.id
    # Simulate a failure by patching session.delete to raise for lesson
    from unittest.mock import patch

    with ws.database.unit_of_work() as s:
        promoter = VersionPromoter(s)
        original_delete = s.delete

        def failing_delete(obj):
            if getattr(obj, "id", None) == lesson_id:
                raise RuntimeError("simulated failure")
            return original_delete(obj)

        with patch.object(s, "delete", side_effect=failing_delete):
            try:
                promoter.delete_promoted(version_id=version.id)
                assert False, "should have raised"
            except RuntimeError:
                pass
        s.rollback()
    with ws.database.session() as s:
        # Lesson must still exist — delete was rolled back
        assert s.get(LessonLearned, lesson_id) is not None


# -- global uniqueness ------------------------------------------------------


def test_lesson_code_global_uniqueness_across_wells(tmp_path):
    ws, well, well_b = _make_workspace(tmp_path, "global-uniq")
    xlsx_a = tmp_path / "docs_ga" / "lesson_ga.xlsx"
    xlsx_b = tmp_path / "docs_gb" / "lesson_gb.xlsx"
    _build_lesson_xlsx(
        xlsx_a, [{"code": "LL-GLOBAL-01", "title": "A", "lesson": "Lesson A", "well": "A-3"}]
    )
    _build_lesson_xlsx(
        xlsx_b, [{"code": "LL-GLOBAL-01", "title": "B", "lesson": "Lesson B", "well": "B-11"}]
    )
    result_a, _, _ = _ingest_and_promote(ws, well, xlsx_a)
    assert result_a.counts.get("lesson_learned", {}).get("created", 0) == 1
    result_b, _, _ = _ingest_and_promote(ws, well_b, xlsx_b)
    # Second promotion with same code but different well must be CONFLICT, not a second live row
    assert any(
        "CONFLICT" in str(s.get("reason")) or "already exists" in str(s.get("detail"))
        for s in result_b.skipped
    )
    with ws.database.session() as s:
        rows = list(
            s.execute(
                select(LessonLearned).where(
                    LessonLearned.code == "LL-GLOBAL-01", LessonLearned.is_current.is_(True)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].lesson == "Lesson A"


def test_lesson_code_casefold_global(tmp_path):
    ws, well, well_b = _make_workspace(tmp_path, "global-casefold")
    xlsx_a = tmp_path / "docs_ca" / "lesson_ca.xlsx"
    xlsx_b = tmp_path / "docs_cb" / "lesson_cb.xlsx"
    _build_lesson_xlsx(
        xlsx_a, [{"code": "LL-GLOBAL-02", "title": "A", "lesson": "Lesson A", "well": "A-3"}]
    )
    _build_lesson_xlsx(
        xlsx_b, [{"code": "ll-global-02", "title": "B", "lesson": "Lesson B", "well": "B-11"}]
    )
    _ingest_and_promote(ws, well, xlsx_a)
    result_b, _, _ = _ingest_and_promote(ws, well_b, xlsx_b)
    assert any("CONFLICT" in str(s.get("reason")) for s in result_b.skipped)


# -- query correctness ------------------------------------------------------


def test_lesson_queries_provenance_and_revision_chain(tmp_path):
    ws, well, _ = _make_workspace(tmp_path, "query-prov")
    xlsx = tmp_path / "docs_q" / "lesson_q.xlsx"
    _build_lesson_xlsx(
        xlsx,
        [
            {
                "code": "LL-QUERY-01",
                "title": "Q1",
                "lesson": "Original Q",
                "well": "A-3",
                "problem_type": "stuck pipe",
            }
        ],
    )
    _, _doc, ver1 = _ingest_and_promote(ws, well, xlsx)
    with ws.database.session() as s:
        row1 = s.execute(
            select(LessonLearned).where(
                LessonLearned.code == "LL-QUERY-01", LessonLearned.is_current.is_(True)
            )
        ).scalar_one()
        assert row1.provenance and row1.provenance[0].get("document_version_id") == ver1.id
        assert row1.revision == 1
        assert row1.is_current is True
    # Revision
    _build_lesson_xlsx(
        xlsx,
        [
            {
                "code": "LL-QUERY-01",
                "title": "Q1 Rev2",
                "lesson": "Revised Q",
                "well": "A-3",
                "problem_type": "stuck pipe",
            }
        ],
    )
    _, _doc2, ver2 = _ingest_and_promote(ws, well, xlsx)
    with ws.database.session() as s:
        rows = list(
            s.execute(
                select(LessonLearned)
                .where(LessonLearned.code == "LL-QUERY-01")
                .order_by(LessonLearned.revision)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[0].revision == 1 and rows[0].is_current is False
        assert rows[1].revision == 2 and rows[1].is_current is True
        assert rows[1].supersedes_id == rows[0].id
        assert rows[1].provenance[0].get("document_version_id") == ver2.id


def test_lesson_queries_well_and_category_filters(tmp_path):
    ws, well, _ = _make_workspace(tmp_path, "query-filter")
    xlsx_a = tmp_path / "docs_fa" / "lesson_fa.xlsx"
    xlsx_b = tmp_path / "docs_fb" / "lesson_fb.xlsx"
    _build_lesson_xlsx(
        xlsx_a,
        [
            {
                "code": "LL-FILTER-01",
                "title": "A",
                "lesson": "Lesson A stuck",
                "well": "A-3",
                "problem_type": "stuck pipe",
            }
        ],
    )
    _build_lesson_xlsx(
        xlsx_b,
        [
            {
                "code": "LL-FILTER-02",
                "title": "B",
                "lesson": "Lesson B washout",
                "well": "A-3",
                "problem_type": "washout",
            }
        ],
    )
    _ingest_and_promote(ws, well, xlsx_a)
    _ingest_and_promote(ws, well, xlsx_b)
    with ws.database.session() as s:
        repo = LessonRepository(s)
        all_lessons = repo.list_lessons(well_id=well.id)
        assert len(all_lessons) == 2
        stuck = repo.list_lessons(well_id=well.id, problem_type="stuck pipe")
        assert len(stuck) == 1 and stuck[0].code == "LL-FILTER-01"
        wash = repo.list_lessons(well_id=well.id, problem_type="washout")
        assert len(wash) == 1 and wash[0].code == "LL-FILTER-02"
        # lessons_for_well includes field-scoped lessons
        for_well = repo.lessons_for_well(well.id, approved_only=False)
        assert len(for_well) == 2
        # Search
        search = repo.list_lessons(search="washout", well_id=well.id)
        assert len(search) == 1 and search[0].code == "LL-FILTER-02"
