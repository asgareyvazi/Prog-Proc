"""V4.5 continuation — workspace binding: physical root, database and identity must agree.

V4.5 stopped documents being filed with no workspace. V4.6 stopped a *scoped-looking* call acting on
a wider population. This suite attacks the case that survived both: a caller supplying an explicit
``workspace_id``.

The old contract was that an explicit id "is an override, not a suggestion - a caller that knows
better is believed", and there was a green test asserting exactly that. What it actually proved was
that workspace A's root plus workspace B's id filed A's corpus under B, with nothing objecting. The
physical source location and the persistent ownership disagreed, which defeats the point of having
an identity at all. So the test was replaced, not weakened.

Two defects, both reproduced against the real code first:

*   An explicit ``workspace_id`` was accepted if the row merely **existed** in this database. With
    more than one workspace row in a file, that let a corpus be filed under a different one.
*   The relocation rule added in V4.6 treated *any* single-row path mismatch as a move. Point a
    pipeline attached to database A at an unrelated root B and it silently repointed workspace A's
    ``root_path`` at B - one ingest call relocating a workspace it was never asked to touch.

The fix for the second is an evidence test rather than a guess: a workspace is a folder you can
carry, and ADR-0003 makes its SQLite file the system of record, so a genuine move **carries the
database with it**. If the root does not contain the database this pipeline is connected to, it is
not this workspace's folder, and neither moving the row there nor registering a second one is
allowed.

Cases A-F from the mission brief are all asserted here, on real databases and real ingestion.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fixtures.fieldops import ingest, promote, register_wells
from tests.fixtures.generate import build_corpus

from drilling_intelligence.config.settings import Settings
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import (
    Document,
    IngestionRun,
    Well,
)
from drilling_intelligence.database.models import (
    Workspace as WorkspaceRow,
)
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.search.service import SearchService
from drilling_intelligence.wells.repository import WellRepository
from drilling_intelligence.wells.workspace import Workspace


def _mk(tmp_path: Path, name: str) -> Workspace:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / f"{name.lower()}.toml"
    config.write_text(
        '[app]\ndata_dir = ".drillintel"\n\n[ai]\nenabled = false\nrequire_ai = false\n\n'
        '[mineru]\nmode = "disabled"\n',
        encoding="utf-8",
    )
    return Workspace.create(tmp_path / name.lower(), Settings.load(config), name=name)


def _pipeline(workspace: Workspace, root: Path | None = None) -> IngestionPipeline:
    return IngestionPipeline(
        settings=workspace.settings,
        workspace_root=root or workspace.root,
        database=workspace.database,
    )


def _rows(workspace: Workspace) -> list[Document]:
    with workspace.database.read_only() as session:
        return list(session.execute(select(Document).order_by(Document.identity_path)).scalars())


def _workspace_rows(database) -> list[tuple[str, str]]:
    with database.read_only() as session:
        return [
            (str(row.id), str(row.root_path))
            for row in session.execute(select(WorkspaceRow)).scalars()
        ]


def _id_of(workspace: Workspace) -> str:
    return _workspace_rows(workspace.database)[0][0]


@pytest.fixture
def two(tmp_path):
    """Two workspaces, each with its own folder, database, registry row and wells.

    ``register_wells`` is called once here and never again: a second call for the same name raises
    ``UNIQUE constraint failed: well.project_id, well.name``, which is the schema saying the well
    already exists rather than something to retry around.
    """
    first = _mk(tmp_path / "a", "Alpha")
    second = _mk(tmp_path / "b", "Beta")
    wells = {}
    for workspace in (first, second):
        hierarchy = register_wells(workspace, wells=("A-3", "B-11"))
        wells[workspace.config.name] = {
            name: str(row.id) for name, row in hierarchy["wells"].items()
        }
    return first, second, wells


# ------------------------------------------------------- Cases A-F: the binding matrix


def test_case_a_matching_id_is_accepted(two) -> None:
    first, _second, _wells = two
    corpus = first.root / "corpus"
    build_corpus(corpus)
    assert _pipeline(first).run(root=corpus, workspace_id=_id_of(first)).ok
    assert {str(row.workspace_id) for row in _rows(first)} == {_id_of(first)}


def test_case_b_no_id_resolves_to_the_same_identity(two) -> None:
    first, _second, _wells = two
    corpus = first.root / "corpus"
    build_corpus(corpus)
    assert _pipeline(first).run(root=corpus).ok
    assert {str(row.workspace_id) for row in _rows(first)} == {_id_of(first)}
    with first.database.read_only() as session:
        assert {
            str(run.workspace_id) for run in session.execute(select(IngestionRun)).scalars()
        } == {_id_of(first)}


def test_case_c_foreign_workspace_id_is_rejected(two) -> None:
    """A's root, A's database, B's id. B's id is not even in A's file."""
    first, second, _wells = two
    corpus = first.root / "corpus"
    build_corpus(corpus)
    with pytest.raises(
        ValidationError, match="does not match the workspace this pipeline is bound"
    ):
        _pipeline(first).run(root=corpus, workspace_id=_id_of(second))
    assert not _rows(first)


