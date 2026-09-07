"""Deterministic document classification (no model calls).

The classifier is deliberately rule-based: a document type must be explainable
from the evidence in the file, and an LLM is only ever allowed to *propose*
labels that this module can validate against the same taxonomy.
"""

from ..core.enums import DocumentClassification
from .rules import WEAK_CONFIDENCE, WEIGHTS, ClassificationResult, DeterministicClassifier, Evidence
from .taxonomy import TAXONOMY, TypeSignature, authority_for

__all__ = [
    "TAXONOMY",
    "WEAK_CONFIDENCE",
    "WEIGHTS",
    "ClassificationResult",
    "DeterministicClassifier",
    "DocumentClassification",
    "Evidence",
    "TypeSignature",
    "authority_for",
]
