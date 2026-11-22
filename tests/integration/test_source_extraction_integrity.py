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


def _make_doc(workspace, tmp_path, name="v.md", content=b"# H\n\nbody text\n"):
    import hashlib
    from datetime import UTC, datetime

    docs = DocumentRepository(workspace.database.unit_of_work().__enter__()) if False else None
    with workspace.database.unit_of_work() as session:
        docs = DocumentRepository(session)
        doc = docs.create_document(
            workspace_id=None,
            unscoped=True,
            identity_path=f"fixtures/{name}",
            filename=name,
            extension=".md",
            mime_type="text/markdown",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            file_modified_at=datetime.now(UTC),
        )
        src = tmp_path / name
        src.write_bytes(content)
        ver = docs.create_version(
            doc,
            sha256=hashlib.sha256(content).hexdigest(),
            source_path=str(src),
            size_bytes=len(content),
            parser="t",
            parser_version="1",
            extraction_version="1",
            origin="NEW",
            mime_type="text/markdown",
            source_relative_path=str(src),
        )
        return doc.id, ver.id


def test_resolver_refuses_document_without_any_version(workspace, tmp_path) -> None:
    import hashlib
    from datetime import UTC, datetime

    with workspace.database.unit_of_work() as session:
        docs = DocumentRepository(session)
        doc = docs.create_document(
            workspace_id=None,
            unscoped=True,
            identity_path="fixtures/empty.md",
            filename="empty.md",
            extension=".md",
            mime_type="text/markdown",
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            file_modified_at=datetime.now(UTC),
        )
        session.flush()
        from drilling_intelligence.engineering.source_to_draft import extract_candidates

        with pytest.raises(ValidationError, match="no current version"):
            extract_candidates(_svc(session), doc.id, workspace_root=tmp_path)


def test_resolver_pointer_wins_over_is_current_disagreement(workspace, tmp_path) -> None:
    """Pointer/is_current disagreement: the canonical pointer is authoritative."""
    doc_id, ver_id = _make_doc(workspace, tmp_path)
    with workspace.database.unit_of_work() as session:
        docs = DocumentRepository(session)
        doc = docs.get(doc_id)
        for v in docs.versions_for(doc_id):
            v.is_current = False  # row flag cleared, pointer intact
        session.flush()
        from drilling_intelligence.engineering.source_to_draft import _resolve_version

        resolved = _resolve_version(docs, doc, None)
        assert resolved.id == ver_id  # pointer wins, no silent substitution


def test_resolver_rejects_version_of_other_document(workspace, tmp_path) -> None:
    doc_a, _ = _make_doc(workspace, tmp_path, "a.md", b"# A\n\na\n")
    _doc_b, ver_b = _make_doc(workspace, tmp_path, "b.md", b"# B\n\nb\n")
    with workspace.database.unit_of_work() as session:
        from drilling_intelligence.engineering.source_to_draft import extract_candidates

        with pytest.raises(ValidationError, match="does not belong"):
            extract_candidates(_svc(session), doc_a, ver_b, workspace_root=tmp_path)


def test_resolver_rejects_missing_version_id(workspace, tmp_path) -> None:
    doc_id, _ = _make_doc(workspace, tmp_path)
    with workspace.database.unit_of_work() as session:
        from drilling_intelligence.engineering.source_to_draft import extract_candidates

        with pytest.raises(ValidationError, match="does not belong"):
            extract_candidates(_svc(session), doc_id, "no-such-version", workspace_root=tmp_path)


def _svc(session):
    from drilling_intelligence.engineering.authoring import EngineeringAuthoringService

    return EngineeringAuthoringService(session)


