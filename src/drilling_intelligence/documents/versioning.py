"""Revision and status parsing for the document registry (sections 14 and 29).

Drilling document naming is a mess of conventions - ``Rev 0``, ``R2``,
``Rev.C``, ``03``, ``As-Built``, ``Approved for Drilling``.  Parsing is
*deterministic and additive*: we derive a sortable ``revision_key`` and a status
when the evidence is unambiguous, and we leave the fields empty (recording a
diagnostic) when it is not.  The original filename is never rewritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..core.enums import DocumentStatus

_FILENAME_REV = re.compile(r"(?i)\b(?:rev(?:ision)?\.?|r|v)\s*[-_]?\s*(\d{1,3}|[a-kA-K])\b")
_ORDINAL_REV = re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)\s+revision\b")
_DATE_IN_NAME = re.compile(
    r"(?<!\d)(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)"
)
_DASH_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_STATUS_PATTERNS: tuple[tuple[re.Pattern[str], DocumentStatus], ...] = (
    (
        re.compile(r"(?i)\b(approved for drilling|afd|approved|issued for drilling|ifd)\b"),
        DocumentStatus.APPROVED,
    ),
    (re.compile(r"(?i)\b(draft|rev for comment|pre-release)\b"), DocumentStatus.DRAFT),
    (
        re.compile(r"(?i)\b(superseded|obsolete|old|cancelled|withdrawn)\b"),
        DocumentStatus.SUPERSEDED,
    ),
    (re.compile(r"(?i)\b(as[- ]built|as[- ]executed|final)\b"), DocumentStatus.APPROVED),
    (
        re.compile(r"(?i)\b(issued for review|for review|ifr|for comment)\b"),
        DocumentStatus.ISSUED_FOR_REVIEW,
    ),
)
_CONTENT_REV = re.compile(
    r"(?im)^\s*(?:document|program|report)?\s*revision\s*(?:no\.?|#|:)?\s*([0-9]{1,3}|[A-K])\b"
)
_APPROVAL_LINE = re.compile(
    r"(?im)^(?!.*\bnot\b).{0,40}\b(approved|authorised|authorized)\b.{0,60}$"
)


@dataclass(frozen=True)
class RevisionInfo:
    """What we could legitimately infer about a document's revision state."""

    revision: str = ""
    revision_key: int = 0
    status: DocumentStatus = DocumentStatus.UNKNOWN
    #: Where the evidence came from: filename | content | none
    source: str = "none"
    #: Approval status is tracked separately from revision (section 14).
    approved: bool = False
    notes: tuple[str, ...] = ()
    #: Parsed date associated with the revision (from the filename or content).
    revision_date: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "revision_key": self.revision_key,
            "status": self.status.value,
            "source": self.source,
            "approved": self.approved,
            "notes": list(self.notes),
            "revision_date": self.revision_date.isoformat() if self.revision_date else None,
        }


def revision_from_token(token: str) -> int:
    """``0``->0, ``3``->300, ``C``->300?  Numeric revisions dominate letters."""
    text = (token or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text) * 100
    if text.isalpha() and len(text) == 1:
        return (ord(text.upper()) - ord("A") + 1) * 100 + 50
    return 0


def parse_revision(
    filename: str, content: str = "", file_modified: datetime | None = None
) -> RevisionInfo:
    notes: list[str] = []
    revision = ""
    key = 0
    source = "none"
    revision_date: datetime | None = None

    # Underscores and dashes are word characters, so "program_rev12" has no word
    # boundary in front of "rev": search the raw name *and* a separator-normalised
    # copy, otherwise revision markers written with underscores are invisible.
    normalised_name = re.sub(r"[_\-.]+", " ", filename or "")
    match = _FILENAME_REV.search(filename or "") or _FILENAME_REV.search(normalised_name)
    if match:
        token = match.group(1)
        revision = f"Rev {token}"
        key = revision_from_token(token)
        source = "filename"
    else:
        ordinal = _ORDINAL_REV.search(filename or "")
        if ordinal:
            revision = f"Rev {ordinal.group(1)}"
            key = revision_from_token(ordinal.group(1))
            source = "filename"
    if not revision and content:
        content_match = _CONTENT_REV.search(content[:20000])
        if content_match:
            token = content_match.group(1)
            revision = f"Rev {token}"
            key = revision_from_token(token)
            source = "content"
            notes.append("revision read from the document body, not the filename")

    date_match = _DATE_IN_NAME.search(filename or "")
    if date_match:
        try:
            revision_date = datetime(
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
                tzinfo=UTC,
            )
        except ValueError:
            notes.append(f"filename date {date_match.group(0)!r} is not a valid date; ignored")
    elif file_modified is not None:
        revision_date = None  # file mtime is *not* the document date; never substitute silently

    status = DocumentStatus.UNKNOWN
    approved = False
    for pattern, candidate in _STATUS_PATTERNS:
        # Status markers appear in names written every way ("IFR_well_a3", "as-built",
        # "as_built"): the word-boundary test must see the separator-normalised copy
        # too, or an underscore silently swallows the evidence.
        if pattern.search(filename or "") or pattern.search(normalised_name):
            status = candidate
            approved = candidate is DocumentStatus.APPROVED
            break
    if status is DocumentStatus.UNKNOWN and content:
        head = content[:8000]
        for pattern, candidate in _STATUS_PATTERNS:
            if pattern.search(head):
                status = candidate
                approved = candidate is DocumentStatus.APPROVED
                notes.append("status read from the first pages of the document")
                break
    if _APPROVAL_LINE.search(content or ""):
        approved = True
        if status is DocumentStatus.UNKNOWN:
            status = DocumentStatus.APPROVED
            notes.append("approval line found in content")
    if not revision:
        notes.append("no revision evidence: treated as an unlabelled single version")
    return RevisionInfo(
        revision=revision,
        revision_key=key,
        status=status,
        source=source,
        approved=approved,
        notes=tuple(notes),
        revision_date=revision_date or _dash_date(filename),
    )


def _dash_date(filename: str) -> datetime | None:
    match = _DASH_DATE.search(filename or "")
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=UTC)
    except ValueError:
        return None


def sort_key(info: RevisionInfo, modified: datetime | None) -> tuple[int, float]:
    """Ordering for 'which version is newest': revision first, then mtime."""
    stamp = modified.timestamp() if modified else 0.0
    return (info.revision_key, stamp)


def is_latest(candidates: list[tuple[RevisionInfo, datetime | None]]) -> int:
    """Index of the latest version in a list of ``(revision, modified)`` pairs."""
    if not candidates:
        return -1
    return max(range(len(candidates)), key=lambda i: sort_key(candidates[i][0], candidates[i][1]))


__all__ = ["RevisionInfo", "is_latest", "parse_revision", "revision_from_token", "sort_key"]
