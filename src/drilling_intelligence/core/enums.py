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
    """Lifecycle of a knowledge object.

    ``CONFLICTED`` is what two sources that disagree leave behind: both objects keep their value
    and neither is deleted (section 19).  ``UNVERIFIED`` marks a value the platform holds but
    cannot point at a source for - a manual note, an inference whose evidence has not been
    recorded - so "no provenance" is a state a query can find, not an absence.
    """

    CANDIDATE = "CANDIDATE"
    #: Agreed with by every source that mentions it, and traceable to at least one.
    ACTIVE = "ACTIVE"
    #: At least two sources give different values for the same subject and property.
    CONFLICTED = "CONFLICTED"
    #: A newer revision of the same source says otherwise; the value stays queryable.
    SUPERSEDED = "SUPERSEDED"
    #: Not source-derived (or its evidence could not be recorded): nobody should act on it.
    UNVERIFIED = "UNVERIFIED"
    RETIRED = "RETIRED"


class KnowledgeRelationType(StrEnumLike):
    """Directed edge vocabulary for the knowledge graph (section 20)."""

    WELL_HAS_DOCUMENT = "WELL_HAS_DOCUMENT"
    #: The mud a well was actually drilled with, as opposed to the one a program planned.
    WELL_HAS_MUD = "WELL_HAS_MUD"
    #: A problem or event the well ran into (lost circulation, a kick, NPT), cited to its source.
    WELL_ENCOUNTERED_EVENT = "WELL_ENCOUNTERED_EVENT"
    #: A document refers to a well without the workspace having linked them yet.
    DOCUMENT_MENTIONS_WELL = "DOCUMENT_MENTIONS_WELL"
    #: Facts are attributed to the *version* that contains them, not the document, so a newer
    #: revision never rewrites what an older one said.
    VERSION_CONTAINS_KNOWLEDGE = "VERSION_CONTAINS_KNOWLEDGE"
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
    # -- the operational and engineering record (ADR-0010) -----------------
    # A report is evidence; the rows promoted out of it are records.  Both directions are queryable
    # because a reader follows either: "what did this CSV become?" and "where did this NPT come from?".
    REPORT_CONTAINS_OPERATION = "REPORT_CONTAINS_OPERATION"
    REPORT_CONTAINS_EVENT = "REPORT_CONTAINS_EVENT"
    REPORT_CONTAINS_KNOWLEDGE = "REPORT_CONTAINS_KNOWLEDGE"
    OPERATION_HAS_EVENT = "OPERATION_HAS_EVENT"
    OPERATION_USED_RIG = "OPERATION_USED_RIG"
    OPERATION_USED_SERVICE = "OPERATION_USED_SERVICE"
    PROBLEM_CAUSES_NPT = "PROBLEM_CAUSES_NPT"
    EVENT_HAS_PROBLEM = "EVENT_HAS_PROBLEM"
    PROBLEM_CITED_BY_LESSON = "PROBLEM_CITED_BY_LESSON"
    #: An asserted offset relationship: a person (or a program) decided these two wells are
    #: comparable, which is a different claim from "they are in the same field".
    WELL_IS_OFFSET_OF = "WELL_IS_OFFSET_OF"
    WELL_USED_RIG = "WELL_USED_RIG"
    WELL_USED_SERVICE = "WELL_USED_SERVICE"
    #: The "based on" tree of a procedure: what it was written against, in the words of the
    #  procedure's own author, with the field that named it as provenance.
    PROCEDURE_BASED_ON_PROGRAM = "PROCEDURE_BASED_ON_PROGRAM"
    PROCEDURE_CITES_STANDARD = "PROCEDURE_CITES_STANDARD"
    PROCEDURE_CITES_DOCUMENT = "PROCEDURE_CITES_DOCUMENT"
    PROCEDURE_ADDRESSES_LESSON = "PROCEDURE_ADDRESSES_LESSON"
    PROCEDURE_ADDRESSES_RISK = "PROCEDURE_ADDRESSES_RISK"
    PROCEDURE_USES_CALCULATION = "PROCEDURE_USES_CALCULATION"
    PROCEDURE_REQUIRES_KNOWLEDGE = "PROCEDURE_REQUIRES_KNOWLEDGE"
    PROCEDURE_OBSERVES_WELL = "PROCEDURE_OBSERVES_WELL"
    PROGRAM_CONTAINS_PROCEDURE = "PROGRAM_CONTAINS_PROCEDURE"
    PROGRAM_USES_CALCULATION = "PROGRAM_USES_CALCULATION"
    PROGRAM_LEARNED_FROM_LESSON = "PROGRAM_LEARNED_FROM_LESSON"
    PROGRAM_ADDRESSES_RISK = "PROGRAM_ADDRESSES_RISK"
    PROGRAM_OFFSETS_WELL = "PROGRAM_OFFSETS_WELL"
    LESSON_DERIVED_FROM_EVENT = "LESSON_DERIVED_FROM_EVENT"
    LESSON_CITES_EVIDENCE = "LESSON_CITES_EVIDENCE"
    LESSON_RECOMMENDS_PROCEDURE = "LESSON_RECOMMENDS_PROCEDURE"
    LESSON_BEST_PRACTICE = "LESSON_BEST_PRACTICE"
    RISK_DERIVED_FROM_PROBLEM = "RISK_DERIVED_FROM_PROBLEM"
    RISK_MITIGATED_BY_PROCEDURE = "RISK_MITIGATED_BY_PROCEDURE"
    RISK_CITES_EVIDENCE = "RISK_CITES_EVIDENCE"
    PATTERN_SEEN_IN_WELL = "PATTERN_SEEN_IN_WELL"
    PATTERN_CITES_EVIDENCE = "PATTERN_CITES_EVIDENCE"
    ITEM_CONFLICTS_WITH = "ITEM_CONFLICTS_WITH"
    ITEM_SUPPORTS = "ITEM_SUPPORTS"
    DOCUMENT_HAS_VERSION = "DOCUMENT_HAS_VERSION"
    SKILL_USES_KNOWLEDGE = "SKILL_USES_KNOWLEDGE"


