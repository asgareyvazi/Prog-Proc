"""Identifier helpers.

Ids are opaque strings (``uuid4`` hex) generated in the application, not by the
database, so a record is addressable before it is flushed and so SQLite and
PostgreSQL behave identically (docs/DECISIONS.md ADR-0004).

:class:`SubjectKey` is the platform's one canonical *subject* identity: the thing a
value belongs to.  It is what makes "which calculations used this mud weight?" a
lookup instead of a guess (docs/DECISIONS.md ADR-0016).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from .hashing import sha256_text


def new_id(prefix: str = "") -> str:
    """Return a new opaque id, optionally namespaced (``well:1a2b...``)."""
    value = uuid.uuid4().hex
    return f"{prefix}{value}" if not prefix else f"{prefix}-{value}"


def is_new_id(value: str, prefix: str = "") -> bool:
    return bool(value) and len(value) == (32 + len(prefix) + 1 if prefix else 32)


#: The anchor kinds a canonical subject may be rooted at, in precedence order.  An anchor is the
#: *durable thing* a subject hangs off; the property and record state narrow it.  A rendered key
#: whose leading component is not one of these is not canonical - it is legacy free text, and the
#: platform says so rather than guessing (ADR-0016).
ANCHOR_KINDS: tuple[str, ...] = (
    "well",
    "section",
    "document_version",
    "document",
    "project",
)

#: The marker a stored subject carries when it could not be recognised as canonical.  It is a
#: label, never an interpretation: the original text is preserved verbatim beside it.
LEGACY_KIND = "legacy"

#: The widest canonical rendering that is stored literally.  Anything longer is stored as a
#: deterministic digest (see :meth:`SubjectKey.storage_key`) rather than silently truncated.
MAX_RENDERED_LENGTH = 300

#: The prefix of the digest form.  ``k256:`` + sha256 hex is 69 characters, so it always fits.
DIGEST_PREFIX = "k256:"

_SEPARATOR = "|"
_ASSIGN = ":"
_ESCAPE = "\\"


def _escape(value: str) -> str:
    """Make a component safe to place between ``|`` and ``:`` delimiters.

    Only the three characters that can be confused with structure are touched, so a component
    without them renders byte-for-byte as it always has.  That is what keeps every key the
    knowledge layer has ever written - where property names are snake_case tokens and ids are
    opaque hex - unchanged by the introduction of escaping.
    """
    text = str(value)
    if not (_ESCAPE in text or _SEPARATOR in text or _ASSIGN in text):
        return text
    out = []
    for char in text:
        if char in (_ESCAPE, _SEPARATOR, _ASSIGN):
            out.append(_ESCAPE)
        out.append(char)
    return "".join(out)


def _unescape(value: str) -> str:
    out: list[str] = []
    pending = False
    for char in value:
        if pending:
            out.append(char)
            pending = False
        elif char == _ESCAPE:
            pending = True
        else:
            out.append(char)
    if pending:  # a trailing lone backslash is data, not a broken escape
        out.append(_ESCAPE)
    return "".join(out)


def _split_unescaped(text: str, delimiter: str, *, maxsplit: int = -1) -> list[str]:
    """Split on *delimiter* occurrences that are not escaped, honouring ``maxsplit``."""
    parts: list[str] = []
    current: list[str] = []
    pending = False
    splits = 0
    for char in text:
        if pending:
            current.append(char)
            pending = False
            continue
        if char == _ESCAPE:
            current.append(char)
            pending = True
            continue
        if char == delimiter and (maxsplit < 0 or splits < maxsplit):
            parts.append("".join(current))
            current = []
            splits += 1
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def normalize_property(value: str) -> str:
    """The documented normalization rule for a property name (ADR-0016).

    A property name is a *token*, not prose: it is lowercased, its separators (spaces, hyphens
    and dots) become underscores, and runs of underscores collapse to one.  ``Mud Weight``,
    ``MUD_WEIGHT``, ``mud-weight`` and ``mud weight`` are therefore the same property, which is
    the whole point - two engineers naming one quantity differently must not produce two
    subjects.  Every predicate the knowledge layer emits is already in this form, so normalising
    is a no-op on the data this platform has written to date.
    """
    text = str(value or "").strip().lower()
    for char in (" ", "-", ".", "\t"):
        text = text.replace(char, "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def normalize_state(value: str) -> str:
    """A record state is an enum token: upper case, underscores, no surrounding space."""
    text = str(value or "").strip().upper()
    for char in (" ", "-", ".", "\t"):
        text = text.replace(char, "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


@dataclass(frozen=True)
class SubjectKey:
    """Stable key for a *thing a value belongs to*, used for change impact.

    Format: ``<anchor>|property:<name>|state:<state>``, where the anchor is one of
    ``well:``, ``section:``, ``document_version:``, ``document:`` or ``project:``.  The classic
    five-field form (``well:…|section:…|property:…|state:…``) is unchanged; ``entity_type`` /
    ``entity_id`` express the anchors the knowledge layer prepends for a fact that belongs to a
    document version rather than a well, and ``project_id`` the one it appends.

    Three guarantees (ADR-0016):

    *   **deterministic** - the same components always render the same bytes, in the same order,
        with no timestamp, counter or random element anywhere;
    *   **reversible** - :meth:`parse` returns the components :meth:`render` was given, including
        components that contain the ``|`` and ``:`` delimiters themselves;
    *   **collision-free** - a component containing a delimiter is escaped, so
        ``property="x|state:PLANNED"`` and ``property="x", state="PLANNED"`` are different keys.
    """

    well_id: str = ""
    section_id: str = ""
    property_name: str = ""
    record_state: str = ""
    document_id: str = ""
    entity_type: str = ""
    entity_id: str = ""
    project_id: str = ""

    def render(self) -> str:
        """The canonical rendering.

        Component order is fixed (anchor, well, section, property, state, document, project), so
        a caller cannot change the key by changing the order it supplied things in.  Components
        are escaped only when they contain a delimiter, which is what makes this rendering
        byte-identical to the one this repository has always produced for real data.
        """
        parts: list[str] = []
        if self.entity_type and self.entity_id:
            parts.append(f"{_escape(self.entity_type)}{_ASSIGN}{_escape(self.entity_id)}")
        if self.well_id:
            parts.append(f"well{_ASSIGN}{_escape(self.well_id)}")
        if self.section_id:
            parts.append(f"section{_ASSIGN}{_escape(self.section_id)}")
        if self.property_name:
            parts.append(f"property{_ASSIGN}{_escape(self.property_name)}")
        if self.record_state:
            parts.append(f"state{_ASSIGN}{_escape(self.record_state)}")
        if self.document_id:
            parts.append(f"document{_ASSIGN}{_escape(self.document_id)}")
        if self.project_id:
            parts.append(f"project{_ASSIGN}{_escape(self.project_id)}")
        return _SEPARATOR.join(parts) or "unscoped"

    @classmethod
    def parse(cls, rendered: str) -> SubjectKey:
        """The inverse of :meth:`render`, tolerant of the unescaped keys written before ADR-0016.

        Unknown leading ``kind:id`` components are kept as ``entity_type``/``entity_id`` when the
        kind is a recognised anchor; anything else is dropped, which is exactly why
        :func:`is_canonical_subject` exists - a string that does not round-trip is legacy text and
        must not be treated as an identity.
        """
        text = str(rendered or "")
        fields: dict[str, str] = {}
        entity: tuple[str, str] = ("", "")
        for part in _split_unescaped(text, _SEPARATOR):
            if not part:
                continue
            pieces = _split_unescaped(part, _ASSIGN, maxsplit=1)
            if len(pieces) != 2:
                continue
            key = _unescape(pieces[0])
            value = _unescape(pieces[1])
            if key in ("well", "section", "property", "state", "document", "project"):
                fields.setdefault(key, value)
            elif key in ANCHOR_KINDS and not entity[0]:
                entity = (key, value)
        return cls(
            well_id=fields.get("well", ""),
            section_id=fields.get("section", ""),
            property_name=fields.get("property", ""),
            record_state=fields.get("state", ""),
            document_id=fields.get("document", ""),
            entity_type=entity[0],
            entity_id=entity[1],
            project_id=fields.get("project", ""),
        )

    def canonical(self) -> SubjectKey:
        """This subject with the documented normalization applied to every component.

        Ids keep their case (they are opaque tokens and ``Well-A`` is not ``well-a``); the
        property name and the record state are normalised, because those are the two components
        a human types.
        """
        return replace(
            self,
            well_id=str(self.well_id or "").strip(),
            section_id=str(self.section_id or "").strip(),
            property_name=normalize_property(self.property_name),
            record_state=normalize_state(self.record_state),
            document_id=str(self.document_id or "").strip(),
            entity_type=normalize_property(self.entity_type),
            entity_id=str(self.entity_id or "").strip(),
            project_id=str(self.project_id or "").strip(),
        )

    def canonical_key(self) -> str:
        """The normalised canonical rendering - the form two equivalent subjects agree on."""
        return self.canonical().render()

    def storage_key(self, *, limit: int = MAX_RENDERED_LENGTH) -> str:
        """The canonical key as it is stored, never silently truncated.

        A rendering longer than the column can hold is stored as ``k256:<sha256>`` of the
        canonical rendering.  The digest is deterministic and is computed identically on the
        write path and the read path, so a long subject stays findable by the key its author
        used - which a truncating write followed by an exact-match read does not.
        """
        rendered = self.canonical_key()
        if len(rendered) <= limit:
            return rendered
        return f"{DIGEST_PREFIX}{sha256_text(rendered)}"

    def anchor(self) -> tuple[str, str]:
        """The ``(kind, id)`` this subject hangs off, or ``("", "")`` when it has none."""
        canonical = self.canonical()
        if canonical.entity_type in ANCHOR_KINDS and canonical.entity_id:
            return (canonical.entity_type, canonical.entity_id)
        for kind, value in (
            ("well", canonical.well_id),
            ("section", canonical.section_id),
            ("document_version", ""),
            ("document", canonical.document_id),
            ("project", canonical.project_id),
        ):
            if value:
                return (kind, value)
        return ("", "")

    @property
    def is_anchored(self) -> bool:
        return bool(self.anchor()[0])

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.render()


def subject_key(**kwargs: str) -> str:
    return SubjectKey(**kwargs).render()


def is_canonical_subject(rendered: str) -> bool:
    """True when *rendered* is a subject key this platform can interpret as an identity.

    The test is a round trip: parse it, render it again, and require the result to equal the
    input.  A string that survives that is structured data; one that does not is free text that
    happens to contain a colon (``"well:A-3|mud_weight"``, ``"mud_report.xlsx!Summary!B9"``), and
    the platform stores it verbatim and labels it legacy rather than inventing an identity for
    it.  An anchor is required as well: ``property:x`` alone names no durable thing.
    """
    text = str(rendered or "").strip()
    if not text or text == "unscoped":
        return False
    parsed = SubjectKey.parse(text)
    if not parsed.is_anchored:
        return False
    return parsed.render() == text


__all__ = [
    "ANCHOR_KINDS",
    "DIGEST_PREFIX",
    "LEGACY_KIND",
    "MAX_RENDERED_LENGTH",
    "SubjectKey",
    "is_canonical_subject",
    "is_new_id",
    "new_id",
    "normalize_property",
    "normalize_state",
    "subject_key",
]
