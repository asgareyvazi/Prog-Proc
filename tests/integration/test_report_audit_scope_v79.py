"""V7.9 audit integrity against the authoritative database.

These tests go through ``verify_report_citations`` and ``RetrievalService.resolve_structured``.
A helper assertion is not a substitute for that path.
"""

from __future__ import annotations

from sqlalchemy import event, func, select

from drilling_intelligence.core.enums import ConfirmationStatus
from drilling_intelligence.core.hashing import sha256_file
from drilling_intelligence.database.base import Base
from drilling_intelligence.database.models import Document, DocumentVersion, HseIncident, NptRecord
from drilling_intelligence.reporting.audit import AUDIT_CAP, verify_report_citations
from drilling_intelligence.reporting.service import build_report
from drilling_intelligence.retrieval.contract import EvidenceItem
from drilling_intelligence.retrieval.service import RetrievalService
from drilling_intelligence.wells.repository import WellRepository


def _counts(session) -> dict[str, int]:
    out: dict[str, int] = {}
    for mapper in Base.registry.mappers:
        name = mapper.local_table.name
        out[name] = int(
            session.execute(select(func.count()).select_from(mapper.local_table)).scalar_one()
        )
    return out


def _selects(engine, fn) -> int:
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


def _ref(
    domain: str,
    sample: list[str],
    well_id: str,
    *,
    count: int | None = None,
    truncated: bool = False,
) -> dict:
    return {
        "section": "operations",
        "domain": domain,
        "kind": "rows",
        "identity": "structured",
        "count": count if count is not None else len(sample),
        "sample": sample,
        "truncated": truncated,
        "method": None,
        "scope": {"well_id": well_id},
    }


def _report(well_id: str, evidence: list[dict], *, window: bool = False) -> dict:
    return {
        "schema": "engineering-report/1",
        "identity": "report-v79",
        "mode": "single_well",
        "title": "Scope <script>",
        "subject": {
            "kind": "well",
            "id": well_id,
            "name": "A",
            "window": {
                "applied": window,
                "windowed_domains": ["npt", "problems", "well_control", "hse"],
            },
        },
        "request": {"well_ids": [well_id], "mode": "single_well"},
        "source_packs": [],
        "sections": [{"section_id": "npt", "title": "NPT", "state": "PRESENT", "tables": []}],
        "exhibits": [],
        "evidence": evidence,
        "limitations": [],
        "observations": [],
        "freshness": {},
    }


def _plant(session, workspace):
    wells = WellRepository(session)
    left = wells.create_well("V79-A")
    right = wells.create_well("V79-B")
    session.add(
        NptRecord(
            id="npt-v79-a",
            well_id=left.id,
            category="stuck_pipe",
            description="current stuck",
            status=ConfirmationStatus.CONFIRMED.value,
        )
    )
    session.add(
        NptRecord(
            id="npt-v79-rejected",
            well_id=left.id,
            category="stuck_pipe",
            description="rejected stuck",
            status=ConfirmationStatus.REJECTED.value,
        )
    )
    session.add(
        NptRecord(
            id="npt-v79-b",
            well_id=right.id,
            category="equipment_failure",
            description="other well",
            status=ConfirmationStatus.CONFIRMED.value,
        )
    )
    session.add(
        HseIncident(
            id="hse-v79-site",
            well_id=None,
            description="camp slip",
            location_text="camp steps",
            status=ConfirmationStatus.CONFIRMED.value,
        )
    )
    for index in range(3):
        session.add(
            NptRecord(
                id=f"npt-v79-cap-{index}",
                well_id=left.id,
                category="wait",
                description=f"cap {index}",
                status=ConfirmationStatus.CONFIRMED.value,
            )
        )
    session.commit()
    return left.id, right.id


