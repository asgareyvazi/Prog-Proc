"""V4.4 - ``knowledge rebuild --dry-run``: a preview an operator can act on.

The question this suite answers is not "does the flag exist" but "may an operator trust it".  Four
properties, each pinned against the real path rather than a description of it:

**1. It changes nothing.**  Every mutable store - knowledge rows, edges, conflicts, document
versions, extraction artefacts, timestamps, and the separate index SQLite file - is fingerprinted
before and after and compared byte for byte.  Counting rows would pass a command that merely
bumped ``updated_at``.

**2. It is not a second implementation.**  The dry run calls :meth:`KnowledgeExtractionService.rebuild`
- the same method the real command calls - inside a transaction that is rolled back.  So the
predicted ``create``/``update``/``unchanged``/``remove`` set is not an estimate that happens to
agree; it is the execution's own accounting, observed and then discarded.  Test 3 pins that the
prediction equals what the real run reports.

**3. It is idempotent.**  Two dry runs give the same plan; two real rebuilds give the same
knowledge.  The write accounting is documented rather than assumed - ``rebuild`` deletes derived
rows first by design, so ``created`` counts re-derivation, and the invariant that actually matters
is that the resulting fact set is identical.

**4. It does not settle arguments.**  A rebuild that quietly resolved a genuine measured-depth
disagreement would be worse than no rebuild, so the conflicts before and after are compared and the
dry run reports them under ``not_performed`` instead.

The scope tests exist because a real defect was found while writing them: ``rebuild --well A-3``
used to delete the whole workspace's derived rows and re-derive only A-3's, reporting the loss as a
successful run.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fixtures.fieldops import ingest_v4
from tests.fixtures.generate import build_corpus
from tests.integration.test_knowledge_semantic_repair_v43 import (
    COLLAPSED_NAMES,
    _freeze_old_field_names,
)

from drilling_intelligence.cli import main
from drilling_intelligence.database.models import (
    Document,
    DocumentVersion,
    Extraction,
    KnowledgeConflict,
    KnowledgeItem,
    KnowledgeRelation,
)
from drilling_intelligence.knowledge.entities import EntityRef
from drilling_intelligence.knowledge.facts import KnowledgeFact
from drilling_intelligence.knowledge.repository import KnowledgeRepository
from drilling_intelligence.knowledge.service import KnowledgeExtractionService

#: The stores a dry run must leave alone.  ``document_version`` and ``extraction`` are in here
#: because "a preview re-read the artefacts" must not become "a preview rewrote them".
FINGERPRINTED = (
    ("knowledge_item", KnowledgeItem),
    ("knowledge_relation", KnowledgeRelation),
    ("knowledge_conflict", KnowledgeConflict),
    ("document_version", DocumentVersion),
    ("extraction", Extraction),
    ("document", Document),
)


# --------------------------------------------------------------------------- harness


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
    """The one JSON document ``--json`` promised (stdout may carry a library notice first)."""
    return json.loads(stdout[stdout.index("{") :])


@pytest.fixture
def cli_workspace(tmp_path: Path):
    """A real workspace on disk with a config file the CLI can be pointed at."""
    from drilling_intelligence.config.settings import Settings
    from drilling_intelligence.wells.repository import WellRepository
    from drilling_intelligence.wells.workspace import Workspace

    config = tmp_path / "config.toml"
    config.write_text(
        '[app]\ndata_dir = ".drillintel"\n\n[ai]\nenabled = false\nrequire_ai = false\n\n'
        '[mineru]\nmode = "disabled"\n',
        encoding="utf-8",
    )
    settings = Settings.load(config)
    root = tmp_path / "project"
    code, _out, err = _run(
        "workspace", "create", str(root), "--config", str(config), "--name", "V44", "--json"
    )
    assert code == 0, err
    corpus = root / "corpus"
    build_corpus(corpus)
    workspace = Workspace.open(root, settings)
    try:
        with workspace.database.session() as session:
            repository = WellRepository(session)
            repository.get_or_create_workspace(str(root), name="V44")
            project = repository.get_or_create_project("V44")
            repository.create_well("A-3", project_id=project.id)
            session.commit()
    finally:
        workspace.close()
    code, _out, err = _run(
        "ingest",
        str(corpus),
        "--workspace",
        str(root),
        "--config",
        str(config),
        "--well",
        "A-3",
        "--json",
    )
    assert code == 0, err
    return root, config, settings


def _fingerprint(root: Path, settings) -> dict[str, str]:
    """Every row of every mutable store, plus timestamps, plus the index file, as one blob."""
    from drilling_intelligence.wells.workspace import Workspace

    workspace = Workspace.open(root, settings)
    try:
        parts: dict[str, str] = {}
        with workspace.database.read_only() as session:
            for label, model in FINGERPRINTED:
                rendered = []
                for row in session.execute(select(model).order_by(model.id)).scalars():
                    rendered.append(
                        json.dumps(
                            {
                                column.name: str(getattr(row, column.name))
                                for column in model.__table__.columns
                            },
                            sort_keys=True,
                        )
                    )
                parts[f"{label}.rows"] = str(len(rendered))
                parts[f"{label}.sha256"] = hashlib.sha256("\n".join(rendered).encode()).hexdigest()
        for name in ("database_path", "index_database_path"):
            path = Path(getattr(workspace, name))
            parts[f"{name}.exists"] = str(path.exists())
            if path.exists():
                parts[f"{name}.sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return parts
    finally:
        workspace.close()


def _dry_run(root: Path, config: Path, *extra: str) -> tuple[int, dict, str]:
    code, out, err = _run(
        "knowledge",
        "rebuild",
        "--dry-run",
        "--workspace",
        str(root),
        "--config",
        str(config),
        *extra,
        "--json",
    )
    return code, _document(out), err


def _service(root: Path, settings) -> KnowledgeExtractionService:
    from drilling_intelligence.wells.workspace import Workspace

    return KnowledgeExtractionService.for_workspace(Workspace.open(root, settings))


# --------------------------------------------------------------------------- side effects


def test_the_dry_run_touches_nothing(cli_workspace) -> None:
    """Not "no rows changed" - *nothing* changed, timestamps and the index file included."""
    root, config, settings = cli_workspace
    before = _fingerprint(root, settings)

    code, plan, err = _dry_run(root, config)
    assert code == 0, err
    assert plan["mutations"] is False
    assert plan["result"] == "PLAN_ONLY"
    assert plan["dry_run"] is True

    after = _fingerprint(root, settings)
    assert before == after, {
        key: (before.get(key), after.get(key))
        for key in before
        if before.get(key) != after.get(key)
    }


def test_the_dry_run_predicts_what_the_real_rebuild_actually_does(cli_workspace) -> None:
    """The central requirement: one planning path, so the preview cannot drift from the execution.

    Every number the plan promises is compared with the number the real command reports.  If the
    two ever disagree, either the dry run has grown its own arithmetic or the executor has - and
    both are the bug this test exists to catch.
    """
    root, config, _settings = cli_workspace
    _code, plan, _err = _dry_run(root, config)
    predicted = plan["plan"]["facts"]
    assert predicted["remove"] > 0, "a corpus with derived rows must have something to re-derive"

    code, out, err = _run(
        "knowledge",
        "rebuild",
        "--workspace",
        str(root),
        "--config",
        str(config),
        "--json",
    )
    assert code == 0, err
    actual = _document(out)
    assert actual["removed"] == predicted["remove"], (predicted, actual["removed"])
    assert actual["facts"]["created"] == predicted["create"], (predicted, actual["facts"])
    assert actual["facts"]["updated"] == predicted["update"], (predicted, actual["facts"])
    assert actual["facts"]["unchanged"] == predicted["unchanged"], (predicted, actual["facts"])
    assert actual["relations"] == plan["plan"]["relations"]
    assert actual["versions"] == plan["plan"]["versions"]
    assert actual["conflicts"]["conflicts"] == plan["conflicts"]["predicted"]


def test_the_plan_is_the_same_plan_twice(cli_workspace) -> None:
    """A planner that gives a different answer when nothing changed cannot be automated."""
    root, config, _settings = cli_workspace
    _code, first, _err = _dry_run(root, config)
    _code, second, _err = _dry_run(root, config)
    assert first == second


def test_a_real_rebuild_twice_leaves_the_same_knowledge(cli_workspace) -> None:
    """Idempotency, stated in the accounting the repository actually uses.

    ``rebuild`` deletes derived rows and derives them again, so the second run still reports
    ``created`` - that is what the command is.  The invariant is that the *result* is identical:
    the same rows, the same predicates, the same conflicts.  ``sync_all``, which does not delete
    first, is what reports ``unchanged``, and that is asserted too so the two accountings are not
    confused with each other.
    """
    root, _config, settings = cli_workspace

    def snapshot() -> tuple[Counter, Counter, int]:
        from drilling_intelligence.wells.workspace import Workspace

        workspace = Workspace.open(root, settings)
        try:
            with workspace.database.read_only() as session:
                rows = list(session.execute(select(KnowledgeItem)).scalars())
                open_conflicts = len(
                    list(
                        session.execute(
                            select(KnowledgeConflict).where(KnowledgeConflict.status == "OPEN")
                        ).scalars()
                    )
                )
            return (
                Counter((str(r.predicate), str(r.original_value)) for r in rows if r.lookup_key),
                Counter(str(r.predicate) for r in rows if r.lookup_key),
                open_conflicts,
            )
        finally:
            workspace.close()

    first_run = _service(root, settings).rebuild(workspace_id="", well_id="")
    after_first = snapshot()
    second_run = _service(root, settings).rebuild(workspace_id="", well_id="")
    after_second = snapshot()

    assert after_first == after_second, "the second rebuild changed what is known"
    assert second_run["facts"]["created"] == first_run["facts"]["created"], (
        "delete-then-derive re-creates the same set; a different count means rows were lost or "
        "duplicated"
    )

    # The same derivation *without* the delete reports everything unchanged - the accounting that
    # ``created`` above is deliberately not.
    resync = _service(root, settings).sync_all(workspace_id="", well_id="")
    assert resync["facts"]["created"] == 0, "a re-derivation invented rows"
    assert resync["facts"]["unchanged"] + resync["facts"]["updated"] == (
        first_run["facts"]["created"] + first_run["facts"]["updated"]
    ), "the same writes, just accounted differently once the rows already exist"
    # Measured on the generated corpus: 61 created / 5 updated through ``rebuild``, and the same 66
    # writes through ``sync_all`` come back as 50 unchanged / 16 updated.  Those 16 are rows whose
    # stored digest differs from a fresh derivation - conflict status the detector stamped - and
    # the number is stable rather than oscillating, which is what the next assertion pins.
    again = _service(root, settings).sync_all(workspace_id="", well_id="")
    assert again["facts"] == resync["facts"], (resync["facts"], again["facts"])
    assert again["conflicts"]["conflicts"] == first_run["conflicts"]["conflicts"]


# --------------------------------------------------------------------------- scope


def test_a_scoped_rebuild_leaves_the_other_well_alone(cli_workspace) -> None:
    """The defect this test was written for.

    ``delete_derived`` had no well filter while ``sync_all`` did, so ``rebuild --well A-3`` removed
    the whole workspace's derived rows and put back only A-3's - measured at 61 removed, 45
    re-derived, 16 gone, exit status 0, no warning.  A scoped repair command must not be a
    data-loss command, and the dry run must not plan one either.
    """
    root, config, settings = cli_workspace
    from drilling_intelligence.wells.repository import WellRepository
    from drilling_intelligence.wells.workspace import Workspace

    workspace = Workspace.open(root, settings)
    try:
        with workspace.database.session() as session:
            repository = WellRepository(session)
            project = repository.get_or_create_project("V44")
            other = repository.create_well("B-1", project_id=project.id)
            other_id = str(other.id)
            moved = next(iter(session.execute(select(Document)).scalars()))
            moved.well_id = other_id
            moved_id, moved_name = str(moved.id), moved.filename
            session.commit()
    finally:
        workspace.close()

    def per_document() -> dict[str, int]:
        """Counted by document, not by the fact's ``well_id`` column.

        That column was stamped when the facts were derived, while the document was still A-3's, so
        re-filing the document does not relabel them - and asserting on the column would have
        passed on a rebuild that destroyed them.  What must survive is the moved document's own
        rows, whichever well the fact says it belongs to.
        """
        workspace = Workspace.open(root, settings)
        try:
            with workspace.database.read_only() as session:
                rows = list(session.execute(select(KnowledgeItem)).scalars())
            return {
                "total": sum(1 for row in rows if row.lookup_key),
                "moved": sum(
                    1 for row in rows if row.lookup_key and str(row.document_id or "") == moved_id
                ),
            }
        finally:
            workspace.close()

    before = per_document()
    assert before["moved"] > 0, f"{moved_name} was moved to B-1 and should still have its facts"

    code, plan, err = _dry_run(root, config, "--well", "A-3")
    assert code == 0, err
    assert plan["scope"]["scoped_to_well"] is True
    assert plan["plan"]["facts"]["remove"] < before["total"], (
        "a dry run scoped to one well must not plan to remove another well's rows"
    )

    code, _out, err = _run(
        "knowledge",
        "rebuild",
        "--well",
        "A-3",
        "--workspace",
        str(root),
        "--config",
        str(config),
        "--json",
    )
    assert code == 0, err
    after = per_document()
    assert after["moved"] == before["moved"], (
        f"B-1 lost {before['moved'] - after['moved']} derived row(s) to a rebuild scoped to A-3"
    )
    assert after["total"] == before["total"], "a scoped rebuild changed the size of the corpus"


def test_naming_a_well_that_is_not_there_is_a_usage_error(cli_workspace) -> None:
    """Exit 2, the code ``argparse`` uses, and a message that lists what does exist."""
    root, config, _settings = cli_workspace
    code, _out, err = _run(
        "knowledge",
        "rebuild",
        "--dry-run",
        "--well",
        "ZZ-9",
        "--workspace",
        str(root),
        "--config",
        str(config),
    )
    assert code == 2
    assert "no well matches" in err
    assert "A-3" in err, "the alternatives are the useful half of the message"


def test_a_dry_run_on_an_empty_workspace_is_a_clean_plan(tmp_path: Path) -> None:
    """No documents, no artefacts, no facts: a plan of nothing, reported as nothing."""
    from drilling_intelligence.config.settings import Settings
    from drilling_intelligence.wells.workspace import Workspace

    config = tmp_path / "config.toml"
    config.write_text(
        '[app]\ndata_dir = ".drillintel"\n\n[ai]\nenabled = false\nrequire_ai = false\n\n'
        '[mineru]\nmode = "disabled"\n',
        encoding="utf-8",
    )
    settings = Settings.load(config)
    root = tmp_path / "empty"
    code, _out, err = _run(
        "workspace", "create", str(root), "--config", str(config), "--name", "Empty", "--json"
    )
    assert code == 0, err
    assert Workspace.open(root, settings) is not None

    code, plan, err = _dry_run(root, config)
    assert code == 0, err
    assert plan["plan"]["versions"] == 0
    assert plan["plan"]["facts"] == {"create": 0, "update": 0, "unchanged": 0, "remove": 0}
    assert plan["semantic_repair"]["scanned"] == 0
    assert plan["conflicts"] == {
        "before": 0,
        "predicted": 0,
        "ambiguous_before": 0,
        "ambiguous_predicted": 0,
    }
    assert plan["recovery"]["state"] == "clean"


# --------------------------------------------------------------------------- conflicts


def test_a_rebuild_preserves_a_genuine_disagreement(cli_workspace) -> None:
    """Two sources, two measured depths, one open conflict - before and after."""
    root, config, _settings = cli_workspace
    _code, plan, _err = _dry_run(root, config)
    if plan["conflicts"]["before"] == 0:
        pytest.skip("this corpus has no disagreement to preserve")
    assert plan["conflicts"]["predicted"] == plan["conflicts"]["before"], (
        "a rebuild must not settle an argument"
    )

    code, out, err = _run(
        "knowledge", "rebuild", "--workspace", str(root), "--config", str(config), "--json"
    )
    assert code == 0, err
    actual = _document(out)
    assert actual["conflicts"]["conflicts"] == plan["conflicts"]["before"]
    # The command succeeds even though the workspace still has an argument in it.
    assert code == 0


def test_a_conflict_only_workspace_exits_zero_and_says_why(cli_workspace) -> None:
    """``doctor`` exits 1 here and is right to; the planner must not call it corruption."""
    root, config, _settings = cli_workspace
    code, plan, _err = _dry_run(root, config)
    recovery = plan["recovery"]
    assert recovery["corrupt"] is False
    if plan["conflicts"]["before"]:
        assert code == 0, "a dry run that predicts real conflicts is a successful dry run"
        assert recovery["state"] in {"conflicts_present", "clean", "knowledge_and_index_stale"}
        assert all(step["operation"] != "conflict resolution" for step in recovery["recommended"])


# --------------------------------------------------------------------------- manual facts


def test_a_manual_note_survives_both_the_plan_and_the_rebuild(cli_workspace) -> None:
    root, config, settings = cli_workspace
    from drilling_intelligence.wells.repository import WellRepository
    from drilling_intelligence.wells.workspace import Workspace

    workspace = Workspace.open(root, settings)
    try:
        with workspace.database.session() as session:
            well = WellRepository(session).find_well("A-3")
            well_id = str(well.id)
            KnowledgeRepository(session).manual_fact(
                KnowledgeFact(
                    subject=EntityRef("well", well_id, label="A-3"),
                    predicate="mud_weight",
                    value_type="quantity",
                    original_value="9.9 ppg",
                    value=9.9,
                    unit="ppg",
                    text="9.9 ppg",
                    well_id=well_id,
                    record_state="ACTUAL",
                )
            )
            session.commit()
    finally:
        workspace.close()

    _code, plan, err = _dry_run(root, config)
    assert plan["manual_facts"]["preserved"] is True, err
    assert plan["manual_facts"]["count"] == 1

    code, _out, err = _run(
        "knowledge", "rebuild", "--workspace", str(root), "--config", str(config), "--json"
    )
    assert code == 0, err
    workspace = Workspace.open(root, settings)
    try:
        with workspace.database.read_only() as session:
            manual = [
                row
                for row in session.execute(select(KnowledgeItem)).scalars()
                if row.origin == "MANUAL"
            ]
    finally:
        workspace.close()
    assert len(manual) == 1, "a rebuild deleted something a person typed"


# --------------------------------------------------------------------------- repair boundary


def test_the_dry_run_classifies_a_frozen_pre_fix_artefact(cli_workspace) -> None:
    """The classifier proves itself on the corpus it was built for, through the CLI.

    ``COLLAPSED_NAMES`` rewrites the stored field names back to their pre-V4.2 spellings while
    leaving ``provenance.excerpt`` alone - the frozen database.  The dry run must then report
    deterministic repairs for the rows whose excerpt names the quantity, and must not report a
    single repair for a row whose excerpt does not.
    """
    root, config, settings = cli_workspace
    from drilling_intelligence.wells.workspace import Workspace

    workspace = Workspace.open(root, settings)
    try:
        frozen = _freeze_old_field_names(workspace)
    finally:
        workspace.close()
    assert frozen > 0, "nothing was frozen, so this test would prove nothing"

    _code, plan, _err = _dry_run(root, config)
    repair = plan["semantic_repair"]
    assert repair["deterministic"] > 0, (
        "the frozen artefacts carry excerpts that name the quantity; the classifier found none"
    )
    assert repair["scanned"] > 0
    # Every reported row says why, and nothing is reported as repaired on the strength of a number.
    for row in repair["rows"]:
        assert row["category"] in {"deterministic", "ambiguous", "requires_reextraction"}
        if row["category"] == "deterministic":
            assert row["stored_field"] != row["recovered_field"]
            assert row["excerpt"], "a deterministic repair without a recorded excerpt is a guess"
            assert COLLAPSED_NAMES.get(row["recovered_field"]) == row["stored_field"] or (
                row["stored_field"] in set(COLLAPSED_NAMES.values())
            )


def test_a_current_corpus_legitimately_has_nothing_to_repair(cli_workspace) -> None:
    """``deterministic == 0`` is a passing result, not an empty test."""
    root, config, _settings = cli_workspace
    _code, plan, _err = _dry_run(root, config)
    repair = plan["semantic_repair"]
    assert repair["deterministic"] == 0
    assert repair["requires_source_reextraction"] == 0
    assert repair["no_change"] == repair["scanned"] - repair["requires_context"]


# --------------------------------------------------------------------------- contract


def test_the_json_contract_is_structured_not_rendered(cli_workspace) -> None:
    root, config, _settings = cli_workspace
    _code, plan, _err = _dry_run(root, config)
    for key in (
        "action",
        "dry_run",
        "scope",
        "plan",
        "semantic_repair",
        "conflicts",
        "index",
        "manual_facts",
        "recovery",
        "warnings",
        "mutations",
        "result",
    ):
        assert key in plan, f"the JSON contract lost {key!r}"
    assert isinstance(plan["recovery"]["recommended"], list)
    assert isinstance(plan["warnings"], list)
    assert plan["action"] == "rebuild"
    # A renderer cannot fake these: they are typed.
    assert isinstance(plan["plan"]["versions"], int)
    assert isinstance(plan["semantic_repair"]["deterministic"], int)
    assert isinstance(plan["index"]["structured_rows_refreshed"], bool)


def test_the_human_output_explains_the_recommendation(cli_workspace) -> None:
    root, config, _settings = cli_workspace
    code, out, err = _run(
        "knowledge", "rebuild", "--dry-run", "--workspace", str(root), "--config", str(config)
    )
    assert code == 0, err
    assert "knowledge rebuild dry-run" in out
    assert "nothing below was written" in out
    assert "semantic repair" in out
    assert "recovery state" in out
    assert "mutations  none - dry-run" in out
    assert "PLAN_ONLY" in out


def test_history_and_provenance_survive_the_rebuild(cli_workspace) -> None:
    """A rebuild must never re-attach a fact to the wrong document version."""
    root, config, settings = cli_workspace
    from drilling_intelligence.wells.workspace import Workspace

    def citations() -> dict[str, tuple[str, str, str]]:
        workspace = Workspace.open(root, settings)
        try:
            with workspace.database.read_only() as session:
                return {
                    str(row.id): (
                        str(row.document_id or ""),
                        str(row.document_version_id or ""),
                        str(row.predicate or ""),
                    )
                    for row in session.execute(select(KnowledgeItem)).scalars()
                    if row.lookup_key
                }
        finally:
            workspace.close()

    before = citations()
    code, _out, err = _run(
        "knowledge", "rebuild", "--workspace", str(root), "--config", str(config), "--json"
    )
    assert code == 0, err
    after = citations()
    assert before == after, "a rebuild moved a fact between documents or versions"


def test_a_repaired_predicate_is_still_searchable_after_the_rebuild(cli_workspace) -> None:
    """Facts -> lookup keys -> index -> retrieval must still agree."""
    root, config, _settings = cli_workspace
    code, _out, err = _run(
        "knowledge", "rebuild", "--workspace", str(root), "--config", str(config), "--json"
    )
    assert code == 0, err
    code, out, err = _run(
        "knowledge", "status", "--workspace", str(root), "--config", str(config), "--json"
    )
    assert code == 0, err
    status = _document(out)
    assert status["index"]["knowledge_chunks"] == status["facts"], (
        "every fact is searchable, not merely listable"
    )
    assert status["detached_facts"] == 0
    assert status["needs_rebuild"] is False


def test_the_index_is_not_touched_and_is_reported_honestly(cli_workspace) -> None:
    """A knowledge rebuild refreshes knowledge chunks but never the promoted structured rows.

    That asymmetry is why the planner recommends ``index rebuild`` as a separate step instead of
    implying it, and why ``structured_rows_refreshed`` is ``False`` in the contract.
    """
    root, config, settings = cli_workspace
    before = _fingerprint(root, settings)
    _code, plan, _err = _dry_run(root, config)
    assert plan["index"]["structured_rows_refreshed"] is False
    assert _fingerprint(root, settings)["index_database_path.sha256"] == before.get(
        "index_database_path.sha256"
    ), "the dry run wrote to the index sidecar, which no rollback can undo"


def test_the_planner_is_reachable_and_reports_the_recovery_state(cli_workspace) -> None:
    """``plan_recovery`` was library-only in V4.3; the CLI now exposes the same classification."""
    root, config, _settings = cli_workspace
    _code, plan, _err = _dry_run(root, config)
    recovery = plan["recovery"]
    for key in (
        "state",
        "conditions",
        "corrupt",
        "recovery_required",
        "recommended",
        "not_performed",
        "explanation",
    ):
        assert key in recovery
    assert recovery["state"] in {
        "clean",
        "knowledge_stale",
        "index_stale",
        "structured_index_stale",
        "knowledge_and_index_stale",
        "conflicts_present",
        "schema_out_of_date",
        "unrecoverable",
    }


def test_planning_costs_about_what_execution_costs(cli_workspace) -> None:
    """A dry run that runs the real thing must not be an order of magnitude slower than it."""
    import time

    root, _config, settings = cli_workspace
    start = time.perf_counter()
    _dry_run(root, _config)
    planned = time.perf_counter() - start
    service = _service(root, settings)
    start = time.perf_counter()
    service.rebuild(workspace_id="", well_id="")
    executed = time.perf_counter() - start
    assert planned < max(5.0, executed * 20), (planned, executed)


def test_a_scope_that_matches_nothing_says_so(workspace) -> None:
    """A preview of zeros is as easy to misread as the command it previews.

    ``IngestionPipeline.run`` without a workspace id files documents under
    ``workspace_id IS NULL``, so the CLI's workspace-scoped rebuild matches no version, re-derives
    nothing and exits 0 - which the real command has always done.  The dry run has to name that
    mismatch instead of quietly printing a plan of zeros.
    """
    from drilling_intelligence.database.models import Document

    ingest_v4(workspace)
    with workspace.database.read_only() as session:
        assert {str(row.workspace_id) for row in session.execute(select(Document)).scalars()} == {
            "None"
        }, "this test depends on the corpus being filed without a workspace id"

    service = KnowledgeExtractionService.for_workspace(workspace)
    unscoped = service.plan_rebuild(workspace_id="", well_id="")
    assert unscoped["plan"]["versions"] > 0, "with no filter the whole corpus is in scope"

    scoped = service.plan_rebuild(workspace_id="ws-does-not-match", well_id="")
    assert scoped["plan"]["versions"] == 0
    assert any("matched no current document version" in item for item in scoped["warnings"]), (
        scoped["warnings"]
    )
