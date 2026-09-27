"""Static, source-shaped directional-survey parsing.

A directional survey is a set of *stations*: measured depth with the inclination and azimuth measured
at it, plus whatever else the surveying company printed - TVD, northing, easting, toolface, dogleg
severity.  This module reads the tables the extractor already stored and recognises exactly one shape:
a station table with an explicit header row.

The acceptance rule is the narrowest one that means "this is a survey":

*   a **measured depth** column, *and*
*   an **inclination** column, *and*
*   an **azimuth** column.

A depth/time table is a drilling log, a depth/lithology table is a geology log, and a table of depths
alone is a tally.  None of them becomes a survey here.

Three rules this module exists to keep:

*   **Nothing is computed.**  TVD, northing, easting and dogleg severity are preserved when the source
    printed them and left NULL when it did not.  There is no minimum-curvature, no tangential method
    and no average-angle calculation anywhere in the platform, and ingestion is not the place to
    smuggle one in: a trajectory method is an engineering assumption, and an assumption that arrives
    inside a parser cannot be reviewed.
*   **Nothing is converted.**  Degrees stay degrees, feet stay feet.  ``*_unit`` is the source's own
    header text, and a station whose unit the source did not state keeps an empty unit and an
    ``UNVERIFIED`` quality rather than a plausible one.
*   **Sets are never merged.**  When the source has a run/set column, each distinct value becomes its
    own :class:`SurveyStation` parent, so two surveys of one well stay two surveys.  When it does not,
    the table itself is the set and its identity is used - which means a workbook holding two
    unlabelled surveys in two sheets stays two runs, and one sheet holding both stays one run, exactly
    as the source presented them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .tableshape import (
    alias_column,
    cell_text,
    header_index,
    header_unit,
    normalise_label,
    numeric,
    table_key,
    tables,
    without_units,
)

__all__ = [
    "STATION_ALIASES",
    "SUMMARY_ALIASES",
    "SurveyStationEntry",
    "SurveySummaryEntry",
    "canonical_summary_label",
    "station_entries",
    "station_tables",
    "summary_entries",
]

#: Unit decorations a survey header may carry.
SURVEY_UNIT_TOKENS: tuple[str, ...] = (
    "ft",
    "feet",
    "m",
    "deg",
    "degs",
    "degrees",
    "deg/100ft",
    "deg/30m",
    "in",
)

#: The label/value summary vocabulary.  A survey's own header block, not a guess about the file.
SUMMARY_ALIASES: dict[str, tuple[str, ...]] = {
    "well": ("well", "well name", "wellbore", "uwi"),
    "field": ("field", "field name"),
    "run_label": (
        "survey run",
        "survey run no",
        "survey run no.",
        "run no",
        "run no.",
        "run number",
        "survey no",
        "survey no.",
        "survey set",
        "set",
    ),
    "survey_date": ("survey date", "date", "run date", "shot date"),
    "survey_tool": ("tool", "survey tool", "mwd tool", "gyro", "instrument"),
    "section_id": ("section id", "section identifier", "hole section id"),
    "section": ("section", "hole section"),
    "hole_size_in": ("hole size", "hole size in", "nominal hole size"),
}

#: The station table's column vocabulary.
STATION_ALIASES: dict[str, tuple[str, ...]] = {
    "station": ("station", "sta", "stn", "station no", "station no.", "station #", "stn no"),
    "md": ("md", "measured depth", "depth md", "md depth"),
    "tvd": ("tvd", "true vertical depth", "tvdss", "tvd ss", "depth tvd"),
    "inclination": ("inclination", "incl", "inc", "hole angle", "inclination deg"),
    "azimuth": ("azimuth", "azim", "az", "azi", "hole direction"),
    "toolface": ("toolface", "tf", "tool face", "gravity toolface"),
    "northing": ("northing", "north", "n s", "north south"),
    "easting": ("easting", "east", "e w", "east west"),
    "dls": ("dls", "dogleg", "dogleg severity", "dog leg severity", "dl severity"),
    "run_label": ("survey run", "run", "run no", "run no.", "run number", "set", "survey set"),
    "section": ("section", "hole section", "section id"),
    "survey_date": ("date", "survey date", "shot date", "time"),
    "well": ("well", "well name", "wellbore", "uwi"),
}


@dataclass(frozen=True)
class SurveySummaryEntry:
    """One recognised label/value row of a survey's own header block."""

    property_name: str
    source_label: str
    source_value: str
    source_unit: str
    table: Mapping[str, Any]
    row_index: int


@dataclass(frozen=True)
class SurveyStationEntry:
    """One recognised survey station, exactly as the source stated it."""

    sequence: int
    source_row_index: int
    station_number_text: str
    run_label: str
    section_text: str
    well_name: str
    date_text: str
    md_text: str
    md_value: float | None
    md_unit: str
    tvd_text: str
    tvd_value: float | None
    tvd_unit: str
    inclination_text: str
    inclination_value: float | None
    inclination_unit: str
    azimuth_text: str
    azimuth_value: float | None
    azimuth_unit: str
    toolface_text: str
    toolface_value: float | None
    toolface_unit: str
    northing_text: str
    northing_value: float | None
    northing_unit: str
    easting_text: str
    easting_value: float | None
    easting_unit: str
    dls_text: str
    dls_value: float | None
    dls_unit: str
    table: Mapping[str, Any]


def canonical_summary_label(value: Any) -> str:
    """The summary label a source row states, or ``""`` when the label is not in the vocabulary."""
    label = normalise_label(value)
    stripped = without_units(label, SURVEY_UNIT_TOKENS)
    for canonical, aliases in SUMMARY_ALIASES.items():
        if label in aliases or stripped in aliases:
            return canonical
    for canonical, aliases in SUMMARY_ALIASES.items():
        if any(stripped == alias or stripped.startswith(alias + " ") for alias in aliases):
            return canonical
    return ""


