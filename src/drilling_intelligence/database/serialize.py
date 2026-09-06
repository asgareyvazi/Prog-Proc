"""One way to turn a mapped row into the JSON the CLI and the AI layer are allowed to read.

Every record table - a procedure, an NPT row, a lesson, a pattern - needs the same treatment for
``--json`` output and for the conflict/evidence payloads the knowledge layer already emits:
column order, ISO datetimes, JSON columns passed through, and the private ordering keys left out.
Writing that once here is the difference between fourteen hand-built dictionaries that drift and a
contract the tests can state once.

Deliberately not a serializer framework: it reads ``__table__.columns``, because that is what the
database holds.  Anything computed in Python is not a record and does not belong in this output -
a reader of ``--json`` has to be able to assume every key is a column they could query.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .base import Base

#: Column names that are storage plumbing rather than content: they exist to make an idempotent
#: write cheap, and a caller who needs them can query the table.
HIDDEN_COLUMNS: frozenset[str] = frozenset({"identity_key"})


def column_names(model: type[Base]) -> tuple[str, ...]:
    """The mapped column names of a model, in table order."""
    return tuple(column.name for column in model.__table__.columns)


def encode_value(value: Any) -> Any:
    """JSON-safe form of one column value: datetimes to ISO, everything else as it is.

    ISO rather than a locale-formatted string for a reason beyond parseability: ISO-8601 sorts the
    way the timeline expects, so the text a reader sees and the order they get are the same order.
    """
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def record_to_dict(
    row: Base,
    *,
    include: tuple[str, ...] | list[str] | None = None,
    exclude: tuple[str, ...] | list[str] | None = None,
    hidden: frozenset[str] | set[str] = HIDDEN_COLUMNS,
) -> dict[str, Any]:
    """The row as a plain dictionary, keyed by column name.

    ``include`` restricts the output to columns that exist (a missing name is a bug in the caller,
    and it is raised rather than skipped); ``exclude`` drops columns that are readable but not
    worth a reader's attention, such as a stored blob.
    """
    names = list(include) if include is not None else list(column_names(type(row)))
    if include is not None:
        available = set(column_names(type(row)))
        missing = [name for name in names if name not in available]
        if missing:
            raise KeyError(f"{type(row).__name__} has no column named {', '.join(sorted(missing))}")
    drop = set(exclude or ()) | set(hidden)
    payload: dict[str, Any] = {}
    for name in names:
        if name in drop:
            continue
        payload[name] = encode_value(getattr(row, name, None))
    return payload


def records_to_dicts(
    rows: list[Any] | tuple[Any, ...],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """The list form, order preserved (the repository already decided what the order means)."""
    return [record_to_dict(row, **kwargs) for row in rows]


__all__ = [
    "HIDDEN_COLUMNS",
    "column_names",
    "encode_value",
    "record_to_dict",
    "records_to_dicts",
]