def test_procedure_disposition_uses_canonical_events(workspace, tmp_path) -> None:
    """Procedure accept/edit/reject must persist canonical dispositions like Program."""
    from drilling_intelligence.engineering.source_to_draft import (
        DISPOSITION_ACCEPTED,
        DISPOSITION_EDITED,
        DISPOSITION_PENDING,
        DISPOSITION_REJECTED,
        create_draft_from_document,
        decide_extracted_item,
        extraction_disposition,
    )

    doc_id, ver_id = _make_doc(
        workspace, tmp_path, "p.md", b"# S1\n\nalpha\n\n# S2\n\nbeta\n\n# S3\n\ngamma\n"
    )
    with workspace.database.unit_of_work() as session:
        service = _svc(session)
        record = create_draft_from_document(
            service,
            "procedure",
            doc_id,
            content={"title": "P", "code": "PRC-DISP", "author": "a", "created_by": "a"},
            extract=True,
            workspace_root=tmp_path,
        )
        session.flush()
        repo = EngineeringRepository(session)
        steps = repo.list_procedure_steps(record.id)
        assert len(steps) == 6
        assert all(extraction_disposition(s.provenance) == DISPOSITION_PENDING for s in steps)

        def decide(idx, decision, text=None):
            row = repo.get_procedure(record.id)
            decide_extracted_item(
                service,
                "procedure",
                record.id,
                steps[idx].id,
                decision=decision,
                expected_updated_at=service._token(row),
                by="reviewer",
                edited_text=text,
            )
            session.flush()
            return repo.list_procedure_steps(record.id)

        steps = decide(0, "accept")
        steps = decide(1, "edit", "replacement instructions")
        steps = decide(2, "reject")
        assert extraction_disposition(steps[0].provenance) == DISPOSITION_ACCEPTED
        assert steps[0].completion_state == "SOURCE_PROVIDED"  # D-01: persisted state
        assert extraction_disposition(steps[1].provenance) == DISPOSITION_EDITED
        assert steps[1].completion_state == "SOURCE_PROVIDED"
        assert steps[1].instructions == "replacement instructions"
        assert extraction_disposition(steps[2].provenance) == DISPOSITION_REJECTED
        assert steps[2].completion_state == "EXTRACTED_UNVERIFIED"
        # D-02: top-level event version id equals the exact registered version id
        ev = [
            q
            for q in steps[0].provenance
            if isinstance(q, dict) and q.get("event") == "extraction_disposition"
        ][-1]
        assert ev["version_id"] == ver_id
        assert ev["document_id"] == doc_id
        assert len(ev["sha256"]) == 64
        # canonical event schema present on all three
        for st in steps[:3]:
            ev = [
                p
                for p in st.provenance
                if isinstance(p, dict) and p.get("event") == "extraction_disposition"
            ]
            assert ev, "canonical disposition event missing"
            assert ev[-1]["decision"] in {"ACCEPTED", "EDITED", "REJECTED"}
            assert ev[-1]["by"] == "reviewer" and ev[-1]["at"] and ev[-1]["sha256"]


def test_repeat_decision_policy(workspace, tmp_path) -> None:
    """Allowed re-decisions update state; SUPERSEDED is terminal; history preserved."""
    import hashlib

    from drilling_intelligence.engineering.source_to_draft import (
        DISPOSITION_ACCEPTED,
        DISPOSITION_EDITED,
        DISPOSITION_REJECTED,
        create_draft_from_document,
        decide_extracted_item,
        extraction_disposition,
    )

    raw = b"# S1\n\none\n\n# S2\n\ntwo\n"
    doc_id, ver_id = _make_doc(workspace, tmp_path, "r.md", raw)
    with workspace.database.unit_of_work() as session:
        service = _svc(session)
        record = create_draft_from_document(
            service,
            "program",
            doc_id,
            content={"title": "R", "code": "PR-DISP", "author": "a"},
            extract=True,
            workspace_root=tmp_path,
        )
        session.flush()
        repo = EngineeringRepository(session)
        sections = repo.list_program_sections(record.id)
        target = sections[0]

        def decide(dec, text=None):
            row = repo.get_program(record.id)
            decide_extracted_item(
                service,
                "program",
                record.id,
                target.id,
                decision=dec,
                expected_updated_at=service._token(row),
                by="rev",
                edited_text=text,
            )
            session.flush()
            return repo.list_program_sections(record.id)[0]

        target = decide("accept")
        assert extraction_disposition(target.provenance) == DISPOSITION_ACCEPTED
        assert target.completion_state == "SOURCE_PROVIDED"
        # accepted -> rejected: allowed, and completion state must not stay SOURCE_PROVIDED
        target = decide("reject")
        assert extraction_disposition(target.provenance) == DISPOSITION_REJECTED
        assert target.completion_state == "EXTRACTED_UNVERIFIED"
        # rejected -> accepted: allowed again
        target = decide("accept")
        assert extraction_disposition(target.provenance) == DISPOSITION_ACCEPTED
        # accepted -> edited: allowed
        target = decide("edit", "new text")
        assert extraction_disposition(target.provenance) == DISPOSITION_EDITED
        # full history preserved: 4 canonical events
        events = [
            p
            for p in target.provenance
            if isinstance(p, dict) and p.get("event") == "extraction_disposition"
        ]
        assert [e["decision"] for e in events] == ["ACCEPTED", "REJECTED", "ACCEPTED", "EDITED"]
        # superseded is terminal
        from drilling_intelligence.engineering.source_to_draft import disposition_event

        [
            *target.provenance,
            disposition_event(
                "SUPERSEDED",
                by="rev",
                candidate_id=target.id,
                document_id=doc_id,
                version_id=ver_id,
                sha256=hashlib.sha256(raw).hexdigest(),
                locator={"document_version_id": ver_id},
            ),
        ]
        with pytest.raises(ValidationError, match="forbidden"):
            from drilling_intelligence.engineering.source_to_draft import (
                check_disposition_transition,
            )

            check_disposition_transition("SUPERSEDED", DISPOSITION_ACCEPTED)
