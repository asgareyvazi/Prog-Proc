"""The V4 domains on every read surface: search, retrieval evidence, review, doctor, CLI.

A domain that can be written but not read is not finished.  These tests promote the V4 forensic corpus
once and then check that each surface reports the same assembly, the same bit runs and the same survey -
including the parts of the record that come from the child tables, which are the parts most likely to be
quietly dropped by a read path written before the domain existed.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from tests.fixtures.fieldops import fetch, register_wells, well_id_for
from tests.fixtures.generate import build_v4_forensic_corpus

from drilling_intelligence.database.integrity import (
    check_domain_identities,
    check_operational_integrity,
    describe_problems,
)
from drilling_intelligence.database.models import (
    BhaComponent,
    BhaReport,
    BitRecord,
    Document,
    SurveyRun,
    SurveyStation,
)
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.operations.service import OperationalService
from drilling_intelligence.review.contract import DomainReviewRequest
from drilling_intelligence.review.service import DomainReviewService
from drilling_intelligence.search.structured import structured_records


def _v4_workspace(workspace):
    register_wells(workspace)
    corpus = workspace.root / "corpus"
    build_v4_forensic_corpus(corpus)
    result = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    ).run(root=corpus, well_id=well_id_for(workspace, "A-3"))
    assert result.ok, result.error
    summary = OperationalService.for_workspace(workspace).promote_workspace(
        include_unsupported=True
    )
    assert summary["outcomes"]["error"] == 0, summary["outcomes"]
    assert summary["counts"]["bha_component"]["created"] == 6
    assert summary["counts"]["bit_record"]["created"] == 2
    assert summary["counts"]["survey_station"]["created"] == 5
    return workspace


def _by_type(workspace) -> dict[str, list]:
    with workspace.database.read_only() as session:
        records = structured_records(session)
    grouped: dict[str, list] = {}
    for record in records:
        grouped.setdefault(record.record_type, []).append(record)
    return grouped


# ---------------------------------------------------------------- search projection
def test_the_structured_projection_carries_the_three_v4_record_types(workspace) -> None:
    _v4_workspace(workspace)
    by_type = _by_type(workspace)
    assert {"bha_report", "bit_record", "survey_run"} <= set(by_type), sorted(by_type)
    assert len(by_type["bha_report"]) == 1
    assert len(by_type["bit_record"]) == 2
    assert len(by_type["survey_run"]) == 1

    bha = by_type["bha_report"][0]
    assert "Drill collar 6-1/4 x 2-13/16" in bha.text, bha.text
    assert "SN-1041" in bha.text
    assert bha.provenance, "a projected record must carry the evidence it was built from"
    assert len(bha.provenance["component_evidence"]) == 6
    assert bha.well_id == well_id_for(workspace, "A-3")

    survey = by_type["survey_run"][0]
    assert len(survey.provenance["station_evidence"]) == 5
    assert "9500" in survey.text, survey.text

    bits = sorted(by_type["bit_record"], key=lambda row: row.text)
    assert any("TD - casing point" in row.text for row in bits)
    assert any("WT-1-NO-X-I-NO" in row.text for row in bits)


def test_the_projection_is_scoped_to_the_well_that_owns_the_record(workspace) -> None:
    _v4_workspace(workspace)
    a3 = well_id_for(workspace, "A-3")
    for record_type, records in _by_type(workspace).items():
        if record_type in {"bha_report", "bit_record", "survey_run"}:
            for record in records:
                assert record.well_id == a3, (record_type, record.well_id)
                assert record.well_name == "A-3", record.well_name


def test_a_superseded_assembly_is_not_projected(workspace) -> None:
    """The index is built from what the domain considers current, not from every row that exists."""
    from openpyxl import load_workbook

    _v4_workspace(workspace)
    source = workspace.root / "corpus" / "bha_tally_well-a3.xlsx"
    workbook = load_workbook(source)
    workbook["Summary"]["B5"] = "15"
    workbook.save(source)
    IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    ).run(root=workspace.root / "corpus", force=True)
    OperationalService.for_workspace(workspace).promote_workspace()

    with workspace.database.read_only() as session:
        assert len(list(session.scalars(select(BhaReport)))) == 2
    projected = _by_type(workspace)["bha_report"]
    assert len(projected) == 1, "a superseded assembly must not be searchable as the current one"
    assert "15" in projected[0].text


# ---------------------------------------------------------------- retrieval evidence
def test_retrieval_returns_the_assembly_with_its_component_evidence(workspace) -> None:
    from drilling_intelligence.retrieval.contract import RetrievalRequest
    from drilling_intelligence.retrieval.service import RetrievalService
    from drilling_intelligence.search.service import SearchService

    _v4_workspace(workspace)
    # Retrieval discovers candidates through the index and then re-reads them from the database, so
    # the disposable sidecar has to exist before a question can be answered at all.
    SearchService.for_workspace(workspace).rebuild()
    bundle = RetrievalService.for_workspace(workspace).retrieve(
        RetrievalRequest(
            query="drill collar stabilizer bottom hole assembly",
            well_id=well_id_for(workspace, "A-3"),
            limit=20,
        )
    )
    bha = next(item for item in bundle.items if item.record_type == "bha_report")
    assert bha.current is True
    assert "Drill collar 6-1/4 x 2-13/16" in bha.text
    assert bha.provenance, "a retrieved record must cite where it came from"
    assert bha.well_name == "A-3"


def test_retrieval_carries_the_bit_run_and_the_survey_stations(workspace) -> None:
    from drilling_intelligence.retrieval.contract import RetrievalRequest
    from drilling_intelligence.retrieval.service import RetrievalService
    from drilling_intelligence.search.service import SearchService

    _v4_workspace(workspace)
    SearchService.for_workspace(workspace).rebuild()
    service = RetrievalService.for_workspace(workspace)
    well_id = well_id_for(workspace, "A-3")

    bits = service.retrieve(
        RetrievalRequest(query="bit footage rotating hours dull grade", well_id=well_id, limit=20)
    )
    bit_items = [item for item in bits.items if item.record_type == "bit_record"]
    assert bit_items, [item.record_type for item in bits.items]
    bit_text = "\n".join(item.text for item in bit_items)
    # Both runs are retrievable, each with the pull reason and dull grade its own source stated.
    assert "TD - casing point" in bit_text
    assert "WT-1-NO-X-I-NO" in bit_text
    assert "Change BHA for MWD" in bit_text

    surveys = service.retrieve(
        RetrievalRequest(query="inclination azimuth measured depth", well_id=well_id, limit=20)
    )
    survey = next(item for item in surveys.items if item.record_type == "survey_run")
    # Station detail is folded in, so the retrieved record answers "what did the survey say at 9500".
    assert "9500" in survey.text


def test_retrieval_returns_nothing_for_a_well_that_has_no_v4_records(workspace) -> None:
    """Scoping is real: another well's question does not surface this well's assembly."""
    from drilling_intelligence.retrieval.contract import RetrievalRequest
    from drilling_intelligence.retrieval.service import RetrievalService
    from drilling_intelligence.search.service import SearchService

    _v4_workspace(workspace)
    SearchService.for_workspace(workspace).rebuild()
    bundle = RetrievalService.for_workspace(workspace).retrieve(
        RetrievalRequest(
            query="drill collar stabilizer assembly",
            well_id=well_id_for(workspace, "B-11"),
            limit=20,
        )
    )
    assert not [
        item
        for item in bundle.items
        if item.record_type in {"bha_report", "bit_record", "survey_run"}
    ], [item.record_type for item in bundle.items]


# ---------------------------------------------------------------- review
def test_the_domain_review_lists_the_v4_domains_with_their_children(workspace) -> None:
    _v4_workspace(workspace)
    review = DomainReviewService.for_workspace(workspace).review(
        DomainReviewRequest(well_id=well_id_for(workspace, "A-3"))
    )
    by_type: dict[str, list] = {}
    for record in review.records:
        by_type.setdefault(record.record_type, []).append(record)
    assert {"bha_report", "bit_record", "survey_run"} <= set(by_type), sorted(by_type)

    bha = by_type["bha_report"][0]
    assert bha.current is True
    components = bha.data["components"]
    assert len(components) == 6
    assert [component["sequence"] for component in components] == [1, 2, 3, 4, 5, 6]
    assert bha.provenance, "a reviewed record must show where its numbers came from"
    # Every component carries its own citation, so a reviewer can follow any one of them back to the
    # row of the tally it came from.  The parent's merged list is deduplicated - six components read
    # from one table cite one table - which is why the count is checked on the components themselves.
    assert all(component["provenance"] for component in components)
    assert {component["source_label"] for component in components} == {
        "Drill collar 6-1/4 x 2-13/16",
        "Stabilizer 6-1/2 in",
        "PDM 5 in",
        "MWD tool",
        "Drilling jar",
        "Heavy weight drill pipe 5 in",
    }

    survey = by_type["survey_run"][0]
    assert len(survey.data["stations"]) == 5
    assert [station["sequence"] for station in survey.data["stations"]] == [1, 2, 3, 4, 5]

    assert len(by_type["bit_record"]) == 2
    assert all("components" not in record.data for record in by_type["bit_record"])


def test_the_review_history_view_adds_the_superseded_assembly(workspace) -> None:
    from openpyxl import load_workbook

    from drilling_intelligence.review.contract import REVIEW_HISTORY

    _v4_workspace(workspace)
    source = workspace.root / "corpus" / "bha_tally_well-a3.xlsx"
    workbook = load_workbook(source)
    workbook["Summary"]["B5"] = "15"
    workbook.save(source)
    IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    ).run(root=workspace.root / "corpus", force=True)
    OperationalService.for_workspace(workspace).promote_workspace()

    service = DomainReviewService.for_workspace(workspace)
    well_id = well_id_for(workspace, "A-3")
    current = service.review(DomainReviewRequest(well_id=well_id))
    history = service.review(DomainReviewRequest(well_id=well_id, lifecycle=REVIEW_HISTORY))

    def assemblies(review):
        return [record for record in review.records if record.record_type == "bha_report"]

    assert len(assemblies(current)) == 1
    assert len(assemblies(history)) == 2, "history must add the superseded run, not replace it"
    assert sum(1 for record in assemblies(history) if record.current) == 1


def test_a_confirmed_component_is_visible_as_confirmed_in_the_review(workspace) -> None:
    _v4_workspace(workspace)
    with workspace.database.read_only() as session:
        component_id = str(session.scalar(select(BhaComponent.id)))
    outcome = OperationalService.for_workspace(workspace).set_status(
        "bha_component", component_id, "CONFIRMED", by="bha.engineer"
    )
    assert outcome["status"] == "CONFIRMED"

    review = DomainReviewService.for_workspace(workspace).review(
        DomainReviewRequest(well_id=well_id_for(workspace, "A-3"))
    )
    # Components are reviewed as part of the assembly they belong to rather than as separate records,
    # so the confirmation is visible on the component inside its parent's review record.
    parent = next(record for record in review.records if record.record_type == "bha_report")
    matching = [
        component
        for component in parent.data["components"]
        if component["id"] == component_id
    ]
    assert len(matching) == 1
    assert matching[0]["status"] == "CONFIRMED"


# ---------------------------------------------------------------- integrity / doctor
def test_a_promoted_corpus_passes_the_domain_invariant_checks(workspace) -> None:
    _v4_workspace(workspace)
    with workspace.database.read_only() as session:
        assert check_domain_identities(session) == []
        assert check_operational_integrity(session) == []


def test_an_orphaned_component_is_reported(workspace) -> None:
    """A component whose assembly is gone.

    SQLite enforces the foreign key when it is told to, so this state cannot be reached through the
    ORM - it is what a hand-repaired or partially restored database looks like, which is precisely the
    file ``doctor`` is run against.  Enforcement is switched off for one statement to build it.
    """
    from sqlalchemy import text

    _v4_workspace(workspace)
    with workspace.database.read_only() as session:
        component_id = str(session.scalar(select(BhaComponent.id)))
    with workspace.database.engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys = OFF"))
        connection.execute(
            text("update bha_component set bha_report_id = :missing where id = :id"),
            {"missing": "bha-report-that-does-not-exist", "id": component_id},
        )
        connection.execute(text("PRAGMA foreign_keys = ON"))

    with workspace.database.read_only() as session:
        problems = check_operational_integrity(session)
    assert any(
        problem.table == "bha_component" and "does not exist" in problem.problem
        for problem in problems
    ), describe_problems(problems)


def test_a_component_count_that_does_not_match_the_string_is_reported(workspace) -> None:
    _v4_workspace(workspace)
    with workspace.database.read_only() as session:
        report_id = str(session.scalar(select(BhaReport.id)))
    with workspace.database.session() as session:
        session.get(BhaReport, report_id).component_count = 4
        session.commit()

    with workspace.database.read_only() as session:
        problems = check_domain_identities(session)
    assert any("claims 4 children and owns 6" in problem.problem for problem in problems), (
        describe_problems(problems)
    )


def test_a_station_count_that_does_not_match_the_set_is_reported(workspace) -> None:
    _v4_workspace(workspace)
    with workspace.database.read_only() as session:
        run_id = str(session.scalar(select(SurveyRun.id)))
    with workspace.database.session() as session:
        session.get(SurveyRun, run_id).station_count = 3
        session.commit()

    with workspace.database.read_only() as session:
        problems = check_domain_identities(session)
    assert any("claims 3 children and owns 5" in problem.problem for problem in problems), (
        describe_problems(problems)
    )


def test_two_current_assemblies_for_one_version_is_reported(workspace) -> None:
    _v4_workspace(workspace)
    with workspace.database.read_only() as session:
        report = session.scalar(select(BhaReport))
        version_id, well_id, document_id = (
            str(report.document_version_id),
            str(report.well_id),
            str(report.document_id),
        )
    with workspace.database.session() as session:
        session.add(
            BhaReport(
                id="bha-report-twin",
                well_id=well_id,
                document_id=document_id,
                document_version_id=version_id,
                bha_number="14",
                component_count=0,
                identity_key="promote:twin-identity-key",
                status="CANDIDATE",
                origin="DERIVED",
                is_current=True,
            )
        )
        session.commit()

    with workspace.database.read_only() as session:
        problems = check_domain_identities(session)
    assert any(
        "more than one current row from the same document version" in problem.problem
        for problem in problems
    ), (
        describe_problems(problems)
    )


def test_two_rows_cannot_share_one_identity_in_a_migrated_database(workspace) -> None:
    """The schema refuses the state ``DUPLICATE_IDENTITY`` reports, so the check is a backstop.

    ``uq_survey_station_identity`` makes a repeated identity impossible on a file this migration
    built.  The integrity check still looks for it, because a database assembled by some other path -
    a partial restore, a hand-written schema - may not have the constraint, and the failure mode it
    describes (two rows claiming to be the same station) is the one that silently doubles a count.
    What is asserted here is the guarantee that actually holds in production.
    """
    from sqlalchemy.exc import IntegrityError

    _v4_workspace(workspace)
    with workspace.database.read_only() as session:
        station = session.scalar(select(SurveyStation))
    with pytest.raises(IntegrityError), workspace.database.session() as session:
        session.add(
            SurveyStation(
                id="survey-station-twin",
                well_id=str(station.well_id),
                survey_run_id=str(station.survey_run_id),
                document_version_id=str(station.document_version_id),
                sequence=99,
                station_number_text="1",
                identity_key=str(station.identity_key),
                origin="DERIVED",
                status="CANDIDATE",
                is_current=True,
            )
        )
        session.commit()
    with workspace.database.read_only() as session:
        assert check_domain_identities(session) == []


# ---------------------------------------------------------------- CLI
def test_the_cli_lists_the_v4_tables(workspace, capsys) -> None:
    from drilling_intelligence.cli.app import build_parser

    _v4_workspace(workspace)
    parser = build_parser()
    for table in ("bha", "bit", "survey"):
        args = parser.parse_args(
            [
                "records",
                "list",
                "--workspace",
                str(workspace.root),
                "--table",
                table,
                "--well",
                "A-3",
            ]
        )
        assert args.handler(args) == 0
    out = capsys.readouterr().out
    assert "1 bha row(s)" in out, out
    assert "2 bit row(s)" in out, out
    assert "1 survey row(s)" in out, out
    # The listed columns are the ones a reader triages on: the run's identity, its interval and
    # whether it is the current statement.
    assert "bha_number" in out and "component_count" in out, out
    assert "bit_number" in out and "pull_reason" in out, out
    assert "station_count" in out and "station_identity" in out, out
    assert "14" in out and "TD - casing point" in out and "NUMBERED" in out, out


def test_the_cli_refuses_a_table_it_does_not_know(workspace) -> None:
    """The table vocabulary is closed at the argument parser, so a typo cannot reach the repository.

    ``trajectory`` is the interesting refusal: it is a word this domain uses constantly, and a CLI that
    accepted it and returned nothing would read as "this well has no surveys" rather than as a mistake.
    """
    from drilling_intelligence.cli.app import build_parser

    _v4_workspace(workspace)
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["records", "list", "--workspace", str(workspace.root), "--table", "trajectory"]
        )
    # And the three V4 tables are in the vocabulary it does accept.
    for table in ("bha", "bit", "survey"):
        args = parser.parse_args(
            ["records", "list", "--workspace", str(workspace.root), "--table", table]
        )
        assert args.table == table


def test_the_operational_summary_reports_all_three_new_domains(workspace) -> None:
    _v4_workspace(workspace)
    summary = OperationalService.for_workspace(workspace).report()
    assert summary["bha_reports"] == 1
    assert summary["bit_records"] == 2
    assert summary["survey_runs"] == 1
    assert summary["bha"] == {"reports": 1, "components": 6}
    # Bit 13 names BHA 14, which this corpus promotes; bit 12 names BHA 13, which nothing states, so
    # its link stays NULL rather than being matched to the assembly that happens to exist.
    assert summary["bit"] == {"runs": 2, "linked_to_bha": 1}
    assert summary["survey"] == {"runs": 1, "stations": 5}


def test_the_v4_domain_is_absent_from_a_workspace_without_the_tables(workspace) -> None:
    """``report`` gates on the schema, so an unmigrated workspace is not an error."""
    from sqlalchemy import inspect as sa_inspect

    from drilling_intelligence.operations import repository as repository_module

    def without_v4(bind):
        inspector = sa_inspect(bind)

        class _Inspector:
            def __getattr__(self, name):
                return getattr(inspector, name)

            def has_table(self, table_name, *args, **kwargs):
                return table_name != "bha_report"

        return _Inspector()

    original = repository_module.inspect
    repository_module.inspect = without_v4
    try:
        summary = OperationalService.for_workspace(workspace).report()
    finally:
        repository_module.inspect = original
    assert "bha" not in summary
    assert "bha_reports" not in summary
    assert summary["reports"] == 0


def test_the_v4_writers_leave_the_document_registry_intact(workspace) -> None:
    _v4_workspace(workspace)
    with workspace.database.read_only() as session:
        documents = list(session.scalars(select(Document)))
    assert len(documents) == 14
    # The two shape refusals keep their artefact and their version: refusing to promote is not
    # refusing to ingest.
    for name in ("bha_report_well-a3.txt", "bit_record_well-a3.txt"):
        document = next(row for row in documents if row.filename == name)
        assert document.current_version_id, name
    assert len(fetch(workspace, BhaReport)) == 1
    assert len(fetch(workspace, SurveyRun)) == 1
    assert len(fetch(workspace, BitRecord)) == 2


def test_doctor_names_the_v4_rows_the_index_has_not_seen(workspace, capsys) -> None:
    """Promoted V4 rows are structured records, so an unbuilt index is a finding, not a silence.

    The existing doctor test proves the finding fires for a lesson.  V4 added five more tables to
    ``STRUCTURED_RECORD_TYPES``, and a counter that silently ignored them would report a clean index
    over a workspace whose assemblies, bit runs and stations no search could reach.  Promotion does
    not build the index - that is a separate, deliberate act - so this is the expected state of a
    freshly promoted workspace, and doctor has to say so and exit non-zero.
    """
    from drilling_intelligence.cli.app import build_parser

    def doctor() -> tuple[int, dict]:
        """Run ``doctor --json`` and read its payload.

        ``--json`` writes the document to stdout, but a library may also print a deprecation notice
        there first.  The payload is parsed from the first brace rather than from the whole buffer,
        because a warning is not a reason for the command's own contract to look broken.
        """
        parsed = parser.parse_args(["doctor", "--workspace", str(workspace.root), "--json"])
        code = parsed.handler(parsed)
        text = capsys.readouterr().out
        payload, _end = json.JSONDecoder().raw_decode(text[text.index("{") :])
        return code, payload

    _v4_workspace(workspace)
    parser = build_parser()
    code, payload = doctor()

    assert code == 1, payload
    # The promoted corpus holds exactly 18 structured rows: 5 npt_record, 3 problem_occurrence,
    # 3 well_event, 2 problem_definition, 2 bit_record, and 1 each of bha_report, survey_run and
    # mud_report.  The exact number is asserted because a V4 *parent* silently dropped from
    # ``STRUCTURED_RECORD_TYPES`` would still leave this greater than zero, and would read as "the
    # index is merely behind" rather than as "this domain is unsearchable".
    #
    # ``bha_component`` and ``survey_station`` are deliberately absent from that list: children are
    # reached through their parent, which is the same rule review follows.  If a child table is ever
    # added to the index, this number moves and the reason has to be stated here.
    assert payload["index"]["structured_missing"] == 18, payload["index"]
    finding = [line for line in payload["findings"] if "structured row(s)" in line]
    assert finding, payload["findings"]
    assert "index rebuild" in finding[0], finding[0]

    # Rebuilding is what closes *this* finding, so it describes a real gap rather than a counter that
    # can never be satisfied.  ``doctor`` still exits 1 afterwards - this corpus carries unresolved
    # knowledge conflicts, which is a different finding about a different layer and is none of the
    # index's business.  What is asserted is that the structured-index finding specifically is gone.
    rebuild = parser.parse_args(["index", "rebuild", "--workspace", str(workspace.root)])
    assert rebuild.handler(rebuild) == 0
    capsys.readouterr()
    after_code, after_payload = doctor()
    assert after_payload["index"]["structured_missing"] == 0, after_payload["index"]
    assert not [line for line in after_payload["findings"] if "structured row(s)" in line], (
        after_payload["findings"]
    )
    assert after_code == 1, after_payload
    assert after_payload["knowledge"]["open_conflicts"] > 0, after_payload["knowledge"]
