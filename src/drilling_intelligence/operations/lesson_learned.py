"""Static, source-shaped Lesson Learned parsing.

A lesson-learned document is a *tally* of discrete lessons: each row is one
lesson stated by the source, kept intact with its own locators.  This module
recognises a narrow table shape, the same way `bha.py` does:

* a lesson table with an explicit header row containing a `lesson` column and
  at least one of `lesson_code` or `title`.

What it does **not** do:

* it never infers a root cause, causal chain, applicability, recommendation or
  operational impact the source did not state — those columns are stored when
  present and left absent when not, with `root_cause_status` kept `UNKNOWN`;
* it never invents a lesson from prose.  A narrative lesson note in a TXT
  file stays knowledge evidence, not a promoted row;
* it never merges or sums lessons.  Each source row is one `lesson_learned`
  row; a footnote row with no lesson text is not a lesson;
* it never converts or normalises lesson wording.  The source's own text is
  kept verbatim.

Row identity is assigned by the promoter via :func:`promotion_identity` as
``version_id + kind + table_id + row_index + well_id + (code or title[:40])``,
so a re-extraction that changes a lesson's wording (title/extra) or its well
scope produces a different ``identity_key`` and is reported as a content
change, not a silent edit.  Duplicates within a single artefact (same code)
are reported, not silently merged.  The ``code`` itself is globally unique
when live (``uq_lesson_one_current`` on ``code``), not per-well.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .tableshape import (
    alias_column,
    cell_text,
    header_index,
    table_key,
    tables,
)

__all__ = [
    "LESSON_ALIASES",
    "LessonEntry",
    "lesson_entries",
    "lesson_table_key",
    "lesson_tables",
]

#: The lesson table's column vocabulary.  ``lesson`` is the only column a row
#: needs; everything else is optional and stays empty when the source did not
#: state it.  A table with a lesson column and at least one identifier column
#: (code or title) is a lesson tally.
LESSON_ALIASES: dict[str, tuple[str, ...]] = {
    "lesson_code": (
        "lesson code",
        "code",
        "lesson no",
        "lesson no.",
        "lesson number",
        "ll code",
        "ll no",
        "ll no.",
        "reference",
        "ref",
        "ref no",
        "ref no.",
        "id",
        "lesson id",
    ),
    "title": (
        "title",
        "subject",
        "heading",
        "lesson title",
        "topic",
        "short description",
        "summary",
    ),
    "lesson": (
        "lesson",
        "lesson learned",
        "what was learned",
        "lesson description",
        "key learning",
        "learning",
        "lesson statement",
        "lesson text",
    ),
    "observation": (
        "observation",
        "what happened",
        "situation",
        "observation / situation",
        "occurrence",
        "event description",
        "findings",
    ),
    "context": (
        "context",
        "background",
        "well context",
        "situation context",
        "conditions",
        "preconditions",
    ),
    "recommendation": (
        "recommendation",
        "recommendation / action",
        "action",
        "what to do",
        "action item",
        "proposed action",
        "follow up",
        "follow-up",
    ),
    "root_cause": (
        "root cause",
        "cause",
        "why",
        "root cause analysis",
        "rca",
        "contributing factor",
    ),
    "problem_type": (
        "problem type",
        "category",
        "area",
        "discipline",
        "type",
        "problem",
        "failure mode",
    ),
    "well": (
        "well",
        "well name",
        "well id",
        "wellbore",
        "uwi",
    ),
    "field": (
        "field",
        "field name",
    ),
    "project": (
        "project",
        "project name",
    ),
    "section": (
        "section",
        "hole section",
        "interval",
    ),
    "applicable_operations": (
        "applicable operations",
        "operations",
        "applicable ops",
    ),
    "hole_size_in": (
        "hole size",
        "hole size in",
        "nominal hole size",
        "hole size (in)",
    ),
}


@dataclass(frozen=True)
class LessonEntry:
    """One recognised lesson row, in the source's own order."""

    source_row_index: int
    lesson_code: str
    title: str
    lesson: str
    observation: str
    context: str
    recommendation: str
    root_cause: str
    problem_type: str
    well_name: str
    field_name: str
    project_name: str
    section_name: str
    applicable_operations: str
    hole_size_text: str
    table: Mapping[str, Any]


def _lesson_columns(row: Sequence[Any]) -> dict[str, int] | None:
    """Return column map when a row is a lesson header, else None.

    The contract is deliberately narrow: a lesson column **and** at least one
    identifier column (code or title).  A table with a lesson description and
    nothing to identify it is a note, not a lesson register, and admitting it
    would let any free-text table become authoritative lessons.
    """
    headers = header_index(row, strip_units=False)
    columns = {name: alias_column(headers, aliases) for name, aliases in LESSON_ALIASES.items()}
    if columns["lesson"] < 0:
        return None
    if columns["lesson_code"] < 0 and columns["title"] < 0:
        return None
    return columns


def lesson_tables(
    payload: Mapping[str, Any],
) -> list[tuple[dict[str, Any], dict[str, int], int]]:
    """(table, columns, header_row_index) for every stored table that is a lesson tally."""
    found: list[tuple[dict[str, Any], dict[str, int], int]] = []
    for table in tables(payload):
        rows = table.get("rows") or []
        # Only inspect first 8 rows for a header, same as BHA contract
        for row_index, row in enumerate(rows[:8]):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                continue
            columns = _lesson_columns(row)
            if columns is None:
                continue
            found.append((table, columns, row_index))
            break
    return found


def lesson_entries(payload: Mapping[str, Any]) -> tuple[LessonEntry, ...]:
    """Every recognised lesson row of every lesson table, in source order.

    A row with an empty lesson cell is not a lesson, even if it carries a
    code or title.  A row with lesson text but no other distinguishing field
    is also refused — it would have no stable identity.
    """
    entries: list[LessonEntry] = []
    for table, columns, header_row in lesson_tables(payload):
        rows = table.get("rows") or []
        for row_index, row in enumerate(rows[header_row + 1 :], start=header_row + 1):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                continue
            if not any(str(value or "").strip() for value in row):
                continue
            lesson_text = cell_text(row, columns["lesson"])
            if not lesson_text.strip():
                continue
            # At least code or title must be present for identity
            code = cell_text(row, columns["lesson_code"])
            title = cell_text(row, columns["title"])
            if not code.strip() and not title.strip():
                continue
            entries.append(
                LessonEntry(
                    source_row_index=row_index,
                    lesson_code=code.strip(),
                    title=title.strip(),
                    lesson=lesson_text.strip(),
                    observation=cell_text(row, columns["observation"]).strip(),
                    context=cell_text(row, columns["context"]).strip(),
                    recommendation=cell_text(row, columns["recommendation"]).strip(),
                    root_cause=cell_text(row, columns["root_cause"]).strip(),
                    problem_type=cell_text(row, columns["problem_type"]).strip(),
                    well_name=cell_text(row, columns["well"]).strip(),
                    field_name=cell_text(row, columns["field"]).strip(),
                    project_name=cell_text(row, columns["project"]).strip(),
                    section_name=cell_text(row, columns["section"]).strip(),
                    applicable_operations=cell_text(row, columns["applicable_operations"]).strip(),
                    hole_size_text=cell_text(row, columns["hole_size_in"]).strip(),
                    table=table,
                )
            )
    return tuple(entries)


def lesson_table_key(entries: Sequence[LessonEntry]) -> str:
    """The identity of the table the lessons came from (the first one, in stored order)."""
    for entry in entries:
        return table_key(entry.table)
    return ""
