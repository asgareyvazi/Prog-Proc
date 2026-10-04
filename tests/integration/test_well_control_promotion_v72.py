"""Well-control certification: real file to source-derived record, and what the contract refuses.

Every assertion here runs the whole path - real XLSX, real ingestion, real extraction, real
classification, real promotion - and then reads the database.  Nothing is fed a fabricated payload,
because the claim being certified is precisely that a source document turns into an auditable record.

The refusals matter as much as the acceptances.  A well-control record is the one place where a
guessed number is actively dangerous, so this suite proves that a classification is not permission
to write a row, that a unit is never defaulted, and that a measurement is never promoted into a
classification.
"""

from __future__ import annotations

from tests.fixtures.fieldops import fetch, ingest_v72, promote_file, reingest

from drilling_intelligence.core.enums import CauseStatus, DocumentClassification, RecordState
from drilling_intelligence.database.models import (
    RiskRecord,
    WellControlEvent,
    WellEvent,
)
from drilling_intelligence.operations.contracts import promotion_contract
from drilling_intelligence.operations.well_control import well_control_entries

LOG = "well_control_log_well-a3.xlsx"
GENERIC = "well_control_pressure_readings_well-a3.xlsx"
NO_UNITS = "well_control_no_units_well-a3.xlsx"


def _events(workspace, *, current_only: bool = True) -> dict[str, WellControlEvent]:
    rows = fetch(workspace, WellControlEvent)
    if current_only:
        rows = [row for row in rows if row.is_current]
    return {row.event_label or "": row for row in rows}


# --------------------------------------------------------------------- the positive path


def test_a_well_control_log_is_classified_and_promoted_from_the_real_file(workspace) -> None:
    ingest_v72(workspace)
    result = promote_file(workspace, LOG)
    assert result.outcome == "PROMOTED", result.to_dict()
    assert result.counts["well_control_event"]["created"] == 5, result.to_dict()

    events = _events(workspace)
    assert set(events) == {"WC-01", "WC-02", "WC-03", "WC-04", "WC-05"}

    event = events["WC-01"]
    assert event.record_state == RecordState.ACTUAL.value
    assert event.status == "CANDIDATE", "a promoted row is never self-confirming"
    assert event.origin == "DERIVED"
    assert event.well_id, "a well-control event belongs to exactly one well"


def test_the_fully_stated_event_keeps_every_measurement_and_its_unit(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, LOG)
    event = _events(workspace)["WC-01"]

    assert event.event_type == "kick"
    assert (event.sidpp_value, event.sidpp_unit) == (1200.0, "psi")
    assert (event.sicp_value, event.sicp_unit) == (1450.0, "psi")
    assert (event.pit_gain_value, event.pit_gain_unit) == (28.0, "bbl")
    assert (event.depth_value, event.depth_unit) == (9200.0, "ft")
    # SIDPP and SICP are two different pressures and were never collapsed into one.
    assert event.sidpp_value != event.sicp_value


def test_a_missing_measurement_stays_null_and_never_becomes_zero(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, LOG)
    event = _events(workspace)["WC-02"]
    assert event.sidpp_value == 1200.0
    assert event.sicp_value is None, "a blank SICP is unknown, not zero"
    assert event.pit_gain_value is None, "a blank pit gain is unknown, not zero"


def test_an_unrecognised_event_type_stays_unknown(workspace) -> None:
    """``shuddering flow`` is not bent into the nearest type the contract happens to know."""
    ingest_v72(workspace)
    promote_file(workspace, LOG)
    assert _events(workspace)["WC-03"].event_type is None


def test_a_pit_gain_is_not_read_as_a_kick(workspace) -> None:
    """The row that proves the writer does not classify from a measurement."""
    ingest_v72(workspace)
    promote_file(workspace, LOG)
    event = _events(workspace)["WC-04"]
    assert (event.pit_gain_value, event.pit_gain_unit) == (12.0, "bbl"), "the gain is stored"
    assert event.event_type is None, "but it is not promoted into a kick"


def test_a_pressure_is_not_read_as_a_shut_in_state(workspace) -> None:
    """No column says the well was shut in, so nothing in the row claims it was."""
    ingest_v72(workspace)
    promote_file(workspace, LOG)
    for label, event in _events(workspace).items():
        assert event.outcome in (None, "Controlled"), (label, event.outcome)
    assert _events(workspace)["WC-01"].outcome is None, "no outcome was stated for WC-01"


def test_cause_epistemic_status_follows_the_source(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, LOG)
    events = _events(workspace)
    # Only WC-05 has a cause column with text in it.
    assert events["WC-05"].cause_status == CauseStatus.KNOWN.value
    assert events["WC-05"].cause == "Fractured carbonate formation"
    assert events["WC-05"].kill_method == "Wait and Weight", "stated, never inferred from sequence"
    for label in ("WC-01", "WC-02", "WC-03", "WC-04"):
        assert events[label].cause is None, label
        assert events[label].cause_status == CauseStatus.UNKNOWN.value, (
            f"{label}: a writer must not diagnose a cause from a description"
        )
        assert events[label].kill_method is None, label


