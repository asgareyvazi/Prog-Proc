"""The review workbench's *logic* layer, certified without Qt.

``ReviewController`` imports no Qt at all - only settings, errors, services, repositories and the
workspace - so everything that actually decides what the workbench shows and what it may do is
testable in the default, headless suite.  The widget layer in ``tests/ui`` remains optional and
skips truthfully where the host has no Qt; this module is deliberately **not** marked ``ui``,
because making default CI depend on a GUI would be the wrong trade.

What is certified here is the guarantee that matters most about a review surface: that looking at
data does not change it.  Every read is bracketed by a fingerprint of every row in every table,
including migration metadata.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import inspect

from drilling_intelligence.core.errors import ValidationError, WorkspaceError
from drilling_intelligence.ui.controller import ReviewController


def _database_fingerprint(workspace) -> tuple[Any, ...]:
    """Every row of every table, so any write - however incidental - is visible."""
    names = sorted(inspect(workspace.database.engine).get_table_names())
    with workspace.database.engine.connect() as connection:
        return tuple(
            (
                name,
                tuple(
                    tuple(str(value) for value in row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{name}" ORDER BY rowid'  # noqa: S608
                    ).fetchall()
                ),
            )
            for name in names
        )


@pytest.fixture
def reviewed(workspace):
    """A workspace with ingested and promoted data, plus a controller opened on it."""
    from tests.fixtures.fieldops import ingest, promote, well_id_for

    ingest(workspace)
    promote(workspace)
    controller = ReviewController()
    info = controller.open_workspace(workspace.root, config_path=workspace.settings.source_path)
    assert info, "the controller must open the workspace it was handed"
    return controller, well_id_for(workspace, "A-3")


def test_opening_the_controller_does_not_import_qt() -> None:
    """The headless guarantee, stated where it can actually fail.

    If the controller ever grew a Qt import, every CLI and API user would start paying for a GUI
    toolkit they never asked for, and the optional extra would stop being optional.
    """
    import sys

    before = set(sys.modules)
    import drilling_intelligence.ui.controller  # noqa: F401

    newly = {name for name in set(sys.modules) - before if "PySide" in name}
    assert not newly, f"importing the controller pulled in Qt: {sorted(newly)}"


def test_reading_the_whole_review_surface_changes_nothing(reviewed, workspace) -> None:
    """Wells, current view, history view and per-record actions: all reads, zero writes.

    This is the property a presentation layer breaks most easily - a lazy save, a cache write, an
    implicit "mark as seen".  The fingerprint covers every table, so nothing hides.
    """
    controller, well_id = reviewed
    before = _database_fingerprint(workspace)

    choices = controller.list_wells()
    assert any(choice.well_id == well_id for choice in choices), choices
    assert controller.resolve_well("A-3") == well_id

    current = controller.load_review(well_id, lifecycle="current")
    history = controller.load_review(well_id, lifecycle="history")
    assert current.records or current.conflicts, "the promoted corpus must yield a review surface"
    # Current and history are different *questions*.  On this corpus nothing has been superseded,
    # so they may legitimately return the same records - what must not happen is the lifecycle
    # being dropped, which would make "history" a synonym for "current" without saying so.
    assert current.request["lifecycle"] == "current", current.request
    assert history.request["lifecycle"] == "history", history.request

    for record in list(current.records)[:5]:
        controller.available_actions(record)
    for conflict in list(current.conflicts)[:5]:
        controller.available_actions(conflict)

    assert _database_fingerprint(workspace) == before, (
        "reading the review surface wrote to the database"
    )


def test_a_citation_verifying_read_is_also_read_only(reviewed, workspace) -> None:
    """``verify_citations`` reaches into evidence, which makes it the likeliest read to write."""
    controller, well_id = reviewed
    before = _database_fingerprint(workspace)

    review = controller.load_review(well_id, lifecycle="current", verify_citations=True)
    assert review is not None

    assert _database_fingerprint(workspace) == before, "verifying citations wrote to the database"


def test_an_unknown_well_is_an_explicit_error_not_an_empty_view(reviewed) -> None:
    """A review surface that silently shows nothing for a typo would be worse than one that objects."""
    controller, _well_id = reviewed

    with pytest.raises((ValidationError, WorkspaceError, KeyError, LookupError)) as excinfo:
        controller.load_review("no-such-well", lifecycle="current")
    assert str(excinfo.value), "the refusal has to say something"


def test_a_bad_lifecycle_is_refused_rather_than_defaulted(reviewed) -> None:
    """``lifecycle`` is a contract, not a hint: an invented value must not quietly mean 'current'."""
    controller, well_id = reviewed

    with pytest.raises((ValidationError, ValueError, KeyError)):
        controller.load_review(well_id, lifecycle="whatever-the-user-typed")


def test_a_controller_with_no_workspace_open_says_so_instead_of_guessing() -> None:
    """Using a controller before opening a workspace is a programming error, and must be loud."""
    controller = ReviewController()

    with pytest.raises((WorkspaceError, ValidationError, AttributeError, RuntimeError)):
        controller.list_wells()


def test_the_decision_pack_reads_through_the_controller_and_writes_nothing(
    reviewed, workspace
) -> None:
    """The workbench's decision view is the service's pack, and viewing it changes nothing."""
    controller, well_id = reviewed
    before = _database_fingerprint(workspace)

    payload = controller.decision(well_id)

    assert payload["schema"].startswith("decision-pack/")
    assert payload["subject"]["kind"] == "well"
    for section in (
        "execution",
        "operations",
        "economics",
        "risk",
        "learning",
        "recommendations",
        "patterns",
        "calculations",
        "evidence",
        "limitations",
        "freshness",
        "observations",
    ):
        assert section in payload, section
    assert isinstance(payload["limitations"], list)
    assert payload["identity"], "a rendered pack carries its content identity"

    assert _database_fingerprint(workspace) == before, (
        "building the decision pack through the controller wrote to the database"
    )


def test_the_decision_read_is_refused_without_a_workspace_like_every_other_read() -> None:
    controller = ReviewController()
    with pytest.raises((WorkspaceError, ValidationError, AttributeError, RuntimeError)):
        controller.decision("any-well")


def test_the_comparison_pack_reads_through_the_controller_and_writes_nothing(
    reviewed, workspace
) -> None:
    """The workbench's comparison view is the service's pack, and viewing it changes nothing."""
    controller, a3 = reviewed
    b11 = controller.resolve_well("B-11")
    before = _database_fingerprint(workspace)

    payload = controller.compare([a3, b11])

    assert payload["schema"] == "well-comparison/1"
    assert len(payload["basis"]["subjects"]) == 2
    assert payload["basis"]["kind"] == "explicit_wells"
    for section in (
        "schema",
        "request",
        "basis",
        "sections",
        "evidence",
        "limitations",
        "freshness",
        "observations",
        "summary",
        "identity",
    ):
        assert section in payload, section
    assert payload["identity"], "a rendered pack carries its content identity"
    again = controller.compare([a3, b11])
    assert again == payload, "the same request over the same state is the same document"

    assert _database_fingerprint(workspace) == before, (
        "building the comparison pack through the controller wrote to the database"
    )


def test_the_controller_compare_refuses_one_well_like_every_other_read(reviewed) -> None:
    controller, a3 = reviewed
    with pytest.raises(ValidationError):
        controller.compare([a3])
    with pytest.raises(ValidationError):
        controller.compare([])


def test_the_controller_compare_is_refused_without_a_workspace_like_every_other_read() -> None:
    controller = ReviewController()
    with pytest.raises((WorkspaceError, ValidationError, AttributeError, RuntimeError)):
        controller.compare(["well-a", "well-b"])
