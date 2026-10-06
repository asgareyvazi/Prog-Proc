"""V7.6 - the analyst question catalog, on the real repositories.

Every question id answers from the certified read paths, the catalog contract is closed and
exact, unknown or ill-scoped questions fail clearly, pack questions pass their packs through
byte-for-byte, and the whole surface is read-only typed JSON with no AI anywhere.
"""

from __future__ import annotations

import json

import pytest

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.intelligence.analyst import (
    ANALYST_SCHEMA,
    QUESTION_CATALOG,
    QUESTION_IDS,
    AnalystIntelligence,
)
from drilling_intelligence.intelligence.comparison import ComparisonIntelligence
from drilling_intelligence.intelligence.decision import DecisionIntelligence
from drilling_intelligence.intelligence.service import IntelligenceService


def _well_scoped_questions() -> list[str]:
    return [
        "well_profile",
        "npt_summary",
        "npt_by_category",
        "problem_summary",
        "problem_recurrence",
        "well_control_summary",
        "hse_summary",
        "plan_actual",
        "cost_summary",
        "risk_summary",
        "learning_summary",
        "pattern_summary",
        "calculation_status",
        "decision_pack",
    ]


# --------------------------------------------------------------------------------------
# The catalog contract
# --------------------------------------------------------------------------------------


def test_the_catalog_is_closed_and_complete():
    assert QUESTION_IDS == (
        "well_profile",
        "npt_summary",
        "npt_by_category",
        "problem_summary",
        "problem_recurrence",
        "well_control_summary",
        "hse_summary",
        "plan_actual",
        "cost_summary",
        "risk_summary",
        "learning_summary",
        "pattern_summary",
        "calculation_status",
        "decision_pack",
        "well_comparison",
        "offset_comparison",
    )
    assert set(QUESTION_CATALOG) == set(QUESTION_IDS)
    for question_id in QUESTION_IDS:
        entry = QUESTION_CATALOG[question_id]
        assert entry["question_id"] == question_id
        for key in ("params", "scope_keys", "output", "evidence", "lifecycle", "missing"):
            assert entry[key] is not None, (question_id, key)
        assert entry["output"], question_id
        assert entry["missing"], question_id


@pytest.mark.parametrize("question", _well_scoped_questions())
def test_every_well_scoped_question_answers_deterministically(session, golden, question):
    service = AnalystIntelligence(session)
    first = service.analyze(question, well_id=golden["a3"])
    second = service.analyze(question, well_id=golden["a3"])
    assert first.schema == ANALYST_SCHEMA
    assert first.question_id == question
    assert len(first.identity) == 64
    assert first.identity == second.identity
    assert first.to_dict() == second.to_dict()
    assert list(first.to_dict()) == [
        "schema",
        "question_id",
        "request",
        "scope",
        "answer",
        "evidence",
        "limitations",
        "freshness",
        "observations",
        "identity",
    ]
    assert first.freshness["question"] == "CURRENT"
    assert first.evidence, "every answer names what backed it"
    assert first.observations
    # no AI-shaped keys anywhere in the payload
    dumped = json.dumps(first.to_dict()).lower()
    for banned in ("embedding", "vector", "confidence", "prompt", "llm"):
        assert banned not in dumped, banned


def test_well_comparison_and_offset_comparison_answer_with_full_packs(session, golden):
    service = AnalystIntelligence(session)
    well_answer = service.analyze(
        "well_comparison", well_ids=[golden["a3"], golden["b11"], golden["c17"]]
    )
    direct = ComparisonIntelligence(session).compare(
        well_ids=[golden["a3"], golden["b11"], golden["c17"]]
    )
    assert well_answer.answer == direct.to_dict()
    assert well_answer.limitations == direct.limitations
    assert (
        well_answer.identity
        == service.analyze(
            "well_comparison", well_ids=[golden["a3"], golden["b11"], golden["c17"]]
        ).identity
    )

    offset_answer = service.analyze("offset_comparison", anchor=golden["a3"])
    direct_offset = ComparisonIntelligence(session).compare(anchor=golden["a3"])
    assert offset_answer.answer == direct_offset.to_dict()
    assert offset_answer.answer["basis"]["kind"] == "offset_candidates"


