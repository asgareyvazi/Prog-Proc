"""HSE certification: a well incident, a site incident, and everything the contract refuses.

The interesting half of this file is the site register.  An incident at a camp or a laydown area has
no well, and the platform must not invent one - not from the document's location, not from the
project it happens to belong to.  These tests prove the row keeps ``well_id`` NULL, stays findable
by the things it *does* name, and never becomes a WellEvent to fit an existing table.
"""

from __future__ import annotations

from tests.fixtures.fieldops import fetch, ingest_v72, promote_file, reingest

from drilling_intelligence.core.enums import CauseStatus, DocumentClassification, RecordState
from drilling_intelligence.database.models import (
    HseIncident,
    NptRecord,
    ProblemOccurrence,
    RiskRecord,
    WellEvent,
)
from drilling_intelligence.operations.contracts import promotion_contract
from drilling_intelligence.operations.hse import hse_incident_entries

WELL = "hse_register_well-a3.xlsx"
SITE = "hse_site_register.xlsx"
GENERIC = "hse_summary_well-a3.xlsx"


def _incidents(workspace, *, current_only: bool = True) -> dict[str, HseIncident]:
    rows = fetch(workspace, HseIncident)
    if current_only:
        rows = [row for row in rows if row.is_current]
    return {row.incident_reference or "": row for row in rows}


# --------------------------------------------------------------------- the positive path


def test_a_well_incident_register_is_classified_and_promoted(workspace) -> None:
    ingest_v72(workspace)
    result = promote_file(workspace, WELL)
    assert result.outcome == "PROMOTED", result.to_dict()
    # Four rows in the register; HSE-104 names B-11 and is refused.
    assert result.counts["hse_incident"]["created"] == 3, result.to_dict()
    assert set(_incidents(workspace)) == {"HSE-101", "HSE-102", "HSE-103"}

    row = _incidents(workspace)["HSE-101"]
    assert row.record_state == RecordState.ACTUAL.value
    assert row.status == "CANDIDATE"
    assert row.origin == "DERIVED"
    assert row.well_id, "a register filed under a well stays attached to that well"
    assert row.occurred_at is not None, "the register's date column is parsed"


def test_severity_is_kept_verbatim_and_never_scored(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, WELL)
    rows = _incidents(workspace)
    assert rows["HSE-101"].severity == "Low"
    assert rows["HSE-102"].severity == "Medium"
    assert rows["HSE-103"].severity == "Low"
    for label, row in rows.items():
        assert not any(
            name.endswith("score") or name.endswith("rating_value")
            for name in [column.name for column in row.__table__.columns]
            if getattr(row, name) not in (None, "")
        ), f"{label}: a severity word must not become a number"


def test_cause_epistemic_status_follows_the_source(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, WELL)
    rows = _incidents(workspace)
    assert rows["HSE-101"].root_cause_status == CauseStatus.KNOWN.value
    assert rows["HSE-101"].root_cause, "the register states one, so it is stored"
    for label in ("HSE-102", "HSE-103"):
        assert rows[label].root_cause is None, label
        assert rows[label].root_cause_status == CauseStatus.UNKNOWN.value, (
            f"{label}: an immediate cause, or a description, is never promoted into a root cause"
        )


def test_a_site_incident_keeps_well_id_null(workspace) -> None:
    """The core site-scope rule: no well is invented from the project or the location."""
    ingest_v72(workspace)
    promote_file(workspace, SITE)
    rows = _incidents(workspace)
    assert set(rows) == {"SITE-01", "SITE-02", "SITE-03"}, rows
    for label, row in rows.items():
        assert row.well_id is None, f"{label}: a camp/road/laydown incident has no well"
        assert row.location_text, f"{label}: the site it did happen at is kept"


def test_a_site_incident_still_carries_its_real_project_scope(workspace) -> None:
    """``well_id`` NULL is not ``no scope``.  The project is stated by the ingestion, so it is kept."""
    ingest_v72(workspace)
    promote_file(workspace, SITE)
    for label, row in _incidents(workspace).items():
        assert row.project_id, f"{label}: a site incident belongs to a project even without a well"


def test_a_site_incident_is_never_written_as_a_well_event(workspace) -> None:
    """The alternative to ``well_id`` NULL would be a fake well; this proves neither happened."""
    ingest_v72(workspace)
    promote_file(workspace, SITE)
    assert _incidents(workspace), "site incidents must exist for this to mean anything"
    assert fetch(workspace, WellEvent) == [], (
        "no WellEvent stand-in - WellEvent.well_id is NOT NULL, so a site incident cannot live there"
    )


