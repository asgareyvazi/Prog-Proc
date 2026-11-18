"""Workspace marker serialization preserves TOML string boundaries."""

from __future__ import annotations

import tomllib

from drilling_intelligence.wells.workspace import Workspace, WorkspaceConfig


def test_workspace_config_round_trips_quotes_unicode_and_control_characters() -> None:
    config = WorkspaceConfig(
        name='North "Cormorant"\\A',
        description="first line\nsecond line\twith tab",
        project_code='PRJ="quoted"',
        created_by="operator\\reviewer",
        database_path="database/a\\b.db",
        index_database_path="index/搜索.db",
        corpus_dirs=["corpus/東京", 'corpus/quote"folder'],
        ignore_patterns=["~$*.xlsx", "literal\\pattern", "line\nbreak"],
    )

    parsed = tomllib.loads(config.to_toml())

    assert parsed["name"] == config.name
    assert parsed["description"] == config.description
    assert parsed["project_code"] == config.project_code
    assert parsed["created_by"] == config.created_by
    assert parsed["database_path"] == config.database_path
    assert parsed["index_database_path"] == config.index_database_path
    assert parsed["corpus_dirs"] == config.corpus_dirs
    assert parsed["ignore_patterns"] == config.ignore_patterns


def test_workspace_with_quoted_name_reopens_from_its_persisted_marker(tmp_path) -> None:
    root = tmp_path / "workspace"
    name = 'Well "A-3" — North\\Cormorant'

    workspace = Workspace.create(root, name=name)
    workspace.close()

    reopened = Workspace.open(root)
    try:
        assert reopened.config.name == name
        assert tomllib.loads((root / "workspace.toml").read_text(encoding="utf-8"))["name"] == name
    finally:
        reopened.close()
