"""V7.8 traceability and citation-status policy. No database."""

from __future__ import annotations

from drilling_intelligence.reporting.audit import overall_status
from drilling_intelligence.reporting.exhibits import _METRIC_DOMAIN
from drilling_intelligence.reporting.html import render_html
from drilling_intelligence.reporting.lineage import METRIC_DOMAIN, traceability_manifest


def _pack() -> dict:
    return {
        "schema": "engineering-report/1",
        "identity": "report-id",
        "title": "Report <script>",
        "mode": "explicit_well_set",
        "subject": {"subjects": [{"well_id": "w-1", "name": "A"}]},
        "source_packs": [{"schema": "comparison-pack/1", "identity": "pack-id"}],
        "evidence": [
            {
                "section": "operations",
                "domain": "npt_record",
                "kind": "aggregate",
                "identity": "method",
                "method": "sum_hours",
                "scope": {"well_id": "w-1"},
                "count": 12,
                "sample": [],
                "truncated": False,
            },
            {
                "section": "operations",
                "domain": "npt_record",
                "kind": "rows",
                "identity": "structured",
                "method": "",
                "scope": {"well_id": "w-1"},
                "count": 3,
                "sample": ["structured:npt_record:npt-1"],
                "truncated": True,
            },
        ],
        "sections": [
            {
                "section_id": "npt",
                "title": "NPT",
                "state": "PRESENT",
                "tables": [
                    {
                        "table_id": "matrix",
                        "title": "Matrix",
                        "rows": [
                            {"metric": "npt.hours", "label": "Hours", "value": 12},
                            {"metric": "cost.planned", "label": "Cost", "value": 12},
                        ],
                    }
                ],
            },
            {
                "section_id": "timeline",
                "title": "Timeline",
                "state": "UNSUPPORTED",
                "tables": [],
            },
        ],
        "exhibits": [],
        "limitations": [],
        "observations": [],
        "freshness": {},
    }


def test_domain_map_matches_the_exhibit_map():
    assert METRIC_DOMAIN == _METRIC_DOMAIN


def test_a_shared_number_is_not_a_link():
    manifest = traceability_manifest(_pack()).to_dict()
    linked = {(item["target_id"], item["ref_id"]) for item in manifest["links"]}
    assert ("metric:npt.hours", "src-0000") in linked
    assert ("metric:cost.planned", "src-0000") not in linked
    cost = next(item for item in manifest["targets"] if item["target_id"] == "metric:cost.planned")
    assert cost["state"] == "MISSING_REFERENCE"
    assert cost["ref_ids"] == []


def test_manifest_identity_is_stable_and_excludes_itself():
    first = traceability_manifest(_pack()).to_dict()
    second = traceability_manifest(_pack()).to_dict()
    assert first == second
    assert first["identity"]
    body = {key: value for key, value in first.items() if key != "identity"}
    from drilling_intelligence.core.hashing import sha256_obj

    assert sha256_obj(body) == first["identity"]


def test_html_says_verification_was_not_requested_and_escapes():
    text = render_html(_pack())
    assert "Citation verification was not requested" in text
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "file://" not in text
    assert "tmp" not in text.lower()


def test_html_with_an_audit_names_the_overall_status():
    text = render_html(
        _pack(),
        audit={
            "overall": "PARTIALLY_VERIFIED",
            "entries": [
                {
                    "ref_id": "src-0000",
                    "resolution": "AGGREGATE",
                    "citation_status": "NOT_CHECKABLE",
                    "detail": "method only /tmp/secret.txt",
                }
            ],
        },
    )
    assert "PARTIALLY_VERIFIED" in text
    assert "Citation verification was requested" in text
    assert "/opt/secret/secret.txt" not in text
    assert "secret.txt" in text


def test_overall_status_does_not_treat_silence_as_full_verification():
    assert overall_status([], omitted=0, incomplete=False) == "PARTIALLY_VERIFIED"
    assert (
        overall_status(
            [{"resolution": "AGGREGATE", "citation_status": "NOT_CHECKABLE"}],
            omitted=0,
            incomplete=False,
        )
        == "PARTIALLY_VERIFIED"
    )
    assert (
        overall_status(
            [{"resolution": "RESOLVED", "citation_status": "MATCH"}],
            omitted=1,
            incomplete=False,
        )
        == "PARTIALLY_VERIFIED"
    )
    assert (
        overall_status(
            [
                {"resolution": "RESOLVED", "citation_status": "MATCH"},
                {"resolution": "RESOLVED", "citation_status": "UNREADABLE"},
            ],
            omitted=0,
            incomplete=False,
        )
        == "FAILED"
    )
    assert (
        overall_status(
            [
                {
                    "resolution": "RESOLVED",
                    "citation_status": "MATCH",
                    "scope_status": "IN_SCOPE",
                    "domain_status": "AGREE",
                    "lifecycle": "CURRENT",
                    "current": True,
                    "relationship": "DIRECT_RECORD",
                    "coverage": "COMPLETE",
                }
            ],
            omitted=0,
            incomplete=False,
        )
        == "FULLY_VERIFIED"
    )
    assert (
        overall_status(
            [{"resolution": "RESOLVED", "citation_status": "MATCH"}],
            omitted=0,
            incomplete=False,
        )
        == "PARTIALLY_VERIFIED"
    )
    assert overall_status([], omitted=0, incomplete=True) == "INCOMPLETE"