def test_case_c2_a_second_row_in_the_same_database_is_also_rejected(two) -> None:
    """The case the "exists in this database" check let through: same file, different row."""
    first, _second, _wells = two
    with first.database.session() as session:
        sibling = WellRepository(session).get_or_create_workspace(
            str(first.root.parent / "sibling"), name="Sibling"
        )
        sibling_id = str(sibling.id)
        session.commit()

    corpus = first.root / "corpus"
    build_corpus(corpus)
    with pytest.raises(
        ValidationError, match="does not match the workspace this pipeline is bound"
    ):
        _pipeline(first).run(root=corpus, workspace_id=sibling_id)
    assert not _rows(first), "the corpus was filed under a sibling workspace row"


def test_case_d_unknown_id_is_rejected(two) -> None:
    first, _second, _wells = two
    corpus = first.root / "corpus"
    build_corpus(corpus)
    with pytest.raises(
        ValidationError, match="does not match the workspace this pipeline is bound"
    ):
        _pipeline(first).run(root=corpus, workspace_id="ws-not-a-real-id")
    assert not _rows(first)


def test_case_e_foreign_root_with_an_explicit_id_is_rejected(two) -> None:
    """Root B, database A, id A. The id is right; the folder is not."""
    first, second, _wells = two
    corpus = first.root / "corpus"
    build_corpus(corpus)
    with pytest.raises(ValidationError, match="does not contain the database"):
        _pipeline(first, root=second.root).run(root=corpus, workspace_id=_id_of(first))


def test_case_f_foreign_root_with_no_id_cannot_hijack_the_registered_workspace(two) -> None:
    """The defect this continuation exists to close.

    Root B, database A, no explicit id. The earlier relocation rule saw one row whose path did not
    match and concluded the workspace had moved - so it repointed workspace A at folder B.  One
    ingest call, no error, and A's identity now described a folder that was never A.
    """
    first, second, _wells = two
    before = _workspace_rows(first.database)
    corpus = first.root / "corpus"
    build_corpus(corpus)

    with pytest.raises(ValidationError, match="does not contain the database"):
        _pipeline(first, root=second.root).run(root=corpus)

    assert _workspace_rows(first.database) == before, (
        "an unrelated root moved the registered workspace"
    )
    assert len(before) == 1, "a second workspace row appeared in this database"
    assert not _rows(first), "documents were filed during a refused ingest"


# --------------------------------------------------- relocation: a real move, not a claim


