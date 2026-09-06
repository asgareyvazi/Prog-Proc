"""Field extraction against the real corpus: what we read must be what the document says.

Every expectation comes from ``tests.fixtures.generate`` (``GROUND_TRUTH`` /
``NEGATIVE_TRUTH``), so a value is asserted *present* because the fixture wrote it there,
and *absent* because the fixture deliberately placed it where it must not be read from (a
limit, a dynamic density, another hole section).  The negative half is what catches a
greedy regex, and it is the half that keeps a fabricated number out of the program.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from tests.fixtures.generate import GROUND_TRUTH, NEGATIVE_TRUTH

from drilling_intelligence.config.settings import Settings
from drilling_intelligence.extraction.fields import FieldExtractor
from drilling_intelligence.extraction.interfaces import ExtractionContext
from drilling_intelligence.extraction.registry import build_default_router

pytestmark = pytest.mark.engineering


def _router(tmp_path_factory) -> object:
    config = tmp_path_factory.mktemp("cfg") / "settings.toml"
    config.write_text('[mineru]\nmode = "disabled"\n', encoding="utf-8")
    return build_default_router(Settings.load(config))


def _extract(router, path: Path):
    document, _choice, _extractor = router.extract(
        ExtractionContext(
            path=path,
            filename=path.name,
            sha256="",
            extension=path.suffix,
            size_bytes=path.stat().st_size,
        )
    )
    return document


# Extracting the corpus costs a second or two; every test in this module reads the same
# artefacts, so they are parsed once and shared.  A cache keyed by the corpus directory
# keeps the fixture function-scoped (``corpus_dir`` builds into ``tmp_path``) without
# paying for a module-scoped fixture scope mismatch.
_DOCUMENTS: dict[Path, dict[str, object]] = {}


@pytest.fixture
def documents(corpus_dir: Path, tmp_path_factory) -> dict[str, object]:
    cached = _DOCUMENTS.get(corpus_dir)
    if cached is not None:
        return cached
    router = _router(tmp_path_factory)
    parsed = {path.name: _extract(router, path) for path in sorted(corpus_dir.iterdir())}
    _DOCUMENTS[corpus_dir] = parsed
    return parsed


@pytest.fixture
def fields_by_document(documents) -> dict[str, dict[str, list]]:
    gathered: dict[str, dict[str, list]] = {}
    for name, document in documents.items():
        per_field: dict[str, list] = {}
        for field in FieldExtractor().apply(document):
            per_field.setdefault(field.name, []).append(field.value)
        gathered[name] = per_field
    return gathered


@pytest.mark.parametrize(
    ("field", "document"),
    [(field, document) for field, truth in GROUND_TRUTH.items() for document in truth["files"]],
)
def test_value_written_in_the_document_is_found(
    field: str, document: str, fields_by_document
) -> None:
    truth = GROUND_TRUTH[field]
    values = fields_by_document.get(document, {}).get(field, [])
    assert values, f"{field} was not extracted from {document}"
    if isinstance(truth["value"], str):
        assert truth["value"] in values
        return
    assert any(
        isinstance(value, (int, float))
        and math.isclose(float(value), float(truth["value"]), rel_tol=1e-9)
        for value in values
    ), f"{field}: expected {truth['value']} in {values}"


@pytest.mark.parametrize(
    "case", NEGATIVE_TRUTH, ids=[f"{e['field']}@{e['file']}" for e in NEGATIVE_TRUTH]
)
def test_value_that_merely_looks_like_the_field_is_not_recorded(case, fields_by_document) -> None:
    values = fields_by_document.get(case["file"], {}).get(case["field"], [])
    for forbidden in case["forbidden"]:
        offending = [
            value
            for value in values
            if isinstance(value, (int, float)) and math.isclose(value, forbidden, rel_tol=1e-9)
        ]
        assert not offending, (
            f"{case['field']} in {case['file']} picked up {offending}: {case['why']}"
        )


def test_every_field_carries_its_unit_and_provenance(documents) -> None:
    document = documents["well_a3_program_rev12.pdf"]
    fields = FieldExtractor().apply(document)
    assert fields, "the program page should yield fields"
    for field in fields:
        assert field.name and field.value is not None
        assert field.provenance is not None, (
            f"{field.name}: a value without provenance cannot be audited"
        )
        assert field.provenance.ref.count(">") >= 1, f"{field.name}: provenance has no location"
        assert 0.0 < field.confidence <= 1.0
        assert field.method, "no method recorded"


def test_a_spreadsheet_label_becomes_the_canonical_field_key(documents) -> None:
    """A key/value row and the same number in prose must compete on the same key."""
    from drilling_intelligence.extraction.fields import canonical_field_name

    assert canonical_field_name("Mud weight (ppg)") == "mud_weight"
    assert canonical_field_name("Depth (MD, ft)") == "depth_md"
    assert canonical_field_name("Hole size (in)") == "hole_size_in"
    # A label that only starts with a field name describes something else.
    assert canonical_field_name("pressure test") is None
    assert canonical_field_name("Mud weight report date") is None
    mud = [
        field
        for field in FieldExtractor().apply(documents["mud_report_well-a3.xlsx"])
        if "mud_weight" in field.name
    ]
    assert mud, "the mud report has a mud weight row"
    assert any(field.name == "mud_weight" for field in mud), [field.name for field in mud]
