# The engineering domain: what is stored, what is computed, and where a number came from

Phase 0 gave the platform a document record and a knowledge layer on top of it. This document is the map
of the third thing it keeps - the engineering domain: reports, operations, NPT, problems, programmes,
procedures, lessons, risks, costs and the people and rigs named in them - plus the boundaries that decide
what this layer deliberately does *not* do.

It is written as a map rather than a specification because the interesting questions are about ownership:
which table a value lives in, who is allowed to write it, and what has to be true before it can be quoted.
The decisions behind each rule are in `DECISIONS.md` (ADR-0009 for the shape of the layer, ADR-0010 for
promotion and durations, ADR-0011 for aggregation and snapshots).

## Who contains whom

```
company ── project ── field ── well ── well_section ── well_operation ── well_event ── npt_record
                          │                │                                   │
                          │                └── program_target ── drilling_program   problem_occurrence
                          └── ddr_report                                     │
                                                                              └── cost_item (via npt_id)
rig ── service_company        procedure_record        risk_record        lesson_learned
                                                       best_practice      recommendation
                                                       field_pattern
problem_definition ──< problem_occurrence
```

`problem_definition` is the reusable normalized concept (`stuck_pipe`, `lost_circulation`, and so on); `problem_occurrence` is the evidence-backed instance at a well, section, operation, event, or NPT. The definition is canonicalized by the existing vocabulary matcher, while occurrence provenance remains authoritative. A definition without source provenance is valid when it is a system/domain concept rather than an extracted claim.

A `well_section`'s own depth columns (`top_depth_value`/`bottom_depth_value`) are the **as-drilled**
interval - `plan_actual_summary` reports them as the achieved depth - so the *planned* depth of a section
is `program_target.planned_depth_md_value` and a `PLANNED` write of a section depth is refused
(ADR-0018). Duration and mud weight do have genuine planned/actual pairs on the section. Since 0009 a
section also carries `origin`/`provenance`/`document_id`/`document_version_id`, so one read out of a
document can show the document and one a person entered stays `MANUAL`.

`company`, `project`, `field`, `well` and `well_section` are the hierarchy the well registry owns; nothing
below re-parents anything above. The five tables on the first line are the operational spine, and each of
them - plus `ddr_report`, the versioned records, the lessons, the costs, the patterns and the engineering
record (`calculation`) - can carry `document_id`, `document_version_id`, a `provenance` list, an `origin`
and a `created_by`. That is the whole of the evidence model: there is no second one, and
:func:`drilling_intelligence.database.integrity.check_promoted_evidence` reports a derived row that cites
nothing.

Three kinds of claim live side by side and are not interchangeable:

| a row says | it is stored in | it may be quoted as |
| --- | --- | --- |
| "the file says X" | `document_version` + `extraction_artefact` | what a source states, with a locator |
| "X is the value for this well" | `knowledge_item` (facts) + `knowledge_relation` (edges) | a claim with a status, a validity window and a source - or a recorded conflict |
| "X happened, to this well, in this hole section" | the operational and engineering tables | a record, which a person confirms; promotion never does |
| "X was arrived at from Y this way" | `calculation` + `calculation_input` | an engineering record: method and version, inputs with units, outputs, assumptions, validation |

## What the domain adds to the schema, and what it refuses to

Three migrations carry the domain. **0004** is the shape: sixteen tables, their indexes, four partial unique
indexes carrying the "one current revision per code" rule, and no backfill of anything - a workspace that
has documents but no promoted records upgrades into sixteen empty tables and stays exactly as readable as
it was. **0005** is a correction found while auditing the layer against its own brief: `calculation` and
`calculation_input` have existed since 0001 with a read path and no writer, and the table predated the
evidence convention, so it could not record where its numbers came from. 0005 adds `origin`, `created_by`,
`identity_key`, `document_id`, `document_version_id` and `attributes` to `calculation`, plus the unique
index the "re-run is not a second copy" rule needs, and backfills `origin = 'MANUAL'` on every existing row
- the honest statement that nobody has seen its source. **0006** restores the legacy document hash index
that the 0002 SQLite batch rebuild omitted, so historical downgrade paths can safely remove it; it is
otherwise a no-op for the domain schema. **0007** adds `problem_definition`, links every existing
`problem_occurrence` to one deterministic canonical definition, and preserves the occurrence's identity,
timestamps and evidence. No migration renames or drops a column, and none touches a table the Knowledge
Layer owns.