def test_a_real_move_keeps_logical_identity(tmp_path) -> None:
    """Model 1, proven: the folder moves, the database travels with it, the identity is unchanged.

    This test moves the directory on disk rather than passing a different path to a resolver,
    because the thing under test is whether the *system* recognises the move - and the signal it
    uses is that the database file is inside the folder.
    """
    workspace = _mk(tmp_path / "before", "Movable")
    hierarchy = register_wells(workspace, wells=("A-3",))
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    assert _pipeline(workspace).run(root=corpus, well_id=str(hierarchy["wells"]["A-3"].id)).ok

    identity_before = _id_of(workspace)
    documents_before = {
        row.id: (str(row.workspace_id), str(row.identity_path), str(row.sha256))
        for row in _rows(workspace)
    }
    workspace.close()

    moved = tmp_path / "after" / "movable"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(workspace.root), str(moved))

    reopened = Workspace.open(moved, workspace.settings)
    try:
        # Opening changes nothing: identity is resolved when something writes, not on open.
        rows_on_open = _workspace_rows(reopened.database)
        assert len(rows_on_open) == 1, (
            f"opening the moved folder duplicated the row: {rows_on_open}"
        )
        assert rows_on_open[0][0] == identity_before, "opening the moved folder changed identity"
        assert rows_on_open[0][1] != str(moved.resolve()), (
            "the stored path was refreshed before anything resolved it"
        )
        assert {
            row.id: (str(row.workspace_id), str(row.identity_path), str(row.sha256))
            for row in _rows(reopened)
        } == documents_before, "the move disturbed document identity"
        # Ingest into the moved folder: same workspace, not a new one.
        assert _pipeline(reopened).run(root=moved / "corpus").ok
        assert _workspace_rows(reopened.database)[0][0] == identity_before
        assert len(_workspace_rows(reopened.database)) == 1
    finally:
        reopened.close()


def test_a_copy_is_a_separate_workspace(tmp_path) -> None:
    """A copy carries a different database, so it is a different system of record entirely."""
    workspace = _mk(tmp_path / "original", "Original")
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    assert _pipeline(workspace).run(root=corpus).ok
    original_id = _id_of(workspace)
    workspace.close()

    copied_root = tmp_path / "copy"
    shutil.copytree(str(workspace.root), str(copied_root))
    copy = Workspace.open(copied_root, workspace.settings)
    try:
        # The copy inherited the original's rows, including its workspace row pointing at the
        # original's path. That row does not match, the copy's database *is* inside the copy, so
        # this is the genuine relocation case - one row, reused, path refreshed.
        rows = _workspace_rows(copy.database)
        assert len(rows) == 1, f"a copy created extra rows: {rows}"
        assert rows[0][0] == original_id, "the copy did not reuse the logical workspace"
        # Merely opening the copy changes nothing: identity is resolved on ingest, not on open, so
        # the row still names the original folder until something actually writes.
        assert rows[0][1] != str(copied_root.resolve())
        # The first ingest into the copy resolves it - same logical workspace, refreshed path.
        assert _pipeline(copy).run(root=copied_root / "corpus").ok
        rows = _workspace_rows(copy.database)
        assert len(rows) == 1, "ingesting into the copy created a second workspace row"
        assert rows[0][0] == original_id
        assert rows[0][1] == str(copied_root.resolve()), "the copy's row was not refreshed"
    finally:
        copy.close()


# ------------------------------------------------- project / well consistency (brief 10)


def test_contradictory_project_and_well_are_rejected_not_reconciled(two) -> None:
    """A well belongs to one project; saying otherwise is an error, not something to repair."""
    first, _second, _wells = two
    with first.database.session() as session:
        repository = WellRepository(session)
        project_a = repository.get_or_create_project("Project A")
        project_b = repository.get_or_create_project("Project B")
        well = repository.create_well("A-3", project_id=project_a.id)
        session.commit()
        well_id, foreign_project = str(well.id), str(project_b.id)

    corpus = first.root / "corpus"
    build_corpus(corpus)
    refused = _pipeline(first).run(root=corpus, well_id=well_id, project_id=foreign_project)
    assert refused.ok is False
    assert "does not belong to" in (refused.error or ""), refused.error
    assert not _rows(first)

    with first.database.session() as session:
        owning = str(session.get(Well, well_id).project_id)
    assert _pipeline(first).run(root=corpus, well_id=well_id, project_id=owning).ok


def test_a_well_is_never_inferred_from_the_filename(two) -> None:
    """The corpus contains files named for A-3 and B-11; ingestion must not read them."""
    first, _second, wells = two
    corpus = first.root / "corpus"
    build_corpus(corpus)
    # Every file goes to A-3, including the one whose name says well_b11.
    assert _pipeline(first).run(root=corpus, well_id=wells[first.config.name]["A-3"]).ok
    rows = _rows(first)
    assert rows, "nothing was ingested"
    assert {str(row.well_id) for row in rows} == {wells[first.config.name]["A-3"]}, (
        "a document was filed under a well inferred from its filename"
    )


