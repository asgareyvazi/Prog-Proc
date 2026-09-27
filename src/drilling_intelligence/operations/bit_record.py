"""Static, source-shaped bit-record parsing.

A bit record is a *run history*: one row per bit that was run, with what it was, where it went in and
came out, how much hole it made and why it was pulled.  This module reads the tables the extractor
already stored and recognises exactly one shape: a bit tally with an explicit header row.

The acceptance rule is narrow on purpose:

*   a column the vocabulary knows as the **bit number**, *and*
*   at least one of ``size``, ``footage``, ``rotating hours``, ``drilling hours`` or ``iadc code``.

A table with a "Bit" column and nothing else is a parts list, and a table of durations with a bit
column is a time breakdown; neither becomes a bit run here.

What it does **not** do:

*   it never computes footage from depth in/out, ROP from hours, or a bearing/seal grade from a pull
    reason.  Every number on a promoted row is a number the source printed;
*   it never ranks or compares bits.  There is no performance engine in the platform and this parser
    does not build one;
*   it never invents an IADC code from a bit's description, nor a nozzle count from a nozzle size.

A replacement bit is a *new* run, not an edit of the previous one: identity is carried by the source's
own bit/run number (see :mod:`drilling_intelligence.operations.promote`), so a re-tally that adds a
bit leaves the earlier runs in place as history.
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
    "BIT_ALIASES",
    "BitRunEntry",
    "bit_run_entries",
    "bit_run_tables",
]

#: Unit decorations a bit table's headers may carry.
BIT_UNIT_TOKENS: tuple[str, ...] = ("in", "inch", "inches", "ft", "feet", "m", "hrs", "hr", "h")

#: The bit tally's column vocabulary.
BIT_ALIASES: dict[str, tuple[str, ...]] = {
    "bit_number": ("bit no", "bit no.", "bit number", "bit #", "bit num", "no of bit"),
    "run_number": ("run no", "run no.", "run number", "run #", "run"),
    "manufacturer": ("manufacturer", "make", "mfr", "vendor", "brand", "supplier"),
    "model": ("model", "model no", "model no.", "model number", "part number"),
    "bit_type": ("bit type", "type", "type of bit", "bit class"),
    "iadc_code": ("iadc", "iadc code", "iadc classification", "iadc no"),
    "serial": ("serial", "serial no", "serial no.", "serial number", "sn", "s/n", "serial #"),
    "size": ("size", "bit size", "diameter", "bit diameter"),
    "depth_in": ("depth in", "in depth", "depth in md", "spud depth", "start depth"),
    "depth_out": ("depth out", "out depth", "depth out md", "pull depth", "end depth"),
    "footage": ("footage", "footage drilled", "ft drilled", "drilled footage", "footage made"),
    "rotating_hours": ("rotating hours", "rotary hours", "rotating hrs", "rotating time"),
    "drilling_hours": (
        "drilling hours",
        "drilling hrs",
        "drilling time",
        "on bottom hours",
        "on-bottom hours",
        "on bottom time",
    ),
    "pull_reason": (
        "pull reason",
        "reason pulled",
        "reason for pull",
        "reason for pulling",
        "pulled for",
        "dull reason",
    ),
    "dull_grade": ("dull grade", "dull grading", "iadc dull", "dull condition", "dull"),
    "nozzle_count": ("nozzles", "nozzle count", "number of nozzles", "nozzle no"),
    "nozzle_size": ("nozzle size", "nozzle sizes", "tfa"),
    "bha_number": ("bha no", "bha no.", "bha number", "bha run"),
    "section": ("section", "hole section", "section id"),
    "well": ("well", "well name", "wellbore", "uwi"),
    "run_date": ("date", "run date", "start date"),
}

#: The columns that make a bit table a bit *history* rather than a list of hardware.
_MEASUREMENT_COLUMNS: tuple[str, ...] = (
    "size",
    "footage",
    "rotating_hours",
    "drilling_hours",
    "iadc_code",
)


@dataclass(frozen=True)
class BitRunEntry:
    """One recognised bit run row, exactly as the source stated it."""

    source_row_index: int
    bit_number: str
    run_number: str
    manufacturer: str
    model: str
    bit_type: str
    iadc_code: str
    serial_number: str
    size_text: str
    size_value: float | None
    size_unit: str
    depth_in_text: str
    depth_in_value: float | None
    depth_in_unit: str
    depth_out_text: str
    depth_out_value: float | None
    depth_out_unit: str
    footage_text: str
    footage_value: float | None
    footage_unit: str
    rotating_hours: float | None
    drilling_hours: float | None
    pull_reason: str
    dull_grade: str
    nozzle_count: int | None
    nozzle_size_text: str
    bha_number: str
    section_text: str
    well_name: str
    run_date_text: str
    table: Mapping[str, Any]


def _columns_of(row: Sequence[Any]) -> dict[str, int] | None:
    """The bit tally's column map, or ``None`` when the row is not a bit tally header.

    Every lookup is exact (unit decoration is folded into the header index, not searched for by
    prefix) because a prefix search for ``"bit"`` would match ``"bit size"``, which is a different
    column and a different meaning.
    """
    headers = header_index(row, strip_units=True)
    columns = {name: alias_column(headers, aliases) for name, aliases in BIT_ALIASES.items()}
    if columns["bit_number"] < 0:
        return None
    if not any(columns[name] >= 0 for name in _MEASUREMENT_COLUMNS):
        return None
    return columns


def bit_run_tables(payload: Mapping[str, Any]) -> list[tuple[dict[str, Any], dict[str, int], int]]:
    """``(table, columns, header_row_index)`` for every stored table that is a bit tally."""
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


def _number(row: Sequence[Any], columns: Mapping[str, int], name: str) -> tuple[str, float | None]:
    text = _text(row, columns, name)
    return text, numeric(text, BIT_UNIT_TOKENS)


def bit_run_entries(payload: Mapping[str, Any]) -> tuple[BitRunEntry, ...]:
    """Every recognised bit run row of every bit tally, in source order."""
    entries: list[BitRunEntry] = []
    for table, columns, header_row in bit_run_tables(payload):
        rows = table.get("rows") or []
        header_values = rows[header_row]
        for row_index, row in enumerate(rows[header_row + 1 :], start=header_row + 1):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                continue
            if not any(str(value or "").strip() for value in row):
                continue
            bit_number = _text(row, columns, "bit_number")
            if not bit_number:
                continue
            # The same rule the BHA tally applies: a row stating nothing but the bit's number is a
            # label or a footnote, not a run with anything recorded about it.
            if not any(
                _text(row, columns, name)
                for name in columns
                if name != "bit_number" and columns[name] >= 0
            ):
                continue
            size_text, size_value = _number(row, columns, "size")
            depth_in_text, depth_in_value = _number(row, columns, "depth_in")
            depth_out_text, depth_out_value = _number(row, columns, "depth_out")
            footage_text, footage_value = _number(row, columns, "footage")
            _, rotating_hours = _number(row, columns, "rotating_hours")
            _, drilling_hours = _number(row, columns, "drilling_hours")
            nozzle_text = _text(row, columns, "nozzle_count")
            nozzle_count = numeric(nozzle_text, BIT_UNIT_TOKENS)
            entries.append(
                BitRunEntry(
                    source_row_index=row_index,
                    bit_number=bit_number,
                    run_number=_text(row, columns, "run_number"),
                    manufacturer=_text(row, columns, "manufacturer"),
                    model=_text(row, columns, "model"),
                    bit_type=_text(row, columns, "bit_type"),
                    iadc_code=_text(row, columns, "iadc_code").strip().upper(),
                    serial_number=_text(row, columns, "serial"),
                    size_text=size_text,
                    size_value=size_value,
                    size_unit=header_unit(
                        cell_text(header_values, columns["size"]), "", BIT_UNIT_TOKENS
                    ),
                    depth_in_text=depth_in_text,
                    depth_in_value=depth_in_value,
                    depth_in_unit=header_unit(
                        cell_text(header_values, columns["depth_in"]), "", BIT_UNIT_TOKENS
                    ),
                    depth_out_text=depth_out_text,
                    depth_out_value=depth_out_value,
                    depth_out_unit=header_unit(
                        cell_text(header_values, columns["depth_out"]), "", BIT_UNIT_TOKENS
                    ),
                    footage_text=footage_text,
                    footage_value=footage_value,
                    footage_unit=header_unit(
                        cell_text(header_values, columns["footage"]), "", BIT_UNIT_TOKENS
                    ),
                    rotating_hours=rotating_hours,
                    drilling_hours=drilling_hours,
                    pull_reason=_text(row, columns, "pull_reason"),
                    dull_grade=_text(row, columns, "dull_grade"),
                    nozzle_count=int(nozzle_count) if nozzle_count is not None else None,
                    nozzle_size_text=_text(row, columns, "nozzle_size"),
                    bha_number=_text(row, columns, "bha_number"),
                    section_text=_text(row, columns, "section"),
                    well_name=_text(row, columns, "well"),
                    run_date_text=_text(row, columns, "run_date"),
                    table=table,
                )
            )
    return tuple(entries)
