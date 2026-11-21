"""Phase C integrity tests: source-byte hash verification, version pinning, strict kinds."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.session import Session
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.engineering.authoring import EngineeringAuthoringService
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.engineering.source_to_draft import (
    create_draft_from_document,
    extract_candidates,
)
from drilling_intelligence.engineering.templates import create_from_template, normalize_kind

pytestmark = pytest.mark.integration


def _register_text_document(
    workspace, tmp_path: Path, name: str, content: bytes
) -> tuple[str, str, Path]:
    with workspace.database.unit_of_work() as session:
        return _register(session, tmp_path, name, content)


def _register(session: Session, tmp_path: Path, name: str, content: bytes) -> tuple[str, str, Path]:
    docs = DocumentRepository(session)
    sha = hashlib.sha256(content).hexdigest()
    doc = docs.create_document(
        workspace_id=None,
        unscoped=True,  # deliberate test row
        identity_path=f"fixtures/{name}",
        filename=name,
        extension=Path(name).suffix,
        mime_type="text/markdown" if name.endswith(".md") else "text/plain",
        size_bytes=len(content),
        sha256=sha,
        file_modified_at=datetime.now(UTC),
    )
    src = tmp_path / name
    src.write_bytes(content)
    version = docs.create_version(
        doc,
        sha256=sha,
        source_path=str(src),
        size_bytes=len(content),
        parser="test",
        parser_version="1",
        extraction_version="1",
        origin="NEW",
        mime_type="text/markdown",
        source_relative_path=str(src),
    )
    session.flush()
    return doc.id, version.id, src


def test_extraction_refuses_when_source_bytes_changed(workspace, tmp_path) -> None:
    content = b"# Title\n\noriginal body\n"
    with workspace.database.unit_of_work() as session:
        doc_id, ver_id, src = _register_text_document(workspace, tmp_path, "note.md", content)
    src.write_bytes(b"# Title\n\nTAMPERED body\n")  # same path, different bytes
    with workspace.database.unit_of_work() as session:
        service = EngineeringAuthoringService(session)
        with pytest.raises(ValidationError, match="do not match the registered document version"):
            extract_candidates(service, doc_id, ver_id, workspace_root=workspace.root)


def test_extraction_refuses_wrong_registered_version(workspace, tmp_path) -> None:
    doc_id, _ver_id, _src = _register_text_document(workspace, tmp_path, "a.md", b"# A\n\nbody a\n")
    _doc2, ver2, _ = _register_text_document(workspace, tmp_path, "b.md", b"# B\n\nbody b\n")
    with workspace.database.unit_of_work() as session:
        service = EngineeringAuthoringService(session)
        with pytest.raises(ValidationError, match="does not belong"):
            extract_candidates(service, doc_id, ver2, workspace_root=workspace.root)


def test_extraction_refuses_invalid_utf8(workspace, tmp_path) -> None:
    content = b"# T\n\nok\n"
    doc_id, ver_id, src = _register_text_document(workspace, tmp_path, "bad.md", content)
    bad = content + b"\xff\xfe invalid utf8 \x80\n"
    src.write_bytes(bad)
    # re-register the bad bytes honestly so the hash matches, then decode must fail strictly
    with workspace.database.unit_of_work() as session:
        docs = DocumentRepository(session)
        version = docs.version(ver_id)
        version.sha256 = hashlib.sha256(bad).hexdigest()
        service = EngineeringAuthoringService(session)
        with pytest.raises(ValidationError, match="not valid UTF-8"):
            extract_candidates(service, doc_id, ver_id, workspace_root=workspace.root)


def test_extraction_refuses_missing_source(workspace, tmp_path) -> None:
    doc_id, ver_id, src = _register_text_document(workspace, tmp_path, "gone.md", b"# G\n\nbody\n")
    src.unlink()
    with workspace.database.unit_of_work() as session:
        service = EngineeringAuthoringService(session)
        with pytest.raises(ValidationError, match="not resolvable"):
            extract_candidates(service, doc_id, ver_id, workspace_root=workspace.root)


def test_hash_and_locator_round_trip(workspace, tmp_path) -> None:
    content = b"# Sect one\n\nfirst body\n\n# Sect two\n\nsecond body\n"
    doc_id, ver_id, _src = _register_text_document(workspace, tmp_path, "rt.md", content)
    with workspace.database.unit_of_work() as session:
        service = EngineeringAuthoringService(session)
        result = extract_candidates(service, doc_id, ver_id, workspace_root=workspace.root)
        assert result["sha256"] == hashlib.sha256(content).hexdigest()
        for cand in result["candidates"]:
            loc = cand["locator"]
            assert loc["sha256"] == result["sha256"]
            assert loc["document_version_id"] == ver_id
            lines = content.decode().splitlines()
            assert 1 <= loc["line_start"] <= loc["line_end"] <= len(lines)


def test_version_pinning_single_version_for_parent_and_children(workspace, tmp_path) -> None:
    content = b"# One\n\nbody one\n\n# Two\n\nbody two\n"
    doc_id, ver_id, _src = _register_text_document(workspace, tmp_path, "pin.md", content)
    with workspace.database.unit_of_work() as session:
        service = EngineeringAuthoringService(session)
        record = create_draft_from_document(
            service,
            "program",
            doc_id,
            content={"title": "Pinned", "code": "PIN-1", "author": "a"},
            extract=True,
            workspace_root=workspace.root,
        )
        session.flush()
        repo = EngineeringRepository(session)
        parent = repo.get_program(record.id)
        assert parent.document_version_id == ver_id, "parent pins the resolved current version"
        sections = repo.list_program_sections(parent.id)
        assert sections
        for section in sections:
            for prov in section.provenance or []:
                if isinstance(prov, dict) and prov.get("extracted"):
                    assert prov["locator"]["document_version_id"] == ver_id
                    assert prov["locator"]["sha256"] == hashlib.sha256(content).hexdigest()


def test_strict_kind_validation_rejects_unknown() -> None:
    for bad in ("", "   ", "section", "Programs!", "program1", "procedures2"):
        with pytest.raises(ValidationError):
            normalize_kind(bad)
    with pytest.raises(ValidationError):
        normalize_kind(None)
    assert normalize_kind(" PROGRAM ") == "program"
    assert normalize_kind("Procedures") == "procedure"


def test_create_draft_rejects_unknown_kind(workspace, tmp_path) -> None:
    doc_id, _ver, _src = _register_text_document(workspace, tmp_path, "k.md", b"# K\n\nx\n")
    with workspace.database.unit_of_work() as session:
        service = EngineeringAuthoringService(session)
        with pytest.raises(ValidationError, match="kind"):
            create_draft_from_document(
                service, "banana", doc_id, content={"title": "t", "author": "a"}
            )


def test_create_from_template_rejects_unknown_kind(workspace) -> None:
    with workspace.database.unit_of_work() as session:
        service = EngineeringAuthoringService(session)
        with pytest.raises(ValidationError, match="kind"):
            create_from_template(
                service,
                "widget",
                "standard_drilling_program",
                content={"title": "t", "author": "a"},
            )
