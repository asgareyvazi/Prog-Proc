"""Static, source-shaped casing-run parsing.

A casing report states what was actually run: a string, its size, its weight and grade, and the
depth its shoe landed at.  This module reads the tables the extractor already stored and recognises
exactly one shape - a casing tally with an explicit header row.

The acceptance rule is the narrowest one that means "this is a casing run":

*   a **size / OD** column, *and*
*   a **shoe depth** column, *and*
*   at least one **string property** - grade, weight, connection or an explicit type column.

Each condition rules out something real that is not a casing run.  A tubular inventory has a size,
a weight and a grade but no shoe depth.  A hole-section plan has depths and a size but no grade,
weight or connection - and a plan is the other side of the plan-versus-actual line this module
exists to keep.  A narrative mentions casing and states no table at all.

Four rules this module exists to keep:

*   **The string type is never inferred.**  ``string_type`` is what an explicit type column says,
    and NULL when there is no such column.  A 9 5/8 in string is not automatically production
    casing and a deep string is not automatically intermediate: size and depth are measurements,
    and reading a classification out of them would be a guess dressed as data.  A sheet that only
    embeds the type in a free-text label keeps that label verbatim and leaves the type unset.
*   **Nothing is converted.**  Inches stay inches, ``lb/ft`` stays ``lb/ft``, feet stay feet.  Every
    dimension is kept as three separate facts - the source's text, a value only when the text is
    unambiguously a number, and the unit the source printed - so ``9 5/8 in`` survives as
    ``9 5/8 in`` rather than becoming a float somebody reverse-engineered.
*   **Top and shoe are different depths.**  They are read from different columns and are never
    substituted for one another.
*   **A plan is not an actual.**  A table that carries both a planned and an actual shoe column is
    refused rather than picked from, because choosing one side silently would state the other as
    fact.
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
    "CASING_ALIASES",
    "CASING_DEPTH_UNITS",
    "CASING_SIZE_UNITS",
    "CASING_STRING_TYPES",
    "CASING_WEIGHT_UNITS",
    "CasingRunEntry",
    "casing_run_entries",
    "casing_table_is_ambiguous",
    "locate_casing_header",
]

#: Unit decorations a casing header may carry.  Declared per quantity because a size in ``mm`` and a
#: depth in ``m`` are not the same unit, and one vocabulary for both would accept a header the
#: source never meant.
CASING_SIZE_UNITS: tuple[str, ...] = ("in", "inch", "inches", "mm")
CASING_WEIGHT_UNITS: tuple[str, ...] = ("lb/ft", "lbs/ft", "lbft", "kg/m", "kgm")
CASING_DEPTH_UNITS: tuple[str, ...] = ("ft", "feet", "m", "metre", "metres", "meter", "meters")

#: What a source may call a string.  Closed on purpose: a label this vocabulary does not know is
#: kept in ``string_label`` verbatim rather than being folded into the nearest type.
CASING_STRING_TYPES: frozenset[str] = frozenset(
    {
        "conductor",
        "surface",
        "intermediate",
        "production",
        "liner",
        "tieback",
        "contingency",
    }
)

#: The casing tally's column vocabulary.  Order within each tuple decides precedence, so the
#: contract - not the order the sheet happens to print its columns - controls which spelling wins.
CASING_ALIASES: dict[str, tuple[str, ...]] = {
    "size": ("size", "od", "casing size", "casing od", "nominal od", "nominal size", "od size"),
    "string_label": (
        "casing",
        "casing string",
        "string",
        "string name",
        "string description",
        "description",
    ),
    "string_type": ("type", "string type", "casing type", "string category"),
    "weight": ("weight", "wt", "linear weight", "nominal weight", "unit weight"),
    "grade": ("grade", "steel grade", "material grade", "casing grade"),
    "connection": ("connection", "conn", "thread", "thread form", "connection type"),
    "top": ("top", "top depth", "top md", "from", "from depth", "top md ft", "top of string"),
    "shoe": ("shoe", "shoe depth", "bottom", "bottom depth", "to", "to depth", "shoe md"),
    "run_date": ("date", "run date", "running date", "date run", "run"),
    "well": ("well", "well name", "wellbore", "uwi"),
    "section": ("section", "hole section", "section name"),
}

#: Headers that say the column belongs to a *plan* rather than to what was run.  A table carrying
#: both sides is refused: picking one would state the other as fact.
_PLANNED_MARKERS: tuple[str, ...] = ("planned", "plan", "design", "programme", "program", "target")
_ACTUAL_MARKERS: tuple[str, ...] = ("actual", "as run", "as-run", "final")

#: How far down a stored table to look for the header row, matching the other domain parsers.
_HEADER_SEARCH_DEPTH = 8


@dataclass(frozen=True)
class CasingRunEntry:
    """One recognised casing string, exactly as the source stated it.

    Every dimension carries three facts: the source's text, a value only when that text is
    unambiguously a number, and the unit the header stated.  A mixed-fraction size such as
    ``9 5/8 in`` keeps its text and leaves the value ``None`` - deriving 9.625 would be a decision
    the source did not make.
    """

    source_row_index: int
    string_label: str
    string_type: str
    size_text: str
    size_value: float | None
    size_unit: str
    weight_text: str
    weight_value: float | None
    weight_unit: str
    grade: str
    connection: str
    top_text: str
    top_value: float | None
    top_unit: str
    shoe_text: str
    shoe_value: float | None
    shoe_unit: str
    run_date_text: str
    well_name: str
    section_text: str
    table: Mapping[str, Any]


def _string_type(text: str) -> str:
    """The canonical type an explicit type column states, or ``""`` when it states none.

    Only the vocabulary's own words count, matched whole.  ``"Prod"`` is production; ``"9 5/8 in"``
    is nothing at all, because a size is not a type and there is no convention safe enough to
    promote to a rule here.
    """
    wanted = text.strip().casefold()
    if not wanted:
        return ""
    for known in sorted(CASING_STRING_TYPES):
        if wanted == known:
            return known
    for known, spellings in (
        ("surface", ("surface casing",)),
        ("intermediate", ("intermediate casing", "int", "inter")),
        ("production", ("production casing", "prod", "production string")),
        ("tieback", ("tie back", "tie-back", "tieback string")),
        ("contingency", ("contingency string", "contingency liner")),
        ("conductor", ("conductor casing",)),
        ("liner", ("liner string",)),
    ):
        if wanted in spellings:
            return known
    return ""


def _is_casing_header(headers: Mapping[str, int]) -> bool:
    """Whether a header row is a casing tally rather than an inventory, a plan or a narrative."""
    has_size = alias_column(headers, CASING_ALIASES["size"]) >= 0
    # A header *containing* "shoe" counts here, not only an exact alias match.  That is what lets a
    # table headed "Planned Shoe (ft) | Actual Shoe (ft)" be recognised as a casing table at all,
    # so it can be refused for the specific reason it deserves - it states both sides and this
    # contract will not choose between them - instead of vanishing as an unsupported shape.
    has_shoe = alias_column(headers, CASING_ALIASES["shoe"]) >= 0 or any(
        "shoe" in label for label in headers
    )
    has_property = any(
        alias_column(headers, CASING_ALIASES[name]) >= 0
        for name in ("grade", "weight", "connection", "string_type")
    )
    return has_size and has_shoe and has_property


def _states_both_sides(header_row: Sequence[Any]) -> bool:
    """Whether the table labels one column planned and another actual.

    Checked over the raw header text rather than the alias vocabulary, because the words a sheet
    uses to separate a plan from a result are the ones a reviewer would read, and a table that
    prints both cannot be promoted without choosing.
    """
    labels = [str(cell or "").strip().casefold() for cell in header_row]
    planned = any(any(marker in label for marker in _PLANNED_MARKERS) for label in labels)
    actual = any(any(marker in label for marker in _ACTUAL_MARKERS) for label in labels)
    return planned and actual


def locate_casing_header(
    rows: Sequence[Sequence[Any]],
) -> tuple[int, Sequence[Any], Mapping[str, int]] | None:
    """``(row index, header row, columns)`` for the first row that is a casing tally's header.

    The header is found rather than assumed to be first: an extractor stores the sheet region it
    found, which usually includes a title and a note above the column headings.
    """
    for index, row in enumerate(rows[:_HEADER_SEARCH_DEPTH]):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            continue
        headers = header_index(row, strip_units=True)
        if _is_casing_header(headers):
            return index, row, headers
    return None


def casing_table_is_ambiguous(rows: Sequence[Sequence[Any]]) -> bool:
    """Whether a table that *looks* like a casing tally mixes plan and actual without saying which."""
    located = locate_casing_header(rows)
    return located is not None and _states_both_sides(located[1])


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

    A value is stored only when the source also stated the unit it is in.  A bare number in a
    column nobody labelled is not a measurement in any unit this platform is entitled to assert,
    so it stays as text with a NULL value.
    """
    index = columns.get(name, -1)
    raw = cell_text(row, index)
    unit = header_unit(cell_text(header_row, index), "", units)
    return raw, (numeric(raw, units) if unit else None), unit