Things this layer does *not* do, each because doing it would make a number unaccountable:

- **It does not compute a risk score.** `probability`, `impact` and `severity` are preserved when a
  source states them, with the scale they were stated on (`risk_record.scale`, default `MATRIX_5X5`);
  when they are absent they stay `NULL`. There is no scoring methodology in this repository to invent one
  with, and a score that a platform derived from a 5×5 nobody agreed is the kind of number that ends up
  in a safety case unchallenged.
- **It does not infer a root cause.** `cause_status` is `KNOWN`, `INFERRED`, `UNKNOWN` or `CONFLICTED`, and
  `INFERRED` means a person inferred it and said so. A promoted row arrives `UNKNOWN`.
- **It does not convert currency.** `cost_item` totals per currency only: no rate, no inflation, no AFE
  logic, and an absent side of a variance is `None` rather than zero. A summary also never filters cost
  lines by the lifecycle state of the record they cite - a cancelled AFE line is still a spent dollar.
- **It does not run arbitrary engineering calculations.** The sole V1 exception is the explicit
  `npt.hours_rollup` method in `EngineeringService`: it reads promoted NPT rows for one well, validates their
  evidence and scope, performs deterministic finite-hour summation, and records the result through the
  existing calculation tables. No other method is executable. `calculation` still stores method id and
  version, inputs with their units, outputs, assumptions, validation, uncertainty, confidence and
  provenance; review, indexing, stale detection and UI display never execute it. An additional method
  needs its own narrow capability contract rather than a generic computation engine.
- **It does not store a timeline.** `drillintel timeline` projects the tables into a sequence at read time.
  An entry exists only because a record carries its own timestamp, and the undated ones are reported as
  undated rather than placed at the end of a guess.
- **It does not cache an answer.** `fields summary`, `patterns find` and a per-well breakdown are
  recomputed on every call. The one persisted aggregate is a `field_pattern` snapshot, which stores the
  query that produced it so that "has this changed" is a diff against the records (`patterns stale`)
  instead of a stale number.
- **It does not run a model.** Nothing in `operations/`, `engineering/`, `lessons/` or `intelligence/`
  imports a client, opens a socket or calls Ollama. Extraction and classification stay where they were,
  behind their own adapters.
- **No duplicates of existing concepts.** There is no `Well2`, no parallel `document`-shaped table, no
  per-record `source_note` free text. A record points at `well.id` and at `document_version.id`.

## How a record gets written, and how it can be re-run

Promotion is a separate step (`drillintel records promote`, or `ingest --promote` when a person asks for
it). For each report-shaped document version in scope, `VersionPromoter` reads the *stored artefact* -
never the file - and maps it: `NptRecord`s from the NPT lines, `WellEvent`s and `ProblemOccurrence`s from
what those lines say happened, `WellOperation`s from the daily report's activity rows, and the `DdrReport`
itself. Every write goes through the generic `record_*` API, which is create-or-return on a content
identity key, so the pass is idempotent: a second run creates nothing and reports `unchanged`. A line it
cannot place is skipped and counted (`ZERO_NPT`, `NOT_A_REPORT`, an unknown well name), never filed
somewhere plausible.

The generic engineering record has the same shape, and the only production writer in V1 is the explicit
NPT method:
:func:`~drilling_intelligence.engineering.service.EngineeringService.record_npt_rollup` reads authoritative
promoted `NptRecord` rows for exactly one well, validates status, finite durations, source provenance,
current document versions and evidence completeness, sums deterministic hours, and delegates persistence
to :func:`~drilling_intelligence.engineering.repository.EngineeringRepository.record_calculation`.
`npt.hours_rollup` version `1` is the registered capability; its method identity, scope, rounding policy,
validation counts and input evidence are stored with the result. It never overwrites history. Re-running
the same evidence is an idempotent no-op, while a changed evidence set is a new content identity and an
explicit supersession is required for a revision chain.

