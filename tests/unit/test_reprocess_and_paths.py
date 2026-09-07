"""Reprocessing, durable source paths and what a workspace move must not break.

Three rules are tested here, all of them about the difference between "what the database
believes" and "what is on the disk right now":

*   ``reprocess()`` measures the file.  The hash recorded in ``document.sha256`` describes
    the content we last read, which is exactly the assumption a reprocess exists to
    challenge - trusting it would let a modified file be filed under the old version's
    identity;
*   a missing or unreadable source is an error, and the registry is left alone: no new
    version, no updated hash, because "I could not read it" is not a content state;
*   the durable reference for a version is its path *inside the workspace*.  The absolute
    path is recorded too, because it is handy while the folder has not moved, but it must
    never be the only way back to the file - people move project folders.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select

from drilling_intelligence.core.filesystem import (
    candidate_source_paths,
    file_timestamps,
    has_creation_time_support,
    posix_relative,
)
from drilling_intelligence.core.provenance import Provenance, verify_provenance
from drilling_intelligence.database.models import Document, DocumentVersion, Extraction
from drilling_intelligence.documents.registry import DocumentRegistry
from drilling_intelligence.documents.repository import DocumentRepository
from drilling_intelligence.extraction.registry import build_default_router


@pytest.fixture
def corpus(workspace) -> Path:
    root = workspace.root / "corpus"
    root.mkdir(parents=True, exist_ok=True)
    (root / "field_note.txt").write_text(
        "Well A-3 casing pressure test\n"
        "Date: 2025-06-14\n"
        "Max surface pressure 520 psi at 3105 ft MD.\n"
        "Mud weight 10.2 ppg, EMW 10.9 ppg equivalent.\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def registered(workspace, corpus, ids):
    """One document, registered through the real registry, with its extraction stored."""
    from drilling_intelligence.core.enums import FileChangeKind

    with workspace.database.session() as session:
        registry = build_registry(workspace, session)
        result = registry.register(
            path=corpus / "field_note.txt",
            workspace_root=workspace.root,
            workspace_id=ids[0],
            change=FileChangeKind.NEW,
            well_id=ids[1],
        )
        session.commit()
        assert result.ok, result.error
        return result


def build_registry(workspace, session) -> DocumentRegistry:
    return DocumentRegistry(
        DocumentRepository(session),
        router=build_default_router(workspace.settings),
        settings=workspace.settings,
    )


@pytest.fixture
def ids(workspace):
    from drilling_intelligence.wells.repository import WellRepository

    with workspace.database.session() as session:
        repo = WellRepository(session)
        workspace_row = repo.get_or_create_workspace(str(workspace.root), name="Reprocess Test")
        project = repo.get_or_create_project("Reprocess Test")
        well = repo.create_well("A-3", project_id=project.id)
        session.commit()
        return workspace_row.id, well.id


# --------------------------------------------------------------------------- reprocess hashing
def test_reprocess_uses_the_hash_of_the_file_on_disk(workspace, corpus, registered, ids) -> None:
    """A -> B on disk: the version created by ``reprocess`` must carry B's hash."""
    target = corpus / "field_note.txt"
    target.write_text(
        target.read_text(encoding="utf-8") + "Revised after the pressure test: EMW 11.1 ppg.\n",
        encoding="utf-8",
    )
    expected = hashlib.sha256(target.read_bytes()).hexdigest()

    with workspace.database.session() as session:
        registry = build_registry(workspace, session)
        result = registry.reprocess(registered.document_id, workspace_root=workspace.root)
        session.commit()
        assert result.ok, result.error
        assert result.sha256 == expected, "the registry must record what the file says now"
        assert result.change.value in {"MODIFIED", "UNCHANGED"}
        document = session.get(Document, registered.document_id)
        assert document.sha256 == expected
        version = session.get(DocumentVersion, result.version_id)
        assert version.sha256 == expected
        assert version.version_number == 2
        old = session.scalar(select(DocumentVersion).where(DocumentVersion.version_number == 1))
        assert old.sha256 != expected, "the previous content state stays in history"
        assert (
            version.supersedes_version_id == old.id and old.superseded_by_version_id == version.id
        )


def test_reprocess_never_trusts_the_stored_hash(workspace, corpus, registered) -> None:
    """Same bytes rewritten with different metadata: hashing is what decides, not the DB."""
    target = corpus / "field_note.txt"
    original = target.read_bytes()
    target.write_bytes(original + b"\nappended line\n")
    stale = registered.sha256

    with workspace.database.session() as session:
        registry = build_registry(workspace, session)
        result = registry.reprocess(registered.document_id, workspace_root=workspace.root)
        session.commit()
        assert result.sha256 != stale, "a stored hash is a description of the past, not of the file"
        assert result.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()


