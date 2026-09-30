"""Static, source-shaped cost-line parsing.

A cost table is a set of lines: a cost code, what the line is for, and one or more money columns
whose *meaning the source states*.  This module reads the tables the extractor already stored and
recognises exactly one shape - a cost line table with an explicit header row.

The acceptance rule is the narrowest one that means "this is a cost table":

*   a **cost code** column (CBS or WBS or an equivalent explicit account code), *and*
*   a **description** column, *and*
*   at least one **money column whose header says whether it is planned-side or actual-side**, *and*
*   an **explicit currency** the source states for that money.

Why the code is required rather than optional: ``CostRepository`` identifies a line by
``_IDENTITY_KEYS``, which deliberately excludes the description - the same line re-worded is the
same line.  So two different lines that share a category, an amount and a scope are one row unless
something else separates them, and in a cost table that something is the code.  Admitting a
code-less table would silently merge distinct lines, which is data loss with no diagnostic behind
it.  A narrative paragraph mentioning "$12k" is not a cost line either, and never becomes one here.

Four rules this module exists to keep:

*   **Planned and actual never merge.**  A column is planned-side or actual-side only when its
    header says so.  ``Forecast`` and ``Committed`` are neither: they are not what was budgeted and
    not what was spent, so they stay evidence and are reported as unmapped rather than folded into
    whichever numeric column happens to be free.
*   **No currency is ever invented.**  ``CostItem.planned_unit`` and ``actual_unit`` default to
    ``"USD"`` at the column level, and ``currency_of("")`` folds an empty unit to ``"USD"`` - both
    sensible for an engineer typing a line at a terminal, and both wrong for a source-derived row.
    So this module refuses a money column whose currency the source did not state, and the writer
    always passes an explicit currency.  A NOK source can never arrive as USD by omission.
*   **Nothing is converted.**  There is no exchange rate anywhere in the platform.  Amounts keep
    the currency the source printed, and two lines in different currencies are never added.
*   **Nothing is inferred.**  ``npt_id`` is never guessed from a line sitting near an NPT row, and a
    dotted code like ``1.2.4`` is kept as the source's ``cbs_code``; a display path derived from it
    is a derived aid, recorded as such, not a source fact.
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
    "CATEGORY_ALIASES",
    "COST_UNIT_TOKENS",
    "CostLineEntry",
    "cost_line_entries",
    "cost_tables",
    "locate_cost_header",
    "unmapped_money_columns",
]

#: Currencies a cost header may state.  Deliberately a currency vocabulary and not a unit
#: vocabulary: ``USD/t`` is a rate, not a currency, and ``header_unit`` only accepts a token that
#: is declared here.
COST_UNIT_TOKENS: tuple[str, ...] = (
    "USD",
    "EUR",
    "NOK",
    "GBP",
    "CAD",
    "AUD",
    "DKK",
    "SEK",
    "AED",
    "SAR",
    "BRL",
    "MXN",
)

#: Columns that name the line.  ``cost code`` and ``account`` are accepted as a CBS because a sheet
#: that calls its coding column by a generic name still coded the line - the code is the source's,
#: the column heading is not a fact about the cost.
CODE_ALIASES: dict[str, tuple[str, ...]] = {
    "cbs_code": (
        "cbs",
        "cbs code",
        "cbs no",
        "cbs no.",
        "cost code",
        "cost code no",
        "account",
        "account code",
        "account no",
        "account no.",
        "cost element",
        "cost element code",
    ),
    "wbs_code": ("wbs", "wbs code", "wbs no", "wbs no.", "wbs element"),
}

DESCRIPTION_ALIASES: tuple[str, ...] = (
    "description",
    "cost description",
    "line description",
    "item",
    "item description",
    "detail",
    "details",
    "narrative",
)

#: Money columns whose header states they are the **planned** side.  ``AFE`` and ``budget`` are
#: planned: they are what was authorised or budgeted before the work, not what it cost.
PLANNED_ALIASES: tuple[str, ...] = (
    "planned",
    "plan",
    "budget",
    "budgeted",
    "afe",
    "afe value",
    "estimate",
    "estimated",
    "approved",
    "authorised",
    "authorized",
)

#: Money columns whose header states they are the **actual** side.
ACTUAL_ALIASES: tuple[str, ...] = (
    "actual",
    "actuals",
    "actual cost",
    "spent",
    "spend",
    "incurred",
    "invoiced",
    "paid",
    "to date",
    "ytd",
)

#: Money-looking columns that are **neither** side, and are therefore never mapped.  A forecast is a
#: prediction and a commitment is a purchase obligation; folding either into planned or actual would
#: state something the source did not say.
NEITHER_SIDE_ALIASES: tuple[str, ...] = (
    "forecast",
    "forecasted",
    "commitment",
    "committed",
    "committed cost",
    "variance",
    "delta",
    "remaining",
    "balance",
    "total",
)

CURRENCY_ALIASES: tuple[str, ...] = ("currency", "ccy", "curr", "currency code", "cur")

#: A category the sheet states.  It is read as a *label*, not as an inference: ``cost_category``
#: folds a recognised label onto the platform vocabulary and keeps an unrecognised one verbatim in
#: ``attributes``, so a sheet that says "RIG MOVE" keeps saying so.
CATEGORY_ALIASES: tuple[str, ...] = (
    "category",
    "cost category",
    "cost type",
    "type",
    "cost element group",
    "group",
)

NPT_ALIASES: tuple[str, ...] = ("npt", "npt id", "npt ref", "npt reference", "npt no", "npt no.")

#: A label that merely *mentions* money does not make a cost table.  These are kept out of the
#: description vocabulary so that a remarks column cannot stand in for a line description.
_TEMPTING_BUT_NOT_COST: tuple[str, ...] = ("remarks", "comment", "comments", "notes", "note")


@dataclass(frozen=True)
class CostLineEntry:
    """One recognised cost line, exactly as the source stated it.

    Every money field carries its source text, its parsed value and the currency the source
    declared for it.  A value the source did not state is ``None`` with empty text - never zero,
    because zero is an amount and ``None`` is the absence of one.
    """

    source_row_index: int
    cbs_code: str
    wbs_code: str
    description: str
    category_text: str
    planned_text: str
    planned_value: float | None
    planned_currency: str
    actual_text: str
    actual_value: float | None
    actual_currency: str
    currency_text: str
    npt_reference: str
    table: Mapping[str, Any]

    @property
    def has_money(self) -> bool:
        return self.planned_value is not None or self.actual_value is not None


def cost_tables(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The stored rectangular tables of one artefact, in stored order."""
    return tables(payload)


