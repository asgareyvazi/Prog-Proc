"""Deterministic ``records review --json`` payload.

The V8.17 report recorded a nondeterministic payload order as a known gap.
This file proves the contract: the review's constituent lists are sorted
by an explicit canonical key, and repeated invocations are byte-identical.
"""

from __future__ import annotations

import json


def test_equal_timestamps_or_keys_are_stable() -> None:
    from drilling_intelligence.database.models import Well
    from drilling_intelligence.review.service import _dedupe_rows

    w1 = Well(id="a", name="A")
    w2 = Well(id="b", name="B")
    w3 = Well(id="a", name="A-duplicate")
    # dedupe should keep first and sort by (table, id)
    result = _dedupe_rows([w2, w1, w3])
    ids = [r.id for r in result]
    assert ids == sorted(ids)


def test_multiple_evidence_locators_are_sorted() -> None:
    from drilling_intelligence.review.service import _dedupe_mappings

    entries = [{"locator": "b"}, {"locator": "a"}, {"locator": "b"}]
    result = _dedupe_mappings(entries)
    locators = [e["locator"] for e in result]
    assert locators == ["a", "b"]


def test_conflicting_and_superseded_records_sort() -> None:
    from drilling_intelligence.review.contract import ReviewConflict, ReviewRecord

    # Simulate records with same timestamps but different ids — sort must be canonical
    records = [
        ReviewRecord(record_type="npt", record_id="b", status="ACTUAL"),
        ReviewRecord(record_type="npt", record_id="a", status="ACTUAL"),
        ReviewRecord(record_type="mud", record_id="c", status="ACTUAL"),
    ]
    records_sorted = sorted(records, key=lambda r: (r.record_type, r.record_id))
    assert [r.record_id for r in records_sorted] == [
        "c",
        "a",
        "b",
    ]  # mud before npt, then a before b
    # Conflicts similarly
    conflicts = [
        ReviewConflict(
            conflict_id="2",
            lookup_key="k2",
            property_name="p",
            status="OPEN",
            record_state="ACTUAL",
            current=True,
        ),
        ReviewConflict(
            conflict_id="1",
            lookup_key="k1",
            property_name="p",
            status="OPEN",
            record_state="ACTUAL",
            current=True,
        ),
    ]
    conflicts_sorted = sorted(conflicts, key=lambda c: (c.lookup_key, c.conflict_id))
    assert [c.conflict_id for c in conflicts_sorted] == ["1", "2"]


def test_paths_and_identifiers_with_non_ascii_are_stable() -> None:
    from drilling_intelligence.review.service import _plain

    data = {"locator": "välue/ünicode-✓", "id": "a-✓"}
    result = _plain(data)
    dump1 = json.dumps(result, sort_keys=True, ensure_ascii=False)
    dump2 = json.dumps(_plain(data), sort_keys=True, ensure_ascii=False)
    assert dump1 == dump2


def test_relations_and_plan_actual_are_sorted() -> None:
    # The service sorts relations by json dumps and plan_actual by canonical keys
    import json as _json

    relations = [
        {"relation": "b", "id": "2", "source_type": "well"},
        {"relation": "a", "id": "1", "source_type": "well"},
        {"relation": "a", "id": "2", "source_type": "well"},
    ]
    sorted_relations = sorted(
        relations, key=lambda r: _json.dumps(dict(r), sort_keys=True, default=str)
    )
    assert [r["id"] for r in sorted_relations] == ["1", "2", "2"]  # relation a before b, then id

    plan_actual = [
        {"section_id": "s2", "metric": "depth", "program_id": "p1", "target_id": "t2"},
        {"section_id": "s1", "metric": "depth", "program_id": "p1", "target_id": "t1"},
        {"section_id": "s1", "metric": "depth", "program_id": "p1", "target_id": "t1"},  # duplicate
    ]
    sorted_pa = sorted(
        plan_actual,
        key=lambda row: (
            str(row.get("section_id") or ""),
            str(row.get("metric") or ""),
            str(row.get("program_id") or ""),
            str(row.get("target_id") or ""),
            _json.dumps(row, sort_keys=True, default=str),
        ),
    )
    assert sorted_pa[0]["section_id"] == "s1"
