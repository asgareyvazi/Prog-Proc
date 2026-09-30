"""Casing runs and cement jobs reach the search index as themselves.

A promoted record that nobody can find is a record only the promotion path knows about.  These tests
go the whole way - ingest, extraction, classification, promotion, index rebuild, search - and split
the two claims that are easy to blur together: that the row is *findable* (a property of search),
and that the projection says what the *source* said rather than a value the platform happened to
parse (a property of the projection itself).
"""

from __future__ import annotations

import pytest
from tests.fixtures.fieldops import ingest_v71, promote

from drilling_intelligence.search.service import SearchService
from drilling_intelligence.search.structured import structured_records


@pytest.fixture
def promoted(workspace):
    ingest_v71(workspace)
    promote(workspace)
    return workspace


@pytest.fixture
def rebuilt(promoted) -> SearchService:
    """A search service rebuilt over a workspace holding promoted casing runs and cement jobs."""
    service = SearchService.for_workspace(promoted)
    service.rebuild()
    return service


def _units(workspace, record_type: str) -> list:
    """The projection for one record type, read straight from the projector."""
    with workspace.database.read_only() as session:
        return [u for u in structured_records(session) if u.record_type == record_type]


def _hits(service: SearchService, query: str, record_type: str) -> list:
    """The structured hits of one search, filtered to a record type by its own provenance."""
    return [
        hit
        for hit in service.search(query).results
        if hit.source_type == "structured" and hit.provenance.get("record_type") == record_type
    ]


# --------------------------------------------------------------------- is it in the index at all


def test_promoted_casing_runs_and_cement_jobs_are_projected(promoted) -> None:
    assert len(_units(promoted, "casing_run")) == 4
    assert len(_units(promoted, "cement_job")) == 4


def test_a_promoted_casing_run_is_searchable(rebuilt) -> None:
    assert _hits(rebuilt, "conductor", "casing_run"), (
        "a promoted casing run must be findable by the string its source named"
    )


def test_a_promoted_cement_job_is_searchable(rebuilt) -> None:
    assert _hits(rebuilt, "slurry", "cement_job"), (
        "a promoted cement job must be findable by the slurry its source named"
    )


def test_a_superseded_run_leaves_the_index_but_stays_in_the_database(promoted) -> None:
    """A stood-down version is history, not the answer to what is in the hole now."""
    from tests.fixtures.fieldops import fetch

    from drilling_intelligence.database.models import CasingRun

    run = next(row for row in fetch(promoted, CasingRun) if row.is_current)
    with promoted.database.unit_of_work() as session:
        row = session.get(CasingRun, run.id)
        row.is_current = False
        row.status = "SUPERSEDED"

    ids = {unit.source_id for unit in _units(promoted, "casing_run")}
    assert run.id not in ids, "a superseded run is no longer projected"
    assert run.id in {row.id for row in fetch(promoted, CasingRun)}, "but it is still readable"


# --------------------------------------------------------- what the projection says, verbatim


def test_a_fraction_size_is_indexed_as_written(promoted) -> None:
    """``9 5/8`` is what the source said; ``9.625`` would be a unit change nobody asked for."""
    texts = [unit.text for unit in _units(promoted, "casing_run")]
    assert any("size: 9 5/8" in text for text in texts), texts
    assert not any("9.625" in text for text in texts), (
        "the parsed value must never replace the source's own representation"
    )


def test_lead_and_tail_are_separate_lines_and_never_summed(promoted) -> None:
    text = next(unit.text for unit in _units(promoted, "cement_job") if "CMT-02" in unit.text)
    assert "lead volume: 410" in text, text
    assert "tail volume: 260" in text, text
    assert "670" not in text, "no summed volume may appear - the source never stated one"
    assert "lead density: 15.8" in text and "tail density: 16.4" in text, text


def test_top_of_cement_and_shoe_depth_stay_distinguishable(promoted) -> None:
    text = next(unit.text for unit in _units(promoted, "cement_job") if "CMT-02" in unit.text)
    assert "top of cement: 4200" in text, text
    assert "shoe depth: 9200" in text, text


def test_a_total_only_job_projects_no_invented_split(promoted) -> None:
    """A source that stated one number projects one number, not a lead and a tail."""
    text = next(unit.text for unit in _units(promoted, "cement_job") if "CMT-09" in unit.text)
    assert "lead volume" not in text, text
    assert "tail volume" not in text, text


def test_a_field_the_source_left_blank_contributes_no_line(promoted) -> None:
    text = next(unit.text for unit in _units(promoted, "casing_run") if "Conductor" in unit.text)
    assert "casing string: Conductor" in text
    assert "grade: X-52" in text


# ------------------------------------------------------------------------------- provenance


def test_the_projection_carries_its_own_provenance(promoted, rebuilt) -> None:
    """A search hit must still be traceable to the artefact that produced it."""
    hit = next(iter(_hits(rebuilt, "conductor", "casing_run")))
    assert hit.provenance.get("record_type") == "casing_run"
    assert hit.provenance.get("document_id"), "the citation survives into the index"
    assert hit.provenance.get("document_version_id"), "down to the exact version"
    assert hit.provenance.get("evidence"), "the row's own locator survives into the index"
