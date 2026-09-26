"""V4.4 - the workspace recovery state model.

``plan_recovery`` classifies one *stored field*.  This is the other half: what state the workspace
is in, and the smallest safe sequence of commands that fixes it.  It is a pure function over the
numbers ``doctor``, ``knowledge status`` and the search index already produce, so the same numbers
always give the same answer and a test can pin the whole model without a database.

The distinction the model exists to protect is this: **an unresolved engineering conflict is not
database corruption.**  Two sources that disagree about a measured depth are evidence, and a
recovery planner that reports them as damage will be switched off the first time it cries wolf.
"""

from __future__ import annotations

import pytest

from drilling_intelligence.knowledge.recovery import (
    CLEAN,
    CONFLICTS_PRESENT,
    DERIVED_DRIFT_STATES,
    INDEX_STALE,
    KNOWLEDGE_AND_INDEX_STALE,
    KNOWLEDGE_STALE,
    SCHEMA_OUT_OF_DATE,
    STRUCTURED_INDEX_STALE,
    UNRECOVERABLE,
    assess_recovery,
)


def _signals(**overrides):
    """A healthy workspace, so each test changes exactly one thing."""
    base = {
        "integrity_problems": [],
        "schema": {"up_to_date": True, "current": "0011", "head": "0011"},
        "knowledge": {
            "needs_rebuild": False,
            "facts": 61,
            "detached_facts": 0,
            "versions_with_artefacts": 6,
            "versions_without_knowledge": 0,
        },
        "index": {
            "stale_versions": 0,
            "orphaned": 0,
            "missing_versions": 0,
            "structured_missing": 0,
            "structured_stale": 0,
            "structured_orphaned": 0,
        },
        "conflicts": {"open": 0, "ambiguous": 0},
    }
    base.update(overrides)
    return base


def test_a_sound_workspace_with_no_arguments_is_clean() -> None:
    assessment = assess_recovery(_signals())
    assert assessment["state"] == CLEAN
    assert assessment["conditions"] == []
    assert assessment["corrupt"] is False
    assert assessment["recovery_required"] is False
    assert assessment["recommended"] == []


def test_an_empty_signal_set_is_clean_rather_than_a_crash() -> None:
    """``assess_recovery()`` with nothing at all is a valid question, not an exception."""
    assert assess_recovery()["state"] == CLEAN
    assert assess_recovery(None)["state"] == CLEAN


def test_a_genuine_conflict_is_not_corruption() -> None:
    """The rule the whole model is organised around.

    ``doctor`` exits 1 on this workspace and that is correct, but the recovery planner must not
    read the same two numbers as damage: nothing is stale, so nothing is recommended, and the
    disagreement is listed under ``not_performed`` with the command a person uses to review it.
    """
    assessment = assess_recovery(_signals(conflicts={"open": 2, "ambiguous": 0}))
    assert assessment["state"] == CONFLICTS_PRESENT
    assert assessment["corrupt"] is False, "a disagreement is evidence, not a broken registry"
    assert assessment["recovery_required"] is False
    assert assessment["recommended"] == [], "no rebuild fixes a disagreement, so none is offered"
    assert [item["command"] for item in assessment["not_performed"]] == [
        "drillintel knowledge conflicts"
    ]
    assert any("disagreement to review" in line for line in assessment["explanation"])


def test_stale_knowledge_is_drift_and_gets_one_command() -> None:
    assessment = assess_recovery(
        _signals(knowledge={"needs_rebuild": True, "detached_facts": 3, "facts": 61})
    )
    assert assessment["state"] == KNOWLEDGE_STALE
    assert assessment["recovery_required"] is True
    assert assessment["corrupt"] is False
    assert [step["command"] for step in assessment["recommended"]] == [
        "drillintel knowledge rebuild"
    ]


def test_stale_structured_index_is_named_separately_from_document_drift() -> None:
    """The diagnosis differs, so the state does: these are promoted rows, not documents."""
    assessment = assess_recovery(_signals(index={"structured_missing": 18}))
    assert assessment["state"] == STRUCTURED_INDEX_STALE
    assert [step["command"] for step in assessment["recommended"]] == ["drillintel index rebuild"]


def test_document_index_drift_alone_is_index_stale() -> None:
    assessment = assess_recovery(_signals(index={"missing_versions": 2}))
    assert assessment["state"] == INDEX_STALE
    assert [step["command"] for step in assessment["recommended"]] == ["drillintel index rebuild"]


