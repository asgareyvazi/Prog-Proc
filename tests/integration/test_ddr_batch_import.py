"""Phase C: DDR batch PDF import — hardened manifest, path containment (D-15),
workspace-scoped duplicates (D-16), exact-version review queue (D-17),
resource limits and partial-failure isolation (D-18)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drilling_intelligence.cli.app import _workspace_row_id
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.documents.registry import DocumentRegistry
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.extraction.registry import build_default_router
from drilling_intelligence.operations.ddr_batch import (
    MANIFEST_FORMAT,
    MAX_MANIFEST_ENTRIES,
    import_ddr_batch,
    load_manifest,
    review_queue,
)


def _pdf_bytes(text: str) -> bytes:
    """Minimal synthetic single-page PDF carrying plain text (labelled synthetic)."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    stream = content.encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


DDR_TEXT = (
    "Daily Drilling Report - SYNTHETIC FIXTURE - Rig RIG-1 spud. "
    "Report No. SYN-1. Depth from 100 to 200 m. Activity code DRILL. 06:00 08:00."
)


def _write_corpus(base: Path) -> Path:
    (base / "ddr_syn_1.pdf").write_bytes(_pdf_bytes(DDR_TEXT))
    (base / "ddr_syn_2.pdf").write_bytes(_pdf_bytes(DDR_TEXT + " Report No. SYN-2."))
    (base / "notes.txt.pdf").write_bytes(
        _pdf_bytes("unrelated synthetic text, not a drilling report")
    )
    (base / "plain.txt").write_text("not a pdf")
    manifest = base / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "format": MANIFEST_FORMAT,
                "files": [
                    {"path": "ddr_syn_1.pdf", "note": "synthetic fixture"},
                    {"path": "ddr_syn_2.pdf"},
                    {"path": "notes.txt.pdf"},
                    {"path": "missing.pdf"},
                    {"path": "plain.txt"},
                ],
            }
        )
    )
    return manifest


def _registry(workspace, session):
    return DocumentRegistry(
        DocumentRepository(session),
        router=build_default_router(workspace.settings),
        settings=workspace.settings,
    )


def _import(workspace, manifest, base_dir):
    with workspace.database.unit_of_work() as session:
        registry = _registry(workspace, session)
        summary = import_ddr_batch(
            registry,
            manifest_path=manifest,
            base_dir=base_dir,
            workspace_root=workspace.root,
            workspace_id=_workspace_row_id(workspace),
        )
        queue = review_queue(registry, workspace_id=_workspace_row_id(workspace))
    return summary, queue


def test_batch_import_manifest_classification_queue_idempotency_and_partial_failures(
    workspace, tmp_path
) -> None:
    manifest = _write_corpus(tmp_path)

    with pytest.raises(ValidationError, match="unreadable or not valid JSON"):
        load_manifest(tmp_path / "plain.txt")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format": "other/v9", "files": [{"path": "x.pdf"}]}))
    with pytest.raises(ValidationError, match="unsupported batch manifest format"):
        load_manifest(bad)
    entries = load_manifest(manifest)
    assert len(entries) == 5

    summary, queue = _import(workspace, manifest, tmp_path)
    assert summary.total == 5
    assert summary.imported == 3
    assert summary.failed == 2  # missing file + non-PDF
    assert summary.skipped_duplicate == 0
    statuses = {e.filename: e.status for e in summary.entries}
    assert statuses["missing.pdf"] == "FAILED"
    assert "file missing" in next(
        e.error for e in summary.entries if e.filename == "missing.pdf"
    ) or "not an existing regular file" in next(
        e.error for e in summary.entries if e.filename == "missing.pdf"
    )
    assert statuses["plain.txt"] == "UNSUPPORTED"
    ddr_entries = [e for e in summary.entries if e.filename.startswith("ddr_syn")]
    assert all(e.status == "IMPORTED" for e in ddr_entries)
    assert all(e.classification == "DDR" for e in ddr_entries)
    assert all(e.pending_review for e in ddr_entries)
    assert summary.pending_review == 2
    non_ddr = next(e for e in summary.entries if e.filename == "notes.txt.pdf")
    assert non_ddr.status == "IMPORTED" and not non_ddr.pending_review

    # D-17: queue is keyed by exact current version with SHA and pending state.
    assert len(queue) == 2
    for item in queue:
        assert item["classification"] == "DDR"
        assert item["pending_review"] is True
        assert item["version_disposition"] == "PENDING_REVIEW"
        assert item["document_version_id"]
        assert len(item["sha256"]) == 64

    # Idempotency: re-importing the same manifest creates nothing new.
    again, _ = _import(workspace, manifest, tmp_path)
    assert again.imported == 0
    assert again.skipped_duplicate == 3
    assert again.failed == 2
    assert again.entries[0].status == "SKIPPED_DUPLICATE"


