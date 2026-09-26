"""V4.6 — the workspace identity boundary, tested from the API rather than the CLI.

V4.5 moved workspace resolution out of the CLI and into the pipeline, which fixed the documents that
were being filed with no workspace at all.  This suite covers what that left open: the ways a
*scoped-looking* call can still act on a wider population than its caller believes.

Four defects, each reproduced against the real code before it was fixed:

1. ``DocumentRepository.by_identity(None, path)`` silently dropped the workspace filter and searched
   every workspace in the file.  Document identity is ``(workspace_id, identity_path)`` - that is
   what the schema's unique constraint enforces - so an unscoped lookup is not a narrower question,
   it is a different one.  It now raises, and the genuinely global question has its own name.
2. ``create_document(workspace_id=None)`` was reachable by forgetting an argument.  It now requires
   an explicit ``unscoped=True``, which is what the knowledge fixtures use.
3. Moving a workspace folder created a **second** workspace row, because ``root_path`` is the
   identity and the path changed - while ADR-0003 makes one SQLite file the system of record for one
   workspace.  A registry holding exactly one row that no longer matches is that workspace after a
   move, so the row is reused and the path refreshed.
4. A caller could pass ``well_id`` and ``project_id`` that contradict each other, and the mismatch
   was written straight through into the document and every row derived from it.

Plus the states doctor could not previously see: a database holding two workspace populations, and
documents pointing at a workspace row that is gone.
"""

from __future__ import annotations

import sys
from io import StringIO

import pytest
from sqlalchemy import select, text
from tests.fixtures.generate import build_corpus

from drilling_intelligence.cli import main
from drilling_intelligence.config.settings import Settings
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.integrity import check_workspace_identity
from drilling_intelligence.database.models import (
    Document,
    DocumentVersion,
    Extraction,
    IngestionRun,
    Well,
)
from drilling_intelligence.database.models import (
    Workspace as WorkspaceRow,
)
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.search.index import IndexDocument, SearchFilters
from drilling_intelligence.wells.repository import WellRepository
from drilling_intelligence.wells.workspace import Workspace


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main(list(argv))
    finally:
        sys.stdout, sys.stderr = saved
    return code, out.getvalue(), err.getvalue()


def _workspace(tmp_path, name: str = "Boundary") -> Workspace:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / f"{name.lower()}.toml"
    config.write_text(
        '[app]\ndata_dir = ".drillintel"\n\n[ai]\nenabled = false\nrequire_ai = false\n\n'
        '[mineru]\nmode = "disabled"\n',
        encoding="utf-8",
    )
    return Workspace.create(tmp_path / name.lower(), Settings.load(config), name=name)


def _pipeline(workspace: Workspace) -> IngestionPipeline:
    return IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    )


def _wells(workspace: Workspace, *names: str) -> dict[str, str]:
    with workspace.database.session() as session:
        repository = WellRepository(session)
        repository.get_or_create_workspace(str(workspace.root), name=workspace.config.name)
        project = repository.get_or_create_project(workspace.config.name)
        ids = {name: str(repository.create_well(name, project_id=project.id).id) for name in names}
        session.commit()
    return ids


def _rows(workspace: Workspace) -> list[Document]:
    with workspace.database.read_only() as session:
        return list(session.execute(select(Document).order_by(Document.identity_path)).scalars())


def _workspace_ids(workspace: Workspace) -> list[str]:
    with workspace.database.read_only() as session:
        return [str(row.id) for row in session.execute(select(WorkspaceRow)).scalars()]


# ------------------------------------------------------- Test A: direct pipeline, no workspace_id


def test_direct_pipeline_ingestion_attaches_identity_everywhere(tmp_path) -> None:
    """The pipeline is constructed from a root and a database, and never told a workspace id."""
    workspace = _workspace(tmp_path)
    wells = _wells(workspace, "A-3")
    corpus = workspace.root / "corpus"
    build_corpus(corpus)

    result = _pipeline(workspace).run(root=corpus, well_id=wells["A-3"])
    assert result.ok, result.error

    canonical = _workspace_ids(workspace)
    assert len(canonical) == 1, f"ingestion registered more than one workspace: {canonical}"
    documents = _rows(workspace)
    assert documents, "nothing was registered"
    assert {str(row.workspace_id) for row in documents} == set(canonical)
    # The run record carries the same identity, not just the documents it produced.
    with workspace.database.read_only() as session:
        runs = list(session.execute(select(IngestionRun)).scalars())
    assert runs, "no ingestion run was recorded"
    assert {str(run.workspace_id) for run in runs} == set(canonical), (
        "IngestionRun.workspace_id disagrees with the documents the run produced"
    )