The repository write path still requires a `method_id`, refuses a row whose `origin` is not `MANUAL` unless
it cites its evidence, hashes its content into `identity_key` so a re-record returns the row it wrote instead
of a twin (the unique index makes that hold across concurrent callers too), and writes one
`calculation_input` per input so that "which results used this value" is an indexed query. A record whose
content differs is a new row, and `supersedes_id` marks the parent `SUPERSEDED` with the child at
`revision + 1` - the chain is append-only, and `current_only` means "nobody has superseded it", decided by
the chain rather than by a status column that can drift. Other methods may remain stored-only. Review
classifies rows as `EXECUTABLE`, `STORED_ONLY`, `INCOMPLETE`, `STALE` or `UNRESOLVED` without invoking an
executor. `drilling_intelligence.core.units` parses indexed values into a number and a unit; it does not
invent a dimension or perform the NPT aggregation.

The corpus the tests run against - two wells, one NPT export, one daily report - produces 22 rows: 2
reports, 9 operations, 3 events, 5 NPT records and 3 problem occurrences, 59.25 h of non-productive time
in the field, 28.75 h of it stuck pipe across both wells, and two of the five NPT rows undated. Those
numbers are asserted in `tests/integration/test_field_intelligence.py` and `test_operations_promotion.py`,
which is where a change to the mapping shows up as a failing test rather than as a quiet edit to a
spreadsheet someone trusted.

## Reading it from a terminal

```
$ drillintel fields summary --field "North Cormorant"
field: North Cormorant (2 well(s))
non-productive time: 59.25 h over 5 record(s); 2 undated, 0 without a duration
by category: equipment_failure 12 h in 1 row(s) on 1 well(s), other 18.5 h in 2 row(s) on 1 well(s), stuck_pipe 28.75 h in 2 row(s) on 2 well(s)
problems: 3 occurrence(s) of equipment_failure x1 (1 well(s)), stuck_pipe x2 (2 well(s))
events: 3, lessons: 0, reports: 2
hours are summed per record: if two files describe one event, both are counted, and the record that says so is `drillintel records list --table npt`

$ drillintel records list --table npt --field "North Cormorant"
5 npt row(s)
id                              category          started_at         duration_hours  duration_basis  root_cause_status  status
npt-db7b138d4684430891e691d3a   stuck_pipe        2025-04-02T00:00   22.25         STATED      UNKNOWN         CANDIDATE
npt-7998ec7a481f488face5c459f   stuck_pipe        2025-06-13T00:00   6.5           STATED      UNKNOWN         CANDIDATE
npt-6154d612bba044e78817af844   equipment_failu   2025-06-14T00:00   12            STATED      UNKNOWN         CANDIDATE
npt-008058fa78d54c979aabf7ece   other             -                  12            STATED      UNKNOWN         CANDIDATE
npt-880a5f7e4bd9479cbb6835177   other             -                  6.5           STATED      UNKNOWN         CANDIDATE
```

The last line of that table is the whole design in one cell: `CANDIDATE`, because a promotion is a proposal
nobody has accepted. A decision has to be attributed: `patterns confirm` requires `--by`, and
`OperationsRepository.set_record_status` - which is how a record leaves `CANDIDATE` - refuses to record one
with no author. Moving a record to `CONFIRMED` is not a CLI verb in this phase either, for the same reason
as the approvals below: the person deciding needs to read the evidence, and a terminal can print a
`document_version_id` but not a spreadsheet.

