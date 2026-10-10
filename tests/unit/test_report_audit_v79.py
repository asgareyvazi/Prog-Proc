"""V7.9 audit policy, identity, scope and path redaction. No database."""

from __future__ import annotations

from drilling_intelligence.core.hashing import sha256_obj
from drilling_intelligence.reporting.audit import (
    audit_exit_code,
    classify_sample,
    overall_status,
    report_subject_wells,
    safe_detail,
)
from drilling_intelligence.reporting.lineage import (
    REL_DIRECT,
    REL_ENUMERATED,
    manifest_links_closed,
    traceability_manifest,
)


def _full(**overrides):
    body = {
        "resolution": "RESOLVED",
        "citation_status": "MATCH",
        "scope_status": "IN_SCOPE",
        "domain_status": "AGREE",
        "lifecycle": "CURRENT",
        "current": True,
        "relationship": "DIRECT_RECORD",
        "coverage": "COMPLETE",
    }
    body.update(overrides)
    return body


def test_matching_citation_does_not_make_a_stale_row_fully_verified():
    assert (
        overall_status([_full(lifecycle="NOT_CURRENT", current=False)], omitted=0, incomplete=False)
        == "PARTIALLY_VERIFIED"
    )
    assert (
        overall_status(
            [_full(), _full(lifecycle="NOT_CURRENT", current=False)], omitted=0, incomplete=False
        )
        == "PARTIALLY_VERIFIED"
    )


def test_mismatch_outranks_stale_and_incomplete():
    stale = _full(lifecycle="NOT_CURRENT", current=False, citation_status="MISMATCH")
    assert overall_status([_full(), stale], omitted=0, incomplete=True) == "FAILED"
    assert (
        overall_status([_full(citation_status="UNREADABLE")], omitted=0, incomplete=True)
        == "FAILED"
    )
    assert overall_status([_full()], omitted=0, incomplete=True) == "INCOMPLETE"


def test_empty_and_aggregate_are_not_full():
    assert overall_status([], omitted=0, incomplete=False) == "PARTIALLY_VERIFIED"
    assert (
        overall_status(
            [
                _full(
                    relationship="AGGREGATE_METHOD",
                    coverage="AGGREGATE",
                    citation_status="NOT_CHECKABLE",
                )
            ],
            omitted=0,
            incomplete=False,
        )
        == "PARTIALLY_VERIFIED"
    )
    assert (
        overall_status(
            [_full(relationship="TRUNCATED_SAMPLE", coverage="TRUNCATED")],
            omitted=0,
            incomplete=False,
        )
        == "PARTIALLY_VERIFIED"
    )
    assert overall_status([_full()], omitted=1, incomplete=False) == "PARTIALLY_VERIFIED"


def test_historical_window_match_can_be_full_and_current_state_stale_cannot():
    historical = _full(lifecycle="HISTORICAL_WINDOW", current=False)
    assert overall_status([historical], omitted=0, incomplete=False) == "FULLY_VERIFIED"
    assert (
        overall_status([_full(lifecycle="UNKNOWN", current=None)], omitted=0, incomplete=False)
        == "PARTIALLY_VERIFIED"
    )


def test_site_and_wrong_well_are_not_in_scope():
    assert (
        overall_status([_full(scope_status="SITE_LEVEL")], omitted=0, incomplete=False)
        == "PARTIALLY_VERIFIED"
    )
    assert (
        overall_status(
            [_full(scope_status="OUT_OF_SCOPE", resolution="SCOPE_MISMATCH")],
            omitted=0,
            incomplete=False,
        )
        == "PARTIALLY_VERIFIED"
    )
    assert (
        overall_status([_full(scope_status="UNKNOWN")], omitted=0, incomplete=False)
        == "PARTIALLY_VERIFIED"
    )


def test_exit_code_policy():
    assert audit_exit_code("FAILED") == 1
    assert audit_exit_code("INCOMPLETE") == 1
    assert audit_exit_code("PARTIALLY_VERIFIED") == 0
    assert audit_exit_code("FULLY_VERIFIED") == 0


def test_cross_domain_structured_identity_is_not_resolved():
    ref = {"domain": "npt_record", "identity_kind": "structured"}
    foreign = classify_sample(ref, "structured:cost_item:cost-1")
    assert foreign["action"] == "domain_mismatch"
    swapped = classify_sample(
        {"domain": "cost_item", "identity_kind": "record"}, "structured:npt_record:npt-1"
    )
    assert swapped["action"] == "domain_mismatch"
    assert classify_sample(ref, "structured:npt_record:")["action"] == "malformed"
    assert classify_sample(ref, "structured::npt-1")["action"] == "malformed"
    assert classify_sample(ref, "structured:npt_record")["action"] == "malformed"
    assert classify_sample(ref, "not-an-id")["action"] == "resolve"
    assert (
        classify_sample({"domain": "npt_record", "identity_kind": "record"}, "plain-id")["action"]
        == "not_on_path"
    )
    agreed = classify_sample(ref, "structured:npt_record:npt-1")
    assert agreed["action"] == "resolve"
    assert agreed["identity"] == "structured:npt_record:npt-1"