def test_lost_time_does_not_become_an_npt_record(workspace) -> None:
    """``Lost Time (hr) = 6.5`` is a duration.  NPT is a classified record, and none was created."""
    ingest_v72(workspace)
    promote_file(workspace, WELL)
    rows = _incidents(workspace)
    # NOTE, recorded deliberately: ``HseIncidentEntry`` parses ``npt_hours`` from a "Lost Time (hr)"
    # column, but ``hse_incident`` has no ``npt_hours_*`` column, so the parsed value is dropped -
    # the same half-typed shape as ``spill_volume``.  This is a known open decision, not certified
    # behaviour; the assertion below only proves the safe half of it today.
    assert all(row.npt_id is None for row in rows.values()), (
        "no NPT row may be manufactured from a duration"
    )
    assert fetch(workspace, NptRecord) == [], "and none was created behind the scenes either"


def test_no_risk_problem_or_well_event_row_is_created(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, WELL)
    assert _incidents(workspace)
    assert fetch(workspace, RiskRecord) == [], "a severity word is not a risk score"
    assert fetch(workspace, ProblemOccurrence) == [], "an incident is not a problem occurrence"
    assert fetch(workspace, WellEvent) == []


# --------------------------------------------------------------------- the refusals


def test_a_generic_incident_table_is_refused(workspace) -> None:
    """``Incident | Date | Severity`` says nothing an HSE record can be built from."""
    ingest_v72(workspace)
    promote_file(workspace, GENERIC)
    assert _incidents(workspace) == {}, "no incident row from a refused table"


def test_the_generic_shape_is_refused_by_the_parser(workspace) -> None:
    rows = [["Incident", "Date", "Severity"], ["Slip", "2026-01-01", "High"]]
    assert hse_incident_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]}) == []


def test_prose_mentioning_incident_words_creates_nothing(workspace) -> None:
    rows = [["Notes"], ["A spill was reported and the injury was treated on site."]]
    assert hse_incident_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]}) == []


def test_a_row_naming_another_well_is_refused_not_reassigned(workspace) -> None:
    """The document is A-3's; a row that says B-11 is not filed under A-3 to be helpful.

    This runs on the real workbook: ``HSE-104`` states ``B-11`` in its own Well column inside a
    register filed under A-3.  Filing it under A-3 would put another well's spill in this well's
    history, so the row is refused and the other three are unaffected.
    """
    ingest_v72(workspace)
    result = promote_file(workspace, WELL)

    conflicts = [item for item in result.skipped if item.get("reason") == "WELL_SCOPE_CONFLICT"]
    assert len(conflicts) == 1, result.to_dict()
    assert "B-11" in conflicts[0]["detail"], conflicts[0]

    rows = _incidents(workspace)
    assert set(rows) == {"HSE-101", "HSE-102", "HSE-103"}, (
        "the conflicting row was not written, and its three siblings were"
    )
    assert all(row.well_id for row in rows.values())


def test_an_unknown_well_is_refused_by_the_same_rule(workspace) -> None:
    """A name that matches no well is a conflict too - resolving it would mean guessing by name."""
    from sqlalchemy import select

    from drilling_intelligence.database.models import Document, DocumentVersion
    from drilling_intelligence.operations.promote import PromotionResult, VersionPromoter

    ingest_v72(workspace)
    promote_file(workspace, WELL)
    rows = [
        ["Incident Ref", "Well", "Incident Type", "Description"],
        ["HSE-XX", "ZZ-99", "spill", "Names a well that does not exist"],
    ]
    with workspace.database.read_only() as session:
        document = session.scalars(select(Document).where(Document.filename == WELL)).first()
        version = session.get(DocumentVersion, str(document.current_version_id))
        promoter = VersionPromoter(session)
        result = PromotionResult(document_id=document.id, version_id=version.id)
        promoter._promote_hse(
            payload={
                "tables": [
                    {"table_id": "t", "sheet": "S", "rows": rows, "provenance": {"sheet": "S"}}
                ]
            },
            document=document,
            version=version,
            result=result,
            replace=False,
        )
    assert "WELL_SCOPE_CONFLICT" in [i.get("reason") for i in result.skipped], result.to_dict()


def test_a_site_document_cannot_confer_a_well_on_a_row_that_asks_for_one(workspace) -> None:
    """The inverse refusal: a site register has no scope to confirm a row's well against."""
    from sqlalchemy import select

    from drilling_intelligence.database.models import Document, DocumentVersion
    from drilling_intelligence.operations.promote import PromotionResult, VersionPromoter

    ingest_v72(workspace)
    promote_file(workspace, SITE)
    rows = [
        ["Incident Ref", "Well", "Incident Type", "Description"],
        ["S-XX", "A-3", "spill", "Names a well in a site-scoped register"],
    ]
    with workspace.database.read_only() as session:
        document = session.scalars(select(Document).where(Document.filename == SITE)).first()
        version = session.get(DocumentVersion, str(document.current_version_id))
        promoter = VersionPromoter(session)
        result = PromotionResult(document_id=document.id, version_id=version.id)
        promoter._promote_hse(
            payload={
                "tables": [
                    {"table_id": "t", "sheet": "S", "rows": rows, "provenance": {"sheet": "S"}}
                ]
            },
            document=document,
            version=version,
            result=result,
            replace=False,
        )
    assert "UNRESOLVED_WELL_REFERENCE" in [i.get("reason") for i in result.skipped], (
        result.to_dict()
    )


