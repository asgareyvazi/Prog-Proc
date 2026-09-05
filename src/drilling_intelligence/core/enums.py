"""Domain enumerations.

Kept in one place because they form the shared vocabulary of the schema
(stored as strings in the DB so that adding a member never requires a
migration and so that PostgreSQL/SQLite behave identically).
"""

from __future__ import annotations

from enum import StrEnum


class StrEnumLike(StrEnum):
    """``str`` enum: stores as text in the DB, so adding a member needs no migration."""

    @classmethod
    def parse(cls, raw: object) -> StrEnumLike | None:
        if raw is None:
            return None
        if isinstance(raw, cls):
            return raw
        try:
            return cls(str(raw))
        except ValueError:
            return None


class WellLifecycleStatus(StrEnumLike):
    """Well lifecycle (master spec section 10)."""

    PLANNED = "PLANNED"
    PRE_SPUD = "PRE_SPUD"
    DRILLING = "DRILLING"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"
    CLOSED = "CLOSED"
    HISTORICAL = "HISTORICAL"


#: Allowed forward transitions used by the well service for validation.
#: Anything else is rejected rather than silently accepted.
WELL_LIFECYCLE_TRANSITIONS: dict[WellLifecycleStatus, tuple[WellLifecycleStatus, ...]] = {
    WellLifecycleStatus.PLANNED: (
        WellLifecycleStatus.PRE_SPUD,
        WellLifecycleStatus.SUSPENDED,
        WellLifecycleStatus.ABANDONED,
    ),
    WellLifecycleStatus.PRE_SPUD: (
        WellLifecycleStatus.DRILLING,
        WellLifecycleStatus.SUSPENDED,
        WellLifecycleStatus.ABANDONED,
    ),
    WellLifecycleStatus.DRILLING: (
        WellLifecycleStatus.SUSPENDED,
        WellLifecycleStatus.COMPLETED,
        WellLifecycleStatus.ABANDONED,
    ),
    WellLifecycleStatus.SUSPENDED: (
        WellLifecycleStatus.DRILLING,
        WellLifecycleStatus.COMPLETED,
        WellLifecycleStatus.ABANDONED,
    ),
    WellLifecycleStatus.COMPLETED: (
        WellLifecycleStatus.CLOSED,
        WellLifecycleStatus.ABANDONED,
    ),
    WellLifecycleStatus.ABANDONED: (WellLifecycleStatus.CLOSED,),
    WellLifecycleStatus.CLOSED: (WellLifecycleStatus.HISTORICAL,),
    WellLifecycleStatus.HISTORICAL: (),
}


class RecordState(StrEnumLike):
    """Digital well state for any planned/actual quantity (section 11).

    Planned and actual values must never be mixed silently; every quantitative
    record carries the state it belongs to.
    """

    PLANNED = "PLANNED"
    FORECAST = "FORECAST"
    CURRENT = "CURRENT"
    ACTUAL = "ACTUAL"
    AS_BUILT = "AS_BUILT"
    HISTORICAL = "HISTORICAL"


class DocumentClassification(StrEnumLike):
    """Deterministic document taxonomy (section 15)."""

    DRILLING_PROGRAM = "DRILLING_PROGRAM"
    DDR = "DDR"
    MUD_REPORT = "MUD_REPORT"
    BHA_REPORT = "BHA_REPORT"
    BIT_RECORD = "BIT_RECORD"
    DIRECTIONAL_SURVEY = "DIRECTIONAL_SURVEY"
    CEMENT_REPORT = "CEMENT_REPORT"
    CASING_REPORT = "CASING_REPORT"
    WELL_CONTROL = "WELL_CONTROL"
    LOGGING = "LOGGING"
    WIRELINE = "WIRELINE"
    LWD_MWD = "LWD_MWD"
    SERVICE_REPORT = "SERVICE_REPORT"
    HSE = "HSE"
    NPT = "NPT"
    COST = "COST"
    INVOICE = "INVOICE"
    TIME_BREAKDOWN = "TIME_BREAKDOWN"
    EOWR = "EOWR"
    PROCEDURE = "PROCEDURE"
    STANDARD = "STANDARD"
    CONTRACT = "CONTRACT"
    TECHNICAL_REFERENCE = "TECHNICAL_REFERENCE"
    BOOK = "BOOK"
    LESSON_LEARNED = "LESSON_LEARNED"
    OTHER = "OTHER"


class DocumentStatus(StrEnumLike):
    """Business status of a document as authored (not processing state)."""

    DRAFT = "DRAFT"
    ISSUED_FOR_REVIEW = "ISSUED_FOR_REVIEW"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"
    UNKNOWN = "UNKNOWN"


class ProcessingStatus(StrEnumLike):
    """Where a document sits in the ingestion pipeline."""

    DISCOVERED = "DISCOVERED"
    REGISTERED = "REGISTERED"
    EXTRACTED = "EXTRACTED"
    CLASSIFIED = "CLASSIFIED"
    INDEXED = "INDEXED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class FileChangeKind(StrEnumLike):
    """Incremental ingestion outcome per file (section 13)."""

    NEW = "NEW"
    MODIFIED = "MODIFIED"
    UNCHANGED = "UNCHANGED"
    DUPLICATE = "DUPLICATE"
    REMOVED = "REMOVED"


