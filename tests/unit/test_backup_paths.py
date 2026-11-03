"""Backup archives accept only portable relative paths, not host-specific or traversing paths."""

from __future__ import annotations

import pytest

from drilling_intelligence.database.backup import BackupError, _safe_relative_path


@pytest.mark.parametrize(
    "candidate",
    [
        "../outside.db",
        "database/../../outside.db",
        "/var/lib/outside.db",
        "C:/outside.db",
        "D:outside.db",
        "//server/share/outside.db",
        "database\\outside.db",
    ],
)
def test_backup_path_validation_rejects_nonportable_or_escaping_paths(candidate: str) -> None:
    with pytest.raises(BackupError):
        _safe_relative_path(candidate, label="database_path", default="database.sqlite")


def test_backup_path_validation_keeps_safe_relative_path() -> None:
    result = _safe_relative_path(
        "database/drilling_intelligence.db",
        label="database_path",
        default="database.sqlite",
    )

    assert result.as_posix() == "database/drilling_intelligence.db"
