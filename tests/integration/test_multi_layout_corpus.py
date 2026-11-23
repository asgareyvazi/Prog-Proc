"""D-22: multi-layout golden corpus for DDR extraction.

Three synthetic DDR PDFs with deliberately different layouts (prose paragraph,
labelled key-value block, tabular activity grid) must all classify as DDR and
extract their report number through the real router. All fixtures are synthetic
and labelled; no real well data.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.integration.test_ddr_batch_import import _pdf_bytes

from drilling_intelligence.documents.registry import DocumentRegistry
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.extraction.registry import build_default_router
from drilling_intelligence.operations.ddr_batch import MANIFEST_FORMAT, import_ddr_batch

LAYOUTS = {
    "prose": (
        "Daily Drilling Report - SYNTHETIC LAYOUT-A - Report No. LAY-A1. "
        "Well A-1 spudded; depth from 10 to 30 m. Activity code DRILL."
    ),
    "keyvalue": (
        "DAILY DRILLING REPORT SYNTHETIC LAYOUT-B\n"
        "Report No.: LAY-B1\nWell: B-1\nDepth From: 40 m To: 55 m\nActivity: DRILL\n"
    ),
    "grid": (
        "DAILY DRILLING REPORT - SYNTHETIC LAYOUT-C - report LAY-C1 | well C-1 | "
        "interval 60-75 m | code DRILL | 06:00 08:00 | 08:00 10:00"
    ),
}


def _build_corpus(base: Path) -> Path:
    files = []
    for name, text in LAYOUTS.items():
        fname = f"ddr_{name}_syn.pdf"
        (base / fname).write_bytes(_pdf_bytes(text))
        files.append({"path": fname, "note": f"synthetic layout {name}"})
    manifest = base / "manifest.json"
    manifest.write_text(json.dumps({"format": MANIFEST_FORMAT, "files": files}))
    return manifest


def test_multi_layout_corpus_all_classify_ddr(workspace, tmp_path):
    base = tmp_path / "corpus"
    base.mkdir()
    manifest = _build_corpus(base)
    with workspace.database.unit_of_work() as session:
        registry = DocumentRegistry(
            DocumentRepository(session),
            router=build_default_router(workspace.settings),
            settings=workspace.settings,
        )
        from drilling_intelligence.cli.app import _workspace_row_id

        summary = import_ddr_batch(
            registry,
            manifest_path=manifest,
            base_dir=base,
            workspace_root=workspace.root,
            workspace_id=_workspace_row_id(workspace),
        )
    assert summary.total == 3
    assert summary.imported == 3, [(e.filename, e.error) for e in summary.entries]
    assert all(e.classification == "DDR" for e in summary.entries)
    # Each layout's extracted text must be retrievable from the extraction cache
    # and carry that layout's report token.
    from sqlalchemy import select

    from drilling_intelligence.database.models import Extraction

    numbers = set()
    with workspace.database.read_only() as session:
        for entry in summary.entries:
            version = DocumentRepository(session).version(entry.document_version_id)
            assert version is not None and version.sha256 == entry.sha256
            rows = (
                session.execute(
                    select(Extraction).where(Extraction.document_version_id == version.id)
                )
                .scalars()
                .all()
            )
            text = " ".join(
                (r.text_blob or "") + " " + json.dumps(r.document_json or {}) for r in rows
            )
            for token in ("LAY-A1", "LAY-B1", "LAY-C1"):
                if token in text:
                    numbers.add(token)
    assert numbers == {"LAY-A1", "LAY-B1", "LAY-C1"}, numbers
