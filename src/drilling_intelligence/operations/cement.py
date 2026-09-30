"""Static, source-shaped cement-job parsing.

A cement report states what was pumped: a job, its stages, the slurries and their volumes and
densities, where the cement ended up, and what happened.  This module reads the tables the
extractor already stored and recognises exactly one shape - a cement job table with an explicit
header row.

The acceptance rule is the narrowest one that means "this is a cement job":

*   at least one **cement-specific volume** column - lead, tail or total, and
*   at least one **cement-specific datum** - a slurry name, a top of cement, a shoe depth, a
  displacement or a wait-on-cement.

Both halves matter.  A table headed ``Volume | Pressure | Depth`` has numbers in it and means
nothing: it is not a cement job because nothing in it says so, and a filename containing the word
"cement" is not evidence about a table's columns.  The classification says what the document is;
the table shape says what the row can hold, and only the two together make a contract.

Four rules this module exists to keep:

*   **Lead and tail are never summed.**  A two-stage design is two volumes with two densities.  A
    source that states only a total gets a total, recorded as such, and no invented split; a source
    that states both gets both.  Neither is derived from the other, and neither is folded into one
    anonymous number.
*   **Top of cement is not shoe depth.**  One is where the slurry ended up, the other is where the
    string it was pumped behind ends.  They come from different columns and are never
    interchangeable, and neither is ever computed from the other.
*   **No unit is defaulted.**  A volume with no stated unit stays text with a NULL value.  Assuming
    ``bbl`` for a number that was ``m3`` is a factor of six, in a quantity an engineer will quote.
*   **Nothing is calculated.**  There is no annular volume, no excess, no hydrostatic pressure and
    no displacement arithmetic here or anywhere in the platform.  WOC is stored as the wording the
    source printed.
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
    numeric,
    tables,
)

__all__ = [
    "CEMENT_ALIASES",
    "CEMENT_DENSITY_UNITS",
    "CEMENT_DEPTH_UNITS",
    "CEMENT_PRESSURE_UNITS",
    "CEMENT_VOLUME_UNITS",
    "CementJobEntry",
    "cement_job_entries",
    "locate_cement_header",
]

#: Unit decorations a cement header may carry, declared per quantity.  One shared vocabulary would
#: accept a unit the source never meant for that column.
CEMENT_VOLUME_UNITS: tuple[str, ...] = (
    "bbl",
    "bbls",
    "m3",
    "m^3",
    "gal",
    "gals",
    "l",
    "ltr",
    "litres",
)
CEMENT_DENSITY_UNITS: tuple[str, ...] = ("ppg", "lb/gal", "sg", "g/cm3", "g/cc", "kg/m3")
CEMENT_DEPTH_UNITS: tuple[str, ...] = ("ft", "feet", "m", "metre", "metres", "meter", "meters")
CEMENT_PRESSURE_UNITS: tuple[str, ...] = ("psi", "mpa", "kpa", "bar")

#: The cement job table's column vocabulary.  Bare ``volume`` and bare ``pressure`` are deliberately
#: absent: they are the columns a generic report has, and accepting them would turn any table of
#: numbers into a cement job.
CEMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "job_label": ("cement job", "job", "job no", "job no.", "job number", "job id", "job name"),
    "stage": ("stage", "stage no", "stage no.", "stage number", "stage #"),
    "job_type": ("job type", "cement type", "cementing type", "operation type"),
    "job_date": ("date", "job date", "cement date", "date pumped"),
    "lead_slurry": ("lead", "lead slurry", "lead cement", "lead slurry type", "lead blend"),
    "tail_slurry": ("tail", "tail slurry", "tail cement", "tail slurry type", "tail blend"),
    "lead_volume": ("lead volume", "lead vol", "lead vol.", "lead cement volume"),
    "tail_volume": ("tail volume", "tail vol", "tail vol.", "tail cement volume"),
    "total_volume": ("total volume", "total cement", "total cement volume", "cement volume"),
    "lead_density": ("lead density", "lead mw", "lead weight", "lead slurry density"),
    "tail_density": ("tail density", "tail mw", "tail weight", "tail slurry density"),
    "toc": ("toc", "top of cement", "cement top", "top cement", "toc depth"),
    "shoe": ("shoe", "shoe depth", "casing shoe", "casing shoe depth"),
    "displacement": ("displacement", "disp", "displacement volume", "displaced volume"),
    "pressure": ("pressure", "job pressure", "max pressure", "final pressure", "maximum pressure"),
    "woc": ("woc", "wait on cement", "waiting on cement", "woc hrs"),
    "returns": ("returns", "returns status", "cement returns", "returns to surface"),
    "casing": ("casing", "casing string", "casing size", "string", "casing run"),
    "well": ("well", "well name", "wellbore", "uwi"),
}

#: A cement volume column, which is what makes a table a cement table rather than a table of numbers.
_VOLUME_COLUMNS: tuple[str, ...] = ("lead_volume", "tail_volume", "total_volume")
#: A cement datum: something only a cement report states.
_CEMENT_COLUMNS: tuple[str, ...] = (
    "lead_slurry",
    "tail_slurry",
    "toc",
    "shoe",
    "displacement",
    "woc",
)

_HEADER_SEARCH_DEPTH = 8


@dataclass(frozen=True)
class CementJobEntry:
    """One recognised cement job or stage, exactly as the source stated it.

    Every quantity carries its source text, a value only when the text is unambiguously a number,
    and the unit the header stated.  ``total_volume_*`` is kept separate from lead and tail on
    purpose: a source that states only a total is not a source that stated a lead and a tail.
    """

    source_row_index: int
    job_label: str
    stage_text: str
    job_type: str
    job_date_text: str
    lead_slurry: str
    tail_slurry: str
    lead_volume_text: str
    lead_volume_value: float | None
    lead_volume_unit: str
    tail_volume_text: str
    tail_volume_value: float | None
    tail_volume_unit: str
    total_volume_text: str
    total_volume_value: float | None
    total_volume_unit: str
    lead_density_text: str
    lead_density_value: float | None
    lead_density_unit: str
    tail_density_text: str
    tail_density_value: float | None
    tail_density_unit: str
    toc_text: str
    toc_value: float | None
    toc_unit: str
    shoe_text: str
    shoe_value: float | None
    shoe_unit: str
    displacement_text: str
    displacement_value: float | None
    displacement_unit: str
    pressure_text: str
    pressure_value: float | None
    pressure_unit: str
    woc_text: str
    returns: str
    casing_reference: str
    well_name: str
    table: Mapping[str, Any]

    @property
    def states_anything(self) -> bool:
        """Whether the row states at least one cement quantity worth an operational row."""
        return bool(
            self.lead_volume_value is not None
            or self.tail_volume_value is not None
            or self.total_volume_value is not None
            or self.toc_value is not None
            or self.shoe_value is not None
            or self.lead_slurry.strip()
            or self.tail_slurry.strip()
        )


def _cell(row: Sequence[Any], columns: Mapping[str, int], name: str) -> str:
    """The stripped text of one aliased column, or ``""`` when the table has no such column."""
    return cell_text(row, columns.get(name, -1))


def _measured(
    row: Sequence[Any],
    header_row: Sequence[Any],
    columns: Mapping[str, int],
    name: str,
    units: Sequence[str],
) -> tuple[str, float | None, str]:
    """``(source text, value or None, the unit the header stated)`` for one aliased column.

    A value is stored only when the source also stated the unit it is in.  A bare number in an
    unlabelled column is not a measurement in any unit this platform may assert, so it stays text
    with a NULL value rather than acquiring a plausible unit.
    """
    index = columns.get(name, -1)
    raw = cell_text(row, index)
    unit = header_unit(cell_text(header_row, index), "", units)
    return raw, (numeric(raw, units) if unit else None), unit


def _is_cement_header(headers: Mapping[str, int]) -> bool:
    """Whether a header row is a cement job table rather than a table that merely has numbers."""
    has_volume = any(alias_column(headers, CEMENT_ALIASES[name]) >= 0 for name in _VOLUME_COLUMNS)
    has_datum = any(alias_column(headers, CEMENT_ALIASES[name]) >= 0 for name in _CEMENT_COLUMNS)
    return has_volume and has_datum


def locate_cement_header(
    rows: Sequence[Sequence[Any]],
) -> tuple[int, Sequence[Any], Mapping[str, int]] | None:
    """``(row index, header row, columns)`` for the first row that is a cement table's header."""
    for index, row in enumerate(rows[:_HEADER_SEARCH_DEPTH]):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            continue
        headers = header_index(row, strip_units=True)
        if _is_cement_header(headers):
            return index, row, headers
    return None


