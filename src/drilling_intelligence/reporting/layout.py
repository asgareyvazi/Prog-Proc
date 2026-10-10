"""Shared chart geometry for SVG and the native painter.

This module is a drawing specification, not a calculation. It places bars only
where an exhibit already marked a point plottable. A null value is never given
a bar, and it is never treated as zero when the axis is scaled.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .contract import EXHIBIT_RENDERED
from .format import format_number, format_value, is_number

HEADER = 64
ROW_H = 32
GROUP_ROW_H = 44
PLOT_W = 460
RIGHT = 88
MIN_LABEL_W = 168
MAX_LABEL_W = 360


def _num(value: float) -> float:
    rounded = round(float(value) + 0.0, 2)
    return 0.0 if rounded == 0.0 else rounded


def _points(exhibit: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    series = list(exhibit.get("series") or [])
    if not series:
        for subject in exhibit.get("subjects") or []:
            rows.append(
                {
                    "subject_id": str(subject.get("id") or ""),
                    "name": str(subject.get("name") or subject.get("id") or ""),
                    "series_id": "",
                    "series_label": "",
                    "value": None,
                    "raw_value": None,
                    "unit": exhibit.get("unit"),
                    "value_state": "",
                    "plottable": False,
                    "missing": True,
                }
            )
        return rows
    for item in series:
        label = str(item.get("label") or item.get("series_id") or "")
        for point in item.get("points") or []:
            rows.append(
                {
                    "subject_id": str(point.get("subject_id") or ""),
                    "name": str(point.get("name") or point.get("subject_id") or ""),
                    "series_id": str(item.get("series_id") or ""),
                    "series_label": label,
                    "value": point.get("value"),
                    "raw_value": point.get("raw_value"),
                    "unit": point.get("unit")
                    if point.get("unit") is not None
                    else item.get("unit"),
                    "value_state": str(point.get("value_state") or ""),
                    "plottable": bool(point.get("plottable")) and is_number(point.get("value")),
                    "missing": point.get("raw_value") is None,
                }
            )
    return rows


def _subject_order(exhibit: Mapping[str, Any], points: Sequence[Mapping[str, Any]]) -> list[str]:
    ordered = [str(subject.get("id") or "") for subject in exhibit.get("subjects") or []]
    if ordered:
        return ordered
    seen: list[str] = []
    for point in points:
        subject_id = str(point.get("subject_id") or "")
        if subject_id not in seen:
            seen.append(subject_id)
    return seen


def layout_exhibit(exhibit: Mapping[str, Any] | None) -> dict[str, Any]:
    """Plain drawing instructions. Coordinates are deterministic; values are not rescaled into new units."""
    if not exhibit:
        return {
            "width": 760,
            "height": 120,
            "state": "NO_DATA",
            "title": "",
            "unit": None,
            "reason": "no exhibit",
            "bars": [],
            "markers": [],
            "labels": [],
            "legend": [],
            "axis_min": 0.0,
            "axis_max": 1.0,
            "plot_x": 230.0,
            "plot_w": float(PLOT_W),
        }
    points = _points(exhibit)
    subjects = _subject_order(exhibit, points)
    names = {
        str(subject.get("id") or ""): str(subject.get("name") or subject.get("id") or "")
        for subject in exhibit.get("subjects") or []
    }
    for point in points:
        names.setdefault(point["subject_id"], point["name"])
    longest = max((len(name) for name in names.values()), default=8)
    label_w = min(MAX_LABEL_W, max(MIN_LABEL_W, 7 * longest))
    plot_x = label_w + 16
    width = int(plot_x + PLOT_W + RIGHT)
    series_ids = []
    for point in points:
        if point["series_id"] and point["series_id"] not in series_ids:
            series_ids.append(point["series_id"])
    grouped = str(exhibit.get("exhibit_type") or "") == "grouped_bar" and len(series_ids) > 1
    row_h = GROUP_ROW_H if grouped else ROW_H
    height = HEADER + 16 + max(len(subjects), 1) * row_h + 24
    state = str(exhibit.get("state") or "")
    rendered = state == EXHIBIT_RENDERED
    plottable_values = [
        float(point["value"]) for point in points if rendered and point["plottable"]
    ]
    axis_min = 0.0
    axis_max = 1.0
    if plottable_values:
        axis_min = min(0.0, *plottable_values)
        axis_max = max(plottable_values)
        if axis_max == axis_min:
            axis_max = axis_min + 1.0
    span = axis_max - axis_min or 1.0

    def x_of(value: float) -> float:
        return _num(plot_x + (value - axis_min) / span * PLOT_W)

    bars: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    by_subject: dict[str, list[dict[str, Any]]] = {subject_id: [] for subject_id in subjects}
    for point in points:
        by_subject.setdefault(point["subject_id"], []).append(point)
    for index, subject_id in enumerate(subjects):
        y = HEADER + 12 + index * row_h
        name = names.get(subject_id, subject_id)
        labels.append(
            {
                "text": name,
                "x": 8.0,
                "y": _num(y + (row_h / 2) + 4),
                "kind": "subject",
            }
        )
        group = by_subject.get(subject_id) or []
        slot_h = 12 if grouped else 14
        for slot, point in enumerate(group):
            bar_y = y + 6 + (slot * 16 if grouped else 4)
            display = format_value(point["raw_value"], point.get("unit") or None)
            if not rendered or not point["plottable"]:
                markers.append(
                    {
                        "kind": "missing" if point["missing"] else "unavailable",
                        "x": float(plot_x),
                        "y": _num(bar_y),
                        "text": "—"
                        if point["missing"]
                        else str(point["value_state"] or "unavailable"),
                        "detail": display,
                        "subject_id": subject_id,
                        "series_id": point["series_id"],
                    }
                )
                continue
            value = float(point["value"])
            x0 = x_of(min(0.0, value))
            x1 = x_of(max(0.0, value))
            width_px = _num(max(x1 - x0, 0.0))
            bars.append(
                {
                    "x": x0,
                    "y": _num(bar_y),
                    "w": width_px,
                    "h": float(slot_h),
                    "value": value,
                    "zero": value == 0.0,
                    "subject_id": subject_id,
                    "series_id": point["series_id"],
                    "series_label": point["series_label"],
                    "text": format_number(value),
                    "unit": point.get("unit"),
                }
            )
    label_by_id = {}
    for point in points:
        if point["series_id"]:
            label_by_id[point["series_id"]] = point["series_label"] or point["series_id"]
    legend = [
        {"series_id": series_id, "label": label_by_id.get(series_id, series_id)}
        for series_id in series_ids
    ]
    return {
        "width": width,
        "height": height,
        "state": state,
        "title": str(exhibit.get("title") or ""),
        "unit": exhibit.get("unit"),
        "reason": str(exhibit.get("reason") or ""),
        "bars": bars,
        "markers": markers,
        "labels": labels,
        "legend": legend,
        "axis_min": axis_min,
        "axis_max": axis_max,
        "plot_x": float(plot_x),
        "plot_w": float(PLOT_W),
        "label_w": float(label_w),
    }