def _money_column(
    headers: Mapping[str, int],
    raw_header_row: Sequence[Any],
    aliases: Sequence[str],
    *,
    exclude: Sequence[Mapping[str, int]] = (),
) -> tuple[int, str, str]:
    """The column one of ``aliases`` names, its raw header text and the currency that header states.

    Returns ``(-1, "", "")`` when the table has no such column.  ``exclude`` keeps a column already
    claimed by the other side from being claimed twice, which is what stops a table with a single
    ambiguous money column from being read as both planned and actual.
    """
    index = alias_column(headers, aliases)
    if index < 0:
        return -1, "", ""
    for claimed in exclude:
        if index in claimed.values():
            return -1, "", ""
    header = cell_text(raw_header_row, index)
    return index, header, header_unit(header, default="", units=COST_UNIT_TOKENS)


def unmapped_money_columns(headers: Mapping[str, int], raw_header_row: Sequence[Any]) -> list[str]:
    """Money-ish headers this contract will not map, so the caller can report them as evidence.

    Reporting beats silence: a ``Forecast`` column that is quietly dropped looks identical to one
    that was never there, and a reviewer comparing the row against the sheet would have no way to
    tell that a number was left behind on purpose.
    """
    claimed = {
        alias_column(headers, PLANNED_ALIASES),
        alias_column(headers, ACTUAL_ALIASES),
        alias_column(headers, CODE_ALIASES["cbs_code"]),
        alias_column(headers, CODE_ALIASES["wbs_code"]),
        alias_column(headers, DESCRIPTION_ALIASES),
        alias_column(headers, CURRENCY_ALIASES),
        alias_column(headers, NPT_ALIASES),
        alias_column(headers, CATEGORY_ALIASES),
    }
    found: list[str] = []
    for alias in NEITHER_SIDE_ALIASES:
        index = alias_column(headers, (alias,))
        if index >= 0 and index not in claimed:
            found.append(cell_text(raw_header_row, index) or alias)
    return found


