"""Static document-to-domain promotion contracts.

A taxonomy value is not a permission to write a domain row.  This module is the
small, inspectable registry that separates those concerns.  A contract names the
single handler that is allowed to promote a classification, its destination
models, and the evidence requirements that must be satisfied before the handler
is entered.  Classifications without a contract are evidence/knowledge inputs
only; they are not guessed into the nearest operational table.

The registry intentionally contains no callable plugin mechanism.  ``handler``
is a stable name resolved by :class:`VersionPromoter` to one of its explicit
methods.  Adding a new domain writer therefore requires a visible registry
entry, a writer, and a test/certification update.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..core.enums import DocumentClassification


class CoverageLevel(StrEnum):
    """Highest source-derived capability certified for a classification."""

    CLASSIFY_ONLY = "CLASSIFY_ONLY"
    EXTRACT_ONLY = "EXTRACT_ONLY"
    KNOWLEDGE_SUPPORTED = "KNOWLEDGE_SUPPORTED"
    DOMAIN_PROMOTABLE = "DOMAIN_PROMOTABLE"
    REVIEWABLE = "REVIEWABLE"
    END_TO_END_CERTIFIED = "END_TO_END_CERTIFIED"
    UNSUPPORTED = "UNSUPPORTED"


class PromotionOutcome(StrEnum):
    """Version-level outcomes exposed by the promotion service and CLI."""

    ELIGIBLE = "ELIGIBLE"
    PROMOTED = "PROMOTED"
    UNCHANGED = "UNCHANGED"
    UNSUPPORTED = "UNSUPPORTED"
    AMBIGUOUS = "AMBIGUOUS"
    MISSING_ARTEFACT = "MISSING_ARTEFACT"
    MISSING_WELL = "MISSING_WELL"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    INVALID_FIELDS = "INVALID_FIELDS"
    CONFLICT = "CONFLICT"
    ERROR = "ERROR"


@dataclass(frozen=True)
class PromotionContract:
    """The static eligibility contract for one document classification."""

    classification: DocumentClassification
    level: CoverageLevel
    handler: str = ""
    target_models: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    review_surface: str = "DomainReviewService"
    notes: str = ""
    #: Existing V2 contracts retain their ids; the admitted mud contract is explicitly versioned V3.
    contract_revision: str = "v2"

    @property
    def domain_promotable(self) -> bool:
        return bool(self.handler)

    @property
    def contract_id(self) -> str:
        return f"document:{self.classification.value}:promotion:{self.contract_revision}"

    def to_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification.value,
            "level": self.level.value,
            "contract_id": self.contract_id,
            "handler": self.handler or None,
            "target_models": list(self.target_models),
            "required_evidence": list(self.required_evidence),
            "review_surface": self.review_surface,
            "notes": self.notes,
        }


# These are the only admitted domain writers.  The first four preserve the V2
# contracts; the narrow mud writer is the explicitly revisioned V3 addition; the
# BHA, bit-run and directional-survey writers are the explicitly revisioned V4
# additions.  They correspond to existing tables and repository methods; no
# cost/invoice/cement/casing/procedure writer is implied by the taxonomy or a
# knowledge entity, and a classification that has no entry here cannot write one.
_PROMOTABLE: Final[dict[DocumentClassification, PromotionContract]] = {
    DocumentClassification.DRILLING_PROGRAM: PromotionContract(
        classification=DocumentClassification.DRILLING_PROGRAM,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="program",
        target_models=("drilling_program", "program_target", "well_section"),
        required_evidence=(
            "stored_extraction",
            "VALID_fields",
            "field_units",
            "field_provenance",
            "well_link",
        ),
        notes="Planned targets only; never copied into actual section measurements.",
    ),
    DocumentClassification.DDR: PromotionContract(
        classification=DocumentClassification.DDR,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="report",
        target_models=(
            "ddr_report",
            "well_operation",
            "well_event",
            "npt_record",
            "problem_occurrence",
        ),
        required_evidence=(
            "stored_extraction",
            "recognised_table_headers",
            "row_provenance",
            "well_linkage",
        ),
        notes="Table/typed-field rows only; narrative is not a deterministic domain writer.",
    ),
    DocumentClassification.NPT: PromotionContract(
        classification=DocumentClassification.NPT,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="report",
        target_models=(
            "ddr_report",
            "well_operation",
            "well_event",
            "npt_record",
            "problem_occurrence",
        ),
        required_evidence=(
            "stored_extraction",
            "NPT_duration_header",
            "row_duration_unit",
            "row_provenance",
            "well_linkage",
        ),
        notes="Only explicit NPT/lost-hours columns are promoted; arbitrary prose is not.",
    ),
    DocumentClassification.TIME_BREAKDOWN: PromotionContract(
        classification=DocumentClassification.TIME_BREAKDOWN,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="report",
        target_models=("ddr_report", "well_operation", "npt_record"),
        required_evidence=(
            "stored_extraction",
            "activity_header",
            "duration_header",
            "source_duration_preserved",
            "row_provenance",
            "well_linkage",
            "explicit_actual_state",
        ),
        notes=(
            "Certified on a standalone source-shaped activity/hours table: every source activity becomes "
            "an actual candidate operation, only explicit NPT-coded rows become NPT, and totals are not rows."
        ),
    ),
    DocumentClassification.MUD_REPORT: PromotionContract(
        classification=DocumentClassification.MUD_REPORT,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="mud_report",
        target_models=("mud_report", "mud_measurement"),
        required_evidence=(
            "stored_extraction",
            "summary_label_value_unit_table",
            "repeated_daily_measurement_table",
            "source_units_preserved",
            "row_provenance",
            "well_linkage",
            "explicit_section_or_null",
        ),
        review_surface="DomainReviewService",
        notes=(
            "Summary and repeated daily tests are retained as separate rows; section matching is explicit "
            "and ambiguous or absent matches remain NULL. No unit conversion is claimed."
        ),
        contract_revision="v3",
    ),
    DocumentClassification.BHA_REPORT: PromotionContract(
        classification=DocumentClassification.BHA_REPORT,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="bha_report",
        target_models=("bha_report", "bha_component"),
        required_evidence=(
            "stored_extraction",
            "component_description_column",
            "component_sizing_column",
            "source_order_preserved",
            "row_provenance",
            "well_linkage",
            "explicit_section_or_null",
        ),
        review_surface="DomainReviewService",
        notes=(
            "A component tally with an explicit header row: description plus at least one sizing column. "
            "Components keep the source's order, words, identifiers and units; no type is inferred from "
            "prose and no assembly total is computed."
        ),
        contract_revision="v4",
    ),
    DocumentClassification.BIT_RECORD: PromotionContract(
        classification=DocumentClassification.BIT_RECORD,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="bit_record",
        target_models=("bit_record",),
        required_evidence=(
            "stored_extraction",
            "bit_number_column",
            "bit_measurement_column",
            "row_provenance",
            "well_linkage",
        ),
        review_surface="DomainReviewService",
        notes=(
            "A bit tally: a bit number column plus at least one of size/footage/rotating hours/drilling "
            "hours/IADC code.  A replacement bit is a new run, never an edit of the previous one, and no "
            "footage, ROP or grade is computed from anything else."
        ),
        contract_revision="v4",
    ),
    DocumentClassification.DIRECTIONAL_SURVEY: PromotionContract(
        classification=DocumentClassification.DIRECTIONAL_SURVEY,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="directional_survey",
        target_models=("survey_run", "survey_station"),
        required_evidence=(
            "stored_extraction",
            "measured_depth_column",
            "inclination_column",
            "azimuth_column",
            "source_order_preserved",
            "row_provenance",
            "well_linkage",
            "explicit_section_or_null",
        ),
        review_surface="DomainReviewService",
        notes=(
            "Stations carrying measured depth, inclination and azimuth in three distinct columns.  TVD, "
            "northing, easting and dogleg severity are preserved only when the source supplied them; no "
            "trajectory calculation exists in the platform and none is performed during ingestion."
        ),
        contract_revision="v4",
    ),
    DocumentClassification.COST: PromotionContract(
        classification=DocumentClassification.COST,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="cost",
        target_models=("cost_item",),
        required_evidence=(
            "stored_extraction",
            "cost_code_column",
            "description_column",
            "side_labelled_amount_column",
            "explicit_currency",
            "row_provenance",
        ),
        review_surface="DomainReviewService",
        notes=(
            "A cost line table: a cost code column, a description column and at least one money "
            "column whose header says whether it is planned-side or actual-side.  The existing "
            "CostItem record is reused rather than duplicated.  Planned and actual are never "
            "merged, a currency the source did not state is refused rather than defaulted to USD, "
            "there is no currency conversion anywhere, and npt_id is set only on an exact stored "
            "record identity - never from a cost line sitting near an NPT row."
        ),
        contract_revision="v7",
    ),
    DocumentClassification.CASING_REPORT: PromotionContract(
        classification=DocumentClassification.CASING_REPORT,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="casing",
        target_models=("casing_run",),
        required_evidence=(
            "stored_extraction",
            "casing_size_column",
            "shoe_depth_column",
            "string_property_column",
            "source_units_preserved",
            "row_provenance",
            "well_linkage",
            "explicit_section_or_null",
        ),
        review_surface="DomainReviewService",
        notes=(
            "A casing tally: a size column, a shoe depth column and at least one of grade, weight, "
            "connection or an explicit type column.  Writes actual runs only - never a programme, a "
            "target or a WellSection, so plan and actual stay two truths.  The string type is taken "
            "from an explicit type column and is never inferred from size or depth; a 9 5/8 in "
            "string is not automatically production casing.  Every dimension keeps the source's "
            "text, value and unit as three facts, and nothing is converted."
        ),
        contract_revision="v7",
    ),
    DocumentClassification.CEMENT_REPORT: PromotionContract(
        classification=DocumentClassification.CEMENT_REPORT,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="cement",
        target_models=("cement_job",),
        required_evidence=(
            "stored_extraction",
            "cement_volume_column",
            "cement_datum_column",
            "source_units_preserved",
            "row_provenance",
            "well_linkage",
        ),
        review_surface="DomainReviewService",
        notes=(
            "A cement job table: a cement-specific volume column (lead, tail or total) together "
            "with a slurry, top of cement, shoe depth, displacement or wait-on-cement column.  A "
            "generic Volume/Pressure/Depth table is not a cement job.  Lead and tail are never "
            "summed, top of cement is never read as shoe depth, and no annular volume, excess, "
            "hydrostatic or displacement arithmetic exists anywhere in the platform.  A casing "
            "association is stored only when the source names a string that resolves to exactly one "
            "current run of the well."
        ),
        contract_revision="v7",
    ),
    DocumentClassification.WELL_CONTROL: PromotionContract(
        classification=DocumentClassification.WELL_CONTROL,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="well_control",
        target_models=("well_control_event",),
        required_evidence=(
            "stored_extraction",
            "named_pressure_or_gain_column",
            "second_event_fact_column",
            "source_units_preserved",
            "row_provenance",
            "well_linkage",
        ),
        review_surface="DomainReviewService",
        notes=(
            "A well-control event table: a column named SIDPP, SICP or pit gain, alongside at least "
            "one of depth, event type, date or description.  A generic Pressure/Volume/Time table "
            "inside a well-control report is not a well-control event.  Every quantity keeps text, "
            "value and unit as three facts and a value is stored only when its header stated the "
            "unit - a bare 1200 is not known to be psi, bar or kPa.  Event type comes from an "
            "explicit type column matched whole, never from a pit gain or a depth.  Cause is "
            "KNOWN only when the sheet states one.  No NPT row, no problem occurrence and no "
            "risk record is created: lost time is a separate fact the source states or does not, and "
            "no kill method, pressure simulation or diagnosis is ever computed."
        ),
        contract_revision="v7",
    ),
    DocumentClassification.HSE: PromotionContract(
        classification=DocumentClassification.HSE,
        level=CoverageLevel.END_TO_END_CERTIFIED,
        handler="hse",
        target_models=("hse_incident",),
        required_evidence=(
            "stored_extraction",
            "incident_type_or_reference_column",
            "description_column",
            "row_provenance",
            "source_severity_verbatim",
        ),
        review_surface="DomainReviewService",
        notes=(
            "An HSE incident table: a column named as an incident type or an incident reference, "
            "together with a description column.  A generic Incident/Date/Severity or "
            "Action/Date/Status table is not an incident register.  Severity is stored exactly as "
            "reported and is never calculated from a probability and an impact, never defaulted when "
            "absent, and never converted into a RiskRecord score.  Root cause is stored only when "
            "the source states one.  well_id is nullable on purpose: a camp, warehouse or access "
            "road event is filed at its site rather than against an invented well, which is why this "
            "is not WellEvent(category='safety') - that table's well_id is NOT NULL.  No NPT row is "
            "created for an event with no stated lost time."
        ),
        contract_revision="v7",
    ),
}


# The remaining taxonomy classes are deliberately registered as non-domain
# contracts.  This is an explicit deny list, not an accidental fall-through.
_KNOWLEDGE_SUPPORTED = frozenset(
    {
        DocumentClassification.LOGGING,
        DocumentClassification.SERVICE_REPORT,
        DocumentClassification.EOWR,
        DocumentClassification.LESSON_LEARNED,
        DocumentClassification.PROCEDURE,
        DocumentClassification.STANDARD,
        DocumentClassification.CONTRACT,
        DocumentClassification.TECHNICAL_REFERENCE,
    }
)

_EXTRACT_ONLY = frozenset(
    {
        # These enum members are accepted as stored/manual labels, but have no
        # dedicated deterministic classifier signature or domain destination.
        DocumentClassification.WIRELINE,
        DocumentClassification.LWD_MWD,
        DocumentClassification.INVOICE,
        DocumentClassification.BOOK,
        DocumentClassification.OTHER,
    }
)


def _non_promotable(
    classification: DocumentClassification,
    level: CoverageLevel,
    notes: str,
) -> PromotionContract:
    return PromotionContract(
        classification=classification,
        level=level,
        required_evidence=("stored_extraction", "field_provenance_for_knowledge"),
        notes=notes,
    )


# Build the complete registry from the enum and fail at import if a new enum
# value is not classified here.  This prevents a future taxonomy addition from
# silently becoming a promotable file.
CONTRACTS: Final[dict[DocumentClassification, PromotionContract]] = {
    **_PROMOTABLE,
    **{
        classification: _non_promotable(
            classification,
            CoverageLevel.KNOWLEDGE_SUPPORTED,
            "Facts may be derived from stored, provenance-carrying fields; no domain writer is registered.",
        )
        for classification in _KNOWLEDGE_SUPPORTED
    },
    **{
        classification: _non_promotable(
            classification,
            CoverageLevel.EXTRACT_ONLY,
            "Generic extraction is retained for evidence; no classification-specific knowledge/domain contract is claimed.",
        )
        for classification in _EXTRACT_ONLY
    },
}

if set(CONTRACTS) != set(DocumentClassification):  # pragma: no cover - import-time guard
    missing = sorted(set(DocumentClassification) - set(CONTRACTS), key=str)
    extra = sorted(set(CONTRACTS) - set(DocumentClassification), key=str)
    raise RuntimeError(
        f"promotion contract registry is incomplete: missing={missing}, extra={extra}"
    )


PROMOTION_CONTRACTS: Final[dict[str, PromotionContract]] = {
    classification.value: contract for classification, contract in CONTRACTS.items()
}


def promotion_contract(value: str | DocumentClassification | None) -> PromotionContract | None:
    """Return the explicit contract for ``value``; unknown values are unsupported."""
    classification = DocumentClassification.parse(value)
    return CONTRACTS.get(classification) if classification is not None else None


def contract_registry() -> tuple[PromotionContract, ...]:
    """Stable, inspectable registry order used by docs, CLI and tests."""
    return tuple(CONTRACTS[classification] for classification in DocumentClassification)


__all__ = [
    "CONTRACTS",
    "PROMOTION_CONTRACTS",
    "CoverageLevel",
    "PromotionContract",
    "PromotionOutcome",
    "contract_registry",
    "promotion_contract",
]
