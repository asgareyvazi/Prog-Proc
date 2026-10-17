"""Looking a well up by name - the query the CLI and the UI both run.

``WellRepository.find_well`` had a ``.limit()`` applied to the *result* instead of the
statement, which is not a method a ``Result`` has: the first caller that reached it with a
matching row got ``AttributeError: 'ChunkedIteratorResult' object has no attribute 'limit'``
instead of a well.  The CLI's ``--well A-3`` reached it on every unknown name, which is how it
was found.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from drilling_intelligence.wells.repository import WellRepository


@pytest.fixture
def repository(session):
    return WellRepository(session)


def test_find_well_returns_the_row_it_matched(repository, session) -> None:
    workspace = repository.get_or_create_workspace("/data/project", name="North Cormorant")
    project = repository.get_or_create_project("North Cormorant")
    created = repository.create_well("A-3", project_id=project.id)
    session.commit()

    found = repository.find_well("A-3")
    assert found is not None, "a well that exists must be returned, not raise"
    assert found.id == created.id
    assert workspace.root_path == "/data/project"


def test_find_well_of_an_unknown_name_is_none_not_an_error(repository) -> None:
    assert repository.find_well("does-not-exist") is None


def test_find_well_is_deterministic_when_two_projects_share_a_name(repository, session) -> None:
    first = repository.get_or_create_project("Alpha")
    second = repository.get_or_create_project("Beta")
    older = repository.create_well("A-3", project_id=first.id)
    newer = repository.create_well("A-3", project_id=second.id)
    session.commit()

    assert repository.find_well("A-3").id == older.id, "the first well carrying the name answers"
    assert repository.find_well("A-3", project_id=second.id).id == newer.id


def test_a_frozen_clock_still_returns_the_first_well(repository, session, monkeypatch) -> None:
    frozen = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("drilling_intelligence.database.models._utcnow", lambda: frozen)
    first = repository.get_or_create_project("Alpha")
    second = repository.get_or_create_project("Beta")
    older = repository.create_well("A-3", project_id=first.id)
    newer = repository.create_well("A-3", project_id=second.id)
    session.commit()
    assert older.created_at < newer.created_at
    assert repository.find_well("A-3").id == older.id
    assert repository.find_well("A-3", project_id=second.id).id == newer.id
