"""Cost lines: stated figures, kept per currency, and no arithmetic nobody asked for.

The promises these tests hold the repository to are the ones that keep a money number honest:

*   re-reading the same line does not double-count it, and the second call says so by returning the row it
    already had (``created`` is ``False``);
*   totals never cross currencies, because there is no rate in the database and an implied one would be a
    fiction;
*   an unpriced line is counted as unpriced rather than as zero;
*   a category the vocabulary does not know is kept in the source's own words, not folded into ``other``;
*   and ``npt_id`` is the only link a cost row needs - no duplicate edge in the relation table.
"""

from __future__ import annotations

import pytest

from drilling_intelligence.core.enums import ConfirmationStatus
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import CostItem, KnowledgeRelation
from drilling_intelligence.engineering.costs import CostRepository, currency_of
from drilling_intelligence.wells.repository import WellRepository

EXCERPT = [
    {
        "kind": "spreadsheet",
        "document": {"sheet": "AFE", "cell": "F12", "page": 1},
        "excerpt": "2nd round bit and reamer: 148,000 USD",
        "method": "manual",
    }
]


@pytest.fixture
def hierarchy(session):
    """A field and a well in it, so a cost line can be reached by either."""
    repository = WellRepository(session)
    repository.get_or_create_workspace(str(session.bind.url.database or "."), name="Cost Test")
    project = repository.get_or_create_project("Cost Test")
    field = repository.get_or_create_field("Cost Test", project=project)
    well = repository.create_well("A-3", project_id=project.id, field_id=field.id)
    return {"project": project, "field": field, "well": well}


def test_the_currency_of_a_unit_is_the_unit_without_its_case() -> None:
    assert currency_of(" usd ") == "USD" and currency_of("Nok") == "NOK"
    assert currency_of(None) == "USD", "the stored default, not an invention"
    assert currency_of("USD/t") == "USD/T", "a rate is not a currency, and is not quietly truncated"


def test_a_line_is_stated_once(hierarchy, session) -> None:
    repository = CostRepository(session)
    row, created = repository.record_item(
        description="2nd round bit and reamer",
        planned_value=148_000.0,
        actual_value=151_240.0,
        wbs_code="1.2.3",
        cbs_code="DRIL.BIT",
        cbs_path="DRIL.BIT",
        well_id=hierarchy["well"].id,
        provenance=EXCERPT,
    )
    session.flush()
    assert (
        created and row.planned_unit == "USD" and row.status == ConfirmationStatus.CANDIDATE.value
    )
    again, created_again = repository.record_item(
        description="2nd round bit and reamer - as re-read",
        planned_value=148_000.0,
        actual_value=151_240.0,
        wbs_code="1.2.3",
        cbs_code="DRIL.BIT",
        cbs_path="DRIL.BIT",
        well_id=hierarchy["well"].id,
    )
    session.flush()
    assert not created_again and again.id == row.id, "the same figures are the same line"
    assert again.description == row.description, "the wording a caller re-used is not an update"
    assert len(session.query(CostItem).all()) == 1


def test_an_unattributed_line_is_reported_as_unattributed(session) -> None:
    repository = CostRepository(session)
    repository.record_item(description="sourced", planned_value=10.0, provenance=EXCERPT)
    repository.record_item(description="unsourced", planned_value=20.0)
    session.flush()
    numbers = repository.counts()
    assert numbers["total"] == 2 and numbers["by_status"] == {"CANDIDATE": 2}
    summary = repository.summary()
    assert summary["items"] == 2 and summary["unattributed"] == 1


def test_figures_in_two_currencies_are_never_added(hierarchy, session) -> None:
    repository = CostRepository(session)
    repository.record_item(
        description="day rate",
        planned_value=1000.0,
        planned_unit="usd",
        actual_value=1100.0,
        actual_unit="USD",
        well_id=hierarchy["well"].id,
    )
    repository.record_item(
        description="offshore allowance",
        planned_value=9000.0,
        planned_unit="NOK",
        well_id=hierarchy["well"].id,
    )
    session.flush()
    summary = repository.summary(field_id=hierarchy["field"].id)
    assert summary["mixed_currency"] is True and summary["currencies"] == ["NOK", "USD"]
    assert summary["by_currency"]["USD"]["planned"] == 1000.0
    assert summary["by_currency"]["USD"]["actual"] == 1100.0
    assert summary["by_currency"]["USD"]["variance"] == 100.0
    # A currency with nothing actual yet has no variance - "not comparable yet", not "0".
    assert summary["by_currency"]["NOK"]["variance"] is None
    assert summary["by_currency"]["NOK"]["actual"] == 0.0
    assert summary["by_currency"]["NOK"]["actual_lines"] == 0
    assert summary["items"] == 2 and summary["priced"] == 2 and summary["unpriced"] == 0
    # The field scope reached a row that only named a well, through the well - not through a copy.
    assert summary["scope"]["field_id"] == hierarchy["field"].id