def test_reprocess_of_a_missing_source_is_an_error_and_changes_nothing(
    workspace, corpus, registered
) -> None:
    """No file, no version: a reprocess must not record content it could not read."""
    versions_before, hash_before = state_of(workspace, registered.document_id)
    target = corpus / "field_note.txt"
    target.unlink()

    with workspace.database.session() as session:
        registry = build_registry(workspace, session)
        result = registry.reprocess(registered.document_id, workspace_root=workspace.root)
        session.commit()
        assert not result.ok
        assert result.error_code == "SCANNER", result.error
        assert "not reachable" in result.error
    versions_after, hash_after = state_of(workspace, registered.document_id)
    assert (versions_after, hash_after) == (versions_before, hash_before), (
        "a failed reprocess must be a no-op"
    )


def test_reprocess_of_an_unreadable_source_is_reported(workspace, corpus, registered) -> None:
    """A directory where the file was: reported, not recorded as an empty document."""
    target = corpus / "field_note.txt"
    target.unlink()
    target.mkdir()
    try:
        with workspace.database.session() as session:
            registry = build_registry(workspace, session)
            result = registry.reprocess(registered.document_id, workspace_root=workspace.root)
            session.commit()
            assert not result.ok, result.to_dict()
    finally:
        target.rmdir()


def state_of(workspace, document_id: str) -> tuple[int, str]:
    with workspace.database.read_only() as session:
        count = session.scalar(
            select(func.count())
            .select_from(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
        )
        document = session.get(Document, document_id)
        return int(count or 0), document.sha256


# --------------------------------------------------------------------------- durable paths
def test_version_records_a_workspace_relative_reference(workspace, corpus, registered) -> None:
    with workspace.database.read_only() as session:
        document = session.get(Document, registered.document_id)
        version = session.get(DocumentVersion, document.current_version_id)
        assert version.source_relative_path == "corpus/field_note.txt", version.source_relative_path
        assert not Path(version.source_relative_path).is_absolute(), (
            "the durable reference is relative by definition"
        )
        assert version.source_path.endswith("field_note.txt")
        assert Path(version.source_path).is_absolute(), "the convenience reference stays absolute"
        # The extraction artefact keeps the path it was produced from, and the relative
        # form next to it, so the stored document is explainable on its own.
        extraction = session.scalar(
            select(Extraction).where(Extraction.document_version_id == version.id)
        )
        assert extraction.document_json["metadata"]["filename"] == "field_note.txt"
        assert version.metadata_json["relative_path"] == "corpus/field_note.txt"


def test_provenance_still_verifies_after_the_workspace_moves(
    workspace, corpus, registered, tmp_path
) -> None:
    """The test the whole relocation argument rests on: move the folder, cite the file.

    The recorded absolute path is deliberately made false (the folder is renamed), then
    provenance is resolved from the *relative* reference and re-verified against the real
    bytes.  If this works, every citation in the product survives a user moving a project.
    """
    with workspace.database.read_only() as session:
        document = session.get(Document, registered.document_id)
        version = session.get(DocumentVersion, document.current_version_id)
        extraction = session.scalar(
            select(Extraction).where(Extraction.document_version_id == version.id)
        )
        field = (extraction.document_json or {}).get("extracted_fields")[0]
        provenance = Provenance.from_dict(field["provenance"])
        assert provenance.excerpt, "the test needs a real excerpt to verify against"

    # A relocation, not a copy: the folder the absolute path points at is gone.
    relocated = tmp_path / "moved_workspace"
    relocated.mkdir(parents=True)
    shutil.move(str(workspace.root / "corpus"), str(relocated / "corpus"))
    stale = Path(version.source_path)
    assert not stale.exists(), "the recorded absolute path must genuinely be broken"

    with workspace.database.read_only() as session:
        moved_version = session.get(DocumentVersion, version.id)
        moved_document = session.get(Document, document.id)
        repository = DocumentRepository(session)
        resolved = repository.resolve_source_path(
            moved_version, moved_document, workspace_root=workspace.root
        )
        assert resolved is None, "the original really is gone"
        resolved_after_move = repository.resolve_source_path(
            moved_version, moved_document, workspace_root=relocated
        )
        assert resolved_after_move is not None and resolved_after_move.is_file()
        assert resolved_after_move == relocated / "corpus" / "field_note.txt"
        # Full verification, hash included: the bytes are the same, only the location
        # changed - which is precisely the case a path-based-only design would lose.
        assert resolved_after_match(provenance, resolved_after_move) is True
        # And the locator JSON format is untouched: same keys, same ref, same round trip.
        assert field["provenance"]["locator"]["kind"] == "text", field["provenance"]
        assert field["provenance"]["locator"]["line_start"] >= 1
        assert provenance.ref == Provenance.from_dict(field["provenance"]).ref
        assert provenance.filename == "field_note.txt"
        # The old absolute path is genuinely unusable: resolving through it would fail.
        assert verify_provenance(Path(version.source_path), provenance).status == "UNREADABLE"


def resolved_after_match(provenance: Provenance, path: Path) -> bool:
    """Verification against the *current* workspace, using the relocated file."""
    return verify_provenance(path, provenance).ok


def test_candidate_paths_prefer_the_recorded_absolute_then_the_relative(
    workspace, tmp_path
) -> None:
    absolute = tmp_path / "here" / "doc.pdf"
    candidates = candidate_source_paths(
        recorded_path=str(absolute),
        workspace_root=tmp_path / "ws",
        relative_path="docs/doc.pdf",
        filename="doc.pdf",
    )
    assert [str(item) for item in candidates] == [
        str(absolute),
        str(tmp_path / "ws" / "docs/doc.pdf"),
    ]
    # No absolute path recorded (an imported row from another machine): the relative one
    # is still enough, which is what makes old data relocatable.
    fallback = candidate_source_paths(
        recorded_path="", workspace_root=tmp_path / "ws", relative_path="", filename="doc.pdf"
    )
    assert fallback == [tmp_path / "ws" / "doc.pdf"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Windows separators, a ``..`` segment, a duplicated separator, and a path that
        # lives outside the workspace entirely (which degrades to the file name).
        ("docs\\Sub\\DDR.xlsx", "docs/Sub/DDR.xlsx"),
        ("docs/../docs/DDR.xlsx", "docs/DDR.xlsx"),
        ("./docs//sub/../DDR.xlsx", "docs/DDR.xlsx"),
        ("/other/place/DDR.xlsx", "DDR.xlsx"),
    ],
)
def test_relative_paths_are_normalised_but_not_rewritten(raw: str, expected: str) -> None:
    """Separators and ``.``/``..`` collapse; case is preserved - a path is not an id."""
    root = Path("/ws")
    candidate = Path(raw) if Path(raw).is_absolute() else root / raw
    text = posix_relative(candidate, root)
    assert text == expected, text


