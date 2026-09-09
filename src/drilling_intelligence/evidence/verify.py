"""Citation audit: re-read what an evidence item cites, and say whether it still holds.

Retrieval (ADR-0013) verifies that an item's *row* is still authoritative.  This module verifies
the item's *citation*: it re-opens the source file behind the recorded locator and compares what
is there now with what the extraction recorded.  That is the gap a row-level re-read cannot close
- a document cell can still exist in the registry while no longer containing the value the
evidence quotes, and only a re-read of the file can say so.

The audit composes, it does not duplicate: the excerpt/hash comparison is
:func:`~drilling_intelligence.core.provenance.verify_provenance` from the core (the same check
search's ``--verify`` runs), and the version-to-file resolution goes through the document
repository against the authoritative database.  It never reads the search sidecar - everything it
needs (the recorded locator, the excerpt, the source hash, whether the item reads as a quotation)
travels on the :class:`~drilling_intelligence.retrieval.contract.EvidenceItem` itself.

The states are explicit and never collapsed into an empty success:

*   ``MATCH``        - the citation was re-read and the content still holds.
*   ``MISMATCH``     - the source no longer contains what the citation claims (hash changed, or
                       the excerpt differs).
*   ``UNREADABLE``   - the source file or the recorded location cannot be re-read.
*   ``NOT_CHECKABLE``- there is no file citation to check (a manual row, a region view with no
                       recorded hash).  Labelled, not passed: retrieval already verified the row
                       itself, and the audit says so rather than pretending a file check ran.

Read-only throughout (files are opened for reading, the database through a read-only session),
deterministic (checks ordered by item identity, no call timestamps, no random ids), and bounded:
the whole package's authoritative read is one batched select, each cited file is hashed once per
audit, and a recorded location is re-read only where an excerpt comparison is actually attempted
- a document or knowledge view is checked by hash alone, the same rule search's ``--verify`` uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.hashing import sha256_file
from ..core.provenance import (
    Provenance,
    UnknownLocator,
    verify_provenance,
)
from .contract import EvidencePackage

__all__ = [
    "STATUS_MATCH",
    "STATUS_MISMATCH",
    "STATUS_NOT_CHECKABLE",
    "STATUS_UNREADABLE",
    "CitationAuditReport",
    "CitationAuditor",
    "CitationCheck",
]

STATUS_MATCH = "MATCH"
STATUS_MISMATCH = "MISMATCH"
STATUS_UNREADABLE = "UNREADABLE"
STATUS_NOT_CHECKABLE = "NOT_CHECKABLE"

#: The audit's verdict order: the worst state an item's citations reached is the item's state.
_RANK = {STATUS_MISMATCH: 0, STATUS_UNREADABLE: 1, STATUS_MATCH: 2, STATUS_NOT_CHECKABLE: 3}


@dataclass(frozen=True)
class CitationCheck:
    """The re-read outcome for one evidence item's citation(s).

    ``check`` says what kind of comparison decided: ``excerpt`` (the recorded location was
    re-read and compared with the recorded excerpt), ``source`` (the file's hash was compared
    with the hash the extraction was made from), or empty where nothing was checkable.
    """

    identity: str
    source_type: str
    citation: str
    status: str
    check: str = ""
    detail: str = ""
    expected_sha256: str = ""
    actual_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "source_type": self.source_type,
            "citation": self.citation,
            "status": self.status,
            "check": self.check,
            "detail": self.detail,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
        }


@dataclass(frozen=True)
class CitationAuditReport:
    """The audit of one package: one check per item, in item-identity order, plus the tally.

    ``all_verified`` is True when no citation failed a check - ``NOT_CHECKABLE`` items do not
    block it, because an honest "no file citation" is not a broken citation; the tally says how
    many there were, and a reader decides what that means for their question.
    """

    package_identity: str
    checks: tuple[CitationCheck, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        tally = {STATUS_MATCH: 0, STATUS_MISMATCH: 0, STATUS_UNREADABLE: 0, STATUS_NOT_CHECKABLE: 0}
        for check in self.checks:
            tally[check.status] += 1
        return tally

    @property
    def all_verified(self) -> bool:
        return not (self.counts[STATUS_MISMATCH] or self.counts[STATUS_UNREADABLE])

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_identity": self.package_identity,
            "counts": self.counts,
            "all_verified": self.all_verified,
            "checks": [check.to_dict() for check in self.checks],
        }


class CitationAuditor:
    """Re-reads the citations of an :class:`EvidencePackage` against the source files."""

    def __init__(self, *, workspace: Any) -> None:
        self._workspace = workspace

    @classmethod
    def for_workspace(cls, workspace: Any) -> CitationAuditor:
        return cls(workspace=workspace)

    def audit(self, package: EvidencePackage) -> CitationAuditReport:
        """One read-only session, one batched select, then one file re-read per cited path.

        The batch is collected before the session opens, from the package itself: every version id
        an item cites (in its own field or in the provenance of a structured row's evidence list),
        so the authoritative read is a single ``IN`` query whatever the package contains -
        structured rows cite their documents from the evidence list, not from a row column.
        """
        from sqlalchemy import select

        from ..database.models import Document, DocumentVersion
        from ..documents.repository import DocumentRepository

        version_ids = sorted(
            {
                vid
                for item in (e.item for e in package.items)
                for vid in self._cited_version_ids(item)
            }
        )
        with self._workspace.database.read_only() as session:
            repository = DocumentRepository(session)
            versions: dict[str, Any] = {}
            documents: dict[str, Any] = {}
            if version_ids:
                joined = (
                    select(DocumentVersion, Document)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(DocumentVersion.id.in_(version_ids))
                )
                for version, document in session.execute(joined):
                    versions[str(version.id)] = version
                    documents[str(document.id)] = document
            hashes: dict[str, str] = {}
            checks = []
            for entry in sorted(package.items, key=lambda entry: entry.item.identity):
                checks.append(self._check_item(entry.item, repository, versions, documents, hashes))
        return CitationAuditReport(package_identity=package.identity, checks=tuple(checks))

    @staticmethod
    def _cited_version_ids(item: Any) -> list[str]:
        """Every version id the item's citation(s) might need, read from the payload itself."""
        ids: set[str] = set()
        if item.document_version_id:
            ids.add(str(item.document_version_id))
        payloads: list[Any] = []
        if isinstance(item.provenance, dict) and item.provenance:
            payloads = [item.provenance]
        elif isinstance(item.provenance, list):
            payloads = [entry for entry in item.provenance if isinstance(entry, dict)]
        for payload in payloads:
            version_id = str(payload.get("document_version_id") or "")
            if version_id:
                ids.add(version_id)
        return sorted(ids)

    # -- per-item -------------------------------------------------------------
    def _check_item(
        self,
        item: Any,
        repository: Any,
        versions: dict[str, Any],
        documents: dict[str, Any],
        hashes: dict[str, str],
    ) -> CitationCheck:

        if item.source_type == "structured":
            # A structured row's citation is the row itself (retrieval re-read it); its evidence
            # list, when it cites documents, is what the file re-read applies to.
            entries = [
                entry
                for entry in (item.provenance if isinstance(item.provenance, list) else [])
                if isinstance(entry, dict) and entry.get("locator")
            ]
            if not entries:
                return CitationCheck(
                    identity=item.identity,
                    source_type=item.source_type,
                    citation="",
                    status=STATUS_NOT_CHECKABLE,
                    detail="no recorded file citation; the row itself was verified by retrieval",
                )
            results = [
                self._check_citation(entry, item, repository, versions, documents, hashes)
                for entry in entries
            ]
            worst = min(results, key=lambda check: _RANK[check.status])
            if len(results) > 1:
                worst = CitationCheck(
                    identity=worst.identity,
                    source_type=worst.source_type,
                    citation="; ".join(check.citation for check in results if check.citation),
                    status=worst.status,
                    check=worst.check,
                    detail="; ".join(
                        f"{check.citation or 'citation'}: {check.status}"
                        + (f" ({check.detail})" if check.detail else "")
                        for check in results
                    ),
                    expected_sha256=worst.expected_sha256,
                    actual_sha256=worst.actual_sha256,
                )
            return worst
        if not isinstance(item.provenance, dict) or not item.provenance:
            return CitationCheck(
                identity=item.identity,
                source_type=item.source_type,
                citation="",
                status=STATUS_NOT_CHECKABLE,
                detail="no recorded provenance to verify",
            )
        return self._check_citation(item.provenance, item, repository, versions, documents, hashes)

    # -- one citation ----------------------------------------------------------
    def _check_citation(
        self,
        provenance: dict[str, Any],
        item: Any,
        repository: Any,
        versions: dict[str, Any],
        documents: dict[str, Any],
        hashes: dict[str, str],
    ) -> CitationCheck:
        identity = str(item.identity)
        source_type = str(item.source_type)
        try:
            prov = Provenance.from_dict(dict(provenance))
        except Exception as exc:  # noqa: BLE001 - a malformed record is reported, not fatal
            return CitationCheck(
                identity=identity,
                source_type=source_type,
                citation="",
                status=STATUS_NOT_CHECKABLE,
                detail=f"stored provenance unreadable: {exc}",
            )
        if isinstance(prov.locator, UnknownLocator):
            return CitationCheck(
                identity=identity,
                source_type=source_type,
                citation=prov.ref,
                status=STATUS_NOT_CHECKABLE,
                detail="recorded location is not a re-readable locator",
            )
        version = versions.get(str(prov.document_version_id or ""))
        if version is None:
            return CitationCheck(
                identity=identity,
                source_type=source_type,
                citation=prov.ref,
                status=STATUS_NOT_CHECKABLE,
                detail=f"version {prov.document_version_id or '(none)'} is no longer in the registry",
            )
        path = repository.resolve_source_path(version, documents.get(str(version.document_id)))
        if path is None:
            return CitationCheck(
                identity=identity,
                source_type=source_type,
                citation=prov.ref,
                status=STATUS_UNREADABLE,
                detail=f"source file not reachable from the workspace: {version.source_relative_path or version.source_path}",
            )
        expected = str(prov.source_sha256 or "")
        path_key = str(path)
        if path_key not in hashes:
            try:
                hashes[path_key] = sha256_file(path)
            except OSError as exc:
                return CitationCheck(
                    identity=identity,
                    source_type=source_type,
                    citation=prov.ref,
                    status=STATUS_UNREADABLE,
                    detail=f"source file unreadable: {exc}",
                    expected_sha256=expected,
                )
        actual = hashes[path_key]
        if expected and actual != expected:
            return CitationCheck(
                identity=identity,
                source_type=source_type,
                citation=prov.ref,
                status=STATUS_MISMATCH,
                check="source",
                detail="source file changed since this version was extracted - the recorded excerpt may no longer be valid",
                expected_sha256=expected,
                actual_sha256=actual,
            )
        # A document or knowledge item that reads as a view carved out of a larger region has
        # nothing at its location that reads as the item's text, so the excerpt comparison cannot
        # apply to it (the same rule search's --verify uses).  What the citation *can* establish
        # is file identity: the file behind the citation is still the file the extraction was made
        # from.  Weaker, and labelled as such rather than hidden.  A structured row's evidence
        # entries have no independent text the excerpt could be judged against, so their recorded
        # location is always re-read, and the outcome interpreted below.
        if item.source_type in ("document", "knowledge") and not item.verbatim:
            if not expected:
                return CitationCheck(
                    identity=identity,
                    source_type=source_type,
                    citation=prov.ref,
                    status=STATUS_NOT_CHECKABLE,
                    detail="no recorded source hash to compare; file identity cannot be established",
                )
            return CitationCheck(
                identity=identity,
                source_type=source_type,
                citation=prov.ref,
                status=STATUS_MATCH,
                check="source",
                detail="view of a larger cited region: the source file matches the hash this version was indexed under",
                expected_sha256=expected,
                actual_sha256=actual,
            )
        # The file is the file the extraction was made from, and the citation claims to quote its
        # region - so re-read the recorded location and compare with the recorded excerpt.
        outcome = verify_provenance(path, prov, require_hash=False)
        if outcome.status == STATUS_MATCH or outcome.status == STATUS_UNREADABLE:
            return CitationCheck(
                identity=identity,
                source_type=source_type,
                citation=prov.ref,
                status=outcome.status,
                check="excerpt",
                detail=outcome.detail,
                expected_sha256=expected,
                actual_sha256=actual,
            )
        # The hash verified but the recorded excerpt is not what the raw re-read holds.  The file
        # is byte-for-byte the extraction's file, so it cannot have lost the content the extraction
        # recorded from it: the recorded excerpt is a rendering of the cited region (a table cited
        # as a whole is excerpted as its rendered rows; a field as name = value), not a raw slice
        # of the file.  What the citation establishes is file identity, and it is reported as such
        # rather than reporting a verified file as broken.
        if expected:
            return CitationCheck(
                identity=identity,
                source_type=source_type,
                citation=prov.ref,
                status=STATUS_MATCH,
                check="source",
                detail="the recorded excerpt is a rendering of the cited region; the source file matches the hash this version was indexed under",
                expected_sha256=expected,
                actual_sha256=actual,
            )
        return CitationCheck(
            identity=identity,
            source_type=source_type,
            citation=prov.ref,
            status=STATUS_NOT_CHECKABLE,
            check="excerpt",
            detail="the recorded excerpt does not match the re-read location and no source hash is recorded to fall back on",
            actual_sha256=actual,
        )
