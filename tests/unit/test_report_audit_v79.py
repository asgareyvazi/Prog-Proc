"""V7.9 audit policy, identity, scope and path redaction. No database."""

from __future__ import annotations

from drilling_intelligence.core.hashing import sha256_obj
from drilling_intelligence.core.redact import visible_diagnostic
from drilling_intelligence.reporting.audit import (
    audit_exit_code,
    classify_sample,
    expects_current,
    overall_status,
    report_subject_wells,
    safe_detail,
    window_applied,
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
    broadened = {
        "subject": {"kind": "well", "id": "well-a"},
        "request": {"well_ids": ["well-a", "well-foreign"]},
    }
    assert report_subject_wells(broadened) == {"well-a"}
    assert report_subject_wells({"request": {"well_ids": ["well-only"]}}) == {"well-only"}


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
    assert _relationship({"kind": "rows", "count": 0, "sample": []}) == "COUNT_ONLY"
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
    spaced = safe_detail("cannot open /home/user/My Documents/secret file.txt now")
    assert "/home/user" not in spaced
    assert "My Documents" not in spaced
    assert "secret file.txt" in spaced
    assert "now" in spaced
    relative = safe_detail("missing ../private/secret.txt and ..\\share\\secret.txt")
    assert "../private" not in relative
    assert "..\\share" not in relative
    quoted = safe_detail('quoted "/opt/host/My Documents/secret.txt" done')
    assert "/opt/host" not in quoted
    assert "secret.txt" in quoted
    secret = safe_detail(
        "postgresql://analyst:s3cret-token@db.internal/wells password=s3cret "
        "api_key=abcd1234 Bearer abcdefghijklmnop"
    )
    assert "s3cret-token" not in secret
    assert "s3cret" not in secret
    assert "abcd1234" not in secret
    assert "abcdefghijklmnop" not in secret
    assert "[redacted]" in secret
    assert "db.internal" in secret
    traced = safe_detail(
        'Traceback (most recent call last):\n  File "/opt/host/audit.py", line 3, in run\n'
        "RuntimeError: cannot open /opt/host/secret.txt\n"
    )
    assert "Traceback" not in traced
    assert "/opt/host/audit.py" not in traced
    assert "secret.txt" in traced


def test_prefixed_credentials_are_redacted_and_url_hosts_remain():
    sentinels = (
        "sk-sentinel-openai",
        "ghp_sentinel_github",
        "sentinel-client-secret",
        "sentinel-refresh",
        "sentinel-pg-pass",
        "sentinel-aws-secret",
        "sentinel-dsn-pass",
        "query-pass-sentinel",
        "basic-sentinel",
    )
    text = safe_detail(
        "OPENAI_API_KEY=sk-sentinel-openai "
        "GITHUB_TOKEN=ghp_sentinel_github "
        "CLIENT_SECRET=sentinel-client-secret "
        "refresh_token=sentinel-refresh "
        "POSTGRES_PASSWORD=sentinel-pg-pass "
        "AWS_SECRET_ACCESS_KEY=sentinel-aws-secret "
        "DATABASE_URL=postgresql://analyst:sentinel-dsn-pass@db.internal/wells "
        "Authorization: Basic basic-sentinel "
        "https://host.example/path?password=query-pass-sentinel "
        "postgres://user:sentinel-pg-pass@localhost/db "
        "notasecret=keep-this-label "
        "connection failed; check the workspace path"
    )
    for sentinel in sentinels:
        assert sentinel not in text
    assert "OPENAI_API_KEY=[redacted]" in text
    assert "DATABASE_URL=[redacted]" in text
    assert "postgres://[redacted]@localhost/db" in text
    assert "https://host.example/path?password=[redacted]" in text
    assert "notasecret=keep-this-label" in text
    assert "workspace path" in text
    assert "/opt/secret" not in safe_detail("cannot open /opt/secret/dirname now")


def test_quoted_and_encoded_credentials_are_redacted_without_eating_the_sentence():
    text = safe_detail(
        'password="two word secret" host remains. '
        "API_KEY='token value with spaces' done. "
        'DATABASE_URL="postgresql://user:quoted-dsn-secret@db.internal/wells" next. '
        "postgres://user:s3cret%3Avalue@localhost/db "
        "https://host.example/path?token=query%20sentinel&kept=1 "
        "cannot open /opt/secret/dirname now"
    )
    for sentinel in (
        "two word secret",
        "token value with spaces",
        "quoted-dsn-secret",
        "s3cret%3Avalue",
        "query%20sentinel",
    ):
        assert sentinel not in text
    assert "host remains." in text
    assert "done." in text
    assert "next." in text
    assert "postgres://[redacted]@localhost/db" in text
    assert "kept=1" in text
    assert "dirname" in text
    assert "/opt/secret" not in text


def test_adversarial_credentials_stay_out_of_the_visible_diagnostic():
    sentinels = (
        "json-sentinel",
        "odbc-sentinel",
        "mixed-sentinel",
        "srv-sentinel",
        "bearer-sentinel-token",
        "quoted-bearer-sentinel",
        "basic-sentinel-value",
        "query-access-sentinel",
        "hyphen-pass-sentinel",
    )
    text = safe_detail(
        'status failed "password": "json-sentinel" well remains. '
        "Driver=ODBC;Pwd=odbc-sentinel;Server=db.internal "
        "Api-Key: mixed-sentinel next sentence stays. "
        "mongodb+srv://user:srv-sentinel@cluster.example/db "
        "Authorization: bearer bearer-sentinel-token after bearer. "
        'Authorization: Bearer "quoted-bearer-sentinel" after quoted bearer. '
        "authorization: basic basic-sentinel-value workspace stays. "
        "https://host.example/path?access_key=query-access-sentinel&well=A-3 "
        "POSTGRES-PASSWORD=hyphen-pass-sentinel "
        "tokenizer=keep-this secretive note stays the well is 10.2 ppg"
    )
    for sentinel in sentinels:
        assert sentinel not in text
    assert "well remains." in text
    assert "Server=db.internal" in text
    assert "next sentence stays." in text
    assert "mongodb+srv://[redacted]@cluster.example/db" in text
    assert "after bearer." in text
    assert "after quoted bearer." in text
    assert "workspace stays." in text
    assert "well=A-3" in text
    assert "tokenizer=keep-this" in text
    assert "secretive note stays" in text
    assert "10.2 ppg" in text
    detail = visible_diagnostic(
        RuntimeError('load failed "password": "json-sentinel" check the workspace path')
    )
    assert "json-sentinel" not in detail
    assert "[redacted]" in detail
    assert "workspace path" in detail


def test_json_object_credential_keeps_the_following_sentence() -> None:
    sentinel = "object-secret-sentinel"
    text = safe_detail(
        'token={"nested": {"value": "' + sentinel + '"}} host remains. '
        'api_key = { "key" : "' + sentinel + '" } next field stays. '
        "password = 'two word secret' after the quoted value."
    )
    assert sentinel not in text
    assert "two word secret" not in text
    assert "host remains." in text
    assert "next field stays." in text
    assert "after the quoted value." in text
    assert "[redacted]" in text


def test_visible_diagnostic_redacts_a_quoted_secret_and_keeps_the_exception_type():
    detail = visible_diagnostic(RuntimeError('password="two word secret" check the workspace path'))
    assert "two word secret" not in detail
    assert "password=[redacted]" in detail
    assert "workspace path" in detail
    assert visible_diagnostic(RuntimeError("")) == "RuntimeError"


def test_core_redact_does_not_import_retrieval_or_the_auditor():
    import subprocess
    import sys

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import drilling_intelligence.core.redact as redact\n"
            "assert redact.safe_detail('password=s3cret') == 'password=[redacted]'\n"
            "loaded = set(sys.modules)\n"
            "assert 'drilling_intelligence.reporting.audit' not in loaded\n"
            "assert 'drilling_intelligence.reporting.retrieval' not in loaded\n"
            "assert not any(name.startswith('drilling_intelligence.database') for name in loaded)\n",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_missing_coverage_is_not_an_implied_complete_enumeration():
    assert overall_status([_full(coverage="")], omitted=0, incomplete=False) == "PARTIALLY_VERIFIED"
    assert (
        overall_status([_full(relationship="")], omitted=0, incomplete=False)
        == "PARTIALLY_VERIFIED"
    )
    bare = _full()
    del bare["coverage"]
    assert overall_status([bare], omitted=0, incomplete=False) == "PARTIALLY_VERIFIED"


def test_an_explicit_unapplied_window_is_not_overridden_by_the_request():
    payload = {
        "subject": {"window": {"applied": False}},
        "request": {"since": "2024-01-01"},
    }
    assert window_applied(payload) is False
    assert expects_current(payload, "npt_record") is True
    windowed = {"subject": {"window": {"applied": True}}, "request": {"since": "2024-01-01"}}
    assert expects_current(windowed, "npt_record") is False
    assert expects_current(windowed, "cost_item") is True
    assert expects_current(windowed, "lesson_learned") is True


def test_whitespace_does_not_normalise_into_a_different_identity():
    ref = {"domain": "npt_record", "identity_kind": "structured"}
    padded = classify_sample(ref, "structured:npt_record: npt-1")
    assert padded["action"] == "resolve"
    assert padded["identity"] != "structured:npt_record:npt-1"
    assert classify_sample(ref, "structured:npt_record: ")["action"] == "malformed"
    assert classify_sample(ref, "structured: npt_record:npt-1")["action"] == "domain_mismatch"


def test_workbench_errors_redact_before_they_reach_the_status_bar():
    import ast
    from pathlib import Path

    # WorkerError lives in the Qt-independent errors module so redaction can
    # be exercised headless without importing PySide6. The worker layer
    # re-exports it; check both locations.
    errors_src = Path("src/drilling_intelligence/ui/errors.py").read_text(encoding="utf-8")
    worker_src = Path("src/drilling_intelligence/ui/worker.py").read_text(encoding="utf-8")
    tree = ast.parse(errors_src)
    found = False
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "WorkerError":
            found = any(
                isinstance(item, ast.FunctionDef) and item.name == "__post_init__"
                for item in node.body
            )
    assert found, "WorkerError with __post_init__ must be defined in errors.py"
    assert "core.redact import safe_detail" in errors_src
    assert "reporting.audit import safe_detail" not in errors_src
    assert "reporting.audit import safe_detail" not in worker_src
    # worker.py must re-export WorkerError from the Qt-independent module
    assert (
        "from .errors import WorkerError" in worker_src
        or "from drilling_intelligence.ui.errors import WorkerError" in worker_src
    )
    # No duplicate definition in worker.py
    worker_tree = ast.parse(worker_src)
    for node in worker_tree.body:
        assert not (isinstance(node, ast.ClassDef) and node.name == "WorkerError"), (
            "WorkerError must not be redefined in worker.py"
        )


def test_overall_status_does_not_depend_on_entry_order():
    mismatch = _full(citation_status="MISMATCH")
    match = _full()
    assert overall_status([mismatch, match], omitted=0, incomplete=True) == "FAILED"
    assert overall_status([match, mismatch], omitted=0, incomplete=True) == "FAILED"
    gap = _full(coverage="")
    assert overall_status([match, gap], omitted=0, incomplete=False) == "PARTIALLY_VERIFIED"
    assert overall_status([gap, match], omitted=0, incomplete=False) == "PARTIALLY_VERIFIED"


def test_a_full_status_with_an_operational_error_is_not_rendered_as_verification():
    from drilling_intelligence.reporting.html import render_html

    payload = {
        "schema": "engineering-report/1",
        "identity": "r",
        "title": "Report",
        "mode": "single_well",
        "sections": [],
        "exhibits": [],
        "evidence": [],
        "limitations": [],
        "observations": [],
        "freshness": {},
    }
    html = render_html(
        payload,
        audit={
            "overall": "FULLY_VERIFIED",
            "operational_error": "RuntimeError: cannot open /opt/host-secret/citation.txt",
            "entries": [],
        },
    )
    assert "FULLY_VERIFIED" not in html
    assert "not full verification" in html
    assert "/opt/host-secret" not in html
    assert "citation.txt" in html
    quoted = render_html(
        payload,
        audit={
            "overall": "INCOMPLETE",
            "operational_error": 'RuntimeError: password="two word secret" after the assignment',
            "entries": [],
        },
    )
    assert "two word secret" not in quoted
    assert "Operational failure:" in quoted
    assert "after the assignment" in quoted


def test_an_unrecognised_audit_status_is_not_rendered_as_verification():
    from drilling_intelligence.reporting.html import render_html

    payload = {
        "schema": "engineering-report/1",
        "identity": "r",
        "title": "A <script> & title",
        "mode": "single_well",
        "sections": [],
        "exhibits": [],
        "evidence": [],
        "limitations": [],
        "observations": [],
        "freshness": {},
    }
    html = render_html(payload, audit={"overall": "", "entries": []})
    assert "FULLY_VERIFIED" not in html
    assert "not full verification" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