| command | what it reads | behind it |
| --- | --- | --- |
| `records list/summary/promote` | `ddr_report`, `well_operation`, `well_event`, `npt_record`, `problem_occurrence` | `OperationsRepository` / `OperationsService` + `VersionPromoter` |
| `records rollup` | promoted NPT rows and their existing evidence/provenance | `EngineeringService.record_npt_rollup` + `EngineeringRepository.record_calculation` |
| `records impact` | indexed calculation inputs and current document-version state | `EngineeringRepository.calculation_impact` (read-only; `CURRENT` / `STALE` / `UNRESOLVED`) |
| `records review` | one well's authoritative domain rows, sections, conflicts, relations, evidence, calculations and plan/actual projection | `DomainReviewService` + the existing repositories and `CitationAuditor` |
| `timeline` | the same tables, plus the versioned records and the well itself | `intelligence.timeline.build_timeline` |
| `fields list/summary/offsets` | per-field rollups, and other wells with the same recorded problems | `FieldIntelligence`, `IntelligenceService` |
| `patterns find/snapshot/list/stale/confirm/recommend` | `problem_occurrence` groupings and `field_pattern`/`recommendation` rows | `intelligence.patterns` |
| `lessons list/practices/counts/show` | `lesson_learned`, `best_practice`, `recommendation` | `LessonRepository` |
| `doctor` | everything above, as counts and as integrity checks | `check_operational_integrity` |

Every one of them takes `--well`, `--field` or `--project` (a name or an id) and `--json`, and every one of
them prints the same dictionaries `--json` emits rather than a second implementation of the query.

That table is also the boundary: there is **no mutation CLI for procedures, programmes, risks, costs, rigs or
service companies**. Those are written through `EngineeringRepository`, `RiskRepository` and
`CostRepository` (and their revision/approval methods), which is where the invariants live. The read-only
`records review` boundary exposes those existing rows for inspection without advertising a workflow -
approve a programme, retire a procedure - that a shell history can re-run. Approving a lesson is likewise
not a CLI verb: the repository's `LessonRepository.approve(lesson_id, by=..., note=...)` refuses an
unattributed approval, refuses the lesson's own author as its approver, and refuses a lesson that cites no
evidence. These decisions belong in a human review flow, not in the read boundary.

## Domain Review: the read-only human decision boundary

`DomainReviewRequest(well_id=..., lifecycle="current"|"history", limit=0, verify_citations=False)` is the
single subject-scoped read contract. `DomainReviewService` opens the database's explicit `read_only`
session and composes the existing rows; it never writes a snapshot, audit event, cache entry, search
sidecar row or migration. `drillintel records review --well A-3 --json` is a thin rendering of the same
contract, and `--verify-citations` opts into the existing file citation auditor.

The result preserves `record_to_dict` data, raw status/record-state, current/history identity, scope,
provenance/evidence references, knowledge relations, open and resolved conflict candidates, and explicit
verification metadata. Programs and procedures use their repository inheritance rules; `program_target`
remains owned by its `drilling_program`; and plan/actual rows are delegated to
`EngineeringRepository.plan_actual_summary`, so WellSection's as-drilled depth and missing-side statuses
are not reimplemented here. Calculations carry method/version, indexed inputs, outputs, assumptions,
validation, uncertainty, status and provenance. The review projects their capability as
`EXECUTABLE`, `STORED_ONLY`, `INCOMPLETE`, `STALE` or `UNRESOLVED`: only a complete current NPT V1 contract
is executable, an unknown complete method is stored-only, and malformed evidence/scope/validation is
incomplete. A cited old source is stale and a missing source is unresolved. These labels, dependency
impact and source-navigation hints are read-only; the review never invokes NPT arithmetic or repairs a
row.

Records are stably ordered by table/id (sections and relations have explicit tie-breakers), conflicts
carry both candidates without choosing a winner, and the output contains observations/counts only: no
risk, quality, confidence, approval, safety or business decision is invented. Search is deliberately not
a discovery source for this boundary; the result says `search_sidecar_used: false`. A citation audit
reports `MATCH`, `MISMATCH`, `UNREADABLE` and `NOT_CHECKABLE` through the existing evidence contract and
never changes the registry.

## How this coexists with search and knowledge

The search index carries three kinds of unit, all ranked through the one BM25 path in
`search/ranking.py` and rebuilt/pruned by the one sidecar:

- **document chunks** — extracted text (and diagnostics), cited to a page/sheet/cell;
- **knowledge-fact chunks** — the facts derived from those artefacts, weighted above the prose they came
  from;
- **structured records** — one unit per authoritative operational/domain row (`problem_definition`,
  `problem_occurrence`, `npt_record`, `well_event`, `lesson_learned`, `recommendation`), projected by
  `search/structured.py` into the same sidecar under a deterministic identity `structured:<type>:<row id>`.

The structured projection is the same idea as the document half, stated once: the database is the
authority, the sidecar is disposable, and a record's text is its own deterministic fields — never a copy of
the source document's sentence. A promoted `NptRecord` whose wording is also reachable as an indexed
document chunk is *not* re-chunked as prose; it is one record row whose provenance keeps the
`document_id`/`document_version_id` links, so the two source types meet at a locator rather than at a
duplicated row. Evidence multiplicity is not record multiplicity: a row with three evidence relationships
is still one searchable record, and the evidence stays in its provenance.

Lifecycle is respected at build time, by the domain's own rules, and lifecycle state stays out of indexed
text (ADR-0008): a rejected `NptRecord`/`WellEvent`/`ProblemOccurrence`, a superseded lesson revision, or
a superseded recommendation leaves the searchable projection the way a superseded document version does,
while its row remains in the registry — reachable by id, but not presented as "what to act on".

So: search answers "where is this written, in which file — or which record says it", knowledge answers
"what does that source assert about this well", and the domain answers "what happened, how long it took,
what it cost and what was learnt". A `drillintel search "stuck pipe"` hit and a
`drillintel records list --table npt` row are expected to point at the same version; neither is a copy of
the other, and `SourceLocator.ref` is shared by both.

And because the sidecar is a projection that can be stale, a hit is not yet evidence: the retrieval layer
(ADR-0013) re-reads every search candidate from the authoritative database before it may answer, drops
what no longer is - with the reason - and returns the rest as deterministic, provenance-carrying records
that a reader can cite without re-checking.

The evidence package (ADR-0014) is the answer a reader actually keeps: a set of topics composed through
retrieval into one deduplicated set of verified records, with a per-topic account of what was returned,
dropped and broadened, and a content identity that the same database state always earns for the same
question. A package stores its own query, so "is this still true?" is answered by re-asking and diffing -
added, removed, changed - never by a timestamp. Nothing about a package is stored; it is a read with an
address, and the read is the only thing that can go stale.

The citation audit (ADR-0015) answers the one question the re-reads above do not: *does the file still
say what the citation recorded?* Retrieval proves the row exists; the package proves the answer holds;
the audit re-opens each item's source file and re-checks the recorded citation against it - the file's
hash, and, where the item quotes its region, the recorded location re-read and compared with the recorded
excerpt. The states are explicit, not a silent pass: a mutated source is a named `MISMATCH` (with the
recorded and the current hash), a deleted one `UNREADABLE`, and a row that cites no file is
`NOT_CHECKABLE` - counted, not invented away. A table cited as a whole is excerpted as its rendered rows,
so on a hash-verified file the audit reports file identity rather than a false content mismatch. It is a
read that re-checks what the evidence cites, opt-in via `evidence query --verify`, and it stores nothing.

## Names in the schema, where they differ from the sketch

The Phase 1 brief listed the fields each entity must support. Five of them arrived under different names or
units, and each difference is a decision a reader should be able to find rather than discover:

| the brief called for | the row stores | why |
| --- | --- | --- |
| `npt_record.duration_minutes` | `duration_hours` (REAL) + `duration_basis` + `duration_text` | Hours are what an NPT sheet states and what every aggregate in this layer adds up; a minutes column would either round a stated 22.25 h or force a conversion step at every read. A cell that *does* state another unit - `90 min`, `1.5 days` - is converted through `core.units`, which is the unit authority, and the wording stays in `duration_text`. |
| `well_operation.name` | `label` | `name` is what the hierarchy tables use for identity; `label` is what a report called an activity, and the two must not be confused when a report is re-read. |
| `well_event.title` / `event_time` | `label` / `occurred_at` (+ `occurred_at_text`) | One wording column and one timestamp column across the spine, so "the report's own words" and "the date the platform parsed" are the same pair of ideas in every table. |
| `problem_occurrence.title` | `code` + `description` | A problem's identity is its code and its occurrence; a second free-text name would be a third place to be wrong. |
| `ddr_report.revision` | the cited `document_version`'s revision | A DDR is one day as filed by one version of a file. Its revision *is* the document's revision, so repeating it in the row would let the two disagree. |