def cement_job_entries(payload: Mapping[str, Any]) -> list[CementJobEntry]:
    """Every recognised cement job or stage in the stored tables of one artefact.

    A table without a cement-specific volume column *and* a cement-specific datum is not read at
    all - it stays evidence.  A row that states no cement quantity is skipped rather than stored as
    an empty job, because a row asserting a job happened is a claim the source has to support.
    """
    found: list[CementJobEntry] = []
    for table in tables(payload):
        rows = list(table.get("rows") or [])
        located = locate_cement_header(rows)
        if located is None:
            continue
        header_row_index, header_row, headers = located
        columns = {name: alias_column(headers, aliases) for name, aliases in CEMENT_ALIASES.items()}

        for offset, row in enumerate(rows[header_row_index + 1 :], start=header_row_index + 1):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                continue
            # One call per column: each measured fact is a (text, value, unit) triple, and reading
            # it three times to pull the three apart would be three chances to disagree.
            measured = {
                name: _measured(row, header_row, columns, name, units)
                for name, units in (
                    ("lead_volume", CEMENT_VOLUME_UNITS),
                    ("tail_volume", CEMENT_VOLUME_UNITS),
                    ("total_volume", CEMENT_VOLUME_UNITS),
                    ("lead_density", CEMENT_DENSITY_UNITS),
                    ("tail_density", CEMENT_DENSITY_UNITS),
                    ("toc", CEMENT_DEPTH_UNITS),
                    ("shoe", CEMENT_DEPTH_UNITS),
                    ("displacement", CEMENT_VOLUME_UNITS),
                    ("pressure", CEMENT_PRESSURE_UNITS),
                )
            }

            entry = CementJobEntry(
                source_row_index=offset,
                job_label=_cell(row, columns, "job_label"),
                stage_text=_cell(row, columns, "stage"),
                job_type=_cell(row, columns, "job_type"),
                job_date_text=_cell(row, columns, "job_date"),
                lead_slurry=_cell(row, columns, "lead_slurry"),
                tail_slurry=_cell(row, columns, "tail_slurry"),
                lead_volume_text=measured["lead_volume"][0],
                lead_volume_value=measured["lead_volume"][1],
                lead_volume_unit=measured["lead_volume"][2],
                tail_volume_text=measured["tail_volume"][0],
                tail_volume_value=measured["tail_volume"][1],
                tail_volume_unit=measured["tail_volume"][2],
                total_volume_text=measured["total_volume"][0],
                total_volume_value=measured["total_volume"][1],
                total_volume_unit=measured["total_volume"][2],
                lead_density_text=measured["lead_density"][0],
                lead_density_value=measured["lead_density"][1],
                lead_density_unit=measured["lead_density"][2],
                tail_density_text=measured["tail_density"][0],
                tail_density_value=measured["tail_density"][1],
                tail_density_unit=measured["tail_density"][2],
                toc_text=measured["toc"][0],
                toc_value=measured["toc"][1],
                toc_unit=measured["toc"][2],
                shoe_text=measured["shoe"][0],
                shoe_value=measured["shoe"][1],
                shoe_unit=measured["shoe"][2],
                displacement_text=measured["displacement"][0],
                displacement_value=measured["displacement"][1],
                displacement_unit=measured["displacement"][2],
                pressure_text=measured["pressure"][0],
                pressure_value=measured["pressure"][1],
                pressure_unit=measured["pressure"][2],
                woc_text=_cell(row, columns, "woc"),
                returns=_cell(row, columns, "returns"),
                casing_reference=_cell(row, columns, "casing"),
                well_name=_cell(row, columns, "well"),
                table=table,
            )
            if entry.states_anything:
                found.append(entry)
    return found
