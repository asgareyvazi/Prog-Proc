"""current-version invariants, durable source paths and the unique extraction cache

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05

Why this migration exists (it tightens rules that Phase 0 only stated in comments):

*   ``document.current_version_id`` becomes a real foreign key to
    ``document_version.id`` (``ON DELETE SET NULL``, deferred because document and
    document_version reference each other).  On SQLite a foreign key can only be added
    by rebuilding the table, so this runs inside ``batch_alter_table``; PostgreSQL takes
    the same code path and simply issues ``ALTER TABLE ... ADD CONSTRAINT``.
*   ``uq_document_version_one_current`` - a *partial unique index* (supported by SQLite
    and PostgreSQL) allowing at most one ``is_current`` version per document.  Existing
    rows are repaired first, deterministically: the highest version number of each
    document becomes the current one and the registry pointer is set to it.  A
    migration that only adds the index would fail on a database that already had two
    current versions, which is precisely the state worth fixing.
*   ``document.fs_metadata_changed_at`` records ``st_ctime`` under its real meaning
    (inode change time on POSIX), and ``file_created_at`` is left alone: from now on it
    is only written where the platform reports a genuine birth time.
*   ``document_version.source_relative_path`` is the durable citation - the path inside
    the workspace - so history stays resolvable after the workspace folder is moved.
    Backfilled from the document identity, which is the same path.
*   ``extraction_cache`` holds exactly one row per
    ``(content_sha256, extractor, extractor_version, config_hash)``.  ``document_version_id``
    is deliberately *not* in the key: one artefact may be reused by any number of
    versions.  Existing extractions are backfilled to the artefact that first produced
    each key, so a cache built before this migration keeps working.

The ordering note matters: the partial unique index and the cache index are created
*after* the batch rebuild of ``document``, because SQLite's table rebuild reflects the
existing indexes and would otherwise re-create a partial index as a plain one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


#: The ``document`` table as 0001 created it, restated so the table can be rebuilt
#: without reflecting it.  ``batch_alter_table`` needs this to work in ``--sql``
#: (offline) mode, where there is no live connection to reflect through; listing the
#: indexes here too means the rebuild does not silently drop them.
def _document_table(*, with_current_version_fk: bool) -> sa.Table:
    """``document`` as it looks *at the moment a batch rebuild starts*.

    ``copy_from`` is what makes ``batch_alter_table`` work offline and on a database whose
    indexes alembic must not have to discover: it restates every column, constraint and
    index of the table being copied, because SQLite's rebuild is
    "create the new table, copy the rows, drop the old one, rename" - anything left out of
    this description is silently lost from the user's workspace.  ``fs_metadata_changed_at``
    is listed here even though this migration adds it: by the time the rebuild runs it
    exists, and the copy has to carry it across.
    """
    table = sa.Table(
        "document",
        sa.MetaData(),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("well_id", sa.String(length=36), nullable=True),
        sa.Column("identity_path", sa.String(length=1024), nullable=True),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("extension", sa.String(length=16), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("file_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fs_metadata_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("classification", sa.String(length=40), nullable=False),
        sa.Column("classification_confidence", sa.Float(), nullable=True),
        sa.Column("title", sa.String(length=400), nullable=True),
        sa.Column("document_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.String(length=64), nullable=True),
        sa.Column("revision_key", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_authority", sa.String(length=64), nullable=True),
        sa.Column("wellbore", sa.String(length=80), nullable=True),
        sa.Column("interval_from", sa.String(length=40), nullable=True),
        sa.Column("interval_to", sa.String(length=40), nullable=True),
        sa.Column("processing_status", sa.String(length=24), nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("current_version_id", sa.String(length=36), nullable=True),
        sa.Column("change_count", sa.Integer(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_document"),
        sa.UniqueConstraint("workspace_id", "identity_path", name="uq_document_workspace_identity"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_document_workspace_id_workspace",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name="fk_document_project_id_project",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["well_id"], ["well.id"], name="fk_document_well_id_well", ondelete="SET NULL"
        ),
        sa.Index("ix_document_well", "well_id"),
        sa.Index("ix_document_classification", "classification"),
        sa.Index("ix_document_status", "processing_status"),
    )
    if with_current_version_fk:
        table.append_constraint(
            sa.ForeignKeyConstraint(
                ["current_version_id"],
                ["document_version.id"],
                name="fk_document_current_version_id_document_version",
                ondelete="SET NULL",
                deferrable=True,
                initially="DEFERRED",
            )
        )
    return table


def _document_table_v1() -> sa.Table:
    """The table as the *upgrade* finds it: new column present, new foreign key absent."""
    return _document_table(with_current_version_fk=False)


def _document_table_v2() -> sa.Table:
    """The table as the *downgrade* finds it: with the foreign key, so it can be dropped."""
    return _document_table(with_current_version_fk=True)


def upgrade() -> None:
    # -- new columns (both dialects support ADD COLUMN directly) ------------
    op.add_column(
        "document", sa.Column("fs_metadata_changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "document_version", sa.Column("source_relative_path", sa.String(length=1024), nullable=True)
    )

    # -- repair the data first, so the constraints below can be created -----
    # "Exactly one current version per document": the highest version number wins.
    # Written as boolean literals (not 0/1) because PostgreSQL needs them, and a bare
    # ``and v.is_current`` because SQLite accepts that form too.
    op.execute("update document_version set is_current = false")
    op.execute(
        "update document_version set is_current = true "
        "where (document_id, version_number) in ("
        "  select document_id, max(version_number) from document_version group by document_id)"
    )
    # The registry pointer has to name that version (and a document with no versions
    # must stop pointing at anything).
    op.execute(
        "update document set current_version_id = ("
        "  select v.id from document_version v"
        "  where v.document_id = document.id and v.is_current"
        ")"
    )
    # The durable path reference: the version's location inside its workspace, which is
    # the document identity - the same string, but frozen per version from now on.
    op.execute(
        "update document_version set source_relative_path = "
        "(select d.identity_path from document d where d.id = document_version.document_id) "
        "where source_relative_path is null"
    )

    # -- constraints --------------------------------------------------------
    op.create_index(
        "uq_document_version_one_current",
        "document_version",
        ["document_id"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
        postgresql_where=sa.text("is_current"),
    )

    # SQLite cannot ADD a foreign key to an existing table, so the table is rebuilt from
    # the definition above (offline ``--sql`` capable, because nothing is reflected).
    # On PostgreSQL the constraint is simply added.
    bind_dialect = op.get_context().dialect.name
    if bind_dialect == "sqlite":
        with op.batch_alter_table(
            "document", copy_from=_document_table_v1(), recreate="always"
        ) as batch_op:
            batch_op.create_foreign_key(
                "fk_document_current_version_id_document_version",
                "document_version",
                ["current_version_id"],
                ["id"],
                ondelete="SET NULL",
                deferrable=True,
                initially="DEFERRED",
            )
    else:
        op.create_foreign_key(
            "fk_document_current_version_id_document_version",
            "document",
            "document_version",
            ["current_version_id"],
            ["id"],
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        )

    # -- the unique extraction cache ---------------------------------------
    op.create_table(
        "extraction_cache",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("extractor", sa.String(length=48), nullable=False),
        sa.Column("extractor_version", sa.String(length=48), nullable=False),
        sa.Column("config_hash", sa.String(length=16), nullable=False),
        sa.Column("extraction_id", sa.String(length=36), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("produced_by_version_id", sa.String(length=36), nullable=True),
        sa.Column("refreshed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_cache")),
        sa.UniqueConstraint(
            "content_sha256",
            "extractor",
            "extractor_version",
            "config_hash",
            name="uq_extraction_cache_key",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["extraction.id"],
            name=op.f("fk_extraction_cache_extraction_id_extraction"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["produced_by_version_id"],
            ["document_version.id"],
            name=op.f("fk_extraction_cache_produced_by_version_id_document_version"),
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_extraction_cache_sha", "extraction_cache", ["content_sha256"], unique=False)
    op.execute(_cache_backfill())


def _cache_backfill() -> str:
    """Publish the artefacts an existing database already has, one entry per key.

    "First producer" is the oldest row for the key, chosen by a correlated subquery so
    the statement is one static string that SQLite and PostgreSQL both accept.  Only
    rows that carry a parsed artefact qualify: a failed extraction is not cacheable.

    The id is derived from the extraction's own hex body so it stays inside ``String(36)``
    (SQLite would not complain about a longer one; PostgreSQL would reject the insert and
    the upgrade would die half-applied).
    """
    return (
        "insert into extraction_cache"
        " (id, content_sha256, extractor, extractor_version, config_hash, extraction_id,"
        "  hits, produced_by_version_id, refreshed, created_at, updated_at) "
        "select 'xc-' || substr(e.id, 5, 32), e.content_sha256, e.extractor, e.extractor_version, e.config_hash, e.id,"
        "       0, e.document_version_id, false, e.created_at, e.updated_at "
        "from extraction e "
        "where e.status = 'OK' and e.document_json is not null "
        "  and e.id = ("
        "    select e2.id from extraction e2"
        "    where e2.status = 'OK' and e2.document_json is not null"
        "      and e2.content_sha256 = e.content_sha256 and e2.extractor = e.extractor"
        "      and e2.extractor_version = e.extractor_version and e2.config_hash = e.config_hash"
        "    order by e2.created_at asc, e2.id asc limit 1)"
    )


def downgrade() -> None:
    op.drop_index("ix_extraction_cache_sha", table_name="extraction_cache")
    op.drop_table("extraction_cache")
    op.drop_index("uq_document_version_one_current", table_name="document_version")
    bind_dialect = op.get_context().dialect.name
    if bind_dialect == "sqlite":
        # ``copy_from`` has to describe the table *with* the constraint being dropped, or
        # batch mode cannot resolve the name and the downgrade dies with "No such constraint".
        with op.batch_alter_table(
            "document", copy_from=_document_table_v2(), recreate="always"
        ) as batch_op:
            batch_op.drop_constraint(
                "fk_document_current_version_id_document_version", type_="foreignkey"
            )
    else:
        op.drop_constraint(
            "fk_document_current_version_id_document_version", "document", type_="foreignkey"
        )
    with op.batch_alter_table("document_version") as batch_op:
        batch_op.drop_column("source_relative_path")
    with op.batch_alter_table("document") as batch_op:
        batch_op.drop_column("fs_metadata_changed_at")
