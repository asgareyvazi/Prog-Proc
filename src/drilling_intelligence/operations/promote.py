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
from datetime import UTC, date, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import (
    CauseStatus,
    ConfirmationStatus,
    KnowledgeOrigin,
    KnowledgeRelationType,
    ProgramLifecycle,
    RecordState,
)
from ..core.errors import UnitError
from ..core.hashing import sha256_obj
from ..core.ids import new_id
from ..core.units import Quantity, parse_decimal
from ..core.vocabulary import problem_type
from ..database.models import (
    BhaComponent,
    BhaReport,
    BitRecord,
    DdrReport,
    Document,
    DocumentVersion,
    DrillingProgram,
    Extraction,
    Field,
    MudMeasurement,
    MudReport,
    NptRecord,
    ProblemOccurrence,
    SurveyRun,
    SurveyStation,
    Well,
    WellEvent,
    WellOperation,
    WellSection,
)
from ..database.serialize import record_to_dict
from ..engineering.repository import EngineeringRepository
from ..wells.repository import WellRepository
from .bha import (
    component_entries as bha_component_entries,
)
from .bha import (
    summary_entries as bha_summary_entries,
)
from .bit_record import bit_run_entries
from .contracts import PromotionOutcome, promotion_contract
from .mud import (
    SummaryEntry,
    daily_entries,
    summary_entries,
)
from .mud import (
    numeric as mud_numeric,
)
from .mud import (
    table_key as mud_table_key,
)
from .mud import (
    tables as mud_tables,
)
from .program import PROGRAM_CLASSIFICATIONS, SectionPlan, find_program_plan
from .repository import REPORT_CLASSIFICATIONS, OperationsRepository, _stamp
from .survey import (
    station_entries as survey_station_entries,
)
from .survey import (
    summary_entries as survey_summary_entries,
)
from .tableshape import normalise_label as source_label_key
from .tableshape import table_key as source_table_key