The generic engineering record needs no such row: `calculation` already used the brief's names for what it
stores - `method_id`, `method_version`, `inputs`, `outputs`, `assumptions`, `validation`, `uncertainty`,
`confidence`, `provenance`, `status`, `supersedes_id` - and what it lacked was the evidence convention, which
migration 0005 adds rather than what the brief's own wording implies it should already have had.

The rest of the sketch is present under the names the brief used: `cause_status`-style columns exist as
`root_cause_status`/`immediate_cause_status`, `cost_impact` as a value/unit pair
(`cost_impact_value`/`cost_impact_unit`), and `source/provenance` as `provenance` plus
`document_id`/`document_version_id`/`origin`/`created_by`.

## Portability

Everything here stays in the subset SQLite and PostgreSQL share: partial unique indexes instead of trigger
logic, `JSON` columns read by the application rather than queried with `JSONB` operators, `TEXT` for
verbatim wording, `REAL` for values that came from a spreadsheet cell, `TIMESTAMP` stored in UTC. The one
SQLite-specific thing is `PRAGMA foreign_keys=ON`, which the engine issues on every connection, and the
integrity checks exist because cross-well link rules are not expressible as constraints on either server.

One exception is worth naming, because it looks like an oversight. The citation columns 0005 adds to
`calculation` carry **no foreign keys**. SQLite cannot add a constraint to a table it has already filled
without rebuilding the table, and a rebuild is not expressible in offline mode - the `alembic upgrade --sql`
text a DBA reviews before applying a migration to a shared file - and would leave a fresh workspace and a
migrated one holding structurally different tables. So the columns are plain, the model matches them (a
difference between the two is a bug waiting to be trusted), and the promise is kept where it can be
policed: `check_promoted_evidence` and the document-version checks report a citation that cannot be opened.
On PostgreSQL the same migration is a fast `ADD COLUMN`, and adding real constraints there later is a
one-line migration that needs no data movement.

## V2 ingestion and promotion boundary

The domain model is not expanded by classification alone. `operations/contracts.py` is the static
contract registry for the current production path. It has a completeness guard over every
`DocumentClassification`; only `DRILLING_PROGRAM`, `DDR`, `NPT`, and `TIME_BREAKDOWN` resolve to
existing promotion handlers. A handler must read the stored normalized artefact, not search or
narrative prose, and must retain row/field provenance, units, quality, linkage and source-version
identity. A row with no trustworthy answer is reported as an explicit outcome rather than filled with
zero, a nearest well, a guessed unit, or an inferred root cause.

The promotion result vocabulary is version-level and exclusive: `ELIGIBLE`, `PROMOTED`, `UNCHANGED`,
`UNSUPPORTED`, `AMBIGUOUS`, `MISSING_ARTEFACT`, `MISSING_WELL`, `MISSING_PROVENANCE`,
`INVALID_FIELDS`, `CONFLICT`, and `ERROR`. Row counts continue to distinguish created, unchanged and
conflict. A source edit never overwrites a row that may have been human-confirmed; the conflict is
reported and the prior row remains. A newer program revision supersedes the previous current program
without deleting its targets or changing actual section measurements. A re-run of one source version
is identity-based and idempotent.

See [`DOCUMENT_DOMAIN_COVERAGE.md`](DOCUMENT_DOMAIN_COVERAGE.md) for the per-class evidence matrix,
[`PRODUCTION_INGESTION_V3_CERTIFICATION.md`](PRODUCTION_INGESTION_V3_CERTIFICATION.md) for the
certification record, and `tests/golden_corpus/manifest.json` for the deterministic corpus index.
