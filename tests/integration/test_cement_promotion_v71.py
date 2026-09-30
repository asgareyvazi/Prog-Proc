"""Cement promotion: lead is not tail, top of cement is not shoe, and nothing is calculated.

A cement record is worth keeping only if its quantities stay distinguishable.  A writer that summed
the lead and tail into one volume, read the shoe depth as the top of cement, or defaulted a volume
unit would produce rows that look complete and cannot be argued with - which is worse than rows
that admit a gap.  These tests hold those boundaries on the real path: file, ingestion, extraction,
classification, promotion, then a read from the database.
"""

from __future__ import annotations

from sqlalchemy import select
from tests.fixtures.fieldops import fetch, ingest_v71, promote_file, reingest

from drilling_intelligence.database.models import CasingRun, CementJob, Well

CEMENT = "cement_report_well-a3.xlsx"
TOTAL_ONLY = "cement_total_only_well-a3.xlsx"
CASING = "casing_report_well-a3.xlsx"


def _jobs(workspace, *, current_only: bool = True) -> dict[str, CementJob]:
    rows = fetch(workspace, CementJob)
    if current_only:
        rows = [row for row in rows if row.is_current]
    return {row.job_label or "": row for row in rows}


def _reasons(result) -> list[str]:
    return [str(item.get("reason") or "") for item in result.skipped]


def test_a_cement_report_becomes_one_row_per_stage(workspace) -> None:
    ingest_v71(workspace)
    result = promote_file(workspace, CEMENT)
    assert result.outcome == "PROMOTED", result.to_dict()
    assert result.counts["cement_job"]["created"] == 3, result.to_dict()

    jobs = _jobs(workspace)
    assert set(jobs) == {"CMT-01", "CMT-02", "CMT-03"}

    job = jobs["CMT-02"]
    assert job.stage_text == "2"
    assert job.stage_number == 2
    assert job.job_type == "Primary"
    assert job.record_state == "ACTUAL"
    assert job.status == "CANDIDATE"
    assert job.origin == "DERIVED"
    assert job.provenance, "a promoted cement job must carry its source locator"


def test_lead_and_tail_stay_separate_and_are_never_summed(workspace) -> None:
    ingest_v71(workspace)
    promote_file(workspace, CEMENT)
    job = _jobs(workspace)["CMT-02"]

    assert job.lead_slurry == "Class G + silica"
    assert job.tail_slurry == "Class G neat"
    assert (job.lead_volume_value, job.lead_volume_unit) == (410.0, "bbl")
    assert (job.tail_volume_value, job.tail_volume_unit) == (260.0, "bbl")
    # The two densities differ, which is the whole point of a two-stage design.
    assert (job.lead_density_value, job.lead_density_unit) == (15.8, "ppg")
    assert (job.tail_density_value, job.tail_density_unit) == (16.4, "ppg")
    assert job.lead_density_value != job.tail_density_value
    # And neither was folded into the other, nor replaced by their sum.
    assert job.lead_volume_value != job.tail_volume_value
    assert 670.0 not in {job.lead_volume_value, job.tail_volume_value}


def test_a_tail_only_job_does_not_borrow_the_lead(workspace) -> None:
    """A stage that states a lead and no tail keeps the tail empty."""
    ingest_v71(workspace)
    promote_file(workspace, CEMENT)
    job = _jobs(workspace)["CMT-03"]
    assert job.lead_volume_value == 95.0
    assert job.tail_volume_value is None
    assert job.tail_slurry == ""


def test_top_of_cement_is_not_shoe_depth(workspace) -> None:
    ingest_v71(workspace)
    promote_file(workspace, CEMENT)
    job = _jobs(workspace)["CMT-02"]
    assert (job.toc_depth_value, job.toc_depth_unit) == (4200.0, "ft")
    assert (job.shoe_depth_value, job.shoe_depth_unit) == (9200.0, "ft")
    assert job.toc_depth_value != job.shoe_depth_value


def test_a_total_only_source_gets_no_invented_split(workspace) -> None:
    """One stated number stays one number, recorded as a total rather than as a lead or a tail."""
    ingest_v71(workspace)
    result = promote_file(workspace, TOTAL_ONLY)
    assert result.outcome == "PROMOTED", result.to_dict()

    jobs = _jobs(workspace)
    assert set(jobs) == {"CMT-09"}
    job = jobs["CMT-09"]
    assert job.lead_volume_value is None
    assert job.tail_volume_value is None
    source = job.attributes.get("source", {})
    assert source.get("total_volume_value") == 420.0
    assert source.get("total_volume_unit") == "bbl"


def test_wait_on_cement_is_wording_not_a_calculation(workspace) -> None:
    ingest_v71(workspace)
    promote_file(workspace, CEMENT)
    assert _jobs(workspace)["CMT-02"].woc_text == "24 hrs"


def test_a_generic_volume_pressure_depth_table_is_not_a_cement_job(workspace) -> None:
    """Numbers in a table are not a cement job; nothing in the columns says cement."""
    from drilling_intelligence.operations.cement import cement_job_entries

    rows = [["Volume (bbl)", "Pressure (psi)", "Depth (ft)"], ["100", "500", "9000"]]
    assert cement_job_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]}) == []


