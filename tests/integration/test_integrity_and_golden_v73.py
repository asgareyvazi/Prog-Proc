"""V7.3A integrity coverage and the composed cross-domain golden scenario.

Two things get proved here that the per-domain tests cannot.

First, ``database.integrity`` had never heard of the V7.1/V7.2 record tables.  Its
``RELATION_ENDPOINT_MODELS`` map decides which tables a knowledge relation may name as an endpoint,
and by that map's own contract a record table missing from it is *not* an unsupported relationship -
it is a rejected write.  So an edge pointing at a real, provenance-carrying well-control row was
being rejected in exactly the way a typo in an endpoint name is, which made a genuine row
indistinguishable from a bug in the caller.

Second, the golden scenario: one workspace holding A-3 (well control, HSE, NPT and a problem),
B-11 (its own NPT and problem), and a site-only HSE incident that belongs to no well at all.  Every
domain is read back through its own surface and the counts are reconciled against the database.
"""

from __future__ import annotations

import sys

from tests.fixtures.fieldops import fetch, ingest_v72, promote, promote_file, well_id_for

from drilling_intelligence.database.integrity import (
    RELATION_ENDPOINT_MODELS,
    KnowledgeIntegrityError,
    validate_knowledge_relation,
)
from drilling_intelligence.database.models import (
    Base,
    HseIncident,
    LessonLearned,
    MudReport,
    NptRecord,
    ProblemOccurrence,
    WellControlEvent,
    WellEvent,
)
from drilling_intelligence.intelligence.field import FieldIntelligence
from drilling_intelligence.intelligence.timeline import TIMELINE_KINDS

HSE_REGISTER = "hse_register_well-a3.xlsx"
HSE_SITE = "hse_site_register.xlsx"
WC_LOG = "well_control_log_well-a3.xlsx"


def _prepared(workspace) -> None:
    """The whole V7.2 corpus promoted, so every domain is populated from the same run.

    ``promote`` covers the operational documents (NPT, problems, mud, DDR); the three V7.2 sources
    are promoted by name because the site register carries no well and must not share a run with
    one that does.
    """
    ingest_v72(workspace)
    promote(workspace)
    for file_name in (WC_LOG, HSE_REGISTER, HSE_SITE):
        promote_file(workspace, file_name)


def _project_id(workspace) -> str:
    from drilling_intelligence.database.models import Well

    with workspace.database.read_only() as session:
        return str(session.get(Well, well_id_for(workspace, "A-3")).project_id)


# ------------------------------------------------------------------ P17: integrity coverage


def test_every_v7_record_table_is_a_relation_endpoint_it_can_actually_be(workspace) -> None:
    """A real row must be a legal endpoint, not a rejected write."""
    for name in ("well_control_event", "hse_incident", "casing_run", "cement_job"):
        assert name in RELATION_ENDPOINT_MODELS, (
            f"{name} is an authoritative record table but is not a supported relation endpoint, "
            "so an edge naming it is rejected like a typo"
        )


def test_an_edge_to_a_real_well_control_row_validates(workspace) -> None:
    _prepared(workspace)
    row = fetch(workspace, WellControlEvent)[0]
    with workspace.database.session() as session:
        validate_knowledge_relation(
            session,
            source_type="well_control_event",
            source_id=str(row.id),
            target_type="well",
            target_id=str(row.well_id),
            relation="observed_at",
        )


def test_an_edge_to_a_real_site_only_incident_validates_without_a_well(workspace) -> None:
    """The incident needs no well to be a legal endpoint; the project is the honest target."""
    _prepared(workspace)
    site = [row for row in fetch(workspace, HseIncident) if row.well_id is None]
    assert site, "the fixture must contain a site-only incident"
    with workspace.database.session() as session:
        validate_knowledge_relation(
            session,
            source_type="hse_incident",
            source_id=str(site[0].id),
            target_type="project",
            target_id=str(site[0].project_id),
            relation="recorded_in",
        )


def test_a_dangling_endpoint_is_still_rejected(workspace) -> None:
    """Extending the registry must not make it permissive: an invented row is still a hard error."""
    _prepared(workspace)
    with workspace.database.session() as session:
        try:
            validate_knowledge_relation(
                session,
                source_type="well_control_event",
                source_id="does-not-exist",
                target_type="well",
                target_id=str(well_id_for(workspace, "A-3")),
                relation="observed_at",
            )
        except KnowledgeIntegrityError as error:
            assert "does not exist" in str(error)
        else:  # pragma: no cover - the guard is the point
            raise AssertionError("a dangling endpoint must be rejected")


