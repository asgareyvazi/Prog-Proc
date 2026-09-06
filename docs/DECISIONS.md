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

---

## ADR-0007 — The document core's invariants live in the schema, the repository and the checker

**Status:** accepted (2026-09-05)

**Context.** Phase-0 review found four places where a correct-looking system could store a
wrong one: the extraction cache was consulted only *after* the full parse (so it saved
nothing and could serve an artefact produced by a different extractor); `reprocess()` hashed
`document.sha256` instead of the file (so a changed file was recorded as unchanged);
"exactly one current version" was a convention with nothing behind it, and version numbers
were allocated by `max(version_number) + 1`, which two writers can both pick; a
workbook over its cell budget returned a partial sheet that looked complete, and
`st_ctime` was stored under the name `file_created_at`, which it is not on any platform.

**Decision.** Each invariant goes to the layer that can actually hold it:

*   **Routing before the cache.** `registry.register` builds a context, asks the router to
    `route` (a bounded probe: `extraction.pdf_probe_pages` pages, never a full parse), and
    only then looks the key up - `content_sha256` + extractor id + extractor version +
    config hash over the *options that change the artefact*. A hit copies the stored
    artefact and never enters an extractor; the routing decision is stored with the version
    either way, so provenance does not depend on the cache being warm.
*   **The cache is its own table.** `extraction_cache` holds one row per key
    (`uq_extraction_cache_key`) pointing at the `extraction` row to reuse, with
    `document_version_id` deliberately outside the key so one artefact may serve many
    versions; the write is a savepoint upsert, so the loser of a concurrent insert reuses
    the winner instead of failing the run.
*   **Current version, three ways.** A partial unique index
    (`unique (document_id) where is_current`) makes two current versions impossible; a real
    `deferrable` foreign key from `document.current_version_id` (rebuilt through
    `batch_alter_table` on SQLite, added directly elsewhere) makes a dangling pointer
    impossible in the same transaction; and `database.integrity.check_current_version_invariants`
    reports the three-table statement the schema cannot express - exposed as
    `DocumentRepository.check_current_version_invariants()` and
    `require_current_version_invariants()`, which the ingestion path and the migration tests
    call. Nothing here is SQLite-specific, because the system of record moves.
*   **Version numbers are claimed, not computed.** `create_version` allocates under the
    existing `(document_id, version_number)` unique constraint and retries on
    `IntegrityError` within a savepoint, bounded by `MAX_VERSION_NUMBER_ATTEMPTS`. Sequential
    numbering is preserved; nothing is loosened to avoid the race, and the supersede
    back-link is written by the repository so a caller cannot forget it.
*   **Sources of truth on disk.** `reprocess`/`register` hash the *file*; a missing or
    unreadable file is an error with no version written. The durable reference is the
    canonical workspace-relative path (`document_version.source_relative_path`, `/`-separated,
    normalised), with the absolute path kept for convenience only, so a relocated workspace
    still resolves and still verifies. `st_ctime` is `fs_metadata_changed_at`, documented as
    "metadata change" and never used as a document revision date; a genuine creation time is
    stored only where the platform reports one.
*   **Limits are reported, not hidden.** The Excel reader keeps `max_sheets`/`max_cells` and
    emits `EXTRACTION_TRUNCATED: max_cells=N in sheet ...`, storing `truncated`,
    `cells_read`/`cells_skipped` per sheet in the artefact metadata, so a partial extraction
    can never be mistaken for a complete one; the two-pass read is bounded by
    `excel_max_bytes` because openpyxl cannot give values and formulas in one load.
    A PDF probe that samples says so in `DocumentComplexity.reasons`.

**Consequences.** `tests/integration/test_extraction_cache.py` can assert the *performance*
property in the same breath as correctness (parser call counts on the second run), and the
invariant tests deliberately break the rules with raw SQL to prove the checker names each
failure mode. Migration 0002 repairs data before constraining it - highest version number
wins, pointer follows, relative paths backfilled from the document identity, cache entries
backfilled from the artefacts that already exist - and is round-trip tested against a
populated 0001 database, downgrade included.


---

## ADR-0008 — Knowledge is derived facts that cite their source; a conflict is data, never a decision

**Status:** accepted (2026-09-06)

**Context.** Ingestion could already answer "what did the corpus say", and that turned out not to be
the question anyone asks first. The question is "what is the mud weight in this hole", and three
ways of answering it are all wrong in different ways: keep the number in a summary field (it detaches
from the evidence and from any later revision), let the newest write win (a safety-relevant
discrepancy becomes a stale row nobody can find), or let a model read the documents (an answer nobody
can argue with, because it has no source). The knowledge layer also has to survive being wrong: a
parser that mis-reads a sheet must be fixable by re-deriving from what was stored, without re-running
extraction and without touching what a person typed.

**Decision.**

*   **Facts, not documents, are the unit of knowledge.** One fact is a subject, a predicate, the
    value as written, the value normalised, a validity window, a status and provenance. It is stored
    in ``knowledge_item``, which migration 0003 widens with those columns rather than adding a table
    per entity type - a well, a hole section, a bit and a document version are addressed by the same
    ``(entity_type, entity_id)`` pair a ``knowledge_relation`` edge carries, so facts and edges are
    walked together.
