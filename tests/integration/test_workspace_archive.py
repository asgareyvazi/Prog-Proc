"""Full workspace archive is explicit about scope and restores only after verification."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from drilling_intelligence.cli.app import main
from drilling_intelligence.database.models import IntakeQueueItem
from drilling_intelligence.database.workspace_archive import (
    WorkspaceArchiveError,
    create_workspace_archive,
    restore_workspace_archive,
)
from drilling_intelligence.wells.workspace import Workspace


def _workspace(root: Path) -> Workspace:
    created = Workspace.create(root, name="Archive Test")
    _ = created.database  # Initialise the schema before a headless archive operation.
    created.close()
    return Workspace.open(root)


def test_full_archive_round_trip_reports_exclusions_and_external_references(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = _workspace(root)
    (root / "corpus").mkdir(exist_ok=True)
    (root / "corpus" / "well-log.txt").write_text("source evidence", encoding="utf-8")
    (root / ".env").write_text("PRIVATE_TOKEN=do-not-archive", encoding="utf-8")
    (root / ".env.example").write_text("PRIVATE_TOKEN=replace-me", encoding="utf-8")
    (root / ".drillintel" / "cache").mkdir(exist_ok=True)
    (root / ".drillintel" / "cache" / "stale.bin").write_bytes(b"cache")
    (root / ".drillintel" / "logs").mkdir(exist_ok=True)
    (root / ".drillintel" / "logs" / "runtime.log").write_text("runtime", encoding="utf-8")
    (root / ".drillintel" / "index").mkdir(exist_ok=True)
    (root / ".drillintel" / "index" / "search.db").write_bytes(b"index")
    external_path = tmp_path / "outside" / "source.csv"
    with workspace.database.unit_of_work() as session:
        session.add_all(
            [
                IntakeQueueItem(
                    id="archive-external-source",
                    source_path=str(external_path),
                    filename="source.csv",
                    extension=".csv",
                    size_bytes=8,
                    sha256="a" * 64,
                    enqueued_by="test",
                ),
                IntakeQueueItem(
                    id="archive-url-source",
                    source_path="https://operator:secret@example.invalid/source.csv?token=must-not-leak#fragment",
                    filename="remote.csv",
                    extension=".csv",
                    size_bytes=8,
                    sha256="b" * 64,
                    enqueued_by="test",
                ),
            ]
        )

    archive = tmp_path / "workspace.diwa"
    try:
        result = create_workspace_archive(workspace, archive)
    finally:
        workspace.close()

    assert result.action == "archive"
    assert result.backup_scope == "full_workspace_owned_files"
    assert result.search_index_included is False
    assert result.external_sources_included is False
    assert result.included_files >= 3
    assert result.database_sha256
    assert result.unarchived_source_references == (
        {
            "table": "intake_queue_item",
            "record_id": "archive-external-source",
            "recorded_path": str(external_path),
            "reason": "external source path is not copied by default",
        },
        {
            "table": "intake_queue_item",
            "record_id": "archive-url-source",
            "recorded_path": "https://example.invalid/source.csv",
            "reason": "external source path is not copied by default",
        },
    )

    with zipfile.ZipFile(archive) as zipped:
        names = set(zipped.namelist())
        assert "workspace.toml" in names
        assert ".drillintel/database/drilling_intelligence.db" in names
        assert "corpus/well-log.txt" in names
        assert ".env.example" not in names
        assert ".env" not in names
        assert ".drillintel/cache/stale.bin" not in names
        assert ".drillintel/logs/runtime.log" not in names
        assert ".drillintel/index/search.db" not in names
        manifest_bytes = zipped.read("__drillintel_archive__/manifest.json")
        manifest = json.loads(manifest_bytes)
    assert b"operator:secret@" not in manifest_bytes
    assert b"token=must-not-leak" not in manifest_bytes
    assert b"#fragment" not in manifest_bytes
    assert manifest["backup_scope"] == "full_workspace_owned_files"
    assert manifest["database_sha256"] == result.database_sha256
    assert manifest["external_sources_included"] is False
    assert manifest["unarchived_source_references"] == list(result.unarchived_source_references)
    assert any(item["path"] == ".env" for item in manifest["excluded_items"])
    assert any(item["path"] == ".env.example" for item in manifest["excluded_items"])

    held = tmp_path / "held-workspace"
    root.replace(held)
    restored = restore_workspace_archive(archive, root)
    assert restored.action == "restore"
    assert (root / "corpus" / "well-log.txt").read_text(encoding="utf-8") == "source evidence"
    assert not (root / ".env.example").exists()
    assert not (root / ".env").exists()
    assert not (root / ".drillintel" / "index").exists()
    reopened = Workspace.open(root)
    try:
        assert reopened.config.name == "Archive Test"
        assert reopened.database_path.is_file()
    finally:
        reopened.close()
    assert (held / ".env").is_file()


def test_tampered_full_archive_is_rejected_without_publishing_a_partial_workspace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    workspace = _workspace(root)
    (root / "corpus").mkdir(exist_ok=True)
    (root / "corpus" / "source.txt").write_text("original evidence", encoding="utf-8")
    archive = tmp_path / "workspace.diwa"
    try:
        create_workspace_archive(workspace, archive)
    finally:
        workspace.close()

    tampered = tmp_path / "tampered.diwa"
    with (
        zipfile.ZipFile(archive) as original,
        zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_STORED) as changed,
    ):
        for info in original.infolist():
            content = original.read(info)
            if info.filename == "corpus/source.txt":
                content = b"tampered evidence"
            changed.writestr(info, content)

    root.replace(tmp_path / "held-workspace")
    try:
        restore_workspace_archive(tampered, root)
    except WorkspaceArchiveError as exc:
        assert "hash or size differs" in str(exc)
    else:
        raise AssertionError("restore accepted a member whose content did not match the manifest")
    assert not root.exists()
    assert not list(tmp_path.glob(".drillintel-workspace-restore-*"))


def test_full_archive_cli_emits_machine_readable_scope_and_restores(tmp_path: Path, capsys) -> None:
    root = tmp_path / "workspace"
    workspace = _workspace(root)
    (root / "notes.txt").write_text("operator notes", encoding="utf-8")
    workspace.close()
    archive = tmp_path / "cli.diwa"

    assert (
        main(["workspace", "archive", "--workspace", str(root), "--output", str(archive), "--json"])
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["ok"] is True
    assert created["backup_scope"] == "full_workspace_owned_files"
    assert created["external_sources_included"] is False
    assert created["included_files"] >= 3

    root.rename(tmp_path / "held-workspace")
    assert main(["workspace", "restore-archive", str(archive), str(root), "--json"]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["ok"] is True
    assert restored["action"] == "restore"
    assert (root / "notes.txt").read_text(encoding="utf-8") == "operator notes"


def test_restore_publication_race_preserves_the_concurrent_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    import drilling_intelligence.database.workspace_archive as archive_module

    root = tmp_path / "workspace"
    workspace = _workspace(root)
    archive = tmp_path / "workspace.diwa"
    try:
        create_workspace_archive(workspace, archive)
    finally:
        workspace.close()
    root.rename(tmp_path / "held-workspace")
    sentinel = b"concurrent writer owns this workspace"
    publish = archive_module._publish_directory_no_replace

    def race_publish(staging: Path, destination: Path) -> None:
        Path(destination).mkdir()
        (Path(destination) / "sentinel.txt").write_bytes(sentinel)
        publish(staging, destination)

    monkeypatch.setattr(archive_module, "_publish_directory_no_replace", race_publish)
    try:
        restore_workspace_archive(archive, root)
    except WorkspaceArchiveError as exc:
        assert "appeared during atomic publication" in str(exc)
    else:
        raise AssertionError("restore overwrote a workspace created during publication")
    assert (root / "sentinel.txt").read_bytes() == sentinel
    assert not list(tmp_path.glob(".drillintel-workspace-restore-*"))
