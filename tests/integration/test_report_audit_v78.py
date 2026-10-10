"""V7.8 lineage against the certified A-3 / B-11 world.

Traceability is a reading of a built report. Citation verification is optional,
read-only, and separate from the report identity.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event, func, select
from tests.integration.test_cli_domain import _capture, call

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.base import Base
from drilling_intelligence.reporting.audit import AUDIT_CAP, verify_report_citations
from drilling_intelligence.reporting.html import render_html
from drilling_intelligence.reporting.lineage import traceability_manifest
from drilling_intelligence.reporting.service import build_report


def _select_count(engine, fn) -> int:
    count = {"n": 0}

    def before(_conn, _cursor, statement, *_args, **_kwargs) -> None:
        if str(statement).lstrip().upper().startswith("SELECT"):
            count["n"] += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return count["n"]


def _counts(session) -> dict[str, int]:
    out: dict[str, int] = {}
    for mapper in Base.registry.mappers:
        name = mapper.local_table.name
        out[name] = int(
            session.execute(select(func.count()).select_from(mapper.local_table)).scalar_one()
        )
    return out


def test_traceability_reads_the_pack_and_adds_no_sql(session, golden):
    report = build_report(session, well_ids=[golden["a3"], golden["b11"]]).to_dict()
    engine = session.get_bind()

    def read() -> None:
        manifest = traceability_manifest(report).to_dict()
        render_html(report)
        assert manifest["report_identity"] == report["identity"]
        npt = next(item for item in manifest["targets"] if item["target_id"] == "metric:npt.hours")
        assert npt["ref_ids"]
        cost = next(
            item for item in manifest["targets"] if item["target_id"] == "metric:cost.planned"
        )
        # A shared displayed number must not attach the NPT reference to cost.
        assert set(npt["ref_ids"]).isdisjoint(set(cost["ref_ids"]))

    assert _select_count(engine, read) == 0


def test_citation_audit_is_separate_read_only_and_not_a_silent_pass(session, golden):
    session.commit()
    report = build_report(session, well_ids=[golden["a3"], golden["b11"]]).to_dict()
    before = _counts(session)
    engine = golden["workspace"].database.engine
    holder: dict = {}

    def _run() -> None:
        holder["audit"] = verify_report_citations(golden["workspace"], report)

    audit_selects = _select_count(engine, _run)
    audit = holder["audit"]
    # Measured on the golden pair: 22 eligible samples, 10 batched SELECTs, not one per row.
    assert audit_selects <= 20, audit_selects
    after = _counts(session)
    assert before == after
    payload = audit.to_dict()
    assert payload["schema"] == "report-evidence-audit/1"
    assert payload["report_identity"] == report["identity"]
    assert payload["overall"] != "FULLY_VERIFIED"
    assert payload["eligible"] <= AUDIT_CAP or payload["omitted"] == payload["eligible"] - AUDIT_CAP
    assert payload["attempted"] <= AUDIT_CAP
    assert payload["omitted"] == max(0, payload["eligible"] - AUDIT_CAP)
    html = render_html(report, audit=payload)
    assert "Citation verification was requested" in html
    assert payload["overall"] in html
    assert (
        report["identity"] == build_report(session, well_ids=[golden["a3"], golden["b11"]]).identity
    )


def test_default_report_json_is_unchanged_and_audit_is_opt_in(golden):
    workspace = golden["workspace"]
    body = call(workspace, "fields", "report", "--well", "A-3")
    assert body["schema"] == "engineering-report/1"
    assert "audit" not in body
    code, out, _err = _capture(workspace, "fields", "report", "--well", "A-3", "--verify-citations")
    envelope = json.loads(out)
    assert envelope["schema"] == "engineering-report-audited/1"
    assert envelope["report"]["identity"] == body["identity"]
    assert envelope["audit"]["overall"] in {
        "PARTIALLY_VERIFIED",
        "FAILED",
        "INCOMPLETE",
    }
    assert code in {0, 1}


def test_failed_export_leaves_no_partial_file(tmp_path, golden):
    from drilling_intelligence.reporting.html import write_report_html

    report = {
        "schema": "engineering-report/1",
        "identity": "x",
        "title": "T",
        "mode": "single_well",
        "subject": {},
        "source_packs": [],
        "sections": [],
        "exhibits": [],
        "evidence": [],
        "limitations": [],
        "observations": [],
        "freshness": {},
    }
    missing = tmp_path / "missing" / "report.html"
    with pytest.raises(ValidationError):
        write_report_html(missing, report)
    assert not missing.exists()
    assert not missing.with_name(missing.name + ".partial").exists()
