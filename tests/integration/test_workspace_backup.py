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
    assert backup_result.backup_scope == "database_snapshot_only"
    assert backup_result.corpus_included is False
    assert backup_result.search_index_included is False
    assert (
        root.joinpath(".drillintel/database/drilling_intelligence.db").read_bytes()
        == source_bytes_before
    )
    with zipfile.ZipFile(backup) as archive:
        assert set(archive.namelist()) == {"database.sqlite", "workspace.toml", "manifest.json"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["backup_scope"] == "database_snapshot_only"
        assert manifest["included"] == ["workspace.toml", "database.sqlite"]
        assert manifest["omitted"] == [
            "configured corpus/source files",
            "rebuildable search index",
        ]

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


def test_backup_publication_race_does_not_overwrite_destination(
    tmp_path: Path, monkeypatch
) -> None:
    import drilling_intelligence.database.backup as backup_module

    workspace = _populated_workspace(tmp_path / "workspace")
    destination = tmp_path / "racing.dibak"
    sentinel = b"racing writer owns this backup path"
    real_link = backup_module.os.link

    def race_publish(source, target):
        if Path(target) == destination:
            destination.write_bytes(sentinel)
        return real_link(source, target)

    monkeypatch.setattr(backup_module.os, "link", race_publish)
    try:
        with pytest.raises(BackupError, match="appeared during creation"):
            create_workspace_backup(workspace, destination)
        assert destination.read_bytes() == sentinel
    finally:
        workspace.close()


def test_backup_publication_permission_failure_does_not_create_archive(
    tmp_path: Path, monkeypatch
) -> None:
    import drilling_intelligence.database.backup as backup_module

    workspace = _populated_workspace(tmp_path / "workspace")
    destination = tmp_path / "denied.dibak"

    def refuse_publish(_source, _target):
        raise PermissionError("injected publication permission failure")

    monkeypatch.setattr(backup_module.os, "link", refuse_publish)
    try:
        with pytest.raises(BackupError, match="cannot safely publish backup"):
            create_workspace_backup(workspace, destination)
        assert not destination.exists()
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


def test_cli_human_output_calls_backup_database_only(tmp_path: Path, capsys) -> None:
    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    workspace.close()
    backup = tmp_path / "workspace.dibak"

    assert main(["workspace", "backup", "--workspace", str(root), "--output", str(backup)]) == 0
    backup_output = capsys.readouterr().out
    assert "database snapshot verified" in backup_output
    assert "NOT a full workspace/source backup" in backup_output
    assert "configured corpus/source files" in backup_output

    root.replace(tmp_path / "held-workspace")
    assert main(["workspace", "restore", str(backup), str(root)]) == 0
    restore_output = capsys.readouterr().out
    assert "database snapshot restored and verified" in restore_output
    assert "source files were not restored" in restore_output


def _rewrite_archive(source: Path, destination: Path, *, manifest_update=None, database_info=None):
    with zipfile.ZipFile(source, "r") as original:
        contents = {name: original.read(name) for name in original.namelist()}
    if manifest_update is not None:
        manifest = json.loads(contents["manifest.json"])
        manifest_update(manifest)
        contents["manifest.json"] = json.dumps(manifest).encode("utf-8")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("database.sqlite", "workspace.toml", "manifest.json"):
            info = database_info if name == "database.sqlite" and database_info else name
            archive.writestr(info, contents[name])


def test_restore_rejects_manifest_claiming_full_workspace_coverage(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    backup = tmp_path / "workspace.dibak"
    try:
        create_workspace_backup(workspace, backup)
    finally:
        workspace.close()
    false_full = tmp_path / "false-full.dibak"
    _rewrite_archive(
        backup,
        false_full,
        manifest_update=lambda manifest: manifest.update(backup_scope="full_workspace"),
    )

    with pytest.raises(BackupError, match="unsupported backup scope"):
        restore_workspace_backup(false_full, tmp_path / "restore-target")

    assert not (tmp_path / "restore-target").exists()


def test_restore_rejects_archives_with_more_than_three_members(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    backup = tmp_path / "workspace.dibak"
    try:
        create_workspace_backup(workspace, backup)
    finally:
        workspace.close()
    expanded = tmp_path / "expanded.dibak"
    with zipfile.ZipFile(backup) as original, zipfile.ZipFile(expanded, "w") as output:
        for name in original.namelist():
            output.writestr(name, original.read(name))
        output.writestr("extra.bin", b"unexpected archive member")

    with pytest.raises(BackupError, match="three-member entry limit"):
        restore_workspace_backup(expanded, tmp_path / "restore-target")

    assert not (tmp_path / "restore-target").exists()


def test_restore_rejects_symlink_archive_member(tmp_path: Path) -> None:
    import stat

    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    backup = tmp_path / "workspace.dibak"
    try:
        create_workspace_backup(workspace, backup)
    finally:
        workspace.close()
    symlink_archive = tmp_path / "symlink-member.dibak"
    link_info = zipfile.ZipInfo("database.sqlite")
    link_info.create_system = 3
    link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
    _rewrite_archive(backup, symlink_archive, database_info=link_info)

    with pytest.raises(BackupError, match="non-regular member"):
        restore_workspace_backup(symlink_archive, tmp_path / "restore-target")

    assert not (tmp_path / "restore-target").exists()


def test_restore_rejects_database_member_over_the_resource_limit(
    tmp_path: Path, monkeypatch
) -> None:
    import drilling_intelligence.database.backup as backup_module

    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    backup = tmp_path / "workspace.dibak"
    try:
        create_workspace_backup(workspace, backup)
    finally:
        workspace.close()
    monkeypatch.setattr(backup_module, "_MAX_DATABASE_BYTES", 64)

    with pytest.raises(BackupError, match="8 GiB restore limit"):
        restore_workspace_backup(backup, tmp_path / "restore-target")

    assert not (tmp_path / "restore-target").exists()


def test_restore_marker_is_published_last_and_failed_restore_is_not_a_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    import drilling_intelligence.database.backup as backup_module

    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    backup = tmp_path / "workspace.dibak"
    try:
        create_workspace_backup(workspace, backup)
    finally:
        workspace.close()
    held = tmp_path / "held-workspace"
    root.replace(held)
    real_link = backup_module.os.link

    def fail_marker(source, destination):
        if Path(destination).name == "workspace.toml":
            raise PermissionError("injected marker publication failure")
        return real_link(source, destination)

    monkeypatch.setattr(backup_module.os, "link", fail_marker)
    with pytest.raises(BackupError, match="restore was interrupted"):
        restore_workspace_backup(backup, root)

    assert (root / ".drillintel/database/drilling_intelligence.db").is_file()
    assert not (root / "workspace.toml").exists()
    assert (held / "workspace.toml").is_file()


def test_restore_destination_race_does_not_overwrite_a_database_file(
    tmp_path: Path, monkeypatch
) -> None:
    import drilling_intelligence.database.backup as backup_module

    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    backup = tmp_path / "workspace.dibak"
    try:
        create_workspace_backup(workspace, backup)
    finally:
        workspace.close()
    held = tmp_path / "held-workspace"
    root.replace(held)
    real_link = backup_module.os.link
    sentinel = b"racing writer owns this path"

    def race_database(source, destination):
        if Path(destination).name == "drilling_intelligence.db":
            Path(destination).write_bytes(sentinel)
        return real_link(source, destination)

    monkeypatch.setattr(backup_module.os, "link", race_database)
    with pytest.raises(BackupError, match="refusing to overwrite existing data"):
        restore_workspace_backup(backup, root)

    assert (root / ".drillintel/database/drilling_intelligence.db").read_bytes() == sentinel
    assert not (root / "workspace.toml").exists()
    assert (held / "workspace.toml").is_file()


def test_restore_rejects_traversing_archive_member_name(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = _populated_workspace(root)
    backup = tmp_path / "workspace.dibak"
    try:
        create_workspace_backup(workspace, backup)
    finally:
        workspace.close()
    traversing = tmp_path / "traversing.dibak"
    with zipfile.ZipFile(backup, "r") as original:
        database = original.read("database.sqlite")
        marker = original.read("workspace.toml")
        manifest = original.read("manifest.json")
    with zipfile.ZipFile(traversing, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../database.sqlite", database)
        archive.writestr("workspace.toml", marker)
        archive.writestr("manifest.json", manifest)

    with pytest.raises(BackupError, match="exactly one manifest"):
        restore_workspace_backup(traversing, tmp_path / "restore-target")

    assert not (tmp_path / "database.sqlite").exists()
    assert not (tmp_path / "restore-target").exists()
