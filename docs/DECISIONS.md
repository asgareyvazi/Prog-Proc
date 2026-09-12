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
DOCX files, no server, and no installer for a database daemon. Retrieval needs ranked
full-text search (with vector search as a deferred, optional extension).

**Decision.** One SQLite file per workspace under `.drillintel/database/` is the source of
truth: documents, versions, extractions, provenance, knowledge, skills, calculations and
the audit trail. The search index is a **separate** SQLite file (`index/search_index.db`,
BM25-ranked, with FTS5 as an optional candidate-acceleration pass when the SQLite build
provides it; vector search via `sqlite-vec` is not part of the index) that is rebuilt from
the record database and therefore carries no migrations and no authority.

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


---

## ADR-0009 — The engineering domain is tables in this database, not a second system next to it

**Status:** accepted (2026-09-06)

**Context.** Phase 1 asked for a persistent engineering core: reports, operations, NPT, problems,
programmes, procedures, lessons, risks, costs, rigs and service companies - queryable apart from the
document registry, and with the same evidence rule the knowledge layer already keeps. Three designs are
available and two of them are traps. Bolting columns onto `document` gives a table that is a spreadsheet
and a filing cabinet at once, and every engineering query becomes a query over files. Mirroring each
source format with its own model (`Well2`, `NptRow`, `ExcelDdr`) reproduces the duplication the platform
exists to remove. And building a second provenance mechanism - a `source_note` column on every table, or
a per-table citation format - means the same document supports a fact in one place and an occurrence in
another, with nothing forcing the two to agree.

**Decision.**

*   **One database, one metadata, one `Base`.** Migration 0004 adds sixteen tables to the same SQLite
    file: the operational spine (`ddr_report`, `well_operation`, `well_event`, `npt_record`,
    `problem_occurrence`), the versioned engineering record (`procedure_record`, `drilling_program`,
    `program_target`, `risk_record`), what was learnt (`lesson_learned`), what to do next
    (`best_practice`, `recommendation`, `field_pattern`), who and how much (`rig`, `service_company`,
    `cost_item`). There is no shadow model of anything: `Well` is the well, and `WellSection` is the hole
    section, and the new rows point at those ids. The generic engineering record is not among the sixteen -
    `calculation` and `calculation_input` arrived with 0001 - and migration 0005 brings it into this
    convention rather than inventing a second table beside it. **Amended (2026-09-06):** that amendment is
    ADR-0012.
*   **Evidence is inherited, not reimplemented.** Every record table carries
    `document_id`, `document_version_id`, a JSON `provenance` list, `origin` and `created_by`, and joins
    the existing graph through `knowledge_relation` rather than a parallel edge table - which is why
    `RELATION_ENDPOINT_MODELS` is the one registry of what an edge may point at, and why a table missing
    from it is a *rejected write* rather than a dangling edge. `check_promoted_evidence` closes the loop:
    a row that says it came from a document and cites nothing is reported. `Calculation` is registered in
    both places - it is a promoted-model endpoint of an edge, and its evidence columns are the ones 0005
    added - so the loop closes on engineering records with the same check and no extra machinery.
*   **The repository owns the write, the service owns the transaction, the CLI owns the argument
    parsing.** A repository method takes a `Session` and does not commit; a service method may commit
    because it owns the unit of work; a command is a thin wrapper over a service call and prints what that
    call returned. `records list` renders the same dictionaries `--json` prints, from the same
    repository query, so a number on a screen and a number in a pipe cannot disagree.
*   **The verbatim wording lives in the row, the interpretation in a column.** `report_date_text`,
    `npt_record.duration_text`, `cost_item.attributes["source_wording"]` and the vocabulary functions'
    `VocabMatch(raw=...)` exist so that "what did the file say" stays answerable after a classifier
    changes. A token nobody recognises is kept as `("rig_move", recognised=False)`, not discarded and not
    coerced to `other`.
*   **SQLite now, PostgreSQL later, by staying in the common subset.** Partial unique indexes carry the
    one-current-revision rules; no `ARRAY`, no `JSONB` operators, no enum types; `PRAGMA foreign_keys=ON`
    on connect makes the schema's foreign keys real here as well as there.