def test_a_line_stated_in_two_currencies_is_a_question_not_a_total(session) -> None:
    repository = CostRepository(session)
    repository.record_item(
        description="rental invoiced in NOK, budgeted in USD",
        planned_value=100.0,
        planned_unit="USD",
        actual_value=950.0,
        actual_unit="NOK",
    )
    session.flush()
    summary = repository.summary()
    assert summary["mixed_currency_lines"] == 1
    assert summary["by_currency"]["USD"]["planned"] == 100.0
    assert summary["by_currency"]["NOK"]["actual"] == 950.0


def test_an_unpriced_line_is_counted_not_zeroed(session) -> None:
    repository = CostRepository(session)
    repository.record_item(description="awaiting the invoice", wbs_code="1.3")
    repository.record_item(description="priced", planned_value=5.0, wbs_code="1.3")
    session.flush()
    summary = repository.summary()
    assert summary["items"] == 2 and summary["unpriced"] == 1 and summary["priced"] == 1
    assert summary["by_state"] == {"CURRENT": 2}
    # The priced line contributes its 5.0; the unpriced one contributes nothing at all - not a 0.0 that
    # would make the well look cheaper by rounding a blank into a figure.
    assert summary["currencies"] == ["USD"] and summary["mixed_currency"] is False
    assert summary["by_currency"]["USD"]["planned"] == 5.0
    assert summary["by_currency"]["USD"]["planned_lines"] == 1
    assert summary["by_currency"]["USD"]["actual_lines"] == 0


def test_the_structure_codes_are_what_group_a_cost_breakdown(session) -> None:
    repository = CostRepository(session)
    for code, path, planned in (
        ("1.1", "DRIL.BIT", 10.0),
        ("1.1", "DRIL.BIT", 15.0),
        ("1.2", "DRIL.TRIP", 40.0),
    ):
        repository.record_item(
            description=f"line {code} {path}",
            wbs_code=code,
            cbs_code=path,
            cbs_path=path,
            planned_value=planned,
        )
    session.flush()
    by_wbs = repository.rollup(by="wbs")
    assert [(row["code"], row["currency"], row["planned"]) for row in by_wbs] == [
        ("1.1", "USD", 25.0),
        ("1.2", "USD", 40.0),
    ]
    assert {row["lines"] for row in by_wbs} == {1, 2}
    assert by_wbs[0]["variance"] is None, "no actuals stated, so no variance claimed"
    by_path = repository.rollup(by="cbs_path")
    assert [row["code"] for row in by_path] == ["DRIL.BIT", "DRIL.TRIP"]
    with pytest.raises(ValidationError, match="roll costs up by"):
        repository.rollup(by="vibes")


def test_a_category_the_vocabulary_does_not_know_keeps_its_words(session) -> None:
    repository = CostRepository(session)
    known, _ = repository.record_item(description="lost time", category="NPT", planned_value=1.0)
    unknown, _ = repository.record_item(
        description="mobilisation", category="RIG MOVE", planned_value=2.0
    )
    session.flush()
    assert known.category == "npt_recovery", "the vocabulary's own token, not the sheet's spelling"
    assert unknown.category == "rig_move"
    assert unknown.attributes["source_wording"]["category"] == "RIG MOVE"
    assert set(repository.summary()["by_category"]) == {"npt_recovery", "rig_move"}


def test_a_cost_line_is_a_record_a_person_can_confirm(session) -> None:
    repository = CostRepository(session)
    row, _ = repository.record_item(description="contingency draw", planned_value=3.0)
    session.flush()
    with pytest.raises(ValidationError, match="without an author"):
        repository.set_status(row.id, ConfirmationStatus.CONFIRMED.value)
    confirmed = repository.set_status(row.id, ConfirmationStatus.CONFIRMED.value, by="k.adeyemi")
    session.flush()
    assert confirmed.status == ConfirmationStatus.CONFIRMED.value
    assert confirmed.attributes["status_history"][-1]["by"] == "k.adeyemi"


def test_the_npt_link_is_a_column_and_not_a_second_truth(hierarchy, session) -> None:
    from drilling_intelligence.operations.repository import OperationsRepository

    repository = CostRepository(session)
    row, _ = repository.record_item(description="bit consumed in the hang", planned_value=30.0)
    npt = OperationsRepository(session).record_npt(
        well_id=hierarchy["well"].id, description="stuck at 9 940 ft MD", duration_hours=6.5
    )
    session.flush()
    linked = repository.link_to_npt(row.id, npt.id)
    session.flush()
    assert linked.npt_id == npt.id
    assert linked.identity_key, "the identity moved with the link, so the pair is not counted twice"
    assert (
        session.query(KnowledgeRelation)
        .filter(KnowledgeRelation.source_type == "cost_item")
        .count()
        == 0
    )
    with pytest.raises(ValidationError, match="needs an npt_id"):
        repository.link_to_npt(row.id, "")


def test_a_cost_row_needs_a_description_and_a_known_scope(session) -> None:
    repository = CostRepository(session)
    with pytest.raises(ValidationError, match="needs a description"):
        repository.record_item(description="   ")
    with pytest.raises(ValidationError, match="unknown cost scope"):
        repository.record_item(description="misfiled", rig_id="ncf-1")
    with pytest.raises(ValidationError, match="no cost item"):
        repository.get("cost-nope")
    with pytest.raises(ValidationError, match="unknown cost scope"):
        repository.summary(rig_id="ncf-1")
