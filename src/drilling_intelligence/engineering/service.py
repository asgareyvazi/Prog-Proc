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
from ..core.ids import SubjectKey, normalize_property, normalize_state
from ..core.units import UnitError, resolve_unit
from ..database.models import Calculation, NptRecord, Well
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

#: Fixed, because ``triggered_by`` is part of the content-addressed identity: if it varied with the caller,
#: the CLI and a scheduled run would write two rows for one result.
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
            yield own
            own.commit()

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
                hours = record.duration_hours
                if hours is None:
                    # Counted, never imputed: "nothing was recorded" is not "nothing was lost".
                    without_duration += 1
                    continue
                value = float(hours)
                if not math.isfinite(value):
                    # A NaN or an infinity would poison the total silently - every comparison against it
                    # is false, so the stored number would look like a number and behave like nothing.
                    raise ValidationError(
                        "an NPT row states a duration that is not a finite number",
                        hint="fix the promoted row; a total cannot be computed from it",
                        well_id=identifier,
                        npt_record_id=str(record.id),
                        duration_hours=repr(hours),
                    )
                evidence = _evidence_of(record)
                if not evidence.get("document_version_id"):
                    without_evidence += 1
                total += value
                quantified += 1
                inputs[_input_name(record)] = {
                    "value": value,
                    "unit": NPT_ROLLUP_UNIT,
                    "dimension": unit.dimension.value,
                    "subject_key": subject,
                    "source_kind": "npt_record",
                    "provenance": evidence,
                }
                version_id = str(evidence.get("document_version_id") or "")
                if version_id and version_id not in citations:
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


def _input_name(record: NptRecord) -> str:
    """A stable name for the input one NPT row contributes.

    ``identity_key`` is preferred because promotion derives it from what the source said and where it said
    it, so re-promoting the same version reuses it and the roll-up's identity does not move.  A row entered
    by hand has none, and its primary key is the only stable handle it has.
    """
    handle = str(getattr(record, "identity_key", "") or "").strip() or str(record.id)
    return f"npt.{handle}"[:80]


def _evidence_of(record: NptRecord) -> dict[str, Any]:
    """The citation for one NPT row: where it came from, in a form that cannot drift.

    Only the identifying fields are copied - the document, the version, the source digest and the row's own
    handles.  The row's full provenance (excerpt, parser, locator) stays where it is rather than being
    duplicated here: this is a link to the evidence, and a link that carried a stale copy of the wording
    would be the more dangerous of the two.  Nothing time-varying is included, because these bytes end up
    inside the calculation's content-addressed identity.
    """
    citation: dict[str, Any] = {}
    for column in ("document_id", "document_version_id"):
        value = str(getattr(record, column, "") or "")
        if value:
            citation[column] = value
    stored = getattr(record, "provenance", None)
    first: Mapping[str, Any] | None = None
    if isinstance(stored, Sequence) and not isinstance(stored, str | bytes):
        for item in stored:
            if isinstance(item, Mapping):
                first = item
                break
    elif isinstance(stored, Mapping):
        first = stored
    if first is not None:
        for key in ("document_id", "document_version_id", "source_sha256"):
            value = str(first.get(key) or "")
            if value and key not in citation:
                citation[key] = value
        # The digest is what proves the cited version is the bytes that were read, so it is kept even
        # when the ids were already present.
        digest = str(first.get("source_sha256") or "")
        if digest:
            citation["source_sha256"] = digest
    citation["npt_record_id"] = str(record.id)
    handle = str(getattr(record, "identity_key", "") or "").strip()
    if handle:
        citation["npt_identity_key"] = handle
    return citation
