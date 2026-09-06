"""SQLAlchemy ORM models: the schema of the system of record.

These entities are the Phase-0 domain model (docs/DECISIONS.md ADR-0006):
rich enough to enforce the invariants the platform depends on - well-centric
identity, immutable document versions, provenance, knowledge objects,
deterministic calculation records - while staying portable across dialects.

Invariants enforced here rather than "by convention" in service code:

*   every document belongs to a workspace and is optionally linked to a well;
*   a document has **exactly one** current version: the partial unique index
    ``uq_document_version_one_current`` refuses a second ``is_current`` row per
    document, ``document.current_version_id`` is a real (deferred) foreign key to
    ``document_version.id``, and :mod:`drilling_intelligence.database.integrity`
    checks the whole-graph rule - "exactly one current, the pointer names it, and no
    superseded version still claims to be current" - which no relational schema can
    express as a column constraint;
*   the extraction cache is content-addressed and unique: one ``extraction_cache``
    row per ``(content_sha256, extractor, extractor_version, config_hash)``;
*   ``audit_event`` is append-only, enforced by ORM guards (see ``AuditEvent``);
*   content hash uniqueness is *not* enforced on documents (duplicates across
    wells are legitimate) but *is* enforced per version to keep dedupe cheap;
*   extracted knowledge always carries a source and provenance payload;
*   calculations record their inputs, method version and validation output so
    an engineering decision can be reconstructed later.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


# --------------------------------------------------------------------------- hierarchy
class Company(Base, TimestampMixin):
    __tablename__ = "company"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    code: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)

    projects: Mapped[list[Project]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class Project(Base, TimestampMixin):
    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_project_company_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("company.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)

    company: Mapped[Company | None] = relationship(back_populates="projects")
    fields: Mapped[list[Field]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    wells: Mapped[list[Well]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Field(Base, TimestampMixin):
    __tablename__ = "field"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    basin: Mapped[str | None] = mapped_column(String(120))
    offshore: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project | None] = relationship(back_populates="fields")
    wells: Mapped[list[Well]] = relationship(back_populates="field")


# --------------------------------------------------------------------------- wells
class Well(Base, TimestampMixin):
    __tablename__ = "well"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_well_project_name"),
        Index("ix_well_status", "lifecycle_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    field_id: Mapped[str | None] = mapped_column(ForeignKey("field.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Operator/partner designation as written on reports (used for matching offsets).
    well_identifier: Mapped[str | None] = mapped_column(String(120))
    lifecycle_status: Mapped[str] = mapped_column(String(24), default="PLANNED", nullable=False)
    well_type: Mapped[str | None] = mapped_column(
        String(40)
    )  # exploration/appraisal/development/...
    trajectory_type: Mapped[str | None] = mapped_column(String(40))  # vertical/planar/3d/horizontal
    spud_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Numeric engineering attributes are stored as (value, unit) pairs so the
    #: unit is never lost - see docs/DATA_MODEL.md "Value with unit".
    total_depth_md_value: Mapped[float | None] = mapped_column(Float)
    total_depth_md_unit: Mapped[str] = mapped_column(String(16), default="m")
    total_depth_tvd_value: Mapped[float | None] = mapped_column(Float)
    total_depth_tvd_unit: Mapped[str] = mapped_column(String(16), default="m")
    kb_elevation_value: Mapped[float | None] = mapped_column(Float)
    kb_elevation_unit: Mapped[str] = mapped_column(String(16), default="m")
    surface_x_value: Mapped[float | None] = mapped_column(Float)
    surface_y_value: Mapped[float | None] = mapped_column(Float)
    coordinate_system: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    #: Free-form attributes for things we refuse to guess a schema for yet.
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)

    project: Mapped[Project | None] = relationship(back_populates="wells")
    field: Mapped[Field | None] = relationship(back_populates="wells")
    sections: Mapped[list[WellSection]] = relationship(
        back_populates="well", cascade="all, delete-orphan", order_by="WellSection.sequence"
    )
    documents: Mapped[list[Document]] = relationship(back_populates="well")


class WellSection(Base, TimestampMixin):
    """A hole section: the unit of most drilling engineering reasoning."""

    __tablename__ = "well_section"
    __table_args__ = (UniqueConstraint("well_id", "name", name="uq_well_section_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    well_id: Mapped[str] = mapped_column(ForeignKey("well.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)  # e.g. '12 1/4" Intermediate'
    #: Nominal hole size, stored in inches (industry convention for nominal sizes).
    hole_size_in: Mapped[float | None] = mapped_column(Float)
    casing_program: Mapped[str | None] = mapped_column(String(120))
    top_depth_value: Mapped[float | None] = mapped_column(Float)
    top_depth_unit: Mapped[str] = mapped_column(String(16), default="m")
    bottom_depth_value: Mapped[float | None] = mapped_column(Float)
    bottom_depth_unit: Mapped[str] = mapped_column(String(16), default="m")
    #: planned vs actual stay separate columns - never overwritten in place (section 11).
    planned_duration_days: Mapped[float | None] = mapped_column(Float)
    actual_duration_days: Mapped[float | None] = mapped_column(Float)
    planned_mud_weight_value: Mapped[float | None] = mapped_column(Float)
    planned_mud_weight_unit: Mapped[str] = mapped_column(String(16), default="ppg")
    actual_mud_weight_value: Mapped[float | None] = mapped_column(Float)
    actual_mud_weight_unit: Mapped[str] = mapped_column(String(16), default="ppg")
    formation_top: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)

    well: Mapped[Well] = relationship(back_populates="sections")


# --------------------------------------------------------------------------- documents
class Workspace(Base, TimestampMixin):
    """A registered on-disk project workspace (the unit the user scans)."""

    __tablename__ = "workspace"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    data_dir: Mapped[str] = mapped_column(String(1024), nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Free-form scan settings overrides (folder includes/excludes).
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)


class Document(Base, TimestampMixin):
    """Registry entry for one document slot (path identity) in a workspace."""

    __tablename__ = "document"
    __table_args__ = (
        UniqueConstraint("workspace_id", "identity_path", name="uq_document_workspace_identity"),
        Index("ix_document_well", "well_id"),
        Index("ix_document_classification", "classification"),
        Index("ix_document_status", "processing_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace.id", ondelete="SET NULL")
    )
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    #: Normalised path within the workspace; identity, not content (section 13).
    identity_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    extension: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Hash of the *current* content (also carried per version for history).
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Genuine creation time only, i.e. where the platform reports one (macOS/BSD
    #: ``st_birthtime``, Windows ``st_ctime``).  On Linux this stays ``NULL`` rather
    #: than pretending the inode change time is a creation date - see
    #: :mod:`drilling_intelligence.core.filesystem`.
    file_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: ``st_ctime`` under its real name: when the *inode* last changed (metadata,
    #: rename, link count) on POSIX.  Recorded for forensics, never presented as a
    #: creation or revision date.
    fs_metadata_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    #: Document-level metadata that survives a revision change.
    classification: Mapped[str] = mapped_column(String(40), default="OTHER", nullable=False)
    classification_confidence: Mapped[float | None] = mapped_column(Float)
    title: Mapped[str | None] = mapped_column(String(400))
    document_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[str | None] = mapped_column(String(64))
    revision_key: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", nullable=False)
    #: Authority tier used for conflict resolution (configurable ladder).
    source_authority: Mapped[str | None] = mapped_column(String(64))
    wellbore: Mapped[str | None] = mapped_column(String(80))
    interval_from: Mapped[str | None] = mapped_column(String(40))
    interval_to: Mapped[str | None] = mapped_column(String(40))
    processing_status: Mapped[str] = mapped_column(String(24), default="DISCOVERED", nullable=False)
    processing_error: Mapped[str | None] = mapped_column(Text)
    #: Which version the registry currently points at.  A real foreign key, deferred
    #: because document <-> document_version is a cycle: the pointer is written after
    #: the version row exists, and deleting the version nulls the pointer instead of
    #: deleting the document.  Deferred constraints are standard SQL (SQLite >= 3.6.19
    #: and PostgreSQL both enforce them at COMMIT), so this is not a SQLite-only trick.
    current_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "document_version.id", ondelete="SET NULL", deferrable=True, initially="DEFERRED"
        )
    )
    #: Number of times this slot was seen with different content.
    change_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tags: Mapped[list | None] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    well: Mapped[Well | None] = relationship(back_populates="documents")
    #: ``foreign_keys`` is required because the cycle gives the two tables two
    #: join paths; the *history* link is ``document_version.document_id``.
    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentVersion.version_number",
        foreign_keys="DocumentVersion.document_id",
    )


class DocumentVersion(Base, TimestampMixin):
    """Immutable snapshot of one content state of a document (section 14)."""

    __tablename__ = "document_version"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_version_number"),
        Index("ix_document_version_sha", "sha256"),
        # At most one "current" version per document, at the database level.  A
        # partial unique index is the portable way to say that: SQLite and
        # PostgreSQL both support it (MySQL would simply ignore the predicate and
        # keep a plain unique index, which is the same rule with the same meaning).
        Index(
            "uq_document_version_one_current",
            "document_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Human revision label as authored ("Rev 2", "Rev B").  Never invented.
    revision: Mapped[str | None] = mapped_column(String(64))
    revision_key: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revision_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", nullable=False)
    #: Absolute path as recorded on the machine that scanned it.  Convenient, not
    #: durable - a workspace folder that is moved or copied makes it stale.
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    #: The durable citation: path inside the workspace, forward slashes, no drive
    #: letter.  Resolved against whichever workspace root is open now, which is what
    #: keeps provenance readable after a relocation (section 85).
    source_relative_path: Mapped[str | None] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    file_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mime_type: Mapped[str] = mapped_column(String(128), default="")
    #: How the content was read, so a re-extraction is reproducible or explainable.
    parser: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    extraction_version: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    page_count: Mapped[int | None] = mapped_column(Integer)
    sheet_count: Mapped[int | None] = mapped_column(Integer)
    word_count: Mapped[int | None] = mapped_column(Integer)
    #: Provenance of the supersede chain (section 14).
    supersedes_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    superseded_by_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    #: 'NEW' / 'MODIFIED' / 'DUPLICATE' - why this version exists (section 13).
    origin: Mapped[str] = mapped_column(String(16), default="NEW", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Approval information is kept on the version, since that is what is approved.
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Duplicate handling: pointer to the version holding identical content.
    duplicate_of_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON, default=dict)

    document: Mapped[Document] = relationship(
        back_populates="versions", foreign_keys="DocumentVersion.document_id"
    )
    extractions: Mapped[list[Extraction]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class Extraction(Base, TimestampMixin):
    """Normalised content produced by one extractor for one document version.

    One row *per version* - deliberately not one row per cache key.  A version's
    artefact is immutable history: what this version was read as, with what parser,
    at what time.  Several versions may legitimately hold the same content (a copied
    file, a document that moved, a reprocess after a parser fix), and each keeps its
    own row.  The *cache* - the "identify and reuse this artefact" part - is the
    unique :class:`ExtractionCache` row, which is keyed by content and never by
    version.  So uniqueness lives where it belongs, and history stays intact.
    """

    __tablename__ = "extraction"
    __table_args__ = (
        # Speeds the "which artefacts exist for these bytes" lookup.  Not unique:
        # uniqueness of the cache key is the job of extraction_cache.
        Index(
            "ix_extraction_cache", "content_sha256", "extractor", "extractor_version", "config_hash"
        ),
        Index("ix_extraction_version", "document_version_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_version.id", ondelete="CASCADE"), nullable=False
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor: Mapped[str] = mapped_column(String(48), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(48), nullable=False)
    #: Stable hash of extractor options that change output; part of the cache key.
    config_hash: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    #: Why this extractor was chosen (router decision, including fallbacks taken).
    router_decision: Mapped[dict | None] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="OK", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    stats: Mapped[dict | None] = mapped_column(JSON, default=dict)
    #: Full NormalizedDocument (metadata/pages/sections/tables/fields/provenance).
    document_json: Mapped[dict | None] = mapped_column(JSON)
    #: Large text is kept out of the JSON blob to keep rows small.
    text_blob: Mapped[str | None] = mapped_column(Text)

    version: Mapped[DocumentVersion] = relationship(back_populates="extractions")


class ExtractionCache(Base, TimestampMixin):
    """Content-addressed extraction cache: one row per extraction *key*, never per version.

    ``uq_extraction_cache_key`` is the whole point of this table: ``(content_sha256,
    extractor, extractor_version, config_hash)`` identifies exactly one artefact, so
    two concurrent ingestion runs cannot both claim to be the keeper of that artefact
    (the loser hits the unique constraint and re-reads the winner).  ``document_version_id``
    is *not* part of the key - the same bytes read by the same extractor under the same
    options are the same artefact no matter how many document versions cite it - and it
    is not part of this table at all.

    ``extraction_id`` points at the artefact row that *produced* the cached result (the
    first producer), so the payload is read through it rather than copied.  If that row
    is deleted the entry goes with it (``ON DELETE CASCADE``) and the next ingestion
    re-extracts - a stale pointer is never served.
    """

    __tablename__ = "extraction_cache"
    __table_args__ = (
        UniqueConstraint(
            "content_sha256",
            "extractor",
            "extractor_version",
            "config_hash",
            name="uq_extraction_cache_key",
        ),
        Index("ix_extraction_cache_sha", "content_sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor: Mapped[str] = mapped_column(String(48), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(48), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    #: The stored artefact this key resolves to.
    extraction_id: Mapped[str | None] = mapped_column(
        ForeignKey("extraction.id", ondelete="CASCADE")
    )
    #: How many versions have reused this artefact instead of re-parsing it.
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The first version this artefact was produced for (provenance of the cache entry).
    produced_by_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    #: True when the entry was written by a forced re-extraction replacing an older one.
    refreshed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# --------------------------------------------------------------------------- knowledge
class Source(Base, TimestampMixin):
    """A citable source of information with its (configurable) authority tier."""

    __tablename__ = "source"
    __table_args__ = (UniqueConstraint("kind", "reference", name="uq_source_kind_reference"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # document|manual|calculation|inference|reference
    #: Stable citation key: document version id, book ISBN/section, user id...
    reference: Mapped[str] = mapped_column(String(512), nullable=False)
    label: Mapped[str] = mapped_column(String(400), nullable=False)
    authority_tier: Mapped[str] = mapped_column(
        String(64), default="general_knowledge", nullable=False
    )
    #: 0-100; lets a reviewer override the tier ordering deterministically.
    authority_score: Mapped[float | None] = mapped_column(Float)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    publisher: Mapped[str | None] = mapped_column(String(200))
    publication: Mapped[str | None] = mapped_column(String(300))
    revision: Mapped[str | None] = mapped_column(String(64))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class KnowledgeItem(Base, TimestampMixin):
    """A structured knowledge object (section 18)."""

    __tablename__ = "knowledge_item"
    __table_args__ = (
        Index("ix_knowledge_lookup", "well_id", "lookup_key"),
        Index("ix_knowledge_type", "item_type"),
        Index("ix_knowledge_subject", "entity_type", "entity_id"),
        Index("ix_knowledge_predicate", "predicate", "status"),
        Index("ix_knowledge_version", "document_version_id", "origin"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    domain: Mapped[str] = mapped_column(String(80), default="general", nullable=False)
    applicability: Mapped[str | None] = mapped_column(Text)
    assumptions: Mapped[list | None] = mapped_column(JSON, default=list)
    #: Structured payload: formula expression, variables, units, decision rules...
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)
    #: Canonical key for conflict detection, e.g. ``well:W1|section:12|property:mud_weight``.
    lookup_key: Mapped[str | None] = mapped_column(String(300))
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(24), default="")
    # -- the fact shape (knowledge layer): subject / predicate / object, with the source wording
    #: What the value belongs to.  ``entity_id`` addresses the registry row of ``entity_type``
    #: (a ``well``, a ``document``, a ``knowledge_item`` for types with no table of their own).
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(36))
    #: The assertion made about the subject, e.g. ``mud_weight``.  Open vocabulary, snake_case.
    predicate: Mapped[str | None] = mapped_column(String(120))
    #: quantity|text|date|boolean|ratio - decides how the value is compared and normalised.
    value_type: Mapped[str] = mapped_column(String(16), default="text", nullable=False)
    #: The value exactly as the source wrote it.  ``value``/``unit`` are the normalised pair;
    #: keeping only those would let a converter quietly rewrite engineering history.
    original_value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    original_unit: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    #: When the value applies (a mud weight reported for a shift is not timeless).
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The value in the field's canonical unit (12.5 ppg stays 12.5 ppg; 85 degF becomes 29.44
    #: degC).  ``value``/``unit`` are what the source said, converted only when its unit was
    #: unknown; these two are what comparisons are made on, stored so a query can filter on them
    #: without re-parsing JSON.
    normalized_value: Mapped[float | None] = mapped_column(Float)
    normalized_unit: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    #: Who is responsible for the row existing - ``rebuild`` only replaces ``EXTRACTED``.
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    #: PLANNED/ACTUAL/... so a planned and an actual value never collide (section 11).
    record_state: Mapped[str] = mapped_column(String(16), default="CURRENT", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="CANDIDATE", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source.id", ondelete="SET NULL"))
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    evidence: Mapped[list | None] = mapped_column(JSON, default=list)
    #: Set when a stronger source supersedes this item.
    superseded_by: Mapped[str | None] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(80), default="system", nullable=False)


class KnowledgeRelation(Base, TimestampMixin):
    """Directed, typed, provenance-carrying edge (section 20)."""

    __tablename__ = "knowledge_relation"
    __table_args__ = (
        UniqueConstraint(
            "source_type",
            "source_id",
            "relation",
            "target_type",
            "target_id",
            name="uq_relation_edge",
        ),
        Index("ix_relation_source", "source_type", "source_id"),
        Index("ix_relation_target", "target_type", "target_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    relation: Mapped[str] = mapped_column(String(48), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text)


class KnowledgeConflict(Base, TimestampMixin):
    """Open disagreement between sources (section 19)."""

    __tablename__ = "knowledge_conflict"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lookup_key: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    property_name: Mapped[str] = mapped_column(String(120), nullable=False)
    record_state: Mapped[str] = mapped_column(String(16), default="CURRENT", nullable=False)
    #: [{item_id, value, unit, source, authority_tier, revision, provenance}]
    candidates: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="OPEN", nullable=False)
    resolution: Mapped[dict | None] = mapped_column(JSON)
    #: The unit the values were normalised to for comparison (auditability).
    compare_unit: Mapped[str] = mapped_column(String(24), default="")
    note: Mapped[str | None] = mapped_column(Text)
    detected_by: Mapped[str] = mapped_column(
        String(64), default="conflict_detector", nullable=False
    )


class Skill(Base, TimestampMixin):
    """Versioned engineering skill package (section 9)."""

    __tablename__ = "skill"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    domain: Mapped[str] = mapped_column(String(80), default="drilling", nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Where the skill package is exported on disk (SKILL.md + references/).
    package_path: Mapped[str | None] = mapped_column(String(1024))
    tags: Mapped[list | None] = mapped_column(JSON, default=list)


class SkillVersion(Base, TimestampMixin):
    """One immutable build of a skill, including its full structured content."""

    __tablename__ = "skill_version"
    __table_args__ = (UniqueConstraint("skill_id", "version", name="uq_skill_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    skill_id: Mapped[str] = mapped_column(
        ForeignKey("skill.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    builder: Mapped[str] = mapped_column(String(64), default="skill_pipeline", nullable=False)
    builder_version: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    #: The structured skill (concepts/methods/formulas/rules/limitations/...).
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_document_ids: Mapped[list | None] = mapped_column(JSON, default=list)
    #: Statistics for the pipeline: item counts per section, extraction notes.
    metrics: Mapped[dict | None] = mapped_column(JSON, default=dict)
    #: Provenance summary so a reviewer can jump from a claim to a document.
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    review_status: Mapped[str] = mapped_column(String(24), default="UNREVIEWED", nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    skill: Mapped[Skill] = relationship(backref="versions")


# --------------------------------------------------------------------------- engineering
class Calculation(Base, TimestampMixin):
    """Engineering calculation contract + result (sections 24, 25, 43)."""

    __tablename__ = "calculation"
    __table_args__ = (Index("ix_calculation_well", "well_id", "method_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    method_id: Mapped[str] = mapped_column(String(80), nullable=False)
    method_version: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    calculation_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: PLANNED / FORECAST / ACTUAL / ... the state the numbers describe.
    record_state: Mapped[str] = mapped_column(String(16), default="CURRENT", nullable=False)
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source.id", ondelete="SET NULL"))
    inputs: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    outputs: Mapped[dict | None] = mapped_column(JSON)
    assumptions: Mapped[list | None] = mapped_column(JSON, default=list)
    validation: Mapped[dict | None] = mapped_column(JSON)
    uncertainty: Mapped[dict | None] = mapped_column(JSON)
    confidence: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="COMPUTED", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("calculation.id", ondelete="SET NULL")
    )
    reviewer: Mapped[str | None] = mapped_column(String(200))
    approval_note: Mapped[str | None] = mapped_column(Text)
    #: How the run was triggered (ui/cli/agent) - part of the audit trail.
    triggered_by: Mapped[str] = mapped_column(String(40), default="cli", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    input_records: Mapped[list[CalculationInput]] = relationship(
        back_populates="calculation", cascade="all, delete-orphan"
    )


class CalculationInput(Base):
    """Indexed view of calculation inputs for change-impact analysis (section 44).

    ``subject_key`` names the *source* of an input (a document field, a
    knowledge item, or a well/section attribute).  When that source changes, the
    platform can list every calculation that consumed it.
    """

    __tablename__ = "calculation_input"
    __table_args__ = (Index("ix_calc_input_subject", "subject_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    calculation_id: Mapped[str] = mapped_column(
        ForeignKey("calculation.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(24), default="")
    dimension: Mapped[str] = mapped_column(String(32), default="")
    source_kind: Mapped[str] = mapped_column(String(24), default="user")
    subject_key: Mapped[str | None] = mapped_column(String(300))
    provenance: Mapped[dict | None] = mapped_column(JSON)

    calculation: Mapped[Calculation] = relationship(back_populates="input_records")


# --------------------------------------------------------------------------- operations
class IngestionRun(Base):
    """One scan/ingest pass; keeps incremental statistics auditable."""

    __tablename__ = "ingestion_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace.id", ondelete="SET NULL")
    )
    root_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mode: Mapped[str] = mapped_column(String(24), default="incremental", nullable=False)
    counts: Mapped[dict | None] = mapped_column(JSON, default=dict)
    report: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class AuditEvent(Base):
    """Append-only trail of every transformation (section 85).

    The append-only rule is *enforced*, not just documented: SQLAlchemy mapper events
    refuse UPDATE and DELETE on this table (see
    :mod:`drilling_intelligence.database.audit`), so no repository, service or future
    UI can rewrite or erase history by accident.  Editing what happened is a schema
    migration with an explicit reason, never an ORM write.
    """

    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_subject", "subject_type", "subject_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    actor: Mapped[str] = mapped_column(String(80), default="system", nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- operations
#
# The operational spine: a report is evidence, and the rows below are the platform's records of
# what happened, each one able to name the version it was read out of (ADR-0010).  Nothing here
# replaces a document or a knowledge fact - a ``NptRecord`` is not a copy of the report's NPT line,
# it is the row that makes "which wells in this field lost time to stuck pipe, and how much" a
# query instead of a re-read of forty files.


class DdrReport(Base, TimestampMixin):
    """A daily drilling report as a record, not only as a file (architecture: DDR first-class).

    ``document_version_id`` is the evidence and ``well_id`` is the subject; the two are kept apart
    on purpose, because the well is decided by the registry (a folder, a name, a human edit) while
    the report number and date are what the document said.
    """

    __tablename__ = "ddr_report"
    __table_args__ = (
        UniqueConstraint("document_version_id", "well_id", name="uq_ddr_report_version_well"),
        Index("ix_ddr_report_well_date", "well_id", "report_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    well_id: Mapped[str] = mapped_column(ForeignKey("well.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    #: The number the report carries on its face.  ``None`` when it has none, which is common.
    report_number: Mapped[str | None] = mapped_column(String(64))
    #: The date the report is *for* (a day), as a timestamp so a range query is one comparison.
    report_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The date exactly as the document wrote it, kept so a parser bug is arguable.
    report_date_text: Mapped[str | None] = mapped_column(String(80))
    #: The report's own day/shift label ("Day 12", "Night shift"), when it had one.
    shift: Mapped[str | None] = mapped_column(String(40))
    #: ``RecordState`` - a report describes what happened, but a planned day exists in some formats.
    record_state: Mapped[str] = mapped_column(String(16), default="ACTUAL", nullable=False)
    #: ``ConfirmationStatus``: promoted reports are CANDIDATE until somebody reads them.
    status: Mapped[str] = mapped_column(String(24), default="CANDIDATE", nullable=False)
    #: The document's approval status as registered (DRAFT/APPROVED/...), copied, never guessed.
    document_status: Mapped[str | None] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Where each field above came from, in the shape the knowledge layer uses.
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="system", nullable=False)
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


class WellOperation(Base, TimestampMixin):
    """What was being done: a span of activity, bounded by what the source said and no more.

    Start and end are nullable because most reports give one, neither, or a date with no time.  A
    missing time stays missing: a row that invented ``00:00`` would silently become the earliest
    thing that happened that day in every timeline and every duration.
    """

    __tablename__ = "well_operation"
    __table_args__ = (
        Index("ix_operation_well_start", "well_id", "started_at"),
        Index("ix_operation_type", "operation_type", "well_id"),
        Index("ix_operation_report", "report_id"),
        UniqueConstraint("identity_key", name="uq_operation_identity"),
        Index("ix_operation_version", "document_version_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    well_id: Mapped[str] = mapped_column(ForeignKey("well.id", ondelete="CASCADE"), nullable=False)
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    report_id: Mapped[str | None] = mapped_column(ForeignKey("ddr_report.id", ondelete="SET NULL"))
    #: Snake_case token from :func:`drilling_intelligence.core.vocabulary.operation_type`.
    operation_type: Mapped[str] = mapped_column(String(48), nullable=False)
    #: The activity exactly as the source named it ("Tripping Out"), so a token never hides wording.
    label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The period as written, when the source had one ("14 Jun 06:00 - 14 Jun 18:00").
    period_text: Mapped[str | None] = mapped_column(String(200))
    #: The depth the operation reached, kept as a value/unit pair like every other quantity.
    depth_md_value: Mapped[float | None] = mapped_column(Float)
    depth_md_unit: Mapped[str] = mapped_column(String(16), default="m")
    #: ``RecordState``: a planned operation and an executed one are different claims about the same
    #: name, and both must survive side by side (this is what plan-vs-actual reads).
    record_state: Mapped[str] = mapped_column(String(16), default="ACTUAL", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="CANDIDATE", nullable=False)
    rig_id: Mapped[str | None] = mapped_column(ForeignKey("rig.id", ondelete="SET NULL"))
    service_company_id: Mapped[str | None] = mapped_column(
        ForeignKey("service_company.id", ondelete="SET NULL")
    )
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="system", nullable=False)
    #: Content-addressed key when a promotion wrote this row: two passes are one row, not two.
    identity_key: Mapped[str | None] = mapped_column(String(160))
    #: The evidence that produced the row, when a promotion wrote it.  ``report_id`` says which
    #: report the row belongs to; these say which *version* of it the fields were read out of, and
    #: they are what makes re-promoting a version a replacement rather than an accumulation.
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


class WellEvent(Base, TimestampMixin):
    """Something that happened during an operation.

    An event is not an operation and not an NPT record: it is the occurrence, and the lost time is a
    separate row that cites it (``NptRecord.event_id``).  Folding them together is what makes "11
    events, 38 hours" impossible to ask.
    """

    __tablename__ = "well_event"
    __table_args__ = (
        Index("ix_event_well_time", "well_id", "occurred_at"),
        Index("ix_event_category", "category", "well_id"),
        Index("ix_event_operation", "operation_id"),
        Index("ix_event_version", "document_version_id"),
        UniqueConstraint("identity_key", name="uq_event_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    well_id: Mapped[str] = mapped_column(ForeignKey("well.id", ondelete="CASCADE"), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_operation.id", ondelete="SET NULL")
    )
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    report_id: Mapped[str | None] = mapped_column(ForeignKey("ddr_report.id", ondelete="SET NULL"))
    #: ``KNOWN_EVENT_CATEGORIES`` (or the source's own token when the vocabulary did not match).
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    #: The type within the category: ``stuck_pipe``, ``washout``, ``kick``.  Open, snake_case.
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: What the source called it, verbatim.
    label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurred_at_text: Mapped[str | None] = mapped_column(String(200))
    #: ``SeverityLevel`` or NULL.  NULL means the source did not say, and nothing here decides.
    severity: Mapped[str | None] = mapped_column(String(16))
    depth_md_value: Mapped[float | None] = mapped_column(Float)
    depth_md_unit: Mapped[str] = mapped_column(String(16), default="m")
    #: The equipment, string or assembly involved, addressed through the knowledge item that names
    #: it (equipment has no table of its own - see ``ENTITY_TYPES``), so the reference stays real.
    equipment_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_item.id", ondelete="SET NULL")
    )
    rig_id: Mapped[str | None] = mapped_column(ForeignKey("rig.id", ondelete="SET NULL"))
    service_company_id: Mapped[str | None] = mapped_column(
        ForeignKey("service_company.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), default="CANDIDATE", nullable=False)
    record_state: Mapped[str] = mapped_column(String(16), default="ACTUAL", nullable=False)
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="system", nullable=False)
    identity_key: Mapped[str | None] = mapped_column(String(160))
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


class NptRecord(Base, TimestampMixin):
    """Non-productive time, as its own record, with its cause's epistemic state attached.

    ``duration_hours`` is stored as hours because hours are what every report and every total
    agrees on; the source's own wording is kept in ``duration_text`` and the *basis* says whether
    the number was quoted, measured between two stamps, or computed from one of them.
    """

    __tablename__ = "npt_record"
    __table_args__ = (
        Index("ix_npt_well_start", "well_id", "started_at"),
        Index("ix_npt_category", "category", "well_id"),
        Index("ix_npt_event", "event_id"),
        Index("ix_npt_version", "document_version_id"),
        UniqueConstraint("identity_key", name="uq_npt_identity"),
        CheckConstraint(
            "duration_hours IS NULL OR duration_hours >= 0",
            name="duration_not_negative",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    well_id: Mapped[str] = mapped_column(ForeignKey("well.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("well_event.id", ondelete="SET NULL"))
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_operation.id", ondelete="SET NULL")
    )
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    report_id: Mapped[str | None] = mapped_column(ForeignKey("ddr_report.id", ondelete="SET NULL"))
    #: The NPT reason code as a token (``stuck_pipe``, ``equipment_failure``), mapped by
    #: :func:`drilling_intelligence.core.vocabulary.problem_type`; an unknown code keeps its own.
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    #: The code verbatim (``NPT-STUCK``), because a token that flattens 4 operators' codes into one
    #: name must still be traceable to the one that wrote it.
    subcategory: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at_text: Mapped[str | None] = mapped_column(String(200))
    duration_hours: Mapped[float | None] = mapped_column(Float)
    duration_text: Mapped[str | None] = mapped_column(String(80))
    #: ``DurationBasis``.
    duration_basis: Mapped[str] = mapped_column(String(16), default="STATED", nullable=False)
    #: The three causes, kept separate: what happened, what made it happen now, and what would have
    #: had to be different.  A report usually gives the first, sometimes the second, rarely the third.
    cause: Mapped[str | None] = mapped_column(Text)
    immediate_cause: Mapped[str | None] = mapped_column(Text)
    root_cause: Mapped[str | None] = mapped_column(Text)
    #: ``CauseStatus`` for the root cause, and separately for the immediate one: a row can know the
    #: first and guess the second.
    root_cause_status: Mapped[str] = mapped_column(String(16), default="UNKNOWN", nullable=False)
    immediate_cause_status: Mapped[str] = mapped_column(
        String(16), default="UNKNOWN", nullable=False
    )
    #: Reported cost impact, if the report had one (many do not; a nil is not a zero).
    cost_impact_value: Mapped[float | None] = mapped_column(Float)
    cost_impact_unit: Mapped[str] = mapped_column(String(16), default="USD")
    #: The rig/service company the time is charged against, when the report said.
    rig_id: Mapped[str | None] = mapped_column(ForeignKey("rig.id", ondelete="SET NULL"))
    service_company_id: Mapped[str | None] = mapped_column(
        ForeignKey("service_company.id", ondelete="SET NULL")
    )
    equipment_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_item.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), default="CANDIDATE", nullable=False)
    #: 0-1: how much the *record* is trusted, distinct from how sure the cause is.
    confidence: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="system", nullable=False)
    identity_key: Mapped[str | None] = mapped_column(String(160))
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)

    @property
    def duration_known(self) -> bool:
        return self.duration_hours is not None


class ProblemOccurrence(Base, TimestampMixin):
    """A problem, seen once, at one well.

    A problem is not an event: ``stuck_pipe`` as a *type* is what field intelligence groups by, and
    this row is the single occurrence of it that a specific well had at a specific depth, with the
    corrective action taken and - if anybody ever said so - the root cause.  Root causes default to
    ``UNKNOWN``, and the count of rows that say so is a report on the corpus, not a bug in it.
    """

    __tablename__ = "problem_occurrence"
    __table_args__ = (
        Index("ix_problem_well", "well_id", "problem_type"),
        Index("ix_problem_version", "document_version_id"),
        Index("ix_problem_type_time", "problem_type", "occurred_at"),
        UniqueConstraint("identity_key", name="uq_problem_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    well_id: Mapped[str] = mapped_column(ForeignKey("well.id", ondelete="CASCADE"), nullable=False)
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_operation.id", ondelete="SET NULL")
    )
    event_id: Mapped[str | None] = mapped_column(ForeignKey("well_event.id", ondelete="SET NULL"))
    npt_id: Mapped[str | None] = mapped_column(ForeignKey("npt_record.id", ondelete="SET NULL"))
    #: ``PROBLEM_TYPES`` or the source's own token.
    problem_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The code the source used, verbatim (``NPT-STUCK``).  ``problem_type`` is the token several
    #: spellings collapse to, and the collapse has to be reversible for a reviewer to check it.
    code: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    depth_from_value: Mapped[float | None] = mapped_column(Float)
    depth_from_unit: Mapped[str] = mapped_column(String(16), default="m")
    depth_to_value: Mapped[float | None] = mapped_column(Float)
    depth_to_unit: Mapped[str] = mapped_column(String(16), default="m")
    #: The hole the problem was in, when it is known - the field asks "is this an 8 1/2 problem?".
    hole_size_in: Mapped[float | None] = mapped_column(Float)
    formation: Mapped[str | None] = mapped_column(String(120))
    #: What the source said the problem was, and how much weight that statement carries: ``KNOWN``
    #: when the report wrote it, ``INFERRED`` when somebody reasoned it out of the description.
    immediate_cause: Mapped[str | None] = mapped_column(Text)
    immediate_cause_status: Mapped[str] = mapped_column(
        String(16), default="UNKNOWN", nullable=False
    )
    root_cause: Mapped[str | None] = mapped_column(Text)
    #: ``CauseStatus`` for the root cause: KNOWN only when a source said so.
    root_cause_status: Mapped[str] = mapped_column(String(16), default="UNKNOWN", nullable=False)
    #: What else had to be true for this to happen.  A list of strings, each citable through the
    #: record's provenance; never a paragraph, because "contributing factors" is a set to count.
    contributing_factors: Mapped[list | None] = mapped_column(JSON, default=list)
    corrective_action: Mapped[str | None] = mapped_column(Text)
    preventive_action: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="CANDIDATE", nullable=False)
    #: 0-1: how much the *record* is trusted, distinct from how sure the cause is.
    confidence: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="system", nullable=False)
    identity_key: Mapped[str | None] = mapped_column(String(160))
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


class LessonLearned(Base, TimestampMixin):
    """A lesson, as an engineering record with an approval state and evidence.

    The two rules that make this table worth having: a lesson cannot exist without at least one
    citation (a lesson that is only an opinion is a rumour with a title), and it cannot be approved
    by whoever drafted it, because the review is the only thing separating ``DRAFT`` from
    ``APPROVED``.  Both are enforced in :class:`~drilling_intelligence.lessons.repository.LessonRepository`.
    """

    __tablename__ = "lesson_learned"
    __table_args__ = (
        UniqueConstraint("code", "revision", name="uq_lesson_code_revision"),
        Index("ix_lesson_scope", "field_id", "status"),
        Index("ix_lesson_problem", "problem_type", "status"),
        # One live lesson per code: the same partial-index trick that makes a document's single
        # current version a database rule rather than a convention.
        Index(
            "uq_lesson_one_current",
            "code",
            unique=True,
            sqlite_where=text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=text("is_current AND code IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: The number the organisation gave it (``LL-2025-014``), when it has one.
    code: Mapped[str | None] = mapped_column(String(80))
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: The label as authored ("Rev 2", "Rev B"), kept next to the integer that orders them.
    revision_label: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("lesson_learned.id", ondelete="SET NULL")
    )
    #: ``LessonLifecycle``.
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", nullable=False)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    #: The problem this lesson is about, as a token, so ``lessons_for_problem`` is one WHERE.
    problem_type: Mapped[str | None] = mapped_column(String(64))
    context: Mapped[str] = mapped_column(Text, default="", nullable=False)
    observation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: What the source said the cause was - kept apart from the lesson, because a lesson is what you
    #: do next and a cause is why it happened, and people confuse them constantly.
    root_cause: Mapped[str | None] = mapped_column(Text)
    root_cause_status: Mapped[str] = mapped_column(String(16), default="UNKNOWN", nullable=False)
    lesson: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # -- applicability: the filter that lets a new well ask "which lessons are for me?" ---------
    #: Scopes are ids of the registry rows (well/field/project); the free-text ones are conditions.
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    field_id: Mapped[str | None] = mapped_column(ForeignKey("field.id", ondelete="SET NULL"))
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    #: Snake_case operation tokens this lesson applies to (``["tripping", "reaming"]``).
    applicable_operations: Mapped[list | None] = mapped_column(JSON, default=list)
    applicable_formations: Mapped[list | None] = mapped_column(JSON, default=list)
    hole_size_in: Mapped[float | None] = mapped_column(Float)
    depth_from_value: Mapped[float | None] = mapped_column(Float)
    depth_from_unit: Mapped[str] = mapped_column(String(16), default="m")
    depth_to_value: Mapped[float | None] = mapped_column(Float)
    depth_to_unit: Mapped[str] = mapped_column(String(16), default="m")
    #: The conditions in prose, for the parts no column covers ("below the last casing shoe").
    conditions: Mapped[str | None] = mapped_column(Text)
    # -- review ------------------------------------------------------------------
    created_by: Mapped[str] = mapped_column(String(200), default="system", nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 0-1.  Confidence in the *lesson*, not in the evidence: a well-cited one-off still reads lower
    #: than the same finding on six wells.
    confidence: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)
    #: Why a superseded lesson was replaced: kept on the row so history explains itself.
    status_note: Mapped[str | None] = mapped_column(Text)


class ProcedureRecord(Base, TimestampMixin):
    """A procedure as an engineering object, not a file (architecture: procedures).

    The document is still the evidence - ``document_id``/``document_version_id`` point at it, and
    ``provenance`` says which part of it - but "was this revision approved, and what was it based
    on" are questions about the procedure, which a file's properties cannot answer.
    """

    __tablename__ = "procedure_record"
    __table_args__ = (
        UniqueConstraint("code", "revision", name="uq_procedure_code_revision"),
        Index("ix_procedure_well", "well_id", "status"),
        Index("ix_procedure_field_type", "field_id", "procedure_type"),
        Index(
            "uq_procedure_one_current",
            "code",
            unique=True,
            sqlite_where=text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=text("is_current AND code IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str | None] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    #: Open snake_case vocabulary: ``casing_running``, ``well_control``, ``cementing``.
    procedure_type: Mapped[str] = mapped_column(String(64), default="general", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    revision_label: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("procedure_record.id", ondelete="SET NULL")
    )
    #: ``ProcedureLifecycle``.
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # -- scope (a procedure can be field-wide, well-specific, or both) ----------
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    field_id: Mapped[str | None] = mapped_column(ForeignKey("field.id", ondelete="SET NULL"))
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    # -- authorship and approval, on the record: the file's own properties lie about both --
    created_by: Mapped[str] = mapped_column(String(200), default="system", nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The reference the procedure was written against ("API RP 53", "CP-002 Rev 1").
    source_reference: Mapped[str | None] = mapped_column(String(300))
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The document holding the procedure text, when there is one (there nearly always is).
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)
    status_note: Mapped[str | None] = mapped_column(Text)


class DrillingProgram(Base, TimestampMixin):
    """A drilling program: versioned, approved, and the parent of the targets it set.

    ``program_target`` rows are what plan-vs-actual compares against, so the program is not only a
    document that mentions a number - it is the record that owns the plan.
    """

    __tablename__ = "drilling_program"
    __table_args__ = (
        UniqueConstraint("code", "revision", name="uq_program_code_revision"),
        Index("ix_program_well", "well_id", "status"),
        Index(
            "uq_program_one_current",
            "code",
            unique=True,
            sqlite_where=text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=text("is_current AND code IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str | None] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    revision_label: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("drilling_program.id", ondelete="SET NULL")
    )
    #: ``ProgramLifecycle``.
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    field_id: Mapped[str | None] = mapped_column(ForeignKey("field.id", ondelete="SET NULL"))
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    author: Mapped[str | None] = mapped_column(String(200))
    reviewer: Mapped[str | None] = mapped_column(String(200))
    approver: Mapped[str | None] = mapped_column(String(200))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The program's dates as written on the cover, separate from the registry's timestamps.
    planned_spud_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    planned_completion_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), default="system", nullable=False)
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)
    status_note: Mapped[str | None] = mapped_column(Text)


class ProgramTarget(Base, TimestampMixin):
    """One planned number in a program: depth, duration, mud, NPT allowance, cost.

    Rows are keyed to a program and (where the program was written section by section) a section,
    and they never write to :class:`WellSection` - the plan and the record are separate columns
    there for the same reason they are separate rows here (section 11).
    """

    __tablename__ = "program_target"
    __table_args__ = (
        Index("ix_program_target_program", "program_id", "sequence"),
        Index("ix_program_target_section", "section_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    program_id: Mapped[str] = mapped_column(
        ForeignKey("drilling_program.id", ondelete="CASCADE"), nullable=False
    )
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: The section as the program named it, kept even when ``section_id`` resolves (it may not).
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    hole_size_in: Mapped[float | None] = mapped_column(Float)
    casing_program: Mapped[str | None] = mapped_column(String(120))
    formation_top: Mapped[str | None] = mapped_column(String(120))
    planned_depth_md_value: Mapped[float | None] = mapped_column(Float)
    planned_depth_md_unit: Mapped[str] = mapped_column(String(16), default="m")
    planned_duration_days: Mapped[float | None] = mapped_column(Float)
    planned_mud_weight_value: Mapped[float | None] = mapped_column(Float)
    planned_mud_weight_unit: Mapped[str] = mapped_column(String(16), default="ppg")
    #: The NPT allowance the plan carried.  A program without one has no row-level allowance, and
    #: the variance service says "no allowance stated" instead of treating it as zero.
    planned_npt_hours: Mapped[float | None] = mapped_column(Float)
    planned_cost_value: Mapped[float | None] = mapped_column(Float)
    planned_cost_unit: Mapped[str] = mapped_column(String(16), default="USD")
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


class RiskRecord(Base, TimestampMixin):
    """A risk, with its score kept as the two axes plus the product the matrix defines.

    ``probability`` and ``impact`` are 1-5 by the platform's own matrix, and ``severity`` is written
    by :func:`drilling_intelligence.engineering.risk.score_risk` and nowhere else.  A row with one
    axis missing has no severity - a half-scored risk presented as a whole one is how a "medium"
    ends up in a report nobody can defend.
    """

    __tablename__ = "risk_record"
    __table_args__ = (
        UniqueConstraint("code", "revision", name="uq_risk_code_revision"),
        Index("ix_risk_scope", "field_id", "status"),
        Index("ix_risk_well", "well_id", "status"),
        CheckConstraint(
            "probability IS NULL OR (probability BETWEEN 1 AND 5)", name="probability_scale"
        ),
        CheckConstraint("impact IS NULL OR (impact BETWEEN 1 AND 5)", name="impact_scale"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str | None] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    #: Open snake_case: ``well_control``, ``geomechanical``, ``loss_of_circulation``, ``hse``.
    category: Mapped[str] = mapped_column(String(48), default="other", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    revision_label: Mapped[str | None] = mapped_column(String(64))
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("risk_record.id", ondelete="SET NULL")
    )
    #: ``RiskLifecycle``.
    status: Mapped[str] = mapped_column(String(24), default="OPEN", nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    field_id: Mapped[str | None] = mapped_column(ForeignKey("field.id", ondelete="SET NULL"))
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    #: The depth band, when the risk was written against one.
    depth_from_value: Mapped[float | None] = mapped_column(Float)
    depth_from_unit: Mapped[str] = mapped_column(String(16), default="m")
    depth_to_value: Mapped[float | None] = mapped_column(Float)
    depth_to_unit: Mapped[str] = mapped_column(String(16), default="m")
    probability: Mapped[int | None] = mapped_column(Integer)
    impact: Mapped[int | None] = mapped_column(Integer)
    #: ``probability * impact`` on the 5x5 matrix, written by the deterministic scorer only.
    severity: Mapped[int | None] = mapped_column(Integer)
    #: The band the matrix calls that score ("LOW"/"MEDIUM"/"HIGH"/"CRITICAL").
    severity_band: Mapped[str | None] = mapped_column(String(16))
    #: The scale the score is on, because a 5x5 12 is not a 4x4 12.
    scale: Mapped[str] = mapped_column(String(24), default="MATRIX_5X5", nullable=False)
    causes: Mapped[list | None] = mapped_column(JSON, default=list)
    consequences: Mapped[list | None] = mapped_column(JSON, default=list)
    mitigation: Mapped[str | None] = mapped_column(Text)
    contingency: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(200))
    #: Where the risk came from: "the offset wells' history" is a real answer, and it needs a row.
    source_note: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), default="system", nullable=False)
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


class Rig(Base, TimestampMixin):
    """A rig as a thing that was on a well, not a string in a report.

    Performance comparisons are queries over the operations and NPT rows that name a rig, so this
    table holds identity and specification only - a rig with an ``average_npt_hours`` column is a
    number nobody can recompute and everybody will distrust.
    """

    __tablename__ = "rig"
    __table_args__ = (Index("ix_rig_company", "company_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    #: The operator or the contractor, whichever the workspace registered: both are ``company`` rows.
    company_id: Mapped[str | None] = mapped_column(ForeignKey("company.id", ondelete="SET NULL"))
    #: The registry's own designation ("Superior 150E", "CDS 900-M").
    model: Mapped[str | None] = mapped_column(String(120))
    #: ``ON_LEASE`` / ``MOBILISING`` / ``OFF_LEASE`` / ``DECOMMISSIONED`` - open on purpose.
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    horsepower: Mapped[float | None] = mapped_column(Float)
    #: Pumps, drawworks, VFD, winter package: whatever the operator records, as a dict of strings.
    specifications: Mapped[dict | None] = mapped_column(JSON, default=dict)
    #: ``source`` row that describes the rig (a registry entry, a datasheet), when there is one.
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(80), default="system", nullable=False)
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


class ServiceCompany(Base, TimestampMixin):
    """A vendor, tied to the company that registers it, with its service discipline.

    Like :class:`Rig`, this is identity only: performance is a join over the events and NPT rows
    that name the company, and the reason the row exists at all is so those joins have something to
    group by other than a spelling.
    """

    __tablename__ = "service_company"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_service_company_name"),
        Index("ix_service_company_type", "service_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: The owning corporate entity (``company``), when the workspace models one.
    company_id: Mapped[str | None] = mapped_column(ForeignKey("company.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Open snake_case: ``directional``, ``mud``, ``cementing``, ``logging``, ``bits``.
    service_type: Mapped[str] = mapped_column(String(64), default="other", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    #: Contract reference, when one exists: it is the thing a claim about a vendor leads back to.
    contract_reference: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(80), default="system", nullable=False)
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


class CostItem(Base, TimestampMixin):
    """One line of a cost breakdown structure, planned and actual side by side.

    CBS codes are columns, not tables: a cost sheet is a hierarchy of codes over a handful of
    numbers per well, and the tree is derived from the code (``1.2.3`` under ``1.2``) by the
    aggregation service.  A real estimating system owns rates, commitments and invoices; this row
    owns the pair of numbers the platform was given and the source it got them from.
    """

    __tablename__ = "cost_item"
    __table_args__ = (
        Index("ix_cost_well_category", "well_id", "category"),
        Index("ix_cost_project_cbs", "project_id", "cbs_code"),
        UniqueConstraint("identity_key", name="uq_cost_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    field_id: Mapped[str | None] = mapped_column(ForeignKey("field.id", ondelete="SET NULL"))
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    program_id: Mapped[str | None] = mapped_column(
        ForeignKey("drilling_program.id", ondelete="SET NULL")
    )
    #: Work breakdown structure code ("1.2"), the project's own numbering.
    wbs_code: Mapped[str | None] = mapped_column(String(40))
    #: Cost breakdown structure code ("1.2.4"), verbatim from the sheet.
    cbs_code: Mapped[str | None] = mapped_column(String(40))
    #: The roll-up label derived from the code path, cached for display only - never a source.
    cbs_path: Mapped[str | None] = mapped_column(String(300))
    #: ``COST_CATEGORIES`` or the sheet's own token.
    category: Mapped[str] = mapped_column(String(48), default="other", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    planned_value: Mapped[float | None] = mapped_column(Float)
    planned_unit: Mapped[str] = mapped_column(String(16), default="USD")
    actual_value: Mapped[float | None] = mapped_column(Float)
    actual_unit: Mapped[str] = mapped_column(String(16), default="USD")
    #: ``RecordState`` for a row that is a forecast rather than either of the above.
    record_state: Mapped[str] = mapped_column(String(16), default="CURRENT", nullable=False)
    #: The NPT row this line was incurred against, when the sheet attributed it (PART 6's cost case).
    npt_id: Mapped[str | None] = mapped_column(ForeignKey("npt_record.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(24), default="CANDIDATE", nullable=False)
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="system", nullable=False)
    identity_key: Mapped[str | None] = mapped_column(String(160))
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


class FieldPattern(Base, TimestampMixin):
    """A recurring problem, as counted from the records - with the query that counted it.

    A pattern is a *snapshot* of an aggregation, and it says so: ``query`` holds the parameters and
    ``recompute`` re-runs them to check the numbers still hold.  A pattern that no longer matches
    the rows is marked stale rather than quietly refreshed, because a lesson written against a
    pattern that has since stopped being true is a decision nobody chose.
    """

    __tablename__ = "field_pattern"
    __table_args__ = (
        Index("ix_pattern_field", "field_id", "problem_type"),
        UniqueConstraint("signature", name="uq_pattern_signature"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: What was aggregated: ``field:FLD-A|problem:stuck_pipe|hole:8.5`` - the key a recompute finds.
    signature: Mapped[str] = mapped_column(String(200), nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    field_id: Mapped[str | None] = mapped_column(ForeignKey("field.id", ondelete="SET NULL"))
    problem_type: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_type: Mapped[str | None] = mapped_column(String(48))
    hole_size_in: Mapped[float | None] = mapped_column(Float)
    depth_from_value: Mapped[float | None] = mapped_column(Float)
    depth_from_unit: Mapped[str] = mapped_column(String(16), default="m")
    depth_to_value: Mapped[float | None] = mapped_column(Float)
    depth_to_unit: Mapped[str] = mapped_column(String(16), default="m")
    #: The counts, all of them read out of the records.
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    well_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_npt_hours: Mapped[float | None] = mapped_column(Float)
    #: ``[{"kind": "problem", "id": "prob-..."}]`` - every number above has a row behind it here.
    evidence: Mapped[list | None] = mapped_column(JSON, default=list)
    #: Well ids, denormalised for the "which wells" answer a reviewer checks first.
    well_ids: Mapped[list | None] = mapped_column(JSON, default=list)
    #: The exact aggregation parameters, so a recompute is the same query and not a new judgement.
    query: Mapped[dict | None] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: ``ConfirmationStatus``: a pattern a person has looked at is CONFIRMED; the rest are proposals.
    status: Mapped[str] = mapped_column(String(24), default="CANDIDATE", nullable=False)
    #: 0-1, from how many wells and how much evidence back the count (see :func:`pattern_confidence`).
    confidence: Mapped[float | None] = mapped_column(Float)
    #: Set when a recompute found the numbers no longer match; ``stale_count`` is what it found.
    stale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale_snapshot: Mapped[dict | None] = mapped_column(JSON)
    detected_by: Mapped[str] = mapped_column(
        String(64), default="field_intelligence", nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


class BestPractice(Base, TimestampMixin):
    """A practice the field decided to adopt, and the lessons that earned it.

    Deliberately its own table rather than a flag on a lesson: a lesson is what one well learned,
    while a practice is what several wells have agreed to do, with an owner and an approval behind
    it.  The revision chain is the same shape as a procedure's, because "which version are we
    running" is the same question with the same answer shape - and a practice with no approval
    column is an anecdote somebody forwarded.
    """

    __tablename__ = "best_practice"
    __table_args__ = (
        UniqueConstraint("code", "revision", name="uq_practice_code_revision"),
        Index("ix_practice_field_status", "field_id", "status"),
        Index("ix_practice_well", "well_id", "status"),
        Index(
            "uq_practice_one_current",
            "code",
            unique=True,
            sqlite_where=text("is_current = 1 AND code IS NOT NULL"),
            postgresql_where=text("is_current AND code IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: Optional human key ("BP-014"), stable across revisions of the same practice.
    code: Mapped[str | None] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    #: What kind of practice: ``hole_cleaning``, ``casing_running``.  Open vocabulary.
    practice_type: Mapped[str] = mapped_column(String(64), default="general", nullable=False)
    #: The practice itself, in the imperative: "ream above the shallowest packer before tripping".
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    #: Why it works, as the sources said it - not a mechanism the platform inferred.
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    revision_label: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("best_practice.id", ondelete="SET NULL")
    )
    #: ``ProcedureLifecycle``: a practice is approved, replaced and retired by the same rules a
    #: procedure is, which is why it reuses that machine instead of growing a near-copy.
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", nullable=False)
    #: Who owns it - the person a reader asks when the practice does not fit their well.
    owner: Mapped[str | None] = mapped_column(String(120))
    reviewer: Mapped[str | None] = mapped_column(String(120))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(120))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # -- scope: the same four optional levels as a procedure, and the same consistency check ------
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    field_id: Mapped[str | None] = mapped_column(ForeignKey("field.id", ondelete="SET NULL"))
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    #: Where it applies, as open tokens the search and the recommender both filter on.
    applicable_operations: Mapped[list | None] = mapped_column(JSON, default=list)
    applicable_formations: Mapped[list | None] = mapped_column(JSON, default=list)
    hole_size_in: Mapped[float | None] = mapped_column(Float)
    conditions: Mapped[dict | None] = mapped_column(JSON, default=dict)
    #: Where it must *not* be applied, because half of a practice's value is its exceptions.
    not_applicable_when: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_version.id", ondelete="SET NULL")
    )
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="system", nullable=False)
    #: Source lessons are relations (``LESSON_BEST_PRACTICE``), not a column: a practice rests on
    #: several lessons, and each edge carries the evidence that lesson contributed.
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


class Recommendation(Base, TimestampMixin):
    """Advice derived from the records, kept until a person decides about it.

    A recommendation is not a fact: it is a proposal with a reason attached.  So the row stores
    *why* it was generated (the pattern or lesson that produced it, and the query that did), and
    nothing in the platform treats it as data until ``status`` says otherwise.  ``signature`` is
    unique, which is what makes the next run of the generator a refresh rather than another
    near-duplicate of the same advice.
    """

    __tablename__ = "recommendation"
    __table_args__ = (
        Index("ix_recommendation_field_status", "field_id", "status"),
        Index("ix_recommendation_well", "well_id", "status"),
        UniqueConstraint("signature", name="uq_recommendation_signature"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: ``field:FLD-A|problem:stuck_pipe|action:ream`` - what produced it, so a re-run finds it.
    signature: Mapped[str] = mapped_column(String(200), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    #: Why this was generated: which rows, counted how, imply this action.  Never empty.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``[{"kind": "problem_occurrence", "id": "..."}]`` - the same evidence-ref shape a pattern uses.
    evidence: Mapped[list | None] = mapped_column(JSON, default=list)
    #: The query the generator ran, verbatim, so a reader re-runs it instead of trusting this row.
    query: Mapped[dict | None] = mapped_column(JSON, default=dict)
    #: ``RecommendationLifecycle``.
    status: Mapped[str] = mapped_column(String(24), default="PROPOSED", nullable=False)
    #: 0-1, from the strength of the pattern behind it - never a model's confidence in its own prose.
    confidence: Mapped[float | None] = mapped_column(Float)
    applicability: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # -- what it came from and what it is for: columns, because these are the joins ---------------
    pattern_id: Mapped[str | None] = mapped_column(
        ForeignKey("field_pattern.id", ondelete="SET NULL")
    )
    lesson_id: Mapped[str | None] = mapped_column(
        ForeignKey("lesson_learned.id", ondelete="SET NULL")
    )
    practice_id: Mapped[str | None] = mapped_column(
        ForeignKey("best_practice.id", ondelete="SET NULL")
    )
    problem_id: Mapped[str | None] = mapped_column(
        ForeignKey("problem_occurrence.id", ondelete="SET NULL")
    )
    risk_id: Mapped[str | None] = mapped_column(ForeignKey("risk_record.id", ondelete="SET NULL"))
    procedure_id: Mapped[str | None] = mapped_column(
        ForeignKey("procedure_record.id", ondelete="SET NULL")
    )
    program_id: Mapped[str | None] = mapped_column(
        ForeignKey("drilling_program.id", ondelete="SET NULL")
    )
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    field_id: Mapped[str | None] = mapped_column(ForeignKey("field.id", ondelete="SET NULL"))
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_section.id", ondelete="SET NULL")
    )
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("well_operation.id", ondelete="SET NULL")
    )
    #: Who wrote it: a service name for a generated one, a person for a hand-written one.  The
    #: generator is never the approver, so ``decided_by`` is a separate column that stays NULL until
    #: somebody who owns the operation says so.
    generated_by: Mapped[str] = mapped_column(
        String(80), default="field_intelligence", nullable=False
    )
    decided_by: Mapped[str | None] = mapped_column(String(120))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decline_reason: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict | None] = mapped_column(JSON, default=dict)


__all__ = [
    "AuditEvent",
    "Base",
    "BestPractice",
    "Calculation",
    "CalculationInput",
    "Company",
    "CostItem",
    "DdrReport",
    "Document",
    "DocumentVersion",
    "DrillingProgram",
    "Extraction",
    "ExtractionCache",
    "Field",
    "FieldPattern",
    "IngestionRun",
    "KnowledgeConflict",
    "KnowledgeItem",
    "KnowledgeRelation",
    "LessonLearned",
    "NptRecord",
    "ProblemOccurrence",
    "ProcedureRecord",
    "ProgramTarget",
    "Project",
    "Recommendation",
    "Rig",
    "RiskRecord",
    "ServiceCompany",
    "Skill",
    "SkillVersion",
    "Source",
    "Well",
    "WellEvent",
    "WellOperation",
    "WellSection",
    "Workspace",
]