def summary_entries(payload: Mapping[str, Any]) -> tuple[SurveySummaryEntry, ...]:
    """Recognised label/value rows from every stored table, in stored order."""
    entries: list[SurveySummaryEntry] = []
    for table in tables(payload):
        for row_index, row in enumerate(table.get("rows") or []):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 2:
                continue
            property_name = canonical_summary_label(row[0])
            if not property_name:
                continue
            source_value = cell_text(row, 1)
            if not source_value:
                continue
            label = str(row[0] or "").strip()
            entries.append(
                SurveySummaryEntry(
                    property_name=property_name,
                    source_label=label,
                    source_value=source_value,
                    source_unit=cell_text(row, 2) or header_unit(label, "", SURVEY_UNIT_TOKENS),
                    table=table,
                    row_index=row_index,
                )
            )
    return tuple(entries)


def _columns_of(row: Sequence[Any]) -> dict[str, int] | None:
    """The station table's column map, or ``None`` when the row is not a survey header."""
    headers = header_index(row, strip_units=True)
    columns = {name: alias_column(headers, aliases) for name, aliases in STATION_ALIASES.items()}
    if any(columns[name] < 0 for name in ("md", "inclination", "azimuth")):
        return None
    # Three different columns, not one column recognised three times.
    if len({columns["md"], columns["inclination"], columns["azimuth"]}) < 3:
        return None
    return columns


def station_tables(payload: Mapping[str, Any]) -> list[tuple[dict[str, Any], dict[str, int], int]]:
    """``(table, columns, header_row_index)`` for every stored table that is a survey."""
    found: list[tuple[dict[str, Any], dict[str, int], int]] = []
    for table in tables(payload):
        for row_index, row in enumerate((table.get("rows") or [])[:8]):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                continue
            columns = _columns_of(row)
            if columns is None:
                continue
            found.append((table, columns, row_index))
            break
    return found


def _text(row: Sequence[Any], columns: Mapping[str, int], name: str) -> str:
    return cell_text(row, columns.get(name, -1))


def _measured(
    row: Sequence[Any],
    header_values: Sequence[Any],
    columns: Mapping[str, int],
    name: str,
) -> tuple[str, float | None, str]:
    text = _text(row, columns, name)
    return (
        text,
        numeric(text, SURVEY_UNIT_TOKENS),
        header_unit(cell_text(header_values, columns.get(name, -1)), "", SURVEY_UNIT_TOKENS),
    )


def station_entries(payload: Mapping[str, Any]) -> tuple[SurveyStationEntry, ...]:
    """Every recognised station of every survey table, in source order.

    ``sequence`` restarts at 1 for each table and counts only admitted stations, so it is the source's
    own station order.  A row with no numeric measured depth is not a station the survey measured - it
    is a heading or a total - and is left as extraction evidence.
    """
    entries: list[SurveyStationEntry] = []
    for table, columns, header_row in station_tables(payload):
        rows = table.get("rows") or []
        header_values = rows[header_row]
        sequence = 0
        for row_index, row in enumerate(rows[header_row + 1 :], start=header_row + 1):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                continue
            if not any(str(value or "").strip() for value in row):
                continue
            md_text, md_value, md_unit = _measured(row, header_values, columns, "md")
            if md_value is None:
                continue
            inclination_text, inclination_value, inclination_unit = _measured(
                row, header_values, columns, "inclination"
            )
            azimuth_text, azimuth_value, azimuth_unit = _measured(row, header_values, columns, "azimuth")
            tvd_text, tvd_value, tvd_unit = _measured(row, header_values, columns, "tvd")
            toolface_text, toolface_value, toolface_unit = _measured(
                row, header_values, columns, "toolface"
            )
            northing_text, northing_value, northing_unit = _measured(
                row, header_values, columns, "northing"
            )
            easting_text, easting_value, easting_unit = _measured(
                row, header_values, columns, "easting"
            )
            dls_text, dls_value, dls_unit = _measured(row, header_values, columns, "dls")
            sequence += 1
            entries.append(
                SurveyStationEntry(
                    sequence=sequence,
                    source_row_index=row_index,
                    station_number_text=_text(row, columns, "station"),
                    run_label=_text(row, columns, "run_label"),
                    section_text=_text(row, columns, "section"),
                    well_name=_text(row, columns, "well"),
                    date_text=_text(row, columns, "survey_date"),
                    md_text=md_text,
                    md_value=md_value,
                    md_unit=md_unit,
                    tvd_text=tvd_text,
                    tvd_value=tvd_value,
                    tvd_unit=tvd_unit,
                    inclination_text=inclination_text,
                    inclination_value=inclination_value,
                    inclination_unit=inclination_unit,
                    azimuth_text=azimuth_text,
                    azimuth_value=azimuth_value,
                    azimuth_unit=azimuth_unit,
                    toolface_text=toolface_text,
                    toolface_value=toolface_value,
                    toolface_unit=toolface_unit,
                    northing_text=northing_text,
                    northing_value=northing_value,
                    northing_unit=northing_unit,
                    easting_text=easting_text,
                    easting_value=easting_value,
                    easting_unit=easting_unit,
                    dls_text=dls_text,
                    dls_value=dls_value,
                    dls_unit=dls_unit,
                    table=table,
                )
            )
    return tuple(entries)


def station_table_key(entries: Sequence[SurveyStationEntry]) -> str:
    """The identity of the first table the stations came from, in stored order."""
    for entry in entries:
        return table_key(entry.table)
    return ""
