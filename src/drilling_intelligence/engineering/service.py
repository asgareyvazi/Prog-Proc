"""The engineering application service: promoted records in, a citable engineering number out.

``calculation`` and ``calculation_input`` have had a write method since the engineering phase and no
production caller: every invocation of
:meth:`~drilling_intelligence.engineering.repository.EngineeringRepository.record_calculation` lived in a
test, so the table that is supposed to answer *"what backs this number"* could only ever hold numbers a
test had put there.  This module is the missing half - the production path that turns records the platform
already promoted from real documents into a stored engineering result.

**Why lost-time roll-up, and not plan-versus-actual.**  The obvious candidate was the variance
``EngineeringRepository.plan_actual_summary`` already computes, and it was rejected on the data model
rather than on taste.  A variance needs both halves to be evidence-bearing, and they are not:
``well_section`` - the actual half - has no ``provenance``, ``document_id`` or ``document_version_id``
column at all, and the planned half's ``program_target`` carries a provenance blob but no document-version
foreign key.  A calculation built on those two sides could not say which document version it depended on,
which makes the stale-dependency half of ADR-0016 unanswerable: superseding the source would leave the
change-impact report claiming the result was still current.  ``npt_record`` is the one quantity in this
database that arrives through the real path - parse, extract, promote - carrying ``document_version_id``
and its own provenance per row, so it is the only one whose dependency edges can honestly go stale.

**What is computed.**  Addition, and nothing else.  The hours a well lost are the sum of the hours its
promoted NPT rows state; there is no model, no rate, no distribution and no estimate of a row that never
said how long it took.  That last point is the rule the rest of this platform already follows
(:meth:`~drilling_intelligence.operations.repository.OperationsRepository.npt_totals`): a row with no
duration is *counted and reported*, never read as zero, because "this well lost 59.25 hours" and "two of
its seven rows never said" are different claims and the second one must survive.

**What is deliberately absent.**  No recomputation and no invalidation.  Superseding a source document
does not rewrite, delete or re-run a stored result: the historical row keeps its inputs and its citations,
:meth:`~drilling_intelligence.engineering.repository.EngineeringRepository.calculation_impact` starts
reporting its inputs ``STALE``, and whether to re-run is an engineering decision with a method and a
reviewer (ADR-0012) that a person makes by calling this service again.  Re-running is safe precisely
because identity is content-addressed: the same rows and the same method return the stored calculation
instead of a second copy of it.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from ..core.enums import CalculationStatus, KnowledgeOrigin, RecordState
from ..core.errors import ValidationError
from ..core.hashing import sha256_text
from ..core.ids import SubjectKey, normalize_property, normalize_state
from ..core.units import UnitError, resolve_unit
from ..database.models import Calculation, Document, DocumentVersion, NptRecord, Well
from ..operations.repository import OperationsRepository
from .repository import EngineeringRepository

__all__ = [
    "NPT_ROLLUP_METHOD_ID",
    "NPT_ROLLUP_METHOD_VERSION",
    "NPT_ROLLUP_PROPERTY",
    "NPT_ROLLUP_UNIT",
    "EngineeringService",
]

#: The method this service applies, named so a stored row can be traced back to the code that wrote it.
NPT_ROLLUP_METHOD_ID = "npt.hours_rollup"

#: Bumped when the *arithmetic* changes, never when unrelated code moves.  It is part of the identity, so
#: a new version is a new result rather than a silent edit of an old one.
NPT_ROLLUP_METHOD_VERSION = "1"

#: The property the result is about, in the vocabulary ``plan_actual_summary`` already uses for this
#: metric, so the two describe the same quantity by the same name.
NPT_ROLLUP_PROPERTY = "npt_hours"

#: ``npt_record.duration_hours`` is hours by definition of the column - this is the column's meaning
#: being stated, not a unit being assumed for a bare number.
NPT_ROLLUP_UNIT = "h"

#: How many NPT rows one roll-up will read.  A truncated read would produce a total that is wrong while
#: looking authoritative, so hitting this ceiling is an error rather than a smaller number (see
#: :meth:`EngineeringService.record_npt_rollup`).
_MAX_RECORDS = 5000

#: What this service records is derived from stored rows, not typed by a person, and ``record_calculation``
#: requires such a row to cite its evidence.
_ORIGIN = KnowledgeOrigin.DERIVED.value

#: Fixed as an audit/trigger label.  The repository deliberately excludes it, and the actor, from
#: semantic content identity so a CLI, UI or scheduled retry of the same evidence cannot create a duplicate.
_TRIGGERED_BY = "engineering.npt_rollup"


class EngineeringService:
    """Engineering results for one workspace, computed from promoted records and stored with evidence.

    Holds a *database* rather than a session, like the knowledge and operational services: every method
    takes ``session=None`` and either borrows the caller's transaction or opens and commits its own.  The
    repositories it drives never commit - that rule is what lets a caller compose a promotion and a
    roll-up into one atomic unit instead of discovering a commit in the middle of one.
    """

    def __init__(self, *, database: Any, settings: Any = None) -> None:
        if database is None:
            raise ValueError(
                "the engineering service needs the registry database; there is no fallback to files"
            )
        self.database = database
        self.settings = settings

    @classmethod
    def for_workspace(cls, workspace: Any) -> EngineeringService:
        """Wire the service to an opened workspace (the CLI uses this)."""
        return cls(database=workspace.database, settings=getattr(workspace, "settings", None))

    # -- sessions -------------------------------------------------------------
    @contextmanager
    def _session(self, session: Session | None) -> Iterator[Session]:
        """Borrow the caller's session, or own the transaction.  The operational service's rule,
        kept identical here so composing a promotion and a roll-up gives one transaction."""
        if session is not None:
            yield session
            return
        with self.database.session() as own:
            try:
                yield own
                own.commit()
            except Exception:
                own.rollback()
                raise

    # -- lost-time roll-up ----------------------------------------------------
    def record_npt_rollup(
        self,
        *,
        well_id: str,
        supersedes_id: str = "",
        created_by: str = "system",
        session: Session | None = None,
    ) -> tuple[Calculation, bool]:
        """Sum a well's promoted NPT hours into a stored, cited engineering record.

        Returns the calculation and whether it was created, so a caller that runs this twice can tell a
        fresh result from the stored one it just matched.

        The inputs are the NPT rows themselves - one ``calculation_input`` each, carrying that row's own
        ``document_version_id`` - which is what makes the dependency edge auditable per source rather than
        per total: one revised report among five marks one input stale, and the report says which.

        ``supersedes_id`` is never filled in automatically.  A newer result supersedes an older one when a
        person says it does; this service does not decide that a stored number is obsolete.
        """
        identifier = str(well_id or "").strip()
        if not identifier:
            raise ValidationError(
                "a lost-time roll-up has to name the well it is about",
                hint="pass well_id=<the well whose NPT rows to sum>",
            )
        # The unit is the column's meaning, but the registry still has to recognise it: an unresolvable
        # unit would put a dimensionless number in a table whose whole purpose is comparability.
        try:
            unit = resolve_unit(NPT_ROLLUP_UNIT)
        except (UnitError, ValueError, KeyError) as error:  # pragma: no cover - registry regression
            raise ValidationError(
                f"the unit registry does not recognise {NPT_ROLLUP_UNIT!r}",
                hint="a lost-time total has to carry a unit the platform can compare",
            ) from error
        if unit.dimension.value != "TIME":
            raise ValidationError(
                f"{NPT_ROLLUP_UNIT!r} resolves to {unit.dimension.value}, not TIME",
                hint="a duration total must be a time; refusing to store it under another dimension",
            )

        with self._session(session) as active:
            well = active.get(Well, identifier)
            if well is None:
                raise ValidationError(
                    "no such well, so there is nothing to roll up",
                    hint="the subject of an engineering record has to resolve to a row",
                    well_id=identifier,
                )
            records = OperationsRepository(active).list_npt(
                well_id=identifier, limit=_MAX_RECORDS + 1
            )
            if len(records) > _MAX_RECORDS:
                raise ValidationError(
                    "too many NPT rows to roll up in one record",
                    hint=(
                        "a truncated read would store a total that is quietly too small; narrow the"
                        " scope or raise the ceiling deliberately"
                    ),
                    well_id=identifier,
                    limit=_MAX_RECORDS,
                )

            subject = SubjectKey(
                well_id=identifier,
                property_name=normalize_property(NPT_ROLLUP_PROPERTY),
                record_state=normalize_state(RecordState.ACTUAL.value),
            ).render()

            inputs: dict[str, Any] = {}
            citations: dict[str, dict[str, Any]] = {}
            total = 0.0
            quantified = 0
            without_duration = 0
            without_evidence = 0
            for record in records:
                record_id = str(getattr(record, "id", "") or "").strip()
                row_well_id = str(getattr(record, "well_id", "") or "").strip()
                if not record_id:
                    raise ValidationError(
                        "an NPT input has no durable record id",
                        hint="promote the source row again before rolling it up",
                        well_id=identifier,
                    )
                if row_well_id and row_well_id != identifier:
                    raise ValidationError(
                        "an NPT input belongs to a different well",
                        hint="a roll-up may only consume rows from its named well",
                        well_id=identifier,
                        npt_record_id=record_id,
                        input_well_id=row_well_id,
                    )
                status = str(
                    getattr(getattr(record, "status", ""), "value", getattr(record, "status", ""))
                    or ""
                )
                if status.strip().upper() in {"REJECTED", "SUPERSEDED", "RETIRED"}:
                    raise ValidationError(
                        "a rejected or superseded NPT row cannot be an executable input",
                        hint="remove the rejected row from the authoritative scope or promote its replacement",
                        well_id=identifier,
                        npt_record_id=record_id,
                        status=status,
                    )
                record_state = (
                    str(
                        getattr(
                            getattr(record, "record_state", ""),
                            "value",
                            getattr(record, "record_state", ""),
                        )
                        or ""
                    )
                    .strip()
                    .upper()
                )
                if record_state and record_state != RecordState.ACTUAL.value:
                    raise ValidationError(
                        "an NPT input is not an ACTUAL record",
                        hint="the NPT roll-up only accepts actual promoted source rows",
                        well_id=identifier,
                        npt_record_id=record_id,
                        record_state=record_state,
                    )

                hours = getattr(record, "duration_hours", None)
                if hours is None:
                    # Counted, never imputed: "nothing was recorded" is not "nothing was lost".
                    without_duration += 1
                    continue
                if isinstance(hours, bool):
                    raise ValidationError(
                        "an NPT row states a malformed duration",
                        hint="duration_hours must be a finite non-negative number, not a boolean",
                        well_id=identifier,
                        npt_record_id=record_id,
                        duration_hours=repr(hours),
                    )
                try:
                    value = float(hours)
                except (TypeError, ValueError, OverflowError) as error:
                    raise ValidationError(
                        "an NPT row states a malformed duration",
                        hint="duration_hours must be a finite non-negative number",
                        well_id=identifier,
                        npt_record_id=record_id,
                        duration_hours=repr(hours),
                    ) from error
                if not math.isfinite(value):
                    # A NaN or an infinity would poison the total silently - every comparison against it
                    # is false, so the stored number would look like a number and behave like nothing.
                    raise ValidationError(
                        "an NPT row states a duration that is not a finite number",
                        hint="fix the promoted row; a total cannot be computed from it",
                        well_id=identifier,
                        npt_record_id=record_id,
                        duration_hours=repr(hours),
                    )
                if value < 0.0:
                    raise ValidationError(
                        "an NPT row states a negative duration",
                        hint="NPT duration_hours cannot be negative",
                        well_id=identifier,
                        npt_record_id=record_id,
                        duration_hours=value,
                    )

                evidence = _evidence_of(record)
                row_version_id = str(getattr(record, "document_version_id", "") or "")
                evidence_version_id = str(evidence.get("document_version_id") or "")
                if row_version_id and evidence_version_id and row_version_id != evidence_version_id:
                    raise ValidationError(
                        "an NPT input cites conflicting document versions",
                        hint="the row and its provenance must name the same source version",
                        well_id=identifier,
                        npt_record_id=record_id,
                        row_document_version_id=row_version_id,
                        evidence_document_version_id=evidence_version_id,
                    )
                version_id = row_version_id or evidence_version_id
                if not version_id:
                    without_evidence += 1
                    raise ValidationError(
                        "every summed NPT row must cite a document version",
                        hint="re-promote the source row so its evidence and source digest are present",
                        well_id=identifier,
                        npt_record_id=record_id,
                    )
                version = active.get(DocumentVersion, version_id)
                if version is None:
                    raise ValidationError(
                        "an NPT input cites a document version that does not exist",
                        hint="repair the promoted source row before executing a calculation",
                        well_id=identifier,
                        npt_record_id=record_id,
                        document_version_id=version_id,
                    )
                if not bool(version.is_current):
                    raise ValidationError(
                        "an NPT input cites a superseded document version",
                        hint="the stored historical calculation remains readable; promote the current source before re-running",
                        well_id=identifier,
                        npt_record_id=record_id,
                        document_version_id=version_id,
                    )
                if active.get(Document, str(version.document_id)) is None:
                    raise ValidationError(
                        "an NPT input cites a document version without its document",
                        hint="repair the source registry before executing a calculation",
                        well_id=identifier,
                        npt_record_id=record_id,
                        document_version_id=version_id,
                    )
                if not _valid_sha256(str(version.sha256 or "")):
                    raise ValidationError(
                        "an NPT input cites a document version with a malformed digest",
                        hint="re-ingest the source so its content digest is recorded",
                        well_id=identifier,
                        npt_record_id=record_id,
                        document_version_id=version_id,
                    )
                evidence["document_version_id"] = version_id
                row_document_id = str(getattr(record, "document_id", "") or "")
                evidence_document_id = str(evidence.get("document_id") or "")
                version_document_id = str(version.document_id or "")
                if row_document_id and row_document_id != version_document_id:
                    raise ValidationError(
                        "an NPT input names a document that does not own its version",
                        hint="repair the promoted evidence chain before executing a calculation",
                        well_id=identifier,
                        npt_record_id=record_id,
                        document_id=row_document_id,
                        document_version_id=version_id,
                    )
                if evidence_document_id and evidence_document_id != version_document_id:
                    raise ValidationError(
                        "an NPT input provenance names a document that does not own its version",
                        hint="repair the promoted evidence chain before executing a calculation",
                        well_id=identifier,
                        npt_record_id=record_id,
                        document_id=evidence_document_id,
                        document_version_id=version_id,
                    )
                evidence["document_id"] = (
                    row_document_id or evidence_document_id or version_document_id
                )
                source_digest = str(evidence.get("source_sha256") or "")
                if not _valid_sha256(source_digest) or source_digest != str(version.sha256 or ""):
                    raise ValidationError(
                        "an NPT input does not carry the digest of its cited source version",
                        hint="re-promote the source row; an executable result needs verified evidence",
                        well_id=identifier,
                        npt_record_id=record_id,
                        document_version_id=version_id,
                    )

                input_name = _input_name(record)
                if input_name in inputs:
                    raise ValidationError(
                        "two NPT inputs would have the same indexed name",
                        hint="source row identities must be unique before a calculation can be stored",
                        well_id=identifier,
                        npt_record_id=record_id,
                        input_name=input_name,
                    )
                total += value
                if not math.isfinite(total):
                    raise ValidationError(
                        "the NPT total is not a finite number",
                        hint="the source durations overflow the supported numeric range",
                        well_id=identifier,
                    )
                quantified += 1
                inputs[input_name] = {
                    "value": value,
                    "unit": NPT_ROLLUP_UNIT,
                    "dimension": unit.dimension.value,
                    "subject_key": subject,
                    "source_kind": "npt_record",
                    "provenance": evidence,
                }
                if version_id not in citations:
                    citations[version_id] = {
                        key: value
                        for key, value in evidence.items()
                        if key in ("document_id", "document_version_id", "source_sha256")
                    }

            if quantified == 0:
                raise ValidationError(
                    "this well has no NPT row that states a duration, so there is no total to record",
                    hint=(
                        "promote the reports that state lost time first; a roll-up of nothing is not"
                        " zero hours"
                    ),
                    well_id=identifier,
                    records=len(records),
                )
            if not citations:
                raise ValidationError(
                    "none of this well's NPT rows cite a document version",
                    hint=(
                        "a derived engineering record has to cite its evidence; these rows were not"
                        " promoted from a document"
                    ),
                    well_id=identifier,
                    records=quantified,
                )

            return EngineeringRepository(active).record_calculation(
                method_id=NPT_ROLLUP_METHOD_ID,
                method_version=NPT_ROLLUP_METHOD_VERSION,
                calculation_type=NPT_ROLLUP_PROPERTY,
                record_state=RecordState.ACTUAL,
                well_id=identifier,
                inputs=inputs,
                outputs={
                    NPT_ROLLUP_PROPERTY: {
                        # Four decimals is the rounding ``npt_totals`` and ``_npt_hours_by_section``
                        # already use for this quantity, so the three agree on the same number.
                        "value": round(total, 4),
                        "unit": NPT_ROLLUP_UNIT,
                        "dimension": unit.dimension.value,
                    },
                    "records": quantified,
                },
                validation={
                    "records_considered": len(records),
                    "records_summed": quantified,
                    # Reported rather than silently dropped: the total is only as complete as this says.
                    "records_without_duration": without_duration,
                    "records_without_evidence": without_evidence,
                },
                status=CalculationStatus.COMPUTED,
                # No single ``document_version_id`` on the row: several versions contribute, and naming
                # one of them would misattribute the rest.  The citations live per input, where the
                # change-impact query reads them.
                provenance=[citations[key] for key in sorted(citations)],
                origin=_ORIGIN,
                triggered_by=_TRIGGERED_BY,
                supersedes_id=supersedes_id,
                created_by=created_by,
            )

    def npt_rollup_subject(self, well_id: str) -> str:
        """The canonical subject key a roll-up for this well is filed under.

        Exposed because a caller asking "what depends on this well's lost time" needs the exact key the
        writer used, and re-deriving it by hand is how two spellings of one subject appear.
        """
        identifier = str(well_id or "").strip()
        if not identifier:
            raise ValidationError("a subject key needs the well it is about", hint="pass well_id")
        return SubjectKey(
            well_id=identifier,
            property_name=normalize_property(NPT_ROLLUP_PROPERTY),
            record_state=normalize_state(RecordState.ACTUAL.value),
        ).render()


def _valid_sha256(value: str) -> bool:
    """Accept only the registry's full lowercase/uppercase hexadecimal content digest."""
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)