def test_single_well_subject_is_a_scope_and_an_empty_list_is_not_every_well():
    single = {"subject": {"kind": "well", "id": "well-a"}, "request": {"well_ids": ["well-a"]}}
    assert report_subject_wells(single) == {"well-a"}
    comparison = {
        "subject": {
            "subjects": [
                {"well_id": "well-a", "selection": "anchor"},
                {"well_id": "well-b", "selection": "discovered_offset"},
            ]
        }
    }
    assert report_subject_wells(comparison) == {"well-a", "well-b"}
    assert report_subject_wells({"subject": {"subjects": []}}) == set()
    assert report_subject_wells({"subject": {"kind": "well"}}) == set()


def test_relationship_follows_the_producer_contract():
    from drilling_intelligence.reporting.lineage import _relationship

    assert (
        _relationship({"kind": "rows", "count": 1, "sample": ["a"], "truncated": False})
        == REL_DIRECT
    )
    assert (
        _relationship({"kind": "rows", "count": 3, "sample": ["a", "b", "c"], "truncated": False})
        == REL_ENUMERATED
    )
    assert (
        _relationship({"kind": "rows", "count": 4, "sample": ["a"], "truncated": True})
        == "TRUNCATED_SAMPLE"
    )
    assert (
        _relationship(
            {"kind": "aggregate", "method": "FieldIntelligence.npt", "count": 2, "sample": []}
        )
        == "AGGREGATE_METHOD"
    )
    assert _relationship({"kind": "records", "count": 2, "sample": []}) == "COUNT_ONLY"
    assert (
        _relationship({"kind": "records", "count": 1, "sample": ["kept"], "truncated": False})
        == REL_DIRECT
    )
    assert (
        _relationship({"kind": "rows", "count": 5, "sample": ["a", "b"], "truncated": False})
        == "BOUNDED_SAMPLE"
    )


def test_duplicate_target_ids_are_not_merged():
    payload = {
        "schema": "engineering-report/1",
        "identity": "r",
        "source_packs": [],
        "evidence": [],
        "sections": [
            {"section_id": "npt", "title": "One", "state": "PRESENT", "tables": []},
            {"section_id": "npt", "title": "Two", "state": "PRESENT", "tables": []},
        ],
        "exhibits": [],
    }
    manifest = traceability_manifest(payload).to_dict()
    ids = [item["target_id"] for item in manifest["targets"] if item["kind"] == "section"]
    assert len(ids) == len(set(ids)) == 2
    assert manifest_links_closed(manifest)


def test_lifecycle_changes_the_audit_identity():
    from drilling_intelligence.reporting.audit import ReportEvidenceAudit

    def audit(current: bool) -> str:
        entry = _full(current=current, lifecycle="CURRENT" if current else "NOT_CURRENT")
        body = ReportEvidenceAudit(
            schema="report-evidence-audit/1",
            report_identity="r",
            manifest_identity="m",
            cap=40,
            check_cap=40,
            eligible=1,
            attempted=1,
            completed=1,
            omitted=0,
            overall="PARTIALLY_VERIFIED",
            counts={},
            entries=(entry,),
        )
        return body.to_dict()["identity"]

    assert audit(True) != audit(False)
    rendered = {"schema": "x", "current": True}
    assert sha256_obj(rendered) == sha256_obj({"current": True, "schema": "x"})


def test_html_export_failure_does_not_replace_a_finished_file(tmp_path, monkeypatch):
    from pathlib import Path

    from drilling_intelligence.core.errors import ValidationError
    from drilling_intelligence.reporting.html import render_html, write_report_html

    payload = {
        "schema": "engineering-report/1",
        "identity": "keep-me",
        "title": "<script>alert(1)</script>",
        "mode": "single_well",
        "sections": [],
        "exhibits": [],
        "evidence": [],
        "limitations": [],
        "observations": [],
        "freshness": {},
    }
    path = tmp_path / "report.html"
    path.write_text("original", encoding="utf-8")

    def _fail(self, target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", _fail)
    try:
        write_report_html(path, payload)
    except OSError:
        pass
    else:
        raise AssertionError("export must fail when replace fails")
    assert path.read_text(encoding="utf-8") == "original"
    assert not path.with_name(path.name + ".partial").exists()
    rendered = render_html(payload)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    missing = tmp_path / "missing" / "report.html"
    try:
        write_report_html(missing, payload)
    except ValidationError:
        pass
    else:
        raise AssertionError("missing parent must be an error")
    assert not missing.exists()
    text = safe_detail(
        "posix /opt/secret/secret.txt windows C:\\Users\\host\\secret.txt "
        "drive C:/Users/host/other.txt unc \\\\server\\share\\secret.txt "
        "url file:///opt/secret/secret.txt see (/opt/secret/secret.txt)."
    )
    assert "/opt/secret" not in text
    assert "C:\\Users" not in text
    assert "C:/Users" not in text
    assert "\\\\server" not in text
    assert "file://" not in text
    assert "secret.txt" in text
    assert "other.txt" in text