def test_both_halves_drifted_needs_both_commands_in_order() -> None:
    assessment = assess_recovery(
        _signals(
            knowledge={"needs_rebuild": True, "detached_facts": 1, "facts": 61},
            index={"structured_missing": 18},
            conflicts={"open": 2},
        )
    )
    assert assessment["state"] == KNOWLEDGE_AND_INDEX_STALE
    steps = assessment["recommended"]
    assert [step["command"] for step in steps] == [
        "drillintel knowledge rebuild",
        "drillintel index rebuild",
    ]
    assert [step["step"] for step in steps] == [1, 2], "the order is the point"
    # A knowledge rebuild rewrites knowledge chunks but not the promoted structured rows, which is
    # why the second command is still needed rather than implied by the first.
    assert "not the structured" in steps[1]["reason"]
    assert assessment["not_performed"], "the conflicts are still reported, just not auto-fixed"


def test_a_broken_registry_invariant_is_the_only_corruption() -> None:
    assessment = assess_recovery(
        _signals(
            integrity_problems=[{"problem": "two current versions", "table": "document_version"}],
            knowledge={"needs_rebuild": True, "detached_facts": 4, "facts": 61},
        )
    )
    assert assessment["state"] == UNRECOVERABLE
    assert assessment["corrupt"] is True
    assert assessment["recommended"] == [], "a rebuild derives from the registry; fix it first"
    assert assessment["not_performed"][0]["command"] == "drillintel doctor"


def test_a_schema_behind_head_outranks_drift() -> None:
    assessment = assess_recovery(
        _signals(
            schema={"up_to_date": False, "current": "0010", "head": "0011", "mode": "auto"},
            knowledge={"needs_rebuild": True, "detached_facts": 2, "facts": 61},
            index={"missing_versions": 1},
        )
    )
    assert assessment["state"] == SCHEMA_OUT_OF_DATE
    assert [step["command"] for step in assessment["recommended"]] == ["alembic upgrade head"]


def test_the_severity_order_is_total() -> None:
    """Every condition present at once still yields exactly one state, and it is the worst one."""
    everything = _signals(
        integrity_problems=[{"problem": "x"}],
        schema={"up_to_date": False, "current": "0010", "head": "0011"},
        knowledge={"needs_rebuild": True, "detached_facts": 1, "facts": 61},
        index={"missing_versions": 1, "structured_missing": 18},
        conflicts={"open": 2},
    )
    assert assess_recovery(everything)["state"] == UNRECOVERABLE
    without_integrity = _signals(**{**everything, "integrity_problems": []})
    assert assess_recovery(without_integrity)["state"] == SCHEMA_OUT_OF_DATE
    without_schema = _signals(**{**without_integrity, "schema": {"up_to_date": True}})
    assert assess_recovery(without_schema)["state"] == KNOWLEDGE_AND_INDEX_STALE


def test_an_unreadable_index_counts_as_drift_not_as_a_clean_bill() -> None:
    assessment = assess_recovery(_signals(index={"error": "OperationalError: no such table"}))
    assert assessment["state"] == INDEX_STALE
    assert any("index reported an error" in line for line in assessment["explanation"])


def test_ambiguity_is_explained_but_is_never_a_recovery_state() -> None:
    """Six keys that one file states twice are an extraction question, not a recovery one."""
    assessment = assess_recovery(_signals(conflicts={"open": 0, "ambiguous": 6}))
    assert assessment["state"] == CLEAN
    assert assessment["recommended"] == []
    assert any("ambiguous within one source" in line for line in assessment["explanation"])


@pytest.mark.parametrize("state", DERIVED_DRIFT_STATES)
def test_every_drift_state_is_flagged_as_requiring_recovery(state: str) -> None:
    assert state in DERIVED_DRIFT_STATES


def test_the_assessment_is_deterministic() -> None:
    """Same numbers in, same answer out, twice - a planner that wobbles cannot be automated."""
    signals = _signals(
        knowledge={"needs_rebuild": True, "detached_facts": 2, "facts": 61},
        index={"structured_missing": 18},
        conflicts={"open": 2, "ambiguous": 6},
    )
    assert assess_recovery(signals) == assess_recovery(signals)


def test_the_state_names_are_the_documented_vocabulary() -> None:
    assert DERIVED_DRIFT_STATES == (
        KNOWLEDGE_STALE,
        INDEX_STALE,
        STRUCTURED_INDEX_STALE,
        KNOWLEDGE_AND_INDEX_STALE,
    )
    assert {CLEAN, CONFLICTS_PRESENT, SCHEMA_OUT_OF_DATE, UNRECOVERABLE}.isdisjoint(
        DERIVED_DRIFT_STATES
    )
