"""Persistence layer: SQLAlchemy models, engine, sessions, Alembic integration.

The schema is portable SQLite/PostgreSQL: no SQLite-only SQL in the ORM layer, and
the search index lives in a separate rebuildable database.
"""

from .base import Base
from .engine import build_engine, database_dialect, sqlite_version
from .models import (
    AuditEvent,
    Calculation,
    CalculationInput,
    Company,
    Document,
    DocumentVersion,
    Extraction,
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
    "Base",
    "Calculation",
    "CalculationInput",
    "Company",
    "Database",
    "Document",
    "DocumentVersion",
    "Extraction",
    "Field",
    "IngestionRun",
    "KnowledgeConflict",
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
    "database_dialect",
    "sqlite_version",
]