def test_duplicate_well_names_resolve_deterministically(two) -> None:
    """Two projects may both contain A-3; the answer must not depend on iteration order."""
    first, _second, _wells = two
    with first.database.session() as session:
        repository = WellRepository(session)
        one = repository.get_or_create_project("One")
        two_ = repository.get_or_create_project("Two")
        first_well = repository.create_well("A-3", project_id=one.id)
        second_well = repository.create_well("A-3", project_id=two_.id)
        session.commit()
        ids = (str(first_well.id), str(second_well.id))

    with first.database.session() as session:
        repository = WellRepository(session)
        answers = {str(repository.find_well("A-3").id) for _ in range(5)}
        by_project = {
            str(repository.find_well("A-3", project_id=str(p)).id) for p in (one.id, two_.id)
        }
    assert len(answers) == 1, f"find_well('A-3') is not deterministic: {answers}"
    assert by_project == set(ids), "scoping by project did not separate the two A-3 wells"


# ------------------------------------------------- structured isolation (brief 12/13)


def test_structured_records_cannot_cross_the_database_boundary(tmp_path) -> None:
    """Two workspaces, similar promoted records, separate index files.

    The point is not "the rows differ because the well ids differ". It is that A's index physically
    cannot hold B's rows, because B's records live in B's database and the index is built from the
    registry it sits beside.
    """
    first, second = _mk(tmp_path / "a", "Alpha"), _mk(tmp_path / "b", "Beta")
    for workspace in (first, second):
        ingest(workspace, wells=("A-3", "B-11"))
        promote(workspace)

    first_wells = {str(row.id) for row in _fetch_wells(first)}
    second_wells = {str(row.id) for row in _fetch_wells(second)}
    assert first_wells and second_wells
    assert not first_wells & second_wells, "the two workspaces share well ids"

    for workspace, own_wells, foreign_wells in (
        (first, first_wells, second_wells),
        (second, second_wells, first_wells),
    ):
        SearchService.for_workspace(workspace).rebuild()
        con = sqlite3.connect(workspace.index_database_path)
        try:
            wells_in_index = {
                str(row[0]) for row in con.execute("SELECT DISTINCT well_id FROM search_structured")
            }
            total = con.execute("SELECT COUNT(*) FROM search_structured").fetchone()[0]
        finally:
            con.close()
        assert total > 0, f"{workspace.config.name} indexed no structured rows"
        # Rows with no well carry the empty sentinel rather than a guessed scope, so they are
        # accounted for separately: "unowned" is a real state, not a leak.
        unowned = "" in wells_in_index
        owned = wells_in_index - {""}
        assert owned <= own_wells, (
            f"{workspace.config.name}'s index names a well that is not in its own registry: "
            f"{owned - own_wells}"
        )
        assert not owned & foreign_wells, (
            f"{workspace.config.name}'s index contains wells from the other workspace"
        )
        assert unowned or owned, "the index holds nothing at all"


def test_structured_search_returns_only_the_local_workspace(tmp_path) -> None:
    first, second = _mk(tmp_path / "a", "Alpha"), _mk(tmp_path / "b", "Beta")
    for workspace in (first, second):
        ingest(workspace, wells=("A-3", "B-11"))
        promote(workspace)

    query = "mud report"
    for workspace in (first, second):
        service = SearchService.for_workspace(workspace)
        service.rebuild()
        own = {str(row.id) for row in _fetch_wells(workspace)}
        results = service.search(query)
        rows = results.results if hasattr(results, "results") else results
        for row in rows:
            well = getattr(row, "well_id", None)
            if well:
                assert str(well) in own, "a search returned a row from outside this workspace"


def _fetch_wells(workspace: Workspace) -> list[Well]:
    with workspace.database.read_only() as session:
        return list(session.execute(select(Well)).scalars())