__all__ = [
    "ACTIVITY_HEADERS",
    "CODE_HEADERS",
    "DATE_FIELDS",
    "DATE_HEADERS",
    "DESCRIPTION_HEADERS",
    "DOMAIN_CHILDREN",
    "DURATION_HEADERS",
    "MUD_SUMMARY_METADATA",
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

#: The source-versioned domains and the child rows each parent owns, as ``(model, foreign key)``.
#:
#: One table rather than four near-identical methods, because supersession and the orphan sweep are
#: the same operation in every domain: stand the older source version down, keep its history, and
#: never demote a row a person confirmed.  A domain absent from this map has no children to sweep.
DOMAIN_CHILDREN: Final[dict[type, tuple[tuple[type, str], ...]]] = {
    MudReport: ((MudMeasurement, "mud_report_id"),),
    BhaReport: ((BhaComponent, "bha_report_id"),),
    BitRecord: (),
    SurveyRun: ((SurveyStation, "survey_run_id"),),
}

#: The mud summary labels that describe the *report* rather than a measured property.  They are
#: read into the parent row's own columns and are deliberately never written as measurements.
MUD_SUMMARY_METADATA: Final[frozenset[str]] = frozenset(
    {"well", "field", "report_date", "revision", "section_id", "section", "hole_size_in"}
)


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
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=UTC).isoformat()
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
    #: The static contract selected before any domain writer is entered.
    contract_id: str = ""
    #: Explicit version-level outcome; row counts remain available in ``counts``.
    outcome: str = PromotionOutcome.ELIGIBLE.value
    #: Why nothing at all was promoted ("NO_WELL", "NO_ARTEFACT"), when that is the case.
    error: str = ""

    def finalize(self) -> str:
        """Select a deterministic outcome without hiding partial/degraded rows.

        The outcome is deliberately derived after the writer has run.  ``PROMOTED``
        means at least one new row exists; ``UNCHANGED`` means the contract was
        eligible and every row was already present.  A refusal with a specific
        reason remains distinct from an ordinary empty result.
        """
        reasons = {str(item.get("reason") or "") for item in self.skipped}
        if self.outcome == PromotionOutcome.UNSUPPORTED.value:
            return self.outcome
        if self.error == "NO_ARTEFACT":
            self.outcome = PromotionOutcome.MISSING_ARTEFACT.value
        elif self.error == "NO_WELL":
            self.outcome = PromotionOutcome.MISSING_WELL.value
        elif self.error:
            self.outcome = PromotionOutcome.ERROR.value
        elif "AMBIGUOUS_SECTIONS" in reasons or "AMBIGUOUS" in reasons:
            self.outcome = PromotionOutcome.AMBIGUOUS.value
        elif "MISSING_PROVENANCE" in reasons:
            self.outcome = PromotionOutcome.MISSING_PROVENANCE.value
        elif (
            any(
                reason
                in {"INVALID_FIELD", "UNPARSEABLE_TOTAL", "DEPTH_WITHOUT_UNIT", "NO_SECTION_STATED"}
                for reason in reasons
            )
            and not self.wrote_anything
            and not self.total("unchanged")
        ):
            self.outcome = PromotionOutcome.INVALID_FIELDS.value
        elif self.total("conflict"):
            self.outcome = PromotionOutcome.CONFLICT.value
        elif self.wrote_anything:
            self.outcome = PromotionOutcome.PROMOTED.value
        elif self.total("unchanged"):
            self.outcome = PromotionOutcome.UNCHANGED.value
        else:
            self.outcome = PromotionOutcome.ELIGIBLE.value
        return self.outcome

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
            "contract_id": self.contract_id or None,
            "eligible": bool(self.contract_id)
            and self.outcome
            not in {
                PromotionOutcome.UNSUPPORTED.value,
                PromotionOutcome.MISSING_ARTEFACT.value,
                PromotionOutcome.MISSING_WELL.value,
                PromotionOutcome.MISSING_PROVENANCE.value,
                PromotionOutcome.INVALID_FIELDS.value,
                PromotionOutcome.AMBIGUOUS.value,
                PromotionOutcome.ERROR.value,
            },
            "outcome": self.outcome,
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
        self.engineering = EngineeringRepository(session)
        self._wells_by_name: dict[str, Well | None] = {}

    @staticmethod
    def _missing_promotion_evidence(payload: Mapping[str, Any], handler: str) -> str:
        """Return a blocking evidence finding before a domain writer is entered.

        The stored artefact is authoritative for promotion.  A table or field that
        cannot point back to its source is still retained for knowledge/review, but
        it cannot create an operational row whose provenance would be empty.
        """
        if handler == "program":
            plan = find_program_plan(payload)
            if plan.sections and any(not section.provenance for section in plan.sections):
                return "the planned section fields have no recorded source locator"
            return ""
        if handler == "mud_report":
            summary = summary_entries(payload)
            daily = daily_entries(payload)
            if not summary:
                return "no recognised mud summary label/value rows were stored"
            if not daily:
                return "no repeated daily mud-test table with sample labels was stored"
            relevant = {mud_table_key(entry.table) for entry in (*summary, *daily)}
            missing = [
                key or "table"
                for key in sorted(relevant)
                if not isinstance(
                    next(
                        (
                            table.get("provenance")
                            for table in mud_tables(payload)
                            if mud_table_key(table) == key
                        ),
                        None,
                    ),
                    Mapping,
                )
            ]
            if missing:
                return "mud table provenance is missing for: " + ", ".join(missing)
            return ""
        if handler in {"bha_report", "bit_record", "directional_survey"}:
            # No recognised table is *not* a provenance failure - the writer reports that as an
            # unsupported source shape.  Only tables the contract will actually read are held to the
            # promise that they can show where they came from.
            if handler == "bha_report":
                relevant = [entry.table for entry in bha_component_entries(payload)]
            elif handler == "bit_record":
                relevant = [entry.table for entry in bit_run_entries(payload)]
            else:
                relevant = [entry.table for entry in survey_station_entries(payload)]
            missing = [
                source_table_key(table) or "table"
                for table in relevant
                if not isinstance(table.get("provenance"), Mapping)
            ]
            if missing:
                return f"{handler} table provenance is missing for: " + ", ".join(sorted(set(missing)))
            return ""
        tables = find_npt_tables(payload) + find_breakdown_tables(payload)
        if tables:
            missing = [
                str(table.get("table_id") or table.get("sheet") or table.get("page") or "table")
                for table, _index in tables
                if not isinstance(table.get("provenance"), Mapping)
            ]
            if missing:
                return "table provenance is missing for: " + ", ".join(sorted(missing))
        return ""

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
        # A forced re-extraction may retain immutable artefact history for the same source version.
        # Promotion consumes the newest stored artefact deterministically, never an arbitrary row.
        extraction = self.session.execute(
            select(Extraction)
            .where(Extraction.document_version_id == version.id)
            .order_by(Extraction.created_at.desc(), Extraction.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        payload = dict((extraction.document_json if extraction else None) or {})
        classification = str(document.classification or "")
        contract = promotion_contract(classification)
        result = PromotionResult(
            document_id=document.id,
            version_id=version.id,
            classification=classification,
            contract_id=contract.contract_id if contract is not None else "",
        )
        if not payload:
            result.error = "NO_ARTEFACT"
            result.skipped.append(
                {
                    "reason": "NO_ARTEFACT",
                    "detail": f"version {version.id} has no stored artefact to promote from",
                }
            )
            result.finalize()
            return result
        if contract is None or not contract.domain_promotable:
            # A document classification is not a writer permission.  Keep the legacy NOT_A_REPORT
            # diagnostic used by callers that ask why a mud/reference file produced no operational row,
            # and add the explicit V2 outcome that makes the denial machine-readable.
            result.outcome = PromotionOutcome.UNSUPPORTED.value
            result.skipped.append(
                {
                    "reason": "UNSUPPORTED_CLASSIFICATION",
                    "detail": (
                        f"{classification or 'unclassified'} has no registered domain promotion contract; "
                        "stored extraction/knowledge remain available"
                    ),
                }
            )
            if classification not in REPORT_CLASSIFICATIONS | PROGRAM_CLASSIFICATIONS:
                result.skipped.append(
                    {
                        "reason": "NOT_A_REPORT",
                        "detail": f"{classification or 'unclassified'} has no report/program writer",
                    }
                )
            result.finalize()
            return result
        missing = self._missing_promotion_evidence(payload, contract.handler)
        if missing:
            result.skipped.append({"reason": "MISSING_PROVENANCE", "detail": missing})
            result.finalize()
            return result
        fields = [dict(item) for item in (payload.get("extracted_fields") or [])]
        if contract.handler == "program":
            # A program states a plan, not a day's work.  It leaves the report path entirely: running
            # it through ``_promote_report`` would file next month's intention as this well's history.
            self._promote_program(
                payload=payload, document=document, version=version, result=result
            )
            result.finalize()
            return result
        if contract.handler == "mud_report":
            self._promote_mud_report(
                payload=payload, document=document, version=version, result=result, replace=replace
            )
            result.finalize()
            return result
        if contract.handler == "bha_report":
            self._promote_bha_report(
                payload=payload, document=document, version=version, result=result, replace=replace
            )
            result.finalize()
            return result
        if contract.handler == "bit_record":
            self._promote_bit_record(
                payload=payload, document=document, version=version, result=result, replace=replace
            )
            result.finalize()
            return result
        if contract.handler == "directional_survey":
            self._promote_directional_survey(
                payload=payload, document=document, version=version, result=result, replace=replace
            )
            result.finalize()
            return result
        if contract.handler != "report":  # pragma: no cover - import-time registry guard
            result.error = "UNKNOWN_HANDLER"
            result.finalize()
            return result
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
        result.finalize()
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
        # Children before parents, one flush at a time: the record tables point at each other by
        # ordinary foreign keys, and SQLite enforces only what it is given, in the order it is given.
        for model in (
            ProblemOccurrence,
            NptRecord,
            WellEvent,
            WellOperation,
            DdrReport,
            MudMeasurement,
            MudReport,
            BhaComponent,
            BhaReport,
            SurveyStation,
            SurveyRun,
            BitRecord,
        ):
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

    # -- program --------------------------------------------------------------
    def _promote_program(
        self,
        *,
        payload: Mapping[str, Any],
        document: Document,
        version: DocumentVersion,
        result: PromotionResult,
    ) -> DrillingProgram | None:
        """Write the plan one program version states: one program row, one target per section.

        The program row is keyed to the *document version*, not to the document, because that is what
        "revision 12" means here: a new revision arrives as a new version of the same file, and the
        two plans have to stay separately answerable - revision 11 said 9,900 ft and somebody drilled
        against it.  Re-promoting the same version therefore re-finds its program instead of adding a
        second one, exactly as :meth:`OperationsRepository.register_report` does for a report.

        A program superseded by a newer version keeps its rows and its provenance and loses only
        ``is_current``: nothing recomputes, and no historical plan is rewritten.
        """
        if not document.well_id:
            result.error = "NO_WELL"
            result.skipped.append(
                {
                    "reason": "NO_WELL",
                    "detail": f"{document.filename} is not linked to a well",
                }
            )
            return None
        plan = find_program_plan(payload)
        for entry in plan.skipped:
            result.skipped.append(dict(entry))
        if not plan:
            return None

        existing = self.session.execute(
            select(DrillingProgram).where(DrillingProgram.document_version_id == version.id)
        ).scalar_one_or_none()
        well = self.session.get(Well, str(document.well_id))
        if existing is not None:
            result.bump("program", "unchanged")
            for _section in plan.sections:
                result.bump("target", "unchanged")
            return existing

        # Stand the previous revision down *before* inserting this one: ``uq_program_one_current``
        # is a partial unique index on the code of the current revision, so two current programs of
        # the same document cannot coexist even for the length of a flush.  Superseding first is what
        # makes "revision 13 arrived" a legal state rather than an integrity error.
        superseded = self._supersede_older_programs(document=document)
        program = self.engineering.create_program(
            title=str(document.title or document.filename or "drilling program")[:400],
            code=str(document.identity_path or "") or None,
            summary="",
            # A promoted plan is a candidate like every other derived row: nobody has reviewed it, so
            # it must not arrive wearing the approval the document's own cover page claims.
            status=ProgramLifecycle.DRAFT,
            created_by="promoter",
            origin=KnowledgeOrigin.DERIVED.value,
            provenance=[dict(item) for item in plan.sections[0].provenance],
            attributes={
                "identity_path": document.identity_path,
                "filename": document.filename,
                # The document's own words for its revision, kept verbatim.  The promoter does not
                # renumber: ``DrillingProgram.revision`` counts supersessions inside this database,
                # and "Rev 12" is what the file calls itself - two different questions.
                "document_revision": str(document.revision or ""),
                "document_status": str(version.status or document.status or ""),
            },
            well_id=str(well.id) if well is not None else "",
            field_id=str(well.field_id or "") if well is not None else "",
            project_id=str(well.project_id or "") if well is not None else "",
            revision=int(superseded.revision or 1) + 1 if superseded is not None else 1,
            supersedes_id=superseded.id if superseded is not None else "",
        )
        program.document_id = document.id
        program.document_version_id = version.id
        self.session.flush()
        result.bump("program", "created")

        for sequence, section in enumerate(plan.sections, start=1):
            row = self._section_for_plan(section, well=well)
            self.engineering.add_target(
                program.id,
                name=section.name,
                section_id=str(row.id) if row is not None else "",
                sequence=sequence,
                origin=KnowledgeOrigin.DERIVED.value,
                provenance=[dict(item) for item in section.provenance],
                attributes={
                    # Which artefact field produced which column: the row can say how it was derived
                    # without a reader re-deriving it from the provenance excerpts.
                    "field_sources": dict(section.sources),
                    "identity_key": promotion_identity(
                        version_id=version.id,
                        kind="program_target",
                        well_id=str(well.id) if well is not None else "",
                        extra=section.name,
                    ),
                },
                **section.values(),
            )
            result.bump("target", "created")
        return program

    def _section_for_plan(self, section: SectionPlan, *, well: Well | None) -> WellSection | None:
        """The hole section this planned target is about, created if the well has not got one yet.

        This is the *planned* half of a section and nothing else.  A program states that a 12 1/4 in
        hole is going to be drilled, which is a real fact about the well and is what makes the section
        addressable before anyone spuds it - but it says nothing about what happened, so only the
        section's identity and its nominal size are written here.  ``top_depth``/``bottom_depth``,
        the durations and the actual mud weight stay NULL until an actual source supplies them: the
        plan's own depth is already on the target, and copying it into the section's as-drilled
        column is precisely the substitution ADR-0018 exists to prevent.

        Identity is ``(well_id, name)``, so a later revision of the same program finds the section it
        already created instead of adding a second one, and the section outlives the revision that
        first named it.  ``get_or_create_section`` only records provenance when it *creates* the row,
        which is what keeps revision 13 from restating where revision 12's section came from.
        """
        if well is None or not section.name:
            return None
        return self.wells.get_or_create_section(
            well,
            section.name,
            hole_size_in=section.hole_size_in,
            origin=KnowledgeOrigin.DERIVED.value,
            provenance=[dict(item) for item in section.provenance],
            document_id=str(self._document_id_from(section.provenance)),
            document_version_id=str(self._version_id_from(section.provenance)),
        )

    @staticmethod
    def _document_id_from(provenance: Sequence[Mapping[str, Any]]) -> str:
        for item in provenance:
            value = str(item.get("document_id") or "")
            if value:
                return value
        return ""

    @staticmethod
    def _version_id_from(provenance: Sequence[Mapping[str, Any]]) -> str:
        for item in provenance:
            value = str(item.get("document_version_id") or "")
            if value:
                return value
        return ""

    def _supersede_older_programs(self, *, document: Document) -> DrillingProgram | None:
        """Stand down this document's promoted programs, and return the newest of them.

        Older revisions are *not* deleted and *not* recomputed - a plan somebody drilled against stays
        readable, with its targets and its provenance intact - they simply stop being the plan to
        follow.  Only rows this promoter derived are touched: a program a person entered by hand is
        not superseded by a file arriving.
        """
        previous = list(
            self.session.execute(
                select(DrillingProgram)
                .where(DrillingProgram.document_id == document.id)
                .where(DrillingProgram.origin == KnowledgeOrigin.DERIVED.value)
                .order_by(DrillingProgram.revision.desc(), DrillingProgram.id)
            ).scalars()
        )
        if not previous:
            return None
        for row in previous:
            if row.is_current:
                row.is_current = False
                row.status = str(ProgramLifecycle.SUPERSEDED)
        self.session.flush()
        return previous[0]

    # -- mud report -----------------------------------------------------------
    @staticmethod
    def _table_provenance(
        table: Mapping[str, Any],
        *,
        document: Document,
        version: DocumentVersion,
        row_index: int | None = None,
        column_index: int | None = None,
        source_label: str = "",
        source_unit: str = "",
        source_remark: str = "",
    ) -> list[dict[str, Any]]:
        """Enrich stored table provenance with owning registry ids without changing the locator.

        Direct router output has blank ids because it has not entered the registry yet.  Promotion is
        the first point at which the immutable document/version and its source hash are known, so it
        fills those linkage fields here.  It never fabricates a sheet, range or cell: those remain
        exactly what the stored table carried.
        """
        raw = table.get("provenance")
        if not isinstance(raw, Mapping):
            return []
        evidence = dict(raw)
        for key, value in (
            ("document_id", document.id),
            ("document_version_id", version.id),
            ("source_sha256", version.sha256),
            ("filename", document.filename),
            ("source_relative_path", version.source_relative_path or document.identity_path),
            ("source_table_id", table.get("table_id") or ""),
            ("source_sheet", table.get("sheet") or ""),
            ("source_range", table.get("anchor") or ""),
        ):
            if value and not evidence.get(key):
                evidence[key] = value
        if row_index is not None:
            evidence["source_row_index"] = int(row_index)
        if column_index is not None:
            evidence["source_column_index"] = int(column_index)
        if source_label:
            evidence["source_label"] = source_label
        if source_unit:
            evidence["source_unit"] = source_unit
        if source_remark:
            evidence["source_remark"] = source_remark
        return [evidence]

    @staticmethod
    def _mud_date(value: str) -> tuple[datetime | None, str]:
        iso, wording = _iso(value)
        if not iso:
            return None, wording
        return _stamp(iso), wording

    def _explicit_section(
        self,
        *,
        well: Well,
        explicit_id: str,
        explicit_name: str,
        hole_size_text: str,
        result: PromotionResult,
        domain: str,
    ) -> tuple[str | None, str]:
        """Resolve only explicit section identifiers or exact deterministic attributes.

        MD/TVD are intentionally absent from this decision.  A depth locates a sample in a well but
        does not identify which durable section owns it.  Multiple hole-size/name matches remain NULL
        and are reported rather than selected by order or proximity.  One well's sections are ordered
        by their own ``sequence`` so the *diagnostic* is stable, but the order never selects a winner:
        more than one candidate is always ``AMBIGUOUS``.
        """
        sections = list(
            self.session.execute(
                select(WellSection)
                .where(WellSection.well_id == well.id)
                .order_by(WellSection.sequence, WellSection.id)
            ).scalars()
        )
        hole = mud_numeric(hole_size_text) if hole_size_text else None
        if not explicit_id and not explicit_name and hole is None:
            return None, "NOT_STATED"
        candidates: list[WellSection] = []
        if explicit_id:
            candidates = [
                section
                for section in sections
                if str(section.id) == explicit_id
                or str(section.name).strip().casefold() == explicit_id.casefold()
            ]
        elif explicit_name:
            candidates = [
                section
                for section in sections
                if str(section.name).strip().casefold() == explicit_name.casefold()
            ]
        elif hole is not None:
            candidates = [
                section
                for section in sections
                if section.hole_size_in is not None and float(section.hole_size_in) == hole
            ]
        if len(candidates) == 1:
            return str(candidates[0].id), "EXPLICIT" if (explicit_id or explicit_name) else "ATTRIBUTE"
        if len(candidates) > 1:
            result.skipped.append(
                {
                    "reason": "AMBIGUOUS_SECTIONS",
                    "detail": f"explicit {domain} section attributes match more than one well section",
                }
            )
            return None, "AMBIGUOUS"
        result.skipped.append(
            {
                "reason": "SECTION_NOT_FOUND",
                "detail": (
                    f"explicit {domain} section attributes do not match a durable section of the well"
                ),
            }
        )
        return None, "UNMATCHED"

    @staticmethod
    def _report_duplicates(
        *, summary: Sequence[SummaryEntry], result: PromotionResult
    ) -> None:
        """Report a summary label the source states more than once with different values.

        The parent row takes the first stated value in source order, which is deterministic and
        auditable - but "the first" is only defensible if the reader can see that there was a second.
        Without this the parent's ``depth_md_value`` could silently be one of two numbers the source
        disagreed about.
        """
        by_property: dict[str, list[str]] = {}
        for entry in summary:
            by_property.setdefault(entry.property_name, []).append(entry.source_value.strip())
        for property_name, values in sorted(by_property.items()):
            distinct = sorted(set(values))
            if len(values) > 1 and len(distinct) > 1:
                result.skipped.append(
                    {
                        "reason": "DUPLICATE_PROPERTY",
                        "detail": (
                            f"the mud summary states {property_name} {len(values)} times with "
                            f"different values ({', '.join(distinct)}); the first stated value is "
                            "used and the disagreement is reported"
                        ),
                    }
                )

    def _mud_section(
        self,
        *,
        well: Well,
        summary: Sequence[SummaryEntry],
        result: PromotionResult,
    ) -> tuple[str | None, str]:
        """The mud contract's section decision, from its summary label/value rows."""
        hole_entry = next(
            (entry for entry in summary if entry.property_name == "hole_size_in"), None
        )
        return self._explicit_section(
            well=well,
            explicit_id=next(
                (entry.source_value.strip() for entry in summary if entry.property_name == "section_id"),
                "",
            ),
            explicit_name=next(
                (entry.source_value.strip() for entry in summary if entry.property_name == "section"),
                "",
            ),
            hole_size_text=hole_entry.source_value if hole_entry else "",
            result=result,
            domain="mud",
        )

    def _confirm_row(
        self,
        model: type,
        identity_key: str,
        content: Mapping[str, Any],
        label: str,
        result: PromotionResult,
    ) -> tuple[Any | None, str]:
        """Compare a reprocessed row and never overwrite a row a person may have confirmed."""
        existing = self.session.execute(
            select(model).where(model.identity_key == identity_key)
        ).scalar_one_or_none()
        if existing is None:
            return None, "created"
        differing = [
            key
            for key, value in content.items()
            if key in record_to_dict(existing)
            and _comparable(getattr(existing, key, None)) != _comparable(value)
        ]
        if differing:
            result.skipped.append(
                {
                    "reason": "SOURCE_CHANGED",
                    "detail": (
                        f"the stored {label} row {existing.id} differs from this artefact in "
                        f"{', '.join(sorted(differing))}; left as it is"
                    ),
                }
            )
            return existing, "conflict"
        return existing, "unchanged"

    def _supersede_source_versions(
        self, *, model: type, document: Document, version: DocumentVersion, well: Well
    ) -> None:
        """Stand down older derived source versions, retaining every human decision and row.

        Generic across the source-versioned domains (mud, BHA, bit, survey) because the rule is one
        rule: a newer derived version of the same document for the same well becomes the current
        statement, the older one stays in the database as history, and a row a person confirmed is
        never demoted by a machine.  A version that is *older* than one already promoted changes
        nothing - re-processing an archive must not stand down the current record.
        """
        previous = list(
            self.session.execute(
                select(model)
                .where(
                    model.document_id == document.id,
                    model.well_id == well.id,
                    model.document_version_id != version.id,
                    model.origin == KnowledgeOrigin.DERIVED.value,
                    model.is_current.is_(True),
                )
                .order_by(model.id)
            ).scalars()
        )
        for parent in previous:
            old_version = (
                self.session.get(DocumentVersion, str(parent.document_version_id))
                if parent.document_version_id
                else None
            )
            if old_version is not None and old_version.version_number >= version.version_number:
                continue
            parent.is_current = False
            if str(parent.status or "") != ConfirmationStatus.CONFIRMED.value:
                parent.status = "SUPERSEDED"
            for child_model, foreign_key in DOMAIN_CHILDREN.get(model, ()):
                children = list(
                    self.session.execute(
                        select(child_model).where(
                            getattr(child_model, foreign_key) == parent.id,
                            child_model.is_current.is_(True),
                        )
                    ).scalars()
                )
                for child in children:
                    child.is_current = False
                    if str(child.status or "") != ConfirmationStatus.CONFIRMED.value:
                        child.status = "SUPERSEDED"
        if previous:
            self.session.flush()

    def _write_mud_measurement(
        self,
        *,
        report: MudReport,
        property_name: str,
        source_label: str,
        source_value_text: str,
        source_unit: str,
        sample_key: str,
        sample_index: int,
        sample_label: str,
        measured_at_text: str,
        source_remark: str,
        provenance: list[dict[str, Any]],
        table_id: str,
        row_index: int,
        column_index: int | None,
        result: PromotionResult,
        quality_override: str = "",
    ) -> None:
        value = mud_numeric(source_value_text)
        if value is None:
            return
        quality = quality_override or ("VALID" if source_unit else "UNVERIFIED")
        # A conflicted measurement is stored so the disagreement is auditable, but it is not
        # presented as the value: marking it current would leave two rows answering "what was the mud
        # weight" with different numbers, which is the exact failure the duplicate rule exists to
        # prevent.  It waits for a person instead, and says why in the same field the reader sees.
        conflicted = quality == "CONFLICT"
        identity = promotion_identity(
            version_id=str(report.document_version_id or ""),
            kind="mud-measurement",
            table_id=table_id,
            row_index=row_index,
            well_id=report.well_id,
            extra=f"{property_name}:{sample_key}:{column_index if column_index is not None else ''}",
        )
        result.identities.add(identity)
        content = {
            "property_name": property_name,
            "source_label": source_label,
            "sample_key": sample_key,
            "sample_label": sample_label or None,
            "measured_at_text": measured_at_text or None,
            "source_value_text": source_value_text,
            "unit": source_unit,
            "value": value,
            "source_remark": source_remark or None,
            "quality": quality,
        }
        existing, outcome = self._confirm_row(
            MudMeasurement, identity, content, "mud measurement", result
        )
        if existing is not None:
            result.bump("mud_measurement", outcome)
            return
        self.session.add(
            MudMeasurement(
                id=new_id("mudm"),
                mud_report_id=report.id,
                well_id=report.well_id,
                section_id=report.section_id,
                document_id=report.document_id,
                document_version_id=report.document_version_id,
                property_name=property_name,
                source_label=source_label,
                sample_key=sample_key,
                sample_index=sample_index,
                sample_label=sample_label or None,
                measured_at_text=measured_at_text or None,
                value=value,
                unit=source_unit,
                normalized_value=None,
                normalized_unit=None,
                source_value_text=source_value_text,
                source_remark=source_remark or None,
                quality=quality,
                record_state=RecordState.ACTUAL.value,
                # ``ConfirmationStatus`` has no "needs review" member and inventing one would put a
                # value in the column that every other reader has to learn to handle.  The row *is* a
                # candidate - it just is not the current authority - so it stays CANDIDATE and the
                # conflict is carried where it is already read from: ``quality`` and ``is_current``.
                status=ConfirmationStatus.CANDIDATE.value,
                origin=KnowledgeOrigin.DERIVED.value,
                created_by="promoter",
                provenance=provenance,
                identity_key=identity,
                is_current=not conflicted,
                attributes={
                    "table_id": table_id,
                    "source_row_index": row_index,
                    **({"conflict": "the source states this property more than once, disagreeing"}
                       if conflicted else {}),
                },
            )
        )
        result.bump("mud_measurement", "created")

    def _delete_domain_orphans(
        self, model: type, *, version_id: str, kept: set[str]
    ) -> int:
        """Remove only the unconfirmed derived rows this source version no longer states.

        Shared by every domain the promoter writes because the rule is the same everywhere: an
        identity this pass did not confirm again is a row the artefact stopped stating.  A row a
        person confirmed is **not** deleted - it is stood down and marked, because the confirmation
        is the person's statement about the source they read, and a later extraction that happens to
        omit the line is not evidence the line never existed.
        """
        rows = list(
            self.session.execute(
                select(model).where(
                    model.document_version_id == version_id,
                    model.origin == KnowledgeOrigin.DERIVED.value,
                )
            ).scalars()
        )
        removed = 0
        for row in rows:
            if str(row.identity_key or "") in kept:
                continue
            if str(row.status or "") == ConfirmationStatus.CONFIRMED.value:
                row.is_current = False
                row.attributes = {**(row.attributes or {}), "source_removed": True}
                continue
            self.session.delete(row)
            removed += 1
        if rows:
            self.session.flush()
        return removed

    def _promote_mud_report(
        self,
        *,
        payload: Mapping[str, Any],
        document: Document,
        version: DocumentVersion,
        result: PromotionResult,
        replace: bool = True,
    ) -> MudReport | None:
        """Promote the stored summary and repeated daily-test tables, never a filename or prose."""
        if not document.well_id:
            result.error = "NO_WELL"
            result.skipped.append(
                {"reason": "NO_WELL", "detail": f"{document.filename} is not linked to a well"}
            )
            return None
        well = self.session.get(Well, str(document.well_id))
        if well is None:
            result.error = "NO_WELL"
            result.skipped.append({"reason": "NO_WELL", "detail": "the linked well does not exist"})
            return None
        summary = summary_entries(payload)
        daily = daily_entries(payload)
        source_well = next(
            (entry.source_value for entry in summary if entry.property_name == "well"), ""
        )
        known_names = {str(well.name).strip().casefold()}
        if well.well_identifier:
            known_names.add(str(well.well_identifier).strip().casefold())
        if source_well and source_well.strip().casefold() not in known_names:
            result.error = "WELL_SCOPE_CONFLICT"
            result.skipped.append(
                {
                    "reason": "WELL_SCOPE_CONFLICT",
                    "detail": f"the mud summary names {source_well!r}, not the document's linked well {well.name!r}",
                }
            )
            return None
        source_field = next(
            (entry.source_value for entry in summary if entry.property_name == "field"), ""
        )
        field = self.session.get(Field, str(well.field_id)) if well.field_id else None
        known_field_names = {str(field.name).strip().casefold()} if field is not None else set()
        if source_field and source_field.strip().casefold() not in known_field_names:
            result.error = "WELL_SCOPE_CONFLICT"
            result.skipped.append(
                {
                    "reason": "WELL_SCOPE_CONFLICT",
                    "detail": f"the mud summary names field {source_field!r}, not the linked field {getattr(field, 'name', '')!r}",
                }
            )
            return None
        summary_table = next((entry.table for entry in summary if entry.table.get("rows")), {})
        summary_id = mud_table_key(summary_table) or "summary"
        section_id, section_resolution = self._mud_section(
            well=well, summary=summary, result=result
        )
        self._report_duplicates(summary=summary, result=result)
        report_date_entry = next(
            (entry for entry in summary if entry.property_name == "report_date"), None
        )
        report_date, report_date_text = self._mud_date(
            report_date_entry.source_value if report_date_entry else ""
        )
        revision_entry = next(
            (entry for entry in summary if entry.property_name == "revision"), None
        )
        md_entry = next((entry for entry in summary if entry.property_name == "depth_md"), None)
        tvd_entry = next((entry for entry in summary if entry.property_name == "depth_tvd"), None)
        md_value = mud_numeric(md_entry.source_value) if md_entry else None
        tvd_value = mud_numeric(tvd_entry.source_value) if tvd_entry else None
        higher_current = bool(
            self.session.scalar(
                select(MudReport.id)
                .join(
                    DocumentVersion,
                    DocumentVersion.id == MudReport.document_version_id,
                    isouter=True,
                )
                .where(
                    MudReport.document_id == document.id,
                    MudReport.well_id == well.id,
                    MudReport.is_current.is_(True),
                    DocumentVersion.version_number > version.version_number,
                )
                .limit(1)
            )
        )
        self._supersede_source_versions(
            model=MudReport, document=document, version=version, well=well
        )
        report_identity = promotion_identity(
            version_id=version.id,
            kind="mud-report",
            table_id=summary_id,
            row_index=0,
            well_id=well.id,
            extra=section_id or "",
        )
        result.identities.add(report_identity)
        parent_provenance = self._table_provenance(summary_table, document=document, version=version)
        report_content = {
            "well_id": well.id,
            "section_id": section_id,
            "report_date": report_date,
            "report_date_text": report_date_text or None,
            "revision": revision_entry.source_value if revision_entry else None,
            "depth_md_value": md_value,
            "depth_md_unit": md_entry.source_unit if md_entry else "",
            "depth_tvd_value": tvd_value,
            "depth_tvd_unit": tvd_entry.source_unit if tvd_entry else "",
        }
        existing, outcome = self._confirm_row(
            MudReport, report_identity, report_content, "mud report", result
        )
        if existing is not None:
            report = existing
            result.bump("mud_report", outcome)
        else:
            report = MudReport(
                id=new_id("mud"),
                well_id=well.id,
                section_id=section_id,
                document_id=document.id,
                document_version_id=version.id,
                report_date=report_date,
                report_date_text=report_date_text or None,
                report_number=revision_entry.source_value if revision_entry else None,
                revision=revision_entry.source_value if revision_entry else None,
                depth_md_value=md_value,
                depth_md_unit=md_entry.source_unit if md_entry else "",
                depth_tvd_value=tvd_value,
                depth_tvd_unit=tvd_entry.source_unit if tvd_entry else "",
                record_state=RecordState.ACTUAL.value,
                status=ConfirmationStatus.CANDIDATE.value,
                document_status=str(version.status or document.status or ""),
                origin=KnowledgeOrigin.DERIVED.value,
                created_by="promoter",
                provenance=parent_provenance,
                identity_key=report_identity,
                is_current=not higher_current,
                attributes={
                    "summary_table_id": summary_id,
                    "section_resolution": section_resolution,
                    "source_well_name": source_well,
                    "source_field_name": source_field,
                    "summary_properties": sorted(
                        {
                            entry.property_name
                            for entry in summary
                            if entry.property_name
                            not in {
                                "well",
                                "field",
                                "report_date",
                                "revision",
                                "section_id",
                                "section",
                                "hole_size_in",
                            }
                        }
                    ),
                },
            )
            if higher_current:
                report.status = "SUPERSEDED"
            self.session.add(report)
            self.session.flush()
            result.bump("mud_report", "created")
        # A source-owned relation is useful for review/search and is the only graph edge this contract
        # justifies.  It is asserted after the row exists so endpoint validation remains closed.
        self.records.link(
            source_type="well",
            source_id=well.id,
            relation=KnowledgeRelationType.WELL_HAS_MUD.value,
            target_type="mud_report",
            target_id=report.id,
            provenance=parent_provenance,
            note="source-version mud report promoted from stored extraction",
        )
        if section_id:
            self.records.link(
                source_type="well_section",
                source_id=section_id,
                relation=KnowledgeRelationType.SECTION_HAS_MUD.value,
                target_type="mud_report",
                target_id=report.id,
                provenance=parent_provenance,
                note="section explicitly matched by stored mud attributes",
            )
        # A summary table may state one property under two spellings - "Mud weight (ppg)" and "MW" -
        # and both are genuinely in the source.  Writing both as ACTUAL rows would leave two
        # competing authorities for the same measurement with no sign that they disagree, so the
        # duplicate is detected here and handled deterministically instead.
        summary_measurements: dict[str, list[SummaryEntry]] = {}
        for entry in summary:
            if entry.property_name in MUD_SUMMARY_METADATA:
                continue
            if mud_numeric(entry.source_value) is None:
                continue
            summary_measurements.setdefault(entry.property_name, []).append(entry)
        for property_name, group in summary_measurements.items():
            distinct = {entry.source_value.strip() for entry in group}
            conflicted = len(group) > 1 and len(distinct) > 1
            if len(group) > 1:
                result.skipped.append(
                    {
                        "reason": "DUPLICATE_PROPERTY",
                        "detail": (
                            f"the mud summary states {property_name} {len(group)} times "
                            f"({', '.join(sorted(distinct))}); "
                            + (
                                "the rows disagree, so every copy is stored as CONFLICT and none is "
                                "presented as the value"
                                if conflicted
                                else "the rows agree, so only the first is stored"
                            )
                        ),
                    }
                )
            for position, entry in enumerate(group):
                if len(group) > 1 and not conflicted and position > 0:
                    # The same assertion twice is one measurement, not two authorities.
                    continue
                self._write_mud_measurement(
                    report=report,
                    property_name=entry.property_name,
                    source_label=entry.source_label,
                    source_value_text=entry.source_value,
                    source_unit=entry.source_unit,
                    sample_key="SUMMARY",
                    sample_index=0,
                    sample_label="SUMMARY",
                    measured_at_text=report_date_text,
                    source_remark=entry.remark,
                    provenance=self._table_provenance(
                        entry.table,
                        document=document,
                        version=version,
                        row_index=entry.row_index,
                        source_label=entry.source_label,
                        source_unit=entry.source_unit,
                        source_remark=entry.remark,
                    ),
                    table_id=mud_table_key(entry.table) or summary_id,
                    row_index=entry.row_index,
                    column_index=1,
                    result=result,
                    quality_override="CONFLICT" if conflicted else "",
                )
                if conflicted:
                    result.bump("mud_measurement", "conflict")
        for entry in daily:
            self._write_mud_measurement(
                report=report,
                property_name=entry.property_name,
                source_label=entry.header,
                source_value_text=entry.source_value,
                source_unit=entry.source_unit,
                sample_key=f"{mud_table_key(entry.table) or 'daily'}:row:{entry.row_index}",
                sample_index=entry.row_index,
                sample_label=entry.sample_label,
                measured_at_text=entry.sample_time,
                source_remark=entry.note,
                provenance=self._table_provenance(
                    entry.table,
                    document=document,
                    version=version,
                    row_index=entry.row_index,
                    column_index=entry.column_index,
                    source_label=entry.header,
                    source_unit=entry.source_unit,
                    source_remark=entry.note,
                ),
                table_id=mud_table_key(entry.table) or "daily",
                row_index=entry.row_index,
                column_index=entry.column_index,
                result=result,
            )
        if (
            replace
            and bool(result.identities)
            and result.outcome != PromotionOutcome.UNSUPPORTED.value
        ):
            removed = self._delete_domain_orphans(
                MudMeasurement, version_id=version.id, kept=result.identities
            )
            if removed:
                result.counts["removed"] = {"created": removed, "unchanged": 0, "conflict": 0}
        return report

    # -- shared source-scope guard -------------------------------------------
    def _source_scope_conflict(
        self,
        *,
        well: Well,
        source_well: str,
        source_field: str,
        result: PromotionResult,
        domain: str,
    ) -> bool:
        """Refuse a whole artefact whose own header names another well or field.

        A document is attached to a well by the workspace; the source it contains may disagree.  The
        disagreement is never resolved in favour of the link: promoting a report that says ``B-11``
        into well ``A-3`` because that is where the file happened to be filed would be a silent lie,
        and "attach it to the plausible one" is exactly the failure this check exists to prevent.
        Returns ``True`` when the promotion must stop.
        """
        known_names = {str(well.name).strip().casefold()}
        if well.well_identifier:
            known_names.add(str(well.well_identifier).strip().casefold())
        if source_well and source_well.strip().casefold() not in known_names:
            result.skipped.append(
                {
                    "reason": "WELL_SCOPE_CONFLICT",
                    "detail": (
                        f"the {domain} source names {source_well!r}, not the document's linked "
                        f"well {well.name!r}"
                    ),
                }
            )
            return True
        field = self.session.get(Field, str(well.field_id)) if well.field_id else None
        known_field_names = {str(field.name).strip().casefold()} if field is not None else set()
        if source_field and source_field.strip().casefold() not in known_field_names:
            result.skipped.append(
                {
                    "reason": "WELL_SCOPE_CONFLICT",
                    "detail": (
                        f"the {domain} source names field {source_field!r}, not the linked field "
                        f"{getattr(field, 'name', '')!r}"
                    ),
                }
            )
            return True
        return False

    @staticmethod
    def _row_scope_conflict(source_well: str, well: Well) -> bool:
        """Whether one *row* of a multi-well table names a well this document is not attached to."""
        if not source_well.strip():
            return False
        known = {str(well.name).strip().casefold()}
        if well.well_identifier:
            known.add(str(well.well_identifier).strip().casefold())
        return source_well.strip().casefold() not in known

    def _no_recognised_table(self, result: PromotionResult, detail: str) -> None:
        """The artefact satisfies the classification but not the contract's source shape.

        Reported as ``UNSUPPORTED`` rather than an error: the stored extraction, its evidence and its
        knowledge facts all remain available, and no authoritative row is written.  A prose BHA
        narrative and a component tally are the same classification and not the same contract.
        """
        result.outcome = PromotionOutcome.UNSUPPORTED.value
        result.skipped.append({"reason": "NO_RECOGNISED_TABLE", "detail": detail})

    def _higher_current_exists(
        self, model: type, *, document: Document, version: DocumentVersion, well: Well
    ) -> bool:
        """Whether a newer version of the same document already promoted a current row."""
        return bool(
            self.session.scalar(
                select(model.id)
                .join(
                    DocumentVersion,
                    DocumentVersion.id == model.document_version_id,
                    isouter=True,
                )
                .where(
                    model.document_id == document.id,
                    model.well_id == well.id,
                    model.is_current.is_(True),
                    DocumentVersion.version_number > version.version_number,
                )
                .limit(1)
            )
        )

    # -- BHA ------------------------------------------------------------------
    def _write_bha_component(
        self,
        *,
        report: BhaReport,
        entry: Any,
        table_id: str,
        document: Document,
        version: DocumentVersion,
        result: PromotionResult,
    ) -> None:
        identity = promotion_identity(
            version_id=version.id,
            kind="bha-component",
            table_id=table_id,
            row_index=0,
            well_id=report.well_id,
            extra=f"{report.bha_number or ''}|{entry.sequence}|{source_label_key(entry.source_label)}",
        )
        result.identities.add(identity)
        sizing = [
            (entry.od_text, entry.od_value),
            (entry.id_text, entry.id_value),
            (entry.length_text, entry.length_value),
        ]
        unparsed = [text for text, value in sizing if text and value is None]
        parsed_units = [
            unit
            for text, value, unit in (
                (entry.od_text, entry.od_value, entry.od_unit),
                (entry.id_text, entry.id_value, entry.id_unit),
                (entry.length_text, entry.length_value, entry.length_unit),
            )
            if value is not None and not unit
        ]
        # A sizing cell the source printed but this parser could not read is not silently NULL: it is
        # reported and the row is marked, so a reviewer can see that something was left behind.
        if unparsed:
            result.skipped.append(
                {
                    "reason": "INVALID_FIELD",
                    "detail": (
                        f"component {entry.sequence} ({entry.source_label}) has sizing text this "
                        f"contract cannot read: {', '.join(unparsed)}"
                    ),
                }
            )
            quality = "UNPARSEABLE"
        elif parsed_units:
            quality = "UNVERIFIED"
        else:
            quality = "VALID"
        content = {
            "sequence": entry.sequence,
            "source_label": entry.source_label,
            "component_type": entry.component_type,
            "stated_type": entry.stated_type,
            "manufacturer": entry.manufacturer,
            "model": entry.model,
            "serial_number": entry.serial_number,
            "od_value": entry.od_value,
            "od_unit": entry.od_unit,
            "od_text": entry.od_text,
            "id_value": entry.id_value,
            "id_unit": entry.id_unit,
            "id_text": entry.id_text,
            "length_value": entry.length_value,
            "length_unit": entry.length_unit,
            "length_text": entry.length_text,
            "quantity": entry.quantity,
            "quality": quality,
        }
        existing, outcome = self._confirm_row(
            BhaComponent, identity, content, "bha component", result
        )
        if existing is not None:
            result.bump("bha_component", outcome)
            return
        component_id = new_id("bhac")
        component_provenance = self._table_provenance(
            entry.table,
            document=document,
            version=version,
            row_index=entry.source_row_index,
            source_label=entry.source_label,
        )
        self.session.add(
            BhaComponent(
                id=component_id,
                bha_report_id=report.id,
                well_id=report.well_id,
                section_id=report.section_id,
                document_id=report.document_id,
                document_version_id=report.document_version_id,
                sequence=entry.sequence,
                source_label=entry.source_label,
                component_type=entry.component_type,
                stated_type=entry.stated_type,
                manufacturer=entry.manufacturer,
                model=entry.model,
                serial_number=entry.serial_number,
                od_value=entry.od_value,
                od_unit=entry.od_unit,
                od_text=entry.od_text,
                id_value=entry.id_value,
                id_unit=entry.id_unit,
                id_text=entry.id_text,
                length_value=entry.length_value,
                length_unit=entry.length_unit,
                length_text=entry.length_text,
                quantity=entry.quantity,
                quality=quality,
                record_state=RecordState.ACTUAL.value,
                status=ConfirmationStatus.CANDIDATE.value,
                origin=KnowledgeOrigin.DERIVED.value,
                created_by="promoter",
                provenance=component_provenance,
                identity_key=identity,
                is_current=True,
                attributes={
                    "table_id": table_id,
                    "source_row_index": entry.source_row_index,
                },
            )
        )
        self.session.flush()
        self.records.link(
            source_type="bha_report",
            source_id=report.id,
            relation=KnowledgeRelationType.BHA_HAS_COMPONENT.value,
            target_type="bha_component",
            target_id=component_id,
            provenance=component_provenance,
            note=f"component {entry.sequence} of the source tally, in the source's own order",
        )
        result.bump("bha_component", "created")

    def _promote_bha_report(
        self,
        *,
        payload: Mapping[str, Any],
        document: Document,
        version: DocumentVersion,
        result: PromotionResult,
        replace: bool = True,
    ) -> BhaReport | None:
        """Promote a recognised component tally into one run and its ordered components."""
        if not document.well_id:
            result.error = "NO_WELL"
            result.skipped.append(
                {"reason": "NO_WELL", "detail": f"{document.filename} is not linked to a well"}
            )
            return None
        well = self.session.get(Well, str(document.well_id))
        if well is None:
            result.error = "NO_WELL"
            result.skipped.append({"reason": "NO_WELL", "detail": "the linked well does not exist"})
            return None
        summary = bha_summary_entries(payload)
        components = bha_component_entries(payload)
        if not components:
            self._no_recognised_table(
                result,
                "no stored table has a component description column plus a sizing column, so there "
                "is no bottom hole assembly tally to promote",
            )
            return None
        if self._source_scope_conflict(
            well=well,
            source_well=next(
                (entry.source_value for entry in summary if entry.property_name == "well"), ""
            ),
            source_field=next(
                (entry.source_value for entry in summary if entry.property_name == "field"), ""
            ),
            result=result,
            domain="BHA report",
        ):
            result.error = "WELL_SCOPE_CONFLICT"
            return None
        table_id = source_table_key(components[0].table) or "bha"
        bha_number = next(
            (entry.source_value.strip() for entry in summary if entry.property_name == "bha_number"),
            "",
        )
        hole_entry = next(
            (entry for entry in summary if entry.property_name == "hole_size_in"), None
        )
        section_id, section_resolution = self._explicit_section(
            well=well,
            explicit_id=next(
                (entry.source_value.strip() for entry in summary if entry.property_name == "section_id"),
                "",
            ),
            explicit_name=next(
                (entry.source_value.strip() for entry in summary if entry.property_name == "section"),
                "",
            ),
            hole_size_text=hole_entry.source_value if hole_entry else "",
            result=result,
            domain="BHA",
        )
        date_entry = next(
            (entry for entry in summary if entry.property_name == "report_date"), None
        )
        report_date, report_date_text = self._mud_date(date_entry.source_value if date_entry else "")
        top_entry = next((entry for entry in summary if entry.property_name == "top_depth"), None)
        bottom_entry = next(
            (entry for entry in summary if entry.property_name == "bottom_depth"), None
        )
        description_entry = next(
            (entry for entry in summary if entry.property_name == "assembly_description"), None
        )
        higher_current = self._higher_current_exists(
            BhaReport, document=document, version=version, well=well
        )
        self._supersede_source_versions(
            model=BhaReport, document=document, version=version, well=well
        )
        identity = promotion_identity(
            version_id=version.id,
            kind="bha-report",
            table_id=table_id,
            row_index=0,
            well_id=well.id,
            extra=bha_number,
        )
        result.identities.add(identity)
        parent_provenance = self._table_provenance(
            components[0].table, document=document, version=version
        )
        content = {
            "well_id": well.id,
            "section_id": section_id,
            "bha_number": bha_number or None,
            "report_date": report_date,
            "report_date_text": report_date_text or None,
            "top_depth_value": mud_numeric(top_entry.source_value) if top_entry else None,
            "top_depth_unit": top_entry.source_unit if top_entry else "",
            "bottom_depth_value": mud_numeric(bottom_entry.source_value) if bottom_entry else None,
            "bottom_depth_unit": bottom_entry.source_unit if bottom_entry else "",
            "component_count": len(components),
            "section_resolution": section_resolution,
        }
        existing, outcome = self._confirm_row(BhaReport, identity, content, "bha report", result)
        if existing is not None:
            report = existing
            result.bump("bha_report", outcome)
        else:
            report = BhaReport(
                id=new_id("bha"),
                well_id=well.id,
                section_id=section_id,
                document_id=document.id,
                document_version_id=version.id,
                bha_number=bha_number or None,
                report_date=report_date,
                report_date_text=report_date_text or None,
                top_depth_value=mud_numeric(top_entry.source_value) if top_entry else None,
                top_depth_unit=top_entry.source_unit if top_entry else "",
                bottom_depth_value=mud_numeric(bottom_entry.source_value) if bottom_entry else None,
                bottom_depth_unit=bottom_entry.source_unit if bottom_entry else "",
                assembly_description=(
                    description_entry.source_value if description_entry else None
                ),
                component_count=len(components),
                section_resolution=section_resolution,
                record_state=RecordState.ACTUAL.value,
                status=ConfirmationStatus.CANDIDATE.value,
                document_status=str(version.status or document.status or ""),
                origin=KnowledgeOrigin.DERIVED.value,
                created_by="promoter",
                provenance=parent_provenance,
                identity_key=identity,
                is_current=not higher_current,
                attributes={
                    "component_table_id": table_id,
                    "source_well_name": next(
                        (e.source_value for e in summary if e.property_name == "well"), ""
                    ),
                    "source_field_name": next(
                        (e.source_value for e in summary if e.property_name == "field"), ""
                    ),
                    "component_types": sorted(
                        {entry.component_type for entry in components if entry.component_type}
                    ),
                    "unclassified_components": sum(
                        1 for entry in components if not entry.component_type
                    ),
                },
            )
            if higher_current:
                report.status = "SUPERSEDED"
            self.session.add(report)
            self.session.flush()
            result.bump("bha_report", "created")
        self.records.link(
            source_type="well",
            source_id=well.id,
            relation=KnowledgeRelationType.WELL_HAS_BHA.value,
            target_type="bha_report",
            target_id=report.id,
            provenance=parent_provenance,
            note="source-version bottom hole assembly promoted from stored extraction",
        )
        if section_id:
            self.records.link(
                source_type="well_section",
                source_id=section_id,
                relation=KnowledgeRelationType.SECTION_HAS_BHA.value,
                target_type="bha_report",
                target_id=report.id,
                provenance=parent_provenance,
                note="section explicitly matched by stored BHA attributes",
            )
        for entry in components:
            self._write_bha_component(
                report=report,
                entry=entry,
                table_id=source_table_key(entry.table) or table_id,
                document=document,
                version=version,
                result=result,
            )
        if replace and bool(result.identities):
            removed = self._delete_domain_orphans(
                BhaComponent, version_id=version.id, kept=result.identities
            )
            removed += self._delete_domain_orphans(
                BhaReport, version_id=version.id, kept=result.identities
            )
            if removed:
                result.counts["removed"] = {"created": removed, "unchanged": 0, "conflict": 0}
        return report

    # -- bit record -----------------------------------------------------------
    def _linked_bha(self, *, well: Well, bha_number: str, result: PromotionResult) -> str | None:
        """The one current BHA of this well the source's BHA number names, or ``None``.

        Only a promoted, current assembly of the *same well* is ever linked.  A number that matches
        nothing - or that matches two runs the source numbered identically - leaves the link NULL and
        is reported, because a guessed link between a bit and the wrong assembly is worse than an
        honest gap.
        """
        if not bha_number.strip():
            return None
        matches = list(
            self.session.execute(
                select(BhaReport)
                .where(
                    BhaReport.well_id == well.id,
                    BhaReport.is_current.is_(True),
                    BhaReport.bha_number.is_not(None),
                )
                .order_by(BhaReport.bha_number, BhaReport.id)
            ).scalars()
        )
        selected = [
            row for row in matches if str(row.bha_number or "").strip() == bha_number.strip()
        ]
        if len(selected) == 1:
            return str(selected[0].id)
        if len(selected) > 1:
            result.skipped.append(
                {
                    "reason": "AMBIGUOUS_BHA_LINK",
                    "detail": (
                        f"more than one current BHA of this well is numbered {bha_number!r}; "
                        "the bit run keeps no BHA link"
                    ),
                }
            )
        return None

    def _write_bit_record(
        self,
        *,
        entry: Any,
        well: Well,
        section_id: str | None,
        section_resolution: str,
        document: Document,
        version: DocumentVersion,
        result: PromotionResult,
        higher_current: bool,
    ) -> BitRecord | None:
        identity = promotion_identity(
            version_id=version.id,
            kind="bit-record",
            table_id=source_table_key(entry.table),
            row_index=0,
            well_id=well.id,
            extra=f"{entry.bit_number.strip()}|{entry.run_number.strip()}",
        )
        result.identities.add(identity)
        unparsed = [
            text
            for text, value in (
                (entry.size_text, entry.size_value),
                (entry.depth_in_text, entry.depth_in_value),
                (entry.depth_out_text, entry.depth_out_value),
                (entry.footage_text, entry.footage_value),
            )
            if text and value is None
        ]
        if unparsed:
            result.skipped.append(
                {
                    "reason": "INVALID_FIELD",
                    "detail": (
                        f"bit {entry.bit_number} has numbers this contract cannot read: "
                        f"{', '.join(unparsed)}"
                    ),
                }
            )
        run_date, run_date_text = self._mud_date(entry.run_date_text)
        bha_report_id = self._linked_bha(
            well=well, bha_number=entry.bha_number, result=result
        )
        content = {
            "well_id": well.id,
            "section_id": section_id,
            "bha_report_id": bha_report_id,
            "bit_number": entry.bit_number.strip(),
            "run_number": entry.run_number.strip() or None,
            "manufacturer": entry.manufacturer,
            "model": entry.model,
            "bit_type": entry.bit_type,
            "iadc_code": entry.iadc_code,
            "serial_number": entry.serial_number,
            "size_value": entry.size_value,
            "size_unit": entry.size_unit,
            "size_text": entry.size_text,
            "depth_in_value": entry.depth_in_value,
            "depth_in_unit": entry.depth_in_unit,
            "depth_out_value": entry.depth_out_value,
            "depth_out_unit": entry.depth_out_unit,
            "footage_value": entry.footage_value,
            "footage_unit": entry.footage_unit,
            "rotating_hours": entry.rotating_hours,
            "drilling_hours": entry.drilling_hours,
            "pull_reason": entry.pull_reason,
            "dull_grade": entry.dull_grade,
            "nozzle_count": entry.nozzle_count,
            "nozzle_size_text": entry.nozzle_size_text,
            "run_date": run_date,
            "run_date_text": run_date_text or None,
            "section_resolution": section_resolution,
        }
        provenance = self._table_provenance(
            entry.table,
            document=document,
            version=version,
            row_index=entry.source_row_index,
            source_label=entry.bit_number,
        )
        existing, outcome = self._confirm_row(BitRecord, identity, content, "bit record", result)
        if existing is not None:
            result.bump("bit_record", outcome)
            return existing
        record = BitRecord(
            id=new_id("bit"),
            well_id=well.id,
            section_id=section_id,
            document_id=document.id,
            document_version_id=version.id,
            bha_report_id=bha_report_id,
            bit_number=entry.bit_number.strip(),
            run_number=entry.run_number.strip() or None,
            manufacturer=entry.manufacturer,
            model=entry.model,
            bit_type=entry.bit_type,
            iadc_code=entry.iadc_code,
            serial_number=entry.serial_number,
            size_value=entry.size_value,
            size_unit=entry.size_unit,
            size_text=entry.size_text,
            depth_in_value=entry.depth_in_value,
            depth_in_unit=entry.depth_in_unit,
            depth_out_value=entry.depth_out_value,
            depth_out_unit=entry.depth_out_unit,
            footage_value=entry.footage_value,
            footage_unit=entry.footage_unit,
            rotating_hours=entry.rotating_hours,
            drilling_hours=entry.drilling_hours,
            pull_reason=entry.pull_reason,
            dull_grade=entry.dull_grade,
            nozzle_count=entry.nozzle_count,
            nozzle_size_text=entry.nozzle_size_text,
            run_date=run_date,
            run_date_text=run_date_text or None,
            section_resolution=section_resolution,
            record_state=RecordState.ACTUAL.value,
            status=ConfirmationStatus.CANDIDATE.value,
            document_status=str(version.status or document.status or ""),
            origin=KnowledgeOrigin.DERIVED.value,
            created_by="promoter",
            provenance=provenance,
            identity_key=identity,
            is_current=not higher_current,
            attributes={
                "source_bha_number": entry.bha_number,
                "source_well_name": entry.well_name,
                "source_row_index": entry.source_row_index,
            },
        )
        if higher_current:
            record.status = "SUPERSEDED"
        self.session.add(record)
        self.session.flush()
        result.bump("bit_record", "created")
        self.records.link(
            source_type="well",
            source_id=well.id,
            relation=KnowledgeRelationType.WELL_HAS_BIT_RUN.value,
            target_type="bit_record",
            target_id=record.id,
            provenance=provenance,
            note="source-version bit run promoted from stored extraction",
        )
        if section_id:
            self.records.link(
                source_type="well_section",
                source_id=section_id,
                relation=KnowledgeRelationType.SECTION_HAS_BIT.value,
                target_type="bit_record",
                target_id=record.id,
                provenance=provenance,
                note="section explicitly matched by stored bit-record attributes",
            )
        if bha_report_id:
            self.records.link(
                source_type="bha_report",
                source_id=bha_report_id,
                relation=KnowledgeRelationType.BHA_HAS_BIT.value,
                target_type="bit_record",
                target_id=record.id,
                provenance=provenance,
                note="the bit record's own BHA number matched one current assembly of this well",
            )
        return record

    def _promote_bit_record(
        self,
        *,
        payload: Mapping[str, Any],
        document: Document,
        version: DocumentVersion,
        result: PromotionResult,
        replace: bool = True,
    ) -> BitRecord | None:
        """Promote a recognised bit tally, one run per row, preserving every earlier run."""
        if not document.well_id:
            result.error = "NO_WELL"
            result.skipped.append(
                {"reason": "NO_WELL", "detail": f"{document.filename} is not linked to a well"}
            )
            return None
        well = self.session.get(Well, str(document.well_id))
        if well is None:
            result.error = "NO_WELL"
            result.skipped.append({"reason": "NO_WELL", "detail": "the linked well does not exist"})
            return None
        entries = bit_run_entries(payload)
        if not entries:
            self._no_recognised_table(
                result,
                "no stored table has a bit number column plus a bit measurement column, so there "
                "is no bit record to promote",
            )
            return None
        higher_current = self._higher_current_exists(
            BitRecord, document=document, version=version, well=well
        )
        self._supersede_source_versions(
            model=BitRecord, document=document, version=version, well=well
        )
        written: BitRecord | None = None
        for entry in entries:
            # A tally may cover several wells.  A row naming another well is skipped and reported; it
            # is never re-attached to the well this document happens to be filed under.
            if self._row_scope_conflict(entry.well_name, well):
                result.skipped.append(
                    {
                        "reason": "WELL_SCOPE_CONFLICT",
                        "detail": (
                            f"bit {entry.bit_number} names well {entry.well_name!r}, not this "
                            f"document's well {well.name!r}; row not promoted"
                        ),
                    }
                )
                continue
            section_id, section_resolution = self._explicit_section(
                well=well,
                explicit_id="",
                explicit_name=entry.section_text,
                hole_size_text="",
                result=result,
                domain="bit record",
            )
            record = self._write_bit_record(
                entry=entry,
                well=well,
                section_id=section_id,
                section_resolution=section_resolution,
                document=document,
                version=version,
                result=result,
                higher_current=higher_current,
            )
            if record is not None and written is None:
                written = record
        if replace and bool(result.identities):
            removed = self._delete_domain_orphans(
                BitRecord, version_id=version.id, kept=result.identities
            )
            if removed:
                result.counts["removed"] = {"created": removed, "unchanged": 0, "conflict": 0}
        return written

    # -- directional survey ---------------------------------------------------
    def _write_survey_station(
        self,
        *,
        run: SurveyRun,
        entry: Any,
        table_id: str,
        station_key: str,
        document: Document,
        version: DocumentVersion,
        result: PromotionResult,
    ) -> None:
        identity = promotion_identity(
            version_id=version.id,
            kind="survey-station",
            table_id=table_id,
            row_index=0,
            well_id=run.well_id,
            extra=f"{run.run_label}|{station_key}",
        )
        result.identities.add(identity)
        unverified = [
            name
            for name, value, unit in (
                ("measured depth", entry.md_value, entry.md_unit),
                ("inclination", entry.inclination_value, entry.inclination_unit),
                ("azimuth", entry.azimuth_value, entry.azimuth_unit),
            )
            if value is not None and not unit
        ]
        content = {
            "sequence": entry.sequence,
            "station_number_text": entry.station_number_text,
            "md_value": entry.md_value,
            "md_unit": entry.md_unit,
            "md_text": entry.md_text,
            "inclination_value": entry.inclination_value,
            "inclination_unit": entry.inclination_unit,
            "inclination_text": entry.inclination_text,
            "azimuth_value": entry.azimuth_value,
            "azimuth_unit": entry.azimuth_unit,
            "azimuth_text": entry.azimuth_text,
            "toolface_value": entry.toolface_value,
            "toolface_unit": entry.toolface_unit,
            "toolface_text": entry.toolface_text,
            "tvd_value": entry.tvd_value,
            "tvd_unit": entry.tvd_unit,
            "tvd_text": entry.tvd_text,
            "northing_value": entry.northing_value,
            "northing_unit": entry.northing_unit,
            "northing_text": entry.northing_text,
            "easting_value": entry.easting_value,
            "easting_unit": entry.easting_unit,
            "easting_text": entry.easting_text,
            "dls_value": entry.dls_value,
            "dls_unit": entry.dls_unit,
            "dls_text": entry.dls_text,
            "quality": "UNVERIFIED" if unverified else "VALID",
        }
        if unverified:
            result.skipped.append(
                {
                    "reason": "MISSING_UNIT",
                    "detail": (
                        f"survey station {station_key} has no stated unit for "
                        f"{', '.join(unverified)}; the value is kept unverified and unconverted"
                    ),
                }
            )
        existing, outcome = self._confirm_row(
            SurveyStation, identity, content, "survey station", result
        )
        if existing is not None:
            result.bump("survey_station", outcome)
            return
        station = SurveyStation(
            id=new_id("svy"),
            survey_run_id=run.id,
            well_id=run.well_id,
            section_id=run.section_id,
            document_id=run.document_id,
            document_version_id=run.document_version_id,
            sequence=entry.sequence,
            station_number_text=entry.station_number_text,
            md_value=entry.md_value,
            md_unit=entry.md_unit,
            md_text=entry.md_text,
            inclination_value=entry.inclination_value,
            inclination_unit=entry.inclination_unit,
            inclination_text=entry.inclination_text,
            azimuth_value=entry.azimuth_value,
            azimuth_unit=entry.azimuth_unit,
            azimuth_text=entry.azimuth_text,
            toolface_value=entry.toolface_value,
            toolface_unit=entry.toolface_unit,
            toolface_text=entry.toolface_text,
            tvd_value=entry.tvd_value,
            tvd_unit=entry.tvd_unit,
            tvd_text=entry.tvd_text,
            northing_value=entry.northing_value,
            northing_unit=entry.northing_unit,
            northing_text=entry.northing_text,
            easting_value=entry.easting_value,
            easting_unit=entry.easting_unit,
            easting_text=entry.easting_text,
            dls_value=entry.dls_value,
            dls_unit=entry.dls_unit,
            dls_text=entry.dls_text,
            quality="UNVERIFIED" if unverified else "VALID",
            record_state=RecordState.ACTUAL.value,
            status=ConfirmationStatus.CANDIDATE.value,
            origin=KnowledgeOrigin.DERIVED.value,
            created_by="promoter",
            provenance=self._table_provenance(
                entry.table,
                document=document,
                version=version,
                row_index=entry.source_row_index,
                source_label=entry.station_number_text,
            ),
            identity_key=identity,
            is_current=True,
            attributes={
                "table_id": table_id,
                "source_row_index": entry.source_row_index,
                "station_identity": run.station_identity,
            },
        )
        self.session.add(station)
        self.session.flush()
        result.bump("survey_station", "created")
        self.records.link(
            source_type="survey_run",
            source_id=run.id,
            relation=KnowledgeRelationType.SURVEY_HAS_STATION.value,
            target_type="survey_station",
            target_id=station.id,
            provenance=[],
        )

    def _promote_directional_survey(
        self,
        *,
        payload: Mapping[str, Any],
        document: Document,
        version: DocumentVersion,
        result: PromotionResult,
        replace: bool = True,
    ) -> SurveyRun | None:
        """Promote recognised survey stations, keeping every source-labelled set separate."""
        if not document.well_id:
            result.error = "NO_WELL"
            result.skipped.append(
                {"reason": "NO_WELL", "detail": f"{document.filename} is not linked to a well"}
            )
            return None
        well = self.session.get(Well, str(document.well_id))
        if well is None:
            result.error = "NO_WELL"
            result.skipped.append({"reason": "NO_WELL", "detail": "the linked well does not exist"})
            return None
        summary = survey_summary_entries(payload)
        stations = survey_station_entries(payload)
        if not stations:
            self._no_recognised_table(
                result,
                "no stored table has measured depth, inclination and azimuth in three distinct "
                "columns, so there is no directional survey to promote",
            )
            return None
        if self._source_scope_conflict(
            well=well,
            source_well=next(
                (entry.source_value for entry in summary if entry.property_name == "well"), ""
            ),
            source_field=next(
                (entry.source_value for entry in summary if entry.property_name == "field"), ""
            ),
            result=result,
            domain="directional survey",
        ):
            result.error = "WELL_SCOPE_CONFLICT"
            return None
        higher_current = self._higher_current_exists(
            SurveyRun, document=document, version=version, well=well
        )
        self._supersede_source_versions(
            model=SurveyRun, document=document, version=version, well=well
        )
        date_entry = next(
            (entry for entry in summary if entry.property_name == "survey_date"), None
        )
        survey_date, survey_date_text = self._mud_date(
            date_entry.source_value if date_entry else ""
        )
        tool_entry = next(
            (entry for entry in summary if entry.property_name == "survey_tool"), None
        )
        label_entry = next(
            (entry for entry in summary if entry.property_name == "run_label"), None
        )
        # Sets stay separate: the source's own run/set column groups them, and where it is absent the
        # table is the set.  Two unlabelled sets in one sheet remain one run, exactly as the source
        # presented them; that is a documented limit of the contract, not a silent merge.
        groups: dict[tuple[str, str], list[Any]] = {}
        for entry in stations:
            table_id = source_table_key(entry.table)
            key = (table_id, entry.run_label.strip() or (label_entry.source_value.strip() if label_entry else ""))
            groups.setdefault(key, []).append(entry)
        first_run: SurveyRun | None = None
        for (table_id, run_label), group in groups.items():
            numbers = [entry.station_number_text.strip() for entry in group]
            if all(numbers) and len(set(numbers)) == len(numbers):
                station_identity = "NUMBERED"
            elif all(numbers) or any(numbers):
                # Repeated numbers, or some rows numbered and some not: the source's numbering does
                # not identify a station uniquely, so identity falls back to source position and the
                # ambiguity is reported rather than resolved by choosing one of the candidates.
                station_identity = "AMBIGUOUS"
                result.skipped.append(
                    {
                        "reason": "AMBIGUOUS_STATIONS",
                        "detail": (
                            f"survey set {run_label or table_id} does not number its stations "
                            "uniquely; station identity falls back to source position"
                        ),
                    }
                )
            else:
                station_identity = "UNNUMBERED"
            section_names = sorted(
                {entry.section_text.strip() for entry in group if entry.section_text.strip()}
            )
            section_name = section_names[0] if len(section_names) == 1 else ""
            if len(section_names) > 1:
                result.skipped.append(
                    {
                        "reason": "AMBIGUOUS_SECTIONS",
                        "detail": (
                            "one survey set names more than one section "
                            f"({', '.join(section_names)}); no section is attached"
                        ),
                    }
                )
            hole_entry = next(
                (entry for entry in summary if entry.property_name == "hole_size_in"), None
            )
            section_id, section_resolution = self._explicit_section(
                well=well,
                explicit_id=next(
                    (
                        entry.source_value.strip()
                        for entry in summary
                        if entry.property_name == "section_id"
                    ),
                    "",
                ),
                explicit_name=(
                    section_name
                    or next(
                        (
                            entry.source_value.strip()
                            for entry in summary
                            if entry.property_name == "section"
                        ),
                        "",
                    )
                ),
                hole_size_text=hole_entry.source_value if hole_entry else "",
                result=result,
                domain="directional survey",
            )
            depths = [entry.md_value for entry in group if entry.md_value is not None]
            units = sorted({entry.md_unit for entry in group if entry.md_unit})
            identity = promotion_identity(
                version_id=version.id,
                kind="survey-run",
                table_id=table_id,
                row_index=0,
                well_id=well.id,
                extra=run_label,
            )
            result.identities.add(identity)
            content = {
                "well_id": well.id,
                "section_id": section_id,
                "run_label": run_label,
                "survey_tool": tool_entry.source_value if tool_entry else "",
                "survey_date": survey_date,
                "survey_date_text": survey_date_text or None,
                "station_count": len(group),
                "min_md_value": min(depths) if depths else None,
                "max_md_value": max(depths) if depths else None,
                "md_unit": units[0] if len(units) == 1 else "",
                "station_identity": station_identity,
                "section_resolution": section_resolution,
            }
            provenance = self._table_provenance(
                group[0].table, document=document, version=version
            )
            existing, outcome = self._confirm_row(
                SurveyRun, identity, content, "survey run", result
            )
            if existing is not None:
                run = existing
                result.bump("survey_run", outcome)
            else:
                run = SurveyRun(
                    id=new_id("sur"),
                    well_id=well.id,
                    section_id=section_id,
                    document_id=document.id,
                    document_version_id=version.id,
                    run_label=run_label,
                    survey_tool=tool_entry.source_value if tool_entry else "",
                    survey_date=survey_date,
                    survey_date_text=survey_date_text or None,
                    station_count=len(group),
                    min_md_value=min(depths) if depths else None,
                    max_md_value=max(depths) if depths else None,
                    md_unit=units[0] if len(units) == 1 else "",
                    station_identity=station_identity,
                    section_resolution=section_resolution,
                    record_state=RecordState.ACTUAL.value,
                    status=ConfirmationStatus.CANDIDATE.value,
                    document_status=str(version.status or document.status or ""),
                    origin=KnowledgeOrigin.DERIVED.value,
                    created_by="promoter",
                    provenance=provenance,
                    identity_key=identity,
                    is_current=not higher_current,
                    attributes={
                        "station_table_id": table_id,
                        "run_identity": "LABEL" if run_label else "TABLE",
                        "source_well_name": next(
                            (e.source_value for e in summary if e.property_name == "well"), ""
                        ),
                        "source_field_name": next(
                            (e.source_value for e in summary if e.property_name == "field"), ""
                        ),
                    },
                )
                if higher_current:
                    run.status = "SUPERSEDED"
                self.session.add(run)
                self.session.flush()
                result.bump("survey_run", "created")
            if first_run is None:
                first_run = run
            self.records.link(
                source_type="well",
                source_id=well.id,
                relation=KnowledgeRelationType.WELL_HAS_SURVEY.value,
                target_type="survey_run",
                target_id=run.id,
                provenance=provenance,
                note="source-version directional survey promoted from stored extraction",
            )
            if section_id:
                self.records.link(
                    source_type="well_section",
                    source_id=section_id,
                    relation=KnowledgeRelationType.SECTION_HAS_SURVEY.value,
                    target_type="survey_run",
                    target_id=run.id,
                    provenance=provenance,
                    note="section explicitly matched by stored survey attributes",
                )
            for entry in group:
                station_key = (
                    entry.station_number_text.strip()
                    if station_identity == "NUMBERED"
                    else f"row:{entry.sequence}"
                )
                self._write_survey_station(
                    run=run,
                    entry=entry,
                    table_id=table_id,
                    station_key=station_key,
                    document=document,
                    version=version,
                    result=result,
                )
        if replace and bool(result.identities):
            removed = self._delete_domain_orphans(
                SurveyStation, version_id=version.id, kept=result.identities
            )
            removed += self._delete_domain_orphans(
                SurveyRun, version_id=version.id, kept=result.identities
            )
            if removed:
                result.counts["removed"] = {"created": removed, "unchanged": 0, "conflict": 0}
        return first_run

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
    def _row_provenance(
        provenance: Sequence[Mapping[str, Any]], row_number: int
    ) -> list[dict[str, Any]]:
        """Narrow table provenance to the source row without inventing a cell locator.

        CSV/text rows have a real line number; Excel/DOCX table locators can carry a row number.  For
        other locator kinds the table locator is retained, because pretending that a page or an
        unstructured excerpt identifies one row would be less trustworthy than a bounded table cite.
        """
        narrowed: list[dict[str, Any]] = []
        for item in provenance:
            copy = dict(item)
            locator = item.get("locator")
            if isinstance(locator, Mapping):
                locator_copy = dict(locator)
                kind = str(locator_copy.get("locator_kind") or locator_copy.get("kind") or "")
                if kind == "text":
                    locator_copy["line_start"] = row_number
                    locator_copy["line_end"] = row_number
                elif kind in {"excel", "docx"}:
                    locator_copy["row"] = row_number
                copy["locator"] = locator_copy
            narrowed.append(copy)
        return narrowed

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
                        provenance=self._row_provenance(provenance, offset + 2),
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
            # Keep the raw activity duration even when the row is productive and therefore has no NPT
            # child.  The operational schema has no generic duration column; this source-owned value
            # is not converted or used as an executable calculation.
            "attributes": {
                "promoted_from": {"table_id": table_id, "sheet": sheet, "row": offset + 2},
                "source_duration": {"text": hours_text},
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
