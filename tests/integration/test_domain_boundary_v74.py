"""V7.4: the deterministic boundary for LOGGING and SERVICE_REPORT.

Both classifications are ``KNOWLEDGE_SUPPORTED`` with no domain writer, and this file is what keeps
that true.  It is not a placeholder for a future feature; it is the executable form of the rule that
absence of evidence must never become invented operational knowledge.

The two sources under test are the shapes the repository actually holds:

* ``service_report_well-a3.txt`` is a real fixture in the V4 golden corpus.  Its entire content is
  two sentences.  It names no company, no job identity, no date, no equipment, no person, no result
  and no status - it states only that a service was provided.  Promoting it would mean inventing
  every field the domain needs.
* The logging candidates are built here because the repository holds no logging source at all.  They
  carry curve vocabulary, which is what the classifier looks for, and deliberately no run identity,
  no channel structure and no depth axis.

The assertion is the same in every case: the document is ingested, classified, stored with full
provenance and remains searchable as *knowledge* - and no operational row is written anywhere.
"""

from __future__ import annotations

from pathlib import Path

from tests.fixtures.fieldops import document_id_for, register_wells

from drilling_intelligence.classification.rules import DeterministicClassifier
from drilling_intelligence.database.models import Base
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.operations.contracts import PROMOTION_CONTRACTS
from drilling_intelligence.operations.service import OperationalService

#: Curve vocabulary with no run identity, no channel column and no depth axis.
LOGGING_VOCABULARY_ONLY = (
    "Wireline measurement appendix.\n"
    "Gamma Ray values tabulated below. Resistivity tabulated. Sonic recorded.\n"
)

#: A petrophysical *interpretation*: what the curves mean, not a record of acquiring them.
INTERPRETATION_ONLY = (
    "Petrophysical interpretation summary.\n"
    "Porosity 18 percent, SW 0.35, VSH 0.12. Net pay identified across the interval.\n"
)

#: The exact content of the repository's own golden-corpus service fixture.
SERVICE_TWO_LINES = (
    "Service report.\n"
    "Service provided: mud logging support; equipment used and personnel engineer crew.\n"
)

#: A company name with a date and nothing else. Still not a job.
COMPANY_AND_DATE_ONLY = "Halliburton. 2026-04-12.\n"


def _ingest(workspace, files: dict[str, str], *, well: str = "A-3") -> None:
    """Write the sources and run them through the real ingestion pipeline."""
    hierarchy = register_wells(workspace)
    root = workspace.root / "corpus"
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        Path(root / name).write_text(text, encoding="utf-8")
    pipeline = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    )
    result = pipeline.run(root=root, well_id=str(hierarchy["wells"][well].id))
    assert result.ok, result.error
    assert result.failures == 0, [item.error for item in result.failures_report()]


def _row_counts(workspace) -> dict[str, int]:
    """Every mapped table's row count, so "nothing was written" is checked against all of them."""
    from sqlalchemy import func, select

    with workspace.database.read_only() as session:
        return {
            mapper.class_.__tablename__: int(
                session.scalar(select(func.count()).select_from(mapper.class_)) or 0
            )
            for mapper in Base.registry.mappers
        }


#: Tables that legitimately hold hierarchy, provenance and knowledge - the ones a document always
#: populates. Everything else is an operational or engineering domain row.
_EXPECTED_NONZERO = {
    "company",
    "project",
    "field",
    "well",
    "document",
    "document_version",
    "source",
    "extraction",
    "search_unit",
    "knowledge_item",
    "alembic_version",
    # Platform bookkeeping that any ingestion writes, whatever it finds.  None of these is an
    # operational or engineering domain row - they record that a run happened, not what it said.
    "audit_event",
    "extraction_cache",
    "ingestion_run",
    "knowledge_relation",
    "workspace",
}


def _assert_no_domain_rows(workspace, before: dict[str, int]) -> list[str]:
    after = _row_counts(workspace)
    grown = sorted(
        name
        for name, count in after.items()
        if count > before.get(name, 0) and name not in _EXPECTED_NONZERO
    )
    assert not grown, f"vocabulary alone wrote domain rows into: {grown}"
    return grown