class KnowledgeOrigin(StrEnumLike):
    """Who is responsible for a knowledge object existing.

    This is what makes a rebuild safe: ``rebuild`` regenerates the derived world from the
    extractions it can re-run, and must never touch what a person wrote.  Without an origin on
    the row, "rebuild the knowledge" and "delete the user's notes" are the same operation.
    """

    #: Read out of a document by a deterministic extractor, provenance included.
    EXTRACTED = "EXTRACTED"
    #: Recalculated or linked by the platform from other knowledge (a conflict record, a link).
    DERIVED = "DERIVED"
    #: Typed in by a person; no automated process may overwrite or delete it.
    MANUAL = "MANUAL"


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


# --------------------------------------------------------------------------- operations
class ConfirmationStatus(StrEnumLike):
    """How sure the platform is that a record it made is right.

    The operational spine is populated from two directions - a deterministic promotion out of a
    stored artefact, and a person writing what actually happened - and the two must never look the
    same in the database.  A promoted row is ``CANDIDATE`` until somebody reads the source and
    confirms it; nothing a script wrote becomes ``CONFIRMED`` on its own, which is the difference
    between a head start and a fabrication.
    """

    #: Written by promotion from an artefact, or drafted by a person, not yet vouched for.
    CANDIDATE = "CANDIDATE"
    #: A person (or an approved document) has vouched for it.
    CONFIRMED = "CONFIRMED"
    #: Somebody looked and said this record is wrong.  Kept, never deleted: a rejected event is
    #: evidence that the source claims something the field did not see.
    REJECTED = "REJECTED"


class CauseStatus(StrEnumLike):
    """The epistemic state of a cause or root cause on an event, NPT or problem record.

    A cause is not a number: it is a claim about why something happened, and the platform's only
    honest options are to say where it came from or to say it does not know.  ``UNKNOWN`` is a
    value people can act on (it means "go and ask"); an empty string is not, and a guessed one is
    worse than both.
    """

    #: Stated as such by the source, with provenance attached.
    KNOWN = "KNOWN"
    #: The platform inferred it from a rule it can name (an event code, a category mapping).
    INFERRED = "INFERRED"
    #: Nobody has said.  The default, and the honest one.
    UNKNOWN = "UNKNOWN"
    #: Sources disagree, and the disagreement is recorded rather than resolved (ADR-0008).
    CONFLICTED = "CONFLICTED"


