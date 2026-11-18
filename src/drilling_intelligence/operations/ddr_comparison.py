"""Pure, read-only comparison of two authored DDR revision snapshots.

Activity sequence numbers and database row IDs describe one revision only; neither is a
cross-revision identity. Comparison therefore uses exact operator-authored content as its
conservative anchor, reports reorderings from relative order, and marks non-unique alignments
as ambiguous instead of inventing a pairing. No engineering interpretation is performed.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

_FIELD_KEYS = (
    "report_date",
    "report_date_text",
    "report_number",
    "shift",
    "summary",
)
_ACTIVITY_KEYS = ("operation_type", "label", "description", "period_text")


def compare_ddr_revisions(
    previous: Mapping[str, Any] | Any,
    successor: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Return exact authored-value diffs and explicit malformed/ambiguous diagnostics.

    Missing values are distinct from explicit null/empty values. Activity ``sequence`` is
    validated and retained for display only; matching is based on exact authored text and
    position in the supplied list. Unique exact content is matched independent of sequence.
    A single unmatched predecessor and successor between the same unchanged anchors is a
    conservative one-to-one modification; many-to-many alignment is explicitly ambiguous.
    """
    issues: list[str] = []
    if not isinstance(previous, Mapping) or not isinstance(successor, Mapping):
        return {
            "status": "MALFORMED",
            "read_only": True,
            "approval_evaluated": False,
            "conclusive": False,
            "issues": ["revision history record is not an object"],
            "previous": {},
            "successor": {},
            "fields": [],
            "activities": {"status": "MALFORMED", "items": [], "order_changed": False},
            "has_changes": None,
        }

    fields: list[dict[str, Any]] = []
    for key in _FIELD_KEYS:
        before_present = key in previous
        after_present = key in successor
        before = previous.get(key)
        after = successor.get(key)
        if not before_present and after_present:
            state = "ADDED"
        elif before_present and not after_present:
            state = "REMOVED"
        elif before != after:
            state = "MODIFIED"
        else:
            state = "UNCHANGED"
        fields.append(
            {
                "field": key,
                "status": state,
                "previous_present": before_present,
                "successor_present": after_present,
                "previous": before,
                "successor": after,
            }
        )

    before_activities = _activity_rows(previous.get("activities"), "predecessor", issues)
    after_activities = _activity_rows(successor.get("activities"), "successor", issues)
    items: list[dict[str, Any]] = []
    before_order: list[int] = []
    after_order: list[int] = []
    order_changed = False
    activity_status = "OK"
    if before_activities is None or after_activities is None:
        activity_status = "MALFORMED"
    else:
        before_order = list(range(1, len(before_activities) + 1))
        after_order = list(range(1, len(after_activities) + 1))
        items = _align_activities(before_activities, after_activities)
        order_changed = any(item["status"] == "REORDERED" for item in items)
        if any(item["status"] == "AMBIGUOUS" for item in items):
            activity_status = "AMBIGUOUS"

    changed_statuses = {"ADDED", "REMOVED", "MODIFIED", "REORDERED"}
    definitely_changed = any(item["status"] in changed_statuses for item in fields) or any(
        item["status"] in changed_statuses for item in items
    )
    ambiguous = any(item["status"] == "AMBIGUOUS" for item in items)
    changed: bool | None = True if definitely_changed else None if issues or ambiguous else False
    result_status = "MALFORMED" if issues else "AMBIGUOUS" if ambiguous else "OK"
    counts = Counter(item["status"].lower() for item in items)
    ambiguous_activity_count = sum(
        max(len(item.get("previous") or ()), len(item.get("successor") or ()))
        for item in items
        if item["status"] == "AMBIGUOUS"
    )
    if ambiguous_activity_count:
        counts["ambiguous"] = ambiguous_activity_count
    return {
        "status": result_status,
        "read_only": True,
        "approval_evaluated": False,
        "conclusive": result_status == "OK",
        "previous": {
            "report_id": previous.get("report_id"),
            "revision_number": previous.get("revision_number"),
            "is_current": previous.get("is_current"),
            "status": previous.get("status"),
            "authoring_state": previous.get("authoring_state"),
        },
        "successor": {
            "report_id": successor.get("report_id"),
            "revision_number": successor.get("revision_number"),
            "is_current": successor.get("is_current"),
            "status": successor.get("status"),
            "authoring_state": successor.get("authoring_state"),
        },
        "fields": fields,
        "activities": {
            "status": activity_status,
            "items": items,
            # These are values for the authored-list positions, not matching keys.
            "predecessor_order": before_order if before_activities is not None else [],
            "successor_order": after_order if after_activities is not None else [],
            "order_changed": order_changed,
            "counts": dict(sorted(counts.items())),
            "ambiguous_groups": sum(item["status"] == "AMBIGUOUS" for item in items),
        },
        "issues": issues,
        "has_changes": changed,
    }


