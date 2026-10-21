"""ORM declarative base with a portable naming convention.

The schema is deliberately dialect-neutral (master spec section 51): the same
metadata must create a working SQLite file today and a PostgreSQL schema later,
so no SQLite-specific SQL, no dialect-vendor types and no raw SQL live in the
domain layer.  Derived *indexes* (FTS5, sqlite-vec vectors) are kept in a
separate sidecar database precisely so the system of record stays portable.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, InstrumentedAttribute

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # The schema layout every check reads, stated once.  These are plain
    # annotations and not ``Mapped[...]``: the declarative scanner ignores
    # them, so no model inherits a column from them and the mapping of the
    # schema is exactly what it was before.  Every model in ``models.py``
    # names its table and carries an ``id``, and the integrity checks read
    # both from a list of model classes, so the type of the class has to
    # carry them too.
    __tablename__: str
    id: InstrumentedAttribute[str]
