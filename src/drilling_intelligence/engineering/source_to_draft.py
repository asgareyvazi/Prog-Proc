"""Source-document-to-draft workflow.

Creates a DRAFT Program or Procedure whose structure is anchored to a registered source
document (``document_id`` / ``document_version_id`` recorded on the record itself). The
draft's sections/steps are marked ``EXTRACTED_UNVERIFIED`` with provenance pointing at the
exact document version; no extracted text is copied in as engineering fact, and nothing is
inferred - an author must promote each item to ``SOURCE_PROVIDED`` (with the exact source
reference) or leave it for owner decision.
"""

from __future__ import annotations

import hashlib
from pathlib import Path as _Path
from typing import Any

from ..core.errors import ValidationError
from ..database.models import ProcedureStep, ProgramSection
from ..documents.repository import DocumentRepository
from .templates import (
    PROCEDURE_TEMPLATES,
    PROGRAM_TEMPLATES,
    STEP_PLACEHOLDER,
    normalize_kind,
)

#: Explicit disposition states for extracted candidates (Phase D). The disposition is
#: discoverable from provenance events, never inferred from a source-reference string.
DISPOSITION_PENDING = "PENDING_REVIEW"
DISPOSITION_ACCEPTED = "ACCEPTED"
DISPOSITION_EDITED = "EDITED"
DISPOSITION_REJECTED = "REJECTED"
DISPOSITION_SUPERSEDED = "SUPERSEDED"
DISPOSITIONS = (
    DISPOSITION_PENDING,
    DISPOSITION_ACCEPTED,
    DISPOSITION_EDITED,
    DISPOSITION_REJECTED,
    DISPOSITION_SUPERSEDED,
)


#: Legacy decision spellings persisted by earlier revisions, mapped in exactly one place.
#: New writes always use the canonical uppercase DISPOSITION_* constants.
_LEGACY_DECISION_MAP = {
    "accept": DISPOSITION_ACCEPTED,
    "accepted": DISPOSITION_ACCEPTED,
    "edit": DISPOSITION_EDITED,
    "edited": DISPOSITION_EDITED,
    "reject": DISPOSITION_REJECTED,
    "rejected": DISPOSITION_REJECTED,
    "supersede": DISPOSITION_SUPERSEDED,
    "superseded": DISPOSITION_SUPERSEDED,
}

#: Repeat-decision policy: any decision may follow PENDING_REVIEW/ACCEPTED/EDITED/REJECTED
#: (each new decision is appended, never overwrites), but SUPERSEDED is terminal.
_ALLOWED_FROM = {
    DISPOSITION_PENDING: DISPOSITIONS,
    DISPOSITION_ACCEPTED: (DISPOSITION_EDITED, DISPOSITION_REJECTED),
    DISPOSITION_EDITED: (DISPOSITION_ACCEPTED, DISPOSITION_REJECTED),
    DISPOSITION_REJECTED: (DISPOSITION_ACCEPTED, DISPOSITION_EDITED),
    DISPOSITION_SUPERSEDED: (),
}


def extraction_disposition(provenance: Any) -> str:
    """Return the latest explicit disposition recorded on an item, or PENDING_REVIEW."""
    if not isinstance(provenance, list):
        return DISPOSITION_PENDING
    decision = None
    for entry in provenance:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("decision")
        if entry.get("event") == "extraction_disposition" and raw in DISPOSITIONS:
            decision = raw
        elif raw in _LEGACY_DECISION_MAP:
            decision = _LEGACY_DECISION_MAP[raw]
    if decision is None:
        return DISPOSITION_PENDING
    return decision


def disposition_event(
    decision: str,
    *,
    by: str,
    candidate_id: str,
    document_id: str,
    version_id: str,
    sha256: str,
    locator: dict[str, Any],
    edited: bool = False,
) -> dict[str, Any]:
    """One canonical extraction-disposition event (shared by Program and Procedure)."""
    from datetime import UTC, datetime

    if decision not in DISPOSITIONS or decision == DISPOSITION_PENDING:
        raise ValidationError("not a recordable disposition", decision=decision)
    event: dict[str, Any] = {
        "event": "extraction_disposition",
        "decision": decision,
        "by": by,
        "at": datetime.now(UTC).isoformat(),
        "candidate_id": candidate_id,
        "document_id": document_id,
        "version_id": version_id,
        "sha256": sha256,
        "locator": locator,
    }
    if edited:
        event["edit"] = {"author_text_applied": True}
    return event


def check_disposition_transition(current: str, decision: str) -> None:
    """Enforce the repeat-decision policy; refuse forbidden transitions."""
    if decision not in DISPOSITIONS or decision == DISPOSITION_PENDING:
        raise ValidationError("not a recordable disposition", decision=decision)
    allowed = _ALLOWED_FROM.get(current, DISPOSITIONS)
    if decision not in allowed:
        raise ValidationError(
            "disposition transition is forbidden; the full event history is preserved",
            current=current,
            decision=decision,
        )