def test_an_unknown_endpoint_type_is_still_rejected(workspace) -> None:
    _prepared(workspace)
    with workspace.database.session() as session:
        try:
            validate_knowledge_relation(
                session,
                source_type="not_a_record_table",
                source_id="x",
                target_type="well",
                target_id=str(well_id_for(workspace, "A-3")),
                relation="observed_at",
            )
        except KnowledgeIntegrityError as error:
            assert "not a supported endpoint" in str(error)
        else:  # pragma: no cover - the guard is the point
            raise AssertionError("an unregistered endpoint type must be rejected")


# --------------------------------------------- P32/P33: one workspace, every domain, reconciled


def test_golden_scenario_three_wells_of_context_and_one_with_no_well(workspace) -> None:
    """A-3 (WC + HSE + NPT + problem), B-11 (NPT + problem), and a site-only incident.

    The point is not that any single number is interesting.  It is that all of them are read out of
    one workspace at once and still agree with the database, and that the site incident is never
    absorbed into either well.
    """
    _prepared(workspace)
    a3 = well_id_for(workspace, "A-3")
    b11 = well_id_for(workspace, "B-11")
    project = _project_id(workspace)

    with workspace.database.read_only() as session:
        intelligence = FieldIntelligence(session)
        wc_a3 = intelligence.well_control(well_id=a3)
        hse_a3 = intelligence.hse(well_id=a3)
        hse_b11 = intelligence.hse(well_id=b11)
        wc_b11 = intelligence.well_control(well_id=b11)
        summary = intelligence.summary(project_id=project)

    # -- the database is the authority, and every surface agrees with it -----------------
    # Counts are taken from the rows, not hardcoded: the bulk promote pulls in the whole V7.2
    # corpus including the no-units log, so the total is whatever the sources stated.  The
    # invariant under test is agreement, not a particular number.
    db_wc = fetch(workspace, WellControlEvent)
    db_hse = fetch(workspace, HseIncident)
    assert len(db_wc) == wc_a3["events"], "the aggregate must equal the rows in the database"
    assert len(db_hse) == summary["hse_incidents"]
    assert all(str(row.well_id) == a3 for row in db_wc), "every well-control row here is A-3's"

    # -- A-3 carries the operational domains; B-11 carries neither -----------------------
    assert hse_a3["incidents"] > 0
    assert hse_b11["incidents"] == 0, "B-11 recorded no incident, and must not be lent one"
    assert wc_b11["events"] == 0, "B-11 recorded no well-control event either"

    # -- NPT and problems are still the domains they always were, on both wells ----------
    npt_rows = fetch(workspace, NptRecord)
    problems = fetch(workspace, ProblemOccurrence)
    assert len(npt_rows) > 0 and len(problems) > 0
    assert summary["npt_rows"] == len(npt_rows)
    assert summary["problems"] == len(problems)
    assert summary["mud_reports"] == len(fetch(workspace, MudReport))

    # -- the site-only incident is visible at project scope and attached to no well ------
    site_rows = [row for row in db_hse if row.well_id is None]
    assert site_rows, "the fixture must contain site-only incidents"
    assert summary["hse_site_scoped"] == len(site_rows)
    assert summary["hse_well_scoped"] == len(db_hse) - len(site_rows)
    assert summary["hse_well_scoped"] + summary["hse_site_scoped"] == summary["hse_incidents"]
    assert summary["hse"]["by_well"][""] == len(site_rows), "site rows keep an empty well key"
    assert summary["hse"]["by_well"][a3] == hse_a3["incidents"]
    assert b11 not in summary["hse"]["by_well"], "B-11 must not appear as an HSE well"

    # -- and the domains were not collapsed into each other ------------------------------
    # The bulk corpus does contain genuine generic WellEvents (NPT-category rows from the daily
    # report).  What must hold is that the well-control rows are not among them: the two counts are
    # read from disjoint sets of rows, from disjoint documents, and neither inflates the other.
    events = fetch(workspace, WellEvent)
    assert summary["events"] == len(events)
    assert summary["well_control_events"] == len(db_wc)
    assert not ({row.id for row in events} & {row.id for row in db_wc}), (
        "a well-control row must never also be counted as a generic event"
    )
    event_docs = {row.document_id for row in events}
    wc_docs = {row.document_id for row in db_wc}
    assert not (event_docs & wc_docs), "the two domains come from different source documents"
    assert summary["npt_rows"] > 0 and summary["npt_rows"] != summary["hse_incidents"]
    # Presence is not diagnosis: more rows carry a pit gain than state a kick.
    assert (
        summary["well_control"]["with_pit_gain"] > summary["well_control"]["by_event_type"]["kick"]
    ), "a pit gain is not evidence of a kick"
    # A type the contract does not know stays blank rather than being guessed into one it does.
    assert summary["well_control"]["by_event_type"][""] >= 1, "unstated types stay unstated"
    assert not any("hour" in key.lower() for key in summary["hse"]), (
        "lost-time wording is never summed into hours at any scope"
    )


