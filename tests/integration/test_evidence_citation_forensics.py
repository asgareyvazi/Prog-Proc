"""P11 forensic verification of the Citation Auditor.

The certified chain proves that an evidence item's *row* is still authoritative (retrieval, ADR-0013)
and that the package it was composed into is still the database's answer (P10).  It does not prove
that the item's *citation* still holds: the source file behind the recorded locator can change or
disappear while every row stays put, and only a re-read of the file can say so.  These tests attack
the auditor's own promises, against real workspaces, real SQLite and real files:

*   **truth** - a mutated source is a named MISMATCH while the row still verifies; a deleted
    source is UNREADABLE, never a silent empty success; a clean corpus verifies;
*   **honesty** - the kind of check is stated (``source`` for a view of a larger region,
    ``excerpt`` for a quotation); what cannot be checked is NOT_CHECKABLE, not passed;
*   **structured rows** - a row's evidence list is what its citation check applies to, and the
    row folds its citations to the worst of them;
*   **safety** - one batched authoritative read for the whole package, read-only, byte-for-byte
    deterministic, and independent of the order the items were composed in.

The auditor composes rather than duplicates: the excerpt/hash comparison is the core
``verify_provenance`` (the same check search's ``--verify`` runs) and the version-to-file
resolution goes through the document repository.  Nothing here mocks a database or a file.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select, text

from drilling_intelligence.evidence import (
    STATUS_MATCH,
    STATUS_MISMATCH,
    STATUS_NOT_CHECKABLE,
    STATUS_UNREADABLE,
    CitationAuditor,
    EvidencePackage,
    EvidenceQuery,
    EvidenceQueryService,
    PackageEvidence,
)
from drilling_intelligence.lessons.repository import LessonRepository
from drilling_intelligence.retrieval.contract import EvidenceItem
from drilling_intelligence.retrieval.service import RetrievalService
from drilling_intelligence.search.service import SearchService

MUD_TOPIC = "mud weight"


# ============================================================================ helpers
def _db_fingerprint(workspace) -> str:
    """A hash over every table of the authoritative database - a write anywhere moves it."""
    out: list[str] = []
    with workspace.database.engine.connect() as conn:
        tables = [
            row[0]
            for row in conn.execute(
                text(
                    "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                )
            ).fetchall()
        ]
        for table in sorted(tables):
            assert re.fullmatch(r"[a-z_][a-z0-9_]*", table), f"unexpected table name {table!r}"
            rows = conn.execute(text(f'select rowid, * from "{table}" order by rowid')).fetchall()  # noqa: S608
            out.append(
                f"{table}:{len(rows)}:" + hashlib.sha256(repr(rows).encode()).hexdigest()[:16]
            )
    return hashlib.sha256("\n".join(out).encode()).hexdigest()


def _workspace_file_hashes(workspace) -> dict[str, str]:
    """sha256 of every database and corpus file in the workspace - the auditor moves none of them."""
    files: dict[str, str] = {}
    for path in sorted(workspace.root.rglob("*.db")):
        files[str(path)] = str(path)
    corpus = workspace.root / "corpus"
    if corpus.is_dir():
        for path in sorted(corpus.rglob("*")):
            if path.is_file():
                files[str(path)] = str(path)
    assert files, "a workspace with no databases and no corpus is not the fixture this suite uses"
    return {
        name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in files.items()
    }


def _select_count(engine, fn) -> int:
    count = {"value": 0}

    def before(conn, cursor, statement, params, context, executemany):
        if str(statement).lstrip().upper().startswith("SELECT"):
            count["value"] += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return count["value"]


def _cited_file(workspace, audit, suffix: str) -> Path:
    """The corpus file behind a check that cites ``suffix`` - found from the citation itself."""
    cited = {
        check.citation.split(" > ")[0]
        for check in audit.checks
        if check.citation and check.citation.split(" > ")[0].endswith(suffix)
    }
    assert cited, f"no check citing a {suffix} file in the package"
    corpus = workspace.root / "corpus"
    for name in sorted(cited):
        match = next(corpus.rglob(name), None)
        assert match is not None, f"corpus file {name!r} not found under {corpus}"
        return match
    raise AssertionError("unreachable")


@dataclass
class World:
    """The real generated corpus, promoted, knowledge derived, indexed - plus the auditor."""

    ws: Any
    search: SearchService
    evq: EvidenceQueryService
    auditor: CitationAuditor
    ids: dict[str, Any] = field(default_factory=dict)

    def query(self, **kwargs: Any) -> EvidencePackage:
        return self.evq.query(EvidenceQuery(**kwargs))


@pytest.fixture
def world(workspace) -> World:
    from tests.fixtures.fieldops import field_id, ingest, promote, well_id_for

    from drilling_intelligence.knowledge.service import KnowledgeExtractionService

    ws = workspace
    ingest(ws)
    promote(ws)
    KnowledgeExtractionService.for_workspace(ws).rebuild(workspace_id="", well_id="")
    search = SearchService.for_workspace(ws)
    search.rebuild()
    retrieval = RetrievalService(database=ws.database, search_service=search)
    return World(
        ws=ws,
        search=search,
        evq=EvidenceQueryService(retrieval=retrieval),
        auditor=CitationAuditor.for_workspace(ws),
        ids={
            "A3": well_id_for(ws, "A-3"),
            "B11": well_id_for(ws, "B-11"),
            "field": field_id(ws),
        },
    )


def _clean_baseline(world) -> tuple[EvidencePackage, Any]:
    package = world.query(topics=(MUD_TOPIC,))
    return package, world.auditor.audit(package)


def _structured_items(package: EvidencePackage) -> list[Any]:
    return [entry.item for entry in package.items if entry.item.source_type == "structured"]


# ============================================================================ clean corpus
class TestCleanCorpus:
    def test_every_citation_of_a_clean_corpus_verifies(self, world) -> None:
        package, audit = _clean_baseline(world)
        assert package.count >= 5, "the corpus world must answer the topic with real items"
        assert len(audit.checks) == package.count, "one check per item, no more, no less"
        assert audit.counts[STATUS_MISMATCH] == 0
        assert audit.counts[STATUS_UNREADABLE] == 0
        assert audit.counts[STATUS_MATCH] == package.count
        assert audit.all_verified is True
        for check in audit.checks:
            assert check.citation, "a check must carry the citation it decided on"

    def test_the_check_kind_follows_whether_the_item_quotes_its_region(self, world) -> None:
        package, audit = _clean_baseline(world)
        by_identity = {check.identity: check for check in audit.checks}
        for entry in package.items:
            check = by_identity[entry.item.identity]
            if entry.item.verbatim:
                assert check.check == "excerpt", (
                    "a quotation is verified by re-reading the location"
                )
            else:
                assert check.check == "source", "a view is verified by the source file's hash"
                assert "view" in check.detail.lower()
        kinds = {check.check for check in audit.checks}
        assert kinds == {"excerpt", "source"}, (
            "the corpus exercises both kinds, and both are honest"
        )

    def test_the_report_is_ordered_by_item_identity_and_counts_tally(self, world) -> None:
        package, audit = _clean_baseline(world)
        identities = [check.identity for check in audit.checks]
        assert identities == sorted(identities)
        assert sum(audit.counts.values()) == len(audit.checks)
        assert audit.package_identity == package.identity


# ============================================================================ mutation forensics
class TestMutationForensics:
    def test_a_mutated_source_is_a_mismatch_while_the_row_still_verifies(self, world) -> None:
        """The gap P11 closes: the row is still authoritative, the citation is no longer true."""
        package, audit = _clean_baseline(world)
        assert audit.all_verified
        target = _cited_file(world.ws, audit, ".txt")
        original = target.read_text(encoding="utf-8")
        target.write_text(original + "\nappended after extraction\n", encoding="utf-8")
        try:
            repacked = world.query(topics=(MUD_TOPIC,))
            # Retrieval re-reads the authoritative rows: none of them moved, so the package is
            # byte-for-byte the same address.  The citation state, however, must have changed.
            assert repacked.identity == package.identity
            again = world.auditor.audit(repacked)
            assert not again.all_verified
            broken = {
                check.citation.split(" > ")[0]
                for check in again.checks
                if check.status == STATUS_MISMATCH
            }
            assert broken == {target.name}, "exactly the items citing the mutated file are broken"
            for check in again.checks:
                if check.status == STATUS_MISMATCH:
                    assert check.check == "source"
                    assert "changed" in check.detail.lower()
                    assert check.expected_sha256 and check.actual_sha256
                    assert check.expected_sha256 != check.actual_sha256
                    # The expected hash is the one the extraction recorded, not the auditor's invention.
                    item = next(e.item for e in repacked.items if e.item.identity == check.identity)
                    assert check.expected_sha256 == str(item.provenance.get("source_sha256"))
        finally:
            target.write_text(original, encoding="utf-8")

    def test_restoring_the_source_restores_verification(self, world) -> None:
        _, audit = _clean_baseline(world)
        target = _cited_file(world.ws, audit, ".txt")
        original = target.read_text(encoding="utf-8")
        target.write_text(original + "\ntampered\n", encoding="utf-8")
        tampered = world.auditor.audit(world.query(topics=(MUD_TOPIC,)))
        assert tampered.counts[STATUS_MISMATCH] > 0
        target.write_text(original, encoding="utf-8")
        restored = world.auditor.audit(world.query(topics=(MUD_TOPIC,)))
        assert restored.counts == audit.counts
        assert restored.all_verified is True

    def test_a_deleted_source_is_unreadable_not_silent(self, world) -> None:
        _, audit = _clean_baseline(world)
        target = _cited_file(world.ws, audit, ".txt")
        original = target.read_text(encoding="utf-8")
        target.unlink()
        try:
            again = world.auditor.audit(world.query(topics=(MUD_TOPIC,)))
            gone = [check for check in again.checks if check.status == STATUS_UNREADABLE]
            assert {check.citation.split(" > ")[0] for check in gone} == {target.name}
            for check in gone:
                assert "not reachable" in check.detail or "unreadable" in check.detail
            assert not again.all_verified
            # The other files still verify - the audit localises the damage.
            assert again.counts[STATUS_MATCH] > 0
        finally:
            target.write_text(original, encoding="utf-8")

    def test_the_audit_does_not_depend_on_item_order(self, world) -> None:
        package, audit = _clean_baseline(world)
        shuffled = EvidencePackage(
            identity=package.identity,
            request=dict(package.request),
            items=tuple(reversed(package.items)),
            coverage=tuple(reversed(package.coverage)),
            policy=package.policy,
            scope=dict(package.scope),
        )
        assert world.auditor.audit(shuffled).to_dict() == audit.to_dict()


# ============================================================================ structured rows
class TestStructuredCitations:
    def test_a_structured_row_citing_documents_verifies_through_its_evidence(self, world) -> None:
        package = world.query(topics=("stuck",))
        structured = _structured_items(package)
        assert structured, "the corpus NPT/problem rows must answer 'stuck'"
        with_citations = [
            item for item in structured if isinstance(item.provenance, list) and item.provenance
        ]
        assert with_citations
        audit = world.auditor.audit(package)
        by_identity = {check.identity: check for check in audit.checks}
        for item in with_citations:
            check = by_identity[item.identity]
            assert check.status == STATUS_MATCH
            assert check.check in {"excerpt", "source"}
            filename = str(item.provenance[0].get("filename"))
            assert check.citation.startswith(filename)

    def test_a_rendered_excerpt_of_a_verified_file_is_not_reported_broken(self, world) -> None:
        """The defect the first audit run exposed: a table cited as a whole is excerpted as its
        rendered rows, so the recorded excerpt is a rendering, not a raw slice of the file.  With
        the file verified by hash, that is not a broken citation - and the audit must not report
        it as one.
        """
        package = world.query(topics=("stuck",))
        audit = world.auditor.audit(package)
        structured_checks = [c for c in audit.checks if c.source_type == "structured"]
        assert structured_checks
        rendered = [c for c in structured_checks if c.check == "source" and "rendering" in c.detail]
        assert rendered, "the corpus cites tables as rendered rows; the audit must say so"
        for check in rendered:
            assert check.status == STATUS_MATCH
            assert check.expected_sha256 == check.actual_sha256
        assert audit.all_verified is True

    def test_a_structured_row_without_a_file_citation_is_not_checkable_not_passed(
        self, world
    ) -> None:
        """A manually entered row cites the row itself; the audit says so, rather than inventing a file check."""
        with world.ws.database.session() as session:
            LessonRepository(session).capture(
                lesson="Zebrafishing line: keep the zebrafishing tension gauge in the sight line.",
                title="Zebrafishing tension",
                well_id=world.ids["A3"],
                field_id=world.ids["field"],
                provenance=[],
            )
            session.commit()
        world.search.rebuild()
        package = world.query(topics=("zebrafishing",))
        structured = _structured_items(package)
        assert structured, "the manually captured row must be retrievable"
        assert all(item.provenance == [] for item in structured)
        audit = world.auditor.audit(package)
        by_identity = {check.identity: check for check in audit.checks}
        for item in structured:
            check = by_identity[item.identity]
            assert check.status == STATUS_NOT_CHECKABLE
            assert check.check == ""
            assert "no recorded file citation" in check.detail
        assert audit.all_verified is True, "an honest NOT_CHECKABLE is not a broken citation"

    def test_a_broken_second_citation_folds_the_row_to_the_worst(self, world) -> None:
        from drilling_intelligence.database.models import ProblemOccurrence

        rows = []
        with world.ws.database.read_only() as session:
            rows = list(session.execute(select(ProblemOccurrence)).scalars().all())
        cited = next(row for row in rows if row.provenance and row.provenance[0].get("locator"))
        original = [dict(entry) for entry in cited.provenance]
        corrupted = dict(original[0])
        corrupted["source_sha256"] = "0" * 64  # a hash no file will carry
        with world.ws.database.session() as session:
            row = session.get(ProblemOccurrence, cited.id)
            row.provenance = [*original, corrupted]
            session.commit()
        try:
            package = world.query(topics=("stuck",))
            item = next(
                entry.item
                for entry in package.items
                if entry.item.source_type == "structured" and entry.item.source_id == str(cited.id)
            )
            assert len(item.provenance) == 2
            audit = world.auditor.audit(package)
            check = next(check for check in audit.checks if check.identity == item.identity)
            assert check.status == STATUS_MISMATCH, (
                "the fold takes the worst of the row's citations"
            )
            assert check.check == "source"
            assert "MATCH" in check.detail and "MISMATCH" in check.detail
            assert not audit.all_verified
        finally:
            with world.ws.database.session() as session:
                row = session.get(ProblemOccurrence, cited.id)
                row.provenance = original
                session.commit()


# ============================================================================ explicit states
class TestExplicitStates:
    def _item_with(self, package: EvidencePackage, **overrides: Any) -> EvidenceItem:
        base = package.items[0].item
        return EvidenceItem(**{**base.__dict__, **overrides})

    def _package_with(self, world, package: EvidencePackage, item: EvidenceItem) -> EvidencePackage:
        return EvidencePackage(
            identity=package.identity,
            request=dict(package.request),
            items=(PackageEvidence(item, ("audit",)),),
            coverage=(),
            policy=package.policy,
            scope=dict(package.scope),
        )

    def test_a_citation_at_a_version_missing_from_the_registry_is_not_checkable(
        self, world
    ) -> None:
        package, _ = _clean_baseline(world)
        provenance = dict(package.items[0].item.provenance)
        provenance["document_version_id"] = "ver-" + "0" * 32
        item = self._item_with(package, provenance=provenance)
        audit = world.auditor.audit(self._package_with(world, package, item))
        assert len(audit.checks) == 1
        check = audit.checks[0]
        assert check.status == STATUS_NOT_CHECKABLE
        assert "no longer in the registry" in check.detail

    def test_a_malformed_stored_provenance_is_not_checkable_not_a_crash(self, world) -> None:
        package, _ = _clean_baseline(world)
        item = self._item_with(
            package, provenance={"locator": "lines 1-5", "document_version_id": ""}
        )
        audit = world.auditor.audit(self._package_with(world, package, item))
        assert audit.checks[0].status == STATUS_NOT_CHECKABLE
        assert "unreadable" in audit.checks[0].detail

    def test_an_item_without_any_provenance_is_not_checkable_not_passed(self, world) -> None:
        package, _ = _clean_baseline(world)
        item = self._item_with(package, provenance={})
        audit = world.auditor.audit(self._package_with(world, package, item))
        assert audit.checks[0].status == STATUS_NOT_CHECKABLE
        assert audit.all_verified is True

    def test_an_empty_package_audits_to_an_empty_verified_report(self, world) -> None:
        package = EvidencePackage(
            identity="evpkg:" + "0" * 64, request={"topics": []}, items=(), coverage=()
        )
        audit = world.auditor.audit(package)
        assert audit.checks == ()
        assert audit.counts == {
            STATUS_MATCH: 0,
            STATUS_MISMATCH: 0,
            STATUS_UNREADABLE: 0,
            STATUS_NOT_CHECKABLE: 0,
        }
        assert audit.all_verified is True
        payload = audit.to_dict()
        assert payload["checks"] == [] and payload["package_identity"] == package.identity
        json.dumps(payload)


# ============================================================================ determinism and safety
class TestDeterminismAndSafety:
    def test_the_audit_is_byte_deterministic(self, world) -> None:
        package = world.query(topics=(MUD_TOPIC,))
        first = world.auditor.audit(package).to_dict()
        second = world.auditor.audit(package).to_dict()
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_the_audit_issues_one_batched_authoritative_query(self, world) -> None:
        package, _ = _clean_baseline(world)
        assert package.count >= 5, "the bound only means something with several items"
        count = _select_count(world.ws.database.engine, lambda: world.auditor.audit(package))
        assert count == 1, (
            "the whole package's authoritative read is one IN query, not one per item"
        )

    def test_the_audit_is_read_only(self, world) -> None:
        package, _ = _clean_baseline(world)
        fingerprint_before = _db_fingerprint(world.ws)
        files_before = _workspace_file_hashes(world.ws)
        world.auditor.audit(package)
        world.auditor.audit(package)
        assert _db_fingerprint(world.ws) == fingerprint_before
        assert _workspace_file_hashes(world.ws) == files_before, (
            "the auditor re-reads files, never rewrites them"
        )


# ============================================================================ CLI
class TestCommandLine:
    def _call(self, world, *argv: str) -> tuple[int, str]:
        import sys

        from drilling_intelligence.cli.app import main

        out, err = StringIO(), StringIO()
        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            return int(
                main(["evidence", "query", "--workspace", str(world.ws.root), *argv])
            ), out.getvalue()
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err

    def test_the_cli_reports_the_audit(self, world) -> None:
        code, out = self._call(world, "--topic", MUD_TOPIC, "--verify", "--json")
        assert code == 0
        doc = json.loads(out)
        assert doc["count"] >= 1
        audit = doc["audit"]
        assert audit["package_identity"] == doc["identity"]
        assert audit["all_verified"] is True
        assert audit["counts"][STATUS_MATCH] == doc["count"]
        assert len(audit["checks"]) == doc["count"]
        for check in audit["checks"]:
            assert check["status"] in {
                STATUS_MATCH,
                STATUS_MISMATCH,
                STATUS_UNREADABLE,
                STATUS_NOT_CHECKABLE,
            }
            assert check["identity"]

    def test_the_cli_prints_the_audit_section_in_text_mode(self, world) -> None:
        code, out = self._call(world, "--topic", MUD_TOPIC, "--verify")
        assert code == 0
        assert "citations:" in out
        assert "citation check(s):" in out
        assert "verified" in out

    def test_the_cli_without_verify_does_not_audit(self, world) -> None:
        code, out = self._call(world, "--topic", MUD_TOPIC, "--json")
        assert code == 0
        assert "audit" not in json.loads(out)
        _, out_text = self._call(world, "--topic", MUD_TOPIC)
        assert "citations:" not in out_text
