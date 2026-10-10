"""Exhibit specifications mapped from certified pack cells.

An exhibit describes values that already exist. It does not sum NPT, convert
units, score risk, or drop a subject to make a chart look complete.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .contract import (
    EXHIBIT_INCOMPARABLE,
    EXHIBIT_MISSING,
    EXHIBIT_NO_DATA,
    EXHIBIT_NOT_APPLICABLE,
    EXHIBIT_RENDERED,
    EXHIBIT_STALE,
    EXHIBIT_UNRESOLVED,
    EXHIBIT_UNSUPPORTED,
    ReportExhibit,
)
from .format import is_number

_COMPARABLE_STATES = ("STATED", "PARTIAL", "COUNTED")
_NON_NUMERIC_METRICS = {"profile.spud_date"}
_METRIC_DOMAIN = {
    "npt": "npt_record",
    "problems": "problem_occurrence",
    "well_control": "well_control_event",
    "hse": "hse_incident",
    "cost": "cost_item",
    "risk": "risk_record",
    "lessons": "lesson_learned",
    "practices": "best_practice",
    "recommendations": "recommendation",
    "patterns": "field_pattern",
    "calculations": "calculation",
    "plan": "well_section",
    "conflicts": "knowledge_conflict",
    "profile": "well",
}

_DEPTH_REASON = (
    "point-level depth series are not in the certified packs; figure metadata does not "
    "carry curve values or portable image bytes; interval endpoints are not plotted as a log"
)


def _slug(metric: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in metric.lower()).strip("-") or "metric"


def _subjects(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str], ...]:
    return tuple(
        {"id": str(row.get("well_id") or row.get("id") or ""), "name": str(row.get("name") or "")}
        for row in rows
    )


def _evidence_refs(evidence: Sequence[Mapping[str, Any]], metric: str) -> tuple[str, ...]:
    prefix = metric.split(".", 1)[0]
    domain = _METRIC_DOMAIN.get(prefix, "")
    refs: list[str] = []
    for item in evidence:
        if domain and str(item.get("domain") or "") != domain:
            continue
        method = item.get("method")
        if method:
            refs.append(f"method:{method}")
        for sample in item.get("sample") or []:
            refs.append(str(sample))
    return tuple(dict.fromkeys(refs))


def _point(
    subject: Mapping[str, Any],
    cell: Mapping[str, Any],
    *,
    plottable: bool,
) -> dict[str, Any]:
    raw = cell.get("value")
    number = raw if is_number(raw) else None
    drawn = bool(plottable and number is not None)
    return {
        "subject_id": str(subject.get("id") or ""),
        "name": str(subject.get("name") or subject.get("id") or ""),
        "value": number if drawn else None,
        "raw_value": raw,
        "unit": cell.get("unit"),
        "value_state": cell.get("value_state"),
        "comparability": cell.get("comparability"),
        "missing": raw is None,
        "plottable": drawn,
    }


def _series(
    points: Sequence[Mapping[str, Any]], *, unit: str | None, label: str, series_id: str
) -> dict[str, Any]:
    return {
        "series_id": series_id,
        "label": label,
        "unit": unit,
        "points": [dict(point) for point in points],
    }


def exhibit_from_metric(
    row: Mapping[str, Any],
    subjects: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]] = (),
) -> ReportExhibit:
    """One comparison metric row, in subject order, with an honest chart state."""
    metric = str(row.get("metric") or "")
    label = str(row.get("label") or metric)
    comparability = str(row.get("comparability") or EXHIBIT_MISSING)
    values = row.get("values") or {}
    subject_rows = _subjects(subjects)
    units = {
        str(values.get(subject["id"], {}).get("unit"))
        for subject in subject_rows
        if values.get(subject["id"], {}).get("unit")
    }
    stated_numbers = []
    for subject in subject_rows:
        cell = values.get(subject["id"]) or {}
        if cell.get("value_state") in _COMPARABLE_STATES and is_number(cell.get("value")):
            stated_numbers.append(cell.get("value"))
    non_numeric = metric in _NON_NUMERIC_METRICS or any(
        (values.get(subject["id"]) or {}).get("value") is not None
        and not is_number((values.get(subject["id"]) or {}).get("value"))
        for subject in subject_rows
    )
    reason = ""
    state = comparability
    if non_numeric:
        state = EXHIBIT_UNSUPPORTED
        reason = (
            "the source value is not a numeric series; it stays in the matrix and is not plotted"
        )
    elif len(units) > 1 or comparability == EXHIBIT_INCOMPARABLE:
        state = EXHIBIT_INCOMPARABLE
        reason = "source units differ; values stay visible and are not plotted on one axis"
    elif comparability == EXHIBIT_UNRESOLVED:
        state = EXHIBIT_UNRESOLVED
        reason = "the source contract does not resolve comparability; no shared axis is drawn"
    elif comparability == EXHIBIT_STALE:
        state = EXHIBIT_STALE
        reason = "the source marks this metric stale; it is not plotted as current"
    elif comparability == EXHIBIT_NOT_APPLICABLE:
        state = EXHIBIT_NOT_APPLICABLE
        reason = "the metric does not apply"
    elif comparability == EXHIBIT_MISSING or len(stated_numbers) < 2:
        state = EXHIBIT_MISSING if stated_numbers else EXHIBIT_NO_DATA
        reason = (
            "fewer than two subjects state a comparable number; a shorter chart would look complete"
            if stated_numbers
            else "no comparable number is stated"
        )
    elif comparability == "COMPARABLE" and len(stated_numbers) >= 2 and len(units) <= 1:
        state = EXHIBIT_RENDERED
        reason = ""
    else:
        state = EXHIBIT_UNSUPPORTED
        reason = "the source row cannot be plotted without inventing a scale"

    plot = state == EXHIBIT_RENDERED
    unit = next(iter(units), None) if len(units) == 1 else None
    points = []
    for subject in subject_rows:
        cell = dict(
            values.get(subject["id"]) or {"value": None, "unit": None, "value_state": "MISSING"}
        )
        can_plot = (
            plot
            and cell.get("value_state") in _COMPARABLE_STATES
            and is_number(cell.get("value"))
            and (unit is None or cell.get("unit") in (None, unit) or not cell.get("unit"))
            and (not unit or cell.get("unit") == unit or not cell.get("unit"))
        )
        # A rendered chart still refuses a cell whose unit disagrees, and refuses null.
        if plot and cell.get("unit") and unit and cell.get("unit") != unit:
            can_plot = False
        if plot and cell.get("value") is None:
            can_plot = False
        points.append(_point(subject, cell, plottable=can_plot))
    caption = (
        f"{label}. Chart state {state}. "
        f"Unit {unit or 'none stated'}. "
        "Subject order is the selection order, not a ranking. "
        "A missing value is not drawn as zero."
    )
    if str(row.get("note") or ""):
        caption = f"{caption} {row.get('note')}"
    limitations = tuple(str(item) for item in (row.get("limitations") or []) if item)
    return ReportExhibit(
        exhibit_id=f"exhibit-{_slug(metric)}",
        exhibit_type="horizontal_bar" if state == EXHIBIT_RENDERED else "state",
        title=label,
        metric=metric,
        unit=unit,
        state=state,
        subjects=subject_rows,
        series=(_series(points, unit=unit, label=label, series_id="value"),),
        categories=tuple(subject["id"] for subject in subject_rows),
        data={
            "comparability": comparability,
            "values": {key: dict(value) for key, value in values.items()},
            "units": dict(row.get("units") or {}) or None,
            "note": str(row.get("note") or ""),
            "limitations": list(limitations),
        },
        caption=caption,
        evidence_refs=_evidence_refs(evidence, metric),
        limitations=limitations,
        reason=reason,
    )


def unsupported_depth_exhibit(subjects: Sequence[Mapping[str, Any]] = ()) -> ReportExhibit:
    subject_rows = _subjects(subjects)
    return ReportExhibit(
        exhibit_id="exhibit-depth-series",
        exhibit_type="state",
        title="Depth series",
        metric="depth.series",
        unit=None,
        state=EXHIBIT_UNSUPPORTED,
        subjects=subject_rows,
        series=(),
        categories=(),
        data={"reason": _DEPTH_REASON},
        caption=(
            "Depth series: UNSUPPORTED. This is not a log and not a trajectory. " + _DEPTH_REASON
        ),
        evidence_refs=(),
        limitations=("depth_series_unsupported",),
        reason=_DEPTH_REASON,
    )


def exhibits_from_comparison(pack: Mapping[str, Any]) -> tuple[ReportExhibit, ...]:
    """Every comparison metric, plus the explicit unsupported depth exhibit."""
    basis = pack.get("basis") or {}
    subjects = list(basis.get("subjects") or [])
    evidence = list(pack.get("evidence") or [])
    exhibits = [
        exhibit_from_metric(row, subjects, evidence)
        for section in pack.get("sections") or []
        for row in section.get("metrics") or []
    ]
    exhibits.append(unsupported_depth_exhibit(subjects))
    return tuple(exhibits)


def _category_exhibit(
    *,
    exhibit_id: str,
    metric: str,
    title: str,
    entries: Mapping[str, Any],
    value_key: str,
    unit: str | None,
    subject_name: str,
    caption_extra: str,
    unassessed_when_no_duration: bool = False,
) -> ReportExhibit:
    names = sorted(str(key) for key in entries)
    subjects = tuple({"id": name, "name": name} for name in names)
    points = []
    any_plottable = False
    for name in names:
        entry = entries.get(name) or {}
        raw = entry.get(value_key)
        unknown = int(entry.get("unknown_duration") or 0)
        records = int(entry.get("records") or entry.get("occurrences") or 0)
        unassessed = unassessed_when_no_duration and records > 0 and unknown >= records
        plottable = is_number(raw) and not unassessed
        if plottable:
            any_plottable = True
        cell = {
            "value": None if unassessed else raw,
            "unit": unit,
            "value_state": "UNASSESSED"
            if unassessed
            else ("STATED" if is_number(raw) else "MISSING"),
            "comparability": "COMPARABLE" if plottable else "MISSING",
        }
        # Keep the source number on the point even when the bar is refused.
        point = _point({"id": name, "name": name}, cell, plottable=plottable)
        point["raw_value"] = raw
        point["unknown_duration"] = unknown
        points.append(point)
    if not names:
        state = EXHIBIT_NO_DATA
        reason = "the source fold has no categories"
    elif not any_plottable:
        state = EXHIBIT_MISSING
        reason = "categories exist but none states a plottable number; nothing is drawn as zero"
    else:
        state = EXHIBIT_RENDERED
        reason = ""
    caption = (
        f"{title}. Chart state {state}. Unit {unit or 'count'}. "
        "Category order is alphabetical, not a ranking. "
        "A missing or unstated value is not drawn as zero. " + caption_extra
    )
    return ReportExhibit(
        exhibit_id=exhibit_id,
        exhibit_type="horizontal_bar" if state == EXHIBIT_RENDERED else "state",
        title=title,
        metric=metric,
        unit=unit,
        state=state,
        subjects=subjects,
        series=(_series(points, unit=unit, label=title, series_id="value"),),
        categories=tuple(names),
        data={
            "entries": {key: dict(value) for key, value in entries.items()},
            "subject": subject_name,
        },
        caption=caption,
        reason=reason,
    )


def _currency_exhibit(
    code: str, bucket: Mapping[str, Any], subject: Mapping[str, Any]
) -> ReportExhibit:
    subject_id = str(subject.get("id") or subject.get("well_id") or "")
    name = str(subject.get("name") or subject_id)
    subjects = ({"id": subject_id, "name": name},)

    def side(which: str) -> dict[str, Any]:
        lines = int(bucket.get(f"{which}_lines") or 0)
        raw = bucket.get(which)
        stated = lines > 0 and is_number(raw)
        cell = {
            "value": raw if stated else None,
            "unit": code,
            "value_state": "STATED" if stated else "MISSING",
            "comparability": "NOT_APPLICABLE",
        }
        point = _point(subjects[0], cell, plottable=stated)
        point["raw_value"] = raw
        point["lines"] = lines
        return point

    planned = side("planned")
    actual = side("actual")
    if planned["plottable"] or actual["plottable"]:
        state = EXHIBIT_RENDERED
        reason = ""
        exhibit_type = "grouped_bar"
    else:
        state = EXHIBIT_NO_DATA
        reason = f"no stated {code} lines; the empty-bucket zero is not plotted"
        exhibit_type = "state"
    return ReportExhibit(
        exhibit_id=f"exhibit-cost-{_slug(code)}",
        exhibit_type=exhibit_type,
        title=f"Cost stated in {code}",
        metric=f"cost.currency.{code}",
        unit=code,
        state=state,
        subjects=subjects,
        series=(
            _series((planned,), unit=code, label="planned", series_id="planned"),
            _series((actual,), unit=code, label="actual", series_id="actual"),
        ),
        categories=("planned", "actual"),
        data={"currency": code, "bucket": dict(bucket)},
        caption=(
            f"Cost stated in {code} only. Planned and actual are separate series. "
            "No other currency is on this axis. A side with no lines is not drawn as zero."
        ),
        reason=reason,
    )


def exhibits_from_decision(pack: Mapping[str, Any]) -> tuple[ReportExhibit, ...]:
    """Series already present on a decision pack, plus the unsupported depth exhibit."""
    subject = pack.get("subject") or {}
    subject_row = {
        "id": str(subject.get("id") or ""),
        "well_id": str(subject.get("id") or ""),
        "name": str(subject.get("name") or subject.get("id") or ""),
    }
    operations = pack.get("operations") or {}
    npt = operations.get("npt") or {}
    problems = operations.get("problems") or {}
    well_control = operations.get("well_control") or {}
    hse = operations.get("hse") or {}
    economics = pack.get("economics") or {}
    summary = economics.get("summary") or {}
    exhibits: list[ReportExhibit] = [
        _category_exhibit(
            exhibit_id="exhibit-npt-by-category",
            metric="npt.by_category.hours",
            title="NPT hours by category",
            entries=npt.get("by_category") or {},
            value_key="hours",
            unit="h",
            subject_name=subject_row["name"],
            caption_extra=(
                "Hours are the source fold's stated sum. A category whose rows state no "
                "duration is not drawn as zero."
            ),
            unassessed_when_no_duration=True,
        ),
        _category_exhibit(
            exhibit_id="exhibit-problems-by-type",
            metric="problems.by_type.occurrences",
            title="Problem occurrences by type",
            entries=problems.get("by_type") or {},
            value_key="occurrences",
            unit=None,
            subject_name=subject_row["name"],
            caption_extra="Counts of recorded occurrences. Not a forecast.",
        ),
        _category_exhibit(
            exhibit_id="exhibit-well-control-by-type",
            metric="well_control.by_event_type",
            title="Well-control rows by event type",
            entries={
                key: {"occurrences": value} if not isinstance(value, Mapping) else value
                for key, value in (well_control.get("by_event_type") or {}).items()
            },
            value_key="occurrences",
            unit=None,
            subject_name=subject_row["name"],
            caption_extra="Counts of recorded rows. Not a safety ranking.",
        ),
        _category_exhibit(
            exhibit_id="exhibit-hse-by-type",
            metric="hse.by_incident_type",
            title="Well-scoped HSE incidents by type",
            entries={
                key: {"occurrences": value} if not isinstance(value, Mapping) else value
                for key, value in (hse.get("by_incident_type") or {}).items()
            },
            value_key="occurrences",
            unit=None,
            subject_name=subject_row["name"],
            caption_extra=(
                "Well-scoped incident types only. Site-scoped incidents are not in this chart "
                "and are not a safety ranking."
            ),
        ),
    ]
    by_currency = summary.get("by_currency") or {}
    if by_currency:
        for code in sorted(by_currency):
            exhibits.append(_currency_exhibit(code, by_currency[code] or {}, subject_row))
    else:
        exhibits.append(
            ReportExhibit(
                exhibit_id="exhibit-cost-currency",
                exhibit_type="state",
                title="Cost by currency",
                metric="cost.currency",
                unit=None,
                state=EXHIBIT_NO_DATA,
                subjects=_subjects((subject_row,)),
                series=(),
                categories=(),
                data={"by_currency": {}},
                caption="No currency bucket is present in the source cost summary.",
                reason="the source cost summary has no by_currency entries",
            )
        )
    exhibits.append(unsupported_depth_exhibit((subject_row,)))
    return tuple(exhibits)