def test_identity_survives_a_case_insensitive_relocation(workspace) -> None:
    """The relative path keeps case for the human; identity keeps casefold for matching."""
    from drilling_intelligence.core.hashing import filename_identity

    path = workspace.root / "Docs" / "DDR_Rev12.XLSX"
    assert posix_relative(path, workspace.root) == "Docs/DDR_Rev12.XLSX"
    assert filename_identity(path, workspace.root) == "docs/ddr_rev12.xlsx"


def test_timestamps_are_honest_about_creation() -> None:
    """``st_ctime`` is recorded as what it is; a creation time appears only if real."""
    path = Path(__file__)
    timestamps = file_timestamps(path)
    stat = path.stat()
    assert timestamps.modified_at.timestamp() == pytest.approx(stat.st_mtime, abs=1e-6)
    assert timestamps.metadata_changed_at.timestamp() == pytest.approx(stat.st_ctime, abs=1e-6)
    assert timestamps.creation_is_authoritative == has_creation_time_support()
    if not has_creation_time_support():
        assert timestamps.created_at is None, (
            "an inode change time must not be passed off as a creation time"
        )
    payload = timestamps.to_dict()
    assert payload["note"], "the platform semantics have to travel with the numbers"
    assert "created_at" in payload and payload["created_at"] is (payload["created_at"] or None)


def test_registry_stores_creation_only_when_the_platform_has_one(
    workspace, corpus, registered
) -> None:
    with workspace.database.read_only() as session:
        document = session.get(Document, registered.document_id)
        stat = (corpus / "field_note.txt").stat()
        if has_creation_time_support():
            assert document.file_created_at is not None
        else:
            assert document.file_created_at is None, (
                "Linux has no birth time here; NULL is the honest answer"
            )
        # ...but the inode change time is still kept, under its own name.
        assert document.fs_metadata_changed_at is not None
        assert abs(document.fs_metadata_changed_at.timestamp() - stat.st_ctime) < 1.0
        version = session.get(DocumentVersion, document.current_version_id)
        filesystem = version.metadata_json["filesystem"]
        assert filesystem["creation_is_authoritative"] == has_creation_time_support()
        assert "not a creation time" in filesystem["note"] or has_creation_time_support()


def test_filesystem_times_never_become_document_dates(workspace, corpus, registered) -> None:
    """The rule from section 15, asserted on the row that would otherwise break it."""
    with workspace.database.read_only() as session:
        document = session.get(Document, registered.document_id)
        version = session.get(DocumentVersion, document.current_version_id)
        assert version.file_modified_at is not None
        assert document.document_date is None, "no date was stated inside the document"
