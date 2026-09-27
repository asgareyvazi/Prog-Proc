"""V2 static coverage and promotion-contract guard tests."""

from __future__ import annotations

import json
from pathlib import Path

from drilling_intelligence.core.enums import DocumentClassification
from drilling_intelligence.operations.contracts import (
    CONTRACTS,
    CoverageLevel,
    PromotionOutcome,
    contract_registry,
    promotion_contract,
)
from drilling_intelligence.operations.promote import PromotionResult


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
    }
    assert sum(1 for contract in contract_registry() if contract.domain_promotable) == 8
    assert (
        sum(
            1
            for contract in contract_registry()
            if contract.level == CoverageLevel.END_TO_END_CERTIFIED
        )
        == 8
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
        DocumentClassification.COST,
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
