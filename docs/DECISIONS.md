# Architecture decisions

Records of decisions that the code already depends on. Each entry exists because
something in the tree cites it — a comment, a dependency block, a migration — so the
rule is: no dependency, schema or runtime choice changes without an ADR here.

Numbering is stable; ADR-0001 is reserved for the founding decision (a local-first,
well-centric platform where every extracted number cites its source), which is what the
project description in `README.md` restates.

---

## ADR-0002 — Python floor 3.11, product target 3.14

**Status:** accepted (2026-09-05)

**Context.** The platform has to run on engineer laptops and CI images that do not all
ship the same interpreter, while the product is targeted at the current CPython release
for speed and `typing` features.

**Decision.** Ship a floor of CPython 3.11 and treat 3.14 as the target runtime. Code
must not use syntax or stdlib features newer than 3.11 — which in practice means
`StrEnum`, `tomllib`, `X | None` unions and `datetime.UTC` are in, and anything newer is
deferred until the floor moves. `parse_decimal`, not `float()` on a locale-dependent
string, is why the floor matters at all: numeric parsing is centralised precisely so it
behaves identically on every interpreter we support.

**Consequences.** Two runtimes are exercised in CI as the floor moves. `sqlite3.Cursor`
gained context-manager support in 3.12, so a `connect` event handler that uses one
breaks on the floor (it did, and `database/session.py` now closes the cursor by hand).

## ADR-0003 — SQLite is the system of record; the index is disposable

**Status:** accepted (2026-09-05)

**Context.** Everything must work offline on a rig laptop with a folder of PDFs, XLSX and
DOCX files, no server, and no installer for a database daemon. Retrieval needs FTS5 and
vector search.

**Decision.** One SQLite file per workspace under `.drillintel/database/` is the source of
truth: documents, versions, extractions, provenance, knowledge, skills, calculations and
the audit trail. The search index is a **separate** SQLite file (`index/search_index.db`,
FTS5 plus `sqlite-vec`) that is rebuilt from the record database and therefore carries no
migrations and no authority.

**Consequences.** Backing up a workspace is copying a directory. A corrupt or stale index
costs a rebuild, not data. WAL mode and `PRAGMA foreign_keys=ON` are set per connection.
Multi-writer concurrency is out of scope by design; `busy_timeout` covers the realistic
case of a UI and a pipeline touching the same file.

## ADR-0004 — Alembic owns the schema; identifiers are portable

**Status:** accepted (2026-09-05)

**Context.** The platform starts on SQLite and may be deployed against PostgreSQL for a
shared project database. Silent `create_all()` in production is how schemas drift apart
from the code that reads them.

**Decision.** Alembic is the only writer of schema; `create_all()` exists for tests that
want a throwaway database, and `ensure_schema()` runs the migration chain when a
workspace is opened (idempotently — a second call reports `already-current`). A schema
change requires a revision *and* a note in this file. `alembic.ini` deliberately contains
no URL: the URL comes from settings, or from the engine handed to Alembic through
`config.attributes["engine"]`, so a migration always runs against the file the
application opens. Primary keys are application-generated strings (`core/ids.py`) rather
than dialect-specific serials, and offline mode (`upgrade --sql`) stays working so a DBA
can review a migration before running it.

**Consequences.** `migrations/env.py` is small and boring, which is the point. Drift is a
CI failure: `schema_diff()` compares live tables with ORM metadata, and a test asserts it
comes back empty.

## ADR-0005 — Own the AI seams; talk to Ollama over HTTP

**Status:** accepted (2026-09-05)

**Context.** Summarisation, clause extraction and embedding are useful, but they must be
strictly optional: the platform has to be fully functional with no model present, and a
model must never be able to invent an engineering number.

**Decision.** Define our own `LLMProvider`, `EmbeddingProvider` and `VectorStore`
protocols and implement them with `httpx` against a local Ollama. No heavyweight agent
or RAG framework is taken as a dependency; `chromadb` and `rank-bm25` were rejected
because SQLite already gives us both jobs with less to install and less to trust. AI
output is an *input to validation*, not a result: any proposed label or relation must
satisfy the same contracts the deterministic path does (e.g.
`DeterministicClassifier.validate` accepts only taxonomy members) and must carry
provenance or be discarded. `[ai] provider = "none"` is the default in development
configuration.

**Consequences.** Model swaps are a config change. Tests never need a model: the suite
runs the deterministic path and asserts the AI-free result is usable. The cost is that we
maintain small amounts of plumbing (timeouts, retries, embedding dimension checks) that a
framework would own.

## ADR-0006 — Document intelligence: thin, inspectable engines; MinerU optional

**Status:** accepted (2026-09-05)

**Context.** The corpus is real drilling paperwork: text PDFs, scanned PDFs, workbooks
with hidden sheets and formulas, Word reports with headings, CSV logs. The platform must
explain any extracted number, and a scanned PDF must be reported as unread rather than
guessed at. A general-purpose layout model is attractive but heavy, GPU-leaning, and
produces confident nonsense on images it cannot read.

**Decision.** Four small engines behind a router — PyMuPDF (text PDFs), openpyxl with a
double pass so formulas and cached values coexist, python-docx for an ordered body walk,
and a line-cited reader for text/CSV — each producing the same normalized artefact with
per-element provenance (page/block/bbox, sheet/cell, heading/paragraph, line range). All
extraction is rule-driven where a number is involved: a limit is not a design value, EMW
is not MW, and both are asserted in tests. MinerU is an **optional** engine for scans
only, invoked as a subprocess or over HTTP with `pipeline` (CPU) as the honest default
profile, with its own timeout and a `ParserUnavailableError` when absent. Extraction
confidence and the missing-text diagnostic are surfaced in the UI rather than hidden
behind a label.

Phase-0 domain model, in one sentence: a workspace contains wells; a document is an
identity (workspace-relative path) with content-addressed versions, exactly one current
version, supersede/duplicate links, and an extraction artefact with its provenance; a
run and an audit row record what happened and why. Removal from a folder is a *state* of
a document, never a deletion of its record.

**Consequences.** Zero model downloads for the whole ingestion path: the default install
is the parser, persistence and transport libraries declared in `pyproject.toml` and nothing
that needs a GPU. Anything a reviewer questions maps to a location in a
file, which is what makes the platform usable for engineering decisions. MinerU's licence
is Apache-2.0 with additional terms (attribution if offered as an online service; a
separate commercial licence only past 100M MAU or $20M/month, neither of which applies
here) — recorded so the next reader does not have to re-litigate it.