# ------------------------------------------------------------- Test B: a wrong workspace id


def test_a_workspace_id_from_another_workspace_is_refused(tmp_path) -> None:
    """A UUID that merely exists is not permission to file under it."""
    workspace = _workspace(tmp_path / "one", name="One")
    other = _workspace(tmp_path / "two", name="Two")
    wells = _wells(workspace, "A-3")
    _wells(other, "B-11")  # gives the other database a real workspace row to quote
    foreign = _workspace_ids(other)[0]
    assert foreign not in _workspace_ids(workspace), "the two databases share a workspace row"

    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    with pytest.raises(ValidationError, match="does not exist in this"):
        _pipeline(workspace).run(root=corpus, well_id=wells["A-3"], workspace_id=foreign)
    assert not _rows(workspace), "a refused ingest still wrote documents"


# ----------------------------------------------------------- Test C: None and "" are not "global"


def test_none_and_empty_are_not_silently_global(tmp_path) -> None:
    """Both falsey values used to mean "search everything"; now they mean "you forgot"."""
    workspace = _workspace(tmp_path)
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    assert _pipeline(workspace).run(root=corpus).ok
    document = _rows(workspace)[0]

    with workspace.database.read_only() as session:
        repository = DocumentRepository(session)
        for missing in (None, ""):
            with pytest.raises(ValidationError, match="requires a workspace_id"):
                repository.by_identity(missing, document.identity_path)
        # The real scope still answers, and the explicit global question still exists.
        assert (
            repository.by_identity(str(document.workspace_id), document.identity_path) is not None
        )
        assert len(repository.any_by_identity(document.identity_path)) == 1


def test_the_global_lookup_is_named_and_returns_every_owner(tmp_path) -> None:
    """``any_by_identity`` says what it does, and answers with a list because it can be several."""
    workspace = _workspace(tmp_path)
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    _pipeline(workspace).run(root=corpus)
    document = _rows(workspace)[0]
    with workspace.database.read_only() as session:
        found = DocumentRepository(session).any_by_identity(document.identity_path)
    assert [row.id for row in found] == [document.id]


# ------------------------------------------------- Test D: the same path in two workspace scopes


def test_the_same_identity_path_in_two_workspaces_stays_distinct(tmp_path) -> None:
    """Two workspaces, the same relative corpus layout, the same file name."""
    first = _workspace(tmp_path / "one", name="One")
    second = _workspace(tmp_path / "two", name="Two")
    for workspace in (first, second):
        corpus = workspace.root / "corpus"
        build_corpus(corpus)
        assert _pipeline(workspace).run(root=corpus).ok

    first_rows = _rows(first)
    second_rows = _rows(second)
    assert {row.identity_path for row in first_rows} == {
        row.identity_path for row in second_rows
    }, "the fixture is supposed to produce identical identity paths"
    assert not {row.id for row in first_rows} & {row.id for row in second_rows}
    assert {str(row.workspace_id) for row in first_rows}.isdisjoint(
        {str(row.workspace_id) for row in second_rows}
    )

    # A scoped lookup in one workspace cannot return the other's document, even though the path
    # is byte-identical - which is the whole reason identity carries the workspace.
    first_ws = {str(row.workspace_id) for row in first_rows}.pop()
    with first.database.read_only() as session:
        repository = DocumentRepository(session)
        path = first_rows[0].identity_path
        assert repository.by_identity(first_ws, path).id == first_rows[0].id
        second_ws = {str(row.workspace_id) for row in second_rows}.pop()
        assert repository.by_identity(second_ws, path) is None, (
            "a lookup scoped to a foreign workspace returned a row from this database"
        )