def test_scope_domain_and_lifecycle_are_not_implied_by_a_lookup(session, workspace):
    left, right = _plant(session, workspace)
    before = _counts(session)
    original = _report(left, [_ref("npt_record", ["structured:npt_record:npt-v79-a"], left)])
    identity = original["identity"]
    title = original["title"]

    current = verify_report_citations(workspace, original)
    rejected = verify_report_citations(
        workspace,
        _report(left, [_ref("npt_record", ["structured:npt_record:npt-v79-rejected"], left)]),
    )
    foreign_well = verify_report_citations(
        workspace,
        _report(left, [_ref("npt_record", ["structured:npt_record:npt-v79-b"], right)]),
    )
    cross_domain = verify_report_citations(
        workspace,
        _report(left, [_ref("npt_record", ["structured:hse_incident:hse-v79-site"], left)]),
    )
    site = verify_report_citations(
        workspace,
        _report(left, [_ref("hse_incident", ["structured:hse_incident:hse-v79-site"], left)]),
    )
    windowed = verify_report_citations(
        workspace,
        _report(
            left,
            [_ref("npt_record", ["structured:npt_record:npt-v79-rejected"], left)],
            window=True,
        ),
    )

    assert original["identity"] == identity
    assert original["title"] == title
    assert _counts(session) == before

    current_row = next(
        item for item in current.entries if item["identity"] == "structured:npt_record:npt-v79-a"
    )
    assert current_row["resolution"] == "RESOLVED"
    assert current_row["scope_status"] == "IN_SCOPE"
    assert current_row["lifecycle"] == "CURRENT"
    assert current_row["citation_status"] == "NOT_CHECKABLE"
    assert current.overall != "FULLY_VERIFIED"

    rejected_row = next(
        item
        for item in rejected.entries
        if item["sample_id"] == "structured:npt_record:npt-v79-rejected"
    )
    assert rejected_row["current"] is False
    assert rejected_row["lifecycle"] == "NOT_CURRENT"
    assert rejected.overall != "FULLY_VERIFIED"

    foreign_row = next(
        item
        for item in foreign_well.entries
        if item["sample_id"] == "structured:npt_record:npt-v79-b"
    )
    assert foreign_row["scope_status"] == "OUT_OF_SCOPE"
    assert foreign_row["resolution"] == "SCOPE_MISMATCH"
    assert foreign_well.overall != "FULLY_VERIFIED"

    crossed = next(
        item for item in cross_domain.entries if item["sample_id"].startswith("structured:hse")
    )
    assert crossed["resolution"] == "DOMAIN_MISMATCH"
    assert crossed["citation_status"] == "NOT_CHECKABLE"
    assert cross_domain.overall != "FULLY_VERIFIED"

    site_row = next(
        item for item in site.entries if item["sample_id"] == "structured:hse_incident:hse-v79-site"
    )
    assert site_row["scope_status"] == "SITE_LEVEL"
    assert site_row["well_id"] == ""
    assert site.overall != "FULLY_VERIFIED"

    historical = next(item for item in windowed.entries if item["sample_id"].endswith("rejected"))
    assert historical["lifecycle"] == "HISTORICAL_WINDOW"
    assert historical["current"] is False
    assert "does not make it current" not in historical["detail"]
    assert windowed.overall != "FULLY_VERIFIED"


def test_cap_is_applied_before_resolution_and_counts_are_unique_identities(session, workspace):
    left, _right = _plant(session, workspace)
    samples = [f"structured:npt_record:npt-v79-cap-{index}" for index in range(3)]
    samples.append(samples[0])
    report = _report(left, [_ref("npt_record", samples, left, count=3, truncated=False)])
    engine = workspace.database.engine
    holder: dict = {}

    def run(cap: int) -> None:
        holder["audit"] = verify_report_citations(workspace, report, cap=cap)

    assert _selects(engine, lambda: run(0)) == 0
    zero = holder["audit"]
    assert zero.attempted == 0
    assert zero.omitted == 3
    assert zero.eligible == 3
    assert zero.overall == "PARTIALLY_VERIFIED"

    run(1)
    one = holder["audit"]
    assert one.attempted == 1
    assert one.omitted == 2
    assert one.completed == 1
    assert sum(1 for item in one.entries if item["resolution"] == "OMITTED_BY_CAP") == 2

    run(3)
    exact = holder["audit"]
    assert exact.attempted == 3
    assert exact.omitted == 0
    assert exact.eligible == 3


def test_inconsistent_resolver_item_is_not_a_match(session, workspace, monkeypatch):
    left, _right = _plant(session, workspace)
    report = _report(left, [_ref("npt_record", ["structured:npt_record:npt-v79-a"], left)])

    def _wrong(self, identities, *, session=None):
        identity = identities[0]
        return {
            identity: EvidenceItem(
                identity="structured:hse_incident:hse-v79-site",
                source_type="structured",
                record_type="hse_incident",
                source_id="hse-v79-site",
                well_id=left,
            )
        }

    monkeypatch.setattr(RetrievalService, "resolve_structured", _wrong)
    audit = verify_report_citations(workspace, report)
    row = next(
        item for item in audit.entries if item["sample_id"] == "structured:npt_record:npt-v79-a"
    )
    assert row["resolution"] == "INCONSISTENT_ITEM"
    assert row["citation_status"] == "NOT_CHECKABLE"
    assert audit.overall != "FULLY_VERIFIED"


