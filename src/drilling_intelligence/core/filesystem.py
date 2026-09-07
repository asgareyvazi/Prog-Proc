"""Filesystem facts that are easy to state wrongly: timestamps and paths.

Timestamp semantics (this matters - the registry stores these and people cite them):

*   ``st_mtime`` is the last *content* modification time.  Portable, real, useful.
*   ``st_ctime`` is the **inode change time** on POSIX - it moves on ``chmod``,
    ``chown``, a rename or a link-count change.  On Windows it *is* the creation time.
    One field name, two meanings, so it must never be labelled "created".
*   ``st_birthtime`` is a genuine creation time.  Python exposes it on platforms whose
    ``stat`` provides it (macOS/BSD; Windows reports creation through ``st_ctime``).
    Linux may or may not, depending on the filesystem and glibc version.

The rules this module enforces, and that the rest of the codebase follows:

1.  :func:`file_timestamps` returns creation, modification and metadata-change times
    separately and says whether the creation time is authoritative.  On a platform
    without birth time, ``created_at`` is ``None`` - we do not substitute the inode
    change time and let a UI present it as "created".
2.  Filesystem timestamps are *observations about the file on this disk*.  They are
    never used as document revision dates (master spec section 15): copying a 2019
    report into a workspace gives it this week's mtime, and that says nothing about
    the document.
3.  :func:`candidate_source_paths` resolves a recorded source location against the
    *current* workspace, so provenance stays readable after the workspace folder is
    moved to another drive or machine: the durable reference is the workspace-relative
    path, the absolute path is only a convenience for the machine that scanned it.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "FileTimestamps",
    "candidate_source_paths",
    "file_timestamps",
    "has_creation_time_support",
    "posix_relative",
]

#: ``os.stat_result`` exposes ``st_birthtime`` only where the platform reports it.
_BIRTHTIME_SUPPORTED = hasattr(os.stat_result, "st_birthtime")


def has_creation_time_support() -> bool:
    """True when this platform's ``stat`` distinguishes creation from inode change."""
    return _BIRTHTIME_SUPPORTED


def platform_note() -> str:
    """Human-readable explanation of what the recorded timestamps mean here."""
    if _BIRTHTIME_SUPPORTED:
        return "creation time read from st_birthtime (authoritative); st_ctime recorded separately as metadata-change time"
    return (
        "this platform reports no birth time, so file_created_at stays empty; "
        "st_ctime is recorded as metadata_changed_at only - it is the inode change time on POSIX, not a creation time"
    )


@dataclass(frozen=True)
class FileTimestamps:
    """What a ``stat`` call can honestly say about a file's history."""

    modified_at: datetime
    metadata_changed_at: datetime
    created_at: datetime | None = None
    #: False when ``created_at`` could not be determined (POSIX without birth time).
    creation_is_authoritative: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready form, stored in ``document_version.metadata_json['filesystem']``."""
        return {
            "modified_at": self.modified_at.isoformat() if self.modified_at else None,
            "metadata_changed_at": self.metadata_changed_at.isoformat()
            if self.metadata_changed_at
            else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "creation_is_authoritative": self.creation_is_authoritative,
            "note": self.note,
        }


def _aware(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def file_timestamps(path: Path | str, *, stat: os.stat_result | None = None) -> FileTimestamps:
    """Read the timestamps of ``path`` with their real platform semantics.

    ``stat`` may be passed in when the caller has already stated the file - the values
    then come from that snapshot, which keeps a registration run consistent even if the
    file is written to while we are processing it.
    """
    stat_result = stat if stat is not None else Path(path).stat()
    birth: float | None = None
    if _BIRTHTIME_SUPPORTED:
        birth = getattr(stat_result, "st_birthtime", None)
    created = _aware(birth) if birth is not None else None
    return FileTimestamps(
        modified_at=_aware(stat_result.st_mtime),
        # On POSIX this is "inode changed"; on Windows it is the creation time, which is
        # why it is kept in its own field instead of being renamed into created_at.
        metadata_changed_at=_aware(stat_result.st_ctime),
        created_at=created,
        creation_is_authoritative=created is not None,
        note=platform_note(),
    )


# --------------------------------------------------------------------------- paths
def posix_relative(path: Path | str, root: Path | str | None = None) -> str:
    """Canonical workspace-relative path (forward slashes, case preserved).

    Rules, all of them needed by the durable reference in ``document_version``:

    *   backslashes become forward slashes even on POSIX - the path may have been
        recorded on Windows, and ``identity_path`` already normalises the same way, so
        disagreeing here would make the display path and the identity path diverge;
    *   ``.`` and ``..`` segments collapse against the real filesystem, so two spellings
        of one location store one path;
    *   a file *outside* the root degrades to its name (matching the scanner), because a
        path that is relative to nothing is not a durable reference;
    *   case is preserved: this is what a human re-opens, unlike the casefolded identity.
    """
    candidate = Path(path)
    if root is None:
        return _normalise(candidate)
    try:
        return _normalise(candidate.resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):  # outside the workspace, or an unreadable mount point
        return candidate.name


def _normalise(path: Path) -> str:
    text = unicodedata.normalize("NFKC", str(path)).replace("\\", "/")
    text = re.sub(r"/{2,}", "/", text)
    parts = [part for part in text.split("/") if part not in {"", "."}]
    return "/".join(parts)


def candidate_source_paths(
    *,
    recorded_path: str = "",
    workspace_root: Path | str | None = None,
    relative_path: str = "",
    filename: str = "",
) -> list[Path]:
    """Where the file behind a registry entry *might* be, most likely first.

    The absolute path recorded at scan time is tried first because it is exact; the
    workspace-relative path is tried second because it survives the workspace being
    moved, renamed or mounted elsewhere.  Duplicate candidates are collapsed so a
    relocation that leaves the absolute path valid does not halve the work.
    """
    candidates: list[Path] = []

    def add(candidate: Path) -> None:
        if candidate not in candidates:
            candidates.append(candidate)

    if recorded_path:
        add(Path(recorded_path))
    if workspace_root is not None and relative_path:
        add(Path(workspace_root) / relative_path)
    if workspace_root is not None and not relative_path and filename:
        # Records written before relative paths were stored: fall back to the name.
        add(Path(workspace_root) / filename)
    return candidates


def first_existing(candidates: list[Path]) -> Path | None:
    """First candidate that is a readable file, or ``None``."""
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - unreadable mount point
            continue
    return None
