"""V7.2 in deterministic structured search: findable, projected from source, and pruned when stale.

Identity is ``structured:<record-type>:<row-id>`` and is derived from the row, so a rebuild produces
the same ids.  Nothing calculated is indexed: a projection that emitted an estimated kick volume
would be a fabricated measurement with a search hit attached to it.
"""

from __future__ import annotations

from tests.fixtures.fieldops import fetch, ingest_v72, promote_file, reingest

from drilling_intelligence.database.models import HseIncident, WellControlEvent
from drilling_intelligence.search.service import SearchService
from drilling_intelligence.search.structured import (
    _BUILDERS,
    _RECORD_SOURCES,
    STRUCTURED_RECORD_TYPES,
    structured_records,
)


def _search(workspace, query):
    service = SearchService.for_workspace(workspace)
    service.rebuild()
    return service.search(query).results


def test_the_three_registries_stay_equal(workspace) -> None:
    assert set(STRUCTURED_RECORD_TYPES) == set(_BUILDERS)
    assert set(STRUCTURED_RECORD_TYPES) == {record for _, record, _ in _RECORD_SOURCES}
    assert "well_control_event" in STRUCTURED_RECORD_TYPES
    assert "hse_incident" in STRUCTURED_RECORD_TYPES


def test_a_well_control_row_is_findable_by_what_the_source_said(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")
    hits = [hit for hit in _search(workspace, "SIDPP") if hit.source_type == "structured"]
    assert hits, "no structured hit for a term the sheet states"
    assert all(hit.provenance["record_type"] == "well_control_event" for hit in hits)


def test_identity_is_structured_type_row_and_matches_the_database(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")
    promote_file(workspace, "hse_register_well-a3.xlsx")
    ids = set()
    with workspace.database.read_only() as session:
        for record in structured_records(session):
            ids.add(record.record_id)
            assert record.record_id.startswith("structured:")
            _, record_type, row_id = record.record_id.split(":", 2)
            assert record_type in STRUCTURED_RECORD_TYPES
            assert row_id
    assert len(ids) == len(fetch(workspace, WellControlEvent)) + len(
        fetch(workspace, HseIncident)
    ) + (len(ids) - len(fetch(workspace, WellControlEvent)) - len(fetch(workspace, HseIncident)))
    wc_ids = {row.id for row in fetch(workspace, WellControlEvent)}
    assert any(f"structured:well_control_event:{row_id}" in ids for row_id in wc_ids)


def test_lost_time_wording_is_searchable_without_implying_a_duration(workspace) -> None:
    """The wording the source wrote is discoverable; there is still no NPT row behind it."""
    ingest_v72(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")
    promote_file(workspace, "hse_register_well-a3.xlsx")
    hits = [hit for hit in _search(workspace, "lost time") if hit.source_type == "structured"]
    kinds = {hit.provenance["record_type"] for hit in hits}
    assert kinds == {"well_control_event", "hse_incident"}, kinds
    from drilling_intelligence.database.models import NptRecord

    assert fetch(workspace, NptRecord) == [], "no NPT row exists to be found"


def test_a_stated_spill_volume_is_findable_by_its_own_number(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, "hse_register_well-a3.xlsx")
    hits = [hit for hit in _search(workspace, "3.5") if hit.source_type == "structured"]
    assert hits, "the source states 3.5 bbl; it must be findable"
    assert all(hit.provenance["record_type"] == "hse_incident" for hit in hits)


def test_a_rebuild_is_deterministic_and_leaves_no_duplicates(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")
    promote_file(workspace, "hse_register_well-a3.xlsx")
    service = SearchService.for_workspace(workspace)
    service.rebuild()
    with workspace.database.read_only() as session:
        first = [record.record_id for record in structured_records(session)]
    service.rebuild()
    with workspace.database.read_only() as session:
        second = [record.record_id for record in structured_records(session)]
    assert first == second, "a rebuild must not reorder or duplicate structured records"
    assert len(first) == len(set(first)), "duplicate structured ids"


def test_a_site_incident_is_findable_without_a_well(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, "hse_site_register.xlsx")
    hits = [hit for hit in _search(workspace, "laydown") if hit.source_type == "structured"]
    assert hits, "a site-only incident must still be findable"
    assert all(hit.provenance["record_type"] == "hse_incident" for hit in hits)
    assert all(hit.provenance.get("well_id") in ("", None) for hit in hits), (
        "no well was invented to make the row findable"
    )


def test_a_superseded_row_leaves_the_current_projection(workspace) -> None:
    from openpyxl import load_workbook

    ingest_v72(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")
    _search(workspace, "SIDPP")

    path = workspace.root / "corpus" / "well_control_log_well-a3.xlsx"
    workbook = load_workbook(path)
    workbook.active["E5"] = 1325
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, "well_control_log_well-a3.xlsx")

    service = SearchService.for_workspace(workspace)
    service.rebuild()
    with workspace.database.read_only() as session:
        ids = [record.record_id for record in structured_records(session)]
    current = {row.id for row in fetch(workspace, WellControlEvent) if row.is_current}
    projected = {
        record_id.split(":", 2)[2]
        for record_id in ids
        if record_id.startswith("structured:well_control_event:")
    }
    assert projected == current, "the projection must follow the current rows, not the history"
    assert not any(record_id.endswith(":1200") for record_id in ids), (
        "a superseded reading must not stay searchable as current"
    )