def test_a_matching_citation_does_not_override_lifecycle(session, workspace, tmp_path):
    left = WellRepository(session).create_well("V79-C")
    source = tmp_path / "source.txt"
    source.write_text("stuck pipe\n", encoding="utf-8")
    digest = sha256_file(source)
    document = Document(
        id="doc-v79",
        identity_path="source.txt",
        filename="source.txt",
        sha256=digest,
        well_id=left.id,
    )
    version = DocumentVersion(
        id="dv-v79",
        document_id=document.id,
        version_number=1,
        source_path=str(source),
        sha256=digest,
    )
    citation = [
        {
            "document_id": document.id,
            "document_version_id": version.id,
            "filename": "source.txt",
            "source_sha256": digest,
            "excerpt": "stuck pipe",
            "locator": {"kind": "text", "line_start": 1, "line_end": 1},
        }
    ]
    session.add_all(
        [
            document,
            version,
            NptRecord(
                id="npt-v79-match",
                well_id=left.id,
                category="stuck_pipe",
                description="stuck pipe",
                status=ConfirmationStatus.CONFIRMED.value,
                provenance=citation,
            ),
            NptRecord(
                id="npt-v79-stale-match",
                well_id=left.id,
                category="stuck_pipe",
                description="old stuck pipe",
                status=ConfirmationStatus.REJECTED.value,
                provenance=citation,
            ),
        ]
    )
    session.flush()
    document.current_version_id = version.id
    session.commit()
    current = verify_report_citations(
        workspace,
        _report(left.id, [_ref("npt_record", ["structured:npt_record:npt-v79-match"], left.id)]),
    )
    stale = verify_report_citations(
        workspace,
        _report(
            left.id, [_ref("npt_record", ["structured:npt_record:npt-v79-stale-match"], left.id)]
        ),
    )
    current_row = next(item for item in current.entries if item["identity"].endswith("match"))
    stale_row = next(item for item in stale.entries if "stale" in item["identity"])
    assert current_row["citation_status"] == "MATCH"
    assert current_row["lifecycle"] == "CURRENT"
    assert current.overall == "FULLY_VERIFIED"
    assert stale_row["citation_status"] == "MATCH"
    assert stale_row["lifecycle"] == "NOT_CURRENT"
    assert stale.overall == "PARTIALLY_VERIFIED"
    source.write_text("changed\n", encoding="utf-8")
    mismatched = verify_report_citations(
        workspace,
        _report(left.id, [_ref("npt_record", ["structured:npt_record:npt-v79-match"], left.id)]),
    )
    assert any(item["citation_status"] == "MISMATCH" for item in mismatched.entries)
    assert mismatched.overall == "FAILED"


def test_failed_audit_does_not_write_and_single_well_scope_comes_from_the_pack(session, workspace):
    left, right = _plant(session, workspace)
    before = _counts(session)

    def boom(self, identities, *, session=None):
        raise OSError("audit store unavailable")

    original = RetrievalService.resolve_structured
    RetrievalService.resolve_structured = boom
    try:
        failed = verify_report_citations(
            workspace,
            _report(left, [_ref("npt_record", ["structured:npt_record:npt-v79-a"], left)]),
        )
    finally:
        RetrievalService.resolve_structured = original
    assert failed.overall == "INCOMPLETE"
    assert failed.attempted == 1
    assert failed.completed == 0
    assert _counts(session) == before

    built = build_report(session, well_ids=[left]).to_dict()
    assert built["subject"]["id"] == left
    copied = dict(built)
    copied["evidence"] = [
        *list(built["evidence"]),
        _ref("npt_record", ["structured:npt_record:npt-v79-b"], right),
    ]
    audited = verify_report_citations(workspace, copied)
    assert built["identity"] == build_report(session, well_ids=[left]).identity
    foreign = next(
        item
        for item in audited.entries
        if item.get("sample_id") == "structured:npt_record:npt-v79-b"
    )
    assert foreign["scope_status"] == "OUT_OF_SCOPE"
    assert audited.report_identity == copied["identity"]
    assert AUDIT_CAP == 40
