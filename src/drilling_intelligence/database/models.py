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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


# --------------------------------------------------------------------------- hierarchy
class Company(Base, TimestampMixin):
    __tablename__ = "company"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    code: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)

    projects: Mapped[list[Project]] = relationship(back_populates="company", cascade="all, delete-orphan")


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
    fields: Mapped[list[Field]] = relationship(back_populates="project", cascade="all, delete-orphan")
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
    well_type: Mapped[str | None] = mapped_column(String(40))  # exploration/appraisal/development/...
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
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspace.id", ondelete="SET NULL"))
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
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
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
        ForeignKey("document_version.id", ondelete="SET NULL", deferrable=True, initially="DEFERRED")
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
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), nullable=False)
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
    supersedes_version_id: Mapped[str | None] = mapped_column(ForeignKey("document_version.id", ondelete="SET NULL"))
    superseded_by_version_id: Mapped[str | None] = mapped_column(ForeignKey("document_version.id", ondelete="SET NULL"))
    #: 'NEW' / 'MODIFIED' / 'DUPLICATE' - why this version exists (section 13).
    origin: Mapped[str] = mapped_column(String(16), default="NEW", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Approval information is kept on the version, since that is what is approved.
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Duplicate handling: pointer to the version holding identical content.
    duplicate_of_version_id: Mapped[str | None] = mapped_column(ForeignKey("document_version.id", ondelete="SET NULL"))
    metadata_json: Mapped[dict | None] = mapped_column(JSON, default=dict)

    document: Mapped[Document] = relationship(back_populates="versions", foreign_keys="DocumentVersion.document_id")
    extractions: Mapped[list[Extraction]] = relationship(back_populates="version", cascade="all, delete-orphan")


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
        Index("ix_extraction_cache", "content_sha256", "extractor", "extractor_version", "config_hash"),
        Index("ix_extraction_version", "document_version_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_version.id", ondelete="CASCADE"), nullable=False)
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
        UniqueConstraint("content_sha256", "extractor", "extractor_version", "config_hash", name="uq_extraction_cache_key"),
        Index("ix_extraction_cache_sha", "content_sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor: Mapped[str] = mapped_column(String(48), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(48), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    #: The stored artefact this key resolves to.
    extraction_id: Mapped[str | None] = mapped_column(ForeignKey("extraction.id", ondelete="CASCADE"))
    #: How many versions have reused this artefact instead of re-parsing it.
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The first version this artefact was produced for (provenance of the cache entry).
    produced_by_version_id: Mapped[str | None] = mapped_column(ForeignKey("document_version.id", ondelete="SET NULL"))
    #: True when the entry was written by a forced re-extraction replacing an older one.
    refreshed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# --------------------------------------------------------------------------- knowledge
class Source(Base, TimestampMixin):
    """A citable source of information with its (configurable) authority tier."""

    __tablename__ = "source"
    __table_args__ = (UniqueConstraint("kind", "reference", name="uq_source_kind_reference"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # document|manual|calculation|inference|reference
    #: Stable citation key: document version id, book ISBN/section, user id...
    reference: Mapped[str] = mapped_column(String(512), nullable=False)
    label: Mapped[str] = mapped_column(String(400), nullable=False)
    authority_tier: Mapped[str] = mapped_column(String(64), default="general_knowledge", nullable=False)
    #: 0-100; lets a reviewer override the tier ordering deterministically.
    authority_score: Mapped[float | None] = mapped_column(Float)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(ForeignKey("document_version.id", ondelete="SET NULL"))
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
    #: PLANNED/ACTUAL/... so a planned and an actual value never collide (section 11).
    record_state: Mapped[str] = mapped_column(String(16), default="CURRENT", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="CANDIDATE", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    well_id: Mapped[str | None] = mapped_column(ForeignKey("well.id", ondelete="SET NULL"))
    section_id: Mapped[str | None] = mapped_column(ForeignKey("well_section.id", ondelete="SET NULL"))
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id", ondelete="SET NULL"))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source.id", ondelete="SET NULL"))
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id", ondelete="SET NULL"))
    document_version_id: Mapped[str | None] = mapped_column(ForeignKey("document_version.id", ondelete="SET NULL"))
    provenance: Mapped[list | None] = mapped_column(JSON, default=list)
    evidence: Mapped[list | None] = mapped_column(JSON, default=list)
    #: Set when a stronger source supersedes this item.
    superseded_by: Mapped[str | None] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(80), default="system", nullable=False)


class KnowledgeRelation(Base, TimestampMixin):
    """Directed, typed, provenance-carrying edge (section 20)."""

    __tablename__ = "knowledge_relation"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "relation", "target_type", "target_id", name="uq_relation_edge"),
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
    detected_by: Mapped[str] = mapped_column(String(64), default="conflict_detector", nullable=False)


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
    skill_id: Mapped[str] = mapped_column(ForeignKey("skill.id", ondelete="CASCADE"), nullable=False)
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
    section_id: Mapped[str | None] = mapped_column(ForeignKey("well_section.id", ondelete="SET NULL"))
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
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("calculation.id", ondelete="SET NULL"))
    reviewer: Mapped[str | None] = mapped_column(String(200))
    approval_note: Mapped[str | None] = mapped_column(Text)
    #: How the run was triggered (ui/cli/agent) - part of the audit trail.
    triggered_by: Mapped[str] = mapped_column(String(40), default="cli", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    input_records: Mapped[list[CalculationInput]] = relationship(back_populates="calculation", cascade="all, delete-orphan")


class CalculationInput(Base):
    """Indexed view of calculation inputs for change-impact analysis (section 44).

    ``subject_key`` names the *source* of an input (a document field, a
    knowledge item, or a well/section attribute).  When that source changes, the
    platform can list every calculation that consumed it.
    """

    __tablename__ = "calculation_input"
    __table_args__ = (Index("ix_calc_input_subject", "subject_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    calculation_id: Mapped[str] = mapped_column(ForeignKey("calculation.id", ondelete="CASCADE"), nullable=False)
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
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspace.id", ondelete="SET NULL"))
    root_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
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


__all__ = [
    "AuditEvent",
    "Base",
    "Calculation",
    "CalculationInput",
    "Company",
    "Document",
    "DocumentVersion",
    "Extraction",
    "ExtractionCache",
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
]
