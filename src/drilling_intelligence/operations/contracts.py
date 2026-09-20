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

    @property
    def domain_promotable(self) -> bool:
        return bool(self.handler)

    @property
    def contract_id(self) -> str:
        return f"document:{self.classification.value}:promotion:v2"

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


# These are the only V2 domain writers.  They correspond to existing tables and
# existing repository methods; no cost/invoice/survey/procedure writer is implied
# by the taxonomy or by a knowledge entity.
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
        level=CoverageLevel.DOMAIN_PROMOTABLE,
        handler="report",
        target_models=("ddr_report", "well_operation", "npt_record"),
        required_evidence=(
            "stored_extraction",
            "activity_header",
            "duration_header",
            "row_provenance",
            "well_linkage",
        ),
        notes="Promotable by explicit activity/hours table; V2 still requires a dedicated certification fixture.",
    ),
}


# The remaining taxonomy classes are deliberately registered as non-domain
# contracts.  This is an explicit deny list, not an accidental fall-through.
_KNOWLEDGE_SUPPORTED = frozenset(
    {
        DocumentClassification.MUD_REPORT,
        DocumentClassification.BHA_REPORT,
        DocumentClassification.BIT_RECORD,
        DocumentClassification.DIRECTIONAL_SURVEY,
        DocumentClassification.CEMENT_REPORT,
        DocumentClassification.CASING_REPORT,
        DocumentClassification.WELL_CONTROL,
        DocumentClassification.LOGGING,
        DocumentClassification.SERVICE_REPORT,
        DocumentClassification.HSE,
        DocumentClassification.COST,
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