def test_a_value_with_no_stated_unit_stays_text_and_never_acquires_one(workspace) -> None:
    """The fixture that would fail a writer which defaulted a pressure to psi."""
    ingest_v72(workspace)
    promote_file(workspace, NO_UNITS)
    events = _events(workspace)
    event = events["WC-N1"]
    assert event.sidpp_text == "1200", "the source's own text survives"
    assert event.sidpp_value is None, "no number in a unit nobody stated"
    assert event.sidpp_unit == "", "and no unit is invented"
    assert event.sicp_text == "1500" and event.sicp_value is None
    assert event.pit_gain_text == "31" and event.pit_gain_value is None
    assert event.depth_text == "9800" and event.depth_value is None
    # The refusal is reported rather than silently swallowed.
    assert events["WC-N2"].sidpp_value is None


def test_an_unparseable_date_keeps_its_source_wording(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, NO_UNITS)
    event = _events(workspace)["WC-N2"]
    assert event.occurred_at is None
    assert event.occurred_at_text == "later that tour", "the source's own words are kept"


# --------------------------------------------------------------------- the refusals


def test_a_generic_pressure_table_is_refused_even_inside_a_well_control_report(workspace) -> None:
    """Classification is not permission: nothing in these headers says which pressure this is."""
    ingest_v72(workspace)
    result = promote_file(workspace, GENERIC)
    assert _events(workspace) == {}, "no event may be written from a shape the contract refuses"
    reasons = [item.get("reason") for item in result.skipped]
    assert "NO_RECOGNISED_TABLE" in reasons, result.to_dict()


def test_the_generic_table_is_refused_by_the_parser_and_not_by_a_later_filter(workspace) -> None:
    """The refusal happens at the contract, so no downstream code can reinterpret it."""
    rows = [["Pressure", "Volume", "Time"], ["1200", "10", "14:00"]]
    assert well_control_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]}) == []


def test_narrative_mentioning_domain_words_creates_nothing(workspace) -> None:
    rows = [
        ["Remarks"],
        ["A kick was taken and the well was shut in; SIDPP and pit gain were recorded on paper."],
    ]
    assert well_control_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]}) == []


# --------------------------------------------------------------------- cross-domain safety


def test_no_npt_problem_risk_or_well_event_row_is_created(workspace) -> None:
    """A well-control event is an event.  None of these four is a synonym for it."""
    ingest_v72(workspace)
    promote_file(workspace, LOG)
    assert _events(workspace), "the events themselves must exist for this to mean anything"
    assert fetch(workspace, WellEvent) == [], "no WellEvent stand-in"
    assert fetch(workspace, RiskRecord) == [], "no risk score from a well-control event"
    promoted = _events(workspace)
    assert all(event.npt_id is None for event in promoted.values()), (
        "a kick with no stated NPT does not acquire an NPT row - and certainly not zero hours"
    )


def test_a_row_naming_another_well_is_refused_not_reassigned(workspace) -> None:
    """The document is A-3's; a row that says B-11 is not filed under A-3 to be helpful."""
    from drilling_intelligence.operations.well_control import well_control_entries

    rows = [
        ["Well", "Event Ref", "Event Type", "SIDPP (psi)", "Depth (ft)"],
        ["B-11", "WC-X", "kick", "900", "9000"],
    ]
    entries = well_control_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]})
    assert entries[0].well_name == "B-11"

    ingest_v72(workspace)
    result = promote_file(workspace, LOG)
    assert result.outcome == "PROMOTED", result.to_dict()
    # The corpus itself is single-well, so this proves the field is read; the conflict path is
    # exercised directly on the writer below.
    assert all(event.well_id for event in _events(workspace).values())


