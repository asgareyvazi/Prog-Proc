"""Read-only DDR revision diff semantics, including conservative activity alignment."""

from __future__ import annotations

from copy import deepcopy

from drilling_intelligence.operations.ddr_comparison import compare_ddr_revisions


def _activity(sequence: int, label: str, *, description: str | None = None) -> dict[str, object]:
    return {
        "sequence": sequence,
        "operation_type": "drilling",
        "label": label,
        "description": description if description is not None else f"Operator text: {label}",
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


def _statuses(result: dict[str, object]) -> list[str]:
    return [str(item["status"]) for item in result["activities"]["items"]]  # type: ignore[index]


def test_exact_fields_and_authored_activity_text_are_preserved_without_mutation() -> None:
    previous = _record(
        "r1",
        [
            _activity(1, "Start – 12¼ in"),
            _activity(2, "Pump check", description="Operator's exact wording\nsecond line"),
            _activity(3, "End"),
        ],
    )
    successor = _record(
        "r2",
        [
            _activity(7, "Start – 12¼ in"),
            _activity(8, "Pump check", description="Operator's corrected wording — Δ"),
            _activity(9, "End"),
        ],
    )
    successor["report_number"] = "DDR-08A"
    before_snapshot, after_snapshot = deepcopy(previous), deepcopy(successor)

    result = compare_ddr_revisions(previous, successor)

    fields = {item["field"]: item for item in result["fields"]}
    assert fields["report_number"]["status"] == "MODIFIED"
    assert fields["summary"]["status"] == "UNCHANGED"
    activities = result["activities"]
    assert _statuses(result) == ["UNCHANGED", "MODIFIED", "UNCHANGED"]
    changed = activities["items"][1]
    assert changed["previous_positions"] == [2]
    assert changed["successor_positions"] == [2]
    assert changed["changed_fields"] == ["description"]
    assert changed["previous"]["description"] == "Operator's exact wording\nsecond line"
    assert changed["successor"]["description"] == "Operator's corrected wording — Δ"
    assert activities["order_changed"] is False
    assert result["read_only"] is True
    assert result["approval_evaluated"] is False
    assert result["has_changes"] is True
    assert result["conclusive"] is True
    assert previous == before_snapshot and successor == after_snapshot


def test_middle_insertion_does_not_turn_later_items_into_modifications() -> None:
    previous = _record("r1", [_activity(1, "First"), _activity(2, "Last")])
    successor = _record(
        "r2", [_activity(1, "First"), _activity(2, "Inserted"), _activity(3, "Last")]
    )

    result = compare_ddr_revisions(previous, successor)

    assert _statuses(result) == ["UNCHANGED", "ADDED", "UNCHANGED"]
    assert result["activities"]["items"][1]["successor"]["label"] == "Inserted"
    assert result["activities"]["order_changed"] is False
    assert result["has_changes"] is True


def test_activity_row_id_and_timestamps_are_not_used_as_stable_identity() -> None:
    previous = _record("r1", [_activity(1, "Same authored text")])
    successor = _record("r2", [_activity(88, "Same authored text")])
    previous["activities"][0].update(
        {
            "id": "old-row-id",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }
    )
    successor["activities"][0].update(
        {
            "id": "new-row-id",
            "created_at": "2026-11-01T00:00:00Z",
            "updated_at": "2026-11-02T00:00:00Z",
        }
    )

    result = compare_ddr_revisions(previous, successor)

    assert _statuses(result) == ["UNCHANGED"]
    assert result["activities"]["items"][0]["previous"]["label"] == "Same authored text"
    assert result["activities"]["items"][0]["successor"]["label"] == "Same authored text"


def test_removal_and_insertion_are_classified_by_content_not_sequence_number() -> None:
    previous = _record("r1", [_activity(1, "First"), _activity(2, "Removed"), _activity(3, "Last")])
    successor = _record("r2", [_activity(40, "First"), _activity(41, "Last")])

    result = compare_ddr_revisions(previous, successor)

    assert _statuses(result) == ["UNCHANGED", "REMOVED", "UNCHANGED"]
    assert result["activities"]["items"][1]["previous"]["label"] == "Removed"
    assert result["activities"]["items"][1]["successor"] is None


def test_unique_content_reordering_is_reported_without_claiming_a_specific_moved_identity() -> None:
    previous = _record("r1", [_activity(1, "First"), _activity(2, "Second"), _activity(3, "Third")])
    successor = _record(
        "r2", [_activity(1, "First"), _activity(2, "Third"), _activity(3, "Second")]
    )

    result = compare_ddr_revisions(previous, successor)

    assert result["activities"]["order_changed"] is True
    by_label = {
        item["previous"]["label"]: item
        for item in result["activities"]["items"]
        if item["previous"] is not None
    }
    assert by_label["First"]["status"] == "UNCHANGED"
    assert by_label["Second"]["status"] == "REORDERED"
    assert by_label["Third"]["status"] == "REORDERED"
    assert all(by_label[name]["reason"] == "relative_order_changed" for name in ("Second", "Third"))


def test_repeated_exact_content_is_explicitly_ambiguous_even_when_counts_match() -> None:
    previous = _record("r1", [_activity(1, "Repeated"), _activity(2, "Repeated")])
    successor = _record("r2", [_activity(8, "Repeated"), _activity(9, "Repeated")])

    result = compare_ddr_revisions(previous, successor)

    assert result["status"] == "AMBIGUOUS"
    assert result["conclusive"] is False
    assert result["has_changes"] is None
    assert result["activities"]["status"] == "AMBIGUOUS"
    assert len(result["activities"]["items"]) == 1
    group = result["activities"]["items"][0]
    assert group["status"] == "AMBIGUOUS"
    assert group["reason"] == "repeated_content_has_no_durable_identity"
    assert group["previous_positions"] == [1, 2]
    assert group["successor_positions"] == [1, 2]
    assert len(group["previous"]) == len(group["successor"]) == 2


def test_repeated_content_count_change_is_not_guessed_as_a_specific_removal() -> None:
    previous = _record("r1", [_activity(1, "Repeated"), _activity(2, "Repeated")])
    successor = _record("r2", [_activity(1, "Repeated")])

    result = compare_ddr_revisions(previous, successor)

    assert result["status"] == "AMBIGUOUS"
    item = result["activities"]["items"][0]
    assert item["status"] == "AMBIGUOUS"
    assert len(item["previous"]) == 2
    assert len(item["successor"]) == 1


def test_multiple_unmatched_entries_between_anchors_are_ambiguous_not_fuzzy_paired() -> None:
    previous = _record(
        "r1",
        [
            _activity(1, "Anchor A"),
            _activity(2, "Old one"),
            _activity(3, "Old two"),
            _activity(4, "Anchor B"),
        ],
    )
    successor = _record(
        "r2",
        [_activity(1, "Anchor A"), _activity(2, "New one"), _activity(3, "Anchor B")],
    )

    result = compare_ddr_revisions(previous, successor)

    assert result["status"] == "AMBIGUOUS"
    group = next(item for item in result["activities"]["items"] if item["status"] == "AMBIGUOUS")
    assert group["reason"] == "multiple_unmatched_items_between_same_unchanged_anchors"
    assert [row["label"] for row in group["previous"]] == ["Old one", "Old two"]
    assert [row["label"] for row in group["successor"]] == ["New one"]
    assert result["activities"]["items"][0]["status"] == "UNCHANGED"
    assert result["activities"]["items"][-1]["status"] == "UNCHANGED"


def test_field_added_removed_and_invalid_history_are_reported_without_a_guess() -> None:
    valid = _record("r1", [_activity(1, "Existing operator text")])
    added = _record("r2", [_activity(1, "Existing operator text")])
    added["report_number"] = None
    result = compare_ddr_revisions(valid, added)
    assert {item["field"]: item["status"] for item in result["fields"]}[
        "report_number"
    ] == "MODIFIED"

    removed = _record("r2", [_activity(1, "Existing operator text")])
    del removed["shift"]
    result = compare_ddr_revisions(valid, removed)
    assert {item["field"]: item["status"] for item in result["fields"]}["shift"] == "REMOVED"

    for bad in (
        {},
        {**valid, "activities": None},
        {**valid, "activities": ["not-an-object"]},
        {**valid, "activities": [_activity(0, "bad sequence")]},
        {**valid, "activities": [_activity(1, "one"), _activity(1, "duplicate sequence")]},
        {**valid, "activities": [{"sequence": 1, "label": 42}]},
    ):
        malformed = compare_ddr_revisions(valid, bad)
        assert malformed["status"] == "MALFORMED"
        assert malformed["activities"]["status"] == "MALFORMED"
        assert malformed["has_changes"] is (True if not bad else None)
        assert malformed["issues"]
        assert malformed["conclusive"] is False
        assert malformed["approval_evaluated"] is False

    non_object = compare_ddr_revisions(None, valid)
    assert non_object["status"] == "MALFORMED"
    assert "not an object" in non_object["issues"][0]


def test_large_history_is_compared_without_truncating_the_result() -> None:
    previous_rows = [_activity(index, f"Operator line {index}") for index in range(1, 401)]
    successor_rows = deepcopy(previous_rows)
    for index, row in enumerate(successor_rows, start=1):
        row["sequence"] = index + 700
    successor_rows[-1]["label"] = "Corrected final line — Δ"
    successor_rows[-1]["description"] = "Operator text: Corrected final line — Δ"

    result = compare_ddr_revisions(_record("r1", previous_rows), _record("r2", successor_rows))

    assert len(result["activities"]["items"]) == 400
    assert result["activities"]["items"][-1]["status"] == "MODIFIED"
    assert result["activities"]["items"][-1]["successor"]["label"] == "Corrected final line — Δ"