#: Only plain-text sources are really extracted here. Binary/OCR pipelines (MinerU etc.)
#: remain NOT QUALIFIED, so a binary source is refused rather than pretending to parse it.
_TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
_MAX_EXCERPT = 400


def _text_blocks(text: str) -> list[dict[str, Any]]:
    """Split plain text into blocks with 1-based line locators.

    A block starts at a markdown heading (``#``..``######``) or after a blank line; the
    locator always names the exact source lines so every candidate is checkable.
    """
    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    start = 0
    for index, line in enumerate(lines):
        is_heading = line.lstrip().startswith("#")
        is_blank = not line.strip()
        if (is_heading or is_blank) and index > start:
            block = lines[start:index]
            if any(item.strip() for item in block):
                blocks.append({"line_start": start + 1, "line_end": index, "lines": block})
            start = index + 1 if is_blank else index
    if start < len(lines):
        block = lines[start:]
        if any(item.strip() for item in block):
            blocks.append({"line_start": start + 1, "line_end": len(lines), "lines": block})
    return blocks


def _resolve_version(
    documents: DocumentRepository, document: Any, document_version_id: str | None
) -> Any:
    """One authoritative version resolution: explicit ID (ownership-checked) or the
    canonical current-version pointer. Never scans is_current rows as a substitute for
    the pointer, never substitutes another version silently."""
    if document_version_id is not None:
        version = documents.version(document_version_id)
        if version is None or version.document_id != document.id:
            raise ValidationError(
                "document version does not belong to the given document",
                document_id=document.id,
                document_version_id=document_version_id,
            )
        return version
    version = documents.current_version(document)
    if version is None:
        raise ValidationError(
            "document has no current version (pointer is null or inconsistent)",
            document_id=document.id,
        )
    return version


