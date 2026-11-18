"""Source-document-to-draft workflow.

Creates a DRAFT Program or Procedure whose structure is anchored to a registered source
document (``document_id`` / ``document_version_id`` recorded on the record itself). The
draft's sections/steps are marked ``EXTRACTED_UNVERIFIED`` with provenance pointing at the
exact document version; no extracted text is copied in as engineering fact, and nothing is
inferred - an author must promote each item to ``SOURCE_PROVIDED`` (with the exact source
reference) or leave it for owner decision.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ValidationError
from ..documents.repository import DocumentRepository
from .templates import PROCEDURE_TEMPLATES, PROGRAM_TEMPLATES, STEP_PLACEHOLDER


def create_draft_from_document(
    service: Any,
    kind: str,
    document_id: str,
    *,
    document_version_id: str | None = None,
    template: str | None = None,
    content: dict[str, Any],
) -> Any:
    kind = str(kind).strip().lower()
    singular = "program" if kind in {"program", "programs"} else "procedure"
    if singular not in {"program", "procedure"}:
        raise ValidationError("kind must be 'program' or 'procedure'")

    documents = DocumentRepository(service.session)
    document = documents.get(document_id)
    if document is None:
        raise ValidationError("source document not found", document_id=document_id)
    if document_version_id is not None:
        version = documents.version(document_version_id)
        if version is None or version.document_id != document_id:
            raise ValidationError(
                "document version does not belong to the given document",
                document_id=document_id,
                document_version_id=document_version_id,
            )

    template = template or (
        "standard_drilling_program" if singular == "program" else "standard_operational_procedure"
    )
    catalog = PROGRAM_TEMPLATES if singular == "program" else PROCEDURE_TEMPLATES
    if template not in catalog:
        raise ValidationError(
            f"unknown {singular} template", available=sorted(catalog), got=template
        )
    spec = catalog[template]

    author_key = "author" if singular == "program" else "created_by"
    title = content.get("title") or getattr(document, "title", None)
    author = content.get(author_key)
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("draft-from-document requires a non-empty title")
    if not isinstance(author, str) or not author.strip():
        raise ValidationError(f"draft-from-document requires non-empty {author_key!r} attribution")

    provenance = [
        {
            "source": "document",
            "document_id": document_id,
            "document_version_id": document_version_id,
            "template": template,
            "kind": "structure_only_extracted_unverified",
        }
    ]
    common_scope = {
        "well_id": content.get("well_id"),
        "field_id": content.get("field_id"),
        "project_id": content.get("project_id"),
    }
    if singular == "program":
        record = service.repository.create_program(
            title=title.strip(),
            code=str(content.get("code") or ""),
            summary=str(content.get("summary") or ""),
            author=author,
            created_by=author,
            origin="EXTRACTED",
            provenance=provenance,
            **common_scope,
        )
        record.document_id = document_id
        record.document_version_id = document_version_id
        service._touch(record)
        token = service._token(record)
        for sequence, (section_key, section_title) in enumerate(spec["sections"], start=1):
            service.add_program_section(
                record.id,
                expected_updated_at=token,
                sequence=sequence,
                section_key=section_key,
                title=section_title,
                content=f"Anchored to source document {document_id}"
                + (f" version {document_version_id}" if document_version_id else "")
                + "; content unverified.",
                completion_state="EXTRACTED_UNVERIFIED",
                source_reference=f"document:{document_id}"
                + (f"#{document_version_id}" if document_version_id else ""),
                origin="EXTRACTED",
                provenance=[*provenance, {"section_key": section_key}],
            )
            token = service._token(record)
    else:
        record = service.repository.create_procedure(
            title=title.strip(),
            code=str(content.get("code") or ""),
            procedure_type=str(content.get("procedure_type") or "general"),
            description=str(content.get("description") or ""),
            created_by=author,
            origin="EXTRACTED",
            provenance=provenance,
            **common_scope,
        )
        record.document_id = document_id
        record.document_version_id = document_version_id
        record.source_reference = str(content.get("source_reference") or f"document:{document_id}")
        service._touch(record)
        token = service._token(record)
        for sequence, step_spec in enumerate(spec["steps"], start=1):
            service.add_procedure_step(
                record.id,
                expected_updated_at=token,
                sequence=sequence,
                title=step_spec["title"],
                instructions=STEP_PLACEHOLDER + f" (source document {document_id})",
                hold_point=bool(step_spec.get("hold_point", False)),
                source_reference=f"document:{document_id}"
                + (f"#{document_version_id}" if document_version_id else ""),
                provenance=[*provenance, {"step": step_spec["title"]}],
            )
            token = service._token(record)
    service.session.flush()
    return record
