"""Phase L: DDR batch PDF import — manifest, classification, review queue,
idempotency, partial-failure reporting."""

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
    ddr1 = base / "ddr_syn_1.pdf"
    ddr2 = base / "ddr_syn_2.pdf"
    other = base / "notes.txt.pdf"  # wrong content for DDR classification
    ddr1.write_bytes(_pdf_bytes(DDR_TEXT))
    ddr2.write_bytes(_pdf_bytes(DDR_TEXT + " Report No. SYN-2."))
    other.write_bytes(_pdf_bytes("unrelated synthetic text, not a drilling report"))
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
    (base / "plain.txt").write_text("not a pdf")
    return manifest


def _import(workspace, manifest, base_dir):
    """Run one batch import in a committing unit of work."""
    with workspace.database.unit_of_work() as session:
        registry = DocumentRegistry(
            DocumentRepository(session),
            router=build_default_router(workspace.settings),
            settings=workspace.settings,
        )
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

    # Manifest validation
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
    assert summary.imported == 3  # two DDR-classified + one non-DDR pdf, all registered
    assert summary.failed == 2  # missing file + non-PDF
    assert summary.skipped_duplicate == 0
    statuses = {e.filename: e.status for e in summary.entries}
    assert statuses["missing.pdf"] == "FAILED"
    assert "file missing" in next(e.error for e in summary.entries if e.filename == "missing.pdf")
    assert statuses["plain.txt"] == "FAILED"
    ddr_entries = [e for e in summary.entries if e.filename.startswith("ddr_syn")]
    assert all(e.status == "IMPORTED" for e in ddr_entries)
    assert all(e.classification == "DDR" for e in ddr_entries)
    assert all(e.queued_for_review for e in ddr_entries)
    assert summary.queued_for_review == 2
    non_ddr = next(e for e in summary.entries if e.filename == "notes.txt.pdf")
    assert non_ddr.status == "IMPORTED" and not non_ddr.queued_for_review

    # Review queue lists the DDR-classified documents without disposition.
    assert len(queue) == 2
    assert all(
        item["classification"] == "DDR" and not item["disposition_recorded"] for item in queue
    )

    # Idempotency: re-importing the same manifest creates nothing new.
    again, _ = _import(workspace, manifest, tmp_path)
    assert again.imported == 0
    assert again.skipped_duplicate == 3  # all three PDFs already registered by content hash
    assert again.failed == 2
    assert again.entries[0].status == "SKIPPED_DUPLICATE"
