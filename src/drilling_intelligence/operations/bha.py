"""Static, source-shaped BHA (bottom hole assembly) parsing.

A BHA report is a *tally*: an ordered list of the hardware that went in the hole, plus the run it
belongs to.  This module reads the tables the extractor already stored and recognises exactly two
shapes, the same way :mod:`drilling_intelligence.operations.mud` does:

* a label/value summary table that names the run (BHA/run number, date, the run interval); and
* a component table with an explicit header row.

What it does **not** do:

* it never infers a component type the source did not state - :data:`COMPONENT_TYPES` is a closed
  vocabulary matched against the component's own words, and an unmatched description leaves
  ``component_type`` empty rather than becoming a guess;
* it never computes a total length, an assembly OD or an ID from its components.  A tally's arithmetic
  is the engineer's, not the ingestion layer's;
* it never orders components by anything but the source's own row order.  Position in the string is
  part of what a BHA is, so a re-ordered tally is a different tally;
* it never reads prose.  A BHA narrative in a DOCX stays extraction evidence.
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
    "COMPONENT_ALIASES",
    "COMPONENT_TYPES",
    "SUMMARY_ALIASES",
    "BhaComponentEntry",
    "BhaSummaryEntry",
    "canonical_component_type",
    "component_entries",
    "component_table_key",
    "component_tables",
    "summary_entries",
]

#: Unit decorations a BHA header may carry.  Inches, feet and metres are the tally's own vocabulary.
BHA_UNIT_TOKENS: tuple[str, ...] = ("in", "inch", "inches", "ft", "feet", "m", "mm", "cm")

#: The label/value summary vocabulary.  A label not in this list is evidence, not a BHA attribute.
SUMMARY_ALIASES: dict[str, tuple[str, ...]] = {
    "well": ("well", "well name", "wellbore", "uwi"),
    "field": ("field", "field name"),
    "bha_number": (
        "bha no",
        "bha no.",
        "bha number",
        "bha #",
        "bha run",
        "bha run no",
        "bha run no.",
        "run no",
        "run no.",
        "run number",
        "assembly no",
        "assembly no.",
        "assembly number",
    ),
    "report_date": ("report date", "date", "bha date", "run date", "assembly date"),
    "top_depth": ("from md", "top md", "md from", "top depth", "interval from", "start md"),
    "bottom_depth": ("to md", "bottom md", "md to", "bottom depth", "interval to", "end md"),
    "assembly_description": (
        "assembly",
        "assembly description",
        "bha description",
        "bha",
        "description",
    ),
    "section_id": ("section id", "section identifier", "hole section id"),
    "section": ("section", "hole section", "interval"),
    "hole_size_in": ("hole size", "hole size in", "nominal hole size"),
}

#: The component table's column vocabulary.  ``description`` is the only column a component row
#: needs; everything else is optional and stays empty when the source did not state it.
COMPONENT_ALIASES: dict[str, tuple[str, ...]] = {
    "sequence": ("no", "no.", "#", "item no", "item no.", "order", "seq", "sequence", "position"),
    "description": (
        "description",
        "component",
        "component description",
        "item description",
        "bha component",
        "assembly component",
        "part",
        "part description",
        "equipment",
        "item",
    ),
    "component_type": ("component type", "type", "category", "component class"),
    "manufacturer": (
        "manufacturer",
        "make",
        "mfr",
        "vendor",
        "supplier",
        "brand",
        "manufacturer name",
    ),
    "model": (
        "model",
        "model no",
        "model no.",
        "model number",
        "part number",
        "part no",
        "part no.",
        "catalogue no",
    ),
    "serial": ("serial", "serial no", "serial no.", "serial number", "sn", "s/n", "serial #"),
    "od": ("od", "outside diameter", "o.d."),
    "inner_diameter": ("id", "inside diameter", "i.d."),
    "length": ("length", "len", "joint length", "length per joint"),
    "quantity": ("qty", "quantity", "count", "pieces", "nos", "number of joints"),
}

#: Canonical component types, matched against the component's own words.  Deliberately short and
#: deliberately exact: a description that does not *start with* one of these phrases leaves the type
#: unknown, because inventing a type from prose is exactly the guess this module refuses to make.
COMPONENT_TYPES: dict[str, tuple[str, ...]] = {
    "DRILL_COLLAR": ("drill collar", "drill collars", "dc"),
    "HEAVY_WEIGHT_DRILL_PIPE": ("heavy weight drill pipe", "heavy-weight drill pipe", "hwdp"),
    "DRILL_PIPE": ("drill pipe", "drill pipes"),
    "STABILIZER": (
        "stabilizer",
        "stabiliser",
        "stab",
        "string stabilizer",
        "near bit stabilizer",
        # The hyphenated spelling of the same phrase.  Listed rather than produced by folding hyphens
        # in ``normalise_label``, which ``"6-1/4"`` and ``"x-over"`` rely on keeping.
        "near-bit stabilizer",
        "near-bit stabiliser",
    ),
    "MUD_MOTOR": ("mud motor", "pdm", "positive displacement motor", "downhole motor"),
    "MWD_TOOL": ("mwd", "mwd tool", "measurement while drilling"),
    "LWD_TOOL": ("lwd", "lwd tool", "logging while drilling"),
    "JAR": ("jar", "drilling jar", "mechanical jar", "hydraulic jar"),
    "BIT": ("bit", "pdc bit", "roller cone bit", "tricone bit"),
    "REAMER": ("reamer", "hole opener", "underreamer", "under reamer"),
    "FLOAT_SUB": ("float sub", "float shoe", "float collar"),
    "BIT_SUB": ("bit sub",),
    "SAFETY_SUB": ("safety sub",),
    "CIRCULATING_SUB": ("circulating sub",),
    "BENT_SUB": ("bent sub", "bent housing"),
    "CROSSOVER": ("crossover", "x-over", "xo sub"),
    "DRILL_STRING_SAFETY_VALVE": ("safety valve", "kelly cock", "full opening safety valve"),
}

#: Alias phrases ordered longest-first, so ``"heavy weight drill pipe"`` is matched before any shorter
#: phrase that could share its prefix.  Built once; the order is a property of the vocabulary, not of
#: a particular source.
_TYPE_PHRASES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((normalise_label(alias), canonical) for canonical, aliases in COMPONENT_TYPES.items() for alias in aliases),
        key=lambda pair: (-len(pair[0]), pair[0]),
    )
)


def canonical_component_type(description: Any) -> str:
    """The canonical type a component's own words state, or ``""`` when they state none."""
    text = without_units(str(description or ""), BHA_UNIT_TOKENS)
    for phrase, canonical in _TYPE_PHRASES:
        if text == phrase:
            return canonical
        if text.startswith(phrase) and (len(text) == len(phrase) or not text[len(phrase)].isalnum()):
            return canonical
    return ""