def test_units_are_preserved_and_never_defaulted(workspace) -> None:
    from drilling_intelligence.operations.cement import cement_job_entries

    metric = [
        ["Cement Job", "Lead Volume (m3)", "Lead Density (kg/m3)", "Pressure (MPa)", "TOC (m)"],
        ["CMT-M", "38", "1900", "34", "1280"],
    ]
    entry = cement_job_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": metric}]})[0]
    assert (entry.lead_volume_value, entry.lead_volume_unit) == (38.0, "m3")
    assert (entry.lead_density_value, entry.lead_density_unit) == (1900.0, "kg/m3")
    assert (entry.pressure_value, entry.pressure_unit) == (34.0, "MPa")
    assert (entry.toc_value, entry.toc_unit) == (1280.0, "m")

    # No unit stated: the text survives and the value stays unset, rather than acquiring "bbl".
    unlabelled = [["Cement Job", "Lead Volume", "TOC (ft)"], ["CMT-U", "300", "4000"]]
    entry = cement_job_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": unlabelled}]})[0]
    assert entry.lead_volume_text == "300"
    assert entry.lead_volume_value is None
    assert entry.lead_volume_unit == ""


def test_a_cement_job_is_linked_only_to_a_casing_run_the_source_names(workspace) -> None:
    """The association comes from the source's own string label, resolved exactly."""
    ingest_v71(workspace)
    promote_file(workspace, CASING)
    promote_file(workspace, CEMENT)

    jobs = _jobs(workspace)
    runs = {row.string_label: row for row in fetch(workspace, CasingRun) if row.is_current}
    assert jobs["CMT-02"].casing_run_id == runs["Intermediate"].id
    assert jobs["CMT-02"].casing_resolution == "EXPLICIT"


def test_an_unresolvable_casing_reference_leaves_the_link_unset(workspace) -> None:
    """A reference matching no run is not resolved by size, depth or row order."""
    from drilling_intelligence.operations.cement import cement_job_entries
    from drilling_intelligence.operations.promote import VersionPromoter

    ingest_v71(workspace)
    promote_file(workspace, CASING)

    rows = [
        ["Cement Job", "Casing", "Lead Volume (bbl)", "TOC (ft)"],
        ["CMT-X", "5 1/2 in liner", "120", "9000"],
    ]
    entries = cement_job_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]})
    assert entries[0].casing_reference == "5 1/2 in liner"

    with workspace.database.read_only() as session:
        promoter = VersionPromoter(session)
        well = session.scalars(select(Well).order_by(Well.id)).first()

        class _Result:
            """The two fields the resolver reports through."""

            def __init__(self) -> None:
                self.skipped: list[dict[str, str]] = []

        result = _Result()
        casing_run_id, resolution = promoter._linked_casing_run(
            well=well, reference="5 1/2 in liner", result=result
        )
        assert casing_run_id is None
        assert resolution == "NOT_STATED"
        assert result.skipped[0]["reason"] == "UNRESOLVED_CASING_REFERENCE"


def test_repromoting_the_same_report_creates_nothing_new(workspace) -> None:
    ingest_v71(workspace)
    promote_file(workspace, CEMENT)
    before = {row.id for row in fetch(workspace, CementJob)}

    result = promote_file(workspace, CEMENT)
    after = {row.id for row in fetch(workspace, CementJob)}

    assert after == before
    assert result.counts["cement_job"]["created"] == 0, result.to_dict()
    assert result.counts["cement_job"]["unchanged"] == 3, result.to_dict()


def test_a_corrected_volume_supersedes_rather_than_duplicates(workspace) -> None:
    from openpyxl import load_workbook

    ingest_v71(workspace)
    promote_file(workspace, CEMENT)
    assert _jobs(workspace)["CMT-02"].lead_volume_value == 410.0

    path = workspace.root / "corpus" / CEMENT
    workbook = load_workbook(path)
    workbook.active["G6"] = 455  # CMT-02 lead volume (header is row 4, so CMT-02 is row 6)
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, CEMENT)

    current = _jobs(workspace)
    assert set(current) == {"CMT-01", "CMT-02", "CMT-03"}
    assert current["CMT-02"].lead_volume_value == 455.0

    superseded = [row for row in fetch(workspace, CementJob) if not row.is_current]
    assert any(
        row.job_label == "CMT-02" and row.lead_volume_value == 410.0 for row in superseded
    ), "the original volume stays readable"


def test_a_stage_the_source_stopped_stating_is_removed(workspace) -> None:
    from openpyxl import load_workbook

    ingest_v71(workspace)
    promote_file(workspace, CEMENT)
    assert set(_jobs(workspace)) == {"CMT-01", "CMT-02", "CMT-03"}

    path = workspace.root / "corpus" / CEMENT
    workbook = load_workbook(path)
    workbook.active.delete_rows(7)  # CMT-03
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, CEMENT)

    assert set(_jobs(workspace)) == {"CMT-01", "CMT-02"}


def test_a_cement_document_with_no_cement_table_is_refused(workspace) -> None:
    from drilling_intelligence.operations.cement import cement_job_entries

    rows = [["Remarks"], ["Cement job went to plan, returns to surface"]]
    assert cement_job_entries({"tables": [{"table_id": "t", "sheet": "S", "rows": rows}]}) == []