def test_spill_volume_is_a_typed_fact_with_the_source_unit(workspace) -> None:
    """Option A: the release quantity is a first-class measurement, not a JSON side-channel."""
    ingest_v72(workspace)
    promote_file(workspace, WELL)
    row = _incidents(workspace)["HSE-102"]
    assert (row.spill_volume_text, row.spill_volume_value, row.spill_volume_unit) == (
        "3.5",
        3.5,
        "bbl",
    )
    # A row that states no release keeps no value; the unit is the column's, not a measurement.
    empty = _incidents(workspace)["HSE-101"]
    assert empty.spill_volume_value is None
    assert empty.spill_volume_text is None


def test_lost_time_is_stored_as_wording_and_creates_no_npt(workspace) -> None:
    """Option B: ``Lost Time (hr) = 6.5`` survives as text and implies nothing."""
    ingest_v72(workspace)
    promote_file(workspace, WELL)
    rows = _incidents(workspace)
    assert rows["HSE-102"].npt_hours_text == "6.5"
    assert rows["HSE-101"].npt_hours_text is None
    assert all(row.npt_id is None for row in rows.values())
    assert fetch(workspace, NptRecord) == []


def test_absence_of_a_well_column_leaves_rows_site_scoped(workspace) -> None:
    """No well column is not a conflict - it is simply a site register."""
    rows = [
        ["Incident Ref", "Incident Type", "Date", "Severity", "Description", "Location"],
        ["S-1", "spill", "2026-02-01", "Low", "Small diesel release", "Base laydown area"],
    ]
    entries = hse_incident_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]})
    assert len(entries) == 1
    assert entries[0].well_name == "", "a table with no Well column leaves every row site-scoped"
    assert entries[0].location_text == "Base laydown area"


# --------------------------------------------------------------------- history and identity


def test_repromoting_the_same_register_creates_nothing_new(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, WELL)
    before = {row.id for row in fetch(workspace, HseIncident)}
    result = promote_file(workspace, WELL)
    assert {row.id for row in fetch(workspace, HseIncident)} == before
    assert result.counts["hse_incident"]["created"] == 0, result.to_dict()
    assert result.counts["hse_incident"]["unchanged"] == 3, result.to_dict()


def test_a_corrected_severity_supersedes_rather_than_duplicates(workspace) -> None:
    from openpyxl import load_workbook

    ingest_v72(workspace)
    promote_file(workspace, WELL)
    assert _incidents(workspace)["HSE-102"].severity == "Medium"

    path = workspace.root / "corpus" / WELL
    workbook = load_workbook(path)
    for row in workbook.active.iter_rows(min_row=1):
        for cell in row:
            if cell.value == "Medium":
                cell.value = "High"
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, WELL)

    assert _incidents(workspace)["HSE-102"].severity == "High"
    superseded = [row for row in fetch(workspace, HseIncident) if not row.is_current]
    assert any(
        row.incident_reference == "HSE-102" and row.severity == "Medium" for row in superseded
    ), "the earlier rating stays readable as history"


def test_an_incident_the_source_stopped_stating_is_removed(workspace) -> None:
    from openpyxl import load_workbook

    ingest_v72(workspace)
    promote_file(workspace, WELL)
    assert set(_incidents(workspace)) == {"HSE-101", "HSE-102", "HSE-103"}

    path = workspace.root / "corpus" / WELL
    workbook = load_workbook(path)
    # Delete the row that actually holds HSE-103.  The last row of the sheet is HSE-104, which is
    # refused for a scope conflict and was never promoted, so deleting it would prove nothing.
    target = next(
        row[0].row
        for row in workbook.active.iter_rows(min_col=1, max_col=1)
        if row[0].value == "HSE-103"
    )
    workbook.active.delete_rows(target)
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, WELL)

    assert set(_incidents(workspace)) == {"HSE-101", "HSE-102"}


def test_provenance_locates_every_row_in_its_source(workspace) -> None:
    ingest_v72(workspace)
    for file_name in (WELL, SITE):
        promote_file(workspace, file_name)
    for label, row in _incidents(workspace).items():
        assert row.document_version_id, label
        entry = row.provenance[0]
        assert entry["source_sheet"], label
        assert entry["source_table_id"], label
        assert entry["source_range"], label
        assert entry["source_row_index"] >= 0, label
        assert entry["source_label"] == label, label


def test_the_contract_is_registered(workspace) -> None:
    contract = promotion_contract(DocumentClassification.HSE)
    assert contract.domain_promotable
    assert contract.handler == "hse"
    assert contract.target_models == ("hse_incident",)
    assert contract.contract_revision == "v7"
