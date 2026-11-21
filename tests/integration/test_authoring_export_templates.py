"""Controlled export (JSON/HTML/DOCX/PDF) and structure-only templates for authoring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.engineering.authoring import EngineeringAuthoringService
from drilling_intelligence.engineering.export import export_record
from drilling_intelligence.engineering.repository import EngineeringRepository
from drilling_intelligence.engineering.templates import (
    PROCEDURE_TEMPLATES,
    PROGRAM_TEMPLATES,
    create_from_template,
    list_templates,
)
from drilling_intelligence.wells.repository import WellRepository


@pytest.fixture
def hierarchy(session):
    repository = WellRepository(session)
    project = repository.get_or_create_project("Export Fixture Block")
    field = repository.get_or_create_field("Export Fixture Field", project=project)
    well = repository.create_well("EXPORT-01", project_id=project.id, field_id=field.id)
    return {"project": project, "field": field, "well": well}


def _stamp(row) -> str:
    return row.updated_at.isoformat()


def _make_program(repo, service, hierarchy) -> object:
    program = repo.create_program(
        code="EXP-DRP",
        title="Export fixture program",
        summary="Fixture summary.",
        author="author-a",
        created_by="author-a",
        well_id=hierarchy["well"].id,
    )
    service.add_program_section(
        program.id,
        expected_updated_at=_stamp(program),
        section_key="design_basis",
        title="Design basis",
        content="Fixture design basis text.",
        completion_state="SOURCE_PROVIDED",
        source_reference="FIXTURE-SRC-1",
    )
    return repo.get_program(program.id)


def test_export_json_html_docx_pdf_are_no_clobber_and_draft_watermarked(
    session, hierarchy, tmp_path
) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    program = _make_program(repo, service, hierarchy)

    results = {}
    for fmt in ("json", "html", "docx", "pdf"):
        out = tmp_path / f"program.{fmt}"
        results[fmt] = export_record(service, "program", program.id, fmt=fmt, out_path=out)
        assert out.exists() and out.stat().st_size > 0
        assert results[fmt]["watermarked_draft"] is True
        assert results[fmt]["status"] == "DRAFT"

    payload = json.loads((tmp_path / "program.json").read_text())
    assert payload["record_type"] == "program"
    assert payload["id"] == program.id
    assert payload["sections"][0]["section_key"] == "design_basis"
    assert "DRAFT - NOT APPROVED" in payload["export_notice"]
    assert "not authenticated identities" in payload["attribution_notice"]

    html_text = (tmp_path / "program.html").read_text()
    assert "DRAFT - NOT APPROVED" in html_text
    assert "Export fixture program" in html_text

    # No-clobber: exporting again to the same path must refuse.
    with pytest.raises(ValidationError, match="no-clobber"):
        export_record(
            service, "program", program.id, fmt="json", out_path=tmp_path / "program.json"
        )

    # Unsupported format refused.
    with pytest.raises(ValidationError, match="unsupported export format"):
        export_record(service, "program", program.id, fmt="exe", out_path=tmp_path / "x.exe")


def test_export_procedure_includes_link_status(session, hierarchy, tmp_path) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    procedure = repo.create_procedure(
        code="EXP-PROC",
        title="Export fixture procedure",
        created_by="author-a",
        well_id=hierarchy["well"].id,
    )
    service.add_procedure_step(
        procedure.id,
        expected_updated_at=_stamp(procedure),
        title="Notify",
        instructions="Fixture instruction text.",
        responsible_role="driller",
        acceptance_criteria="Fixture acceptance text.",
    )
    result = export_record(
        service, "procedure", procedure.id, fmt="json", out_path=tmp_path / "proc.json"
    )
    payload = json.loads((tmp_path / "proc.json").read_text())
    assert payload["record_type"] == "procedure"
    assert payload["steps"][0]["title"] == "Notify"
    assert payload["program_link"]["link_status"] == "NONE"
    assert result["watermarked_draft"] is True


def test_program_template_creates_structure_only_draft(session, hierarchy) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    record = create_from_template(
        service,
        "program",
        "standard_drilling_program",
        content={
            "title": "Templated program",
            "author": "author-a",
            "well_id": hierarchy["well"].id,
            "code": "TPL-DRP",
        },
    )
    assert str(record.status) == "DRAFT"
    assert record.origin == "TEMPLATE"
    sections = repo.list_program_sections(record.id)
    expected = PROGRAM_TEMPLATES["standard_drilling_program"]["sections"]
    assert [s.title for s in sections] == [title for _key, title in expected]
    for section in sections:
        assert section.completion_state == "OWNER_DECISION_REQUIRED"
        assert section.content == ""
    # Structural validation must NOT report ready: no real content was supplied.
    assert service.validate_program(record.id)["ready_for_review"] is False


def test_procedure_template_creates_placeholder_steps_only(session, hierarchy) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    record = create_from_template(
        service,
        "procedure",
        "standard_operational_procedure",
        content={
            "title": "Templated procedure",
            "created_by": "author-a",
            "well_id": hierarchy["well"].id,
        },
    )
    steps = repo.list_procedure_steps(record.id)
    assert len(steps) == len(PROCEDURE_TEMPLATES["standard_operational_procedure"]["steps"])
    for step in steps:
        assert "PLACEHOLDER" in step.instructions
        assert step.responsible_role in (None, "")
        assert step.acceptance_criteria in (None, "")
    assert service.validate_procedure(record.id)["ready_for_review"] is False


def test_unknown_template_is_refused(session) -> None:
    service = EngineeringAuthoringService(session)
    with pytest.raises(ValidationError, match="unknown program template"):
        create_from_template(
            service,
            "program",
            "no_such_template",
            content={"title": "x", "author": "a"},
        )


def test_list_templates_covers_both_kinds() -> None:
    programs = list_templates("programs")
    procedures = list_templates("procedures")
    assert "standard_drilling_program" in programs
    assert "standard_operational_procedure" in procedures
    with pytest.raises(ValidationError, match="kind must be one of"):
        list_templates("widgets")


def test_draft_from_document_anchors_without_inferring(session, hierarchy) -> None:
    from drilling_intelligence.database.models import Document, DocumentVersion
    from drilling_intelligence.engineering.source_to_draft import create_draft_from_document

    document = Document(
        id="doc-src-1",
        identity_path="programs/src.docx",
        filename="src.docx",
        sha256="a" * 64,
    )
    version = DocumentVersion(
        id="ver-src-1",
        document_id=document.id,
        version_number=1,
        source_path="programs/src.docx",
        sha256="b" * 64,
        is_current=True,
    )
    session.add_all([document, version])
    session.flush()

    service = EngineeringAuthoringService(session)
    repo = EngineeringRepository(session)
    record = create_draft_from_document(
        service,
        "program",
        "doc-src-1",
        document_version_id="ver-src-1",
        content={
            "title": "Draft from source",
            "author": "author-a",
            "well_id": hierarchy["well"].id,
        },
    )
    assert str(record.status) == "DRAFT"
    assert record.document_id == "doc-src-1"
    assert record.document_version_id == "ver-src-1"
    for section in repo.list_program_sections(record.id):
        assert section.completion_state == "EXTRACTED_UNVERIFIED"
        assert section.source_reference.startswith("document:doc-src-1#ver-src-1")
    assert service.validate_program(record.id)["ready_for_review"] is False


def test_draft_from_document_refuses_unknown_document(session) -> None:
    from drilling_intelligence.engineering.source_to_draft import create_draft_from_document

    service = EngineeringAuthoringService(session)
    with pytest.raises(ValidationError, match="source document not found"):
        create_draft_from_document(
            service,
            "procedure",
            "doc-missing",
            content={"title": "x", "created_by": "a"},
        )


def _program_with_rich_target(repo, service, hierarchy):
    program = repo.create_program(
        code="EXP-DRP-2",
        title="Rich export fixture",
        summary="Fixture with a full target row.",
        author="author-a",
        created_by="author-a",
        well_id=hierarchy["well"].id,
    )
    service.add_program_target(
        program.id,
        expected_updated_at=_stamp(program),
        name="8 1/2 in",
        hole_size_in=8.5,
        casing_program="7 in liner",
        formation_top="Fixture Formation",
        planned_depth_md_value=2500.0,
        planned_depth_md_unit="m",
        planned_duration_days=12.5,
        planned_mud_weight_value=1.45,
        planned_mud_weight_unit="kg/m3",
        planned_npt_hours=6.0,
        planned_cost_value=125000.0,
        planned_cost_unit="EUR",
        provenance=[{"source_path": r"C:\Users\qa\source\plan.xlsx"}],
    )
    return repo.get_program(program.id)


def test_export_preserves_all_target_fields_and_stored_units(session, hierarchy, tmp_path) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    program = _program_with_rich_target(repo, service, hierarchy)
    export_record(service, "program", program.id, fmt="json", out_path=tmp_path / "p.json")
    payload = json.loads((tmp_path / "p.json").read_text())
    target = payload["targets"][0]
    assert target["formation_top"] == "Fixture Formation"
    assert target["casing_program"] == "7 in liner"
    assert target["planned_cost_value"] == 125000.0
    assert target["planned_cost_unit"] == "EUR"
    assert target["planned_mud_weight_unit"] == "kg/m3"
    assert target["planned_depth_md_unit"] == "m"

    # HTML must not claim a hard-coded unit: the stored unit appears, not "ppg".
    export_record(service, "program", program.id, fmt="html", out_path=tmp_path / "p.html")
    html_text = (tmp_path / "p.html").read_text()
    assert "kg/m3" in html_text
    assert "EUR" in html_text
    assert "ppg" not in html_text
    assert "m MD" not in html_text

    # PDF must be multi-page-capable and contain full content without truncation markers.
    export_record(service, "program", program.id, fmt="pdf", out_path=tmp_path / "p.pdf")
    import pymupdf

    doc = pymupdf.open(str(tmp_path / "p.pdf"))
    text = "".join(page.get_text() for page in doc)
    assert "Fixture Formation" in text
    assert "kg/m3" in text
    assert "125000.0 EUR" in text
    assert "DRAFT - NOT APPROVED" in text
    doc.close()


def test_pdf_paginates_long_content(session, hierarchy, tmp_path) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    program = repo.create_program(
        code="EXP-LONG",
        title="Long fixture",
        author="author-a",
        created_by="author-a",
        well_id=hierarchy["well"].id,
    )
    token = _stamp(program)
    for i in range(60):
        service.add_program_section(
            program.id,
            expected_updated_at=token,
            section_key=f"section_{i}",
            title=f"Section {i}",
            content="Long synthetic fixture text. " * 40,
            completion_state="SOURCE_PROVIDED",
            source_reference="FIXTURE",
        )
        program = repo.get_program(program.id)
        token = _stamp(program)
    export_record(service, "program", program.id, fmt="pdf", out_path=tmp_path / "long.pdf")
    import pymupdf

    doc = pymupdf.open(str(tmp_path / "long.pdf"))
    assert doc.page_count > 3
    text = "".join(page.get_text() for page in doc)
    assert "Section 59" in text  # no silent truncation of the last section
    assert "Page 2" in text  # page numbers rendered
    doc.close()


def test_redacted_export_masks_local_paths_only(session, hierarchy, tmp_path) -> None:
    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    program = _program_with_rich_target(repo, service, hierarchy)
    result = export_record(
        service, "program", program.id, fmt="json", out_path=tmp_path / "r.json", redact=True
    )
    assert result["redacted"] is True
    assert result["redaction_policy"] == "path-redaction-v1"
    payload = json.loads((tmp_path / "r.json").read_text())
    assert payload["redaction"]["applied"] is True
    prov = payload["targets"][0]["provenance"][0]["source_path"]
    assert "C:\\Users" not in prov and "REDACTED" in prov
    # Canonical record is untouched by producing a redacted copy.
    stored = repo.get_program(program.id)
    assert stored is not None
    stored_target = repo.list_targets(program.id)[0]
    assert stored_target.provenance[0]["source_path"] == r"C:\Users\qa\source\plan.xlsx"
    # Engineering text with slashes/colons is NOT redacted.
    assert payload["targets"][0]["casing_program"] == "7 in liner"


def test_redacted_export_covers_windows_unc_posix_and_file_uri(
    session, hierarchy, tmp_path
) -> None:
    from drilling_intelligence.engineering.export import _redact_paths

    value = {
        "windows": r"C:\data\src\doc.docx",
        "unc": r"\\fileserver\share\doc.docx",
        "posix": "/home/qa/documents/doc.pdf",
        "uri": "file:///home/qa/doc.pdf",
        "text": "Use 12 1/4 in hole; ratio 3:1 — keep as-is",
    }
    out = _redact_paths(value)
    for key in ("windows", "unc", "posix", "uri"):
        assert "REDACTED" in out[key], key
    assert out["text"] == value["text"]


def test_export_no_clobber_refuses_existing_symlink_and_race(session, hierarchy, tmp_path) -> None:

    repo = EngineeringRepository(session)
    service = EngineeringAuthoringService(session)
    program = _make_program(repo, service, hierarchy)
    existing = tmp_path / "exists.pdf"
    existing.write_bytes(b"foreign")
    with pytest.raises(ValidationError, match="no-clobber"):
        export_record(service, "program", program.id, fmt="pdf", out_path=existing)
    assert existing.read_bytes() == b"foreign"  # untouched

    link = tmp_path / "link.pdf"
    link.symlink_to(existing)
    with pytest.raises(ValidationError, match="no-clobber"):
        export_record(service, "program", program.id, fmt="pdf", out_path=link)

    # Concurrent creator between preflight and publication: pre-create the exact target
    # after the preflight would have run is simulated by exporting to an existing name.
    target = tmp_path / "race.pdf"
    target.write_bytes(b"concurrent")
    with pytest.raises(ValidationError, match="no-clobber"):
        export_record(service, "program", program.id, fmt="pdf", out_path=target)
    assert target.read_bytes() == b"concurrent"
    # No stray temp files remain after refusals.
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".export-")] == []


def _race_worker(target_str: str, payload: bytes, q) -> None:
    import os
    import tempfile

    from drilling_intelligence.engineering.export import _publish_atomic

    target = Path(target_str)
    fd, name = tempfile.mkstemp(dir=str(target.parent), prefix=".race-")
    try:
        view = memoryview(payload)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.close(fd)
        _publish_atomic(Path(name), target)
        q.put("ok")
    except Exception as exc:  # noqa: BLE001
        q.put(f"refused:{type(exc).__name__}")


def test_export_publication_race_exactly_one_winner(tmp_path) -> None:
    """Multi-process race: N processes publish to the same target; exactly one wins."""
    import multiprocessing as mp

    target = tmp_path / "race.bin"
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(target=_race_worker, args=(str(target), bytes([i]) * 100_000, q))
        for i in range(6)
    ]
    for proc in procs:
        proc.start()
    results = [q.get(timeout=60) for _ in procs]
    for proc in procs:
        proc.join(timeout=60)
    assert results.count("ok") == 1, results
    assert target.read_bytes() in [bytes([i]) * 100_000 for i in range(6)]
    leftovers = [
        p
        for p in tmp_path.iterdir()
        if p.name.startswith(".race-") or p.name.startswith(".export-")
    ]
    assert leftovers == []


def test_export_publication_refuses_existing_and_symlink(tmp_path) -> None:
    from drilling_intelligence.engineering.export import _publish_atomic

    target = tmp_path / "taken.bin"
    target.write_bytes(b"original")
    src = tmp_path / "s.bin"
    src.write_bytes(b"new")
    with pytest.raises(ValidationError, match="no-clobber"):
        _publish_atomic(src, target)
    assert target.read_bytes() == b"original"

    link = tmp_path / "link.bin"
    link.symlink_to(target)
    src2 = tmp_path / "s2.bin"
    src2.write_bytes(b"new2")
    with pytest.raises(ValidationError, match="no-clobber"):
        _publish_atomic(src2, link)
    assert target.read_bytes() == b"original"
    assert link.is_symlink()


def test_redaction_covers_all_formats_and_nested_paths(session, hierarchy, tmp_path) -> None:
    """Redacted JSON/HTML/DOCX/PDF must not leak the local source path anywhere."""
    local_path = "/home/user/secret/plan.docx"  # synthetic fixture path, not a credential
    from drilling_intelligence.engineering.authoring import EngineeringAuthoringService
    from drilling_intelligence.engineering.export import export_record

    service = EngineeringAuthoringService(session)
    repo = EngineeringRepository(session)
    program = repo.create_program(
        title="Redact fmt",
        code="RD-1",
        author="a",
        created_by="a",
        origin="HUMAN",
        well_id=hierarchy["well"].id,
        provenance=[{"source_reference": local_path}],
    )
    token = service._token(program)
    service.add_program_section(
        program.id,
        expected_updated_at=token,
        sequence=1,
        section_key="s",
        title="S",
        content="body",
        source_reference=local_path,
        completion_state="SOURCE_PROVIDED",
        origin="HUMAN",
    )
    session.flush()
    for fmt, suffix in (("json", ".json"), ("html", ".html"), ("docx", ".docx"), ("pdf", ".pdf")):
        full = export_record(
            service,
            "program",
            repo.list_programs()[0].id,
            fmt=fmt,
            out_path=tmp_path / f"full{suffix}",
        )
        red = export_record(
            service,
            "program",
            repo.list_programs()[0].id,
            fmt=fmt,
            out_path=tmp_path / f"red{suffix}",
            redact=True,
        )

        def _blob(path: Path, sfx: str = suffix) -> bytes:
            data = path.read_bytes()
            if sfx == ".docx":
                import zipfile

                with zipfile.ZipFile(path) as zf:
                    return b"\n".join(zf.read(n) for n in zf.namelist())
            if sfx == ".pdf":
                import fitz

                with fitz.open(path) as doc:
                    return b"\n".join(page.get_text().encode() for page in doc)
            return data

        full_bytes = _blob(Path(full["path"]))
        red_bytes = _blob(Path(red["path"]))
        assert local_path.encode() in full_bytes, fmt
        assert local_path.encode() not in red_bytes, f"{fmt} redacted export leaked the local path"
        # canonical record untouched
        canon = repo.get_program(repo.list_programs()[0].id)
        assert canon.provenance == [{"source_reference": local_path}]


def test_template_governance_rejects_mismatched_family_version(session, hierarchy) -> None:
    """family/version/template mismatch and unknown kinds are refused."""
    from drilling_intelligence.engineering.templates import (
        create_from_template,
        resolve_template_for_create,
    )

    service = EngineeringAuthoringService(session)
    # procedure template name with program kind must be refused
    with pytest.raises(ValidationError, match="unknown program template"):
        resolve_template_for_create("program", "standard_operational_procedure")
    # version 1 of drilling_program resolves to the v1 template only
    with pytest.raises(ValidationError, match="does not belong"):
        resolve_template_for_create("program", "standard_drilling_program_v2", 1)
    with pytest.raises(ValidationError, match="no version"):
        resolve_template_for_create("program", "standard_drilling_program", 99)
    with pytest.raises(ValidationError, match="kind"):
        create_from_template(
            service,
            "widget",
            "standard_drilling_program",
            content={"title": "t", "author": "a"},
        )
    # default resolution picks the actual CURRENT version (v2)
    resolved = resolve_template_for_create("program", "standard_drilling_program_v2")
    assert resolved["version"] == 2 and resolved["status"] == "CURRENT"


def test_extraction_disposition_states_are_explicit(session, hierarchy, tmp_path) -> None:
    """Pending/accepted/edited/rejected are recorded as explicit provenance events."""
    import hashlib
    from datetime import UTC, datetime

    from drilling_intelligence.documents.repository import DocumentRepository
    from drilling_intelligence.engineering.source_to_draft import (
        DISPOSITION_ACCEPTED,
        DISPOSITION_EDITED,
        DISPOSITION_PENDING,
        DISPOSITION_REJECTED,
        create_draft_from_document,
        decide_extracted_item,
        extraction_disposition,
    )

    content = b"# A\n\nalpha\n\n# B\n\nbeta\n\n# C\n\ngamma\n"
    src = tmp_path / "disp.md"
    src.write_bytes(content)
    docs = DocumentRepository(session)
    doc = docs.create_document(
        workspace_id=None,
        unscoped=True,
        identity_path="fixtures/disp.md",
        filename="disp.md",
        extension=".md",
        mime_type="text/markdown",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        file_modified_at=datetime.now(UTC),
    )
    docs.create_version(
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
    session.flush()
    service = EngineeringAuthoringService(session)
    record = create_draft_from_document(
        service,
        "program",
        doc.id,
        content={"title": "D", "code": "DISP-1", "author": "a"},
        extract=True,
        workspace_root=tmp_path,
    )
    session.flush()
    repo = EngineeringRepository(session)
    sections = repo.list_program_sections(record.id)
    assert len(sections) == 6
    assert all(extraction_disposition(s.provenance) == DISPOSITION_PENDING for s in sections)

    def decide(idx: int, decision: str, text: str | None = None) -> list:
        row = repo.get_program(record.id)
        decide_extracted_item(
            service,
            "program",
            record.id,
            sections[idx].id,
            decision=decision,
            expected_updated_at=service._token(row),
            by="reviewer",
            edited_text=text,
        )
        session.flush()
        return repo.list_program_sections(record.id)

    sections = decide(0, "accept")
    sections = decide(1, "edit", "author replacement text")
    sections = decide(2, "reject")
    assert extraction_disposition(sections[0].provenance) == DISPOSITION_ACCEPTED
    assert extraction_disposition(sections[1].provenance) == DISPOSITION_EDITED
    assert sections[1].content == "author replacement text"
    # original locator retained after edit
    loc = next(p for p in sections[1].provenance if isinstance(p, dict) and p.get("extracted"))
    assert loc["locator"]["sha256"] == hashlib.sha256(content).hexdigest()
    assert extraction_disposition(sections[2].provenance) == DISPOSITION_REJECTED
    # rejected content was never promoted
    assert sections[2].completion_state == "EXTRACTED_UNVERIFIED"
