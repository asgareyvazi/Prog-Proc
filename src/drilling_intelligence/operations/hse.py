"""Deterministic HSE incident extraction, and the shapes it refuses.

An HSE report lists things that happened to people, the environment and equipment.  Two rules do
most of the work here.

First, the acceptance rule is narrow: a table is an HSE table only when it carries a column that is
*named* as an incident type or an incident reference, alongside a column that describes the event.
That is what stops every table headed ``Incident | Date | Severity`` or ``Action | Date | Status``
from being read as an incident list, which would fill the domain with rows whose meaning came from
the classification rather than from the sheet.

Second, nothing is inferred.  A severity is stored only as reported and is never calculated from a
probability and an impact.  A root cause is stored only when the source states one: "the valve
failed" describes what happened, and turning it into "poor maintenance" would be a diagnosis the
platform produced.  A location is kept as the source's own words, and is never resolved into a well -
a slip on the camp steps has no well, and inventing one would misfile the event.
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
    tables,
)

__all__ = [
    "HSE_ALIASES",
    "HSE_INCIDENT_TYPES",
    "HSE_SEVERITIES",
    "HSE_VOLUME_UNITS",
    "HseIncidentEntry",
    "hse_incident_entries",
    "locate_hse_header",
]

#: The incident types a source may state.  Matched whole, never guessed from the description.
HSE_INCIDENT_TYPES: frozenset[str] = frozenset(
    {
        "incident",
        "near miss",
        "near-miss",
        "nearmiss",
        "unsafe act",
        "unsafe condition",
        "environmental",
        "environmental event",
        "injury",
        "first aid",
        "first-aid",
        "first aid case",
        "medical treatment",
        "lost time injury",
        "lti",
        "property damage",
        "spill",
        "release",
        "observation",
        "safety observation",
        "hazard",
        "fire",
        "vehicle",
        "dropped object",
    }
)

#: Severities a source may report.  Stored verbatim; never calculated, never defaulted.
HSE_SEVERITIES: frozenset[str] = frozenset(
    {"low", "minor", "medium", "moderate", "high", "major", "critical", "severe", "fatal"}
)

#: Only used when the source states a release quantity *and* its unit.
HSE_VOLUME_UNITS: tuple[str, ...] = ("bbl", "bbls", "m3", "m³", "ltr", "l", "gal", "us gal")

HSE_ALIASES: dict[str, tuple[str, ...]] = {
    # Discriminator columns: one of these must be present for the table to be an HSE table.
    "incident_type": ("incident type", "event type", "type of incident", "classification", "type"),
    "incident_reference": (
        "incident number",
        "incident ref",
        "incident no",
        "reference number",
        "ref no",
    ),
    # The description column is the second half of the acceptance rule.
    "description": (
        "description",
        "description of incident",
        "incident description",
        "details",
        "detail",
        "what happened",
        "observation",
        "narrative",
    ),
    # Supporting facts.
    "occurred_at": ("date", "time", "date/time", "date of incident", "incident date", "occurred"),
    "location": ("location", "place", "site", "where", "area", "location of incident"),
    "severity": ("severity", "severity rating", "potential severity", "risk rating"),
    "consequence": ("consequence", "consequences", "outcome", "result", "impact"),
    "immediate_cause": ("immediate cause", "direct cause"),
    "root_cause": ("root cause", "basic cause", "underlying cause"),
    "corrective_action": ("corrective action", "action taken", "immediate action"),
    "preventive_action": ("preventive action", "preventative action", "recommended action"),
    "spill_volume": ("spill volume", "release volume", "quantity released", "volume released"),
    "npt_hours": ("npt", "npt hours", "lost time", "lost time hours", "downtime"),
}


@dataclass(frozen=True)
class HseIncidentEntry:
    """One HSE incident as the source stated it."""

    table: Mapping[str, Any]
    row_index: int
    incident_reference: str
    incident_type: str
    description: str
    location_text: str
    occurred_at_text: str
    severity: str
    consequence: str
    immediate_cause: str
    root_cause: str
    corrective_action: str
    preventive_action: str
    spill_volume_text: str
    spill_volume_value: float | None
    spill_volume_unit: str
    npt_hours_text: str
    npt_hours_value: float | None


def _cell(row: Sequence[Any], columns: Mapping[str, int], name: str) -> str:
    return cell_text(row, columns.get(name, -1))


def _measured(
    row: Sequence[Any],
    header_row: Sequence[Any],
    columns: Mapping[str, int],
    name: str,
    units: Sequence[str],
) -> tuple[str, float | None, str]:
    """``(source text, value or None, the unit the header stated)`` for one aliased column."""
    index = columns.get(name, -1)
    raw = cell_text(row, index)
    unit = header_unit(cell_text(header_row, index), "", units)
    return raw, (numeric(raw, units) if unit else None), unit


def _incident_type(text: str) -> str:
    """The stated incident type if the contract recognises it, else an empty string."""
    token = normalise_label(text).strip().lower()
    if not token:
        return ""
    return token if token in HSE_INCIDENT_TYPES else ""


def _is_hse_header(headers: Mapping[str, int]) -> bool:
    """Whether a header row is an HSE incident table rather than a generic action or event list.

    Both halves are required.  A type or reference column alone would make any register an incident
    list, and a description column alone would make any narrative table one.
    """
    identifier = any(
        alias_column(headers, HSE_ALIASES[name]) >= 0
        for name in ("incident_type", "incident_reference")
    )
    described = alias_column(headers, HSE_ALIASES["description"]) >= 0
    return identifier and described


#: How far down a sheet to look for the real header.
_HEADER_SEARCH_DEPTH = 8


def locate_hse_header(
    rows: Sequence[Sequence[Any]],
) -> tuple[Mapping[str, int], Sequence[Any]] | None:
    """The first row within the search depth that is an HSE header."""
    for row in rows[:_HEADER_SEARCH_DEPTH]:
        headers = header_index(row, strip_units=True)
        if headers and _is_hse_header(headers):
            return headers, row
    return None


def hse_incident_entries(payload: Mapping[str, Any]) -> list[HseIncidentEntry]:
    """Every HSE incident row the stored tables state, in sheet order."""
    found: list[HseIncidentEntry] = []
    for table in tables(payload):
        rows = list(table.get("rows") or ())
        located = locate_hse_header(rows)
        if located is None:
            continue
        headers, header_row = located
        start = rows.index(header_row) + 1 if header_row in rows else 1
        columns = {name: alias_column(headers, aliases) for name, aliases in HSE_ALIASES.items()}
        for offset, row in enumerate(rows[start:], start=start):
            if not any(cell_text(row, i).strip() for i in range(len(row))):
                continue
            description = _cell(row, columns, "description")
            incident_type = _incident_type(_cell(row, columns, "incident_type"))
            if not (description or incident_type):
                continue  # a blank or unrelated row inside the region
            spill = _measured(row, header_row, columns, "spill_volume", HSE_VOLUME_UNITS)
            npt_text = _cell(row, columns, "npt_hours")
            found.append(
                HseIncidentEntry(
                    table=table,
                    row_index=offset,
                    incident_reference=_cell(row, columns, "incident_reference"),
                    incident_type=incident_type,
                    description=description,
                    location_text=_cell(row, columns, "location"),
                    occurred_at_text=_cell(row, columns, "occurred_at"),
                    severity=_cell(row, columns, "severity"),
                    consequence=_cell(row, columns, "consequence"),
                    immediate_cause=_cell(row, columns, "immediate_cause"),
                    root_cause=_cell(row, columns, "root_cause"),
                    corrective_action=_cell(row, columns, "corrective_action"),
                    preventive_action=_cell(row, columns, "preventive_action"),
                    spill_volume_text=spill[0],
                    spill_volume_value=spill[1],
                    spill_volume_unit=spill[2],
                    npt_hours_text=npt_text,
                    npt_hours_value=numeric(npt_text, ("hr", "hrs", "hour", "hours", "h")),
                )
            )
    return found