def casing_run_entries(payload: Mapping[str, Any]) -> list[CasingRunEntry]:
    """Every recognised casing string in the stored tables of one artefact.

    A table without a size, a shoe depth and at least one string property is not read at all - it
    stays evidence.  A row with no size or no shoe depth is skipped rather than stored half-formed,
    because a casing run whose shoe depth is unknown is not a run the platform can place in a well.
    """
    found: list[CasingRunEntry] = []
    for table in tables(payload):
        rows = list(table.get("rows") or [])
        located = locate_casing_header(rows)
        if located is None:
            continue
        header_row_index, header_row, headers = located
        if _states_both_sides(header_row):
            continue

        columns = {name: alias_column(headers, aliases) for name, aliases in CASING_ALIASES.items()}

        for offset, row in enumerate(rows[header_row_index + 1 :], start=header_row_index + 1):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                continue
            size_text, size_value, size_unit = _measured(
                row, header_row, columns, "size", CASING_SIZE_UNITS
            )
            shoe_text, shoe_value, shoe_unit = _measured(
                row, header_row, columns, "shoe", CASING_DEPTH_UNITS
            )
            if not size_text.strip() or not shoe_text.strip():
                # No size or no shoe depth: nothing to place in a well, so the row stays evidence.
                continue
            found.append(
                CasingRunEntry(
                    source_row_index=offset,
                    string_label=_cell(row, columns, "string_label"),
                    string_type=_string_type(_cell(row, columns, "string_type")),
                    size_text=size_text,
                    size_value=size_value,
                    size_unit=size_unit,
                    weight_text=_cell(row, columns, "weight"),
                    weight_value=_measured(row, header_row, columns, "weight", CASING_WEIGHT_UNITS)[
                        1
                    ],
                    weight_unit=header_unit(
                        cell_text(header_row, columns["weight"]), "", CASING_WEIGHT_UNITS
                    ),
                    grade=_cell(row, columns, "grade"),
                    connection=_cell(row, columns, "connection"),
                    top_text=_cell(row, columns, "top"),
                    top_value=_measured(row, header_row, columns, "top", CASING_DEPTH_UNITS)[1],
                    top_unit=header_unit(
                        cell_text(header_row, columns["top"]), "", CASING_DEPTH_UNITS
                    ),
                    shoe_text=shoe_text,
                    shoe_value=shoe_value,
                    shoe_unit=shoe_unit,
                    run_date_text=_cell(row, columns, "run_date"),
                    well_name=_cell(row, columns, "well"),
                    section_text=_cell(row, columns, "section"),
                    table=table,
                )
            )
    return found
