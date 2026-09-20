# V2 miniature / golden corpus

`manifest.json` is the compact certification index. The bytes are generated in a temporary directory by
`tests.fixtures.generate.build_corpus`; no binary fixture is checked in. This keeps the corpus reviewable,
reproducible and within the repository while exercising the real PDF, XLSX, DOCX, CSV and scan paths.

The same generated files are used by the ingestion, extraction, knowledge and operational promotion
suites. Expected values come from the builders and `GROUND_TRUTH`/`NEGATIVE_TRUTH`, not from a second
fixture-specific parser. The V2 contract tests verify that every manifest classification has the expected
static contract and that evidence-only files cannot become operational rows.

Run the focused certification set with:

```bash
python -m pytest -q \
  tests/integration/test_operations_promotion.py \
  tests/integration/test_program_promotion.py \
  tests/integration/test_knowledge_pipeline.py \
  tests/integration/test_extraction_cache.py
```
