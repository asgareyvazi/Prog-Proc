"""The V7.2 evidence chain, end to end: search hit -> authoritative row -> provenance.

V7.2 put well-control events and HSE incidents into the structured search index, but a search hit is
only useful if retrieval can reach the row behind it.  It could not: ``retrieval.service`` resolved
structured hits through a ``record_type -> model`` map listing ten of the fourteen types, and a type
missing from that map is skipped silently.  So a search for ``SIDPP`` produced a hit and retrieval
then had nothing to show for it.

These tests run the real chain against the real corpus, and the first one pins the two registries
together so the gap cannot reopen quietly.
"""

from __future__ import annotations

from tests.fixtures.fieldops import fetch, ingest_v72, promote_file, reingest

from drilling_intelligence.database.models import HseIncident, WellControlEvent
from drilling_intelligence.evidence.contract import EvidenceQuery
from drilling_intelligence.evidence.service import EvidenceQueryService
from drilling_intelligence.retrieval.contract import RetrievalRequest
from drilling_intelligence.retrieval.service import _STRUCTURED_MODELS, RetrievalService
from drilling_intelligence.search.service import SearchService
from drilling_intelligence.search.structured import STRUCTURED_RECORD_TYPES

WC_LOG = "well_control_log_well-a3.xlsx"
HSE_REGISTER = "hse_register_well-a3.xlsx"
HSE_SITE = "hse_site_register.xlsx"


def _retrieve(workspace, query: str, limit: int = 20):
    search = SearchService.for_workspace(workspace)
    search.rebuild()
    return RetrievalService(database=workspace.database, search_service=search).retrieve(
        RetrievalRequest(query=query, limit=limit)
    )


def _prepared(workspace) -> None:
    ingest_v72(workspace)
    for file_name in (WC_LOG, HSE_REGISTER, HSE_SITE):
        promote_file(workspace, file_name)


def test_every_structured_type_the_index_emits_is_one_retrieval_can_resolve(workspace) -> None:
    """The drift guard: a type the index emits but retrieval cannot resolve is a silent dead end."""
    missing = sorted(set(STRUCTURED_RECORD_TYPES) - set(_STRUCTURED_MODELS))
    assert not missing, f"retrieval resolves nothing for {missing}"
    assert sorted(set(_STRUCTURED_MODELS) - set(STRUCTURED_RECORD_TYPES)) == []


def test_a_sidpp_search_hit_reaches_the_authoritative_well_control_row(workspace) -> None:
    _prepared(workspace)
    bundle = _retrieve(workspace, "SIDPP")

    structured = [item for item in bundle.items if item.source_type == "structured"]
    assert structured, "no structured item came back for a term the sheet states"
    wc_items = [item for item in structured if item.record_type == "well_control_event"]
    assert wc_items, [item.record_type for item in structured]

    ids = {str(item.source_id) for item in wc_items}
    rows = {row.id: row for row in fetch(workspace, WellControlEvent)}
    assert ids <= set(rows), "a retrieval item named a row that does not exist"
    row = rows[next(iter(ids))]
    entry = row.provenance[0]
    assert entry["document_id"] == row.document_id
    assert entry["document_version_id"] == row.document_version_id
    assert entry["source_sha256"], "no content digest to detect source drift"
    assert entry["source_sheet"] and entry["source_range"] and entry["source_table_id"]


def test_a_spill_search_hit_reaches_the_authoritative_hse_row(workspace) -> None:
    _prepared(workspace)
    bundle = _retrieve(workspace, "spill")
    hse_items = [item for item in bundle.items if item.record_type == "hse_incident"]
    assert hse_items, "no HSE item came back for a search the register satisfies"
    ids = {str(item.source_id) for item in hse_items}
    assert ids <= {row.id for row in fetch(workspace, HseIncident)}


def test_a_site_only_incident_keeps_null_well_through_the_whole_chain(workspace) -> None:
    """No layer invents a well: not the index, not retrieval, not the evidence item."""
    _prepared(workspace)
    bundle = _retrieve(workspace, "laydown")
    items = [item for item in bundle.items if item.record_type == "hse_incident"]
    assert items, "a site-only incident must still be reachable"
    for item in items:
        assert not item.well_id, "the evidence item acquired a well somewhere in the chain"
        assert item.current is True
    ids = {str(item.source_id) for item in items}
    rows = {row.id: row for row in fetch(workspace, HseIncident) if row.id in ids}
    assert rows
    for row in rows.values():
        assert row.well_id is None
        assert row.project_id, "but it did keep the project it genuinely belongs to"


def test_the_evidence_package_carries_a_v72_row(workspace) -> None:
    _prepared(workspace)
    SearchService.for_workspace(workspace).rebuild()
    service = EvidenceQueryService.for_workspace(workspace)
    package = service.query(EvidenceQuery(topics=("SIDPP",), limit=3))
    assert package.items, "an evidence package for a resolvable hit must not be empty"


def test_a_superseded_row_stays_out_of_the_current_projection(workspace) -> None:
    from openpyxl import load_workbook

    _prepared(workspace)
    path = workspace.root / "corpus" / WC_LOG
    workbook = load_workbook(path)
    workbook.active["E5"] = 1325
    workbook.save(path)
    reingest(workspace)
    promote_file(workspace, WC_LOG)

    bundle = _retrieve(workspace, "SIDPP")
    ids = {str(item.source_id) for item in bundle.items if item.record_type == "well_control_event"}
    current = {row.id for row in fetch(workspace, WellControlEvent) if row.is_current}
    assert ids <= current, "a superseded reading is history, not a current retrieval answer"