def extract_candidates(
    service: Any,
    document_id: str,
    document_version_id: str | None = None,
    *,
    workspace_root: _Path | str | None = None,
    max_items: int = 40,
) -> dict[str, Any]:
    """Really read a registered plain-text source and return located extraction candidates.

    Every candidate carries a locator (workspace-relative path, version id, sha256, line
    range) and is honestly marked ``EXTRACTED_UNVERIFIED`` - nothing here is engineering
    fact until an author accepts or edits it.
    """
    documents = DocumentRepository(service.session)
    document = documents.get(document_id)
    if document is None:
        raise ValidationError("source document not found", document_id=document_id)
    version = _resolve_version(documents, document, document_version_id)
    path = documents.resolve_source_path(version, document, workspace_root=workspace_root)
    if path is None or not path.is_file():
        raise ValidationError(
            "source file is not resolvable in this workspace", document_id=document_id
        )
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        raise ValidationError(
            "real extraction supports plain-text sources only; binary/OCR extraction is NOT QUALIFIED",
            suffix=path.suffix,
        )
    # Provenance integrity: verify the PHYSICAL BYTES against the registered version hash
    # before attributing any extracted text to that version. A replaced or modified file
    # must be refused, not silently attributed to the old registered SHA.
    raw_bytes = path.read_bytes()
    actual_sha = hashlib.sha256(raw_bytes).hexdigest()
    if actual_sha != version.sha256:
        raise ValidationError(
            "source bytes do not match the registered document version; "
            "register a new document version through the registry workflow instead of "
            "extracting from stale-version bytes",
            document_id=document_id,
            document_version_id=version.id,
            registered_sha256=version.sha256,
            actual_sha256=actual_sha,
        )
    try:
        raw = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "source is not valid UTF-8; refusing to treat replaced bytes as faithful source content",
            document_id=document_id,
            document_version_id=version.id,
        ) from exc
    rel = path.name if workspace_root is None else str(path)
    candidates: list[dict[str, Any]] = []
    for block in _text_blocks(raw):
        excerpt = "\n".join(block["lines"]).strip()
        if not excerpt:
            continue
        locator = {
            "path": rel,
            "document_id": document_id,
            "document_version_id": version.id,
            "sha256": actual_sha,  # verified against the physical bytes above
            "line_start": block["line_start"],
            "line_end": block["line_end"],
        }
        candidate_id = hashlib.sha256(
            f"{document_id}:{version.id}:{block['line_start']}-{block['line_end']}".encode()
        ).hexdigest()[:16]
        heading = next(
            (
                ln.strip().lstrip("#").strip()
                for ln in block["lines"]
                if ln.lstrip().startswith("#")
            ),
            None,
        )
        candidates.append(
            {
                "candidate_id": candidate_id,
                "locator": locator,
                "excerpt": excerpt[:_MAX_EXCERPT],
                "suggested_title": heading or excerpt.splitlines()[0][:80],
                "state": "EXTRACTED_UNVERIFIED",
            }
        )
        if len(candidates) >= max_items:
            break
    return {
        "document_id": document_id,
        "document_version_id": version.id,
        "sha256": actual_sha,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def document_id_of(locator: dict[str, Any]) -> str:
    return str(locator.get("document_id", ""))


def _locator_ref(locator: dict[str, Any]) -> str:
    return (
        f"document:{locator['document_id']}#{locator['document_version_id']}"
        f":{locator['sha256'][:12]}:L{locator['line_start']}-L{locator['line_end']}"
    )


def decide_extracted_item(
    service: Any,
    kind: str,
    record_id: str,
    item_id: str,
    *,
    decision: str,
    expected_updated_at: str,
    by: str,
    edited_text: str | None = None,
) -> Any:
    """Accept / edit / reject one extracted section or step. Honest states only.

    - ``accept`` promotes the item to SOURCE_PROVIDED with the exact locator as source.
    - ``edit`` replaces the text with author text and keeps the original locator provenance.
    - ``reject`` leaves the item EXTRACTED_UNVERIFIED and records the rejection; nothing is
      silently dropped or silently promoted.
    """
    singular = normalize_kind(kind)
    decision = str(decision).strip().lower()
    if decision not in {"accept", "edit", "reject"}:
        raise ValidationError("decision must be accept, edit, or reject")
    if not str(by or "").strip():
        raise ValidationError("extraction decisions require a declared actor attribution")
    if decision == "edit" and (edited_text is None or not str(edited_text).strip()):
        raise ValidationError("edit decision requires non-empty replacement text")
    if singular == "program":
        record = service.repository.get_program(record_id)
        section = service.session.get(ProgramSection, str(item_id))
        if section is None or section.program_id != record.id:
            raise ValidationError("section does not belong to this Program revision")
        provenance = list(section.provenance or [])
        extracted = next(
            (p for p in provenance if isinstance(p, dict) and p.get("extracted")), None
        )
        if extracted is None:
            raise ValidationError("item is not an extracted candidate")
        locator = extracted["locator"]
        canonical = {
            "accept": DISPOSITION_ACCEPTED,
            "edit": DISPOSITION_EDITED,
            "reject": DISPOSITION_REJECTED,
        }[decision]
        check_disposition_transition(extraction_disposition(provenance), canonical)
        changes: dict[str, Any] = {}
        if decision in {"accept", "edit"}:
            changes["completion_state"] = "SOURCE_PROVIDED"
            changes["source_reference"] = _locator_ref(locator)
            if decision == "edit":
                changes["content"] = str(edited_text)
        else:
            # A rejected item must never remain SOURCE_PROVIDED; the original locator and
            # every prior event stay in provenance (evidence is never erased).
            changes["completion_state"] = "EXTRACTED_UNVERIFIED"
        provenance = [
            *provenance,
            disposition_event(
                canonical,
                by=by,
                candidate_id=str(item_id),
                document_id=str(locator.get("document_id", document_id_of(locator))),
                version_id=str(locator.get("version_id", "")),
                sha256=str(locator.get("sha256", "")),
                locator=locator,
                edited=(decision == "edit"),
            ),
        ]
        changes["provenance"] = provenance
        return service.update_program_section(
            record.id, item_id, expected_updated_at=expected_updated_at, changes=changes
        )
    record = service.repository.get_procedure(record_id)
    step = service.session.get(ProcedureStep, str(item_id))
    if step is None or step.procedure_id != record.id:
        raise ValidationError("step does not belong to this Procedure revision")
    provenance = list(step.provenance or [])
    extracted = next((p for p in provenance if isinstance(p, dict) and p.get("extracted")), None)
    if extracted is None:
        raise ValidationError("item is not an extracted candidate")
    locator = extracted["locator"]
    canonical = {
        "accept": DISPOSITION_ACCEPTED,
        "edit": DISPOSITION_EDITED,
        "reject": DISPOSITION_REJECTED,
    }[decision]
    check_disposition_transition(extraction_disposition(provenance), canonical)
    changes = {}
    if decision in {"accept", "edit"}:
        changes["source_reference"] = _locator_ref(locator)
        if decision == "edit":
            changes["instructions"] = str(edited_text)
    provenance = [
        *provenance,
        disposition_event(
            canonical,
            by=by,
            candidate_id=str(item_id),
            document_id=str(document_id_of(locator)),
            version_id=str(locator.get("version_id", "")),
            sha256=str(locator.get("sha256", "")),
            locator=locator,
            edited=(decision == "edit"),
        ),
    ]
    changes["provenance"] = provenance
    return service.update_procedure_step(
        record.id, item_id, expected_updated_at=expected_updated_at, changes=changes
    )


def read_source_excerpt(
    service: Any,
    document_id: str,
    locator: dict[str, Any],
    *,
    workspace_root: _Path | str | None = None,
) -> dict[str, Any]:
    """Genuinely re-read the registered source excerpt behind a candidate locator.

    Resolves the exact document and version through the canonical repository boundary,
    re-reads the PHYSICAL bytes, verifies their SHA-256 against the registered version
    hash AND the locator hash, then returns the exact line range. Returns an explicit
    blocked state (ok=False with reason) when the file is missing or the bytes no longer
    match - the source pane must never be fabricated from the candidate's own text.
    """
    import hashlib

    documents = DocumentRepository(service.session)
    document = documents.get(document_id)
    if document is None:
        return {"ok": False, "reason": "source document not found", "excerpt": ""}
    version_id = locator.get("document_version_id") or locator.get("version_id")
    version = documents.version(str(version_id)) if version_id else None
    if version is None or version.document_id != document.id:
        return {"ok": False, "reason": "registered source version not found", "excerpt": ""}
    path = documents.resolve_source_path(version, document, workspace_root=workspace_root)
    if path is None or not path.is_file():
        return {
            "ok": False,
            "reason": "source file missing in this workspace",
            "excerpt": "",
            "path": str(path),
        }
    raw = path.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != version.sha256:
        return {
            "ok": False,
            "reason": "source bytes no longer match the registered version (tampered or replaced)",
            "excerpt": "",
            "actual_sha256": actual_sha,
            "registered_sha256": version.sha256,
        }
    locator_sha = locator.get("sha256")
    if locator_sha and str(locator_sha) != actual_sha:
        return {
            "ok": False,
            "reason": "locator hash does not match the verified source bytes",
            "excerpt": "",
            "actual_sha256": actual_sha,
        }
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "reason": "source is not valid UTF-8 text", "excerpt": ""}
    lines = text.splitlines()
    start = int(locator.get("line_start", 1))
    end = int(locator.get("line_end", len(lines)))
    start = max(1, min(start, len(lines) + 1))
    end = max(start - 1, min(end, len(lines)))
    excerpt = "\n".join(lines[start - 1 : end])
    return {
        "ok": True,
        "excerpt": excerpt,
        "sha256": actual_sha,
        "version_id": version.id,
        "document_id": document.id,
        "path": str(path),
        "line_start": start,
        "line_end": end,
        "reason": "",
    }