def _looks_like_cost_table(headers: Mapping[str, int]) -> bool:
    """Whether the header row is a cost line table rather than something that mentions money."""
    return (
        alias_column(headers, DESCRIPTION_ALIASES) >= 0
        and (
            alias_column(headers, CODE_ALIASES["cbs_code"]) >= 0
            or alias_column(headers, CODE_ALIASES["wbs_code"]) >= 0
        )
        and (
            alias_column(headers, PLANNED_ALIASES) >= 0
            or alias_column(headers, ACTUAL_ALIASES) >= 0
        )
    )


#: How far down a stored table to look for the header row.  An extractor stores the sheet region it
#: found, which usually includes a document title and a currency note above the column headings.
_HEADER_SEARCH_DEPTH = 8


def locate_cost_header(
    rows: Sequence[Sequence[Any]],
) -> tuple[int, Sequence[Any], Mapping[str, int]] | None:
    """``(row index, header row, columns)`` for the first row that is a cost table's header.

    The header is *found*, not assumed to be the first stored row: a cost sheet normally carries a
    title and a currency note above its column headings, and the extractor stores those with the
    table.  Taking ``rows[0]`` would read the title as a header and the real table as data.
    """
    for index, row in enumerate(rows[:_HEADER_SEARCH_DEPTH]):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            continue
        headers = header_index(row, strip_units=True)
        if _looks_like_cost_table(headers):
            return index, row, headers
    return None


def cost_line_entries(payload: Mapping[str, Any]) -> list[CostLineEntry]:
    """Every recognised cost line in the stored tables of one artefact.

    A table that does not have a description, an explicit cost code and a side-labelled money
    column is not read at all - it stays evidence.  A table that has those but states no currency
    for a money column yields rows whose value is ``None``, because storing the number under a
    guessed currency would be worse than not storing it.
    """
    found: list[CostLineEntry] = []
    for table in cost_tables(payload):
        rows = list(table.get("rows") or [])
        # ``locate_cost_header`` indexes with ``strip_units`` so that "Budget (NOK)" is also indexed
        # as "budget": alias_column strips units from the *alias*, not from the header, so a
        # unit-decorated money column would otherwise never match.  The raw header text is still
        # read for its currency, which is exactly the part that must not be stripped away.
        located = locate_cost_header(rows)
        if located is None:
            continue
        header_row_index, header_row, headers = located

        description_index = alias_column(headers, DESCRIPTION_ALIASES)
        cbs_index = alias_column(headers, CODE_ALIASES["cbs_code"])
        wbs_index = alias_column(headers, CODE_ALIASES["wbs_code"])
        currency_index = alias_column(headers, CURRENCY_ALIASES)
        npt_index = alias_column(headers, NPT_ALIASES)
        category_index = alias_column(headers, CATEGORY_ALIASES)

        planned = _money_column(headers, header_row, PLANNED_ALIASES)
        actual = _money_column(
            headers,
            header_row,
            ACTUAL_ALIASES,
            exclude=[{"x": planned[0]}] if planned[0] >= 0 else (),
        )

        for offset, row in enumerate(rows[header_row_index + 1 :], start=header_row_index + 1):
            description = cell_text(row, description_index)
            cbs_code = cell_text(row, cbs_index)
            wbs_code = cell_text(row, wbs_index)
            if not description or not (cbs_code or wbs_code):
                # No code means no way to tell this line from another with the same category and
                # amount, so it is skipped rather than merged into a neighbour.
                continue
            currency_text = cell_text(row, currency_index).upper()
            planned_currency = planned[2] or currency_text
            actual_currency = actual[2] or currency_text

            planned_text = cell_text(row, planned[0])
            actual_text = cell_text(row, actual[0])
            found.append(
                CostLineEntry(
                    source_row_index=offset,
                    cbs_code=cbs_code,
                    wbs_code=wbs_code,
                    description=description,
                    category_text=cell_text(row, category_index),
                    planned_text=planned_text,
                    # A value is only parsed when the currency it would be stored under is known.
                    planned_value=(
                        numeric(planned_text, units=COST_UNIT_TOKENS) if planned_currency else None
                    ),
                    planned_currency=planned_currency,
                    actual_text=actual_text,
                    actual_value=(
                        numeric(actual_text, units=COST_UNIT_TOKENS) if actual_currency else None
                    ),
                    actual_currency=actual_currency,
                    currency_text=currency_text,
                    npt_reference=cell_text(row, npt_index),
                    table=table,
                )
            )
    return found
