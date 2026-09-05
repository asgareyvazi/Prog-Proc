"""Persistence layer: SQLAlchemy models, engine, sessions, Alembic integration.

The schema is portable SQLite/PostgreSQL: no SQLite-only SQL in the ORM layer, and
the search index lives in a separate rebuildable database.

Two policies are installed with the package, because they must hold for *every* session
in the process rather than only for sessions opened through a repository:

*   :mod:`.audit` - ``audit_event`` is append-only (UPDATE/DELETE raise);
*   :mod:`.integrity` - the cross-row invariants the schema cannot express
    (exactly one current document version, knowledge-edge endpoints resolve), each with
    a checker that a repair pass or the status bar can run.
"""

from .audit import AuditLog, AuditPolicyError, install_append_only_policy
from .base import Base
from .engine import build_engine, database_dialect, sqlite_version
from .integrity import (
    IntegrityProblem,
    KnowledgeIntegrityError,
    check_current_version_invariants,
    check_extraction_cache,
    check_knowledge_relations,
    require_current_version_invariants,
    validate_knowledge_relation,
)
from .models import (
    AuditEvent,
    Calculation,
    CalculationInput,
    Company,
    Document,
    DocumentVersion,
    Extraction,
    ExtractionCache,
    Field,
    IngestionRun,
    KnowledgeConflict,
    KnowledgeItem,
    KnowledgeRelation,
    Project,
    Skill,
    SkillVersion,
    Source,
    Well,
    WellSection,
    Workspace,
)
from .session import Database

__all__ = [
    "AuditEvent",
    "AuditLog",
    "AuditPolicyError",
    "Base",
    "Calculation",
    "CalculationInput",
    "Company",
    "Database",
    "Document",
    "DocumentVersion",
    "Extraction",
    "ExtractionCache",
    "Field",
    "IngestionRun",
    "IntegrityProblem",
    "KnowledgeConflict",
    "KnowledgeIntegrityError",
    "KnowledgeItem",
    "KnowledgeRelation",
    "Project",
    "Skill",
    "SkillVersion",
    "Source",
    "Well",
    "WellSection",
    "Workspace",
    "build_engine",
    "check_current_version_invariants",
    "check_extraction_cache",
    "check_knowledge_relations",
    "database_dialect",
    "install_append_only_policy",
    "require_current_version_invariants",
    "sqlite_version",
    "validate_knowledge_relation",
]