def create_draft_from_document(
    service: Any,
    kind: str,
    document_id: str,
    *,
    document_version_id: str | None = None,
    template: str | None = None,
    content: dict[str, Any],
    extract: bool = False,
    workspace_root: _Path | str | None = None,
) -> Any:
    singular = normalize_kind(kind)

    documents = DocumentRepository(service.session)
    document = documents.get(document_id)
    if document is None:
        raise ValidationError("source document not found", document_id=document_id)
    # Pin the effective source version exactly once through the canonical resolver and use
    # that exact ID for the parent record, provenance and all candidate locators, so no
    # mixed-version draft can be created and no second current-version lookup happens.
    version = _resolve_version(documents, document, document_version_id)
    document_version_id = version.id
    extracted: list[dict[str, Any]] | None = None
    if extract:
        extracted = extract_candidates(
            service, document_id, document_version_id, workspace_root=workspace_root
        )["candidates"]
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
        if extracted is not None:
            for sequence, cand in enumerate(extracted, start=1):
                service.add_program_section(
                    record.id,
                    expected_updated_at=token,
                    sequence=sequence,
                    section_key=f"extracted_{cand['candidate_id']}",
                    title=str(cand["suggested_title"])[:120],
                    content=cand["excerpt"],
                    completion_state="EXTRACTED_UNVERIFIED",
                    source_reference=_locator_ref(cand["locator"]),
                    origin="EXTRACTED",
                    provenance=[
                        *provenance,
                        {
                            "extracted": True,
                            "candidate_id": cand["candidate_id"],
                            "locator": cand["locator"],
                        },
                    ],
                )
                token = service._token(record)
        else:
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
        if extracted is not None:
            for sequence, cand in enumerate(extracted, start=1):
                service.add_procedure_step(
                    record.id,
                    expected_updated_at=token,
                    sequence=sequence,
                    title=str(cand["suggested_title"])[:120],
                    instructions=cand["excerpt"],
                    hold_point=False,
                    source_reference=_locator_ref(cand["locator"]),
                    provenance=[
                        *provenance,
                        {
                            "extracted": True,
                            "candidate_id": cand["candidate_id"],
                            "locator": cand["locator"],
                        },
                    ],
                )
                token = service._token(record)
        else:
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
