"""DDR batch PDF import (Phase L).

Manifest-driven import of daily drilling report PDFs into the document registry.
Guarantees:

* **Manifest-driven** — a JSON manifest lists the files; nothing outside the manifest
  is touched.
* **Classification** — every imported file gets the deterministic registry
  classification; only DDR-classified files enter the review queue.
* **Review queue** — `review_queue()` lists DDR-classified documents that have no
  extraction disposition yet; import never auto-dispositions content.
* **Idempotency** — re-importing a file whose SHA-256 is already registered as a
  document version is reported SKIPPED_DUPLICATE and creates nothing new.
* **Partial-failure reporting** — a bad entry (missing file, non-PDF, registry
  refusal) is recorded as FAILED with its reason and never aborts the rest of the
  batch; the batch summary reports every outcome.

Imported DDRs stay evidence/candidates until an explicit disposition — import is
not approval, and classification is not authorship.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..classification.taxonomy import DocumentClassification
from ..core.errors import ValidationError

MANIFEST_FORMAT = "ddr-batch-manifest/v1"


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    well_id: str | None = None
    note: str = ""


@dataclass
class BatchEntryResult:
    filename: str
    sha256: str = ""
    status: str = "FAILED"  # IMPORTED | SKIPPED_DUPLICATE | FAILED
    document_id: str | None = None
    classification: str | None = None
    queued_for_review: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "status": self.status,
            "document_id": self.document_id,
            "classification": self.classification,
            "queued_for_review": self.queued_for_review,
            "error": self.error,
        }


@dataclass
class BatchImportSummary:
    manifest_format: str = MANIFEST_FORMAT
    total: int = 0
    imported: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    queued_for_review: int = 0
    entries: list[BatchEntryResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_format": self.manifest_format,
            "total": self.total,
            "imported": self.imported,
            "skipped_duplicate": self.skipped_duplicate,
            "failed": self.failed,
            "queued_for_review": self.queued_for_review,
            "entries": [e.to_dict() for e in self.entries],
        }


def load_manifest(path: Path | str) -> list[ManifestEntry]:
    """Load and validate a batch manifest JSON file."""
    manifest_path = Path(path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "batch manifest is unreadable or not valid JSON", path=str(manifest_path), error=str(exc)
        ) from exc
    if not isinstance(raw, dict) or raw.get("format") != MANIFEST_FORMAT:
        raise ValidationError(
            "unsupported batch manifest format", expected=MANIFEST_FORMAT, got=raw.get("format") if isinstance(raw, dict) else None
        )
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        raise ValidationError("batch manifest must list at least one file under 'files'")
    entries: list[ManifestEntry] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
            raise ValidationError("every manifest file entry must have a non-empty 'path'")
        entries.append(
            ManifestEntry(
                path=item["path"],
                well_id=item.get("well_id") if isinstance(item.get("well_id"), str) else None,
                note=item.get("note") if isinstance(item.get("note"), str) else "",
            )
        )
    return entries


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_ddr_batch(
    registry: Any,
    *,
    manifest_path: Path | str,
    base_dir: Path | str,
    workspace_root: Path | str,
    workspace_id: str | None,
    default_well_id: str | None = None,
) -> BatchImportSummary:
    """Import every manifest entry; never abort the batch on a single failure."""
    entries = load_manifest(manifest_path)
    base = Path(base_dir)
    summary = BatchImportSummary(total=len(entries))
    for entry in entries:
        result = BatchEntryResult(filename=Path(entry.path).name)
        summary.entries.append(result)
        try:
            source = (base / entry.path).resolve()
            if not source.is_file():
                result.error = "file missing"
            elif source.suffix.lower() != ".pdf":
                result.error = "only PDF files are accepted for DDR batch import"
            else:
                result.sha256 = _sha256_of(source)
                # Idempotency: same content already registered -> duplicate, no new rows.
                existing = registry.repository.version_by_sha(result.sha256)
                if existing is not None:
                    result.status = "SKIPPED_DUPLICATE"
                    result.document_id = existing.document_id
                    doc = registry.repository.get(existing.document_id)
                    result.classification = getattr(doc, "classification", None)
                    result.queued_for_review = result.classification == DocumentClassification.DDR.value
                    summary.skipped_duplicate += 1
                    if result.queued_for_review:
                        summary.queued_for_review += 1
                else:
                    registered = registry.register(
                        path=source,
                        workspace_root=workspace_root,
                        workspace_id=workspace_id,
                        well_id=entry.well_id or default_well_id,
                    )
                    if not registered.ok:
                        result.error = registered.error or "registry refused the file"
                    else:
                        result.status = "IMPORTED"
                        result.document_id = registered.document_id
                        result.classification = registered.classification
                        result.queued_for_review = (
                            registered.classification == DocumentClassification.DDR.value
                        )
                        summary.imported += 1
                        if result.queued_for_review:
                            summary.queued_for_review += 1
        except Exception as exc:  # noqa: BLE001 - batch must survive a single bad entry
            result.status = "FAILED"
            result.error = f"{type(exc).__name__}: {exc}"
        if result.status == "FAILED":
            summary.failed += 1
    return summary


def review_queue(registry: Any, *, workspace_id: str | None = None) -> list[dict[str, Any]]:
    """DDR-classified documents awaiting an explicit extraction disposition.

    Import never dispositions content; this queue is the operator's review surface.
    """
    documents = registry.repository.list_documents(
        workspace_id=workspace_id, classification=DocumentClassification.DDR.value
    )
    queue: list[dict[str, Any]] = []
    for doc in documents:
        # Dispositions are provenance events on document versions (never a silent
        # status flip); a DDR is queue-pending until one is recorded.
        has_disposition = False
        for version in registry.repository.versions_for(doc.id):
            provenance = getattr(version, "provenance", None) or []
            if any(
                isinstance(item, dict) and item.get("event") == "extraction_disposition"
                for item in provenance
            ):
                has_disposition = True
                break
        queue.append(
            {
                "document_id": doc.id,
                "filename": doc.filename,
                "classification": doc.classification,
                "disposition_recorded": has_disposition,
                "well_id": doc.well_id,
            }
        )
    return queue
