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
    }
    assert all(
        contract.level in {CoverageLevel.END_TO_END_CERTIFIED, CoverageLevel.DOMAIN_PROMOTABLE}
        for contract in CONTRACTS.values()
        if contract.domain_promotable
    )


def test_evidence_only_classes_are_not_a_fallback_domain_writer() -> None:
    for classification in (
        DocumentClassification.MUD_REPORT,
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
        ) == expected
        if entry["expected"] == "UNSUPPORTED":
            assert contract is not None and not contract.domain_promotable


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