def _input_name(record: NptRecord) -> str:
    """A stable, collision-resistant name for the input one NPT row contributes.

    ``identity_key`` is preferred because promotion derives it from what the source said and where it said
    it, so re-promoting the same version reuses it and the roll-up's identity does not move.  A row entered
    by hand has none, and its primary key is the only stable handle it has.  A long source identity is
    digested instead of truncated: two different rows must never overwrite one another in the JSON input
    map just because their first 80 characters happen to match.
    """
    handle = str(getattr(record, "identity_key", "") or "").strip() or str(record.id)
    candidate = f"npt.{handle}"
    return candidate if len(candidate) <= 80 else f"npt.k256:{sha256_text(handle)[:64]}"


def _evidence_of(record: NptRecord) -> dict[str, Any]:
    """Copy one NPT row's recorded evidence into its calculation input.

    The calculation keeps the complete selected provenance mapping, not only its ids.  That preserves the
    locator, excerpt and source path needed for human navigation while the durable document/version ids and
    digest remain the fields the execution validator checks.  The mapping is a snapshot of the promoted row;
    it is not a second evidence system and it never attempts to re-read or reinterpret the source.
    """
    stored = getattr(record, "provenance", None)
    entries: list[Mapping[str, Any]] = []
    if isinstance(stored, Sequence) and not isinstance(stored, str | bytes):
        entries.extend(item for item in stored if isinstance(item, Mapping))
    elif isinstance(stored, Mapping):
        entries.append(stored)

    record_version_id = str(getattr(record, "document_version_id", "") or "")
    selected: Mapping[str, Any] | None = None
    if record_version_id:
        selected = next(
            (
                item
                for item in entries
                if str(item.get("document_version_id") or "") == record_version_id
            ),
            None,
        )
    if selected is None and entries:
        selected = entries[0]

    citation: dict[str, Any] = dict(selected or {})
    for column in ("document_id", "document_version_id"):
        value = str(getattr(record, column, "") or "")
        if value and not citation.get(column):
            citation[column] = value
    citation["npt_record_id"] = str(getattr(record, "id", "") or "")
    handle = str(getattr(record, "identity_key", "") or "").strip()
    if handle:
        citation["npt_identity_key"] = handle
    return citation