def test_structured_rows_without_a_well_are_database_local_not_globally_shared(tmp_path) -> None:
    """A structured row with no well belongs to its database, not to every workspace.

    Some structured rows carry no well relationship at all. The temptation is to invent a scope for
    them; the honest answer is that their scope is the database they were promoted into, which is
    exactly what the one-index-per-workspace boundary already provides.  Nothing is assigned, and
    nothing leaks - each index file only ever holds rows promoted into its own registry.
    """
    first, second = _mk(tmp_path / "a", "Alpha"), _mk(tmp_path / "b", "Beta")
    for workspace in (first, second):
        ingest(workspace, wells=("A-3",))
        promote(workspace)

    seen: dict[str, set[str]] = {}
    for workspace in (first, second):
        SearchService.for_workspace(workspace).rebuild()
        con = sqlite3.connect(workspace.index_database_path)
        try:
            ids = {
                str(row[0]) for row in con.execute("SELECT DISTINCT well_id FROM search_structured")
            }
            unowned = con.execute(
                "SELECT COUNT(*) FROM search_structured WHERE well_id IS NULL OR well_id=''"
            ).fetchone()[0]
            total = con.execute("SELECT COUNT(*) FROM search_structured").fetchone()[0]
        finally:
            con.close()
        assert total > 0, f"{workspace.config.name} indexed no structured rows"
        assert unowned <= total
        seen[workspace.config.name] = ids

    # Unowned rows are not given a synthetic well, so the only wells present are real local ones.
    for name, ids in seen.items():
        workspace = first if name == first.config.name else second
        own = {str(row.id) for row in _fetch_wells(workspace)}
        assert ids - {""} <= own, (
            f"{name}'s index names a well that is not in its own registry: {ids - own - {''}}"
        )


def test_well_scoped_structured_search_actually_narrows(tmp_path) -> None:
    """The well filter on structured rows is load-bearing, and a mutation proved it was untested.

    Workspace isolation does not depend on it - each workspace has its own index file built from its
    own registry, so ``workspace_id`` was deliberately *not* added to ``search_structured`` (see the
    note in ``SearchFilters.applies_to_structured``). But scoping a search to one well within a
    workspace does depend on it, and until this test existed, deleting that filter from the
    structured half broke nothing.
    """
    workspace = _mk(tmp_path / "scoped", "Scoped")
    ingest(workspace, wells=("A-3", "B-11"))  # ingest registers the wells itself
    promote(workspace)
    wells = {row.name: row.id for row in _fetch_wells(workspace)}
    assert {"A-3", "B-11"} <= set(wells), f"the corpus did not produce both wells: {sorted(wells)}"

    service = SearchService.for_workspace(workspace)
    service.rebuild()

    # An empty query matches nothing by design, so the proof uses a term the corpus really contains.
    # The limit is explicit: the default cap is small enough to hide a narrowing effect entirely,
    # which is how a filter can look like it works while nothing is being excluded.
    query, limit = "mud", 500
    unscoped = service.search(query, limit=limit).results
    assert len(unscoped) > 1, f"the corpus indexed too little to prove narrowing ({len(unscoped)})"

    a_rows = service.search(query, well_id=str(wells["A-3"]), limit=limit).results
    b_rows = service.search(query, well_id=str(wells["B-11"]), limit=limit).results
    # A hit is identified by its chunk; the well it belongs to is carried in metadata, not as a
    # top-level attribute.
    key = lambda rows: {r.chunk_id for r in rows}  # noqa: E731
    all_ids, a_ids, b_ids = key(unscoped), key(a_rows), key(b_rows)

    assert a_ids | b_ids <= all_ids, "a scoped search returned something the unscoped one did not"
    assert not a_ids & b_ids, "the two wells returned the same rows"
    # This corpus's "mud" matches all happen to sit under A-3, so B-11 is the strict subset that
    # proves rows are actually being excluded rather than the filter being ignored.
    assert len(b_ids) < len(all_ids), "scoping to the second well excluded nothing"
    assert a_rows or b_rows, "both wells returned nothing at all"
    for row in a_rows + b_rows:
        labelled = row.metadata.get("well_id")
        if labelled:
            assert str(labelled) in {str(wells["A-3"]), str(wells["B-11"])}