# --------------------------------------------------------------------- the contracts say so


def test_logging_and_service_report_have_no_domain_writer() -> None:
    """The contract level is the authority; this pins it against a future silent upgrade."""
    from drilling_intelligence.operations.contracts import CoverageLevel

    for name in ("LOGGING", "SERVICE_REPORT"):
        contract = PROMOTION_CONTRACTS[name]
        assert contract.level is CoverageLevel.KNOWLEDGE_SUPPORTED, name
        assert not contract.target_models, f"{name} must not name a domain model"
        assert not contract.handler, f"{name} must not register a domain writer"


# --------------------------------------------------------------------- vocabulary-only logging


def test_curve_vocabulary_with_no_run_identity_writes_no_row(workspace) -> None:
    before = _row_counts(workspace)
    _ingest(workspace, {"wireline_appendix_well-a3.txt": LOGGING_VOCABULARY_ONLY})
    _assert_no_domain_rows(workspace, before)

    # It is still real, provenance-carrying knowledge - refused promotion is not rejection.
    assert document_id_for(workspace, "wireline_appendix_well-a3.txt")


def test_a_petrophysical_interpretation_is_not_filed_as_an_acquisition_record(workspace) -> None:
    """Porosity, SW and VSH are conclusions this platform refuses to derive.

    They were weighting the LOGGING signature, so an interpretation report - the document that says
    what the curves mean - outscored a genuine acquisition report.  They no longer do.
    """
    engine = DeterministicClassifier()
    interpretation = engine.classify(
        filename="formation_evaluation_well-a3.pdf",
        text=INTERPRETATION_ONLY,
        extension="pdf",
    )
    acquisition = engine.classify(
        filename="wireline_log_run_well-a3.pdf",
        text="Wireline log run 7. Gamma Ray and Resistivity acquired. Sonic pass recorded.",
        extension="pdf",
    )
    assert acquisition.confidence > interpretation.confidence, (
        "an acquisition report must outrank an interpretation report "
        f"({acquisition.confidence} vs {interpretation.confidence})"
    )


def test_a_genuine_acquisition_report_still_classifies(workspace) -> None:
    """Tightening the signature must not make the real thing unrecognisable."""
    engine = DeterministicClassifier()
    result = engine.classify(
        filename="wireline_log_run_well-a3.pdf",
        text="Wireline log run. Gamma Ray and Resistivity acquired over the interval. Sonic recorded.",
        extension="pdf",
    )
    assert result.classification.name == "LOGGING", result.classification.name


# --------------------------------------------------------------------- service vocabulary


def test_the_golden_corpus_service_fixture_writes_no_row(workspace) -> None:
    """The repository's own service source states that a service happened, and nothing else."""
    before = _row_counts(workspace)
    _ingest(workspace, {"service_report_well-a3.txt": SERVICE_TWO_LINES})
    _assert_no_domain_rows(workspace, before)
    assert document_id_for(workspace, "service_report_well-a3.txt")


def test_a_company_name_with_a_date_is_still_not_a_service_job(workspace) -> None:
    before = _row_counts(workspace)
    _ingest(workspace, {"vendor_note_well-a3.txt": COMPANY_AND_DATE_ONLY})
    _assert_no_domain_rows(workspace, before)


def test_promotion_of_these_documents_reports_no_rows_rather_than_inventing_them(workspace) -> None:
    """Running the real promotion pass must return zero rows, not a partially-guessed record."""
    _ingest(
        workspace,
        {
            "wireline_appendix_well-a3.txt": LOGGING_VOCABULARY_ONLY,
            "service_report_well-a3.txt": SERVICE_TWO_LINES,
        },
    )
    service = OperationalService.for_workspace(workspace)
    for filename in ("wireline_appendix_well-a3.txt", "service_report_well-a3.txt"):
        result = service.promote(document_id=document_id_for(workspace, filename))
        written = sum(
            value for value in getattr(result, "counts", {}).values() if isinstance(value, int)
        )
        assert written == 0, (
            f"{filename} promoted {written} row(s): {getattr(result, 'counts', {})}"
        )
