"""The per-layer ``limit`` contract, pinned so the layers cannot drift apart silently.

``limit = 0`` does **not** mean the same thing everywhere in this platform, and it never did: search
resolves ``int(limit or self.default_limit)``, so a zero there means "use the default", while
retrieval, evidence, the domain review and field intelligence all treat zero as "no cap".

That asymmetry is defensible — search is a search box and an unset limit should not become an
unbounded read — but it was undocumented in the direction that misleads: ``evidence/contract.py``
claimed zero-means-no-cap was "the convention the search and intelligence layers use", which was
false of search. These tests exist so that if either layer changes, the contract document and the
behaviour cannot disagree without a failure.

Real workspace, real SQLite, real services; nothing mocked.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from typing import Any

from tests.integration.test_retrieval_forensics import _solo_world

from drilling_intelligence.evidence.contract import EvidenceQuery
from drilling_intelligence.operations.repository import OperationsRepository
from drilling_intelligence.retrieval.contract import RetrievalRequest
from drilling_intelligence.review.contract import DomainReviewRequest

_ROWS = 60
_TERM = "zermatt"


def _world_with_rows(workspace) -> Any:
    """More matching rows than any layer's default, so a default and a cap are distinguishable."""
    world = _solo_world(workspace)
    with workspace.database.session() as session:
        ops = OperationsRepository(session)
        ops.get_or_create_problem_definition(
            "stuck_pipe", name="Stuck pipe", description="zermatt."
        )
        for index in range(_ROWS):
            ops.record_problem(
                well_id=world.ids["well"],
                problem_type="stuck_pipe",
                description=f"{_TERM} occurrence body number {index}.",
            )
        session.commit()
    world.search.rebuild()
    return world


class TestZeroMeansDifferentThingsPerLayer:
    def test_search_treats_zero_as_the_default_not_as_no_cap(self, workspace) -> None:
        """The one layer where zero is not "everything" — asserted, not assumed."""
        world = _world_with_rows(workspace)
        assert world.search.default_limit == 20
        response = world.search.search(_TERM, limit=0)
        assert len(response.results) == world.search.default_limit, (
            "search returned something other than its default for limit=0"
        )
        assert len(response.results) < _ROWS, "the corpus is large enough to make the point"
        # ...and an explicit limit genuinely larger than the default does return more, so the
        # difference is about zero and not about search capping everything at 20.
        assert len(world.search.search(_TERM, limit=_ROWS).results) == _ROWS

    def test_retrieval_treats_zero_as_no_cap(self, workspace) -> None:
        world = _world_with_rows(workspace)
        uncapped = world.retr.retrieve(
            RetrievalRequest(query=_TERM, well_id=world.ids["well"], limit=0)
        )
        assert uncapped.count == _ROWS, (
            f"an uncapped retrieval returned {uncapped.count} of {_ROWS}"
        )
        assert uncapped.discovery_capped is False

    def test_the_documented_defaults_are_the_ones_the_contracts_state(self, workspace) -> None:
        """The defaults in docs/LIMIT_CONTRACTS.md, read off the real dataclasses."""
        assert RetrievalRequest(query="x").limit == 20
        assert EvidenceQuery(topics=("x",)).limit == 0
        assert DomainReviewRequest(well_id="x").limit == 0

    def test_the_cli_search_command_does_not_silently_turn_zero_into_everything(
        self, workspace
    ) -> None:
        """``drillintel search --limit 0`` gets the default, and the JSON must show that count."""
        from drilling_intelligence.cli.app import main

        world = _world_with_rows(workspace)
        out, err = StringIO(), StringIO()
        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = int(
                main(
                    [
                        "search",
                        "--workspace",
                        str(world.ws.root),
                        "zermatt",
                        "--limit",
                        "0",
                        "--json",
                    ]
                )
            )
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err
        assert code == 0, err.getvalue()
        payload = json.loads(out.getvalue())
        assert len(payload["results"]) == world.search.default_limit, (
            "the CLI advertised a limit of 0 and the service answered with its default; if search "
            "ever changes to treat zero as no cap, update docs/LIMIT_CONTRACTS.md with it"
        )


class TestFieldIntelligenceZeroMeansNoCap:
    """Field intelligence follows the no-cap convention, unlike search.

    ``lessons`` resolves ``.limit(limit if limit and limit > 0 else None)`` (``field.py:826``) and
    ``offset_candidates`` slices only when the limit is positive (``:1002``), so a zero removes the
    bound rather than falling back to the 200/10 defaults.
    """

    def test_a_zero_limit_returns_every_lesson_not_the_default(self, workspace) -> None:
        from drilling_intelligence.intelligence.field import FieldIntelligence
        from drilling_intelligence.lessons.repository import LessonRepository

        world = _solo_world(workspace)
        well, field, project = (
            world.ids["well"],
            world.ids["field"],
            world.ids["project"],
        )
        total = 12
        with workspace.database.session() as session:
            lessons = LessonRepository(session)
            for index in range(total):
                lessons.capture(
                    lesson=f"zermatt lesson body number {index}.",
                    title=f"L{index}",
                    well_id=well,
                    field_id=field,
                    project_id=project,
                )
            session.commit()

        # ``approved_only`` defaults to True and filters on status, so it is turned off here: this
        # test is about the limit, not about the approval workflow.
        with workspace.database.session() as session:
            intel = FieldIntelligence(session)
            bounded = intel.lessons(well_id=well, limit=5, approved_only=False)
            default = intel.lessons(well_id=well, approved_only=False)
            unbounded = intel.lessons(well_id=well, limit=0, approved_only=False)

        assert default["count"] == total, default["count"]
        assert bounded["count"] == 5, bounded["count"]
        assert unbounded["count"] == total, (
            f"limit=0 returned {unbounded['count']} of {total}; field intelligence does not treat "
            "zero as no cap"
        )
        assert len(unbounded["lessons"]) == unbounded["count"]
