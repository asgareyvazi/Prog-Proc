"""Deterministic well-control event extraction, and the shapes it refuses.

A well-control sheet is the one document where a wrong number has immediate consequences, so the
contract here is narrow on purpose.  Nothing is inferred: a pit gain is not evidence of a kick, a
pressure is not evidence of a shut-in state, and a depth is not evidence of a formation.  What the
sheet states is stored; what it does not state stays NULL.

The acceptance rule is deliberately not "the document was classified ``WELL_CONTROL``".  A
classification is a judgement about the file, not permission to invent rows, so a table is accepted
only when it carries a column that is *named* as a well-control measurement - SIDPP, SICP or pit
gain - alongside at least one other event fact.  A generic ``Pressure | Volume | Time`` table inside
a well-control report is therefore rejected, because nothing in it says which pressure or which
volume those numbers are.
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
    "WELL_CONTROL_ALIASES",
    "WELL_CONTROL_DEPTH_UNITS",
    "WELL_CONTROL_EVENT_TYPES",
    "WELL_CONTROL_PRESSURE_UNITS",
    "WELL_CONTROL_VOLUME_UNITS",
    "WellControlEntry",
    "locate_well_control_header",
    "well_control_entries",
]

#: Pressures in a well-control record are stated in one of these, and only these.
WELL_CONTROL_PRESSURE_UNITS: tuple[str, ...] = ("psi", "mpa", "kpa", "bar")
#: Pit gain is a volume; the unit is never assumed to be barrels.
WELL_CONTROL_VOLUME_UNITS: tuple[str, ...] = (
    "bbl",
    "bbls",
    "m3",
    "m³",
    "ltr",
    "l",
    "gal",
    "us gal",
)
WELL_CONTROL_DEPTH_UNITS: tuple[str, ...] = (
    "ft",
    "feet",
    "m",
    "metre",
    "metres",
    "meter",
    "meters",
)

#: The event types a source may state.  Matched whole, never guessed from the numbers.
WELL_CONTROL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "kick",
        "influx",
        "loss",
        "lost_circulation",
        "lost circulation",
        "shut_in",
        "shut-in",
        "shutin",
        "flow",
        "flow check",
        "flow_check",
        "pressure_test",
        "pressure test",
        "bop test",
        "kill",
        "gas",
        "gas influx",
        "water influx",
    }
)

WELL_CONTROL_ALIASES: dict[str, tuple[str, ...]] = {
    # The discriminator columns: these are what make a table a well-control table.
    "sidpp": (
        "sidpp",
        "shut in drill pipe pressure",
        "shut-in drillpipe pressure",
        "shut in dp pressure",
    ),
    "sicp": ("sicp", "shut in casing pressure", "shut-in casing pressure", "shut in csg pressure"),
    "pit_gain": ("pit gain", "pit volume gain", "gain", "kick volume", "influx volume"),
    # Supporting facts.
    "event_type": (
        "event type",
        "event",
        "type",
        "incident type",
        "kick type",
        "event description type",
    ),
    "description": ("description", "remarks", "comments", "detail", "details", "event details"),
    "occurred_at": (
        "date",
        "time",
        "date/time",
        "date / time",
        "date & time",
        "occurred",
        "event date",
        "event time",
    ),
    "depth": ("depth", "depth md", "md", "shoe depth", "shut in depth", "depth at event"),
    "kill_method": ("kill method", "method", "kill procedure"),
    "outcome": ("outcome", "result", "status", "well status", "controlled"),
    "cause": ("cause", "root cause", "reason"),
    "corrective_action": ("corrective action", "action taken", "corrective measures"),
    "severity": ("severity", "magnitude"),
    "label": ("event label", "event ref", "reference", "event no", "event number", "label", "id"),
    "npt_hours": ("npt", "npt hours", "npt (hr)", "npt hr", "lost time", "lost time hours"),
    #: A sheet that names a well is refused for any other well; a sheet that names none is not.
    "well_name": ("well", "well name", "well no", "wellbore"),
}


@dataclass(frozen=True)
class WellControlEntry:
    """One well-control event as the source stated it, with the unit for every quantity."""

    table: Mapping[str, Any]
    row_index: int
    event_label: str
    event_type: str
    description: str
    occurred_at_text: str
    severity: str
    depth_text: str
    depth_value: float | None
    depth_unit: str
    sidpp_text: str
    sidpp_value: float | None
    sidpp_unit: str
    sicp_text: str
    sicp_value: float | None
    sicp_unit: str
    pit_gain_text: str
    pit_gain_value: float | None
    pit_gain_unit: str
    kill_method: str
    outcome: str
    cause: str
    corrective_action: str
    npt_hours_text: str
    npt_hours_value: float | None
    #: The well the row names, when it names one.  Empty means the row said nothing about scope.
    well_name: str


def _cell(row: Sequence[Any], columns: Mapping[str, int], name: str) -> str:
    return cell_text(row, columns.get(name, -1))


def _measured(
    row: Sequence[Any],
    header_row: Sequence[Any],
    columns: Mapping[str, int],
    name: str,
    units: Sequence[str],
) -> tuple[str, float | None, str]:
    """``(source text, value or None, the unit the header stated)`` for one aliased column.

    A value is stored only when the source also stated the unit it is in.  A bare ``1200`` in a
    column headed ``SIDPP`` with no unit is not known to be psi, bar or kPa, so it stays text with a
    NULL value rather than acquiring a plausible unit - the one kind of default that would turn a
    well-control number into something it was never measured in.
    """
    index = columns.get(name, -1)
    raw = cell_text(row, index)
    unit = header_unit(cell_text(header_row, index), "", units)
    return raw, (numeric(raw, units) if unit else None), unit


def _string_type(text: str) -> str:
    """The stated event type if it is one the contract recognises, else an empty string."""
    token = normalise_label(text).strip().lower()
    if not token:
        return ""
    return token if token in WELL_CONTROL_EVENT_TYPES else ""


def _is_well_control_header(headers: Mapping[str, int]) -> bool:
    """Whether a header row is a well-control measurement table rather than a table of numbers."""
    named_measurement = any(
        alias_column(headers, WELL_CONTROL_ALIASES[name]) >= 0
        for name in ("sidpp", "sicp", "pit_gain")
    )
    if not named_measurement:
        return False
    other_fact = any(
        alias_column(headers, WELL_CONTROL_ALIASES[name]) >= 0
        for name in ("depth", "event_type", "occurred_at", "description")
    )
    return other_fact


#: How far down a sheet to look for the real header.  An extractor stores the whole region,
#: including title rows and a units legend, so the header is located rather than assumed.
_HEADER_SEARCH_DEPTH = 8


def locate_well_control_header(
    rows: Sequence[Sequence[Any]],
) -> tuple[Mapping[str, int], Sequence[Any]] | None:
    """The first row within the search depth that is a well-control header."""
    for row in rows[:_HEADER_SEARCH_DEPTH]:
        headers = header_index(row, strip_units=True)
        if headers and _is_well_control_header(headers):
            return headers, row
    return None


def well_control_entries(payload: Mapping[str, Any]) -> list[WellControlEntry]:
    """Every well-control event row the stored tables state, in sheet order."""
    found: list[WellControlEntry] = []
    for table in tables(payload):
        rows = list(table.get("rows") or ())
        located = locate_well_control_header(rows)
        if located is None:
            continue
        headers, header_row = located
        start = rows.index(header_row) + 1 if header_row in rows else 1
        columns = {
            name: alias_column(headers, aliases) for name, aliases in WELL_CONTROL_ALIASES.items()
        }
        for offset, row in enumerate(rows[start:], start=start):
            if not any(cell_text(row, i).strip() for i in range(len(row))):
                continue
            sidpp = _measured(row, header_row, columns, "sidpp", WELL_CONTROL_PRESSURE_UNITS)
            sicp = _measured(row, header_row, columns, "sicp", WELL_CONTROL_PRESSURE_UNITS)
            pit = _measured(row, header_row, columns, "pit_gain", WELL_CONTROL_VOLUME_UNITS)
            depth = _measured(row, header_row, columns, "depth", WELL_CONTROL_DEPTH_UNITS)
            description = _cell(row, columns, "description")
            event_type_text = _cell(row, columns, "event_type")
            if not (sidpp[0] or sicp[0] or pit[0] or depth[0] or description or event_type_text):
                continue  # a blank or unrelated row inside the region
            npt_text = _cell(row, columns, "npt_hours")
            found.append(
                WellControlEntry(
                    table=table,
                    row_index=offset,
                    event_label=_cell(row, columns, "label"),
                    event_type=_string_type(event_type_text),
                    description=description,
                    occurred_at_text=_cell(row, columns, "occurred_at"),
                    severity=_cell(row, columns, "severity"),
                    depth_text=depth[0],
                    depth_value=depth[1],
                    depth_unit=depth[2],
                    sidpp_text=sidpp[0],
                    sidpp_value=sidpp[1],
                    sidpp_unit=sidpp[2],
                    sicp_text=sicp[0],
                    sicp_value=sicp[1],
                    sicp_unit=sicp[2],
                    pit_gain_text=pit[0],
                    pit_gain_value=pit[1],
                    pit_gain_unit=pit[2],
                    kill_method=_cell(row, columns, "kill_method"),
                    outcome=_cell(row, columns, "outcome"),
                    cause=_cell(row, columns, "cause"),
                    corrective_action=_cell(row, columns, "corrective_action"),
                    npt_hours_text=npt_text,
                    npt_hours_value=numeric(npt_text, ("hr", "hrs", "hour", "hours", "h")),
                    well_name=_cell(row, columns, "well_name"),
                )
            )
    return found
