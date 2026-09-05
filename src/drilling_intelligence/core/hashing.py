"""Content hashing and identity helpers used by incremental ingestion."""

from __future__ import annotations

import contextlib
import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

CHUNK = 1 << 20  # 1 MiB - keeps large DDR workbooks and PDFs off the memory budget
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: Path | str, chunk_size: int = CHUNK) -> str:
    """Streaming SHA-256 of file contents (never loads the whole file)."""
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sha256_obj(obj: object) -> str:
    """Stable hash of a JSON-serialisable structure (sorted keys, no whitespace)."""
    import json

    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def short_hash(digest: str, length: int = 12) -> str:
    return (digest or "")[:length]


def is_sha256(value: str) -> bool:
    return bool(_HASH_RE.match(value or ""))


def truncate_digest(digest: str, length: int = 16) -> str:
    return short_hash(digest, length)


def identity_slug(value: str, max_length: int = 48) -> str:
    """Filesystem/DB friendly slug (used for workspace ids and export names)."""
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "item")[:max_length]


def filename_identity(path: Path | str, root: Path | str | None = None) -> str:
    """Normalised path identity used to decide "same document, new content".

    Two paths map to the same identity when they refer to the same relative
    location inside the workspace, case-insensitively, with separators
    normalised.  This is deliberately *not* the content hash: identity answers
    "is this the same document slot", the hash answers "did it change".
    """
    path = Path(path)
    if root is not None:
        root_path = Path(root).resolve()
        with contextlib.suppress(ValueError):
            path = path.resolve().relative_to(root_path)
    text = unicodedata.normalize("NFKC", str(path)).replace("\\", "/")
    text = re.sub(r"/{2,}", "/", text).strip("./").casefold()
    return text


def utc_now() -> datetime:
    """Single timezone-aware clock source for the whole platform."""
    return datetime.now(UTC)


def iso_utc(moment: datetime | None = None) -> str:
    moment = moment or utc_now()
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "CHUNK",
    "filename_identity",
    "identity_slug",
    "is_sha256",
    "iso_utc",
    "sha256_bytes",
    "sha256_file",
    "sha256_obj",
    "sha256_text",
    "short_hash",
    "truncate_digest",
    "utc_now",
]
