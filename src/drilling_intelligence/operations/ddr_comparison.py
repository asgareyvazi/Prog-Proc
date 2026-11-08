"""Pure, read-only comparison of two authored DDR revision snapshots.

This module only compares the supplied display records. It does not open a
workspace, write an audit event, decide review status, or derive engineering
values. Activity sequence is kept in authored order; IDs are intentionally not
used because a revision receives new activity row IDs when copied.
"""

from __future__ import annotations

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
    """Return exact value diffs with explicit malformed-history diagnostics.

    Missing values are distinct from explicit null/empty values. Activities are
    matched by their authored sequence number, and the input order is retained.
    The result includes the full change list; UI callers may bound rendering but
    must identify any omitted rows to the operator.
    """
    issues: list[str] = []
    if not isinstance(previous, Mapping) or not isinstance(successor, Mapping):
        return {
            "status": "MALFORMED",
            "read_only": True,
            "approval_evaluated": False,
            "issues": ["revision history record is not an object"],
            "previous": {},
            "successor": {},
            "fields": [],
            "activities": {"status": "MALFORMED", "items": [], "order_changed": False},
            "has_changes": False,
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
    activity_changes: list[dict[str, Any]] = []
    before_order: list[int] = []
    after_order: list[int] = []
    order_changed = False
    activity_status = "OK"
    if before_activities is None or after_activities is None:
        activity_status = "MALFORMED"
    else:
        before_by_sequence = {int(item["sequence"]): item for item in before_activities}
        after_by_sequence = {int(item["sequence"]): item for item in after_activities}
        before_order = [int(item["sequence"]) for item in before_activities]
        after_order = [int(item["sequence"]) for item in after_activities]
        common = set(before_order) & set(after_order)
        order_changed = [value for value in before_order if value in common] != [
            value for value in after_order if value in common
        ]
        for sequence in before_order:
            before = before_by_sequence[sequence]
            after = after_by_sequence.get(sequence)
            if after is None:
                activity_changes.append(
                    {
                        "sequence": sequence,
                        "status": "REMOVED",
                        "previous": before,
                        "successor": None,
                    }
                )
                continue
            state = (
                "MODIFIED" if _activity_values(before) != _activity_values(after) else "UNCHANGED"
            )
            activity_changes.append(
                {"sequence": sequence, "status": state, "previous": before, "successor": after}
            )
        for sequence in after_order:
            if sequence not in before_by_sequence:
                activity_changes.append(
                    {
                        "sequence": sequence,
                        "status": "ADDED",
                        "previous": None,
                        "successor": after_by_sequence[sequence],
                    }
                )

    changed = (
        any(item["status"] != "UNCHANGED" for item in fields)
        or any(item["status"] != "UNCHANGED" for item in activity_changes)
        or order_changed
    )
    return {
        "status": "MALFORMED" if issues else "OK",
        "read_only": True,
        "approval_evaluated": False,
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
            "items": activity_changes,
            "predecessor_order": before_order if before_activities is not None else [],
            "successor_order": after_order if after_activities is not None else [],
            "order_changed": order_changed,
            "counts": {
                state.lower(): sum(item["status"] == state for item in activity_changes)
                for state in ("UNCHANGED", "ADDED", "REMOVED", "MODIFIED")
            },
        },
        "issues": issues,
        "has_changes": changed,
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


def _activity_values(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(key) for key in _ACTIVITY_KEYS)


__all__ = ["compare_ddr_revisions"]
