"""Helpers for knowledge tests: a registry row and an artefact, without re-running a parser.

The knowledge layer deliberately reads what the extraction stage *stored* rather than opening files
again, so the honest test fixture is a stored artefact: a document, a version, and an
``extraction.document_json`` in exactly the shape the pipeline writes.  Building it here keeps the
four knowledge test files about behaviour instead of about ten lines of row construction each.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from drilling_intelligence.core.enums import DocumentClassification, FileChangeKind
from drilling_intelligence.database.models import Document, DocumentVersion
from drilling_intelligence.documents.repository import DocumentRepository

#: The provenance shape the extractors emit for an Excel cell, which is what a fact must keep.
LOCATOR = {
    "locator_kind": "excel",
    "sheet": "Summary",
    "cell": "B9",
    "range_": "A1:D16",
    "read": "",
    "row": 9,
    "column": 2,
}


def artefact_field(
    name: str,
    value: Any,
    unit: str = "",
    *,
    quality: str = "VALID",
    confidence: float = 0.9,
    note: str = "",
    sheet: str = "Summary",
    cell: str = "B9",
    excerpt: str = "",
    document_id: str = "doc-1",
    version_id: str = "ver-1",
    filename: str = "mud.xlsx",
) -> dict[str, Any]:
    """One ``extracted_fields`` entry, exactly as the artefact stores it."""
    locator = {**LOCATOR, "sheet": sheet, "cell": cell, "read": str(value)}
    return {
        "name": name,
        "value": value,
        "unit": unit,
        "dimension": "",
        "quality": quality,
        "confidence": confidence,
        "method": "fixture",
        "note": note,
        "provenance": {
            "document_id": document_id,
            "document_version_id": version_id,
            "filename": filename,
            "parser": "excel/test",
            "excerpt": excerpt or f"{name} | {value}",
            "source_sha256": "c" * 64,
            "confidence": confidence,
            "locator": locator,
        },
    }


def artefact(*fields: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """A stored-artefact payload: the fields, plus the minimum else the shape has."""
    return {
        "text": "",
        "pages": [],
        "tables": [],
        "figures": [],
        "paragraphs": [],
        "sections": [],
        "diagnostics": [],
        "metadata": dict(metadata or {}),
        "extracted_fields": list(fields),
    }


def register_artefact(
    session: Any,
    *,
    filename: str = "mud.xlsx",
    identity_path: str = "docs/mud.xlsx",
    classification: DocumentClassification | str = DocumentClassification.MUD_REPORT,
    well_id: str | None = None,
    project_id: str | None = None,
    document_date: dt.datetime | None = None,
    revision: str = "Rev 1",
    source_authority: str = "current_operational_report",
    fields: tuple[dict[str, Any], ...] = (),
    sha: str | None = None,
    version_number: int = 1,
    supersedes: DocumentVersion | None = None,
    title: str | None = None,
) -> tuple[Document, DocumentVersion, dict[str, Any]]:
    """Write a document (or its next version) and the artefact the knowledge layer reads.

    A second call with the same ``identity_path`` and ``filename`` and a ``supersedes`` version
    produces revision 2 of the same document, which is how the version-awareness tests get a real
    supersede chain instead of a hand-made one.
    """
    documents = DocumentRepository(session)
    digest = sha or f"{version_number}" * 64
    document = documents.by_identity(None, identity_path)
    if document is None:
        document = documents.create_document(
            workspace_id=None,
            identity_path=identity_path,
            filename=filename,
            extension=".xlsx" if filename.endswith(".xlsx") else ".txt",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=2048,
            sha256=digest,
            well_id=well_id,
            project_id=project_id,
            classification=classification,
            status="INDEXED",
            source_authority=source_authority,
            revision=revision,
            revision_key=version_number,
            document_date=document_date,
        )
        document.title = title or filename
    else:
        documents.update_document_metadata(
            document, {"revision": revision, "revision_key": version_number}
        )
    version = documents.create_version(
        document,
        sha256=digest,
        source_path=f"sources/{filename}",
        source_relative_path=identity_path,
        size_bytes=2048,
        parser="excel",
        parser_version="test",
        extraction_version="test",
        origin=FileChangeKind.NEW if version_number == 1 else FileChangeKind.MODIFIED,
        revision=revision,
        revision_key=version_number,
        supersedes_version_id=supersedes.id if supersedes is not None else None,
    )
    payload = artefact(*fields)
    documents.save_extraction(
        document=document,
        version=version,
        extractor="excel",
        extractor_version="test",
        content_sha256=digest,
        config_hash="cfg",
        document_json=payload,
        text="",
        stats={"fields": len(fields)},
        router_decision={},
    )
    session.flush()
    # ``save_extraction`` fills the ids in the artefact with placeholders; a real pipeline writes
    # the row ids it just created, so the fixture does the same afterwards.
    for entry in payload["extracted_fields"]:
        entry["provenance"]["document_id"] = document.id
        entry["provenance"]["document_version_id"] = version.id
    extraction = documents.extraction_for_version(version.id)
    if extraction is not None:
        extraction.document_json = payload
        session.flush()
    return document, version, payload