def test_decision_pack_question_returns_the_pack_unchanged(session, golden):
    answer = AnalystIntelligence(session).analyze("decision_pack", well_id=golden["a3"])
    pack = DecisionIntelligence(session).pack(well_id=golden["a3"])
    assert answer.answer == pack.to_dict()
    assert answer.limitations == pack.limitations
    assert [ref.payload() for ref in answer.evidence] == [ref.payload() for ref in pack.evidence]


def test_operational_questions_agree_with_the_windowed_methods(session, golden):
    from drilling_intelligence.intelligence.field import FieldIntelligence

    service = AnalystIntelligence(session)
    npt = service.analyze("npt_summary", well_id=golden["a3"])
    direct = FieldIntelligence(session).npt(well_id=golden["a3"])
    assert npt.answer["npt"]["rows"] == direct["rows"]
    assert npt.answer["npt"]["total_hours"] == direct["total_hours"]
    assert npt.answer["npt"]["by_category"] == direct["by_category"]
    wc = service.analyze("well_control_summary", well_id=golden["a3"])
    assert wc.answer["well_control"]["events"] == 7
    # project scope is where site-scoped rows legitimately appear, still unattached
    hse = service.analyze("hse_summary", project_id=golden["project_id"])
    direct_hse = FieldIntelligence(session).hse(project_id=golden["project_id"])
    assert hse.answer["hse"]["incidents"] == direct_hse["incidents"]
    assert hse.answer["hse"]["site_scoped_incidents"] == direct_hse["site_scoped_incidents"] > 0
    assert any("never attached" in line for line in hse.observations)
    assert "site_scoped_hse" in hse.limitations


def test_plan_actual_answers_with_the_fold_and_its_diagnoses(session, golden):
    answer = AnalystIntelligence(session).analyze("plan_actual", well_id=golden["a3"])
    assert answer.answer["rows"] > 0
    statuses = answer.answer["by_status"]
    assert statuses, "the fold reports every status it produced"
    assert any(key in statuses for key in ("NO_PLAN", "NO_ACTUAL", "ON_PLAN", "NO_TARGET"))
    assert "missing_plan" in answer.limitations or "missing_actual" in answer.limitations


def test_cost_question_reports_currencies_without_cross_totals(session, golden):
    answer = AnalystIntelligence(session).analyze("cost_summary", well_id=golden["a3"])
    by_currency = answer.answer["summary"]["by_currency"]
    assert set(by_currency) == {"USD", "NOK"}
    assert "mixed_currency" in answer.limitations
    assert any("No total is reported across currencies" in line for line in answer.observations)
    assert answer.evidence, "cost answers carry row-id samples"


def test_risk_learning_pattern_calculation_questions_keep_their_vocabulary(session, golden):
    service = AnalystIntelligence(session)
    risk = service.analyze("risk_summary", well_id=golden["a3"])
    assert risk.answer["current"] == 2
    assert risk.answer["stated"]["severity_unassessed"] == 1
    assert "unassessed_risk" in risk.limitations

    learning = service.analyze("learning_summary", well_id=golden["a3"])
    assert learning.answer["learning"]["lessons"]["approved"] == 1
    assert "recommendations" in learning.answer

    patterns = service.analyze("pattern_summary", well_id=golden["a3"])
    assert patterns.answer["total"] == 2
    assert "stale_pattern" in patterns.limitations

    calcs = service.analyze("calculation_status", well_id=golden["a3"])
    assert calcs.answer["current"] == 1
    assert calcs.answer["history"] == 1
    assert "none executed" in calcs.observations[0]


# --------------------------------------------------------------------------------------
# Refusals: unknown ids, ill-scoped questions, non-windowed windows
# --------------------------------------------------------------------------------------


def test_an_unknown_question_lists_the_real_ids(session, golden):
    service = AnalystIntelligence(session)
    for bad in ("summarise the risks", "What is the best offset?", "NPT", ""):
        with pytest.raises(ValidationError) as excinfo:
            service.analyze(bad, well_id=golden["a3"])
        assert "unknown question" in str(excinfo.value)
        assert "well_profile" in str(excinfo.value)
        assert "offset_comparison" in str(excinfo.value)