def canonical_summary_label(value: Any) -> str:
    """The summary label a source row states, or ``""`` when the label is not in the vocabulary."""
    label = normalise_label(value)
    stripped = without_units(label, BHA_UNIT_TOKENS)
    for canonical, aliases in SUMMARY_ALIASES.items():
        if label in aliases or stripped in aliases:
            return canonical
    for canonical, aliases in SUMMARY_ALIASES.items():
        if any(stripped == alias or stripped.startswith(alias + " ") for alias in aliases):
            return canonical
    return ""


@dataclass(frozen=True)
class BhaSummaryEntry:
    """One recognised label/value row of a BHA summary table."""

    property_name: str
    source_label: str
    source_value: str
    source_unit: str
    table: Mapping[str, Any]
    row_index: int


@dataclass(frozen=True)
class BhaComponentEntry:
    """One recognised component row, in the source's own order."""

    sequence: int
    source_row_index: int
    source_label: str
    component_type: str
    stated_type: str
    manufacturer: str
    model: str
    serial_number: str
    od_text: str
    od_value: float | None
    od_unit: str
    id_text: str
    id_value: float | None
    id_unit: str
    length_text: str
    length_value: float | None
    length_unit: str
    quantity: int | None
    table: Mapping[str, Any]


def summary_entries(payload: Mapping[str, Any]) -> tuple[BhaSummaryEntry, ...]:
    """Recognised label/value rows from every stored table, in stored order."""
    entries: list[BhaSummaryEntry] = []
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
                BhaSummaryEntry(
                    property_name=property_name,
                    source_label=label,
                    source_value=source_value,
                    # A contract may keep an unqualified value, but may not invent its usual unit.
                    source_unit=cell_text(row, 2) or header_unit(label, "", BHA_UNIT_TOKENS),
                    table=table,
                    row_index=row_index,
                )
            )
    return tuple(entries)