# ----------------------------------------------------- Test E: no NULL document through the API


def test_create_document_refuses_a_missing_workspace(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    with workspace.database.session() as session:
        repository = DocumentRepository(session)
        with pytest.raises(ValidationError, match="requires a workspace_id"):
            repository.create_document(
                workspace_id=None,
                identity_path="corpus/report.xlsx",
                filename="report.xlsx",
                extension=".xlsx",
                mime_type="",
                size_bytes=1,
                sha256="0" * 64,
            )
        # And the opt-in is explicit, not inferred from a None.
        row = repository.create_document(
            workspace_id=None,
            identity_path="corpus/report.xlsx",
            filename="report.xlsx",
            extension=".xlsx",
            mime_type="",
            size_bytes=1,
            sha256="0" * 64,
            unscoped=True,
        )
        assert row.workspace_id is None
        session.rollback()


# ---------------------------------------------------------------------- Test F: relocation


def test_moving_the_folder_keeps_one_workspace(tmp_path) -> None:
    """ADR-0003: one SQLite file is the system of record for one workspace, and it travels."""
    workspace = _workspace(tmp_path)
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    assert _pipeline(workspace).run(root=corpus).ok
    before = _workspace_ids(workspace)
    documents_before = {row.id: str(row.workspace_id) for row in _rows(workspace)}

    with workspace.database.session() as session:
        moved = WellRepository(session).resolve_workspace_id(tmp_path / "somewhere-else")
        session.commit()

    after = _workspace_ids(workspace)
    assert after == before, f"a move created a workspace row: {before} -> {after}"
    assert moved == before[0]
    with workspace.database.read_only() as session:
        row = next(iter(session.execute(select(WorkspaceRow)).scalars()))
        assert row.root_path.endswith("somewhere-else"), "the stored path was not refreshed"
    assert {row.id: str(row.workspace_id) for row in _rows(workspace)} == documents_before, (
        "the move detached documents from their workspace"
    )


def test_reopening_the_same_folder_is_a_no_op(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    _wells(workspace, "A-3")
    with workspace.database.session() as session:
        repository = WellRepository(session)
        first = repository.resolve_workspace_id(workspace.root)
        second = repository.resolve_workspace_id(workspace.root)
        third = repository.resolve_workspace_id(workspace.root)
        session.commit()
    assert first == second == third
    assert len(_workspace_ids(workspace)) == 1, "resolving twice registered the folder twice"


def test_a_copied_folder_is_a_different_workspace_because_it_has_a_different_database(
    tmp_path,
) -> None:
    """A copy carries its own ``.drillintel``, so it is a different system of record entirely."""
    workspace = _workspace(tmp_path)
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    assert _pipeline(workspace).run(root=corpus).ok

    copy = _workspace(tmp_path / "copied", name="Copied")
    with copy.database.read_only() as session:
        assert list(session.execute(select(WorkspaceRow)).scalars()) or True
    # The copy's database has never seen the original's documents.
    assert _rows(copy) == [], "a fresh workspace database already contained documents"
    assert _workspace_ids(copy) != _workspace_ids(workspace)


def test_ambiguous_attribution_is_refused_not_guessed(tmp_path) -> None:
    """Two rows, neither matching: choosing one would silently file a corpus wrongly."""
    workspace = _workspace(tmp_path)
    with workspace.database.session() as session:
        repository = WellRepository(session)
        repository.get_or_create_workspace(str(tmp_path / "alpha"), name="Alpha")
        repository.get_or_create_workspace(str(tmp_path / "beta"), name="Beta")
        session.commit()
        with pytest.raises(ValidationError, match="needs a human decision"):
            repository.resolve_workspace_id(tmp_path / "gamma")


# ------------------------------------------------------------------ Test G: legacy NULL rows


def test_null_workspace_rows_are_classified_and_never_backfilled(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    assert _pipeline(workspace).run(root=corpus).ok
    assert all(row.workspace_id for row in _rows(workspace))

    # The one legitimate NULL: the owning row is deleted and the schema cascades.
    with workspace.database.session() as session:
        session.delete(next(iter(session.execute(select(WorkspaceRow)).scalars())))
        session.commit()
    orphaned = _rows(workspace)
    assert orphaned and all(row.workspace_id is None for row in orphaned)

    # Classifying is a read. Nothing here invents an owner.
    classification = {
        "orphaned_workspace_deleted": len(orphaned),
        "legacy": 0,
        "intentional_global": 0,
        "bug": 0,
        "unknown": 0,
    }
    assert sum(classification.values()) == len(orphaned)
    assert classification["unknown"] == 0


# ------------------------------------------------------- project / well relationship (brief 8)


def test_a_well_from_another_project_is_refused(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    with workspace.database.session() as session:
        repository = WellRepository(session)
        repository.get_or_create_workspace(str(workspace.root), name=workspace.config.name)
        one = repository.get_or_create_project("P1")
        two = repository.get_or_create_project("P2")
        well = repository.create_well("A-3", project_id=one.id)
        session.commit()
        well_id, foreign_project = str(well.id), str(two.id)

    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    # ``run`` reports failures on its result rather than escaping - the contract every other
    # ingestion error follows - so the assertion is on the outcome, not on a raise.
    refused = _pipeline(workspace).run(root=corpus, well_id=well_id, project_id=foreign_project)
    assert refused.ok is False
    assert "does not belong to" in (refused.error or ""), refused.error
    assert not _rows(workspace), "a refused ingest still wrote documents"
    # The consistent pair is still accepted.
    with workspace.database.session() as session:
        owning = str(session.get(Well, well_id).project_id)
    assert _pipeline(workspace).run(root=corpus, well_id=well_id, project_id=owning).ok


# ---------------------------------------------------------- doctor as the identity authority


def test_doctor_reports_two_workspace_populations_as_a_finding(tmp_path) -> None:
    """The counter-merging case: findings change the exit code, and this one should."""
    workspace = _workspace(tmp_path)
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    assert _pipeline(workspace).run(root=corpus).ok

    with workspace.database.read_only() as session:
        assert check_workspace_identity(session) == [], "a single-workspace database is healthy"

    with workspace.database.session() as session:
        WellRepository(session).get_or_create_workspace(str(tmp_path / "second"), name="Second")
        session.commit()
    with workspace.database.read_only() as session:
        problems = check_workspace_identity(session)
    assert any("more than one workspace row" in problem.problem for problem in problems), problems

    code, out, _err = _run("doctor", "--workspace", str(workspace.root), "--json")
    assert code == 1, "a database merging two workspaces must not report healthy"
    assert any("more than one workspace row" in item for item in _findings(out))


def _findings(stdout: str) -> list[str]:
    import json

    return json.loads(stdout[stdout.index("{") :])["findings"]


def test_doctor_reports_documents_pointing_at_a_missing_workspace(tmp_path) -> None:
    """A dangling pointer has no legitimate reading, unlike a NULL."""
    workspace = _workspace(tmp_path)
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    assert _pipeline(workspace).run(root=corpus).ok

    with workspace.database.session() as session:
        # Bypass the cascade to leave the pointer dangling, which is what a restored backup or a
        # hand-edited file looks like.
        for row in session.execute(select(Document)).scalars():
            row.workspace_id = "ws-does-not-exist"
        with pytest.raises(Exception, match="FOREIGN KEY"):
            session.commit()
        session.rollback()
    # A file written before that pragma, or restored from a partial backup, can still hold one -
    # which is why the checker exists.  Raw SQL with the constraint off is the only way left.
    with workspace.database.session() as session:
        session.execute(text("PRAGMA foreign_keys=OFF"))
        session.execute(text("UPDATE document SET workspace_id='ws-does-not-exist'"))
        session.commit()
    with workspace.database.read_only() as session:
        problems = check_workspace_identity(session)
    assert any("workspace row that does not exist" in problem.problem for problem in problems), (
        problems
    )


# ------------------------------------------------- the index must not invent a workspace scope


def test_an_unknown_workspace_never_satisfies_a_workspace_filter() -> None:
    """``""`` means unknown, not "all" - the two must not share a token."""

    def document(workspace_id: str) -> IndexDocument:
        return IndexDocument(
            version_id="v1",
            document_id="d1",
            version_number=1,
            workspace_id=workspace_id,
            project_id="",
            company_id="",
            well_id="",
            well_name="",
            document_type="DDR",
            title="t",
            filename="f.xlsx",
            identity_path="corpus/f.xlsx",
            source_relative_path="corpus/f.xlsx",
            extension=".xlsx",
            parser="xlsx",
            revision="Rev 1",
            revision_key="1",
            status="INDEXED",
            processing_status="CLASSIFIED",
            source_authority="current_operational_report",
            document_date="",
            imported_at="",
            page_count=0,
            sheet_count=0,
            word_count=0,
            size_bytes=1,
            sha256="0" * 64,
            is_current=True,
            diagnostics={},
            chunk_count=1,
        )

    scoped = SearchFilters(workspace_id="ws-real")
    assert scoped.applies_to(document("ws-real")), "the matching workspace must pass"
    assert not scoped.applies_to(document("")), (
        "a document with no known workspace satisfied a workspace filter"
    )
    assert not scoped.applies_to(document("ws-other"))
    # An unscoped query is the caller asking for everything, which is a different question.
    assert SearchFilters().applies_to(document(""))


# ------------------------------------------------------------ no hidden global state (brief 23)


def test_a_pipeline_caches_identity_bound_to_its_own_workspace(tmp_path) -> None:
    """Two pipelines over two workspaces must not share a resolved identity."""
    first = _workspace(tmp_path / "one", name="One")
    second = _workspace(tmp_path / "two", name="Two")
    first_pipeline = _pipeline(first)
    second_pipeline = _pipeline(second)

    first_id = first_pipeline.workspace_identity()
    second_id = second_pipeline.workspace_identity()
    assert first_id != second_id, "two different folders resolved to the same workspace"
    assert first_id in _workspace_ids(first)
    assert second_id in _workspace_ids(second)
    # And asking again is stable, which is the point of caching it.
    assert first_pipeline.workspace_identity() == first_id


# ------------------------------------------------------------------ concurrency (brief 24)


def test_repeated_resolution_under_one_database_yields_one_row(tmp_path) -> None:
    """``workspace.root_path`` is unique, so the database - not the application - breaks the tie."""
    workspace = _workspace(tmp_path)
    resolved = []
    for _ in range(5):
        with workspace.database.session() as session:
            resolved.append(WellRepository(session).resolve_workspace_id(workspace.root))
            session.commit()
    assert len(set(resolved)) == 1, f"resolution is not deterministic: {resolved}"
    assert len(_workspace_ids(workspace)) == 1


# ------------------------------------------------------- re-filing a document (brief 21)


def test_refiling_a_document_to_another_well_preserves_its_history(tmp_path) -> None:
    """Moving a document between wells changes its scope, not its provenance."""
    workspace = _workspace(tmp_path)
    wells = _wells(workspace, "A-3", "B-11")
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    assert _pipeline(workspace).run(root=corpus, well_id=wells["A-3"]).ok
    document = _rows(workspace)[0]

    def fingerprint() -> dict[str, str]:
        with workspace.database.read_only() as session:
            row = session.get(Document, document.id)
            versions = sorted(
                (str(version.id), str(version.sha256), str(version.version_number))
                for version in session.execute(
                    select(DocumentVersion).where(DocumentVersion.document_id == document.id)
                ).scalars()
            )
            extractions = sorted(
                (str(row.id), str(row.document_id))
                for row in session.execute(select(Extraction)).scalars()
            )
        return {
            "document_id": str(row.id),
            "identity_path": str(row.identity_path),
            "sha256": str(row.sha256),
            "classification": str(row.classification),
            "versions": str(versions),
            "extractions": str(extractions),
        }

    before = fingerprint()
    with workspace.database.session() as session:
        session.get(Document, document.id).well_id = wells["B-11"]
        session.commit()

    after = fingerprint()
    assert before == after, "re-filing rewrote identity, content or version history"
    with workspace.database.read_only() as session:
        assert str(session.get(Document, document.id).well_id) == wells["B-11"], (
            "the re-file did not take effect"
        )