def test_scope_refusals_are_specific(session, golden):
    service = AnalystIntelligence(session)
    a3 = golden["a3"]
    with pytest.raises(ValidationError, match="exactly one of well_id"):
        service.analyze("npt_summary")
    with pytest.raises(ValidationError, match="needs well_id"):
        service.analyze("well_profile")
    with pytest.raises(ValidationError, match="exactly one"):
        service.analyze("decision_pack", well_id=a3, field_id=golden["field_id"])
    with pytest.raises(ValidationError, match="at least two well ids"):
        service.analyze("well_comparison", well_ids=[a3])
    with pytest.raises(ValidationError, match="needs an anchor"):
        service.analyze("offset_comparison")
    with pytest.raises(ValidationError, match="does not accept scope"):
        service.analyze("npt_summary", well_id=a3, program_id="prg-1")
    with pytest.raises(ValidationError, match="does not accept a date window"):
        service.analyze("cost_summary", well_id=a3, since="2026-01-01")
    with pytest.raises(ValidationError, match="unknown well"):
        service.analyze("well_profile", well_id="well-does-not-exist")


def test_a_different_question_is_a_different_identity(session, golden):
    service = AnalystIntelligence(session)
    npt = service.analyze("npt_summary", well_id=golden["a3"])
    problems = service.analyze("problem_summary", well_id=golden["a3"])
    assert npt.identity != problems.identity


# --------------------------------------------------------------------------------------
# Service boundary (P48): no sessions/ORM in the public contract
# --------------------------------------------------------------------------------------


def test_service_analyze_returns_the_same_document_as_the_intelligence_object(
    workspace, session, golden
):
    service = IntelligenceService.for_workspace(workspace)
    via_service = service.analyze("npt_summary", well_id=golden["a3"], session=session)
    direct = AnalystIntelligence(session).analyze("npt_summary", well_id=golden["a3"])
    assert via_service.to_dict() == direct.to_dict()
    assert via_service.identity == direct.identity

    via_compare = service.compare(well_ids=[golden["a3"], golden["b11"]], session=session)
    direct_compare = ComparisonIntelligence(session).compare(well_ids=[golden["a3"], golden["b11"]])
    assert via_compare.to_dict() == direct_compare.to_dict()
    # the public contract is plain values: a session never appears in the payload
    dumped = json.dumps(via_compare.to_dict())
    assert "Session" not in dumped


# --------------------------------------------------------------------------------------
# The CLI: names resolve, JSON == service.to_dict(), catalog discoverable, no logic here
# --------------------------------------------------------------------------------------


def test_cli_analyze_json_is_exactly_the_service_document(workspace, corpus):
    from tests.fixtures.fieldops import well_id_for
    from tests.integration.test_cli_domain import call

    from drilling_intelligence.intelligence.analyst import AnalystIntelligence

    a3 = well_id_for(corpus, "A-3")
    payload = call(corpus, "analyze", "--question", "npt_summary", "--well", "A-3")
    with corpus.database.read_only() as session:
        direct = AnalystIntelligence(session).analyze("npt_summary", well_id=a3)
    assert payload == direct.to_dict()
    assert payload["schema"] == "analyst-answer/1"
    assert payload["question_id"] == "npt_summary"


def test_cli_analyze_comparison_questions_end_to_end(workspace, corpus):
    from tests.integration.test_cli_domain import call

    well_payload = call(
        corpus, "analyze", "--question", "well_comparison", "--well", "A-3", "--well", "B-11"
    )
    assert well_payload["answer"]["schema"] == "well-comparison/1"
    offset_payload = call(corpus, "analyze", "--question", "offset_comparison", "--anchor", "A-3")
    assert offset_payload["answer"]["basis"]["kind"] == "offset_candidates"
    pack_payload = call(corpus, "analyze", "--question", "decision_pack", "--well", "A-3")
    assert pack_payload["answer"]["schema"] == "decision-pack/1"


def test_cli_analyze_lists_the_catalog_and_answers_on_an_empty_workspace(workspace):
    from tests.integration.test_cli_domain import _capture, call

    listed = call(workspace, "analyze", "--list-questions")
    assert listed["count"] == 16
    ids = [entry["question_id"] for entry in listed["questions"]]
    assert ids[0] == "well_profile" and ids[-1] == "offset_comparison"
    assert all(entry["missing"] for entry in listed["questions"])
    # on an empty workspace an unknown question still fails with the real ids, not a guess
    code, out, err = _capture(workspace, "analyze", "--question", "how are we doing?")
    assert code != 0, out[:400]
    assert "unknown question" in (err + out)
    assert "well_profile" in (err + out)


def test_cli_analyze_requires_a_question(workspace):
    from tests.integration.test_cli_domain import _capture

    code, out, err = _capture(workspace, "analyze", "--well", "A-3")
    assert code != 0, out[:400]
    assert "question id is required" in (err + out)
