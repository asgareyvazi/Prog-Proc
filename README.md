# Drilling Intelligence & Knowledge Platform

A local-first, well-centric workbench for drilling documents: it reads the PDFs,
workbooks, Word reports and CSV logs a project actually accumulates, keeps every
extracted number tied to the exact place it came from, classifies and versions the
files as they change, and answers questions with citations rather than guesses.

The premise is narrow on purpose. An engineering decision needs a *source*: not "the
summary says the mud weight is 10.2 ppg" but "cell B9 of the Summary sheet in
`mud_report_well-a3.xlsx`, revision 3, approved, which supersedes revision 2". Every
part of this repository serves that sentence.

## Status

Phase 0 — the deterministic document core. What exists and runs today:

| area | state |
| --- | --- |
| Document ingestion: scan, content-hash planning, incremental runs | **implemented, tested end to end** |
| Extraction: PDF (PyMuPDF), XLSX (openpyxl), DOCX (python-docx), text/CSV | **implemented, tested on a generated corpus of real files** |
| Field extraction with typed units and per-value provenance | **implemented, tested with positive *and* negative ground truth** |
| Classification over the taxonomy with evidence and confidence | **implemented, deterministic (no model calls)** |
| Registry: versions, supersede/duplicate links, revisions, status, audit trail | **implemented, tested** |
| Schema and migrations | **implemented** (Alembic owns the schema; SQLite per workspace) |
| Search index, knowledge graph, skills, AI providers, calculations, desktop UI | planned — see `docs/DECISIONS.md` for the constraints they inherit |

Nothing here needs a GPU, a model download, or a server. `mineru` (for scanned pages)
and Ollama (for optional AI) are both opt-in and absent by default.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
PYTHONPATH=src .venv/bin/python -m pytest            # 84 tests: unit, engineering, integration
.venv/bin/ruff check src tests --output-format=concise
```

Ingest a folder of documents into a workspace (this is the whole point of phase 0, so it
is worth running once on your own files):

```python
from pathlib import Path
from drilling_intelligence.config.settings import Settings
from drilling_intelligence.wells.workspace import Workspace
from drilling_intelligence.wells.repository import WellRepository
from drilling_intelligence.ingestion.pipeline import IngestionPipeline

settings = Settings.load(Path("configs/development.toml"))
ws = Workspace.create(Path("/tmp/well-a3"), settings, name="North Cormorant")

with ws.database.session() as session:
    repo = WellRepository(session)
    workspace_row = repo.get_or_create_workspace(str(ws.root), name="North Cormorant")
    project = repo.get_or_create_project("North Cormorant")
    well = repo.create_well("A-3", project_id=project.id)
    session.commit()

pipe = IngestionPipeline(settings=settings, workspace_root=ws.root, database=ws.database)
result = pipe.run(root=Path("/data/projects"), workspace_id=workspace_row.id, well_id=well.id)
print(result.counts)          # NEW / MODIFIED / UNCHANGED / DUPLICATE / REMOVED
for item in result.results:   # every file: what changed, what we read, how sure we are
    print(item.filename, item.change.value, item.classification, item.fields)
```

Run it twice: the second run does no work at all. Edit a file and run again: it becomes a
new version that supersedes the old one and keeps its well link. Delete a file: it is
reported as removed, and its document, provenance and audit history stay queryable.

## Layout

```
src/drilling_intelligence/
  core/           units, provenance and locators, hashing, errors, ids, logging
  config/         TOML settings with DRILLINTEL_<SECTION>_<KEY> overrides
  database/       ORM models, engine/session, Alembic integration
  extraction/     pdf_text, excel, docx, text, normalized artefact, field rules, router
  classification/ taxonomy and the deterministic classifier
  documents/      registry (identity, versions, revisions), repository, versioning
  wells/          workspace and well/project/company repositories
  ingestion/      scanner, planner (incremental decisions), pipeline
  integrations/   MinerU client (subprocess/HTTP), disabled by default
migrations/       Alembic chain; alembic.ini carries no URL by design
tests/            unit, engineering (real files), integration (real database)
docs/             DECISIONS.md - the ADRs the code cites
```

## Conventions this project keeps

- **Every extracted value cites a location.** A field without provenance is not stored.
  `core/provenance.verify_provenance()` re-reads the original file and says whether the
  record still matches.
- **Confidence is earned and doubt is visible.** A filename can hint, never prove; a page
  with no text layer reports that instead of guessing; a best score that is noise returns
  `OTHER`.
- **Deterministic where it matters.** Engineering numbers come from rules and
  calculations, not from a model. Any AI output is an input to validation, never a result.
- **The database is the record, the index is disposable.** Nothing in the search index is
  authoritative, so it is rebuilt rather than migrated.
- **No fake implementations.** If a subsystem is not built, it is absent from the tree —
  there are no stubs that return plausible-looking data.

## Licensing

Proprietary: an internal engineering platform for the drilling team. No public
distribution, no sublicense, and no warranty of fitness is offered; see the `license`
field in `pyproject.toml`. Third-party components keep their own terms — notably MinerU,
which is Apache-2.0 with additional conditions (attribution if it is ever offered to
third parties as an online service), and is not vendored or required by this repository.
`wellpathpy` (LGPL) is planned for a later phase behind a wrapper, per `docs/DECISIONS.md`.