class DurationBasis(StrEnumLike):
    """Where a duration on a record came from.

    "6.5 hours" written in a report's NPT column is not the same kind of claim as "the interval
    between the two timestamps in this row", and neither is the same as "the report day minus the
    non-productive line".  Aggregating them without saying which is which is how a field total
    acquires a precision it never had.
    """

    #: Quoted by the source as a duration.
    STATED = "STATED"
    #: Computed from two timestamps the source gave.
    MEASURED = "MEASURED"
    #: Computed by the platform from partial information (start only, a day, a shift).
    DERIVED = "DERIVED"


class SeverityLevel(StrEnumLike):
    """Event severity, as a word rather than a number.

    Deliberately not numeric: a report says "severe" or it says nothing, and turning the word into
    a 4 invites arithmetic on it.  ``severity_score`` on a record is the ranking the platform
    applies to those words, not a measurement.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}[self.value]


#: The words sources actually use, mapped onto the four the platform recognises.  Anything not here
#: leaves severity ``None``: an unrecognised word is reported, not approximated.
SEVERITY_ALIASES: dict[str, SeverityLevel] = {
    "minor": SeverityLevel.LOW,
    "low": SeverityLevel.LOW,
    "slight": SeverityLevel.LOW,
    "moderate": SeverityLevel.MEDIUM,
    "medium": SeverityLevel.MEDIUM,
    "significant": SeverityLevel.MEDIUM,
    "major": SeverityLevel.HIGH,
    "high": SeverityLevel.HIGH,
    "serious": SeverityLevel.HIGH,
    "severe": SeverityLevel.HIGH,
    "critical": SeverityLevel.CRITICAL,
    "catastrophic": SeverityLevel.CRITICAL,
    "fatal": SeverityLevel.CRITICAL,
    "loss of well": SeverityLevel.CRITICAL,
}


# --------------------------------------------------------------------------- lifecycle
class ProcedureLifecycle(StrEnumLike):
    """A procedure's state in its own review process (architecture section: procedures).

    This is not :class:`DocumentStatus`: a procedure is an engineering record that happens to
    have a document, and the record's approval state is asked about on its own terms - "which
    revision was approved" is a question about the procedure, and a file named ``Rev B`` is not an
    answer to it.
    """

    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"


PROCEDURE_LIFECYCLE_TRANSITIONS: dict[ProcedureLifecycle, tuple[ProcedureLifecycle, ...]] = {
    ProcedureLifecycle.DRAFT: (
        ProcedureLifecycle.IN_REVIEW,
        ProcedureLifecycle.APPROVED,
        ProcedureLifecycle.WITHDRAWN,
    ),
    ProcedureLifecycle.IN_REVIEW: (
        ProcedureLifecycle.DRAFT,
        ProcedureLifecycle.APPROVED,
        ProcedureLifecycle.WITHDRAWN,
    ),
    # An approved procedure changes by revision, never in place, so APPROVED only leads on.
    ProcedureLifecycle.APPROVED: (ProcedureLifecycle.SUPERSEDED, ProcedureLifecycle.WITHDRAWN),
    ProcedureLifecycle.SUPERSEDED: (),
    ProcedureLifecycle.WITHDRAWN: (),
}


class ProgramLifecycle(StrEnumLike):
    """A drilling program's state.  ``ARCHIVED`` rather than ``WITHDRAWN``: a program that was
    drilled to stays as built, it is not retracted."""

    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"


PROGRAM_LIFECYCLE_TRANSITIONS: dict[ProgramLifecycle, tuple[ProgramLifecycle, ...]] = {
    ProgramLifecycle.DRAFT: (
        ProgramLifecycle.IN_REVIEW,
        ProgramLifecycle.APPROVED,
        ProgramLifecycle.ARCHIVED,
    ),
    ProgramLifecycle.IN_REVIEW: (
        ProgramLifecycle.DRAFT,
        ProgramLifecycle.APPROVED,
        ProgramLifecycle.ARCHIVED,
    ),
    ProgramLifecycle.APPROVED: (ProgramLifecycle.SUPERSEDED, ProgramLifecycle.ARCHIVED),
    ProgramLifecycle.SUPERSEDED: (ProgramLifecycle.ARCHIVED,),
    ProgramLifecycle.ARCHIVED: (),
}


class LessonLifecycle(StrEnumLike):
    """A lesson's review state.  ``REJECTED`` exists because refusing to promote a bad lesson is
    the only thing that makes ``APPROVED`` mean something."""

    DRAFT = "DRAFT"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


LESSON_LIFECYCLE_TRANSITIONS: dict[LessonLifecycle, tuple[LessonLifecycle, ...]] = {
    LessonLifecycle.DRAFT: (
        LessonLifecycle.REVIEW,
        LessonLifecycle.APPROVED,
        LessonLifecycle.REJECTED,
    ),
    LessonLifecycle.REVIEW: (
        LessonLifecycle.DRAFT,
        LessonLifecycle.APPROVED,
        LessonLifecycle.REJECTED,
    ),
    LessonLifecycle.APPROVED: (LessonLifecycle.SUPERSEDED,),
    LessonLifecycle.REJECTED: (LessonLifecycle.DRAFT,),
    LessonLifecycle.SUPERSEDED: (),
}


class RiskLifecycle(StrEnumLike):
    """Whether a risk is still live.  Nothing here scores the risk: severity is a separate,
    computed pair of columns."""

    OPEN = "OPEN"
    MITIGATED = "MITIGATED"
    CLOSED = "CLOSED"
    SUPERSEDED = "SUPERSEDED"


RISK_LIFECYCLE_TRANSITIONS: dict[RiskLifecycle, tuple[RiskLifecycle, ...]] = {
    RiskLifecycle.OPEN: (RiskLifecycle.MITIGATED, RiskLifecycle.CLOSED, RiskLifecycle.SUPERSEDED),
    RiskLifecycle.MITIGATED: (RiskLifecycle.OPEN, RiskLifecycle.CLOSED, RiskLifecycle.SUPERSEDED),
    RiskLifecycle.CLOSED: (RiskLifecycle.OPEN,),
    RiskLifecycle.SUPERSEDED: (),
}


class RecommendationLifecycle(StrEnumLike):
    """What a person has decided about a generated recommendation.

    A recommendation starts life as a statement the platform derived from records; the only thing that
    moves it out of ``PROPOSED`` is a decision by someone who owns the operation.  ``IMPLEMENTED`` is
    separate from ``ACCEPTED`` on purpose: agreeing with advice and having put it into a procedure are
    two different facts, and the second one is the one a reader of a program wants to know.
    """

    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    IMPLEMENTED = "IMPLEMENTED"
    SUPERSEDED = "SUPERSEDED"


RECOMMENDATION_LIFECYCLE_TRANSITIONS: dict[
    RecommendationLifecycle, tuple[RecommendationLifecycle, ...]
] = {
    RecommendationLifecycle.PROPOSED: (
        RecommendationLifecycle.ACCEPTED,
        RecommendationLifecycle.DECLINED,
        RecommendationLifecycle.SUPERSEDED,
    ),
    RecommendationLifecycle.ACCEPTED: (
        RecommendationLifecycle.IMPLEMENTED,
        RecommendationLifecycle.DECLINED,
        RecommendationLifecycle.SUPERSEDED,
    ),
    # A declined recommendation can come back: the reason for declining it was usually "not now",
    # and a rule that made decline permanent teaches people to leave proposals unacknowledged.
    RecommendationLifecycle.DECLINED: (
        RecommendationLifecycle.PROPOSED,
        RecommendationLifecycle.SUPERSEDED,
    ),
    RecommendationLifecycle.IMPLEMENTED: (RecommendationLifecycle.SUPERSEDED,),
    RecommendationLifecycle.SUPERSEDED: (),
}


# --------------------------------------------------------------------------- vocabularies
#: Operations, in the words the industry uses.  These are *not* a closed set - see
#: :func:`~drilling_intelligence.core.vocabulary.operation_type` - the list exists so that ``drilling`` and ``Drilling`` and
#: ``drilling/2nd section`` reach the same token, and so a report can be asked for "the tripping
#: events" without the caller knowing which spelling the source used.
KNOWN_OPERATION_TYPES: frozenset[str] = frozenset(
    {
        "drilling",
        "tripping",
        "running_casing",
        "cementing",
        "circulating",
        "conditioning",
        "logging",
        "wireline",
        "directional",
        "survey",
        "well_control",
        "sidetracking",
        "reaming",
        "back_reaming",
        "cleaning",
        "bit_change",
        "bha_change",
        "pressure_test",
        "leak_test",
        "logging_while_drilling",
        "completion",
        "abandonment",
        "mobilisation",
        "demobilisation",
        "waiting_on_weather",
        "waiting_on_company",
    }
)

#: Spellings that mean one of the tokens above.
OPERATION_ALIASES: dict[str, str] = {
    "drill": "drilling",
    "drilling_": "drilling",
    "trip": "tripping",
    "trip_out": "tripping",
    "trip_in": "tripping",
    "tripping_out": "tripping",
    "tripping_in": "tripping",
    "casing": "running_casing",
    "run_casing": "running_casing",
    "running casing": "running_casing",
    "cement": "cementing",
    "cement job": "cementing",
    "circulation": "circulating",
    "condition": "conditioning",
    "log": "logging",
    "lwd": "logging_while_drilling",
    "mwd": "directional",
    "survey": "survey",
    "directional survey": "directional",
    "kick": "well_control",
    "sidetrack": "sidetracking",
    "ream": "reaming",
    "back ream": "back_reaming",
    "hole_cleaning": "cleaning",
    "cleanout": "cleaning",
    "bit_run_change": "bit_change",
    "change_bha": "bha_change",
    "pressure test": "pressure_test",
    "fit": "leak_test",
    "complete": "completion",
    "plug_and_abandon": "abandonment",
    "pab": "abandonment",
}

#: Event categories.  ``npt`` is a category and not a type: a lost-circulation event that cost
#: six hours is one event with an NPT record attached, and counting them as two is how a field
#: total doubles.
KNOWN_EVENT_CATEGORIES: frozenset[str] = frozenset(
    {
        "npt",
        "equipment",
        "well_control",
        "safety",
        "geological",
        "mud",
        "hole_condition",
        "directional",
        "data_quality",
        "logistics",
        "personnel",
        "environmental",
        "other",
    }
)

#: Problems, as the reporting vocabulary knows them.  Again open: a problem type nobody has
#: registered keeps its own token rather than being folded into "other".
PROBLEM_TYPES: frozenset[str] = frozenset(
    {
        "stuck_pipe",
        "lost_circulation",
        "poor_hole_cleaning",
        "bedding",
        "bha_failure",
        "bit_failure",
        "cement_failure",
        "casing_failure",
        "equipment_failure",
        "washout",
        "twist_off",
        "well_control_incident",
        "kick",
        "lost_returns",
        "excessive_torque",
        "excessive_drag",
        "poor_rop",
        "formation_instability",
        "hole_caving",
        "differential_sticking",
        "mud_problem",
        "directional_uncertainty",
        "logging_failure",
        "log_quality",
    }
)

#: The reason codes a report writes, onto the problem the platform aggregates by.  A code that is
#: not here keeps its own normalised token: an unrecognised code is a category nobody has
#: registered yet, not "other".
PROBLEM_CODE_ALIASES: dict[str, str] = {
    "npt-stuck": "stuck_pipe",
    "npt_stuck": "stuck_pipe",
    "stuck": "stuck_pipe",
    "stuck bit": "stuck_pipe",
    "st pipe": "stuck_pipe",
    "npt-lc": "lost_circulation",
    "npt_lost": "lost_circulation",
    "lc": "lost_circulation",
    "lost circulation": "lost_circulation",
    "lost returns": "lost_returns",
    "npt-equip": "equipment_failure",
    "npt_equipment": "equipment_failure",
    "equipment failure": "equipment_failure",
    "npt-hole": "poor_hole_cleaning",
    "hole cleaning": "poor_hole_cleaning",
    "npt-well-control": "well_control_incident",
    "kick": "kick",
    "npt-other": "other",
    "other": "other",
    "npt": "other",
}

#: Cost categories (architecture: cost / CBS).  Open vocabulary again, with the codes a CBS
#: export uses folded in.
COST_CATEGORIES: frozenset[str] = frozenset(
    {
        "rig",
        "drilling_services",
        "materials",
        "personnel",
        "logistics",
        "fuel",
        "cement",
        "mud",
        "bits",
        "downhole_tools",
        "directional",
        "logging",
        "testing",
        "contingency",
        "npt_recovery",
        "other",
    }
)

COST_CATEGORY_ALIASES: dict[str, str] = {
    "day rate": "rig",
    "dayrate": "rig",
    "rental": "rig",
    "services": "drilling_services",
    "consumables": "materials",
    "labour": "personnel",
    "crewing": "personnel",
    "transport": "logistics",
    "freight": "logistics",
    "diesel": "fuel",
    "cementing": "cement",
    "drilling fluids": "mud",
    "bits": "bits",
    "mud motors": "downhole_tools",
    "mwd": "directional",
    "wireline": "logging",
    "drill stem test": "testing",
    "npt": "npt_recovery",
}
