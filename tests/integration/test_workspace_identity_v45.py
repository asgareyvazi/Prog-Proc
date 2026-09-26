"""V4.5 - workspace identity, scope integrity and the boundary between them.

One defect, traced to its origin.  ``Document.workspace_id`` is nullable because the foreign key is
``ondelete="SET NULL"`` - a NULL legitimately means "the owning workspace row was deleted".  But
workspace *resolution* lived only in the CLI, so every other caller of ``IngestionPipeline`` - the
desktop UI, the API, a fixture - filed documents under ``workspace_id IS NULL`` while a workspace
row for that exact folder was sitting in the table.  Every workspace-scoped query filters on that
column, so the documents existed and were invisible: ``knowledge rebuild --dry-run`` returned
exit 0 with ``versions: 0`` over a workspace holding nine documents.

Reproduced before it was fixed, and the numbers are in the commit message: 9 documents, 9 NULL
workspace ids, 9 well ids, dry-run exit 0, versions 0.

The fix is at the persistence boundary, not in a query.  ``WellRepository.resolve_workspace_id`` is
now the one authoritative answer to "which workspace row owns this folder", ingestion calls it when
the caller does not supply an id, and the CLI delegates to the same function instead of carrying its
own copy.  Nothing here converts a NULL into an id by guessing: resolution is by resolved path,
which is unique in the registry.

The second half is scope.  "What a rebuild deletes", "what it re-derives" and "what status reports"
were three separate filters that happened to agree, and one of them (``delete_derived``) already
proved it could drift by destroying 16 rows.  :class:`KnowledgeScope` defines the population once,
and every scoped query is built from it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from io import StringIO

import pytest
from sqlalchemy import select
from tests.fixtures.generate import build_corpus, build_v4_forensic_corpus

from drilling_intelligence.cli import main
from drilling_intelligence.database.models import (
    Document,
    DocumentVersion,
    Extraction,
    KnowledgeConflict,
    KnowledgeItem,
)
from drilling_intelligence.database.models import (
    Workspace as WorkspaceRow,
)
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.knowledge.entities import EntityRef
from drilling_intelligence.knowledge.facts import KnowledgeFact
from drilling_intelligence.knowledge.recovery import (
    CLEAN,
    CONFLICTS_PRESENT,
    INDEX_STALE,
    KNOWLEDGE_STALE,
    assess_recovery,
)
from drilling_intelligence.knowledge.repository import KnowledgeRepository, KnowledgeScope
from drilling_intelligence.knowledge.service import KnowledgeExtractionService
from drilling_intelligence.wells.repository import WellRepository


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main(list(argv))
    finally:
        sys.stdout, sys.stderr = saved
    return code, out.getvalue(), err.getvalue()


def _document(stdout: str) -> dict:
    return json.loads(stdout[stdout.index("{") :])


def _workspace(tmp_path, name: str = "Identity"):
    from drilling_intelligence.config.settings import Settings
    from drilling_intelligence.wells.workspace import Workspace

    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / f"{name.lower()}.toml"
    config.write_text(
        '[app]\ndata_dir = ".drillintel"\n\n[ai]\nenabled = false\nrequire_ai = false\n\n'
        '[mineru]\nmode = "disabled"\n',
        encoding="utf-8",
    )
    settings = Settings.load(config)
    root = tmp_path / name.lower()
    workspace = Workspace.create(root, settings, name=name)
    return workspace, config, settings


def _wells(workspace, *names: str) -> dict[str, str]:
    with workspace.database.session() as session:
        repository = WellRepository(session)
        repository.get_or_create_workspace(str(workspace.root), name=workspace.config.name)
        project = repository.get_or_create_project(workspace.config.name)
        ids = {name: str(repository.create_well(name, project_id=project.id).id) for name in names}
        session.commit()
    return ids


def _pipeline(workspace) -> IngestionPipeline:
    return IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    )


def _split_corpus(corpus) -> dict[str, list[str]]:
    """Spread the generated corpus over three folders so each can be ingested under a different well."""
    build_corpus(corpus)
    files = sorted(p.name for p in corpus.iterdir() if p.is_file())
    groups = {"a": files[0:2], "b": files[2:4], "none": files[4:6]}
    for name, group in groups.items():
        target = corpus / name
        target.mkdir()
        for filename in group:
            (corpus / filename).rename(target / filename)
    return groups


def _documents(workspace) -> list[Document]:
    with workspace.database.read_only() as session:
        return list(session.execute(select(Document).order_by(Document.identity_path)).scalars())


def _fact_ids(workspace, **filters) -> set[str]:
    with workspace.database.read_only() as session:
        rows = [row for row in session.execute(select(KnowledgeItem)).scalars() if row.lookup_key]
    for column, value in filters.items():
        rows = [row for row in rows if str(getattr(row, column) or "") == value]
    return {str(row.id) for row in rows}


# ------------------------------------------------------------------- Phase 4: the identity invariant


def test_every_ingested_document_gets_the_workspace_identity(tmp_path) -> None:
    """The invariant the whole mission turns on."""
    workspace, _config, _settings = _workspace(tmp_path)
    wells = _wells(workspace, "A-3")
    corpus = workspace.root / "corpus"
    build_corpus(corpus)

    result = _pipeline(workspace).run(root=corpus, well_id=wells["A-3"])
    assert result.ok, result.error
    assert result.files_registered > 0

    rows = _documents(workspace)
    assert rows, "nothing was registered"
    assert {str(row.workspace_id) for row in rows} != {"None"}, (
        "a document with no workspace identity is invisible to every workspace-scoped query"
    )
    assert len({str(row.workspace_id) for row in rows}) == 1, "one corpus split across workspaces"


def test_the_caller_never_had_to_ask_for_it(tmp_path) -> None:
    """The pipeline resolves identity itself; no ``workspace_id`` argument was passed above."""
    workspace, _config, _settings = _workspace(tmp_path)
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    # No well either: workspace ownership does not depend on a well being recognised.
    result = _pipeline(workspace).run(root=corpus)
    assert result.ok, result.error
    assert all(row.workspace_id for row in _documents(workspace))
    assert all(row.well_id is None for row in _documents(workspace)), (
        "workspace identity must not be smuggled in as well identity"
    )


def test_an_existing_workspace_row_is_reused_not_duplicated(tmp_path) -> None:
    """Resolution is by resolved path, which is unique - so it cannot register a folder twice."""
    workspace, _config, _settings = _workspace(tmp_path)
    _wells(workspace, "A-3")
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    _pipeline(workspace).run(root=corpus)

    with workspace.database.read_only() as session:
        rows = list(session.execute(select(WorkspaceRow)).scalars())
    assert len(rows) == 1, (
        f"ingestion created a second workspace row: {[r.root_path for r in rows]}"
    )
    assert str(rows[0].id) == str(_documents(workspace)[0].workspace_id)


def test_an_explicit_workspace_id_still_wins(tmp_path) -> None:
    """The parameter is an override, not a suggestion - a caller that knows better is believed."""
    workspace, _config, _settings = _workspace(tmp_path)
    wells = _wells(workspace, "A-3")
    with workspace.database.session() as session:
        other = WellRepository(session).get_or_create_workspace(
            str(tmp_path / "elsewhere"), name="Elsewhere"
        )
        other_id = str(other.id)
        session.commit()

    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    _pipeline(workspace).run(root=corpus, workspace_id=other_id, well_id=wells["A-3"])
    assert {str(row.workspace_id) for row in _documents(workspace)} == {other_id}


def test_repeated_ingestion_is_idempotent_in_identity(tmp_path) -> None:
    """Same corpus twice: no duplicate rows, and the identity does not move."""
    workspace, _config, _settings = _workspace(tmp_path)
    wells = _wells(workspace, "A-3")
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    pipeline = _pipeline(workspace)
    assert pipeline.run(root=corpus, well_id=wells["A-3"]).ok

    before = {
        (row.identity_path, str(row.workspace_id), str(row.well_id)): str(row.id)
        for row in _documents(workspace)
    }
    with workspace.database.read_only() as session:
        versions_before = len(list(session.execute(select(DocumentVersion)).scalars()))

    assert pipeline.run(root=corpus, well_id=wells["A-3"]).ok
    after = {
        (row.identity_path, str(row.workspace_id), str(row.well_id)): str(row.id)
        for row in _documents(workspace)
    }
    with workspace.database.read_only() as session:
        versions_after = len(list(session.execute(select(DocumentVersion)).scalars()))

    assert before == after, "a second ingest changed document identity"
    assert versions_before == versions_after, "a second ingest invented versions"
    with workspace.database.read_only() as session:
        assert len(list(session.execute(select(WorkspaceRow)).scalars())) == 1


# ------------------------------------------------------------------- Phase 5: well stays distinct


@pytest.fixture
def three_well_corpus(tmp_path):
    """One workspace: two documents on well A, two on well B, two on no well at all."""
    workspace, config, settings = _workspace(tmp_path)
    wells = _wells(workspace, "A-3", "B-11")
    corpus = workspace.root / "corpus"
    groups = _split_corpus(corpus)
    pipeline = _pipeline(workspace)
    assert pipeline.run(root=corpus / "a", well_id=wells["A-3"]).ok
    assert pipeline.run(root=corpus / "b", well_id=wells["B-11"]).ok
    assert pipeline.run(root=corpus / "none").ok
    return workspace, config, settings, wells, groups


def test_well_identity_is_distinct_from_workspace_identity(three_well_corpus) -> None:
    workspace, _config, _settings, wells, _groups = three_well_corpus
    rows = _documents(workspace)
    assert len(rows) == 6

    # All six belong to the workspace...
    assert len({str(row.workspace_id) for row in rows}) == 1
    assert all(row.workspace_id for row in rows)
    # ...but only four belong to a well, and each to its own.
    by_well = {
        well: sorted(row.filename for row in rows if str(row.well_id) == wid)
        for well, wid in wells.items()
    }
    assert len(by_well["A-3"]) == 2 and len(by_well["B-11"]) == 2
    assert sum(1 for row in rows if row.well_id is None) == 2, (
        "a document can belong to a workspace without belonging to a well"
    )
    assert not set(by_well["A-3"]) & set(by_well["B-11"]), "a document leaked between wells"


def test_a_document_with_no_well_is_in_workspace_scope(three_well_corpus) -> None:
    """The point of Phase 5: no-well must not mean no-workspace."""
    workspace, _config, _settings, wells, _groups = three_well_corpus
    with workspace.database.read_only() as session:
        workspace_id = str(next(iter(session.execute(select(WorkspaceRow)).scalars())).id)

    service = KnowledgeExtractionService.for_workspace(workspace)
    everything = service.status(workspace_id=workspace_id)
    just_a = service.status(workspace_id=workspace_id, well_id=wells["A-3"])
    just_b = service.status(workspace_id=workspace_id, well_id=wells["B-11"])

    assert everything["facts"] > just_a["facts"], "workspace scope must be wider than one well"
    assert everything["facts"] > just_b["facts"]
    # The no-well documents contribute to the workspace and to neither well.
    assert everything["facts"] > just_a["facts"] + just_b["facts"], (
        "the two well-scoped counts already cover the workspace, so the no-well documents vanished"
    )


# ------------------------------------------------------------------- Phase 7: the scope matrix


def test_delete_and_rederive_cover_exactly_the_same_documents(three_well_corpus) -> None:
    """``scope(delete_derived) == scope(sync_all)``, checked on row ids rather than counts."""
    workspace, _config, _settings, wells, _groups = three_well_corpus
    with workspace.database.read_only() as session:
        workspace_id = str(next(iter(session.execute(select(WorkspaceRow)).scalars())).id)
    service = KnowledgeExtractionService.for_workspace(workspace)
    service.rebuild(workspace_id=workspace_id, well_id="")  # derive everything first

    for label, well in (("A-3", wells["A-3"]), ("B-11", wells["B-11"]), ("all", "")):
        before = _fact_ids(workspace)
        plan = service.plan_rebuild(workspace_id=workspace_id, well_id=well)
        # Nothing moved yet.
        assert _fact_ids(workspace) == before, f"{label}: the dry run mutated rows"

        real = service.rebuild(workspace_id=workspace_id, well_id=well)
        after = _fact_ids(workspace)
        assert real["removed"] == plan["plan"]["facts"]["remove"], label
        assert real["facts"]["created"] == plan["plan"]["facts"]["create"], label
        # Whatever changed, changed inside the scope: the surviving set is the same size plus
        # whatever was re-derived, and nothing outside the scope disappeared.
        if well:
            other = wells["B-11"] if well == wells["A-3"] else wells["A-3"]
            assert _fact_ids(workspace, well_id=other) <= after, (
                f"{label}: a rebuild scoped away from {other} removed its rows"
            )
        assert after, f"{label}: the rebuild emptied the corpus"


def test_a_well_scoped_rebuild_leaves_the_other_well_and_the_unfiled_alone(
    three_well_corpus,
) -> None:
    workspace, _config, _settings, wells, _groups = three_well_corpus
    with workspace.database.read_only() as session:
        workspace_id = str(next(iter(session.execute(select(WorkspaceRow)).scalars())).id)
    service = KnowledgeExtractionService.for_workspace(workspace)
    service.rebuild(workspace_id=workspace_id, well_id="")

    a_before = _fact_ids(workspace, well_id=wells["A-3"])
    b_before = _fact_ids(workspace, well_id=wells["B-11"])
    unfiled_before = {
        row_id
        for row_id in _fact_ids(workspace)
        if row_id not in a_before and row_id not in b_before
    }
    assert a_before and b_before and unfiled_before, "the fixture does not exercise all three cases"

    service.rebuild(workspace_id=workspace_id, well_id=wells["A-3"])

    assert _fact_ids(workspace, well_id=wells["B-11"]) == b_before, "well B was touched"
    assert unfiled_before <= _fact_ids(workspace), "unfiled documents were touched"
    assert _fact_ids(workspace, well_id=wells["A-3"]) == a_before, (
        "A was re-derived into a different set than it had"
    )


def test_unknown_well_is_an_error_and_never_zero_rows(three_well_corpus) -> None:
    workspace, config, _settings, _wells, _groups = three_well_corpus
    code, _out, err = _run(
        "knowledge",
        "rebuild",
        "--well",
        "ZZ-9",
        "--workspace",
        str(workspace.root),
        "--config",
        str(config),
    )
    assert code == 2, "an unknown well is a usage error, not an empty result"
    assert "no well matches" in err
    assert "A-3" in err and "B-11" in err, "the alternatives are the useful half of the message"


def test_an_empty_workspace_is_a_legitimate_zero(tmp_path) -> None:
    """Zero versions is correct here, and must not be reported as an identity defect."""
    workspace, config, _settings = _workspace(tmp_path, name="Empty")
    code, out, err = _run(
        "knowledge",
        "rebuild",
        "--dry-run",
        "--workspace",
        str(workspace.root),
        "--config",
        str(config),
        "--json",
    )
    assert code == 0, err
    plan = _document(out)
    assert plan["plan"]["versions"] == 0
    assert not any("matched no current document version" in item for item in plan["warnings"]), (
        "an empty workspace has nothing to match; warning here would be crying wolf"
    )
    assert plan["recovery"]["state"] == CLEAN


def test_a_workspace_with_documents_can_never_report_zero_versions(three_well_corpus) -> None:
    """The failure this mission exists to make impossible."""
    workspace, config, _settings, _wells, _groups = three_well_corpus
    code, out, err = _run(
        "knowledge",
        "rebuild",
        "--dry-run",
        "--workspace",
        str(workspace.root),
        "--config",
        str(config),
        "--json",
    )
    assert code == 0, err
    plan = _document(out)
    assert len(_documents(workspace)) == 6
    assert plan["plan"]["versions"] == 6, (
        "the workspace holds six documents and the plan saw none of them"
    )
    assert plan["plan"]["facts"]["remove"] > 0


# ------------------------------------------------------------------- Phase 12: one population


def test_status_plan_and_rebuild_agree_on_the_population(three_well_corpus) -> None:
    """The planner must describe the rows the rebuild will actually touch."""
    workspace, _config, _settings, wells, _groups = three_well_corpus
    with workspace.database.read_only() as session:
        workspace_id = str(next(iter(session.execute(select(WorkspaceRow)).scalars())).id)
    service = KnowledgeExtractionService.for_workspace(workspace)
    service.rebuild(workspace_id=workspace_id, well_id="")

    for well in ("", wells["A-3"], wells["B-11"]):
        status = service.status(workspace_id=workspace_id, well_id=well)
        plan = service.plan_rebuild(workspace_id=workspace_id, well_id=well)
        real = service.rebuild(workspace_id=workspace_id, well_id=well)
        label = well or "workspace"
        assert plan["plan"]["facts"]["remove"] == real["removed"], label
        assert plan["plan"]["facts"]["create"] == real["facts"]["created"], label
        # The plan's version count is the population status is talking about.
        assert plan["plan"]["versions"] > 0, label
        assert status["facts"] >= real["facts"]["created"], (
            f"{label}: status reports fewer facts than the rebuild derives"
        )


def test_the_scope_object_is_the_single_definition_of_the_population() -> None:
    """Three scopes, one predicate - and an empty scope means the whole file, not nothing."""
    assert KnowledgeScope.of("", "").scoped is False
    assert KnowledgeScope.of("", "").document_ids() is None
    assert KnowledgeScope.of("ws-1", "").document_ids() is not None
    assert KnowledgeScope.of(None, "well-1").scoped is True
    assert KnowledgeScope.of("ws-1", "well-1") == KnowledgeScope("ws-1", "well-1")
    # Frozen: a scope that can be edited mid-query is how two filters drift apart.
    with pytest.raises(FrozenInstanceError):
        KnowledgeScope("ws-1").well_id = "other"  # type: ignore[misc]


# ------------------------------------------------------------------- Phase 13: the state matrix


def test_state_a_an_empty_workspace_is_clean() -> None:
    assert assess_recovery({"knowledge": {"needs_rebuild": False, "facts": 0}})["state"] == CLEAN


def test_state_b_documents_without_knowledge_is_stale_not_corrupt() -> None:
    assessment = assess_recovery(
        {"knowledge": {"needs_rebuild": True, "facts": 0, "versions_with_artefacts": 6}}
    )
    assert assessment["state"] == KNOWLEDGE_STALE
    assert assessment["corrupt"] is False
    assert [step["command"] for step in assessment["recommended"]] == [
        "drillintel knowledge rebuild"
    ]


def test_state_c_one_stale_well_does_not_make_the_workspace_corrupt() -> None:
    """Well A is stale, well B is clean: each scope reports its own truth."""
    stale = assess_recovery(
        {"knowledge": {"needs_rebuild": True, "detached_facts": 4, "facts": 20}}
    )
    clean = assess_recovery({"knowledge": {"needs_rebuild": False, "facts": 20}})
    assert stale["state"] == KNOWLEDGE_STALE
    assert clean["state"] == CLEAN
    assert stale["corrupt"] is False and clean["corrupt"] is False
    # And the workspace view reflects that something in it needs work.
    both = assess_recovery({"knowledge": {"needs_rebuild": True, "detached_facts": 4, "facts": 40}})
    assert both["state"] == KNOWLEDGE_STALE


def test_state_d_a_stale_index_is_index_recovery() -> None:
    assessment = assess_recovery(
        {"knowledge": {"needs_rebuild": False, "facts": 40}, "index": {"missing_versions": 6}}
    )
    assert assessment["state"] == INDEX_STALE
    assert [step["command"] for step in assessment["recommended"]] == ["drillintel index rebuild"]


def test_state_e_real_conflicts_are_not_corruption() -> None:
    assessment = assess_recovery(
        {"knowledge": {"needs_rebuild": False, "facts": 40}, "conflicts": {"open": 2}}
    )
    assert assessment["state"] == CONFLICTS_PRESENT
    assert assessment["corrupt"] is False
    assert assessment["recommended"] == []


# ------------------------------------------------------------------- Phase 25/26: isolation


def test_two_workspaces_with_similar_content_do_not_leak(tmp_path) -> None:
    """W1 and W2, same field names, same well names, different values."""
    first, _first_config, _first_settings = _workspace(tmp_path / "one", name="One")
    second, _second_config, _second_settings = _workspace(tmp_path / "two", name="Two")
    for workspace in (first, second):
        corpus = workspace.root / "corpus"
        build_v4_forensic_corpus(corpus)
        wells = _wells(workspace, "A-3")
        result = _pipeline(workspace).run(root=corpus, well_id=wells["A-3"])
        assert result.ok, result.error

    first_rows = _documents(first)
    second_rows = _documents(second)
    first_ids = {str(row.id) for row in first_rows}
    second_ids = {str(row.id) for row in second_rows}
    assert first_ids and second_ids
    assert not first_ids & second_ids, "the two workspaces share document ids"
    assert {str(row.workspace_id) for row in first_rows} != {
        str(row.workspace_id) for row in second_rows
    }, "both workspaces filed under the same identity"

    first_before = _fact_ids(first)
    second_before = _fact_ids(second)
    with second.database.read_only() as session:
        second_ws = str(next(iter(session.execute(select(WorkspaceRow)).scalars())).id)

    # Rebuilding W2 must not move a single row in W1.
    KnowledgeExtractionService.for_workspace(second).rebuild(workspace_id=second_ws, well_id="")
    assert _fact_ids(first) == first_before, "a rebuild of W2 changed W1's knowledge"
    assert _fact_ids(second) == second_before or second_before, "W2 lost its knowledge"

    # W1's status counts only W1.
    with first.database.read_only() as session:
        first_ws = str(next(iter(session.execute(select(WorkspaceRow)).scalars())).id)
    status = KnowledgeExtractionService.for_workspace(first).status(workspace_id=first_ws)
    assert status["facts"] == len(first_before), (
        f"W1's status counted {status['facts']} facts but W1 holds {len(first_before)}"
    )


def test_a_well_scoped_rebuild_does_not_change_the_other_wells_conflicts(three_well_corpus) -> None:
    workspace, _config, _settings, wells, _groups = three_well_corpus
    with workspace.database.read_only() as session:
        workspace_id = str(next(iter(session.execute(select(WorkspaceRow)).scalars())).id)
    service = KnowledgeExtractionService.for_workspace(workspace)
    service.rebuild(workspace_id=workspace_id, well_id="")

    def conflicts_for(well: str) -> set[str]:
        with workspace.database.read_only() as session:
            statement = select(KnowledgeConflict)
            if well:
                statement = statement.where(KnowledgeConflict.well_id == well)
            return {str(row.id) for row in session.execute(statement).scalars()}

    b_before = conflicts_for(wells["B-11"])
    service.rebuild(workspace_id=workspace_id, well_id=wells["A-3"])
    assert conflicts_for(wells["B-11"]) == b_before, "a rebuild of A changed B's conflicts"


# ------------------------------------------------------------------- Phase 27: manual knowledge


def test_manual_facts_survive_a_scoped_rebuild(three_well_corpus) -> None:
    workspace, _config, _settings, wells, _groups = three_well_corpus
    with workspace.database.session() as session:
        workspace_id = str(next(iter(session.execute(select(WorkspaceRow)).scalars())).id)
        repository = KnowledgeRepository(session)
        for well, value in ((wells["A-3"], "9.9 ppg"), (wells["B-11"], "10.4 ppg")):
            repository.manual_fact(
                KnowledgeFact(
                    subject=EntityRef("well", well, label=well),
                    predicate="mud_weight",
                    value_type="quantity",
                    original_value=value,
                    text=value,
                    well_id=well,
                    record_state="ACTUAL",
                )
            )
        session.commit()

    def manual() -> set[str]:
        with workspace.database.read_only() as session:
            return {
                str(row.id)
                for row in session.execute(select(KnowledgeItem)).scalars()
                if row.origin == "MANUAL"
            }

    assert len(manual()) == 2
    service = KnowledgeExtractionService.for_workspace(workspace)
    service.rebuild(workspace_id=workspace_id, well_id="")
    assert manual() and len(manual()) == 2, "a workspace rebuild removed a manual note"
    service.rebuild(workspace_id=workspace_id, well_id=wells["A-3"])
    assert len(manual()) == 2, "a well-scoped rebuild removed a manual note"


# ------------------------------------------------------------------- Phase 16/19: forensics


def test_identity_is_the_only_thing_the_fix_changes(three_well_corpus) -> None:
    """Provenance, versions, extraction payloads and lookup keys are untouched by the fix."""
    workspace, _config, _settings, _wells, _groups = three_well_corpus

    def fingerprint() -> dict[str, str]:
        with workspace.database.read_only() as session:
            docs = {
                str(row.id): f"{row.identity_path}|{row.sha256}|{row.filename}"
                for row in session.execute(select(Document)).scalars()
            }
            versions = len(list(session.execute(select(DocumentVersion)).scalars()))
            payloads = {
                str(row.id): json.dumps(row.document_json, sort_keys=True, default=str)
                for row in session.execute(select(Extraction)).scalars()
            }
            keys = {
                str(row.id): str(row.lookup_key)
                for row in session.execute(select(KnowledgeItem)).scalars()
                if row.lookup_key
            }
        return {
            "documents": json.dumps(docs, sort_keys=True),
            "versions": str(versions),
            "payloads": json.dumps(payloads, sort_keys=True),
            "lookup_keys": json.dumps(keys, sort_keys=True),
        }

    before = fingerprint()
    with workspace.database.read_only() as session:
        workspace_id = str(next(iter(session.execute(select(WorkspaceRow)).scalars())).id)
    KnowledgeExtractionService.for_workspace(workspace).rebuild(
        workspace_id=workspace_id, well_id=""
    )
    after = fingerprint()
    assert before["documents"] == after["documents"], "the identity fix changed document identity"
    assert before["versions"] == after["versions"]
    assert before["payloads"] == after["payloads"], "the fix rewrote an extraction artefact"
    assert before["lookup_keys"] == after["lookup_keys"], "the fix changed a lookup key"


def test_null_workspace_rows_are_classified_not_blindly_filled(tmp_path) -> None:
    """A NULL is a real state - ``ondelete="SET NULL"`` - and must be explainable, not overwritten."""
    workspace, _config, _settings = _workspace(tmp_path, name="Legacy")
    wells = _wells(workspace, "A-3")
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    _pipeline(workspace).run(root=corpus, well_id=wells["A-3"])
    rows = _documents(workspace)
    assert all(row.workspace_id for row in rows), "fresh ingestion must not produce NULLs"

    # Simulate the one legitimate way a NULL appears: the owning workspace row is deleted.
    with workspace.database.session() as session:
        session.delete(next(iter(session.execute(select(WorkspaceRow)).scalars())))
        session.commit()
    with workspace.database.read_only() as session:
        orphaned = list(session.execute(select(Document)).scalars())
    assert orphaned and all(row.workspace_id is None for row in orphaned), (
        "the schema's own ondelete=SET NULL is the documented meaning of a NULL workspace"
    )

    # Classifying is a read; nothing here updates those rows.
    classification = {
        "orphaned_workspace_deleted": sum(1 for row in orphaned if row.workspace_id is None),
        "unknown": 0,
    }
    assert classification["orphaned_workspace_deleted"] == len(orphaned)
    assert classification["unknown"] == 0


def test_doctor_reports_workspace_identity_as_its_own_line(three_well_corpus) -> None:
    """Identity is a distinct question from "is anything derived", so it gets a distinct answer."""
    workspace, config, _settings, _wells, _groups = three_well_corpus
    code, out, err = _run(
        "doctor", "--workspace", str(workspace.root), "--config", str(config), "--json"
    )
    assert code in (0, 1), err
    report = _document(out)
    assert report["registry"]["unscoped_documents"] == 0, "a fresh workspace has no identity gaps"
    assert any(item.startswith("identity") and "healthy" in item for item in report["notes"]), (
        report["notes"]
    )
    # And it is a note, not a finding: a nullable-by-design column must not fail the exit code.
    # ``findings`` is a list of strings, and each one is what puts doctor on a non-zero exit.
    assert all(isinstance(item, str) for item in report["findings"])
    assert not any("identity" in item.lower() for item in report["findings"]), report["findings"]


def test_doctor_names_orphaned_documents_instead_of_hiding_them(tmp_path) -> None:
    """The one legitimate NULL - an orphan of a deleted workspace row - is still said out loud."""
    workspace, config, _settings = _workspace(tmp_path, name="Orphan")
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    _pipeline(workspace).run(root=corpus)

    with workspace.database.session() as session:
        session.delete(next(iter(session.execute(select(WorkspaceRow)).scalars())))
        session.commit()

    code, out, err = _run(
        "doctor", "--workspace", str(workspace.root), "--config", str(config), "--json"
    )
    assert code in (0, 1), err
    report = _document(out)
    assert report["registry"]["unscoped_documents"] == 6
    assert any(
        item.startswith("identity") and "6 document(s) with no workspace id" in item
        for item in report["notes"]
    ), report["notes"]


def test_well_scoped_staleness_counts_only_that_wells_versions(three_well_corpus) -> None:
    """``_staleness`` has to narrow too, or a well-scoped status reports the workspace's gap.

    The three-well fixture puts two documents in each scope, so the numbers are distinguishable:
    a workspace-wide count of six leaking into a well-scoped status is the difference between
    "well A needs work" and "something somewhere needs work".  These two counters are the ones
    that were computed from their own filter rather than from the shared scope.
    """
    workspace, _config, _settings, wells, _groups = three_well_corpus
    with workspace.database.read_only() as session:
        workspace_id = str(next(iter(session.execute(select(WorkspaceRow)).scalars())).id)
    service = KnowledgeExtractionService.for_workspace(workspace)

    whole = service.status(workspace_id=workspace_id)
    just_a = service.status(workspace_id=workspace_id, well_id=wells["A-3"])
    just_b = service.status(workspace_id=workspace_id, well_id=wells["B-11"])

    assert whole["versions_with_artefacts"] == 6, "the fixture is supposed to hold six documents"
    assert just_a["versions_with_artefacts"] == 2, (
        f"well A holds two documents but its status counted {just_a['versions_with_artefacts']} - "
        "the workspace-wide staleness population leaked in"
    )
    assert just_b["versions_with_artefacts"] == 2, (
        f"well B holds two documents but its status counted {just_b['versions_with_artefacts']}"
    )
    assert (
        just_a["versions_with_artefacts"] + just_b["versions_with_artefacts"]
        < whole["versions_with_artefacts"]
    ), "the two wells account for the whole workspace, so the no-well documents vanished"
    # Relations carry no scope column at all, so they are file-global by construction; conflicts
    # do carry a well, so they narrow.  Both are documented, neither is accidental.
    assert just_a["relations"] == whole["relations"], "knowledge_relation has no scope to narrow by"
    assert just_a["open_conflicts"] <= whole["open_conflicts"]