*   **Two documented deviations, stated rather than hidden.** `ddr_report.report_date` is nullable with
    `report_date_text` beside it: a daily report that writes "14 June 2025" in prose would otherwise have
    forced promotion to invent a date or fail, and inventing is the one thing this layer is not for. And
    `risk_record` preserves supplied scores with a default `MATRIX_5X5` scale rather than computing
    severity: no scoring methodology was agreed, so none was smuggled in (the earlier "severity is always
    computed" rule is reversed by the Phase 1 brief).

**Consequences.** The domain is queryable without touching a file, `schema_diff` still returns nothing
after the upgrade, and the knowledge layer's rebuild-and-rederive semantics carry over unchanged. Because
the tables are ordinary tables, the integrity checks that the schema cannot express live beside the
existing ones in `database/integrity.py` and run inside `doctor` (ADR-0007's pattern). Because the
provenance columns are per-row, a lesson can never be approved on a document that has since been
superseded without that being visible.

**Rejected.** A separate engineering database (two files, two backups, one question with two answers);
a `document_engineering_record` JSON blob (unqueryable, and it makes the index the authority); storing a
timeline table (ADR-0011); and giving each new table its own citation format.


---

## ADR-0010 — Promotion is a separate, idempotent, self-reporting act; stated time and computed time are different evidence

**Status:** accepted (2026-09-06)

**Context.** Once the tables exist, the tempting implementation is to fill them during ingestion: the
artefact is right there, and a folder that has been ingested "should" have its NPT rows. That is how a
platform ends up with numbers nobody can trace, because the mapping from a file to a record is the part
that is allowed to be wrong. It is also how a missing value becomes a zero. The two rules below exist to
keep both out.

**Decision.**

*   **Promotion is its own step, and ingestion never does it implicitly.** `drillintel ingest` leaves the
    operational tables alone unless `--promote` is passed; `records promote` takes a scope (`--document`
    and `--version`, or `--well`/`--field`/`--project`, or none, which is the whole workspace) and reports
    what it touched. The sweep is scoped through `_candidate_versions`, which counts the versions it
    matched *before* writing anything, because a scope built from a broken `IN` subselect returns "success,
    zero rows" - which is exactly the bug this counter exists to catch.
*   **Write once, then never again.** The generic record API is create-or-return: `record_operation`,
    `record_event`, `record_npt`, `record_problem` find the row whose `identity_key` matches and return it
    rather than updating it. A promoted row is `CANDIDATE`, `origin=DERIVED`, `created_by="promoter"`,
    with `root_cause_status=UNKNOWN`; a person moves it to `CONFIRMED` through `set_record_status`, which
    refuses to record a decision with no author. Nothing in the pipeline writes `CONFIRMED`.
    `record_calculation` obeys the same rule with a content-addressed identity, so re-running a batch of
    engineering records is a no-op instead of a second copy of the same claim (ADR-0012).
*   **Rows that cannot be placed are reported, not placed somewhere plausible.** A record naming an
    unknown well is skipped and counted; a zero-hour NPT line is skipped (it is a section header in most
    exports); a total line never becomes an activity; a duration that cannot be parsed is counted as an
    unknown duration rather than zeroed.
*   **A duration is a claim with a basis.** `duration_hours` sits beside `started_at`/`ended_at` with a
    `duration_basis` of `STATED` or `COMPUTED`, because a report that says "6.5 h" and a clock that says
    09:00→15:30 are not the same evidence and must not be averaged together. Times are nullable on purpose,
    and an unreadable duration stays `NULL` and is counted as an unknown duration rather than as zero. The
    row stores hours and not the sketch's minutes because hours are what an NPT sheet states and what every
    aggregate here adds up; a cell that states another unit - `90 min`, `1.5 days` - is converted through
    `core.units`, which is the unit authority, and its wording is kept verbatim in `duration_text`. The
    daily report's time-breakdown hours are kept in `duration_text` as written, and the field's 59.25 h is
    the sum of *records*, not of distinct incidents - which is why the number is printed with its row count
    beside it.
*   **A window filters dated rows; it does not invent dates.** `since`/`until` exclude undated rows unless
    `include_undated` asks for them, and then they are reported as undated. An aggregate with nothing to
    aggregate is `None`, never `0.0`.
*   **Re-running is the test.** Promotion is idempotent by content identity, and a second pass reports
    `unchanged`. Orphan removal is a sweep of `delete_orphans` by `identity_key` for the swept tables, with
    `DdrReport` deliberately never swept: a report row is a filing decision about a file, not a line
    someone might restate.
*   **Nothing is promoted by a model.** A cause, a severity or a root cause that the source did not state
    is absent, not inferred; `CauseStatus` is `{KNOWN, INFERRED, UNKNOWN, CONFLICTED}` and `INFERRED`
    means a *person* inferred it and said so.

**Consequences.** `records promote` is safe to put in a cron job and in a runbook, and the corpus test
enumerates the numbers: 22 rows created, a re-run that creates nothing, `ZERO_NPT` and
`TOTAL_ALREADY_COUNTED` reported as skips rather than as silence, and `conflict 0`. `doctor` prints the
counts as `notes` rather than `findings`, since findings flip the exit code and "nothing promoted yet" is
context, not an alarm. The cost layer follows the same shape: a line's only NPT link is `cost_item.npt_id`
(a column, not an edge), and a summary never filters cost lines by the state of the record they cite.

**Rejected.** Promoting inside ingestion (a mapping failure would corrupt the ingest path's guarantees);
LLM-assisted promotion behind a flag (there is no flag, because there is no version of this where a model
is the authority for a number); upsert-with-overwrite (a confirmed row must not be erased by a re-read of
a file); and filling unknown durations with zero to make the arithmetic run.


---

## ADR-0011 — An answer is computed on demand; only a reviewed snapshot is stored, and it stores its own query

**Status:** accepted (2026-09-06)

**Context.** "Which wells in this field lost time to stuck pipe, and how much" is a join over
`npt_record` and `problem_occurrence`, not a column. Caching it means the answer outlives the records that
produced it; not caching it means a person reviewing a pattern last month cannot tell the platform what
they reviewed. The domain also needs to link a pattern to its evidence and a risk to what it mitigates,
and if every aggregate gets its own edge table then the graph is only as reliable as the sum of its
copies.

**Decision.**

*   **Derived answers are not persisted.** `FieldIntelligence`, `build_timeline` and `record_summary`
    read rows and return dictionaries; nothing in `intelligence/` writes, except where the next bullet
    says otherwise. A timeline entry exists only because a record carries its own timestamp - the timeline
    is a projection over the tables, and a table called `well_timeline` would be a lie waiting to drift.
*   **One thing is stored: a reviewed `field_pattern` snapshot**, and it stores the `query` that produced
    it alongside the counts it found. `staleness()` re-runs that stored query and reports the difference
    (`{"occurrence_count": {"stored": 2, "now": 3}}`) rather than silently refreshing, because the
    question a reviewer asks is "has what I confirmed changed", not "what is the number now". Its status
    moves only with an author.
*   **Re-asserting a fact is not an insert.** The edges that say which wells and which evidence rows a
    pattern rests on are ordinary `knowledge_relation` edges, so `link_rows` is idempotent - a second pass
    adds nothing - and `snapshot()` with evidence linking disabled leaves the stored evidence alone. A
    graph that grows an edge every time someone asks the same question is a graph nobody can count.
*   **A recommendation is proposed by code and decided by a person.** `propose_recommendation` writes a
    `PROPOSED` row with the pattern's query and a reason built from the counts; `decide_recommendation`
    moves it, requires an author and a reason, and the signature deduplicates the same advice for the same
    scope. Best practices are derived only from `APPROVED` lessons, carry their evidence, and never list
    their own author as an approver. **Amended (2026-09-08):** the proposal itself is gated on the
    pattern's status - `propose_recommendation` refuses a pattern that is not `CONFIRMED`
    (`ValidationError`), because what a grouping of history licenses is a proposal to *someone who
    looked at the grouping*, and a pattern nobody confirmed is an opinion the platform would be
    circulating on its own authority. The gate is one check on the stored status; it changes no
    lifecycle, and a pattern confirmed and then rejected can no longer spawn advice.
*   **Numbers keep their units.** Costs are totalled per currency and never across currencies - no rate,
    no conversion, no inflation, no AFE. A variance is `None` unless both sides exist; planned-only and
    actual-only lines are counted separately so an absent side is visible as absent; unpriced and
    unattributed items are reported rather than dropped.
*   **The CLI covers what a terminal user verifies, not every table.** `records`, `timeline`, `fields`,
    `patterns` and `lessons` are commands; procedures, programmes, risks, costs and rigs/service
    companies are reached through repositories and services (and, for the versioned records, through the
    revision and approval methods). A lesson's approval is deliberately *not* a CLI verb: it is the one
    decision in this domain that a person should make while looking at the evidence, and a flag is not
    that.
*   **Recurrence is a query with thresholds, not a prediction.** `find_recurring` takes
    `min_occurrences`/`min_wells` and returns what the rows support, with `limit=0` meaning "everything";
    offset candidates compare a well against other wells that recorded the same problem types
    (`same_field_only` by default, off with a flag) - an inference about what to read, never about what
    will happen.

**Consequences.** Every number in `--json` output is reproducible from the rows in the same breath, and
`records summary`/`fields summary` disagree only when a row was edited between the two calls. A snapshot
that has drifted is an actionable report rather than a stale row, and the tests pin all of it: 21 timeline
entries for a well of which 11 are undated, five NPT rows totalling 59.25 h with two undated, `stuck_pipe`
across two wells at 28.75 h, and a pattern whose stored count is 2 while the records now say 3. The
boundaries are code-review boundaries too: there is no UI, no model call, no RAG and no predictor in
`intelligence/`, and `docs/DOMAIN.md` keeps that promise in writing.

**Rejected.** A materialised-view cache per aggregate; a `well_timeline` table; auto-refreshing a
snapshot on read (the reviewer's baseline would vanish); summing costs with a default exchange rate;
promoting a pattern to `CONFIRMED` when its counts grow; and a CLI verb for every table, which would put
an approval workflow in a place with no evidence viewer.

## ADR-0012 — The engineering record is a table that can say where its number came from

**Status:** accepted (2026-09-06)

**Context.** The Phase 1 brief asked for a generic foundation for engineering calculations: method,
version, inputs with units, outputs, assumptions, validation, uncertainty, confidence, provenance and
supersession, with arithmetic kept out of the platform. Auditing the layer against that clause found the
table already there - `calculation` and `calculation_input`, since migration 0001 - with
`EngineeringRepository.calculations_for` as a read path and **no writer anywhere in the tree**. The entity
was half-built, and the half that was missing is the half that makes a number accountable: the table had no
`origin`, no `created_by`, no `identity_key`, no `document_id`/`document_version_id` and no `attributes`, so
a record could state a result and could not state where it came from or what else had been computed with the
same inputs. Widening a table that already has rows in it is the part with choices in it.

**Decision.**

*   **Add a write path, not a calculation engine.** `EngineeringRepository.record_calculation` persists what
    someone or something else worked out: it requires a `method_id`, refuses a non-`MANUAL` `origin` unless
    the row cites its evidence, returns `(row, created)`, and writes one `calculation_input` per input so a
    value can be traced to the results that consumed it (`calculations_using`). Only numeric parsing happens
    here, through `core.units`, so `{"value": "10.2 ppg"}` indexes as `10.2` with unit `ppg` and a bare
    number stays bare. No formula in this repository evaluates a result; a client that has a calculator puts
    it behind an adapter.
*   **Identity is the content, so re-recording is idempotent.** `identity_key` is `sha256` over the
    canonical payload (method, version, type, scope, inputs, outputs, assumptions, validation, uncertainty,
    confidence, triggered-by) truncated to 32 hex characters, prefixed `calc:`; a caller who already knows
    its own key may pass one. The same content returns the row it wrote; different content is a new row, and
    `supersedes_id` marks the parent `SUPERSEDED` at `revision + 1`, because a decision may have been made on
    the older number. A unique index enforces that across concurrent writers, and `NULL` identities coexist,
    so a row typed by hand without a payload hash is never blocked. `current_only` reads "nobody has
    superseded it" off the chain, not off `status`.
*   **The new citation columns carry no foreign keys.** SQLite cannot add a constraint to a filled table
    without rebuilding it, and a rebuild is not expressible in `alembic upgrade --sql` - the review path a
    DBA uses on a shared file - and would leave a fresh workspace and a migrated one structurally different.
    So 0005 adds plain columns, the ORM models declare exactly those (a model that believes in a constraint
    the database does not enforce is worse than either alone), and `check_promoted_evidence` plus the
    document-version checks police the promise where corruption can actually arrive. On PostgreSQL the same
    migration stays a fast `ADD COLUMN`, and real constraints can be added there in a later one-line
    migration with no data movement.
*   **Existing rows are told the truth about.** `origin` is backfilled to `'MANUAL'` and `created_by` to
    `'system'`, with `NOT NULL` and server defaults so a raw insert that forgets the column cannot create an
    unattributable record. `'MANUAL'` is the only honest backfill: nobody has seen these rows' sources, and
    stamping them `DERIVED` would manufacture evidence, which is the failure this platform exists to prevent.

**Consequences.** `doctor` now reports an engineering record that cites a version which does not exist, and
`tests/integration/test_migration_0005.py` pins the parts of this that a reviewer cannot eyeball: the diff
against a fresh `create_all` is empty, a legacy row survives with the defaults above, a re-run is a no-op at
the payload level, the downgrade is the exact inverse in both offline and live mode, and the dangling
citations are found on a migrated file. The widening is additive: no column is renamed or dropped, and no
Knowledge Layer table is touched.

**Rejected.** A `batch_alter_table` rebuild to keep the foreign keys (it would break the offline review path
and change the fresh-install schema); a new `engineering_record` table beside the old one (two places for one
concept, and the old one stays readable either way); a `record_calculation` that recomputes and overwrites the
parent (a superseded number that no longer exists cannot be audited); and storing the timeline-style
"current" flag as a column on `calculation`, which every other versioned table needs an index to keep honest.

## ADR-0013 — Retrieval verifies what search locates; a stale sidecar row is not evidence

**Status:** accepted (2026-09-08)

**Context.** The platform answers questions with citations, and search already locates candidates:
BM25 over a disposable SQLite sidecar (ADR-0003), three source kinds (document chunks, knowledge facts
rendered as `knowledge_fact` chunks, and the six structured record types projected at build time). But
the sidecar is, by design, a projection that can be stale - a record rejected, superseded, revised or
deleted after the last rebuild is still in it, and a new row is not. Nothing above search was allowed
to trust that projection past the point of discovery: a future reader - human, UI, or an AI layer that
this repository deliberately does not build (ADR-0005, ADR-0012) - must be handed records that were
re-read from the authoritative database, with a lifecycle and a scope that hold, and with provenance
that the record actually carries. The gap was not a second search engine; it was the verification
boundary between "the index says this exists" and "this is authoritative evidence now".

**Decision.**

*   **One new layer, `drilling_intelligence.retrieval`, with three value objects.**
    `RetrievalRequest` (query, scope, `source_types`, `lifecycle`, `limit`, date bounds),
    `EvidenceItem` (a verified record) and `EvidenceBundle` (the answer). The layer's pipeline is
    *search discovers, retrieval verifies*: candidates come only from the existing
    `SearchService.search` - the same index, the same BM25 ranking, the same tie-breaks - and every
    candidate is re-read from the authoritative tables by its own identity before it may appear in the
    answer. There is no second search engine, no second ranking, and no embedding/vector path.
*   **Identities are the records' own, never retrieval inventions.** A structured row is
    `structured:<record_type>:<row id>` (the domain's primary key), a knowledge fact is
    `knowledge:<item id>`, a document citation is `document:<document>:<version>:<chunk>`. The same
    row retrieved with a different query, a different limit or a different insertion order carries the
    same identity, because the identity is built from the authoritative row, not from content, a
    timestamp or a position in a result list.
*   **Provenance is carried, never fabricated.** Every field a record does not hold is empty. A manual
    lesson has `document_id=""` and `locator_ref=""`; a diagnostic or page chunk - which has no recorded
    location - is dropped with the reason `not citable (no recorded location)` instead of being dressed
    up as a citation. A bundle in which every item resolves to a real row is asserted bundle-wide.
*   **Scope is a single level, decided by the platform's precedence, and re-checked against the
    authoritative row.** A named well is the whole scope; `well_id` beats `field_id` beats
    `project_id`. Only the winning level is passed to search (passing both well and field would make
    search AND them; OR-ing them is exactly the union the precedence forbids), and the re-check is
    applied to the row's own scope columns. A scope that names a row the database does not have is a
    caller error (`ValidationError`), not a silently empty answer.
*   **Current and history are explicit policies, per source type, using the domain's own lifecycle
    rules.** `current` returns only the rows the domain answers "now": a lesson only while
    `is_current`, an occurrence/NPT/event while not `REJECTED`, a recommendation while not
    `SUPERSEDED`, a knowledge item while not `SUPERSEDED`/`RETIRED`, a document version while
    `is_current`. `history` returns those plus the historical rows, each labelled with its state and
    `current=False`. Nothing is "latest by timestamp". A `CONFLICTED` knowledge item is still current
    evidence and is returned *with* its conflict status, never silently resolved to one value. The
    honest boundary is recorded rather than hidden: the search projection stores only the domain's
    current structured rows, and a clean rebuild prunes superseded document versions, so history
    surfaces exactly what the index still holds - retrieval verifies it, and never invents rows the
    index has pruned.
*   **Drops are reported, not absorbed.** A candidate the re-read rejects appears in
    `EvidenceBundle.dropped` with its identity and the reason - `no longer in the authoritative
    database` (deleted, or a fact the registry no longer holds), `outside the requested scope`,
    `not current (...)` or `not citable (...)`. The mandatory forensic is pinned by test: build the
    index, mutate or delete the authoritative row, do not rebuild, and a retrieval must not return the
    stale row as evidence.
*   **Read-only, caller's transaction is the caller's, reads are bounded.** A retrieval opens a
    read-only session (or borrows the caller's, in which case it reads inside the caller's
    transaction and still never commits it). A fingerprint over the authoritative rows is unchanged by
    any retrieval. Candidates are re-read in batches - one `id IN (...)` per structured type actually
    present, plus a few for versions, documents, knowledge items and scope names - so a result of one,
    ten or a hundred rows costs a handful of queries, asserted by counting the cursors.
*   **The bundle is a deterministic snapshot.** It is a frozen value object of strings, numbers,
    booleans and plain mappings (asserted JSON-serialisable, with no reprs, addresses or call
    timestamps), records the request that produced it (query, scope, policy), and orders its items by
    the discovery rank with an identity tie-break. It contains no ORM rows, sessions or detached
    objects. It also records *how* the question was discovered: retrieval never broadens a query on
    its own, but when the exact all-terms AND finds nothing the search layer's documented fallback
    answers any-of-the-terms, and the bundle carries that as `discovery_broadened` - a broadened
    answer is never mistaken for an exact one.

**Consequences.** `tests/integration/test_retrieval_forensics.py` (50 tests, real SQLite, real
repositories, real sidecar, no mocks) pins the whole boundary: identity and determinism; the
authoritative re-read (deleted rows, rejected/superseded/retired rows, deleted and demoted document
versions, deleted knowledge facts); the scope topology Project A { Field A { A1, A2 }, Field B { B1 } }
plus Project B { Field C { C1 } } with the same text in every well, so any leak is caught; all six
structured record types through the one mechanism; CURRENT/HISTORY per type; conflict preservation;
bundle-wide citation integrity; provenance per source class; read-only fingerprints; caller-transaction
safety (including a pending modification seen through a caller session but never committed); bounded
query counts; the broadened-discovery label, and the empty/malformed request
semantics (empty query, invalid lifecycle, negative limit, unknown source type, malformed
date bound). The layer adds no tables, no migrations (head stays
0007) and no dependencies, and it changes no existing search behaviour - `SearchService` answers
exactly what it answered before; retrieval is a new, higher, verifiable boundary above it.

**Rejected.** A retrieval service that trusts the sidecar past discovery (it would make a stale row
authoritative evidence, the exact failure the boundary exists to prevent); a direct database scan as
the discovery path (a second, unranked search engine beside the ranked one, and the two would drift);
OR-ing well and field into one scope (it would return a well's neighbours, which the precedence exists
to forbid); inventing a `latest by timestamp` "current" (the domain's lifecycle rules are the current);
and storing retrieval results (a snapshot of a read is not a new record, and the next read is cheap).

## ADR-0014 — An evidence package is an addressable, re-askable read: composed only from retrieval, stale only by a named diff

**Status:** accepted (2026-09-08)

**Context.** The P9 boundary (ADR-0013) proved the direction: search locates, retrieval verifies, and a
stale sidecar row is not evidence. But the layer that had to *consume* verified evidence - a person at
the terminal, a script, or the AI surface the platform deliberately does not build yet (ADR-0005) - had
nothing to point at. `RetrievalService` answers one question at a time, its bundles carry no stable
address, and the CLI that every other layer of this repository exposes had no way to ask for *the
evidence*. Every future consumer would otherwise re-invent composition, deduplication and
"does this still hold?" - and each reinvention is a chance to read the sidecar past discovery. The gap
was not another query; it was an *addressable answer*.

**Decision.**

*   **One new layer, `drilling_intelligence.evidence`, with one rule: evidence is only ever produced by
    retrieval.** `EvidenceQueryService` holds a `RetrievalService` and nothing else that can see the
    database; a service built without one raises rather than degrading to a raw search or a direct
    scan. Every topic of an `EvidenceQuery` becomes one `RetrievalRequest` - same scope precedence,
    same lifecycle policy, same authoritative re-read, same drop reasons.
*   **A package composes topics into one deduplicated answer, and says so per topic.** An item two
    topics find is one item carrying `found_by` in topic order; `TopicCoverage` records, for each
    topic, how many verified items it answered with, how many candidates the re-read rejected (with
    each reason), and whether discovery was broadened. An empty topic is a statement - `returned: 0` -
    not an error, and a broadened topic keeps its label end to end.
*   **The package's identity addresses the evidence, not its presentation.** `evpkg:` + sha256 over the
    canonical request (topics sorted, scope, source kinds, lifecycle, limit, date bounds) and each item's
    authoritative state (identity, source, status, current), plus per-topic coverage - never over scores
    or display order, because ranking belongs to the disposable index and the same evidence must earn the
    same address under any rendering of it. Topic order changes the display, not the address; inserting
    the same rows in a different order does not either.
*   **A package stores its own query, and staleness is a named diff, not a timestamp.**
    `check_freshness` re-runs the stored query against the live authoritative state and reports
    `added` / `removed` / `changed` identities. A package is either fresh (identity equal, no diff) or
    stale with the exact evidence that moved - including a status move on a row that is still indexed
    (`changed`), which an identity-only comparison would miss. This is ADR-0011's rule - "only a
    snapshot is stored, and it stores its own query" - applied to reads instead of patterns: nothing
    here is persisted, and nothing here trusts a `created_at`.
*   **The boundary the sidecar makes is pinned, not papered over.** A new authoritative row is not
    evidence until a rebuild puts it in the index, so a package stays *fresh* across an unindexed insert
    and goes stale - with the new identity in `added` - only once the rebuild has run. Retrieval
    verifies what discovery can see; the package says what discovery saw, and never the other way round.
*   **The terminal gets the same promise as every other command.** `drillintel evidence query --topic …`
    prints the package (aligned text, or one JSON document under `--json`), and
    `drillintel evidence query --topic … --expect <identity>` re-asks the question and exits 0 when the
    evidence still holds, 1 when it has moved - a freshness check a pipeline can branch on.

**Consequences.** `tests/integration/test_evidence_package_forensics.py` (25 tests, real SQLite, real
repositories, real sidecar, no mocks) pins the boundary: identity stability across service instances,
topic order, and insertion order; composition and per-topic coverage; the any-of label in coverage;
deterministic ordering under score ties; package-wide citation integrity; a CONFLICTED knowledge pair
carried, not resolved; the well/field/project scope topology with leak detection; superseded-lesson
current/history under a stale sidecar; freshness over unchanged data; the mandated
index→mutate→no-rebuild→check forensic with a named `removed` diff; rebuild-driven `added`/`removed`;
the unindexed-insert boundary; status-move `changed` detection; malformed-query rejection; the
refusal to run without retrieval; a whole-database read-only fingerprint; bounded authoritative query
counts across more rows; and the CLI's determinism plus its freshness exit codes. The layer adds no
tables, no migrations (head stays 0007) and no dependencies, writes nothing, and changes no existing
behaviour - retrieval and search answer exactly what they answered before; the package is a higher,
addressable boundary above them.

**Rejected.** Persisting packages (ADR-0013's "a snapshot of a read is not a new record" still holds -
the freshness check is a re-read, and storage would add a second thing that can go stale); hashing
scores or display order into the identity (ranking is the disposable index's property, and an address
that changes when the index is rebuilt is not an address); letting the package discover candidates
itself (a second discovery path beside search's is exactly the drift ADR-0003 exists to prevent);
reporting staleness as a timestamp (a package is stale because the answer moved, and the answer is
found by re-asking, not by the clock); and a free-text "ask" surface (the topics are questions a
person or a script states deliberately; parsing intent is the AI layer's job, and that layer will
consume these packages rather than replace them).

## ADR-0015 — A citation is verified by re-reading the file, not by trusting the row: the citation auditor

**Status:** accepted (2026-09-09)

**Context.** The certified chain proves two things at a time: retrieval (ADR-0013) re-reads each search
candidate from the authoritative database, so an item's *row* still exists and is current; the evidence
package (ADR-0014) re-asks its stored query, so the *answer* still holds. Neither proves the third
thing a reader actually relies on - that the item's *citation* still holds. A record can say "mud
weight 10.2 ppg, read from `mud_report.xlsx`, Sheet `Summary`, cell B9, excerpt '10.2', sha256 …", and
every row-level and package-level check can pass while that cell no longer contains 10.2, or the file
no longer exists, or the file has been re-saved and the bytes have moved. The core already had the
primitive for exactly this - `core.provenance.verify_provenance` (hash pre-check, then re-read the
recorded location and compare with the recorded excerpt, fuzzy ≥ 0.9) - but it was reachable only
through search's `--verify`, one hit at a time, and the evidence layer had never exposed it: "verified
evidence" verified row existence, not citation truth.

**Decision.**

*   **One new read-only object, `CitationAuditor`, on the evidence layer, that audits a package rather
    than a query.** It takes an `EvidencePackage` (already composed, already verified at the row level)
    and returns a `CitationAuditReport`: one `CitationCheck` per item, in item-identity order, with an
    explicit tally. It never writes, never reads the search sidecar, and never re-runs discovery - it
    re-reads what the *items* claim to cite, so the audit is a property of the evidence it was given,
    not of a fresh search that could drift.
*   **The states are explicit and are never collapsed into an empty success.** `MATCH` (the citation was
    re-read and the content still holds), `MISMATCH` (the source no longer contains what the citation
    claims), `UNREADABLE` (the file or the recorded location cannot be re-read), `NOT_CHECKABLE` (there
    is no file citation to check, or no recorded hash to fall back on). `all_verified` is true only when
    nothing is `MISMATCH` or `UNREADABLE`; `NOT_CHECKABLE` items are counted and labelled, not silently
    passed - a manually entered row that cites the row itself is `NOT_CHECKABLE`, and the report says so.
*   **The check kind follows what the citation honestly claims, using the same rule search's `--verify`
    uses.** A document or knowledge item that reads as a *quotation* of its region (`verbatim`, computed
    by search against the full chunk text and now carried on the `EvidenceItem`) is verified by re-reading
    the recorded location and comparing the excerpt (`check="excerpt"`). A *view* of a larger region has
    nothing at its location that reads as its text, so it is verified by the source file's hash alone
    (`check="source"`) - weaker, and labelled as such. A structured row's citation is the row itself
    (retrieval re-read it); each document it cites through its evidence list is re-read, and the row folds
    its citations to the worst of them.
*   **A verified file whose recorded excerpt is a rendering is not reported broken.** The audit's first
    run exposed a real subtlety: a table cited as a whole is excerpted as its *rendered* rows (a CSV's
    tab-joined cells, a docx table's `|`-joined cells), not a raw slice of the file, so a raw re-read of
    the unchanged file legitimately differs from the recorded excerpt. Where the source hash matches the
    extraction's hash, the file is byte-for-byte the extraction's file and cannot have lost its content -
    so the audit reports `MATCH` with `check="source"` and the explanation, rather than a false
    `MISMATCH`. The rendering is normalised away by the hash; the audit does not invent a stricter
    content claim than the citation makes.
*   **It composes, it does not duplicate.** The excerpt/hash comparison is the core's
    `verify_provenance`; the version-to-file resolution is the document repository's
    `resolve_source_path` (recorded absolute path, then workspace-relative - provenance survives a moved
    workspace). The audit adds the layer around them: batch the version ids the package cites, read them
    in one `IN` query, hash each cited file once, and interpret the outcome per the rule above.
*   **The terminal gets it as opt-in, not as a slow default.** `drillintel evidence query --topic …
    --verify` re-reads the sources and prints an `audit` section (one line per item: status, citation,
    check kind, and the hashes where relevant), because re-reading files is the expensive half. Without
    `--verify` the command is byte-for-byte what it was.

**Consequences.** `tests/integration/test_evidence_citation_forensics.py` (21 tests, real workspace, real
SQLite, real generated files, no mocks) pins the boundary: a clean corpus verifies, and the check kind
follows verbatimness; a *mutated* source is a `MISMATCH` while the row still verifies and the package
keeps its identity (the exact gap P11 closes); restoring the source restores verification; a *deleted*
source is `UNREADABLE` and localised to its citing items; a structured row citing documents verifies
through its evidence; a rendered excerpt of a hash-verified file is `MATCH`, not `MISMATCH`; a structured
row with no file citation is `NOT_CHECKABLE`, not passed; a broken second citation folds the row to the
worst; a citation at a version missing from the registry, and a malformed stored provenance, are
`NOT_CHECKABLE`, not crashes; the audit is byte-deterministic, order-independent, one batched
authoritative query, and read-only (whole-DB fingerprint and every workspace file byte-identical after).
The layer adds no tables, no migrations (head stays 0007) and no dependencies, writes nothing, and
changes no existing behaviour - retrieval and the package answer exactly what they answered before; the
auditor is a read that re-checks what they cite.

**Rejected.** Persisting the audit result (a re-read is the audit; storing its outcome would create a
second thing that can go stale - the same rule ADR-0014 applied to the package); making `--verify` the
default (it opens source files; on a large workspace that is a slow command nobody asked for); reporting
a rendering mismatch as `MISMATCH` (it would report a correct citation as broken every time a table was
cited as a whole); a free "check everything" sweep independent of a package (the audit is about *this*
evidence's citations, in identity order - a whole-workspace sweep is `doctor`'s job, and the two stay
separate); and re-reading the search sidecar to learn what to check (the citation rides on the verified
item, and the sidecar is the disposable projection the chain exists not to trust).

---

## ADR-0016 — A calculation input names the durable thing it consumed, or admits it cannot

**Status:** accepted (2026-09-09)

**Context.** The certified chain answers "where did this number come from?" - source file, document,
version, extraction, provenance, fact, record, search, retrieval, evidence, citation audit. The
engineering record answers the inverse question, and it answered it wrongly. `calculation_input.subject_key`
had been free text since 0001: `record_calculation` stored `subject_key or source`, stripped and cut to
300 characters, and `calculations_using` compared it with `=`. Auditing that pair against real repository
code reproduced six distinct false negatives on one physical quantity - a subject spelled
`property:MUD_WEIGHT` did not find `property:mud_weight`, a permuted component order found nothing, a
key longer than the column was truncated on the way in and so was unfindable by the key its own author
had used, and `SubjectKey.render()` escaped nothing, so a property name containing `|` collided with a
different subject. Worse than any single case: the query returned `[]`, which reads as *"nothing depends
on this"* and is indistinguishable from *"I could not resolve what you asked about"*. An engineering
platform whose premise is that a decision needs a source cannot answer "this source changed; which
decisions rest on it?" with a silent empty list.

The canonical mechanism already existed. `core.ids.SubjectKey` is the knowledge layer's grouping key -
`KnowledgeFact.lookup_key()` is built from it, and a fact's content-addressed id includes it - so this is
not a missing identity framework. It is an existing one that the single subsystem written for it (its own
docstring says *"which calculations used this mud weight?"*) did not call.

**Decision.**

*   **`core.ids.SubjectKey` is the one canonical subject identity, and it is now safe to be one.**
    Rendering is deterministic and component order is fixed, so a caller cannot change a key by changing
    the order it passed things in. `|`, `:` and `\` inside a component are escaped, so two distinct
    subjects can no longer render identically; `parse` reverses the escaping, so the round trip is exact.
    Escaping touches *only* components that contain those characters, which is why every key the
    knowledge layer has ever written - opaque hex ids, snake_case predicates - renders byte-for-byte as
    it always did, and no stored `lookup_key` or `knowledge_item.id` moves.
*   **The normalization rule for a property name is written down, not implied.** A property name is a
    token, not prose: lowercased, with spaces, hyphens, dots and tabs becoming underscores and runs of
    underscores collapsing to one (`normalize_property`). `Mud Weight`, `MUD_WEIGHT` and `mud-weight` are
    one property. A record state is the same rule in upper case (`normalize_state`), because
    `RecordState` is an enum of tokens. Ids keep their case: they are opaque, and `Well-A` is not
    `well-a`. Every predicate the extractors emit is already in this form, so normalising is a no-op on
    the data in existing workspaces.
*   **A subject resolves to an anchor, and the anchor is stored as columns.** Migration 0008 adds
    `calculation_input.subject_kind` and `subject_id` plus `ix_calc_input_subject_ref`, so change impact
    is a lookup on `(kind, id)` rather than string equality. The anchor kinds are the durable things this
    schema already has - `well`, `section`, `document_version`, `document`, `project`. No foreign key, and
    for a reason 0005 recorded for `calculation`'s citation columns and which applies twice as hard here:
    there is no single FK target, so a constraint would have to name one of five tables and forbid the
    rest. `check_calculation_dependencies` polices it instead, which is this repository's standing pattern
    for an invariant a constraint cannot state (ADR-0007).
*   **What cannot be recognised is labelled, never guessed.** A string that does not round-trip through
    `SubjectKey` and name an anchor is legacy free text (`mud_report.xlsx!Summary!B9`, or
    `well:A-3|mud_weight` with no `property:` component). It is stored *verbatim*, labelled
    `subject_kind='legacy'` with a NULL id, and stays reachable by its exact text. The backfill in 0008
    follows the same rule: it resolves only a leading `<known anchor>:<id>` where the id is non-empty and
    free of `:`, the string contains no escape character, and whatever follows the anchor is itself a
    recognised component (`well:`, `section:`, `property:`, `state:`, `document:`, `project:`) or nothing
    at all. Everything else is left alone. A fabricated dependency is worse than an admitted unknown,
    which is the same choice 0005 made when it backfilled `origin='MANUAL'` rather than inventing a
    source.
*   **The migration may be more conservative than the write path, never less.** The backfill expresses
    the resolution rule in SQL while `resolve_input_subject` expresses it in Python — one rule, two
    implementations, which is exactly the shape that drifts. The direction of any permitted difference is
    therefore fixed: under-resolving is recoverable (the row stays `legacy`, the query still finds it by
    its exact text, and `doctor` reports it as unresolved), whereas over-resolving silently indexes a row
    under an identity no query will ask for, making the calculation invisible to the question the columns
    exist to answer. Only one difference is permitted today — a key containing an escape character, which
    needs the unescaping grammar SQL cannot express — and
    `test_the_backfill_and_the_write_path_agree_on_every_shape` pins that list, so a new divergence fails
    the suite.
*   **Normalization lives on the read path, because history is not rewritten.** A workspace migrated from
    0007 legitimately holds `property:MUD_WEIGHT` where a row written today holds `property:mud_weight`.
    The migration preserves that text byte-for-byte, so the *query* carries the normalization: it reads
    back the distinct spellings already recorded against the resolved anchor and canonicalises them
    through `SubjectKey.canonical_key`, the single implementation of the rule. The candidate set is
    bounded by the number of distinct subjects on that anchor rather than by the number of inputs, so the
    query count does not grow with the data. Re-encoding the normalization rule in SQL was rejected for
    the same reason the backfill divergence was a defect: a rule written twice is a rule that disagrees
    with itself.
*   **A long subject is digested, never truncated.** A canonical rendering wider than the column is
    stored as `k256:` + sha256 of that rendering, computed identically on the write path and the read
    path, so a long subject is still found by the key its author used. A *legacy* value that is too long
    is refused with an error: free text has no canonical form to digest, and silently cutting it produces
    a string that means something else.
*   **Three answers, because there are three.** `calculation_impact` reports `CURRENT`, `STALE` (the
    input cites a document version the registry no longer calls current) and `UNRESOLVED` (the subject is
    not one the platform can resolve), and says separately whether the subject itself was `resolved`.
    "Nothing depends on this" and "I cannot tell" are different sentences and never print the same way.
    The query de-duplicates (a calculation whose two inputs name one subject is one affected calculation),
    orders deterministically by `(created_at desc, id desc)`, and takes the same `current_only` and scope
    arguments as `calculations_for`, read off the supersession chain rather than the `status` column.
*   **It reports; it never acts.** Nothing here re-runs a calculation, invalidates one, rewrites a
    historical input or guesses which new version replaces an old one. A superseded source makes a
    dependency *reportable as stale* - the number stays exactly what a decision was made on (ADR-0012),
    and re-running it is an engineering act with a method and a reviewer, not a side effect of a
    consistency check. `doctor` gains the two findings and no new command; `drillintel records impact` is
    a read-only inspection with a `--json` twin and a non-zero exit when anything is stale or unresolved.
*   **Identity is not touched.** `calculation.identity_key` hashes the payload the caller stored. 0008
    writes only to `calculation_input`, adds no column to `calculation` and rewrites no `inputs` JSON, so
    a historical record keeps the identity it was recorded under and re-recording it stays a no-op. Two
    different stored spellings therefore remain two records - the *index* resolves them to one
    dependency, which is the point, while rewriting either row's identity would break a reference
    somebody may already have quoted.

**Amendment (2026-09-10).** Re-auditing the shipped migration against the repository — rather than
against its own report — found four defects in the first cut, all now repaired under this ADR and pinned
by tests. (1) `parse` keyed anchors purely off the token, but `document` and `project` are *both* an
anchor kind and a trailing scope field; the anchor form therefore failed its round trip,
`is_canonical_subject` returned False, and a document- or project-anchored subject was permanently
misclassified as legacy. Position now disambiguates: leading means anchor, elsewhere means field. (2) The
SQL backfill resolved any string with a known prefix, so `well:A-3|mud_weight` — which has no `property:`
component and which the runtime resolver calls legacy — was labelled `well`/`A-3`; the tail must now be a
recognised component. (3) The same backfill accepted `:` inside an anchor id, resolving `well:a:b|…` to a
well named `a:b` that `core.ids` would have escaped. (4) A row the backfill deliberately left `legacy`,
and any row holding a pre-0008 spelling, was unreachable from the canonical key; the read path now
resolves both. Defects 2 and 3 are the serious ones — they are the fabricated identities this ADR
promises never to create — and they existed because the resolution rule was implemented twice.

**Consequences.** `tests/integration/test_change_impact_forensics.py` (47 tests) pins the five cases the
audit reproduced - canonical lookup, equivalent representations, delimiter collisions and distinct
subjects, over-long subjects, and the full revision/supersession forensic (source version → input →
re-ingest → supersede → currency check) - plus de-duplication, deterministic ordering, lifecycle and
scope filtering, unresolved-versus-not-affected, input-level provenance, idempotent re-recording,
identity-key stability, a read-only whole-table fingerprint and a bounded query count.
`tests/integration/test_migration_0008.py` pins the additive upgrade, the exact downgrade, the
byte-identical preservation of every pre-existing value, the deterministic backfill against twelve real
subject shapes, and the offline `--sql` rendering with no table rebuild. Migration head becomes 0008 and
`schema_diff` against a fresh `create_all` stays empty. An input that names a subject now also carries
evidence: where the caller supplied none and the record itself cites a document version, the input
inherits that citation (labelled `inherited_from`), which closes the case where a realistic derived
calculation left its dependency edge with NULL provenance - and a record that cites nothing still yields
nothing, because nothing is manufactured.

**Rejected.** A second identity framework beside `SubjectKey` (the bug was that the existing one was
bypassed, and a parallel convention is how one question gets two answers); a foreign key on
`subject_id` (there is no single target, and a constraint naming one table would forbid four legitimate
kinds); rewriting legacy strings into canonical keys during the migration (that is guessing an
identity, and the rows it would silently re-point are exactly the ones a person needs to review);
normalising `calculation.inputs` JSON or re-keying `identity_key` (it changes what a historical record
*is*); making the impact query re-run or invalidate anything (ADR-0012 keeps arithmetic out of this
layer, and an automatic invalidation is a decision the platform is not entitled to make); truncating a
long key as before (a write the reader cannot reproduce is the defect, not the mitigation); and
admitting `calculation` into the structured search projection to make it retrievable (a real question,
but a different one - the chain's six record types are ADR-0013's scope, and widening it to fix a
dependency bug would broaden search for the wrong reason).

## ADR-0017 — A drilling program is promoted like a report, into the planned half of the domain

**Status.** Accepted.

**Context.** Promotion admitted three of twenty-six classifications (`DDR`, `NPT`, `TIME_BREAKDOWN`),
so `drilling_program`, `program_target` and `procedure_record` had no production writer at all. A
correctly classified `DRILLING_PROGRAM` document could be ingested, extracted, indexed and searched and
still leave every plan-versus-actual comparison answering `NO_TARGET`: `plan_actual_summary` is
arithmetic over two sides, and only the actual one was ever written. The planned side was reachable
only from tests, which is not a production path.

**Decision.** A program is promoted through the same explicit act as a report - `records promote`, the
same `OperationalService`, the same `VersionPromoter`, the same session - and writes the engineering
tables that already model a plan. It leaves the report path entirely: `_promote_program` returns
before `_promote_report`, because a program states an intention and filing it as a day's work would put
next month's plan into this well's history.

The reader (`operations/program.py`) does no parsing. The PDF was read once at ingestion by the
existing extractor, and the typed `extracted_fields` - value, unit, `VALID` verdict, page/bbox locator -
are its only input. A second parser over the same bytes is a second opinion, and the two disagree the
first time either changes.

The contract is deliberately narrow, because a wrong planned number is worse than a missing one: it
becomes a variance somebody schedules work against. A field is used only when the extractor marked it
`VALID`; a section is planned only when the artefact names exactly one hole size (two sizes means
deciding which paragraph's depth belongs to which section, which is reading a layout, not a number, and
is refused as `AMBIGUOUS_SECTIONS`); a depth or mud weight with no unit is refused rather than allowed
to take the column's default; and the section's TD is the *deepest* stated measured depth, an
order-independent rule so two extractions that found the same numbers in a different order still
produce the same plan. Everything the program did not state stays `NULL` - a plan with no NPT allowance
is not an allowance of zero.

Identity is the document version, as it is for a report. Re-promoting re-finds the program instead of
adding a second, so the pass is idempotent. A new version of the same file is a new revision: the older
program is stood down (`is_current=False`, `SUPERSEDED`) *before* the new row is inserted, because
`uq_program_one_current` is a partial unique index and `(code, revision)` is unique - two current
revisions of one code are refused by the database even momentarily, which is why `create_program` now
accepts `revision` and `supersedes_id` on the insert rather than patching them afterwards. The old plan
keeps its row, its target and its provenance: "what did we plan at the time" stays answerable, and
nothing is recomputed.

A promoted plan arrives as `DRAFT` with `origin=DERIVED`, even though the page says "Status: APPROVED".
A machine reading a file has not approved anything; that is a person's act, recorded by the method that
validates the transition - the same standing every other derived row gets.

**Rejected.** A second PDF parser or a fixture-specific adapter (the extractor already produces typed,
located, unit-carrying fields; a program-shaped parser beside it is the duplication ADR-0006 exists to
prevent); creating `WellSection` rows from a program (a plan is written before the hole exists -
`ProgramTarget.section_id` is nullable for exactly this reason, and `_match_target` already falls back
to the section *name*, so inventing a drilled interval from an intention would fabricate the thing the
comparison is supposed to check); promoting procedures at the same time (the corpus program states no
procedure content the `procedure_record` model represents, and "promote everything in the PDF" is not a
contract); renumbering `DrillingProgram.revision` from the document's own "Rev 12" (the column counts
supersessions in this database and starts at 1; conflating the two would claim eleven revisions nobody
has, so the document's wording is kept verbatim in `attributes["document_revision"]`); trusting the
page's own approval status; widening the structured search projection to include `drilling_program` or
`program_target` (ADR-0013's six record types are a separate decision, and a new writer is not a reason
to broaden retrieval); and automatic recomputation when a plan is superseded (ADR-0016's rule - a
superseded plan makes dependent reports stale, it does not silently rewrite them).

## ADR-0018 — A section's depth is what the hole reached; the plan lives on the program target

**Status.** Accepted.

**Context.** `WellSection` is the context anchor for most drilling questions, and it holds paired
planned/actual columns for duration and mud weight. Depth is not such a pair: a section has one
interval, `top_depth_value`/`bottom_depth_value`, and `_section_actuals` reports it as the depth the
section *achieved*, while `PLAN_ACTUAL_METRICS` reads the planned depth from
`program_target.planned_depth_md_value`. `WellRepository.update_section` nevertheless accepted
`top_depth`/`bottom_depth` under `state=PLANNED` and wrote them to those same columns. The result was
measurable and wrong: writing a planned bottom depth of 10,450 ft and nothing else produced
`planned=10450, actual=10450, variance=0.0, status=ON_PLAN` for a hole nobody had drilled. Missing
actual data had become agreement with the plan, which is the one thing a plan-versus-actual comparison
must never say.

`well_section` was also the only table a document can describe that could not say where it came from -
no `origin`, no `provenance`, no citation - so `check_promoted_evidence` could not hold it to the
promise every other promoted row keeps. Both gaps had to close *before* anything populates sections,
because once rows exist the ambiguity is baked into stored data.

**Decision.** Three things, and deliberately no more.

*Depth is as-drilled, and a planned depth is refused.* `update_section` rejects `top_depth` and
`bottom_depth` under `PLANNED` with a message naming `planned_depth_md_value` as the place the plan
belongs. No column is added: the planned depth of a section is already modelled on the target that
governs it, and a second home for it would be two answers to one question. Duration and mud weight keep
their real pairs and are untouched.

*A section can say why it exists.* Migration 0009 adds `origin`, `provenance`, `document_id` and
`document_version_id` - the same four the rest of the domain carries, the same `KnowledgeOrigin`
vocabulary, the same nullable-citation shape 0005 gave `calculation` - plus `ix_section_version`.
`get_or_create_section` refuses a non-`MANUAL` origin with no evidence, `WellSection` joins
`_PROMOTED_MODELS`, and every pre-existing row is labelled `MANUAL`: no promoter has ever written a
section, so that is a statement rather than a guess. Provenance is set only at creation - a section is a
durable fact about the well, and the second document to mention it does not get to restate its source.

*Identity stays `(well_id, name)`.* The database already says so, and nothing here changes it. The
honest limit is recorded rather than papered over: two sections of one nominal hole size are distinct
only if the source names them apart, so a sidetrack needs a distinct name, and `sequence` remains
insertion order rather than a claim about physical order.

**Rejected.** `planned_top_depth`/`planned_bottom_depth` columns (the program target already owns the
planned interval; adding a second location for it would let the two disagree, and would be a schema
change made to avoid stating a rule); silently dropping a `PLANNED` depth instead of refusing it (a
caller that believes it stored a number, and did not, is worse off than one that got an error naming the
right column); backfilling historical `bottom_depth_value` into a new "actual" column (the value is
already the as-drilled one - moving it would be reinterpreting data this revision has no evidence
about); giving `WellSection` a lifecycle or an `identity_key` (the *plan* is revised and superseded, the
hole is not, and an identity key beside `(well_id, name)` would be a second identity framework);
normalising section names into a canonical grammar (the corpus has exactly two spellings and no
mechanism exists - inventing one now would be guessing which spellings mean the same hole, the decision
C2 has to make with real evidence in front of it); and populating any section from any source, which is
C2's work and is deliberately still absent - promotion creates no sections, and `ProgramTarget.section_id`
is still NULL on the real corpus.

## ADR-0019 — The drilling program creates the hole section it plans, and only the planned half of it

**Status.** Accepted.

**Context.** After C1 the platform held a promoted `drilling_program` and its `program_target`, and
`well_section` was still empty on the real corpus. `ProgramTarget.section_id` was NULL, so
`plan_actual_summary` - which iterates sections and reads the plan off the target - had nothing to
iterate. The planned side existed and was unreachable.

**Decision.** Promotion creates the section the program is a plan for, through the existing
`WellRepository.get_or_create_section`, inside the existing promoter and the existing transaction, and
passes its id to the existing `add_target(section_id=...)`. No new table, no new repository, no new
identity scheme, and no migration: every field this needs was added by 0009.

A program states that a 12 1/4 in hole *will be* drilled. That is a durable fact about the well - it is
what makes the section addressable before anyone spuds it - so the section is created with its name,
its nominal hole size, `origin=DERIVED` and the document provenance C1 already carries. It is emphatically
not a statement about what happened, so `top_depth`/`bottom_depth`, both durations and the actual mud
weight stay NULL; the planned depth stays on the target, where `PLAN_ACTUAL_METRICS` reads it from.
Copying it onto the section would be the exact substitution ADR-0018 refuses.

Identity is `(well_id, name)`, so revision 13 of the same program finds the section revision 12 created
instead of adding a second one: one hole, two plans, the superseded one keeping its own number.
Provenance is written only at creation, so the later revision does not restate where the hole came
from, and a section that already existed - drilled before its program was filed - is adopted with its
actuals and its `MANUAL` origin intact.

**Nothing else is attached.** The corpus's daily report names its section only in prose
(`Section: 12 1/4 in intermediate`); the one structured section-shaped field the extractor produces
from it is a `hole_size_in` of 12.25 whose excerpt is `12 1/4 in bit` - a bit size. Matching NPT hours
to the well's only section on that basis would look complete and be a guess, and the guess would become
silently wrong the day a second section exists. So `npt_record`, `well_operation`, `well_event`,
`problem_occurrence`, `knowledge_item` and the lesson/risk/recommendation family keep `section_id` NULL,
and a test asserts the evidence for that refusal rather than the refusal alone.

**Rejected.** Normalising `12 1/4 in intermediate` onto `12 1/4 in` (no canonical section grammar
exists, and inventing one here would be guessing which spellings mean one hole - the decision needs a
corpus with real variants in front of it); attaching actual records by hole size, by "the well's only
section", or by first-section fallback (§26's latent corruption, invisible until the second section
arrives); writing the program's planned depth into the section so the comparison "has an actual"
(fabrication); creating a section per program *revision* (the plan is revised, the hole is not);
giving the promoter its own section-name parser (C1's `SectionPlan.name` is already the document's own
wording, deterministically derived, and a second reader would be a second answer); and adding
`well_section` to structured search or retrieval, which stays the six record types ADR-0013 defines.