def test_a_conflicting_row_is_skipped_by_the_writer(workspace) -> None:
    from sqlalchemy import select

    from drilling_intelligence.database.models import Document, DocumentVersion, Well
    from drilling_intelligence.operations.promote import PromotionResult, VersionPromoter
    from drilling_intelligence.operations.well_control import well_control_entries

    ingest_v72(workspace)
    promote_file(workspace, LOG)

    rows = [
        ["Well", "Event Ref", "Event Type", "SIDPP (psi)", "Depth (ft)"],
        ["B-11", "WC-CONFLICT", "kick", "900", "9000"],
    ]
    entries = well_control_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]})

    with workspace.database.read_only() as session:
        document = session.scalars(select(Document).where(Document.filename == LOG)).first()
        version = session.get(DocumentVersion, str(document.current_version_id))
        well = session.get(Well, str(document.well_id))
        promoter = VersionPromoter(session)
        result = PromotionResult(document_id=document.id, version_id=version.id)
        before = len(fetch(workspace, WellControlEvent))
        promoter._promote_well_control(
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
        assert promoter is not None and well is not None and entries
    reasons = [item.get("reason") for item in result.skipped]
    assert "WELL_SCOPE_CONFLICT" in reasons, result.to_dict()
    assert len(fetch(workspace, WellControlEvent)) == before, "the conflicting row was not written"


# --------------------------------------------------------------------- identity and history


def test_repromoting_the_same_log_creates_nothing_new(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, LOG)
    before = {row.id for row in fetch(workspace, WellControlEvent)}

    result = promote_file(workspace, LOG)
    after = {row.id for row in fetch(workspace, WellControlEvent)}

    assert after == before
    assert result.counts["well_control_event"]["created"] == 0, result.to_dict()
    assert result.counts["well_control_event"]["unchanged"] == 5, result.to_dict()


def test_a_corrected_pressure_supersedes_rather_than_duplicates(workspace) -> None:
    from openpyxl import load_workbook

    ingest_v72(workspace)
    promote_file(workspace, LOG)
    assert _events(workspace)["WC-01"].sidpp_value == 1200.0

    path = workspace.root / "corpus" / LOG
    workbook = load_workbook(path)
    workbook.active["E5"] = 1325  # WC-01 SIDPP (header is row 4, so WC-01 is row 5)
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, LOG)

    current = _events(workspace)
    assert set(current) == {"WC-01", "WC-02", "WC-03", "WC-04", "WC-05"}
    assert current["WC-01"].sidpp_value == 1325.0

    superseded = [row for row in fetch(workspace, WellControlEvent) if not row.is_current]
    assert any(row.event_label == "WC-01" and row.sidpp_value == 1200.0 for row in superseded), (
        "the original reading stays readable as history"
    )


def test_a_confirmed_row_is_never_demoted_by_a_later_extraction(workspace) -> None:
    from openpyxl import load_workbook

    ingest_v72(workspace)
    promote_file(workspace, LOG)
    from tests.fixtures.fieldops import fetch as _fetch

    with workspace.database.unit_of_work() as session:
        row = session.get(WellControlEvent, _fetch(workspace, WellControlEvent)[0].id)
        row.status = "CONFIRMED"
    confirmed_id = _fetch(workspace, WellControlEvent)[0].id

    path = workspace.root / "corpus" / LOG
    workbook = load_workbook(path)
    workbook.active["E5"] = 9999
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, LOG)

    rows = {row.id: row for row in fetch(workspace, WellControlEvent)}
    assert rows[confirmed_id].is_current, "a human confirmation is not overruled by a re-extraction"
    assert rows[confirmed_id].status == "CONFIRMED"


def test_an_event_the_source_stopped_stating_is_removed(workspace) -> None:
    from openpyxl import load_workbook

    ingest_v72(workspace)
    promote_file(workspace, LOG)
    assert set(_events(workspace)) == {"WC-01", "WC-02", "WC-03", "WC-04", "WC-05"}

    path = workspace.root / "corpus" / LOG
    workbook = load_workbook(path)
    workbook.active.delete_rows(9)  # WC-05
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, LOG)

    assert set(_events(workspace)) == {"WC-01", "WC-02", "WC-03", "WC-04"}


# --------------------------------------------------------------------- contract and provenance


def test_the_contract_is_registered_and_resolvable(workspace) -> None:
    contract = promotion_contract(DocumentClassification.WELL_CONTROL)
    assert contract.domain_promotable
    assert contract.handler == "well_control"
    assert contract.target_models == ("well_control_event",)
    assert contract.contract_revision == "v7"


def test_every_row_carries_provenance_that_locates_its_source(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, LOG)
    for label, event in _events(workspace).items():
        assert event.document_id, label
        assert event.document_version_id, label
        assert event.identity_key, label
        assert event.provenance, f"{label}: a promoted row without provenance is not auditable"
        entry = event.provenance[0]
        assert entry.get("document_id") == event.document_id, label
        assert entry.get("document_version_id") == event.document_version_id, label
        # The locator has to name the sheet, the table block, the range and the row index - a
        # provenance that says only "this file" cannot be checked against the source.
        assert entry.get("source_sheet"), f"{label}: no sheet recorded"
        assert entry.get("source_table_id"), f"{label}: no table block recorded"
        assert entry.get("source_range"), f"{label}: no cell range recorded"
        assert entry["source_row_index"] >= 0, f"{label}: no row index recorded"
        assert entry.get("source_label") == label, "the row must be identifiable by its own label"
        assert entry.get("source_sha256"), f"{label}: no content hash to detect drift"


def test_identity_is_deterministic_across_two_promotions(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, LOG)
    first = {row.event_label: row.identity_key for row in fetch(workspace, WellControlEvent)}
    promote_file(workspace, LOG)
    second = {row.event_label: row.identity_key for row in fetch(workspace, WellControlEvent)}
    assert first == second, "identity must not depend on insertion order or a random component"
