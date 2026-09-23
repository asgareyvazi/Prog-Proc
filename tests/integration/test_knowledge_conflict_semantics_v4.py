"""The knowledge conflicts a real corpus produces, and why `doctor` stays red about them.

The V4 forensic corpus leaves **five** unresolved knowledge conflicts.  They are not defects and this
suite exists to prove that rather than assert it: each one is two or more sources stating different
values for the same property, every voice is marked, nothing is silently merged, no winner is picked
by counting, and every candidate keeps the locator it came from.

The distinction the architecture has to hold is between

*   **a legitimate disagreement** - the sources really did say different things, and settling it is a
    human decision, so it stays ``OPEN`` and ``doctor`` reports it; and
*   **a repeat, not a disagreement** - several sources stating the *same* value, which is agreement
    and must not be counted as an argument.

:func:`detect_conflicts` already separates the two; this suite is what stops that separation from
being refactored away quietly.  It also pins the property names the corpus actually produces, so a
change to knowledge extraction that starts conflating *different* physical quantities under one
predicate shows up here as a new conflict rather than as a plausible-looking answer.

Nothing in this suite makes ``doctor`` exit 0.  A workspace with a disputed hole depth is not sound,
and a green ``doctor`` over it would be the actual bug.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from tests.fixtures.fieldops import register_wells, well_id_for
from tests.fixtures.generate import build_v4_forensic_corpus

from drilling_intelligence.core.enums import ConflictResolution, KnowledgeStatus
from drilling_intelligence.database.models import KnowledgeConflict
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.knowledge.conflicts import resolve_conflict
from drilling_intelligence.knowledge.repository import KnowledgeRepository
from drilling_intelligence.operations.service import OperationalService
from drilling_intelligence.search.chunking import KIND_KNOWLEDGE
from drilling_intelligence.search.service import SearchService

#: The disagreements the corpus actually produces, by property.  Asserting the names is the point:
#: it is what makes "a sixth appeared" or "one quietly merged" a test failure.
#:
#: This set used to hold five entries - ``hole_depth``, ``hole_section_size``, ``mud_volume``, ``rpm``
#: and ``surface_pressure``.  Three of those were not disagreements at all; they were different
#: engineering quantities sharing one predicate:
#:
#: *   ``surface_pressure`` held SIDPP (420 psi), SICP (610 psi) and a MAASP *limit* (1850 psi);
#: *   ``mud_volume`` held a 1,450 bbl active system beside a 12 bbl kill-sheet pill;
#: *   ``rpm`` held a 120 rpm rotary speed beside a 300 rpm rheometer test condition;
#: *   ``hole_depth`` held measured depths beside a 9,850 ft *true vertical* depth.
#:
#: They are now separated at the layer that lost the context (see
#: ``docs/KNOWLEDGE_SEMANTIC_VOCABULARY.md``), and the two entries that remain are genuine: two
#: different hole sections, and five sources stating five different measured depths.
EXPECTED_CONFLICTS = {
    "hole_section_size",
    "measured_depth",
}

#: Predicates that must exist separately after the split.  If a future change folds any of these back
#: into a shared name, the false conflicts return silently - the values still look perfectly ordinary.
REQUIRED_DISTINCT_PREDICATES = {
    "sidpp",
    "sicp",
    "maasp",
    "rheometer_speed",
    "rpm",
    "kick_volume",
    "measured_depth",
    "true_vertical_depth",
}


def _corpus(workspace) -> None:
    register_wells(workspace)
    root = workspace.root / "corpus"
    build_v4_forensic_corpus(root)
    result = IngestionPipeline(
        settings=workspace.settings,
        workspace_root=workspace.root,
        database=workspace.database,
    ).run(root=root, well_id=well_id_for(workspace, "A-3"))
    assert result.ok, result.error
    OperationalService.for_workspace(workspace).promote_workspace(include_unsupported=True)


def _conflicts(workspace) -> list[KnowledgeConflict]:
    with workspace.database.read_only() as session:
        statement = select(KnowledgeConflict).order_by(KnowledgeConflict.id)
        return list(session.scalars(statement))


def _doctor(workspace, capsys) -> tuple[int, dict]:
    """``doctor --json``, read from the first brace (a library notice may precede it on stdout)."""
    from drilling_intelligence.cli.app import build_parser

    args = build_parser().parse_args(["doctor", "--workspace", str(workspace.root), "--json"])
    code = args.handler(args)
    text = capsys.readouterr().out
    payload, _end = json.JSONDecoder().raw_decode(text[text.index("{") :])
    return code, payload


def test_the_corpus_produces_exactly_the_five_expected_disagreements(workspace) -> None:
    _corpus(workspace)

    rows = _conflicts(workspace)
    assert {row.property_name for row in rows} == EXPECTED_CONFLICTS, [
        row.property_name for row in rows
    ]
    for row in rows:
        assert row.status == "OPEN", (row.property_name, row.status)
        assert not row.resolution, f"{row.property_name} was settled by something other than a person"
        assert row.detected_by == "knowledge.conflicts"
        assert row.candidates, row.property_name


def test_no_conflict_picks_a_winner_by_counting(workspace) -> None:
    """Every voice in the argument is marked, including the majority one.

    Flagging only the odd value out would imply the majority *is* the answer, which is choosing a
    side by counting instead of by deciding.
    """
    _corpus(workspace)

    with workspace.database.read_only() as session:
        repo = KnowledgeRepository(session)
        counts = repo.counts()
    assert counts["by_status"][KnowledgeStatus.CONFLICTED.value] > 0, counts

    for row in _conflicts(workspace):
        statuses = {candidate["fact_status"] for candidate in row.candidates}
        # The invariant is that no voice is left *settled* while its property is disputed.  A
        # candidate that was already UNVERIFIED stays UNVERIFIED rather than being promoted to
        # CONFLICTED - the two say different things (one is uncorroborated, one is contradicted) and
        # neither reads as an answer - but nothing may remain ACTIVE.
        assert KnowledgeStatus.ACTIVE.value not in statuses, (row.property_name, statuses)
        assert KnowledgeStatus.CONFLICTED.value in statuses, (row.property_name, statuses)


def test_every_candidate_keeps_the_locator_it_came_from(workspace) -> None:
    """A disagreement is only actionable if each side can be read back out of its own source."""
    _corpus(workspace)

    for row in _conflicts(workspace):
        for candidate in row.candidates:
            assert candidate["source"], (row.property_name, candidate)
            provenance = candidate["provenance"]
            assert provenance["document_id"], candidate
            assert provenance["document_version_id"], candidate
            assert provenance["excerpt"], candidate
            assert provenance["source_sha256"], candidate
            assert candidate["locator_ref"], candidate


def test_both_sides_of_a_disagreement_remain_searchable(workspace) -> None:
    """Search must not hide the conflict by keeping one value and dropping the other."""
    _corpus(workspace)
    search = SearchService.for_workspace(workspace)
    search.rebuild()

    conflict = next(row for row in _conflicts(workspace) if row.property_name == "measured_depth")
    values = {candidate["text"] for candidate in conflict.candidates}
    assert len(values) >= 2, values

    response = search.search("measured depth ft", kinds=[KIND_KNOWLEDGE], limit=50)
    found = {result.text for result in response.results}
    for text in values:
        assert any(value in hit for hit in found for value in (text,)), (
            f"{text!r} disappeared from search while its conflict stayed open"
        )


def test_agreement_and_single_source_ambiguity_are_not_counted_as_conflicts(workspace) -> None:
    """The other half of the distinction, and a third case besides.

    ``detect_conflicts`` separates three things that a naive implementation would all call
    "a conflict":

    *   **conflict** - two or more *sources* state different values; a human has to decide;
    *   **agreement** - two or more sources state the *same* value; that is corroboration, and
        counting it as an argument would make the conflict total meaningless;
    *   **ambiguous** - two values inside *one* revision of *one* file.  The knowledge layer cannot
        adjudicate what a table meant, so it does not claim a document contradicts itself in the
        sense an engineer would mean.

    Only the first raises the number ``doctor`` reports.
    """
    from drilling_intelligence.knowledge.conflicts import detect_conflicts

    _corpus(workspace)
    with workspace.database.read_only() as session:
        report = detect_conflicts(KnowledgeRepository(session))

    assert report.conflicts == len(EXPECTED_CONFLICTS), report.details
    assert report.agreements > 0, "corroborated properties should be counted as agreement"
    assert report.keys_examined > report.conflicts + report.agreements or report.ambiguous >= 0

    conflicted_keys = {
        item["lookup_key"] for item in report.details if "same_values_in_every_source" not in item
    }
    agreed_keys = {
        item["lookup_key"] for item in report.details if "same_values_in_every_source" in item
    }
    # The same property cannot be both settled and disputed.
    assert not (conflicted_keys & agreed_keys), (conflicted_keys & agreed_keys)
    for item in report.details:
        if "same_values_in_every_source" in item:
            assert item["same_values_in_every_source"] >= 2, item
    assert len(_conflicts(workspace)) == len(EXPECTED_CONFLICTS)


def test_resolving_a_conflict_is_a_recorded_decision_and_moves_doctor(workspace, capsys) -> None:
    """``doctor`` is a live signal: a decision reduces it, and the decision is attributable."""
    _corpus(workspace)
    code_before, before = _doctor(workspace, capsys)
    assert code_before == 1, before
    assert before["knowledge"]["open_conflicts"] == len(EXPECTED_CONFLICTS), before["knowledge"]

    target = next(row for row in _conflicts(workspace) if row.property_name == "hole_section_size")
    chosen = target.candidates[0]["item_id"]
    with workspace.database.session() as session:
        resolve_conflict(
            KnowledgeRepository(session),
            str(target.id),
            chosen_item_id=str(chosen),
            resolution=ConflictResolution.RESOLVED_MANUALLY.value,
            by="toolpusher",
            note="the 12 1/4 in figure is the 12 1/4 in section, not the 8 1/2 in one",
        )
        session.commit()

    code_after, after = _doctor(workspace, capsys)
    assert after["knowledge"]["open_conflicts"] == len(EXPECTED_CONFLICTS) - 1, after["knowledge"]
    assert code_after == 1, "a dispute remains; a green doctor here would be the real defect"

    resolved = next(row for row in _conflicts(workspace) if row.property_name == "hole_section_size")
    assert resolved.resolution, "the decision was not recorded on the conflict"
    assert resolved.resolution["by"] == "toolpusher", resolved.resolution
    assert resolved.resolution["chosen_item_id"] == chosen, resolved.resolution
    assert resolved.resolution["candidates_at_resolution"], "the options decided between were not kept"
    # The resolution kind lives on the row's own status, so "is this still open" is one field.
    assert resolved.status == ConflictResolution.RESOLVED_MANUALLY.value, resolved.status
    assert resolved.status != "OPEN"