# ------------------------------------------------------------- D-15
def test_d15_path_traversal_and_symlink_escape_refused(workspace, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(_pdf_bytes(DDR_TEXT))
    base = tmp_path / "batch"
    base.mkdir()
    (base / "ok.pdf").write_bytes(_pdf_bytes(DDR_TEXT))
    link = base / "link.pdf"
    link.symlink_to(outside / "secret.pdf")

    cases = {
        "traversal": "../outside/secret.pdf",
        "absolute": str(outside / "secret.pdf"),
        "symlink": "link.pdf",
        "nonexistent": "nope.pdf",
    }
    for name, rel in cases.items():
        manifest = base / f"m_{name}.json"
        manifest.write_text(json.dumps({"format": MANIFEST_FORMAT, "files": [{"path": rel}]}))
        with pytest.raises(ValidationError):
            load_manifest(manifest, base_dir=base)
        summary, _ = _import(workspace, manifest, base)
        assert summary.entries[0].status in ("FAILED", "UNSUPPORTED")
        assert summary.imported == 0


# ------------------------------------------------------------- D-16
def test_d16_duplicate_detection_is_workspace_scoped(workspace, tmp_path, settings) -> None:
    from drilling_intelligence.wells.workspace import Workspace

    manifest = _write_corpus(tmp_path)
    summary, _ = _import(workspace, manifest, tmp_path)
    assert summary.imported == 3

    # Same bytes in a SECOND workspace must register as that workspace's own document.
    ws2 = Workspace.create(tmp_path / "ws2", settings, name="Second Workspace")
    summary2, _ = _import(ws2, manifest, tmp_path)
    assert summary2.imported == 3, "identical bytes in another workspace are not duplicates"
    assert summary2.skipped_duplicate == 0

    # Same workspace, same bytes again -> duplicates, same document ids.
    again, _ = _import(workspace, manifest, tmp_path)
    assert again.skipped_duplicate == 3
    first_ids = {e.document_id for e in summary.entries if e.status == "IMPORTED"}
    again_ids = {e.document_id for e in again.entries if e.status == "SKIPPED_DUPLICATE"}
    assert first_ids == again_ids
    ws2.close()


def test_d16_same_sha_different_path_and_different_well(workspace, tmp_path) -> None:
    base = tmp_path / "b"
    base.mkdir()
    (base / "a.pdf").write_bytes(_pdf_bytes(DDR_TEXT))
    (base / "copy_of_a.pdf").write_bytes(_pdf_bytes(DDR_TEXT))  # identical bytes, new path
    manifest = base / "m.json"
    manifest.write_text(
        json.dumps(
            {"format": MANIFEST_FORMAT, "files": [{"path": "a.pdf"}, {"path": "copy_of_a.pdf"}]}
        )
    )
    summary, _ = _import(workspace, manifest, base)
    assert summary.imported == 1
    assert summary.skipped_duplicate == 1  # same workspace, same bytes -> duplicate
    assert summary.entries[1].document_id == summary.entries[0].document_id


# ------------------------------------------------------------- D-17
def test_d17_review_is_exact_version_scoped(workspace, tmp_path) -> None:
    from drilling_intelligence.engineering.source_to_draft import disposition_event

    base = tmp_path / "b"
    base.mkdir()
    (base / "v1.pdf").write_bytes(_pdf_bytes(DDR_TEXT))
    manifest = base / "m.json"
    manifest.write_text(json.dumps({"format": MANIFEST_FORMAT, "files": [{"path": "v1.pdf"}]}))
    summary, queue = _import(workspace, manifest, base)
    assert len(queue) == 1 and queue[0]["pending_review"] is True
    doc_id = summary.entries[0].document_id
    ver1 = summary.entries[0].document_version_id

    # Record a disposition on version 1 via a candidate provenance event whose
    # locator names exactly that version -> queue clears for that exact version.
    from drilling_intelligence.database.models import ProcedureStep
    from drilling_intelligence.engineering.repository import EngineeringRepository

    with workspace.database.unit_of_work() as session:
        registry = _registry(workspace, session)
        doc = registry.repository.get(doc_id)
        current = registry.repository.current_version(doc)
        proc = EngineeringRepository(session).create_procedure(
            title="D17 fixture procedure", created_by="reviewer"
        )
        step = ProcedureStep(
            id="step-d17",
            procedure_id=proc.id,
            sequence=1,
            title="candidate",
            instructions="synthetic candidate",
            completion_state="DRAFT",
            provenance=[
                {
                    "extracted": True,
                    "locator": {
                        "document_id": doc_id,
                        "document_version_id": current.id,
                        "sha256": current.sha256,
                    },
                },
                disposition_event(
                    decision="ACCEPTED",
                    by="reviewer",
                    candidate_id="step-d17",
                    document_id=doc_id,
                    version_id=current.id,
                    sha256=current.sha256,
                    locator={
                        "document_id": doc_id,
                        "document_version_id": current.id,
                        "sha256": current.sha256,
                    },
                ),
            ],
        )
        session.add(step)
    with workspace.database.unit_of_work() as session:
        registry = _registry(workspace, session)
        queue = review_queue(registry, workspace_id=_workspace_row_id(workspace))
    assert queue[0]["document_version_id"] == ver1
    assert queue[0]["pending_review"] is False
    assert queue[0]["version_disposition"] == "ACCEPTED"


# ------------------------------------------------------------- D-18
def test_d18_manifest_hardening_limits(workspace, tmp_path) -> None:
    base = tmp_path / "b"
    base.mkdir()
    # duplicate entries refused
    dup = base / "dup.json"
    dup.write_text(
        json.dumps(
            {
                "format": MANIFEST_FORMAT,
                "files": [{"path": "x.pdf"}, {"path": "x.pdf"}],
            }
        )
    )
    with pytest.raises(ValidationError, match="duplicate manifest entry"):
        load_manifest(dup, base_dir=base)
    # unknown keys refused
    unk = base / "unk.json"
    unk.write_text(json.dumps({"format": MANIFEST_FORMAT, "files": [{"path": "x.pdf", "evil": 1}]}))
    with pytest.raises(ValidationError, match="unknown manifest entry key"):
        load_manifest(unk, base_dir=base)
    # over-limit entry count refused
    big = base / "big.json"
    big.write_text(
        json.dumps(
            {
                "format": MANIFEST_FORMAT,
                "files": [{"path": f"f{i}.pdf"} for i in range(MAX_MANIFEST_ENTRIES + 1)],
            }
        )
    )
    with pytest.raises(ValidationError, match="maximum entry count"):
        load_manifest(big, base_dir=base)
    # non-PDF-signature .pdf refused at import time
    fake = base / "fake.pdf"
    fake.write_bytes(b"this is not a pdf at all")
    m = base / "m.json"
    m.write_text(json.dumps({"format": MANIFEST_FORMAT, "files": [{"path": "fake.pdf"}]}))
    summary, _ = _import(workspace, m, base)
    assert summary.entries[0].status == "UNSUPPORTED"
    assert "PDF signature" in summary.entries[0].error


# ---------------------------------------------------------------- Phase D (2026-11-24)
def _manifest(base: Path, files: list[str]) -> Path:
    m = base / "manifest.json"
    m.write_text(json.dumps({"format": MANIFEST_FORMAT, "files": [{"path": f} for f in files]}))
    return m


def test_d1_missing_workspace_id_fails_closed(workspace, tmp_path) -> None:
    base = tmp_path / "b"
    base.mkdir()
    (base / "a.pdf").write_bytes(_pdf_bytes(DDR_TEXT))
    m = _manifest(base, ["a.pdf"])
    with workspace.database.unit_of_work() as session:
        registry = _registry(workspace, session)
        for bad in (None, "", "   "):
            with pytest.raises(ValidationError, match="workspace_id"):
                import_ddr_batch(
                    registry,
                    manifest_path=m,
                    base_dir=base,
                    workspace_root=workspace.root,
                    workspace_id=bad,
                )


def test_d1_identical_bytes_in_two_workspaces_own_separate_records(
    workspace, tmp_path, settings
) -> None:
    from drilling_intelligence.wells.workspace import Workspace

    other_root = tmp_path / "ws2"
    from drilling_intelligence.cli.app import main as cli_main

    cli_main(["workspace", "create", str(other_root)])
    other = Workspace.open(other_root)
    base = tmp_path / "b"
    base.mkdir()
    (base / "a.pdf").write_bytes(_pdf_bytes(DDR_TEXT))
    m = _manifest(base, ["a.pdf"])

    s1, _ = _import(workspace, m, base)
    with other.database.unit_of_work() as session:
        registry = _registry(other, session)
        s2 = import_ddr_batch(
            registry,
            manifest_path=m,
            base_dir=base,
            workspace_root=other.root,
            workspace_id=_workspace_row_id(other),
        )
    assert s1.imported == 1 and s2.imported == 1  # NOT a cross-workspace duplicate
    assert s1.entries[0].document_id != s2.entries[0].document_id
    # each workspace's queue sees only its own document
    with workspace.database.read_only() as session:
        q1 = review_queue(_registry(workspace, session), workspace_id=_workspace_row_id(workspace))
    with other.database.read_only() as session:
        q2 = review_queue(_registry(other, session), workspace_id=_workspace_row_id(other))
    assert {q["document_id"] for q in q1} == {s1.entries[0].document_id}
    assert {q["document_id"] for q in q2} == {s2.entries[0].document_id}


def test_d2_file_replaced_after_validation_cannot_slip_through(
    workspace, tmp_path, monkeypatch
) -> None:
    """TOCTOU: bytes are read once through a safe fd; the hashed bytes are registered."""
    import drilling_intelligence.operations.ddr_batch as mod

    base = tmp_path / "b"
    base.mkdir()
    target = base / "a.pdf"
    target.write_bytes(_pdf_bytes(DDR_TEXT))
    m = _manifest(base, ["a.pdf"])

    real_validate = mod._validate_entry_path
    swapped = False

    def racing_validate(raw, base_resolved):
        nonlocal swapped
        path = real_validate(raw, base_resolved)
        if not swapped:
            swapped = True
            # an attacker replaces the file between validation and use
            target.write_bytes(_pdf_bytes("TOTALLY DIFFERENT CONTENT"))
        return path

    monkeypatch.setattr(mod, "_validate_entry_path", racing_validate)
    with workspace.database.unit_of_work() as session:
        registry = _registry(workspace, session)
        summary = import_ddr_batch(
            registry,
            manifest_path=m,
            base_dir=base,
            workspace_root=workspace.root,
            workspace_id=_workspace_row_id(workspace),
        )
    # The registered document must carry the SHA of the bytes actually read post-swap,
    # and that SHA must match the bytes on the registered (staged) source path.
    entry = summary.entries[0]
    assert entry.status == "IMPORTED"
    with workspace.database.read_only() as session:
        version = DocumentRepository(session).version(entry.document_version_id)
        assert version.sha256 == entry.sha256
        import hashlib

        assert hashlib.sha256(Path(version.source_path).read_bytes()).hexdigest() == version.sha256


def test_d7_on_entry_fires_exactly_once_per_entry_and_cancel_counted(workspace, tmp_path) -> None:
    base = tmp_path / "b"
    base.mkdir()
    (base / "a.pdf").write_bytes(_pdf_bytes(DDR_TEXT))
    (base / "bad.pdf").write_bytes(b"not a pdf at all")
    m = _manifest(base, ["a.pdf", "bad.pdf", "missing.pdf"])
    calls: list[str] = []
    states = iter([False, False, True])  # cancel before the third entry
    with workspace.database.unit_of_work() as session:
        registry = _registry(workspace, session)
        summary = import_ddr_batch(
            registry,
            manifest_path=m,
            base_dir=base,
            workspace_root=workspace.root,
            workspace_id=_workspace_row_id(workspace),
            should_cancel=lambda: next(states),
            on_entry=lambda r: calls.append(r.status),
        )
    assert len(calls) == summary.total == 3  # exactly once per entry
    assert calls[-1] == "CANCELLED"
    assert summary.cancelled == 1
    assert summary.to_dict()["cancelled"] == 1
    assert summary.imported == 1 and summary.failed == 1
