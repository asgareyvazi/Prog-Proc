"""A Windows drive letter must not break Alembic's ConfigParser."""

from __future__ import annotations

from configparser import ConfigParser

import pytest
from sqlalchemy import create_engine

from drilling_intelligence.database.migrations import alembic_config_url


def test_a_rendered_windows_sqlite_url_round_trips_through_configparser() -> None:
    rendered = str(
        create_engine(
            "sqlite:///C:/Users/runneradmin/AppData/Local/Temp/ws/drilling_intelligence.db"
        ).url
    )
    assert "%3A" in rendered
    parser = ConfigParser()
    parser.read_dict({"alembic": {"sqlalchemy.url": alembic_config_url(rendered)}})
    assert parser.get("alembic", "sqlalchemy.url") == rendered


def test_an_unescaped_windows_url_is_the_interpolation_error() -> None:
    rendered = "sqlite:///C%3A/Users/runneradmin/db.db"
    parser = ConfigParser()
    with pytest.raises(ValueError, match="interpolation"):
        parser.read_dict({"alembic": {"sqlalchemy.url": rendered}})


def test_a_rendered_windows_url_is_the_drive_path() -> None:
    from drilling_intelligence.database.engine import sqlite_file_path

    rendered = str(
        create_engine(
            "sqlite:///C:/Users/runneradmin/AppData/Local/Temp/ws/drilling_intelligence.db"
        ).url
    )
    assert sqlite_file_path(rendered) == (
        "C:/Users/runneradmin/AppData/Local/Temp/ws/drilling_intelligence.db"
    )


def test_a_url_without_percent_is_unchanged() -> None:
    url = "sqlite:////opt/workspace/drilling_intelligence.db"
    assert alembic_config_url(url) == url
