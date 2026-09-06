"""Conflicts: two sources saying different things, and neither one quietly winning.

This is the behaviour the whole knowledge layer is judged by.  A platform that picks the newer
number, or the one from the more respectable document, is a platform whose answers cannot be
argued with - so detection here *stores both*, marks both, records why one ranks higher, and waits
for a person to decide.  The tests are written against a real database and real artefacts for the
same reason: the promise is about what the rows say, not about what a function returns.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from tests.fixtures.knowledge import artefact_field, register_artefact

from drilling_intelligence.core.enums import ConflictResolution, KnowledgeStatus
from drilling_intelligence.database.models import (
    Document,
    DocumentVersion,
    KnowledgeConflict,
    KnowledgeItem,
)
from drilling_intelligence.knowledge.conflicts import (
    ABS_TOLERANCE,
    REL_TOLERANCE,
    detect_conflicts,
    resolve_conflict,
    values_agree,
)
from drilling_intelligence.knowledge.repository import KnowledgeRepository
from drilling_intelligence.knowledge.service import KnowledgeExtractionService


@pytest.fixture
def service(db) -> KnowledgeExtractionService:
    return KnowledgeExtractionService(database=db, index=None, refresh_index=False)


def mud_report(
    session,
    well,
    *,
    value: str,
    filename: str,
    revision: str = "Rev 1",
    date: dt.datetime | None = None,
):
    return register_artefact(
        session,
        filename=filename,
        identity_path=f"docs/{filename}",
        well_id=well.id,
        document_date=date,
        revision=revision,
        fields=(artefact_field("mud_weight", value, "ppg", filename=filename),),
    )


# --------------------------------------------------------------------------- the comparison itself
def test_values_agree_within_a_tolerance_and_not_beyond_it() -> None:
    class Row:
        """The columns the comparison reads - nothing else about a row is consulted."""

        def __init__(self, value, unit, text="", raw=None):
            self.value = value
            self.unit = unit
            self.content = text
            self.original_value = text if raw is None else raw

    assert values_agree(Row(10.2, "ppg"), Row(10.2, "ppg"))
    assert values_agree(Row(10.2, "ppg"), Row(10.2 + REL_TOLERANCE / 2, "ppg"))
    assert not values_agree(Row(10.2, "ppg"), Row(10.3, "ppg"))
    # A text fact agrees only with the same text: there is no fuzzy match on wording, because
    # "A-3" and "A-3 " are the same statement while "A-3" and "A-4" must never collide.
    assert values_agree(Row(None, "", "A-3"), Row(None, "", " a-3 "))
    assert not values_agree(Row(None, "", "A-3"), Row(None, "", "A-4"))
    assert values_agree(Row(1.0, "sg"), Row(1.0 + ABS_TOLERANCE / 2, "sg"))
    # A number on one side and wording on the other is a unit error waiting to be seen, not an
    # agreement - so this stays False even though neither side has anything else in common.
    assert not values_agree(Row(10.2, "ppg"), Row(None, "", "10.2 ppg"))


def test_a_cross_unit_comparison_uses_the_stated_precision() -> None:
    """The rule that keeps a metric sheet off the conflict list, and keeps a real difference on it.

    A tolerance is a decision, so it is written down here as a test: same unit, exact to float
    noise; different units, judged at the precision the coarser source wrote.  ``1222 kg/m3`` is
    how a metric report writes 10.2 ppg, and a platform that called that a conflict would be
    reporting a unit conversion instead of a disagreement.
    """

    class Row:
        def __init__(self, value, unit, raw):
            self.value = value
            self.unit = unit
            self.content = ""
            self.original_value = raw

    assert values_agree(Row(10.2, "ppg", "10.2"), Row(1222.0, "kg/m3", "1222"))
    assert values_agree(Row(1222.23, "kg/m3", "1222.23"), Row(10.2, "ppg", "10.2 ppg"))
    # 1250 kg/m3 is 10.43 ppg: a 2.3 % gap, wider than any rounding of "10.2", so it is a dispute.
    assert not values_agree(Row(10.2, "ppg", "10.2"), Row(1250.0, "kg/m3", "1250"))
    # The same unit leaves no room for rounding: 10.2 and 10.21 ppg are different statements.
    assert not values_agree(Row(10.2, "ppg", "10.2"), Row(10.21, "ppg", "10.21"))
    # A value quoted to one significant figure cannot swallow 3 %, even in another unit.
    assert not values_agree(Row(10.0, "ppg", "10"), Row(1250.0, "kg/m3", "1250"))
    assert values_agree(Row(10.0, "ppg", "10"), Row(1200.0, "kg/m3", "1200"))


def test_two_units_for_one_property_agree_after_conversion(session, well, service) -> None:
    """A metric sheet writing "1222 kg/m3" and a mud log writing "10.2 ppg" are the same mud.

    This is the test that keeps conflict detection from becoming a unit-format detector: two
    documents written on different scales must not be reported as a disagreement, because the
    engineering answer is the same and a user reading "conflict" would be misled twice - about the
    value and about the units.
    """
    mud_report(session, well, value="10.2", filename="mud.xlsx")
    register_artefact(
        session,
        filename="density.xlsx",
        identity_path="docs/density.xlsx",
        well_id=well.id,
        fields=(artefact_field("mud_weight", "1222", "kg/m3", filename="density.xlsx"),),
    )
    for pair in _pairs(session):
        service.sync_version(*pair, session=session)
    repository = KnowledgeRepository(session)
    report = detect_conflicts(repository)
    assert report.conflicts == 0, report.to_dict()
    assert report.agreements >= 1
    statuses = {row.status for row in session.execute(select(KnowledgeItem)).scalars()}
    assert statuses == {KnowledgeStatus.ACTIVE.value}
    units = {row.unit for row in session.execute(select(KnowledgeItem)).scalars()}
    assert units == {"ppg", "kg/m3"}, "each side keeps the unit the source wrote it in"


def _pairs(session) -> list[tuple[str, str]]:
    """Every (document, current version) pair, oldest id first, for a deterministic sync order."""
    rows = list(
        session.execute(
            select(DocumentVersion.document_id, DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentVersion.is_current.is_(True))
            .order_by(Document.filename, DocumentVersion.version_number)
        ).all()
    )
    return [(str(document_id), str(version_id)) for document_id, version_id in rows]


def test_a_disagreement_between_sources_is_stored_not_settled(session, well, service) -> None:
    mud_report(session, well, value="10.2", filename="mud.xlsx")
    mud_report(session, well, value="10.4", filename="check.xlsx")
    for pair in _pairs(session):
        service.sync_version(*pair, session=session)
    repository = KnowledgeRepository(session)
    assert repository.counts()["by_status"][KnowledgeStatus.CONFLICTED.value] == 2
    rows = list(session.execute(select(KnowledgeItem).order_by(KnowledgeItem.id)).scalars())
    assert {row.status for row in rows} == {KnowledgeStatus.CONFLICTED.value}, (
        "both sides are marked"
    )
    assert {row.original_value for row in rows} == {"10.2", "10.4"}, "neither value was overwritten"
    conflicts = list(session.execute(select(KnowledgeConflict)).scalars())
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.status == ConflictResolution.OPEN.value
    assert conflict.well_id == well.id
    assert conflict.property_name == "mud_weight"
    assert conflict.compare_unit == "ppg"
    assert conflict.note == "2 different values stated by 2 sources"
    assert len(conflict.candidates) == 2
    assert [entry["authority_rank"] for entry in conflict.candidates] == sorted(
        entry["authority_rank"] for entry in conflict.candidates
    )
    assert {entry["item_id"] for entry in conflict.candidates} == {str(row.id) for row in rows}
    # Both sides keep their own citation, so the argument can be opened from either.
    for entry in conflict.candidates:
        assert entry["source"] in {"mud.xlsx", "check.xlsx"}
        assert entry["locator_ref"], "a candidate without a location is not reviewable"
        assert entry["authority_tier"]


def test_the_more_authoritative_source_ranks_first_and_says_so(session, well, service) -> None:
    """Ranking is reported, never applied - the order is information, the choice is not."""
    mud_report(session, well, value="10.2", filename="report.xlsx", revision="Rev 1")
    register_artefact(
        session,
        filename="program.pdf",
        identity_path="docs/program.pdf",
        classification="DDR",
        source_authority="approved_drilling_program",
        well_id=well.id,
        fields=(artefact_field("mud_weight", "10.9", "ppg", filename="program.pdf"),),
    )
    for pair in _pairs(session):
        service.sync_version(*pair, session=session)
    conflict = session.execute(select(KnowledgeConflict)).scalars().one()
    ranked = [entry["authority_rank"] for entry in conflict.candidates]
    assert ranked == sorted(ranked), (
        "the ranking is recorded on every candidate, so the order is auditable"
    )
    tiers = [entry["authority_tier"] for entry in conflict.candidates]
    assert "approved_drilling_program" in tiers
    assert tiers[0] == "approved_drilling_program", (
        "the tier that ranks first is the first candidate"
    )


def test_a_plan_and_an_observation_do_not_argue(session, well, service) -> None:
    """The record-state half of the key is what keeps the conflict list worth reading.

    A program that plans 12.0 ppg and a report that recorded 10.2 ppg are both true statements; a
    platform that reported that as a conflict would be reporting that plans differ from reality.
    """
    register_artefact(
        session,
        filename="program.pdf",
        identity_path="docs/program.pdf",
        classification="DRILLING_PROGRAM",
        well_id=well.id,
        fields=(artefact_field("mud_weight", "12.0", "ppg", filename="program.pdf"),),
    )
    mud_report(session, well, value="10.2", filename="mud.xlsx")
    for pair in _pairs(session):
        service.sync_version(*pair, session=session)
    states = {row.record_state for row in session.execute(select(KnowledgeItem)).scalars()}
    assert states == {"PLANNED", "ACTUAL"}
    assert session.execute(select(KnowledgeConflict)).scalars().first() is None
    report = detect_conflicts(KnowledgeRepository(session))
    assert report.conflicts == 0, report.to_dict()


def test_several_values_inside_one_revision_are_ambiguity_not_a_dispute(
    session, well, service
) -> None:
    """One file stating a thing twice is a question for the extractor, not an argument between sources.

    A depth per row of a table is the common case; calling it a conflict would put a parse detail in
    front of the person who is trying to settle what the well actually did.
    """
    register_artefact(
        session,
        filename="ddr.docx",
        identity_path="docs/ddr.docx",
        classification="DDR",
        well_id=well.id,
        fields=(
            artefact_field("measured_depth", "9780", "ft", cell="B4"),
            artefact_field("measured_depth", "10125", "ft", cell="B5"),
        ),
    )
    for pair in _pairs(session):
        service.sync_version(*pair, session=session)
    report = detect_conflicts(KnowledgeRepository(session))
    assert report.conflicts == 0, "a single source cannot contradict another source"
    assert report.ambiguous == 1, report.to_dict()
    statuses = {row.status for row in session.execute(select(KnowledgeItem)).scalars()}
    assert KnowledgeStatus.CONFLICTED.value not in statuses
    assert report.details[0]["ambiguous_within_source"] == 2


def test_agreement_clears_an_earlier_conflict(session, well, service) -> None:
    repository = KnowledgeRepository(session)
    mud_report(session, well, value="10.2", filename="mud.xlsx")
    mud_report(session, well, value="10.4", filename="check.xlsx")
    for pair in _pairs(session):
        service.sync_version(*pair, session=session)
    assert (
        session.execute(select(KnowledgeConflict)).scalars().one().status
        == ConflictResolution.OPEN.value
    )
    # The second source is corrected to the same value; re-running detection must settle the row.
    row = next(
        item
        for item in session.execute(select(KnowledgeItem)).scalars()
        if item.original_value == "10.4"
    )
    row.original_value = "10.2"
    row.value = 10.2
    row.normalized_value = 10.2
    session.flush()
    report = detect_conflicts(repository)
    assert report.conflicts == 0, report.to_dict()
    assert report.cleared == 1, "the argument is closed, not left standing with a new label"
    assert {item.status for item in session.execute(select(KnowledgeItem)).scalars()} == {
        KnowledgeStatus.ACTIVE.value
    }
    # ``clear_conflict`` removes the row: a conflict record is a to-do, and a closed one is kept as
    # the decision that settled it (see the resolve test), not as an open item.
    assert session.execute(select(KnowledgeConflict)).scalars().first() is None


def test_resolving_keeps_both_values_and_records_who_decided(session, well, service) -> None:
    repository = KnowledgeRepository(session)
    mud_report(session, well, value="10.2", filename="mud.xlsx")
    mud_report(session, well, value="10.4", filename="check.xlsx")
    for pair in _pairs(session):
        service.sync_version(*pair, session=session)
    conflict = session.execute(select(KnowledgeConflict)).scalars().one()
    winner = next(entry for entry in conflict.candidates if entry["value"] == pytest.approx(10.2))
    loser = next(entry for entry in conflict.candidates if entry["item_id"] != winner["item_id"])
    result = service.resolve(
        str(conflict.id),
        chosen_item_id=str(winner["item_id"]),
        note="the mud log was corrected the same evening; the check weight was a dirty-line reading",
        by="drilling engineer",
        session=session,
    )
    assert result["status"] == ConflictResolution.RESOLVED_MANUALLY.value
    assert result["resolution"]["by"] == "drilling engineer"
    assert "dirty-line" in result["resolution"]["note"]
    assert result["resolution"]["candidates_at_resolution"], (
        "the decision records what was on the table"
    )
    assert result["recheck"]["conflicts"] == 0, "the key is re-compared, so the marking catches up"
    rows = {
        str(row.id): row for row in session.execute(select(KnowledgeItem)).scalars()
    }  # both sides, by id
    assert rows[str(winner["item_id"])].status == KnowledgeStatus.ACTIVE.value
    assert rows[str(loser["item_id"])].status == KnowledgeStatus.RETIRED.value
    assert rows[str(loser["item_id"])].original_value == "10.4", (
        "the loser is retired, never deleted"
    )
    assert session.get(KnowledgeItem, str(loser["item_id"])) is not None
    # The answer query now excludes the retired side, while the history still contains it.
    current = repository.facts_for_well(well.id)
    assert [fact.original_value for fact in current] == ["10.2"]
    with_history = repository.facts_for_well(well.id, include_superseded=True)
    assert sorted(fact.original_value for fact in with_history) == ["10.2", "10.4"]
    audit = DocumentAudit(session).trail("knowledge_conflict", str(conflict.id))
    assert audit and audit[0].action == "knowledge.conflict_resolved"


class DocumentAudit:
    """Small wrapper so the audit assertion reads as an assertion, not as plumbing."""

    def __init__(self, session) -> None:
        from drilling_intelligence.documents.repository import DocumentRepository

        self._repository = DocumentRepository(session)

    def trail(self, subject_type: str, subject_id: str):
        return self._repository.audit_trail(subject_type, subject_id)


def test_resolving_requires_a_real_candidate(session, well, service) -> None:
    mud_report(session, well, value="10.2", filename="mud.xlsx")
    mud_report(session, well, value="10.4", filename="check.xlsx")
    for pair in _pairs(session):
        service.sync_version(*pair, session=session)
    conflict = session.execute(select(KnowledgeConflict)).scalars().one()
    repository = KnowledgeRepository(session)
    with pytest.raises(ValueError, match="not one of this conflict"):
        resolve_conflict(repository, str(conflict.id), chosen_item_id="ki-invented")
    with pytest.raises(LookupError, match="no conflict"):
        resolve_conflict(repository, "kc-nope", chosen_item_id="ki-invented")


def test_a_superseded_statement_leaves_the_argument(session, well, service) -> None:
    """History is kept and out of the way: revision 2 answers, revision 1 stays readable.

    Detection ignores SUPERSEDED rows; the assertion that matters is that the *conflicted* marking
    from the older revision does not follow into the new one.
    """
    document, first_version, _payload = mud_report(session, well, value="10.2", filename="mud.xlsx")
    mud_report(session, well, value="10.4", filename="check.xlsx")
    pairs = _pairs(session)
    for pair in pairs:
        service.sync_version(*pair, session=session)
    assert session.execute(select(KnowledgeConflict)).scalars().one() is not None
    second_version = register_artefact(
        session,
        filename="mud.xlsx",
        identity_path="docs/mud.xlsx",
        well_id=well.id,
        fields=(artefact_field("mud_weight", "10.4", "ppg", filename="mud.xlsx"),),
        version_number=2,
        supersedes=first_version,
        revision="Rev 2",
    )[1]
    service.sync_version(document.id, second_version.id, session=session)
    repository = KnowledgeRepository(session)
    current = repository.facts_for_well(well.id)
    assert current and all(fact.status != KnowledgeStatus.CONFLICTED.value for fact in current), [
        (fact.original_value, fact.status) for fact in current
    ]
    report = detect_conflicts(repository)
    assert report.conflicts == 0, report.to_dict()


def test_two_sources_quoting_the_same_range_are_not_an_argument(session, well, service) -> None:
    """Each file states both depths; neither contradicts the other, so there is nothing to settle.

    This is the shape a workbook produces when a label/value table is read twice (measured depth and
    true vertical depth both mapping onto a hole-depth field).  Reporting it as a conflict would ask
    a person to adjudicate a parse detail, and the row they were told to look at would already be in
    both files.  So it is counted as ambiguity inside the extraction instead, and the values stay
    ``ACTIVE`` because the platform has not lost confidence in them - it never had a reason to.
    """

    def hole_depth_report(filename: str):
        return register_artefact(
            session,
            filename=filename,
            identity_path=f"docs/{filename}",
            well_id=well.id,
            fields=(
                artefact_field("hole_depth", "9850", "ft", cell="B5", filename=filename),
                artefact_field("hole_depth", "10125", "ft", cell="B6", filename=filename),
            ),
        )

    hole_depth_report("tvd.xlsx")
    hole_depth_report("tvd_check.xlsx")
    for pair in _pairs(session):
        service.sync_version(*pair, session=session)
    report = detect_conflicts(KnowledgeRepository(session))
    assert report.conflicts == 0, report.to_dict()
    assert report.ambiguous == 1, report.to_dict()
    detail = report.details[0]
    assert detail["property"] == "hole_depth"
    assert detail["same_values_in_every_source"] == 4
    statuses = {row.status for row in session.execute(select(KnowledgeItem)).scalars()}
    assert statuses == {KnowledgeStatus.ACTIVE.value}
    assert session.execute(select(KnowledgeConflict)).scalars().first() is None
    # A source that states a value the others do not is back to being a dispute.
    register_artefact(
        session,
        filename="tvd_other.xlsx",
        identity_path="docs/tvd_other.xlsx",
        well_id=well.id,
        fields=(
            artefact_field("hole_depth", "9850", "ft", cell="B5", filename="tvd_other.xlsx"),
            artefact_field("hole_depth", "10250", "ft", cell="B6", filename="tvd_other.xlsx"),
        ),
    )
    session.commit()
    for pair in _pairs(session):
        service.sync_version(*pair, session=session)
    report = detect_conflicts(KnowledgeRepository(session))
    assert report.conflicts == 1, report.to_dict()
    conflict = session.execute(select(KnowledgeConflict)).scalars().one()
    assert "stated by 3 sources" in conflict.note, "the note counts sources, and there are three"