# ------------------------------------------------------- P30: the registries must agree with each other


def test_the_record_universe_is_the_same_set_in_every_registry(workspace) -> None:
    """Six places each keep a list of record types; they drift one at a time, silently.

    Every gap found in V7.3A was exactly this: a table added to the schema and the search projection
    but not to retrieval's model map, so hits vanished; and not to integrity's endpoint map, so real
    rows were rejected like typos.  Each list has its own legitimate reason to be narrower than the
    universe, so this pins the relationship rather than pretending they are identical.
    """
    from drilling_intelligence.retrieval.service import _STRUCTURED_MODELS
    from drilling_intelligence.review.service import _OPERATIONAL_TABLES
    from drilling_intelligence.search.structured import (
        _BUILDERS,
        _RECORD_SOURCES,
        STRUCTURED_RECORD_TYPES,
    )

    universe = set(STRUCTURED_RECORD_TYPES)
    assert len(universe) == 14

    # These three must be exactly the universe: a type the projection emits has to be buildable,
    # sourced and retrievable, or the search index advertises rows nothing can resolve.
    assert set(_BUILDERS) == universe, "a record type with no builder"
    assert {record for _model, record, _fn in _RECORD_SOURCES} == universe, "a type with no source"
    assert set(_STRUCTURED_MODELS) == universe, "a type retrieval cannot resolve"

    # These two are deliberately narrower, and the narrowing must be explainable.
    missing_endpoints = universe - set(RELATION_ENDPOINT_MODELS)
    assert missing_endpoints == {"lesson_learned", "problem_definition"}, (
        "lesson_learned is registered under the endpoint key 'lesson'; problem_definition is a "
        f"definition, not a history record, and no caller names it as an endpoint. Unexpected: "
        f"{sorted(missing_endpoints)}"
    )
    assert RELATION_ENDPOINT_MODELS["lesson"] is LessonLearned

    operational = set(_OPERATIONAL_TABLES)
    assert {"well_control_event", "hse_incident"} <= operational, (
        "the V7.2 domains must be reviewable as operational rows"
    )
    # The two maps key on different vocabularies (``field_pattern`` vs ``pattern``), so compare the
    # models they resolve to, not the strings.  Review covers operational history only, so every
    # table it reviews must also be an endpoint integrity accepts.
    by_table = {mapper.class_.__tablename__: mapper.class_ for mapper in Base.registry.mappers}
    operational_models = {by_table[name] for name in operational}
    endpoint_models = set(RELATION_ENDPOINT_MODELS.values())
    assert operational_models <= endpoint_models, sorted(
        model.__name__ for model in operational_models - endpoint_models
    )
    assert len(operational_models) < len(endpoint_models), (
        "integrity accepts more endpoint types than review projects as operational rows"
    )

    # The timeline vocabulary is a kind namespace, not a table namespace, but the two V7.2 kinds
    # must be present or their rows never reach the chronology.
    assert {"well_control", "hse"} <= set(TIMELINE_KINDS)


# ------------------------------------------------------------------------ P20: doctor is not blind


def test_doctor_counts_the_v7_domains_it_used_to_report_as_empty(workspace) -> None:
    """``doctor`` is the "what does this workspace hold" command; a full domain must not read as empty."""
    import json
    from io import StringIO

    from tests.fixtures.fieldops import promote

    from drilling_intelligence.cli.app import main

    ingest_v72(workspace)
    promote(workspace)
    for file_name in (WC_LOG, HSE_REGISTER, HSE_SITE):
        promote_file(workspace, file_name)

    out, err = StringIO(), StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        main(["doctor", "--workspace", str(workspace.root), "--json"])
    except SystemExit:
        pass
    finally:
        sys.stdout, sys.stderr = saved

    payload = json.loads(out.getvalue())
    operational = payload["operational"]
    assert operational["well_control_events"] == len(
        [row for row in fetch(workspace, WellControlEvent) if row.is_current]
    )
    assert operational["well_control_events"] > 0, "the domain is full; doctor must not print 0"
    assert operational["hse_incidents"] == len(
        [row for row in fetch(workspace, HseIncident) if row.is_current]
    )
    assert operational["hse_site_scoped"] == len(
        [row for row in fetch(workspace, HseIncident) if row.is_current and row.well_id is None]
    )
    assert operational["hse_site_scoped"] > 0
    # The pre-existing counts are additive, not replaced.
    for key in ("reports", "events", "npt", "problems", "lessons", "programs", "targets"):
        assert key in operational, f"legacy doctor key {key} disappeared"
