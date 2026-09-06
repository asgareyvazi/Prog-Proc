"""Turning a stored artefact into operational records - deterministically, and no further.

This is the only bridge in the platform from "what a document said" to "what happened", and it
crosses it with a table, not a model: a promotion reads the artefact's **tables and typed fields**,
recognises the column headings an NPT export actually uses, and writes one row per source row.  It
does not read prose.  There is no regular expression here reaching for "the bit came unstuck" and no
summary of a narrative, because the day a promoter starts inferring causes from sentences, the field
totals stop being a query and start being an opinion - and nobody can tell which rows came from a
cell and which from a guess.

Four rules make the output trustworthy:

*   **A row is about the well its own cell names.**  A shared CSV covering four wells is one
    document; attaching all four rows to whichever well the folder happened to be filed under would
    be a silent lie.  A row naming a well this workspace has never heard of is *skipped and
    reported* - never created against an invented well, never re-attached to a plausible one.
*   **Nothing is invented.**  A date that does not parse stays NULL with the source's wording kept in
    ``*_text``; a duration of "n/a" stays unknown rather than zero; a root cause stays ``UNKNOWN``,
    because a reason code is not a diagnosis.  What the report *did* state as its reason becomes the
    ``cause`` / ``immediate_cause``, labelled ``KNOWN`` for exactly that reason: the source named it.
*   **A total is not another row.**  When a version produced NPT rows out of a table, the document's
    own ``npt_hours`` summary field is deliberately not promoted - the report's total is the sum of
    its lines, and promoting both counts every hour twice.  The result says so, because a rule nobody
    can see is a rule nobody can audit.
*   **Re-running is a repair, not an append.**  Every promoted row carries an ``identity_key``
    derived from the version, the table, the row index and the well, so a second pass reports
    ``UNCHANGED``.  A row that already exists is *never rewritten*: if a re-extraction now says
    something different, that is a change to something the platform already asserted, and the
    promoter reports it as ``SOURCE_CHANGED`` for a person to resolve instead of quietly editing a
    record someone may have confirmed an hour ago.

Promotion is an explicit step (``drillintel records promote``) rather than a stage of ``ingest``, and
:doc:`docs/DECISIONS.md` ADR-0010 keeps the reason on record: it is the one pass that changes what
the platform asserts about operations, so a workspace decides when its operational history starts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import (
    CauseStatus,
    ConfirmationStatus,
    KnowledgeOrigin,
    KnowledgeRelationType,
    RecordState,
)
from ..core.errors import UnitError
from ..core.hashing import sha256_obj
from ..core.units import Quantity, parse_decimal
from ..core.vocabulary import problem_type
from ..database.models import (
    DdrReport,
    Document,
    DocumentVersion,
    Extraction,
    NptRecord,
    ProblemOccurrence,
    Well,
    WellEvent,
    WellOperation,
)
from ..database.serialize import record_to_dict
from ..wells.repository import WellRepository
from .repository import REPORT_CLASSIFICATIONS, OperationsRepository, _stamp

__all__ = [
    "ACTIVITY_HEADERS",
    "CODE_HEADERS",
    "DATE_FIELDS",
    "DATE_HEADERS",
    "DESCRIPTION_HEADERS",
    "DURATION_HEADERS",
    "REFERENCE_HEADERS",
    "TOTAL_NPT_FIELDS",
    "WELL_HEADERS",
    "PromotionResult",
    "VersionPromoter",
    "promotion_identity",
]

# --------------------------------------------------------------------------- recognised headers
#: Spellings that mean "these are the hours lost".  A table needs one to be an NPT table at all.
NPT_DURATION_HEADERS: tuple[str, ...] = (
    "npt hours",
    "npt (hours)",
    "npt hours (h)",
    "npt_hrs",
    "npt hrs",
    "npt",
    "hours lost",
    "lost hours",
    "delay hours",
    "lost time (h)",
    "non productive time",
    "non-productive time",
)
#: A duration column that does *not* say the hours were non-productive.  A daily report's "Activity /
#: Hours" sheet is a time breakdown: the hours in it are mostly the day being spent productively, and
#: filing them all as NPT would turn a schedule into a list of problems.
DURATION_HEADERS: tuple[str, ...] = (
    *NPT_DURATION_HEADERS,
    "hours",
    "duration",
    "time",
    "elapsed",
    "elapsed hours",
)
#: The activity column a breakdown table has to name before its rows mean anything.
BREAKDOWN_REQUIRES: tuple[str, ...] = ("activity",)
#: Rows that add a column up rather than describe work.  A total is not an activity, and promoting one
#: would count the day twice - once as its lines and once as its sum.
TOTAL_LABELS: frozenset[str] = frozenset(
    {"total", "totals", "sum", "subtotal", "total hours", "total time", "grand total"}
)
#: Everything else is optional.  A sheet with only a duration is still a record of lost time, and
#: dropping rows for want of a date column would lose hours the field actually lost.
DATE_HEADERS: tuple[str, ...] = (
    "date",
    "report date",
    "event date",
    "start date",
    "start",
    "from date",
    "npt start",
    "spud date",
)
ACTIVITY_HEADERS: tuple[str, ...] = (
    "activity",
    "activity type",
    "activity performed",
    "operation",
    "job",
    "operation type",
)
CODE_HEADERS: tuple[str, ...] = (
    "code",
    "npt code",
    "reason code",
    "cause code",
    "category",
    "type",
)
DESCRIPTION_HEADERS: tuple[str, ...] = (
    "description",
    "details",
    "npt reason",
    "activity description",
    "comment",
    "remarks",
)
WELL_HEADERS: tuple[str, ...] = ("well", "well name", "wellbore", "uwi", "api", "api no")
REFERENCE_HEADERS: tuple[str, ...] = (
    "event no",
    "event no.",
    "event",
    "no",
    "no.",
    "ref",
    "reference",
    "ticket",
    "#",
)

#: Artefact fields that date a report, and number it, in the order of trust.
DATE_FIELDS: tuple[str, ...] = ("report_date", "date_iso", "date", "date_text")
NUMBER_FIELDS: tuple[str, ...] = ("report_number", "report_no", "ddr_number", "document_number")
SHIFT_FIELDS: tuple[str, ...] = ("shift", "shift_name", "tour")
#: The field that states the day's total, used only when there are no lines to add up.
TOTAL_NPT_FIELDS: tuple[str, ...] = ("npt_hours", "npt", "total_npt")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def promotion_identity(
    *,
    version_id: str,
    kind: str,
    table_id: str = "",
    row_index: int = 0,
    well_id: str = "",
    extra: str = "",
) -> str:
    """The key that makes promoting the same artefact twice a no-op.

    Content-addressed for the reason given in
    :func:`~drilling_intelligence.knowledge.repository.fact_id_for`: derived from what the source
    said and where it said it, so re-promoting the same version produces the same keys and an
    extraction that moved a value produces a different one.  Callers append a per-record suffix
    (``":ev"``, ``":npt"``, ``":problem"``) so the four rows a line produces are four identities.
    """
    digest = sha256_obj(
        {
            "version_id": str(version_id),
            "kind": str(kind),
            "table_id": str(table_id),
            "row_index": int(row_index),
            "well_id": str(well_id),
            "extra": str(extra),
        }
    )
    return f"promote:{digest[:32]}"


def _header_index(row: Sequence[Any]) -> dict[str, int]:
    """``{"npt hours": 4, ...}`` for a header row, punctuation and case folded away.

    First spelling wins: a sheet with two columns called "Description" is a sheet whose author meant
    one of them, and guessing which would be an arbitrary choice with no trace in the data.
    """
    index: dict[str, int] = {}
    for position, cell in enumerate(row):
        key = re.sub(r"\s+", " ", str(cell or "").strip().lower())
        if key and key not in index:
            index[key] = position
    return index


def _column(index: Mapping[str, int], aliases: Sequence[str]) -> int:
    """The position of the first recognised header, or ``-1``."""
    for alias in aliases:
        if alias in index:
            return int(index[alias])
    return -1


def _cell(row: Sequence[Any], position: int) -> str:
    if position < 0 or position >= len(row):
        return ""
    value = row[position]
    return "" if value is None else str(value).strip()


def _hours(text: Any) -> float | None:
    """Parse a duration cell: ``6.5``, ``"6,5"``, ``"6.5 h"`` are hours; anything else is unknown.

    Returning ``0.0`` for an unreadable cell would turn a formatting quirk into a claim that nothing
    was lost, and the aggregate cannot tell the two apart afterwards.

    A cell that carries a *different* time unit is converted rather than discarded: sheets say
    ``90 min`` and lessons say ``1.5 days``, and hours are the unit the row stores.  The conversion is
    :mod:`drilling_intelligence.core.units`' arithmetic, not a division written here, and the wording the
    report used is kept beside the number as ``duration_text`` - so a 1.5 h from a "90 min" cell is
    traceable to the cell rather than looking like someone measured 1.5 hours.
    """
    raw = str(text if text is not None else "").strip()
    if not raw:
        return None
    if isinstance(text, int | float) and not isinstance(text, bool):
        return float(text)
    cleaned = re.sub(r"(?i)\s*(h|hr|hrs|hours|hour)\s*$", "", raw).strip()
    if cleaned:
        try:
            return float(parse_decimal(cleaned))
        except (TypeError, ValueError):
            pass
    try:
        return Quantity.parse(raw).value_in("h")
    except (UnitError, ValueError):  # a cell that is not a duration at all: unknown, not zero
        return None


def _iso(value: Any) -> tuple[str | None, str]:
    """``(iso_or_None, the_wording_we_were_given)`` - the pair that keeps a gap visible.

    ISO-8601 is what parses.  ``"14 June 2025"`` does not, and a promoter that read one of the
    forty date formats in a corpus and not the rest would produce a timeline whose holes are
    invisible: nobody could tell an unreadable date from a day nothing happened.
    """
    raw = str(value if value is not None else "").strip()
    if isinstance(value, datetime | date):
        return value.isoformat(), raw or value.isoformat()
    if not raw or not _ISO_DATE.match(raw):
        return None, raw
    stamp = _stamp(raw)
    return (stamp.isoformat() if stamp else None), raw


def _comparable(value: Any) -> Any:
    """Normalise one value so a stored column and the payload about to be written can be compared.

    The two sides of this comparison come from different places - one is a column round-tripped
    through SQLite, the other the string the artefact held - so a date written "2025-06-13" and one
    stored as a midnight ``datetime`` are the same fact expressed twice.  Folding both through the
    same parser is what keeps "unchanged" honest; without it every dated row would read as a conflict
    the second time a folder was ingested.
    """
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        stamp = _stamp(value)
        if stamp is not None:
            return stamp.isoformat()
    if hasattr(value, "value") and not isinstance(value, str | int | float):
        return str(value.value)
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, Mapping | list | tuple):
        return sha256_obj(list(value) if isinstance(value, tuple) else dict(value))
    return str(value)


def _tables_of(
    payload: Mapping[str, Any], duration: Sequence[str], *, require_activity: bool = False
) -> list[tuple[dict[str, Any], dict[str, int]]]:
    """The tables whose header row has a recognised duration column (and, if asked, an activity one).

    Recognised by header rather than by filename: an NPT export called ``timesheet.xlsx`` is still an
    NPT export, and a mud log that happens to be a table is not one.
    """
    found: list[tuple[dict[str, Any], dict[str, int]]] = []
    for raw in payload.get("tables") or []:
        table = dict(raw)
        rows = [list(row) for row in (table.get("rows") or [])]
        if len(rows) < 2 or not table.get("has_header", True):
            continue
        index = _header_index(rows[0])
        if _column(index, duration) < 0:
            continue
        if require_activity and _column(index, BREAKDOWN_REQUIRES) < 0:
            continue
        found.append((table, index))
    return found


def find_npt_tables(payload: Mapping[str, Any]) -> list[tuple[dict[str, Any], dict[str, int]]]:
    """Tables that state *non-productive* hours: the sheet an NPT report is.

    Public because the service asks the same question when it decides which versions are worth opening
    at all, and a wrong answer there is a whole folder of files nobody looked at.
    """
    return _tables_of(payload, NPT_DURATION_HEADERS)


def find_breakdown_tables(
    payload: Mapping[str, Any],
) -> list[tuple[dict[str, Any], dict[str, int]]]:
    """Tables that state hours spent per activity - a daily report's time breakdown.

    Excludes anything :func:`find_npt_tables` already claimed, so a file with both an NPT sheet and a
    breakdown sheet has each row read once, under the header that actually describes it.
    """
    # Keyed on the table's own identity rather than ``id(dict)``: the rows are re-dictified on every
    # read, so two reads of one table are different objects, and a promoter that trusted ``id()``
    # would promote the same NPT sheet a second time as a time breakdown - double-counting every hour.
    claimed = {_table_key(table) for table, _index in find_npt_tables(payload)}
    return [
        pair
        for pair in _tables_of(payload, DURATION_HEADERS, require_activity=True)
        if _table_key(pair[0]) not in claimed
    ]


def _table_key(table: Mapping[str, Any]) -> str:
    """What identifies a table inside one artefact: its id, sheet and anchor, not its python object."""
    return "|".join(str(table.get(name) or "") for name in ("table_id", "sheet", "anchor", "page"))


def is_npt_code(code: object) -> bool:
    """Whether a report's own code marks a line as non-productive time.

    Only the code column is consulted - never the activity's wording.  "NPT - stuck bit" in an
    Activity cell is what the report chose to call the row, and reading a cause or a problem type out
    of a label is classification of prose, which this module does not do.
    """
    return str(code or "").strip().lower().startswith("npt")


@dataclass
class PromotionResult:
    """What one version's promotion created, confirmed, refused and could not read - each counted."""

    document_id: str = ""
    version_id: str = ""
    classification: str = ""
    #: ``{"npt": {"created": 3, "unchanged": 0}, ...}`` - the kinds promoted, never a mixture.
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Rows the promoter saw and did not write, with the reason.  Absence is not the same as zero.
    skipped: list[dict[str, str]] = field(default_factory=list)
    #: Every ``identity_key`` this pass wrote or confirmed, which is what a sweep compares against: a
    #: promoted row whose key is absent here is a row the artefact no longer supports.
    identities: set[str] = field(default_factory=set)
    report_id: str = ""
    #: Why nothing at all was promoted ("NO_WELL", "NO_ARTEFACT"), when that is the case.
    error: str = ""

    def bump(self, kind: str, outcome: str) -> None:
        """Count one row of one kind under one outcome."""
        bucket = self.counts.setdefault(kind, {"created": 0, "unchanged": 0, "conflict": 0})
        bucket[outcome] = bucket.get(outcome, 0) + 1

    def total(self, outcome: str) -> int:
        return int(sum(bucket.get(outcome, 0) for bucket in self.counts.values()))

    @property
    def wrote_anything(self) -> bool:
        return self.total("created") > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "version_id": self.version_id,
            "classification": self.classification,
            "report_id": self.report_id,
            "counts": {kind: dict(values) for kind, values in sorted(self.counts.items())},
            "totals": {
                outcome: self.total(outcome) for outcome in ("created", "unchanged", "conflict")
            },
            "skipped": list(self.skipped),
            "identities": len(self.identities),
            "error": self.error,
        }