*   **The payload is the fact, the columns are its index, and the registry owns the lifecycle.**
    ``payload`` holds what the source said and is rewritten only by a write of the fact itself;
    ``status``/``superseded_by``/``origin``/the id columns are what the repository decided and are
    therefore read back from the columns (:meth:`KnowledgeFact.from_item`'s ``column`` helper), with
    ``set_status`` appending its explanation to ``payload["status_note"]``. Reading the payload first
    - which is what this layer did until the read paths were tested - printed a disputed value as
    ``ACTIVE``, and offered the retired side of a settled argument as the answer.
*   **Identity is content-addressed, so a rebuild is a no-op.** ``fact_id_for(version, lookup_key,
    original_value)`` keys a row on ``(document_version_id, subject_type:subject_id|property:P|state:PLANNED|ACTUAL,
    wording)``, and a write reports ``CREATED``/``UPDATED``/``UNCHANGED``. ``PLANNED`` never collides
    with ``ACTUAL``: a plan that differs from the record is not a contradiction, and a conflict list
    full of them is a list nobody reads.
*   **Derivation reads the stored artefact, never the file.** ``KnowledgeExtractionService`` takes
    ``extraction.document_json`` and turns its ``extracted_fields`` into facts - so a rebuild a year
    later gives the same answer, an offline workspace needs no MinerU, and no model is anywhere in
    the path. ``facts_for_payload`` is a pure function for the same reason: "what would this document
    assert" needs no database.
*   **Provenance is a storage invariant.** ``put_fact`` refuses an ``EXTRACTED`` fact with no
    provenance; a field the extractor left uncited is reported in ``SyncResult.warnings`` and
    quarantined in the artefact instead of being stored or dropped. ``MANUAL`` facts are the one case
    with no provenance, are never ``is_source_derived``, and are the one rows ``knowledge rebuild``
    leaves alone - which is what makes a repair command not a data-loss command.
*   **A subject is looked up, never invented.** One document classification describes exactly one
    entity type, and that table is checked for uniqueness at import. A document filed under a well
    asserts things about that well; one that names a well in a field asserts them about the well it
    named, with a ``DOCUMENT_MENTIONS_WELL`` edge carrying that field's provenance so the inference
    stays traceable; one that names nothing asserts them about the entity its kind describes, keyed
    deterministically to the version. Types that have a table of their own - a well, a hole section,
    a document - are refused a placeholder, because inventing one would put a second source of truth
    in front of the same name.
*   **A conflict needs two sources and is then data.** Detection compares values in canonical units
    through ``core.units``: same unit, tolerance is float noise; different units, tolerance is the
    precision the coarser source wrote (half its last decimal place, ceiling 2%), because
    "1222 kg/m3" and "10.2 ppg" are one mud and a platform that reported that as a dispute would be
    reporting a unit conversion. Two values inside one revision are ``ambiguous_within_source``, and
    so is a property every source states as the same *set* of values (a table with a depth per row) -
    counted, named, and not put in front of a reviewer as an argument. Otherwise every side is stored,
    every side is marked ``CONFLICTED``, and the conflict row records the candidates, the compare
    unit and the ranking basis. Nothing is chosen.
*   **Deciding is a separate, recorded act.** ``resolve`` is the only path that picks a side: the
    chosen fact becomes ``ACTIVE``, the others ``RETIRED`` (kept, citable), the conflict keeps its
    candidates as it was at that moment, an audit event names who decided, and the key is re-compared
    so the marking catches up. ``clear_conflict`` deletes only rows still ``OPEN`` - a row carrying a
    human decision is the record of that decision, and detection re-runs for many reasons.
*   **Only the current revision answers.** Reads hold ``SUPERSEDED`` and ``RETIRED`` back until a
    caller asks for history (``include_superseded``), the fact's ``revision`` is stamped from the
    registry's version number rather than trusted from the payload, and superseding keeps the
    document's *current* version answering - re-deriving an old revision must not move the answer
    backwards. ``status`` reports the two drift numbers a workspace can act on: current versions with
    no facts, and facts citing a version that is no longer current; it recommends a rebuild only
    when a rebuild would fix what it found.
*   **The index carries claims, not state.** Fact chunks are written by the same pass from the same
    authoritative rows, outrank prose, and render their locator through the one ``SourceLocator.ref``
    the document chunks use, so a hit and a fact listing cite identically. Lifecycle status is
    deliberately absent from indexed text: a marking pass does not rewrite the sidecar, and text that
    disagrees with the registry is worse than text that says less.

**Consequences.** A dispute is visible wherever a person might look: ``knowledge conflicts`` lists it,
``knowledge status`` counts it, and ``doctor`` - which checks that the structures agree, and found no
reason to care about an argument until now - reports an unresolved conflict as a finding and exits 1,
because a workspace where two sources disagree about a mud weight is not corrupt but is not sound
either. ``check_knowledge_relations`` keeps reporting the dangling edges a bad write would leave. ``--json`` renders a domain error
as a document with ``"ok": false`` and exit 1, because a script that asked for machine-readable output
must be able to read a failure too; ``--debug`` still raises, and a bug is never dressed up as data.
The migration backfills every pre-existing ``knowledge_item`` row as ``MANUAL`` and adds no
interpretation - it does not read predicates out of payloads, because that is derivation, and a SQL
statement would be a second, dumber implementation of it.

**Rejected.** Newest-write-wins and authority-ranked auto-resolution (both turn a discrepancy into an
invisible row); a graph database for the edges (a table, a unique constraint and a checker hold the
invariants, and the system of record has to stay one file); storing the ranking's *outcome* anywhere
but the audit trail; running a model over the corpus to fill gaps (an engineering value either comes
from a cell or it does not exist); and rewriting the Excel extractor to emit facts directly (the
artefact is the contract between extraction and knowledge, and both sides are testable because of it).