def _component_columns(row: Sequence[Any]) -> tuple[dict[str, int], int] | None:
    """``(columns, header_row_index)`` when a row is a BHA component header, else ``None``.

    The contract is deliberately narrow: a description column **and** at least one sizing column.  A
    table with a description and nothing measurable is a parts list, not a tally, and admitting it
    would let any equipment table become a bottom hole assembly.
    """
    headers = header_index(row, strip_units=True)
    columns = {
        name: alias_column(headers, aliases) for name, aliases in COMPONENT_ALIASES.items()
    }
    if columns["description"] < 0:
        return None
    if not any(columns[name] >= 0 for name in ("od", "inner_diameter", "length")):
        return None
    return columns, -1


def component_tables(payload: Mapping[str, Any]) -> list[tuple[dict[str, Any], dict[str, int], int]]:
    """``(table, columns, header_row_index)`` for every stored table that is a component tally."""
    found: list[tuple[dict[str, Any], dict[str, int], int]] = []
    for table in tables(payload):
        for row_index, row in enumerate((table.get("rows") or [])[:8]):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                continue
            matched = _component_columns(row)
            if matched is None:
                continue
            columns, _ = matched
            found.append((table, columns, row_index))
            break
    return found


def component_entries(payload: Mapping[str, Any]) -> tuple[BhaComponentEntry, ...]:
    """Every recognised component row of every component table, in source order.

    ``sequence`` restarts at 1 for each table and counts only rows the contract admits, so it is the
    assembly position of the *recognised* components.  A row the vocabulary does not recognise is not
    silently dropped from the tally: it is retained as extraction evidence and the gap is visible in
    the row count the promoter reports.
    """
    entries: list[BhaComponentEntry] = []
    for table, columns, header_row in component_tables(payload):
        headers = table.get("rows") or []
        header_values = headers[header_row]
        sequence = 0
        for row_index, row in enumerate(headers[header_row + 1 :], start=header_row + 1):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                continue
            if not any(str(value or "").strip() for value in row):
                continue
            label = cell_text(row, columns["description"])
            if not label:
                continue
            # A tally's footnote lives in the description column too.  A row that states nothing but
            # words - no dimension, no quantity, no make, no serial - is a note about the assembly,
            # not a component of it, and admitting it would put a sentence in the middle of the
            # string's order.
            others = [
                cell_text(row, index)
                for name, index in columns.items()
                if name != "description" and index >= 0
            ]
            if not any(others):
                continue
            sequence += 1
            od_text = cell_text(row, columns["od"])
            id_text = cell_text(row, columns["inner_diameter"])
            length_text = cell_text(row, columns["length"])
            quantity_text = cell_text(row, columns["quantity"])
            quantity = numeric(quantity_text, BHA_UNIT_TOKENS)
            entries.append(
                BhaComponentEntry(
                    sequence=sequence,
                    source_row_index=row_index,
                    source_label=label,
                    component_type=canonical_component_type(label),
                    stated_type=cell_text(row, columns["component_type"]),
                    manufacturer=cell_text(row, columns["manufacturer"]),
                    model=cell_text(row, columns["model"]),
                    serial_number=cell_text(row, columns["serial"]),
                    od_text=od_text,
                    od_value=numeric(od_text, BHA_UNIT_TOKENS),
                    od_unit=header_unit(
                        cell_text(header_values, columns["od"]), "", BHA_UNIT_TOKENS
                    ),
                    id_text=id_text,
                    id_value=numeric(id_text, BHA_UNIT_TOKENS),
                    id_unit=header_unit(
                        cell_text(header_values, columns["inner_diameter"]), "", BHA_UNIT_TOKENS
                    ),
                    length_text=length_text,
                    length_value=numeric(length_text, BHA_UNIT_TOKENS),
                    length_unit=header_unit(
                        cell_text(header_values, columns["length"]), "", BHA_UNIT_TOKENS
                    ),
                    quantity=int(quantity) if quantity is not None else None,
                    table=table,
                )
            )
    return tuple(entries)


def component_table_key(entries: Sequence[BhaComponentEntry]) -> str:
    """The identity of the table the components came from (the first one, in stored order)."""
    for entry in entries:
        return table_key(entry.table)
    return ""
