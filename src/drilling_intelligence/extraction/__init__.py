"""Document extraction: deterministic, provenance-preserving normalisation.

No extractor here touches the database, classifies a document or calls an LLM;
they turn bytes into a :class:`NormalizedDocument` whose every paragraph, table
cell and extracted field carries a locator back to the source file.
"""

from .docx import DocxExtractor
from .excel import ExcelExtractor
from .fields import COMPARE_UNITS, WELL_QUANTITIES, DataField, FieldExtractor, known_field_names
from .interfaces import (
    DocumentComplexity,
    DocumentExtractor,
    ExtractionContext,
    ProvenanceBuilder,
    new_provenance_builder,
    stamp_extraction,
)
from .normalized import (
    ExtractionMetadata,
    Figure,
    NormalizedDocument,
    Page,
    Paragraph,
    Section,
    Table,
    structure_digest,
)
from .pdf_text import PdfTextExtractor
from .registry import build_default_router, default_extractors
from .router import CandidateRecord, DocumentRouter, ExtractorChoice
from .text import TextExtractor

__all__ = [
    "COMPARE_UNITS",
    "WELL_QUANTITIES",
    "CandidateRecord",
    "DataField",
    "DocumentComplexity",
    "DocumentExtractor",
    "DocumentRouter",
    "DocxExtractor",
    "ExcelExtractor",
    "ExtractionContext",
    "ExtractionMetadata",
    "ExtractorChoice",
    "FieldExtractor",
    "Figure",
    "NormalizedDocument",
    "Page",
    "Paragraph",
    "PdfTextExtractor",
    "ProvenanceBuilder",
    "Section",
    "Table",
    "TextExtractor",
    "build_default_router",
    "default_extractors",
    "known_field_names",
    "new_provenance_builder",
    "stamp_extraction",
    "structure_digest",
]
