"""V2 static coverage and promotion-contract guard tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from drilling_intelligence.core.enums import DocumentClassification
from drilling_intelligence.operations.contracts import (
    CONTRACTS,
    CoverageLevel,
    PromotionOutcome,
    contract_registry,
    promotion_contract,
)
from drilling_intelligence.operations.promote import PromotionResult, promotion_identity


def test_every_taxonomy_member_has_one_explicit_contract() -> None:
    assert tuple(contract.classification for contract in contract_registry()) == tuple(
        DocumentClassification
    )
    assert set(CONTRACTS) == set(DocumentClassification)
    assert all(contract.contract_id.startswith("document:") for contract in CONTRACTS.values())


def test_only_registered_handlers_are_domain_promotable() -> None:
    handlers = {
        contract.classification: contract.handler
        for contract in CONTRACTS.values()
        if contract.domain_promotable
    }
    assert handlers == {
        DocumentClassification.DRILLING_PROGRAM: "program",
        DocumentClassification.DDR: "report",
        DocumentClassification.NPT: "report",
        DocumentClassification.TIME_BREAKDOWN: "report",
        DocumentClassification.MUD_REPORT: "mud_report",
        DocumentClassification.BHA_REPORT: "bha_report",
        DocumentClassification.BIT_RECORD: "bit_record",
        DocumentClassification.DIRECTIONAL_SURVEY: "directional_survey",
        # V7: cost is a real writer now, not an evidence-only class.  It targets the existing
        # ``cost_item`` record rather than a new table.
        DocumentClassification.COST: "cost",
        # V7.1: casing and cement each get their own table, because neither has an existing one that
        # means the same thing - a WellSection is a plan, and there was no cement record at all.
        DocumentClassification.CASING_REPORT: "casing",
        DocumentClassification.CEMENT_REPORT: "cement",
    }


def test_the_v4_writers_are_versioned_rather_than_silently_reattached() -> None:
    """A new domain writer gets a new contract revision; an existing one keeps its id.

    ``contract_id`` is what an operator's audit trail cites.  Attaching the V4 writers to the V2 id
    would make an old certification read as if it had covered them, so each is explicitly revisioned
    and the four V2 ids are unchanged.
    """
    revisions = {
        contract.classification: contract.contract_revision
        for contract in contract_registry()
        if contract.domain_promotable
    }
    assert revisions == {
        DocumentClassification.DRILLING_PROGRAM: "v2",
        DocumentClassification.DDR: "v2",
        DocumentClassification.NPT: "v2",
        DocumentClassification.TIME_BREAKDOWN: "v2",
        DocumentClassification.MUD_REPORT: "v3",
        DocumentClassification.BHA_REPORT: "v4",
        DocumentClassification.BIT_RECORD: "v4",
        DocumentClassification.DIRECTIONAL_SURVEY: "v4",
        # A new writer is revisioned rather than reattached to an existing id, so an audit trail
        # citing ``document:COST:promotion:v7`` cannot be read as an earlier certification.
        DocumentClassification.COST: "v7",
        DocumentClassification.CASING_REPORT: "v7",
        DocumentClassification.CEMENT_REPORT: "v7",
    }
    # Eleven rather than eight: V7 added cost and V7.1 added casing and cement, each both
    # domain-promotable and end-to-end certified.  These counts are deliberately absolute so that
    # admitting a classification cannot happen without a reviewer changing a number here.
    assert sum(1 for contract in contract_registry() if contract.domain_promotable) == 11
    assert (
        sum(
            1
            for contract in contract_registry()
            if contract.level == CoverageLevel.END_TO_END_CERTIFIED
        )
        == 11
    )


def test_a_promoted_domain_names_the_tables_it_is_allowed_to_write() -> None:
    """Every writer declares its destination tables, so a new table cannot appear by accident."""
    for contract in contract_registry():
        if not contract.domain_promotable:
            assert contract.target_models == (), contract.classification
            continue
        assert contract.target_models, contract.classification
        assert contract.required_evidence, contract.classification
    assert all(
        contract.level in {CoverageLevel.END_TO_END_CERTIFIED, CoverageLevel.DOMAIN_PROMOTABLE}
        for contract in CONTRACTS.values()
        if contract.domain_promotable
    )


def test_evidence_only_classes_are_not_a_fallback_domain_writer() -> None:
    for classification in (
        # COST was in this list until V7, when it gained a deterministic writer.  It is
        # deliberately not replaced here by another classification: the point of the list is that
        # these specific classes are not a fallback domain writer.
        DocumentClassification.PROCEDURE,
        DocumentClassification.LESSON_LEARNED,
        DocumentClassification.OTHER,
    ):
        contract = promotion_contract(classification)
        assert contract is not None
        assert not contract.domain_promotable
        assert contract.handler == ""


def test_manifest_names_the_same_certified_contracts() -> None:
    manifest_path = Path(__file__).parents[1] / "golden_corpus" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        contract = promotion_contract(entry["classification"])
        expected = entry["contract"]
        assert (
            contract.contract_id if contract is not None and contract.domain_promotable else None
        ) == expected, entry["filename"]
        if entry["expected"] != "UNSUPPORTED":
            # A promoted case must name the tables its writer is allowed to write.
            assert contract is not None and contract.domain_promotable, entry["filename"]
            assert set(entry["domain_models"]) <= set(contract.target_models), entry["filename"]
        elif entry.get("denied_by") == "source-shape":
            # The classification *has* a writer; this artefact does not satisfy its source contract.
            assert contract is not None and contract.domain_promotable, entry["filename"]
            assert entry["domain_models"] == [], entry["filename"]
        else:
            # No writer is registered for the classification at all.
            assert contract is not None and not contract.domain_promotable, entry["filename"]


def test_version_outcome_taxonomy_does_not_collapse_refusals_into_empty_success() -> None:
    cases = (
        (PromotionResult(outcome=PromotionOutcome.UNSUPPORTED.value), PromotionOutcome.UNSUPPORTED),
        (
            PromotionResult(error="NO_ARTEFACT"),
            PromotionOutcome.MISSING_ARTEFACT,
        ),
        (PromotionResult(error="NO_WELL"), PromotionOutcome.MISSING_WELL),
        (
            PromotionResult(skipped=[{"reason": "MISSING_PROVENANCE", "detail": "locator"}]),
            PromotionOutcome.MISSING_PROVENANCE,
        ),
        (
            PromotionResult(skipped=[{"reason": "AMBIGUOUS_SECTIONS", "detail": "two"}]),
            PromotionOutcome.AMBIGUOUS,
        ),
        (
            PromotionResult(skipped=[{"reason": "NO_SECTION_STATED", "detail": "none"}]),
            PromotionOutcome.INVALID_FIELDS,
        ),
    )
    for result, expected in cases:
        assert result.finalize() == expected.value

    promoted = PromotionResult()
    promoted.bump("npt", "created")
    assert promoted.finalize() == PromotionOutcome.PROMOTED.value
    unchanged = PromotionResult()
    unchanged.bump("npt", "unchanged")
    assert unchanged.finalize() == PromotionOutcome.UNCHANGED.value
    conflict = PromotionResult()
    conflict.bump("npt", "conflict")
    assert conflict.finalize() == PromotionOutcome.CONFLICT.value
    assert PromotionResult().finalize() == PromotionOutcome.ELIGIBLE.value


class TestChildIdentityIsContentAndLocationNotPosition:
    """Ledger row 26: child identity comes from the authoritative key, and its contract is explicit.

    ``promotion_identity`` is documented as content-addressed over *what the source said and where
    it said it* - so ``row_index`` is part of the key by design, not an accident of iteration.  That
    has a consequence worth pinning rather than discovering later: two children with identical
    visible payload at different source positions are **different** children, and re-presenting the
    same rows in a different order yields different keys.  Under a location-addressed contract that
    is the correct answer, and ``replace=True`` is what keeps the database consistent with it.  What
    must never happen is the opposite failure - collapsing distinct children because they look alike.
    """

    @staticmethod
    def _key(**kwargs: Any) -> str:
        base = {
            "version_id": "ver-1",
            "kind": "operations",
            "table_id": "tbl-1",
            "row_index": 3,
            "well_id": "well-1",
            "extra": "",
        }
        base.update(kwargs)
        return promotion_identity(**base)

    def test_the_same_line_promoted_twice_is_the_same_child(self) -> None:
        assert self._key() == self._key(), "re-promotion must be a no-op, not a new row"

    def test_identical_payload_at_different_positions_stays_distinct(self) -> None:
        first = self._key(row_index=3)
        second = self._key(row_index=4)
        assert first != second, (
            "two children that look identical are still two children when the source states them "
            "on different lines - collapsing them would silently lose a row"
        )

    def test_a_moved_value_changes_the_key_because_location_is_part_of_the_contract(self) -> None:
        """The documented reason row_index is in the key: an extraction that moved is a different
        statement about the source, and pretending otherwise would hide the move."""
        assert self._key(row_index=3) != self._key(row_index=9)

    def test_every_component_of_the_key_actually_contributes(self) -> None:
        """No component may be decorative - a key that ignores well_id would merge two wells."""
        base = self._key()
        for field, other in (
            ("version_id", "ver-2"),
            ("kind", "mud"),
            ("table_id", "tbl-2"),
            ("row_index", 4),
            ("well_id", "well-2"),
            ("extra", "x"),
        ):
            assert self._key(**{field: other}) != base, f"{field} does not contribute to identity"

    def test_the_per_child_suffix_makes_four_rows_four_identities(self) -> None:
        """One source line writes up to four child rows; the suffix is what keeps them apart."""
        line = self._key()
        keys = {line + suffix for suffix in (":op", ":ev", ":npt", ":problem")}
        assert len(keys) == 4, "the suffixes must not collide"
        assert all(key.startswith(line) for key in keys)

    def test_identity_is_not_a_uuid_and_not_a_substring_of_one(self) -> None:
        key = self._key()
        assert key.startswith("promote:"), "the prefix names the scheme, so a reader can tell it"
        body = key.split(":", 1)[1]
        assert len(body) == 32 and all(c in "0123456789abcdef" for c in body)
        assert self._key() == key, "a random uuid would not survive a second call"