def _align_activities(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    before_signatures = [_activity_values(item) for item in before]
    after_signatures = [_activity_values(item) for item in after]
    before_positions: dict[tuple[str, ...], list[int]] = defaultdict(list)
    after_positions: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for index, signature in enumerate(before_signatures):
        before_positions[signature].append(index)
    for index, signature in enumerate(after_signatures):
        after_positions[signature].append(index)

    result: list[dict[str, Any]] = []
    used_before: set[int] = set()
    used_after: set[int] = set()
    duplicate_signatures = {
        signature
        for signature in before_positions.keys() | after_positions.keys()
        if len(before_positions.get(signature, ())) > 1
        or len(after_positions.get(signature, ())) > 1
    }
    for signature in sorted(duplicate_signatures):
        before_indices = before_positions.get(signature, [])
        after_indices = after_positions.get(signature, [])
        used_before.update(before_indices)
        used_after.update(after_indices)
        result.append(
            _ambiguous_item(
                before,
                after,
                before_indices,
                after_indices,
                "repeated_content_has_no_durable_identity",
            )
        )

    unique_matches = [
        (before_positions[signature][0], after_positions[signature][0], signature)
        for signature in before_positions.keys() & after_positions.keys()
        if signature not in duplicate_signatures
    ]
    unique_matches.sort(key=lambda match: match[0])
    moved = _matches_in_order_inversions(unique_matches)
    for index, (before_index, after_index, _signature) in enumerate(unique_matches):
        used_before.add(before_index)
        used_after.add(after_index)
        state = "REORDERED" if index in moved else "UNCHANGED"
        result.append(
            _single_item(
                before,
                after,
                state,
                before_index,
                after_index,
                match_basis="exact_unique_authored_content",
                reason="relative_order_changed" if state == "REORDERED" else None,
            )
        )

    # Only common exact items whose relative order is stable are safe segment anchors.
    anchors = [match for index, match in enumerate(unique_matches) if index not in moved]
    before_anchor_positions = [match[0] for match in anchors]
    after_anchor_positions = [match[1] for match in anchors]
    before_anchor_signatures = [match[2] for match in anchors]
    after_anchor_signatures = [match[2] for match in anchors]
    before_gaps: dict[tuple[Any, Any], list[int]] = defaultdict(list)
    after_gaps: dict[tuple[Any, Any], list[int]] = defaultdict(list)
    for index in range(len(before)):
        if index not in used_before:
            before_gaps[_gap_key(index, before_anchor_positions, before_anchor_signatures)].append(
                index
            )
    for index in range(len(after)):
        if index not in used_after:
            after_gaps[_gap_key(index, after_anchor_positions, after_anchor_signatures)].append(
                index
            )

    gap_keys = list(before_gaps)
    gap_keys.extend(key for key in after_gaps if key not in before_gaps)
    for key in gap_keys:
        before_indices = before_gaps.get(key, [])
        after_indices = after_gaps.get(key, [])
        if not before_indices:
            result.extend(
                _single_item(before, after, "ADDED", None, index) for index in after_indices
            )
        elif not after_indices:
            result.extend(
                _single_item(before, after, "REMOVED", index, None) for index in before_indices
            )
        elif len(before_indices) == len(after_indices) == 1:
            before_index, after_index = before_indices[0], after_indices[0]
            changed_fields = [
                name
                for name, left, right in zip(
                    _ACTIVITY_KEYS,
                    before_signatures[before_index],
                    after_signatures[after_index],
                    strict=True,
                )
                if left != right
            ]
            result.append(
                _single_item(
                    before,
                    after,
                    "MODIFIED",
                    before_index,
                    after_index,
                    match_basis="single_unmatched_pair_between_same_anchors",
                    changed_fields=changed_fields,
                )
            )
        else:
            result.append(
                _ambiguous_item(
                    before,
                    after,
                    before_indices,
                    after_indices,
                    "multiple_unmatched_items_between_same_unchanged_anchors",
                )
            )

    return sorted(
        result,
        key=lambda item: (
            min(item["previous_positions"] or item["successor_positions"] or [float("inf")]),
            min(item["successor_positions"] or [float("inf")]),
            item["status"],
        ),
    )


def _matches_in_order_inversions(matches: list[tuple[int, int, tuple[str, ...]]]) -> set[int]:
    """Return every matched item participating in a relative-order inversion in O(n)."""
    if len(matches) < 2:
        return set()
    successor_positions = [match[1] for match in matches]
    suffix_min = [0] * len(matches)
    minimum = successor_positions[-1]
    for index in range(len(matches) - 1, -1, -1):
        minimum = min(minimum, successor_positions[index])
        suffix_min[index] = minimum
    moved: set[int] = set()
    prefix_max = -1
    for index, successor_position in enumerate(successor_positions):
        participates_in_inversion = prefix_max > successor_position
        if index + 1 < len(matches):
            participates_in_inversion = (
                participates_in_inversion or suffix_min[index + 1] < successor_position
            )
        if participates_in_inversion:
            moved.add(index)
        prefix_max = max(prefix_max, successor_position)
    return moved


def _gap_key(
    index: int,
    anchor_positions: list[int],
    anchor_signatures: list[tuple[str, ...]],
) -> tuple[Any, Any]:
    next_anchor = bisect_right(anchor_positions, index)
    before = anchor_signatures[next_anchor - 1] if next_anchor else None
    after = anchor_signatures[next_anchor] if next_anchor < len(anchor_signatures) else None
    return before, after


def _single_item(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    status: str,
    before_index: int | None,
    after_index: int | None,
    *,
    match_basis: str | None = None,
    reason: str | None = None,
    changed_fields: list[str] | None = None,
) -> dict[str, Any]:
    previous_positions = [before_index + 1] if before_index is not None else []
    successor_positions = [after_index + 1] if after_index is not None else []
    item: dict[str, Any] = {
        "status": status,
        "previous_positions": previous_positions,
        "successor_positions": successor_positions,
        "previous": before[before_index] if before_index is not None else None,
        "successor": after[after_index] if after_index is not None else None,
    }
    if match_basis:
        item["match_basis"] = match_basis
    if reason:
        item["reason"] = reason
    if changed_fields:
        item["changed_fields"] = changed_fields
    return item


def _ambiguous_item(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    before_indices: list[int],
    after_indices: list[int],
    reason: str,
) -> dict[str, Any]:
    return {
        "status": "AMBIGUOUS",
        "reason": reason,
        "previous_positions": [index + 1 for index in before_indices],
        "successor_positions": [index + 1 for index in after_indices],
        "previous": [before[index] for index in before_indices],
        "successor": [after[index] for index in after_indices],
    }


def _activity_rows(value: Any, label: str, issues: list[str]) -> list[dict[str, Any]] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        issues.append(f"{label} activities are not an ordered list")
        return None
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            issues.append(f"{label} activity {index + 1} is not an object")
            return None
        sequence = raw.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            issues.append(f"{label} activity {index + 1} has an invalid sequence")
            return None
        if sequence in seen:
            issues.append(f"{label} activities repeat sequence {sequence}")
            return None
        if any(not isinstance(raw.get(key), str) for key in _ACTIVITY_KEYS):
            issues.append(f"{label} activity {index + 1} has malformed operator text")
            return None
        seen.add(sequence)
        rows.append({"sequence": sequence, **{key: raw.get(key) for key in _ACTIVITY_KEYS}})
    return rows


def _activity_values(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(key)) for key in _ACTIVITY_KEYS)


__all__ = ["compare_ddr_revisions"]
