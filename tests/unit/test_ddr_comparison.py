"""Read-only DDR revision diff semantics, including malformed and large history."""

from __future__ import annotations

from copy import deepcopy

from drilling_intelligence.operations.ddr_comparison import compare_ddr_revisions


def _activity(sequence: int, label: str) -> dict[str, object]:
    return {
        "sequence": sequence,
        "operation_type": "drilling",
        "label": label,
        "description": f"Operator text: {label}",
        "period_text": "06:00–18:00",
    }


def _record(report_id: str, activities: list[dict[str, object]]) -> dict[str, object]:
    return {
        "report_id": report_id,
        "revision_number": 1 if report_id == "r1" else 2,
        "is_current": report_id == "r2",
        "status": "CANDIDATE",
        "authoring_state": "DRAFT",
        "report_date": "2026-11-08",
        "report_date_text": "2026-11-08",
        "report_number": "DDR-08",
        "shift": "Night",
        "summary": "Operator summary — exact wording",
        "activities": activities,
    }


def test_diff_classifies_field_and_activity_changes_without_reordering_or_mutation() -> None:
    previous = _record("r1", [_activity(1, "Drilling – 12¼ in"), _activity(2, "Pump check")])
    successor = _record(
        "r2", [_activity(1, "Drilling – corrected 12¼ in"), _activity(3, "Trip out")]
    )
    successor["report_number"] = "DDR-08A"
    before_snapshot, after_snapshot = deepcopy(previous), deepcopy(successor)

    result = compare_ddr_revisions(previous, successor)

    fields = {item["field"]: item for item in result["fields"]}
    assert fields["report_number"]["status"] == "MODIFIED"
    assert fields["summary"]["status"] == "UNCHANGED"
    activities = result["activities"]
    assert [(item["sequence"], item["status"]) for item in activities["items"]] == [
        (1, "MODIFIED"),
        (2, "REMOVED"),
        (3, "ADDED"),
    ]
    assert activities["predecessor_order"] == [1, 2]
    assert activities["successor_order"] == [1, 3]
    assert activities["order_changed"] is False
    assert result["read_only"] is True
    assert result["approval_evaluated"] is False
    assert result["has_changes"] is True
    assert previous == before_snapshot and successor == after_snapshot
    assert result["activities"]["items"][0]["successor"]["label"] == "Drilling – corrected 12¼ in"


def test_diff_marks_added_removed_fields_and_activity_sequence_order_changes() -> None:
    previous = _record("r1", [_activity(1, "First"), _activity(2, "Second")])
    successor = _record("r2", [_activity(2, "Second"), _activity(1, "First")])
    del successor["shift"]
    result = compare_ddr_revisions(previous, successor)

    fields = {item["field"]: item for item in result["fields"]}
    assert fields["shift"]["status"] == "REMOVED"
    assert result["activities"]["order_changed"] is True
    assert result["has_changes"] is True

    added = _record("r3", [_activity(1, "First"), _activity(2, "Second"), _activity(3, "Third")])
    added.pop("report_number")
    result = compare_ddr_revisions(previous, added)
    assert {item["field"]: item["status"] for item in result["fields"]}[
        "report_number"
    ] == "REMOVED"
    assert result["activities"]["items"][-1]["status"] == "ADDED"


def test_missing_and_malformed_history_returns_diagnostics_not_a_guess() -> None:
    valid = _record("r1", [_activity(1, "Existing operator text")])
    for bad in (
        {},
        {**valid, "activities": None},
        {**valid, "activities": ["not-an-object"]},
        {**valid, "activities": [_activity(0, "bad sequence")]},
        {**valid, "activities": [_activity(1, "one"), _activity(1, "duplicate")]},
        {**valid, "activities": [{"sequence": 1, "label": 42}]},
    ):
        result = compare_ddr_revisions(valid, bad)
        assert result["status"] == "MALFORMED"
        assert result["activities"]["status"] == "MALFORMED"
        assert result["issues"]
        assert result["approval_evaluated"] is False

    non_object = compare_ddr_revisions(None, valid)
    assert non_object["status"] == "MALFORMED"
    assert "not an object" in non_object["issues"][0]


def test_large_history_is_compared_without_truncating_the_result() -> None:
    previous_rows = [_activity(index, f"Operator line {index}") for index in range(1, 401)]
    successor_rows = deepcopy(previous_rows)
    successor_rows[-1]["label"] = "Corrected final line — Δ"
    result = compare_ddr_revisions(_record("r1", previous_rows), _record("r2", successor_rows))

    assert len(result["activities"]["items"]) == 400
    assert result["activities"]["items"][-1]["status"] == "MODIFIED"
    assert result["activities"]["items"][-1]["successor"]["label"] == "Corrected final line — Δ"