class VersionPromoter:
    """Promote the operational records one document version's artefact supports.

    One instance per version, holding nothing but the session, the repositories and a cache of wells
    by name.  Caching across versions is deliberately not done: a promoter that remembered a well
    from the previous file would happily promote a row into a record that no longer matches the
    registry it read a second ago.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.wells = WellRepository(session)
        self.records = OperationsRepository(session)
        self._wells_by_name: dict[str, Well | None] = {}

    # -- the pass -------------------------------------------------------------
    def promote(
        self,
        *,
        document_id: str,
        version_id: str = "",
        replace: bool = True,
    ) -> PromotionResult:
        """Read one version's artefact and write the operational rows it supports.

        ``replace`` deletes this version's promoted rows first, so a re-extraction that dropped a
        line does not leave an orphan of it behind.  It deletes only rows this version wrote with
        ``origin=DERIVED``: a row a person typed, or a row another version produced, survives -
        which is the same boundary the knowledge layer's rebuild respects.
        """
        document = self.session.get(Document, str(document_id))
        if document is None:
            raise ValueError(f"no document {document_id!r}")
        version: DocumentVersion | None = (
            self.session.get(DocumentVersion, str(version_id)) if (version_id) else None
        )
        if version_id and version is None:
            raise ValueError(f"no document version {version_id!r}")
        if version is None:
            if not document.current_version_id:
                raise ValueError(f"document {document_id!r} has no current version to promote")
            version = self.session.get(DocumentVersion, str(document.current_version_id))
        if version is None:  # pragma: no cover - a dangling current_version_id is registry damage
            raise ValueError(
                f"document {document_id!r} points at a version that is not in the database"
            )
        extraction = self.session.execute(
            select(Extraction).where(Extraction.document_version_id == version.id)
        ).scalar_one_or_none()
        payload = dict((extraction.document_json if extraction else None) or {})
        result = PromotionResult(
            document_id=document.id,
            version_id=version.id,
            classification=str(document.classification or ""),
        )
        if not payload:
            result.error = "NO_ARTEFACT"
            result.skipped.append(
                {
                    "reason": "NO_ARTEFACT",
                    "detail": f"version {version.id} has no stored artefact to promote from",
                }
            )
            return result
        fields = [dict(item) for item in (payload.get("extracted_fields") or [])]
        report = self._promote_report(
            document=document, version=version, fields=fields, result=result
        )
        lines = self._promote_tables(
            payload=payload, document=document, version=version, report=report, result=result
        )
        self._promote_total(
            document=document,
            version=version,
            report=report,
            fields=fields,
            lines_promoted=lines,
            result=result,
        )
        if replace:
            # Sweep what the artefact no longer supports, rather than truncate-and-reload.  Deleting
            # and re-inserting would also delete every confirmation a person attached to those rows,
            # so a routine re-promotion would quietly demote a reviewed history back to candidates -
            # and re-adding the rows under fresh ids would orphan the edges and lessons pointing at
            # them.
            removed = self.delete_orphans(version_id=version.id, kept=result.identities)
            if removed:
                # Its own kind, so a reader never mistakes a removal for a row promoted this pass.
                result.counts["removed"] = {"created": removed, "unchanged": 0, "conflict": 0}
        return result

    def delete_orphans(self, *, version_id: str, kept: set[str]) -> int:
        """Delete the promoted rows this artefact no longer supports, and only those.

        A row this version derived whose identity the pass did not confirm again is a row the
        artefact has stopped stating - a line deleted from the sheet, a duration changed to zero, a
        well unlinked - and leaving it behind keeps a phantom in every total forever.  ``DdrReport``
        is not swept: there is exactly one per version by construction, and the repository re-finds
        it instead of adding a second.
        """
        removed = 0
        for model in (ProblemOccurrence, NptRecord, WellEvent, WellOperation):
            rows = [
                row
                for row in self.session.execute(
                    select(model)
                    .where(model.document_version_id == version_id)
                    .where(model.origin == KnowledgeOrigin.DERIVED.value)
                )
                .scalars()
                .all()
                if str(row.identity_key or "") not in kept
            ]
            for row in rows:
                self.session.delete(row)
            if rows:
                self.session.flush()
            removed += len(rows)
        return removed

    def delete_promoted(self, *, version_id: str) -> int:
        """Remove everything this version's promotion wrote.  Returns the row count.

        The blunt instrument, kept because "un-derive this file" is a thing a workspace sometimes has
        to do - a promotion made from what turned out to be somebody's draft, for instance.  Children
        before parents, one flush at a time: the record tables point at each other by ordinary foreign
        keys, and SQLite enforces only what it is given, in the order it is given it.
        """
        removed = 0
        for model in (ProblemOccurrence, NptRecord, WellEvent, WellOperation, DdrReport):
            statement = (
                select(model)
                .where(model.document_version_id == version_id)
                .where(model.origin == KnowledgeOrigin.DERIVED.value)
            )
            rows = list(self.session.execute(statement).scalars().all())
            for row in rows:
                self.session.delete(row)
            if rows:
                self.session.flush()
            removed += len(rows)
        self.session.flush()
        return removed

    # -- report ---------------------------------------------------------------
    def _promote_report(
        self,
        *,
        document: Document,
        version: DocumentVersion,
        fields: Sequence[Mapping[str, Any]],
        result: PromotionResult,
    ) -> DdrReport | None:
        """The report row: which day, which number, which well - and nothing the file did not say.

        The date comes from a date field if the artefact has one, and otherwise from the registry's
        own ``document_date`` (extracted from the file, so it is traceable, and recorded as such in
        ``attributes["report_date_source"]``).  It is never taken from a file's mtime: a copy's
        timestamp says something about the folder, not about the well.
        """
        classification = str(document.classification or "")
        if classification not in REPORT_CLASSIFICATIONS:
            result.skipped.append(
                {
                    "reason": "NOT_A_REPORT",
                    "detail": f"{classification or 'unclassified'} does not describe a day's work",
                }
            )
            return None
        if not document.well_id:
            # A report nobody has attached to a well cannot become that well's history.  The row
            # appears once the workspace says which well it is, and not one moment before.
            result.error = "NO_WELL"
            result.skipped.append(
                {
                    "reason": "NO_WELL",
                    "detail": f"{document.filename} is not linked to a well",
                }
            )
            return None
        date_field = self._first_field(fields, DATE_FIELDS)
        parsed, wording = _iso(
            (date_field or {}).get("value") or self._wording(date_field) if date_field else None
        )
        source = "field"
        if not parsed and document.document_date is not None:
            parsed, source = _iso(document.document_date)[0], "document_date"
            wording = str(document.document_date)
        number_field = self._first_field(fields, NUMBER_FIELDS)
        shift_field = self._first_field(fields, SHIFT_FIELDS)
        already = self.session.execute(
            select(DdrReport).where(DdrReport.document_version_id == version.id)
        ).scalar_one_or_none()
        report = self.records.register_report(
            well_id=str(document.well_id),
            document_id=document.id,
            document_version_id=version.id,
            # The registry's own parsed revision is the report's number when the file gave one; it
            # is the document's identifier rather than the promoter's invention, and it came from
            # reading the file, so an empty field is the only case that leaves the column empty.
            report_number=str((number_field or {}).get("value") or "")
            or str(document.revision or ""),
            report_date=parsed,
            report_date_text=wording or None,
            shift=str((shift_field or {}).get("value") or ""),
            document_status=str(version.status or document.status or ""),
            provenance=[dict(date_field["provenance"])]
            if date_field and date_field.get("provenance")
            else [],
            origin=KnowledgeOrigin.DERIVED.value,
            created_by="promoter",
            attributes={
                "identity_path": document.identity_path,
                "filename": document.filename,
                "report_date_source": source if parsed else "none",
            },
        )
        result.report_id = report.id
        result.bump("report", "unchanged" if already is not None else "created")
        return report

    # -- tables ---------------------------------------------------------------
    @staticmethod
    def _npt_tables(payload: Mapping[str, Any]) -> list[tuple[dict[str, Any], dict[str, int]]]:
        return find_npt_tables(payload)

    @staticmethod
    def _breakdown_tables(
        payload: Mapping[str, Any],
    ) -> list[tuple[dict[str, Any], dict[str, int]]]:
        return find_breakdown_tables(payload)

    def _promote_tables(
        self,
        *,
        payload: Mapping[str, Any],
        document: Document,
        version: DocumentVersion,
        report: DdrReport | None,
        result: PromotionResult,
    ) -> int:
        """One row of an NPT table -> an operation, an event, an NPT block and a problem.

        Returns how many *NPT rows* were written, which is also what decides whether the document's
        own total field is promoted alongside them (it is not, once its lines are in).
        """
        written = 0
        for table, index in self._npt_tables(payload):
            rows, hours_column = self._data_rows(table, index, NPT_DURATION_HEADERS)
            for offset, row in enumerate(rows):
                written += int(
                    self._promote_row(
                        row=row,
                        offset=offset,
                        table_id=str(table.get("table_id") or "table"),
                        sheet=str(table.get("sheet") or ""),
                        positions=self._positions(index, hours_column),
                        provenance=self._provenance_of(table),
                        document=document,
                        version=version,
                        report=report,
                        result=result,
                    )
                )
        written += self._promote_breakdown(
            payload=payload,
            document=document,
            version=version,
            report=report,
            result=result,
        )
        return written

    @staticmethod
    def _provenance_of(table: Mapping[str, Any]) -> list[dict[str, Any]]:
        provenance = table.get("provenance")
        return [dict(provenance)] if isinstance(provenance, Mapping) else []

    @staticmethod
    def _data_rows(
        table: Mapping[str, Any], index: Mapping[str, int], duration: Sequence[str]
    ) -> tuple[list[list[Any]], int]:
        """``(the rows below the header, the duration column)`` - blank lines and totals dropped.

        A total row is not work: it is the column added up.  Promoting one would double-count the day,
        once as its lines and once as its sum, and the second count is the one a report quotes.
        """
        hours_column = _column(index, duration)
        rows: list[list[Any]] = []
        for raw in [list(row) for row in (table.get("rows") or [])[1:]]:
            if not any(str(cell or "").strip() for cell in raw):
                continue
            label = _cell(raw, _column(index, ACTIVITY_HEADERS)).strip().lower()
            if label in TOTAL_LABELS:
                continue
            rows.append(raw)
        return rows, hours_column

    @staticmethod
    def _positions(index: Mapping[str, int], hours_column: int) -> dict[str, int]:
        return {
            "duration": hours_column,
            "date": _column(index, DATE_HEADERS),
            "activity": _column(index, ACTIVITY_HEADERS),
            "code": _column(index, CODE_HEADERS),
            "description": _column(index, DESCRIPTION_HEADERS),
            "well": _column(index, WELL_HEADERS),
            "reference": _column(index, REFERENCE_HEADERS),
        }

    def _promote_breakdown(
        self,
        *,
        payload: Mapping[str, Any],
        document: Document,
        version: DocumentVersion,
        report: DdrReport | None,
        result: PromotionResult,
    ) -> int:
        """A daily report's "Activity / Hours" sheet: the day's work, and only its NPT lines as NPT.

        Returns the NPT rows written.  A row whose *code* says NPT becomes an NPT record - the report
        classified it, which is different from this module deciding that an activity looks like a
        problem.  Every row becomes an operation with the hours kept as the cell wrote them.
        """
        npt_rows = 0
        for table, index in self._breakdown_tables(payload):
            rows, hours_column = self._data_rows(table, index, DURATION_HEADERS)
            positions = self._positions(index, hours_column)
            provenance = self._provenance_of(table)
            for offset, row in enumerate(rows):
                npt_rows += int(
                    self._promote_breakdown_row(
                        row=row,
                        offset=offset,
                        table_id=str(table.get("table_id") or "table"),
                        sheet=str(table.get("sheet") or ""),
                        positions=positions,
                        hours_column=hours_column,
                        provenance=provenance,
                        document=document,
                        version=version,
                        report=report,
                        result=result,
                    )
                )
        return npt_rows

    def _promote_row(
        self,
        *,
        row: Sequence[Any],
        offset: int,
        table_id: str,
        sheet: str,
        positions: Mapping[str, int],
        provenance: list[dict[str, Any]],
        document: Document,
        version: DocumentVersion,
        report: DdrReport | None,
        result: PromotionResult,
    ) -> bool:
        """Write - or confirm - the rows one source line supports.  ``False`` means "skipped"."""
        well, refusal = self._row_well(
            row=row, positions=positions, document=document, report=report, offset=offset
        )
        if well is None:
            result.skipped.append(refusal or {"reason": "NO_WELL", "detail": "no well in scope"})
            return False
        hours_text = _cell(row, int(positions["duration"]))
        hours = _hours(hours_text)
        if hours is not None and hours == 0.0:
            # "No NPT recorded" is a report of a clean shift, not a zero-length problem.  Counting it
            # would make a well's event count a function of how often people wrote down that nothing
            # happened.
            result.skipped.append(
                {"reason": "ZERO_NPT", "detail": f"row {offset + 2} states no NPT for {well.name}"}
            )
            return False
        return self._write_rows(
            row=row,
            offset=offset,
            table_id=table_id,
            sheet=sheet,
            positions=positions,
            provenance=provenance,
            document=document,
            version=version,
            report=report,
            well=well,
            hours=hours,
            hours_text=hours_text,
            result=result,
        )

    def _row_well(
        self,
        *,
        row: Sequence[Any],
        positions: Mapping[str, int],
        document: Document,
        report: DdrReport | None,
        offset: int,
    ) -> tuple[Well | None, dict[str, str] | None]:
        """The well a row is about - the one its own cell names, or the one its report belongs to.

        Returns the refusal to report rather than appending it here: the caller knows whether the row
        is an NPT line or a breakdown line, and the message has to say which one a person should fix.
        """
        well_name = _cell(row, int(positions["well"]))
        well = self._well_by_name(well_name) if well_name else None
        if well_name and well is None:
            # Never re-attached to "the nearest well": a row filed under the wrong well is one wrong
            # answer in two wells' histories, and the report is what makes it fixable.
            return None, {
                "reason": "WELL_NOT_FOUND",
                "detail": (
                    f"row {offset + 2} names well {well_name!r}, which this workspace has no such well"
                ),
            }
        if well is None:
            well_id = str(report.well_id if report is not None else document.well_id or "")
            well = self.session.get(Well, well_id) if well_id else None
        if well is None:
            return None, {
                "reason": "NO_WELL",
                "detail": f"row {offset + 2} has no well to belong to",
            }
        return well, None

    def _write_rows(
        self,
        *,
        row: Sequence[Any],
        offset: int,
        table_id: str,
        sheet: str,
        positions: Mapping[str, int],
        provenance: list[dict[str, Any]],
        document: Document,
        version: DocumentVersion,
        report: DdrReport | None,
        well: Well,
        hours: float | None,
        hours_text: str,
        result: PromotionResult,
    ) -> bool:
        """Write - or confirm - the rows one NPT line supports."""
        activity = _cell(row, int(positions["activity"]))
        code = _cell(row, int(positions["code"]))
        description = _cell(row, int(positions["description"]))
        date_iso, date_text = _iso(_cell(row, int(positions["date"])))
        reference = _cell(row, int(positions["reference"]))
        identity = promotion_identity(
            version_id=version.id,
            kind="npt-row",
            table_id=table_id,
            row_index=offset,
            well_id=well.id,
        )
        # ``report_id`` is a column on the operation, the event and the NPT row; a problem reaches its
        # report through the event it was raised from, so it is not copied onto the row.
        common: dict[str, Any] = {
            "well_id": well.id,
            "document_id": document.id,
            "document_version_id": version.id,
            "origin": KnowledgeOrigin.DERIVED.value,
            "created_by": "promoter",
            "status": ConfirmationStatus.CANDIDATE,
            "provenance": provenance,
            "attributes": {
                "promoted_from": {
                    "table_id": table_id,
                    "sheet": sheet,
                    "row": offset + 2,
                    "reference": reference,
                }
            },
        }

        operation = None
        if activity:
            payload = {
                "operation_type": activity,
                "label": activity,
                "description": description,
                "started_at": date_iso,
                "record_state": RecordState.ACTUAL,
            }
            # What this line is *about*, in the columns the row keeps them in.  The mapped columns
            # (``operation_type``, ``category``, ``problem_type``) stay out: the writer stores a token
            # the vocabulary chose, and comparing an argument against that token would report a
            # conflict on every row in the corpus.
            content = {
                "label": activity,
                "description": description,
                "started_at": date_iso,
            }
            # The keys this line owns, whether or not each row is written: a sweep must not delete a
            # row on the strength of a branch this pass happened not to take.
            result.identities.update(f"{identity}{suffix}" for suffix in (":op", ":ev", ":npt"))
            outcome = self._confirm(WellOperation, identity + ":op", content, "operation", result)
            operation = self.records.record_operation(
                identity_key=identity + ":op",
                report_id=self._report_id(report, well),
                **common,
                **payload,
            )
            result.bump("operation", outcome)
            if report is not None and outcome == "created":
                self._link(
                    report=report,
                    relation=KnowledgeRelationType.REPORT_CONTAINS_OPERATION,
                    target_type="well_operation",
                    target_id=operation.id,
                    provenance=provenance,
                )

        # The event's type is the code the report used, mapped onto the problem vocabulary so that
        # "NPT-STUCK" in one file and "stuck pipe" in another group together.  With no code there is
        # no type to claim, and the event is filed as an NPT event and left at that.
        match = problem_type(code) if code else None
        event_type = match.token if match is not None else "npt"
        event_payload = {
            "event_type": event_type,
            "category": "npt",
            "label": code or "npt",
            "description": description or f"{well.name}: {hours_text or 'duration not stated'}",
            "occurred_at": date_iso,
            "occurred_at_text": date_text or None,
            "operation_id": operation.id if operation is not None else "",
        }
        event_outcome = self._confirm(
            WellEvent,
            identity + ":ev",
            {
                "label": code or "npt",
                "description": event_payload["description"],
                "occurred_at_text": date_text or None,
            },
            "event",
            result,
        )
        event = self.records.record_event(
            identity_key=identity + ":ev",
            report_id=self._report_id(report, well),
            **common,
            **event_payload,
        )
        result.bump("event", event_outcome)
        if report is not None and event_outcome == "created":
            self._link(
                report=report,
                relation=KnowledgeRelationType.REPORT_CONTAINS_EVENT,
                target_type="well_event",
                target_id=event.id,
                provenance=provenance,
            )

        npt_payload = {
            "category": code or "",
            "code": code,
            "description": description,
            "started_at": date_iso,
            "started_at_text": date_text or None,
            "duration_hours": hours,
            "duration_text": hours_text or None,
            "duration_basis": "STATED",
            # What the report gave as the reason, in its own words: a stated cause, not a diagnosis.
            "cause": description,
            "event_id": event.id,
            "operation_id": operation.id if operation is not None else "",
        }
        npt_outcome = self._confirm(
            NptRecord,
            identity + ":npt",
            {
                "subcategory": code or None,
                "description": description,
                "duration_text": hours_text or None,
                "started_at_text": date_text or None,
                "duration_hours": hours,
                "cause": description or None,
            },
            "npt",
            result,
        )
        self.records.record_npt(
            identity_key=identity + ":npt",
            report_id=self._report_id(report, well),
            **common,
            **npt_payload,
        )
        result.bump("npt", npt_outcome)

        if code:
            problem_payload = {
                "problem_type": code,
                "code": code,
                "description": description,
                "occurred_at": date_iso,
                "event_id": event.id,
                "operation_id": operation.id if operation is not None else "",
                # What the report wrote as its reason, and only when it wrote one: an empty cause
                # with a confident status on it is exactly the one-sided claim the repository refuses.
                "immediate_cause": description,
                "immediate_cause_status": (
                    CauseStatus.KNOWN.value if description else CauseStatus.UNKNOWN.value
                ),
                # root_cause is left empty on purpose: no reason code, however tidy, is a diagnosis.
                "root_cause_status": CauseStatus.UNKNOWN.value,
            }
            result.identities.add(identity + ":problem")
            problem_outcome = self._confirm(
                ProblemOccurrence,
                identity + ":problem",
                {"code": code, "description": description, "immediate_cause": description or None},
                "problem",
                result,
            )
            self.records.record_problem(
                identity_key=identity + ":problem", **common, **problem_payload
            )
            result.bump("problem", problem_outcome)
        return True

    def _promote_breakdown_row(
        self,
        *,
        row: Sequence[Any],
        offset: int,
        table_id: str,
        sheet: str,
        positions: Mapping[str, int],
        hours_column: int,
        provenance: list[dict[str, Any]],
        document: Document,
        version: DocumentVersion,
        report: DdrReport | None,
        result: PromotionResult,
    ) -> bool:
        """One line of an activity/hours sheet: an operation, plus an NPT row if its code says NPT.

        The hours are kept as the cell wrote them, in ``attributes``, because a time breakdown states a
        duration rather than a start and an end: inventing ``started_at`` to make the number
        arithmetically reachable would be exactly the fabricated timestamp the timeline promises not
        to contain.
        """
        well, refusal = self._row_well(
            row=row, positions=positions, document=document, report=report, offset=offset
        )
        if well is None:
            result.skipped.append(refusal or {"reason": "NO_WELL", "detail": "no well in scope"})
            return False
        activity = _cell(row, int(positions["activity"]))
        code = _cell(row, int(positions["code"]))
        hours_text = _cell(row, hours_column)
        hours = _hours(hours_text)
        date_iso, date_text = _iso(_cell(row, int(positions["date"])))
        identity = promotion_identity(
            version_id=version.id,
            kind="breakdown-row",
            table_id=table_id,
            row_index=offset,
            well_id=well.id,
        )
        common: dict[str, Any] = {
            "well_id": well.id,
            "document_id": document.id,
            "document_version_id": version.id,
            "origin": KnowledgeOrigin.DERIVED.value,
            "created_by": "promoter",
            "status": ConfirmationStatus.CANDIDATE,
            "provenance": provenance,
            # Only where the row came from: the hours themselves live in ``duration_text`` on the NPT
            # row and in the operation's own columns, and a copy in attributes is a second answer.
            "attributes": {
                "promoted_from": {"table_id": table_id, "sheet": sheet, "row": offset + 2}
            },
        }
        payload = {
            "operation_type": activity or code,
            "label": activity or code,
            "description": "",
            "started_at": date_iso,
            "period_text": date_text or "",
            "record_state": RecordState.ACTUAL,
        }
        result.identities.update(f"{identity}{suffix}" for suffix in (":op", ":npt"))
        outcome = self._confirm(
            WellOperation,
            identity + ":op",
            {"label": activity or code, "description": "", "started_at": date_iso},
            "operation",
            result,
        )
        operation = self.records.record_operation(
            identity_key=identity + ":op",
            report_id=self._report_id(report, well),
            **common,
            **payload,
        )
        result.bump("operation", outcome)
        if report is not None and outcome == "created":
            self._link(
                report=report,
                relation=KnowledgeRelationType.REPORT_CONTAINS_OPERATION,
                target_type="well_operation",
                target_id=operation.id,
                provenance=provenance,
            )
        if not is_npt_code(code) or hours is None or hours <= 0:
            return False
        npt_identity = identity + ":npt"
        npt_payload = {
            "category": code,
            "code": code,
            "description": activity,
            "duration_hours": hours,
            "duration_text": hours_text,
            "duration_basis": "STATED",
            "cause": "",
            "operation_id": operation.id,
        }
        npt_outcome = self._confirm(
            NptRecord,
            npt_identity,
            {
                "subcategory": code or None,
                "description": activity,
                "duration_hours": hours,
                "duration_text": hours_text or None,
            },
            "npt",
            result,
        )
        self.records.record_npt(
            identity_key=npt_identity,
            report_id=self._report_id(report, well),
            **{key: value for key, value in common.items() if key != "attributes"},
            attributes=common["attributes"],
            **npt_payload,
        )
        result.bump("npt", npt_outcome)
        return True

    # -- the document's own total --------------------------------------------
    def _promote_total(
        self,
        *,
        document: Document,
        version: DocumentVersion,
        report: DdrReport | None,
        fields: Sequence[Mapping[str, Any]],
        lines_promoted: int,
        result: PromotionResult,
    ) -> None:
        """The report's stated total, and only when it has no lines to add up.

        This is the daily-report case: a summary field of ``18.5 h`` with nothing itemised behind
        it.  The document gave no category, so the open vocabulary files the row under ``other`` - what
        keeps that from being a silent invention is that the row has no ``code``, no event and no
        problem beside it, so it counts in the hours and never in anybody's "top problems".
        """
        row = self._first_field(fields, TOTAL_NPT_FIELDS)
        if row is None:
            return
        if lines_promoted:
            result.skipped.append(
                {
                    "reason": "TOTAL_ALREADY_COUNTED",
                    "detail": f"{lines_promoted} NPT lines from this version are in, so its summary total is not added",
                }
            )
            return
        hours = _hours(row.get("value") or self._wording(row))
        if hours is None:
            result.skipped.append(
                {
                    "reason": "UNPARSEABLE_TOTAL",
                    "detail": f"the npt field read {row.get('value')!r}, which is not a duration",
                }
            )
            return
        if hours == 0.0:
            return
        well_id = str(report.well_id if report is not None else document.well_id or "")
        well = self.session.get(Well, well_id) if well_id else None
        if well is None:
            result.skipped.append(
                {"reason": "NO_WELL", "detail": "the report has no well to charge its total to"}
            )
            return
        identity = promotion_identity(
            version_id=version.id,
            kind="npt-total",
            well_id=well.id,
            extra=str(row.get("name") or ""),
        )
        payload = {
            "category": "",
            "code": "",
            "description": str(row.get("value") or self._wording(row))[:500],
            "duration_hours": hours,
            "duration_text": str(row.get("value") or ""),
            "duration_basis": "STATED",
        }
        result.identities.add(identity)
        outcome = self._confirm(
            NptRecord,
            identity,
            {
                "duration_hours": hours,
                "duration_text": payload["duration_text"],
                "description": payload["description"],
            },
            "npt",
            result,
        )
        provenance = row.get("provenance")
        self.records.record_npt(
            well_id=well.id,
            report_id=self._report_id(report, well),
            document_id=document.id,
            document_version_id=version.id,
            origin=KnowledgeOrigin.DERIVED.value,
            created_by="promoter",
            status=ConfirmationStatus.CANDIDATE,
            identity_key=identity,
            provenance=[dict(provenance)] if isinstance(provenance, Mapping) else [],
            attributes={"promoted_from": {"field": str(row.get("name") or "")}},
            **payload,
        )
        result.bump("npt", outcome)

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _report_id(report: DdrReport | None, well: Well) -> str:
        """The report a row belongs to, if the row belongs to the well that report describes.

        A shared NPT sheet is filed under one well and names several, so most of its rows have no
        report of their own: charging a B-11 event to A-3's daily report would let "everything this
        report produced" return a row from another well, and no aggregate would notice.  The
        provenance columns still say which file and which version the row came from, which is the
        trace that actually matters.
        """
        if report is None or str(report.well_id) != str(well.id):
            return ""
        return report.id

    def _well_by_name(self, name: str) -> Well | None:
        key = str(name or "").strip()
        if not key:
            return None
        if key not in self._wells_by_name:
            self._wells_by_name[key] = self.wells.find_well(key)
        return self._wells_by_name[key]

    @staticmethod
    def _first_field(
        fields: Sequence[Mapping[str, Any]], names: Sequence[str]
    ) -> dict[str, Any] | None:
        """The first named artefact field, in the caller's order of trust."""
        for name in names:
            for item in fields:
                if str(item.get("name") or "").strip().lower() == name:
                    return dict(item)
        return None

    @staticmethod
    def _wording(item: Mapping[str, Any] | None) -> str:
        """What the source actually wrote, as the extraction recorded it.

        An artefact field has no ``raw_text`` of its own: the wording lives in the provenance, in
        ``excerpt`` for a document and ``locator.read`` for a cell.  A promoter that reformatted the
        value would report its own rendering as though the file had said it.
        """
        if not item:
            return ""
        provenance = item.get("provenance")
        if not isinstance(provenance, Mapping):
            return ""
        locator = provenance.get("locator")
        if isinstance(locator, Mapping) and str(locator.get("read") or ""):
            return str(locator["read"])
        return str(provenance.get("excerpt") or "")

    def _confirm(
        self,
        model: type,
        identity_key: str,
        content: Mapping[str, Any],
        label: str,
        result: PromotionResult,
    ) -> str:
        """Decide ``created`` / ``unchanged`` / ``conflict`` before the row is written.

        ``unchanged`` is a fact rather than a claim: the stored row is read and the content the
        promotion is about is compared column by column.  A difference is reported as a conflict and
        left alone, because the row that differs may have been confirmed, annotated or corrected by a
        person since it was written - and a background pass that overwrites that to match a
        re-extraction is how a data-loss bug gets its start.
        """
        existing = self.session.execute(
            select(model).where(model.identity_key == identity_key)
        ).scalar_one_or_none()
        if existing is None:
            return "created"
        stored = record_to_dict(existing)
        differing = [
            key
            for key, value in content.items()
            if key in stored and _comparable(stored[key]) != _comparable(value)
        ]
        if not differing:
            return "unchanged"
        result.skipped.append(
            {
                "reason": "SOURCE_CHANGED",
                "detail": f"the stored {label} row {existing.id} differs from this artefact in {', '.join(sorted(differing))}; left as it is",
            }
        )
        return "conflict"

    def _link(
        self,
        *,
        report: DdrReport,
        relation: KnowledgeRelationType,
        target_type: str,
        target_id: str,
        provenance: list[dict[str, Any]],
    ) -> None:
        """Assert the edge from the report to what came out of it.

        Re-asserting is harmless here for a reason worth stating: the sanctioned write path
        strengthens an existing edge rather than tripping the uniqueness constraint, so promotion can
        run on a workspace that has already been promoted without a cleanup step.
        """
        self.records.link(
            source_type="ddr_report",
            source_id=report.id,
            relation=relation.value,
            target_type=target_type,
            target_id=str(target_id),
            provenance=provenance,
            note="promoted from the artefact",
        )
