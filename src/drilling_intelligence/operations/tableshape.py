"""Shared, static reading helpers for the source-shaped operational parsers.

The mud, BHA, bit and directional-survey contracts all read the same thing: rectangular tables the
extractor already stored, plus typed fields.  They differ only in the vocabulary they recognise.  This
module is the one place that knows *how* a stored table is walked; each contract module keeps the
vocabulary that makes it that contract, so adding a domain never copies the walking logic.

Three rules the helpers exist to enforce:

*   **The header vocabulary is closed and explicit.**  A column is recognised because its label is in
    the contract's alias tuple, or because it is that alias with its unit decoration removed.  There is
    no fuzzy matching, no scoring, and no "closest column".
*   **Nothing is invented.**  A missing cell is ``""``, a non-numeric cell is ``None``.  A helper never
    defaults a value, completes a date or assumes a unit.
*   **Order is the source's.**  Rows and columns are returned in stored order; the first spelling of a
    repeated header wins and later ones are not consulted, because guessing between two columns called
    "Description" would be an arbitrary choice with no trace in the data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "DEFAULT_UNIT_TOKENS",
    "alias_column",
    "cell_text",
    "header_index",
    "header_unit",
    "normalise_label",
    "numeric",
    "table_key",
    "tables",
    "without_units",
]

#: Unit decorations that are header text rather than part of a column's name.  Extended per contract:
#: the mud vocabulary is length/density, the BHA and bit vocabularies add inches, feet and degrees.
DEFAULT_UNIT_TOKENS: tuple[str, ...] = (
    "ft",
    "m",
    "in",
    "inch",
    "inches",
    "deg",
    "degrees",
    "hrs",
    "hr",
    "h",
    "ppg",
    "cp",
    "mg/l",
    "lb/100ft2",
    "psi/ft",
    "bbl",
    "pct",
    "%",
)


def normalise_label(value: Any) -> str:
    """Lowercase a source label without erasing meaningful slash/unit text."""
    text = str(value or "").strip().lower()
    text = text.replace("²", "2").replace("³", "3")
    text = re.sub(r"[,:;]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def without_units(text: str, units: Sequence[str] = DEFAULT_UNIT_TOKENS) -> str:
    """``"OD (in)"`` / ``"OD in"`` -> ``"od"``.

    A unit printed in a header is decoration the source's author added for the reader.  Stripping it
    is what lets one contract recognise ``"Length"``, ``"Length (ft)"`` and ``"Length ft"`` as the
    same column without listing every spelling the world uses.
    """
    label = normalise_label(text)
    label = re.sub(r"\([^)]*\)", "", label)
    pattern = "|".join(re.escape(unit) for unit in units)
    if pattern:
        label = re.sub(rf"\b(?:{pattern})\b", "", label)
    return re.sub(r"\s+", " ", label).strip()


def header_index(row: Sequence[Any], *, strip_units: bool = False) -> dict[str, int]:
    """``{"od": 3, "od (in)": 3, ...}`` for a header row, punctuation and case folded away.

    First spelling wins: a table with two columns called "Description" is a table whose author meant
    one of them, and guessing which would be an arbitrary choice with no trace in the data.  With
    ``strip_units`` the unit-decorated spelling is indexed alongside the bare one, so a contract can
    match either without a prefix search.
    """
    result: dict[str, int] = {}
    for index, cell in enumerate(row):
        for label in (normalise_label(cell), without_units(cell) if strip_units else ""):
            if label and label not in result:
                result[label] = index
    return result


def cell_text(row: Sequence[Any], index: int) -> str:
    """The stripped text of one cell; ``""`` when the column is absent or the cell is empty."""
    if index < 0 or index >= len(row):
        return ""
    value = row[index]
    return "" if value is None else str(value).strip()


def alias_column(
    headers: Mapping[str, int],
    aliases: Sequence[str],
    *,
    prefix: bool = False,
    strip_units: bool = True,
) -> int:
    """The column one of ``aliases`` names, or ``-1`` when the table does not have one.

    Exact labels are consulted first, in the order the contract listed them, so the contract controls
    which spelling wins.  ``prefix`` additionally accepts a label that *starts with* an alias - the
    behaviour the mud daily table relies on for ``"MW in (ppg)"``.  It is opt-in because a prefix
    search turns ``"bit"`` into a match for ``"bit size"``, which is a different column.
    """
    for alias in aliases:
        key = normalise_label(alias)
        if key in headers:
            return int(headers[key])
        if strip_units:
            stripped = without_units(alias)
            if stripped and stripped in headers:
                return int(headers[stripped])
    if prefix:
        for key, index in headers.items():
            if any(key.startswith(normalise_label(alias) + " ") for alias in aliases):
                return int(index)
    return -1


def numeric(value: Any, units: Sequence[str] = DEFAULT_UNIT_TOKENS) -> float | None:
    """Parse a numeric source cell without converting units or accepting formulas."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text.startswith("="):
        return None
    unit_pattern = "|".join(re.escape(unit) for unit in units)
    if unit_pattern:
        text_pattern = rf"[-+]?\d+(?:\.\d+)?(?:\s*(?:{unit_pattern}))?"
    else:  # pragma: no cover - every contract passes a unit vocabulary
        text_pattern = r"[-+]?\d+(?:\.\d+)?"
    if not re.fullmatch(text_pattern, text, flags=re.IGNORECASE):
        return None
    number = re.match(r"[-+]?\d+(?:\.\d+)?", text)
    try:
        return float(number.group(0)) if number else None
    except (TypeError, ValueError):
        return None


def header_unit(header: Any, default: str = "", units: Sequence[str] = DEFAULT_UNIT_TOKENS) -> str:
    """The unit a header states, if it states one.  Never the unit a property usually uses.

    Only a **parenthesised** unit is read, and that restriction is load-bearing rather than
    conservative: ``"Depth In (ft)"`` contains the word ``in`` as part of the column's *name*, and a
    header search that accepted a bare token would stamp every depth in a bit record with inches
    instead of feet.  ``"Depth In ft"`` is worse - there is no way to tell the unit from the name at
    all - so it reads as no unit, which is what the source actually stated.  A measurement whose unit
    the header does not delimit is stored ``UNVERIFIED`` rather than given a confident, wrong unit;
    the value is never converted either way.
    """
    text = str(header or "")
    parenthesised = re.search(r"\(([^)]+)\)", text)
    return parenthesised.group(1).strip() if parenthesised and parenthesised.group(1).strip() else default


def table_key(table: Mapping[str, Any]) -> str:
    """What identifies a table inside one artefact: its id, sheet, anchor and page - not its object."""
    return "|".join(str(table.get(key) or "") for key in ("table_id", "sheet", "anchor", "page"))


def tables(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The stored rectangular tables of one artefact, in stored order."""
    return [dict(table) for table in (payload.get("tables") or []) if isinstance(table, Mapping)]
