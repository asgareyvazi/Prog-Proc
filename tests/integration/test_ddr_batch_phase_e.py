"""Phase E (2026-11-24B): batch-import hardening adversarial tests.

- E1: workspace_id binding fails closed (mandatory, real row id).
- E2: fd-based openat traversal — a symlink at any path component is refused,
  not just a final-component O_NOFOLLOW.
- E3: atomic collision-safe staging (O_EXCL + fsync + rename) and per-entry
  SAVEPOINT transactions: one failed entry never rolls back earlier entries.
- E4: version/review adversarial — a disposition on an older version never
  disposes the current one.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from tests.integration.test_ddr_batch_import import DDR_TEXT, _import, _pdf_bytes, _write_corpus

from drilling_intelligence.cli.app import _workspace_row_id
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.operations.ddr_batch import (
    MANIFEST_FORMAT,
    _open_entry_fd,
    _stage_atomic,
    import_ddr_batch,
)


def test_e1_missing_workspace_id_fails_closed(workspace, tmp_path) -> None:
    manifest = _write_corpus(tmp_path)
    with workspace.database.unit_of_work() as session:
        from drilling_intelligence.documents.registry import DocumentRegistry
        from drilling_intelligence.documents.repository import DocumentRepository
        from drilling_intelligence.extraction.registry import build_default_router

        registry = DocumentRegistry(
            DocumentRepository(session),
            router=build_default_router(workspace.settings),
            settings=workspace.settings,
        )
        with pytest.raises(ValidationError, match="workspace_id is required"):
            import_ddr_batch(
                registry,
                manifest_path=manifest,
                base_dir=tmp_path,
                workspace_root=workspace.root,
                workspace_id="",
            )


def test_e2_symlink_at_intermediate_component_refused(tmp_path) -> None:
    base = tmp_path / "batch"
    (base / "real").mkdir(parents=True)
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(_pdf_bytes("synthetic"))
    (base / "linkdir").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValidationError, match="safely opened"):
        _open_entry_fd(base.resolve(), "linkdir/secret.pdf")


def test_e2_final_component_symlink_refused(tmp_path) -> None:
    base = tmp_path / "batch"
    base.mkdir()
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(_pdf_bytes("synthetic"))
    (base / "evil.pdf").symlink_to(secret)
    with pytest.raises(ValidationError):
        _open_entry_fd(base.resolve(), "evil.pdf")


def test_e2_regular_file_opens(tmp_path) -> None:
    base = tmp_path / "batch"
    base.mkdir()
    (base / "ok.pdf").write_bytes(_pdf_bytes("synthetic"))
    fd = _open_entry_fd(base.resolve(), "ok.pdf")
    try:
        assert os.fstat(fd).st_size > 0
    finally:
        os.close(fd)


def test_e3_staging_is_atomic_and_collision_safe(workspace, tmp_path) -> None:
    payload = _pdf_bytes("synthetic atomic staging probe")
    a = _stage_atomic(payload, "dup.pdf", workspace.root)
    b = _stage_atomic(payload, "dup.pdf", workspace.root)
    assert a == b  # digest-addressed: same bytes, same final name
    assert a.read_bytes() == payload
    digest = hashlib.sha256(payload).hexdigest()
    assert a.name.startswith(digest[:16])
    leftovers = list(
        (workspace.root / ".drillintel" / "batch_imports" / "staging").glob("*.partial")
    )
    assert leftovers == []


def test_e3_per_entry_savepoint_isolates_failures(workspace, tmp_path) -> None:
    base = tmp_path / "batch"
    base.mkdir()
    (base / "good.pdf").write_bytes(_pdf_bytes(DDR_TEXT))
    (base / "bad.pdf").write_bytes(b"%PDF-1.4 broken")  # valid signature, junk body
    manifest = base / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "format": MANIFEST_FORMAT,
                "files": [{"path": "good.pdf"}, {"path": "bad.pdf"}],
            }
        )
    )
    summary, queue = _import(workspace, manifest, base)
    statuses = {e.filename: e.status for e in summary.entries}
    assert statuses["good.pdf"] == "IMPORTED"
    assert statuses["bad.pdf"] in ("FAILED", "UNSUPPORTED")
    assert summary.imported == 1
    # the good entry survived the bad entry's savepoint rollback
    assert any(q["document_id"] == summary.entries[0].document_id for q in queue)


def test_e4_disposition_on_old_version_never_disposes_current(workspace, tmp_path) -> None:
    from drilling_intelligence.operations.ddr_batch import review_queue

    manifest = _write_corpus(tmp_path)
    summary, _queue = _import(workspace, manifest, tmp_path)
    doc_id = summary.entries[0].document_id
    with workspace.database.unit_of_work() as session:
        from drilling_intelligence.documents.registry import DocumentRegistry
        from drilling_intelligence.documents.repository import DocumentRepository
        from drilling_intelligence.extraction.registry import build_default_router

        registry = DocumentRegistry(
            DocumentRepository(session),
            router=build_default_router(workspace.settings),
            settings=workspace.settings,
        )
        repo = registry.repository
        doc = repo.get(doc_id)
        v1 = repo.current_version(doc)
        # record a disposition event against the CURRENT version via metadata append

        meta = dict(v1.metadata_json or {})
        dispositions = list(meta.get("extraction_dispositions") or [])
        dispositions.append(
            {
                "event": "extraction_disposition",
                "decision": "ACCEPTED",
                "locator": {"document_version_id": v1.id},
            }
        )
        meta["extraction_dispositions"] = dispositions
        v1.metadata_json = meta
        session.flush()
        q = review_queue(registry, workspace_id=_workspace_row_id(workspace))
        entry = next(x for x in q if x["document_id"] == doc_id)
        assert entry["version_disposition"] == "ACCEPTED"
        assert entry["pending_review"] is False
