"""V7.2 on the timeline: their own kinds, the source's own dates, and a total order.

Neither domain is folded into ``event``.  A well-control event and an HSE incident are different
things to a reader, and collapsing them would make the kind column useless for exactly the two
domains this wave added.  Neither gains a timestamp it was not given: an unparsable date stays
undated with its wording, rather than being ordered by a date invented from the file or the report.
"""

from __future__ import annotations

from tests.fixtures.fieldops import ingest_v72, promote_file, well_id_for

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.intelligence.timeline import (
    TIMELINE_KINDS,
    build_timeline,
    entry_comparator,
)

WC_LOG = "well_control_log_well-a3.xlsx"
HSE_REGISTER = "hse_register_well-a3.xlsx"
HSE_SITE = "hse_site_register.xlsx"


def _timeline(workspace, **kwargs):
    with workspace.database.read_only() as session:
        return build_timeline(session, **kwargs)


def test_both_domains_have_their_own_kind(workspace) -> None:
    assert "well_control" in TIMELINE_KINDS
    assert "hse" in TIMELINE_KINDS
    assert "event" in TIMELINE_KINDS, "the generic kind still exists and was not reused"


def test_well_control_and_hse_rows_appear_on_the_well_timeline(workspace) -> None:
    ingest_v72(workspace)
    for file_name in (WC_LOG, HSE_REGISTER):
        promote_file(workspace, file_name)
    entries = _timeline(workspace, well_id=well_id_for(workspace, "A-3"))

    wc = [entry for entry in entries if entry.kind == "well_control"]
    hse = [entry for entry in entries if entry.kind == "hse"]
    assert len(wc) == 5, [entry.title for entry in wc]
    assert len(hse) == 3, [entry.title for entry in hse]
    # HSE-104 names B-11 and was refused at promotion, so it has no timeline entry either.
    assert not any("HSE-104" in entry.title for entry in entries)
    assert all(entry.table == "well_control_event" for entry in wc)
    assert all(entry.table == "hse_incident" for entry in hse)


def test_timestamps_come_from_the_source_and_nothing_else(workspace) -> None:
    ingest_v72(workspace)
    for file_name in (WC_LOG, HSE_REGISTER):
        promote_file(workspace, file_name)
    entries = [
        entry
        for entry in _timeline(workspace, well_id=well_id_for(workspace, "A-3"))
        if entry.kind in ("well_control", "hse")
    ]
    dated = {entry.title: entry.at for entry in entries}
    assert str(dated["WC-01 - kick"].date()) == "2026-03-14"
    assert str(dated["HSE-101 - near miss"].date()) == "2026-03-20"


def test_an_unparsable_date_stays_undated_and_keeps_its_wording(workspace) -> None:
    """No timestamp is manufactured from the filename, the mtime or the report title."""
    ingest_v72(workspace)
    promote_file(workspace, "well_control_no_units_well-a3.xlsx")
    entries = [
        entry
        for entry in _timeline(workspace, well_id=well_id_for(workspace, "A-3"))
        if entry.kind == "well_control"
    ]
    undated = [entry for entry in entries if entry.at is None]
    assert undated, "WC-N2's date is unparsable, so it must not be given one"
    assert any(entry.text == "later that tour" for entry in undated), [
        entry.text for entry in undated
    ]


def test_site_hse_keeps_an_empty_well_on_a_project_timeline(workspace) -> None:
    """A camp incident reaches a project timeline without being handed a well it never named."""
    ingest_v72(workspace)
    promote_file(workspace, HSE_SITE)
    with workspace.database.read_only() as session:
        from sqlalchemy import select

        from drilling_intelligence.database.models import Document

        document = session.scalars(select(Document).where(Document.filename == HSE_SITE)).first()
        project_id = str(document.project_id)

    entries = [
        entry
        for entry in _timeline(workspace, project_id=project_id)
        if entry.kind == "hse" and entry.title.startswith("SITE-")
    ]
    assert len(entries) == 3, [entry.title for entry in entries]
    assert all(entry.well_id == "" for entry in entries), "no well was invented"
    assert any("Camp" in entry.text for entry in entries)
    assert any("Access road" in entry.text for entry in entries)


def test_ordering_is_total_and_reproducible_on_equal_timestamps(workspace) -> None:
    ingest_v72(workspace)
    for file_name in (WC_LOG, HSE_REGISTER):
        promote_file(workspace, file_name)
    well = well_id_for(workspace, "A-3")
    first = _timeline(workspace, well_id=well)
    second = _timeline(workspace, well_id=well)
    assert [entry_comparator(entry) for entry in first] == [
        entry_comparator(entry) for entry in second
    ]
    # The comparator is a total order: no two distinct entries tie on every component.
    keys = [entry_comparator(entry) for entry in first]
    assert len(set(keys)) == len(keys), "two entries tie completely, so the order is not total"


def test_a_timeline_needs_a_scope(workspace) -> None:
    ingest_v72(workspace)
    promote_file(workspace, HSE_SITE)
    try:
        _timeline(workspace)
    except ValidationError as error:
        assert "scope" in str(error).lower()
    else:  # pragma: no cover - the guard is the point of the test
        raise AssertionError("an unscoped timeline should be refused")
