"""Identifier helpers.

Ids are opaque strings (``uuid4`` hex) generated in the application, not by the
database, so a record is addressable before it is flushed and so SQLite and
PostgreSQL behave identically (docs/DECISIONS.md ADR-0004).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


def new_id(prefix: str = "") -> str:
    """Return a new opaque id, optionally namespaced (``well:1a2b...``)."""
    value = uuid.uuid4().hex
    return f"{prefix}{value}" if not prefix else f"{prefix}-{value}"


def is_new_id(value: str, prefix: str = "") -> bool:
    return bool(value) and len(value) == (32 + len(prefix) + 1 if prefix else 32)


@dataclass(frozen=True)
class SubjectKey:
    """Stable key for a *thing a value belongs to*, used for change impact.

    Format: ``well:<well_id>|section:<name>|property:<name>|state:<state>``.
    Building it in one place is what makes "which calculations used this mud
    weight?" a lookup instead of a guess (master spec section 44).
    """

    well_id: str = ""
    section_id: str = ""
    property_name: str = ""
    record_state: str = ""
    document_id: str = ""

    def render(self) -> str:
        parts = []
        if self.well_id:
            parts.append(f"well:{self.well_id}")
        if self.section_id:
            parts.append(f"section:{self.section_id}")
        if self.property_name:
            parts.append(f"property:{self.property_name}")
        if self.record_state:
            parts.append(f"state:{self.record_state}")
        if self.document_id:
            parts.append(f"document:{self.document_id}")
        return "|".join(parts) or "unscoped"

    @classmethod
    def parse(cls, rendered: str) -> SubjectKey:
        fields = {
            part.split(":", 1)[0]: part.split(":", 1)[1]
            for part in (rendered or "").split("|")
            if ":" in part
        }
        return cls(
            well_id=fields.get("well", ""),
            section_id=fields.get("section", ""),
            property_name=fields.get("property", ""),
            record_state=fields.get("state", ""),
            document_id=fields.get("document", ""),
        )

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.render()


def subject_key(**kwargs: str) -> str:
    return SubjectKey(**kwargs).render()


__all__ = ["SubjectKey", "is_new_id", "new_id", "subject_key"]