class DataQuality(StrEnumLike):
    """Data quality state of an extracted field (section 58)."""

    VALID = "VALID"
    INVALID = "INVALID"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"
    UNVERIFIED = "UNVERIFIED"
    INFERRED = "INFERRED"


class KnowledgeItemType(StrEnumLike):
    """Structured knowledge object types (section 18)."""

    CONCEPT = "CONCEPT"
    DEFINITION = "DEFINITION"
    FORMULA = "FORMULA"
    VARIABLE = "VARIABLE"
    CONSTANT = "CONSTANT"
    RULE = "RULE"
    REQUIREMENT = "REQUIREMENT"
    CONSTRAINT = "CONSTRAINT"
    ASSUMPTION = "ASSUMPTION"
    PROCEDURE = "PROCEDURE"
    DECISION_RULE = "DECISION_RULE"
    EXAMPLE = "EXAMPLE"
    LESSON = "LESSON"
    RISK = "RISK"
    MITIGATION = "MITIGATION"
    EQUIPMENT = "EQUIPMENT"
    EVENT = "EVENT"
    OBSERVATION = "OBSERVATION"
    METHOD = "METHOD"
    STANDARD = "STANDARD"
    SPECIFICATION = "SPECIFICATION"


class KnowledgeStatus(StrEnumLike):
    """Lifecycle of a knowledge object."""

    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    CONFLICTED = "CONFLICTED"
    SUPERSEDED = "SUPERSEDED"
    RETIRED = "RETIRED"


class KnowledgeRelationType(StrEnumLike):
    """Directed edge vocabulary for the knowledge graph (section 20)."""

    WELL_HAS_DOCUMENT = "WELL_HAS_DOCUMENT"
    DOCUMENT_CONTAINS_KNOWLEDGE = "DOCUMENT_CONTAINS_KNOWLEDGE"
    KNOWLEDGE_SUPPORTS_METHOD = "KNOWLEDGE_SUPPORTS_METHOD"
    METHOD_REQUIRES_INPUT = "METHOD_REQUIRES_INPUT"
    METHOD_HAS_ASSUMPTION = "METHOD_HAS_ASSUMPTION"
    METHOD_HAS_FORMULA = "METHOD_HAS_FORMULA"
    WELL_HAS_SECTION = "WELL_HAS_SECTION"
    SECTION_HAS_BHA = "SECTION_HAS_BHA"
    SECTION_HAS_BIT = "SECTION_HAS_BIT"
    SECTION_HAS_MUD = "SECTION_HAS_MUD"
    SECTION_HAS_SURVEY = "SECTION_HAS_SURVEY"
    EVENT_CAUSES_NPT = "EVENT_CAUSES_NPT"
    NPT_IMPACTS_COST = "NPT_IMPACTS_COST"
    RISK_AFFECTS_ACTIVITY = "RISK_AFFECTS_ACTIVITY"
    LESSON_DERIVED_FROM_WELL = "LESSON_DERIVED_FROM_WELL"
    PROGRAM_CONTAINS_REQUIREMENT = "PROGRAM_CONTAINS_REQUIREMENT"
    ITEM_CONFLICTS_WITH = "ITEM_CONFLICTS_WITH"
    ITEM_SUPPORTS = "ITEM_SUPPORTS"
    DOCUMENT_HAS_VERSION = "DOCUMENT_HAS_VERSION"
    SKILL_USES_KNOWLEDGE = "SKILL_USES_KNOWLEDGE"


class ConflictResolution(StrEnumLike):
    """Outcome of conflict evaluation (section 19)."""

    OPEN = "OPEN"
    RESOLVED_BY_AUTHORITY = "RESOLVED_BY_AUTHORITY"
    RESOLVED_MANUALLY = "RESOLVED_MANUALLY"
    IRRECONCILABLE = "IRRECONCILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CheckResult(StrEnumLike):
    """QA/QC verdict vocabulary (section 30)."""

    PASS = "PASS"  # noqa: S105 - a QA verdict, not a credential
    WARNING = "WARNING"
    ERROR = "ERROR"
    CONFLICT = "CONFLICT"
    MISSING = "MISSING"


class CalculationStatus(StrEnumLike):
    """Status of an engineering calculation record (section 25)."""

    DRAFT = "DRAFT"
    COMPUTED = "COMPUTED"
    CHECKED = "CHECKED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class ClaimType(StrEnumLike):
    """Provenance class of a statement in an AI answer (section 22)."""

    SOURCE_DERIVED = "SOURCE_DERIVED"
    CALCULATED = "CALCULATED"
    USER_PROVIDED = "USER_PROVIDED"
    AI_INFERRED = "AI_INFERRED"


class SourceAuthority(StrEnumLike):
    """Default (configurable) source authority tiers (section 83)."""

    APPROVED_DRILLING_PROGRAM = "approved_drilling_program"
    APPROVED_ENGINEERING_DOCUMENT = "approved_engineering_document"
    CURRENT_OPERATIONAL_REPORT = "current_operational_report"
    CURRENT_PROGRAM_REVISION = "current_program_revision"
    PREVIOUS_REVISION = "previous_revision"
    HISTORICAL_REPORT = "historical_report"
    TECHNICAL_REFERENCE = "technical_reference"
    GENERAL_KNOWLEDGE = "general_knowledge"
