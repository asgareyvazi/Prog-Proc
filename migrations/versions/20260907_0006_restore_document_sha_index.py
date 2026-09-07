"""restore the legacy document hash index before historical downgrades

Revision ID: 0006
Revises: 0005

Migration 0002's SQLite batch rebuild omits the ``ix_document_sha256`` index that
0001 created.  The omission is harmless for current reads but makes a real
head-to-base downgrade fail when 0001 tries to drop the missing index.  This
forward-compatible no-op restores the legacy index on upgrade and removes it on
downgrade, allowing the unchanged historical migrations to complete safely.
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite and PostgreSQL both support this form, and it tolerates a database
    # where an operator already repaired the omitted index.
    op.execute("CREATE INDEX IF NOT EXISTS ix_document_sha256 ON document (sha256)")


def downgrade() -> None:
    # Keep the restored index while traversing 0005 -> 0001.  The original
    # 0001 downgrade owns its removal; dropping it here would recreate the
    # historical failure one revision later.
    pass
