"""Static, source-shaped mud-report parsing and read helpers.

This module is deliberately narrower than a workbook parser.  The Excel extractor has already stored
rectangular tables and typed fields with locators; the mud contract only recognises the two shapes the
certified corpus actually contains:

* a label/value/unit/remark summary table; and
* a repeated daily-test table with an explicit header row.

It never reads workbook bytes, performs a conversion, or infers a section from depth.  Unknown labels,
unknown units, formulas/averages without a sample label, and prose are retained as extraction evidence
but do not become mud measurements.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Canonical names are a deliberately closed vocabulary.  The source label remains on every emitted
# entry, so a later contract can add a label without rewriting the stored artefact or changing the
# meaning of an old measurement.
SUMMARY_ALIASES: dict[str, tuple[str, ...]] = {
    "well": ("well", "well name", "wellbore", "uwi"),
    "field": ("field", "field name"),
    "report_date": ("report date", "date", "reporting date"),
    "revision": ("revision", "rev", "report revision"),
    "depth_md": ("md", "measured depth", "depth md", "md depth"),
    "depth_tvd": ("tvd", "true vertical depth", "tvdss"),
    "mud_weight": ("mud weight", "mw", "active system mw"),
    "plastic_viscosity": ("plastic viscosity", "pv"),
    "yield_point": ("yield point", "yp"),
    "gel_strength_10s": ("gel strength 10s", "gel strength 10 sec", "gel 10s"),
    "chloride_mg_l": ("chloride", "chloride mg l", "chloride mg/l"),
    "equivalent_mud_weight": ("emw", "ecd", "equivalent mud weight"),
    "pore_pressure_gradient": ("pore pressure gradient", "pp gradient"),
    "total_mud_volume": ("total mud volume", "mud volume", "active system volume"),
    # These are optional focused-test attributes.  They are not present in the golden workbook and
    # therefore cannot attach that report to a section.
    "section_id": ("section id", "section identifier", "hole section id"),
    "section": ("section", "hole section", "interval"),
    "hole_size_in": ("hole size", "hole size in", "nominal hole size"),
}

DAILY_ALIASES: dict[str, tuple[str, ...]] = {
    "sample_label": ("slip", "sample", "test", "shift"),
    "sample_time": ("time", "sample time", "test time"),
    "mud_weight_in": ("mw in", "mw-in", "mud weight in", "mud weight in ppg"),
    "mud_weight_out": ("mw out", "mw-out", "mud weight out", "mud weight out ppg"),
    "viscosity": ("visc", "viscosity", "visc cp", "viscosity cp"),
    "sand_content": ("sand", "sand pct", "sand content", "sand content pct"),
    "notes": ("notes", "note", "remarks", "remark"),
}

PROPERTY_UNITS: dict[str, str] = {
    "mud_weight": "ppg",
    "depth_md": "ft",
    "depth_tvd": "ft",
    "plastic_viscosity": "cP",
    "yield_point": "lb/100ft2",
    "gel_strength_10s": "lb/100ft2",
    "chloride_mg_l": "mg/l",
    "equivalent_mud_weight": "ppg",
    "pore_pressure_gradient": "psi/ft",
    "total_mud_volume": "bbl",
    "mud_weight_in": "ppg",
    "mud_weight_out": "ppg",
    "viscosity": "cP",
    "sand_content": "pct",
}


def normalise_label(value: Any) -> str:
    """Lowercase a source label without erasing meaningful slash/unit text."""
    text = str(value or "").strip().lower()
    text = text.replace("²", "2").replace("³", "3")
    text = re.sub(r"[,:;]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _without_units(text: str) -> str:
    text = normalise_label(text)
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\b(?:ft|m|ppg|cp|mg/l|lb/100ft2|psi/ft|bbl|pct|%)\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_summary_label(value: Any) -> str:
    label = normalise_label(value)
    stripped = _without_units(label)
    for canonical, aliases in SUMMARY_ALIASES.items():
        if label in aliases or stripped in aliases:
            return canonical
    # Some extracted labels carry the unit outside parentheses, e.g. ``MD (ft)``.
    for canonical, aliases in SUMMARY_ALIASES.items():
        if any(stripped == alias or stripped.startswith(alias + " ") for alias in aliases):
            return canonical
    return ""


def _headers(row: Sequence[Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, cell in enumerate(row):
        label = normalise_label(cell)
        if label and label not in result:
            result[label] = index
    return result


def _alias_column(headers: Mapping[str, int], aliases: Sequence[str]) -> int:
    for alias in aliases:
        key = normalise_label(alias)
        if key in headers:
            return int(headers[key])
    # Header units and punctuation are source decoration, not a new column vocabulary.
    for key, index in headers.items():
        if any(key.startswith(normalise_label(alias) + " ") for alias in aliases):
            return int(index)
    return -1


def _cell(row: Sequence[Any], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    value = row[index]
    return "" if value is None else str(value).strip()


def numeric(value: Any) -> float | None:
    """Parse a numeric source cell without converting units or accepting formulas."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text.startswith("="):
        return None
    match = re.fullmatch(
        r"[-+]?\d+(?:\.\d+)?(?:\s*(?:ppg|cP|cp|mg/l|lb/100ft2|psi/ft|bbl|pct|%|ft|m))?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    number = re.match(r"[-+]?\d+(?:\.\d+)?", text)
    try:
        return float(number.group(0)) if number else None
    except (TypeError, ValueError):
        return None


def header_unit(header: Any, default: str = "") -> str:
    text = str(header or "")
    match = re.search(r"\(([^)]+)\)|\b(ppg|cP|mg/l|lb/100ft2|psi/ft|bbl|pct|%)\b", text, re.I)
    return (match.group(1) or match.group(2)).strip() if match else default


def table_key(table: Mapping[str, Any]) -> str:
    return "|".join(str(table.get(key) or "") for key in ("table_id", "sheet", "anchor", "page"))


def tables(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(table) for table in (payload.get("tables") or []) if isinstance(table, Mapping)]


@dataclass(frozen=True)
class SummaryEntry:
    property_name: str
    source_label: str
    source_value: str
    source_unit: str
    remark: str
    table: Mapping[str, Any]
    row_index: int


@dataclass(frozen=True)
class DailyEntry:
    property_name: str
    source_value: str
    source_unit: str
    sample_label: str
    sample_time: str
    note: str
    table: Mapping[str, Any]
    row_index: int
    column_index: int
    header: str


def summary_entries(payload: Mapping[str, Any]) -> tuple[SummaryEntry, ...]:
    """Return only recognised label/value rows from the source-shaped summary table(s)."""
    entries: list[SummaryEntry] = []
    for table in tables(payload):
        rows = table.get("rows") or []
        for row_index, row in enumerate(rows):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 2:
                continue
            property_name = canonical_summary_label(row[0])
            if not property_name:
                continue
            source_value = _cell(row, 1)
            if not source_value:
                continue
            label = str(row[0] or "").strip()
            # A contract may preserve an unqualified value as UNVERIFIED, but it may not invent the
            # conventional unit merely because this property usually uses one.
            unit = _cell(row, 2) or header_unit(label, "")
            remark = _cell(row, 3)
            entries.append(
                SummaryEntry(
                    property_name=property_name,
                    source_label=label,
                    source_value=source_value,
                    source_unit=unit,
                    remark=remark,
                    table=table,
                    row_index=row_index,
                )
            )
    # A daily header may contain a label called "sample" but no recognised summary row; no special
    # exclusion is needed because summary labels are a closed vocabulary.
    return tuple(entries)


def _daily_candidate(table: Mapping[str, Any]) -> tuple[int, dict[str, int]] | None:
    rows = table.get("rows") or []
    for row_index, row in enumerate(rows[:8]):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            continue
        headers = _headers(row)
        aliases = {name: _alias_column(headers, values) for name, values in DAILY_ALIASES.items()}
        measurements = sum(
            aliases[name] >= 0
            for name in ("mud_weight_in", "mud_weight_out", "viscosity", "sand_content")
        )
        if measurements >= 2 and aliases["sample_label"] >= 0:
            return row_index, aliases
    return None


def daily_entries(payload: Mapping[str, Any]) -> tuple[DailyEntry, ...]:
    """Return repeated numeric properties, preserving sample/slip and table row identity."""
    entries: list[DailyEntry] = []
    for table in tables(payload):
        candidate = _daily_candidate(table)
        if candidate is None:
            continue
        header_row, columns = candidate
        headers = table.get("rows") or []
        header_values = headers[header_row]
        for row_index, row in enumerate(headers[header_row + 1 :], start=header_row + 1):
            if not isinstance(row, Sequence) or not any(str(value or "").strip() for value in row):
                continue
            sample_label = _cell(row, columns["sample_label"])
            if not sample_label or numeric(sample_label) is not None:
                # Average/formula rows have no source sample identity and are not repeated tests.
                continue
            sample_time = _cell(row, columns["sample_time"])
            note = _cell(row, columns["notes"])
            for property_name in ("mud_weight_in", "mud_weight_out", "viscosity", "sand_content"):
                column_index = columns[property_name]
                if column_index < 0:
                    continue
                source_value = _cell(row, column_index)
                if numeric(source_value) is None:
                    continue
                header = _cell(header_values, column_index)
                entries.append(
                    DailyEntry(
                        property_name=property_name,
                        source_value=source_value,
                        source_unit=header_unit(header, ""),
                        sample_label=sample_label,
                        sample_time=sample_time,
                        note=note,
                        table=table,
                        row_index=row_index,
                        column_index=column_index,
                        header=header,
                    )
                )
    return tuple(entries)


def raw_summary_value(entries: Sequence[SummaryEntry], property_name: str) -> str:
    for entry in entries:
        if entry.property_name == property_name:
            return entry.source_value
    return ""


__all__ = [
    "DAILY_ALIASES",
    "PROPERTY_UNITS",
    "SUMMARY_ALIASES",
    "DailyEntry",
    "SummaryEntry",
    "canonical_summary_label",
    "daily_entries",
    "header_unit",
    "numeric",
    "raw_summary_value",
    "summary_entries",
    "table_key",
    "tables",
]
