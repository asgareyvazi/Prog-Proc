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
    section, and the new rows point at those ids.
*   **Evidence is inherited, not reimplemented.** Every record table carries
    `document_id`, `document_version_id`, a JSON `provenance` list, `origin` and `created_by`, and joins
    the existing graph through `knowledge_relation` rather than a parallel edge table - which is why
    `RELATION_ENDPOINT_MODELS` is the one registry of what an edge may point at, and why a table missing
    from it is a *rejected write* rather than a dangling edge. `check_promoted_evidence` closes the loop:
    a row that says it came from a document and cites nothing is reported.
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
    their own author as an approver.
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
