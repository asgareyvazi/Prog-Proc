"""Workspace backup/restore preserves SQLite evidence and never overwrites a workspace."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from drilling_intelligence.cli.app import main
from drilling_intelligence.database.backup import (
    BackupError,
    create_workspace_backup,
    restore_workspace_backup,
)
from drilling_intelligence.database.models import Well
from drilling_intelligence.wells.repository import WellRepository
from drilling_intelligence.wells.workspace import Workspace


def _populated_workspace(root: Path) -> Workspace:
    workspace = Workspace.create(root, name="Backup Test")
    database = workspace.database
    with database.unit_of_work() as session:
        repository = WellRepository(session)
        repository.get_or_create_workspace(str(root.resolve()), name=workspace.config.name)
        project = repository.get_or_create_project("Backup Project")
        repository.create_well("A-3", project_id=project.id)
    workspace.close()
    return Workspace.open(root)


def test_backup_and_restore_round_trip_to_original_workspace_identity(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    backup = tmp_path / "workspace.dibak"
    source_bytes_before = workspace.database_path.read_bytes()

    backup_result = create_workspace_backup(workspace, backup)
    workspace.close()

    assert backup_result.action == "backup"
    assert backup_result.storage_health["status"] == "healthy"
    assert backup_result.schema_revision == "0026"
    assert backup_result.database_sha256
    assert backup_result.archive_sha256
    assert backup_result.corpus_included is False
    assert backup_result.search_index_included is False
    assert (
        root.joinpath(".drillintel/database/drilling_intelligence.db").read_bytes()
        == source_bytes_before
    )
    with zipfile.ZipFile(backup) as archive:
        assert set(archive.namelist()) == {"database.sqlite", "workspace.toml", "manifest.json"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["included"] == ["workspace.toml", "database.sqlite"]
        assert manifest["omitted"] == ["corpus files", "rebuildable search index"]

    # Restore into the original path only after that path is absent. This is a test-owned
    # temporary workspace; production restore never removes or replaces an existing directory.
    held = tmp_path / "held-workspace"
    root.replace(held)
    restore_result = restore_workspace_backup(backup, root)

    assert restore_result.action == "restore"
    assert restore_result.storage_health["status"] == "healthy"
    assert (root / "workspace.toml").is_file()
    restored = Workspace.open(root)
    try:
        with restored.database.read_only() as session:
            well = session.query(Well).filter_by(name="A-3").one()
            assert well.id
        assert restored.database_path.is_file()
    finally:
        restored.close()
    assert (held / "workspace.toml").is_file()


def test_backup_refuses_existing_destination_without_overwriting_it(tmp_path: Path) -> None:
    workspace = _populated_workspace(tmp_path / "workspace")
    destination = tmp_path / "existing.dibak"
    destination.write_bytes(b"user-owned bytes")
    before = destination.read_bytes()
    try:
        with pytest.raises(BackupError, match="refusing to overwrite"):
            create_workspace_backup(workspace, destination)
        assert destination.read_bytes() == before
    finally:
        workspace.close()


def test_restore_refuses_an_existing_directory_and_preserves_its_files(tmp_path: Path) -> None:
    workspace = _populated_workspace(tmp_path / "workspace")
    backup = tmp_path / "workspace.dibak"
    try:
        create_workspace_backup(workspace, backup)
    finally:
        workspace.close()
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")

    with pytest.raises(BackupError, match="refusing to modify"):
        restore_workspace_backup(backup, destination)

    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_restore_refuses_rehoming_when_the_database_records_another_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    backup = tmp_path / "workspace.dibak"
    try:
        create_workspace_backup(workspace, backup)
    finally:
        workspace.close()
    destination = tmp_path / "different-location"

    with pytest.raises(BackupError, match="re-homing would invalidate workspace identity"):
        restore_workspace_backup(backup, destination)

    assert not destination.exists()


def test_unhealthy_database_is_preserved_but_the_archive_cannot_be_restored(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    connection = sqlite3.connect(workspace.database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE backup_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE backup_child (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER REFERENCES backup_parent(id)
            );
            PRAGMA foreign_keys=OFF;
            INSERT INTO backup_child (id, parent_id) VALUES (1, 404);
            """
        )
        connection.commit()
    finally:
        connection.close()
    backup = tmp_path / "unhealthy.dibak"
    try:
        result = create_workspace_backup(workspace, backup)
    finally:
        workspace.close()

    assert backup.is_file()
    assert result.storage_health["status"] == "failed"
    assert result.storage_health["foreign_key_violation_count"] == 1
    with pytest.raises(BackupError, match="failed its recorded SQLite storage health checks"):
        restore_workspace_backup(backup, tmp_path / "quarantine")
    assert not (tmp_path / "quarantine").exists()


def test_restore_rejects_database_bytes_that_do_not_match_the_manifest(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    backup = tmp_path / "workspace.dibak"
    try:
        create_workspace_backup(workspace, backup)
    finally:
        workspace.close()
    held = tmp_path / "held-workspace"
    root.replace(held)
    tampered = tmp_path / "tampered.dibak"
    with zipfile.ZipFile(backup) as original:
        marker = original.read("workspace.toml")
        manifest = original.read("manifest.json")
        database = bytearray(original.read("database.sqlite"))
    database[-1] ^= 1
    with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("database.sqlite", database)
        archive.writestr("workspace.toml", marker)
        archive.writestr("manifest.json", manifest)

    with pytest.raises(BackupError, match="database bytes do not match"):
        restore_workspace_backup(tampered, root)

    assert not root.exists()
    assert (held / "workspace.toml").is_file()


def test_cli_backup_and_restore_publish_machine_readable_results(tmp_path: Path, capsys) -> None:
    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    workspace.close()
    backup = tmp_path / "workspace.dibak"

    backup_code = main(
        [
            "workspace",
            "backup",
            "--workspace",
            str(root),
            "--output",
            str(backup),
            "--json",
        ]
    )
    backup_payload = json.loads(capsys.readouterr().out)

    assert backup_code == 0
    assert backup_payload["ok"] is True
    assert backup_payload["action"] == "backup"
    assert backup_payload["storage_health"]["status"] == "healthy"

    root.replace(tmp_path / "held-workspace")
    restore_code = main(["workspace", "restore", str(backup), str(root), "--json"])
    restore_payload = json.loads(capsys.readouterr().out)

    assert restore_code == 0
    assert restore_payload["ok"] is True
    assert restore_payload["action"] == "restore"
    assert restore_payload["path"] == str(root)
