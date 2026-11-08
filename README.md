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

> **Current context (2026-11-08):** Prog-Proc remains **Pre-Alpha**, local-first, and single-user; SQLite is the only qualified database, PostgreSQL is **NOT QUALIFIED**, and the 26-class matrix remains 14 `END_TO_END_CERTIFIED` / 7 `KNOWLEDGE_SUPPORTED` / 5 `EXTRACT_ONLY` across 12 named handlers. C1 code/test SHA `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5` is pushed to the authorized branch. Its exact-SHA Linux run [`42381352913`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42381352913) passed 9/9 jobs: CPython 3.11 and 3.14 each collected 2,795 (2,734 passed, 61 skipped, 0 failed); Ubuntu 26.04/3.14 collected 2,795 (2,794 passed, 1 skipped, 0 failed). The exact-SHA Windows run [`42381353106`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42381353106) passed 14/14 jobs. Windows CPython 3.11 and 3.14 each collected 2,795 (2,732 passed, 63 skipped, 0 failed); offscreen UI was 29/29 on both versions, all six stability lanes completed 15/15 attempts, and clean-install plus PowerShell 5.1/7 run-card checks passed. GitHub pytest log/artifact downloads returned EOF, so the Windows skip reasons were not independently recoverable. A focused grouped local suite passed 157 tests and skipped 2 shallow-history integrity checks (0 failures); a separate MinerU boundary run passed 18. The single-process `pytest -q` attempt hit the 30-minute tool limit around 90% without a summary; that local attempt is **NOT PASS** (it does not override the Linux exact-SHA full-suite results). Ruff, format, mypy, compileall, exception policy, Alembic head, and a disposable-workspace doctor smoke passed locally. MinerU 4.1.7 was exercised via a separately installed CPU `mineru-kit` on two synthetic PDFs through the production adapter; OCR model download failed before inference, so OCR accuracy is **NOT RUN**. The extraction-boundary correction is commit `8cc4c0fec923329f9371ddd1ce222a5e8ca0df80`; its exact-SHA Linux run [`42449344642`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42449344642) passed 9/9 jobs and Windows run [`42449344633`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42449344633) passed 14/14 jobs. Linux CPython 3.11/3.14 each collected 2,795 (2,734 passed, 61 skipped, 0 failed), and Ubuntu 26.04/3.14 collected 2,795 (2,794 passed, 1 skipped, 0 failed). Windows CPython 3.11/3.14 each collected 2,795 (2,732 passed, 63 skipped, 0 failed); offscreen UI was 29/29 on both runtimes, six stability lanes completed 15/15 attempts, and clean-install plus PowerShell 5.1/7 run-card checks passed. Both runs show a non-blocking Node.js 20 deprecation notice for an artifact action forced to Node.js 24. The documentation/mission-report follow-up is a separate SHA with independent exact-SHA CI; evidence does not transfer between SHAs. Physical Windows display acceptance remains **NOT RUN**. Product status is **PARTIALLY QUALIFIED — PRE-ALPHA**, not production approval. Current evidence and owner gates are in [`docs/PROJECT_CONTEXT.md`](docs/PROJECT_CONTEXT.md) and the [current Persian mission report](docs/mission-reports/AUTONOMOUS_MISSION_2026-11-08_MINERU_DDR_FA.md).

The following inventory is a product overview; its capability labels do not substitute for current test or CI evidence:

| area | state |
| --- | --- |
| Document ingestion: scan, content-hash planning, incremental runs | **implemented, tested end to end** |
| Extraction: PDF (PyMuPDF), XLSX (openpyxl), DOCX (python-docx), text/CSV | **implemented, tested on a generated corpus of real files** |
| Field extraction with typed units and per-value provenance | **implemented, tested with positive *and* negative ground truth** |
| Classification over the taxonomy with evidence and confidence | **implemented, deterministic (no model calls)** |
| Registry: versions, supersede/duplicate links, revisions, status, audit trail | **implemented, tested** |
| Schema and migrations | **implemented** (Alembic owns the schema; SQLite per workspace) |
| Search: BM25 over chunks, filters, cited results, optional re-verification against the file | **implemented** (a disposable SQLite sidecar, rebuilt rather than migrated) |
| Retrieval & Evidence: search locates candidates, retrieval re-reads each from the authoritative database and returns verified, deterministic, provenance-carrying evidence (or the reason a candidate is not authoritative) | **implemented, tested forensically** (`docs/DECISIONS.md` ADR-0013) |
| Evidence packages: a set of topics composed into one deduplicated, content-addressed answer that stores its own query and reports staleness as a named diff (added / removed / changed) | **implemented, tested forensically** (`docs/DECISIONS.md` ADR-0014) |
| Citation auditor: re-reads each item's source file (hash + recorded location) and reports whether the citation still holds - MATCH / MISMATCH / UNREADABLE / NOT_CHECKABLE, opt-in via `evidence query --verify` | **implemented, tested forensically** (`docs/DECISIONS.md` ADR-0015) |
| Knowledge: typed facts with per-value provenance, entities, edges, conflict records and human resolutions | **implemented, tested end to end** (`docs/DECISIONS.md` ADR-0008) |
| Operational records: DDR as a first-class record, operations, events, NPT, problems — written by an idempotent, self-reporting promotion | **implemented, tested end to end** (`docs/DECISIONS.md` ADR-0010) |
| Engineering records: programmes with targets, versioned procedures, lessons, best practices, recommendations, risks, costs, rigs, service companies | **implemented, tested** (revision chains and lifecycles enforced in the schema) |
| The generic engineering record: a computed number stored with its method, version, inputs and units, assumptions, validation, confidence and evidence | **implemented, tested** (`record_calculation`; re-running is a no-op, superseding keeps the old row) |
| Executable calculation V1: the explicit, fail-closed NPT roll-up through `EngineeringService` | **implemented, tested end to end** — NPT roll-up V1 is the only registered executable method; review never recalculates |
| Field intelligence: derived timelines, NPT/problem rollups, offset candidates, recurring patterns with staleness checks | **implemented, tested on a two-well golden field** (`docs/DOMAIN.md`) |
| Decision pack: one deterministic, evidence-backed read model per well/field/project - execution vs plan, per-currency economics, stated risk, learning, patterns, recommendations, calculations, freshness, limitations and observations (`fields decision`) | **implemented, tested, query-count scale-certified at 41 SELECTs regardless of row count** (`docs/DECISIONS.md` ADR-35) |
| Comparison pack: two or more wells side by side on the same recorded basis - per-metric values with `COMPARABLE`/`INCOMPARABLE`/`MISSING`/`UNRESOLVED` states, unit and risk-scale identity gates, offset-candidate discovery, forensic evidence, limitations (`fields compare`) | **implemented, tested, query-count scale-certified at 6 + 39 SELECTs per subject** (`docs/DECISIONS.md` ADR-36) |
| Analyst question catalog: sixteen fixed question ids answered as typed JSON with declared params, scope, evidence, lifecycle and missing-value semantics - no natural-language parsing, no AI (`analyze`) | **implemented, tested** - every answer reuses the certified read paths unchanged (`docs/DECISIONS.md` ADR-36) |
| Engineering report: a deterministic `engineering-report/1` document composed from the decision or comparison pack, with standalone HTML, inline SVG charts, a source-lineage appendix, and a native workbench preview (`fields report`; `--verify-citations` is optional) | **implemented, tested** - a citation match is not scope, freshness, or complete coverage; selected wells are the report subject (`docs/DECISIONS.md` ADR-37, ADR-38, ADR-39, ADR-40, ADR-41) |
| Domain CLI: `intake` add/list/preview/decide/register, manual `ddr` create/list/show/actions/save/submit/revise/decide, `config validate`, `mineru validate`, plus `records`, `records rollup`, `records review`, `timeline`, `fields`, `patterns`, `lessons`, `evidence`, and `doctor` | **implemented through existing lifecycle services** — DDR CLI input is bounded strict JSON; save requires a complete snapshot and freshness token; submit leaves operations candidate, and review uses only `ReviewActionService`. Declared actor strings are not authenticated. Intake registration creates registry evidence only after human review; it does not promote domain facts. Config validation is local/read-only; MinerU validation separates runtime readiness from OCR qualification. Focused CLI integration evidence: `tests/integration/test_ddr_cli.py`; exact-SHA qualification is tracked separately. |
| Optional Desktop Workbench (`drillintel-ui`) | **Implemented; Linux and Windows Qt-enabled offscreen CI PASS on C1 SHA `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5`** — PySide6-Essentials is optional. It includes governed review, Intake preview/registration, and Daily DDR draft/save/submit/revision with a read-only predecessor/successor comparison of exact fields, authored activity changes/order, and provenance. DDR submit leaves candidate rows; confirm/reject require a reason and a declared actor ID distinct from `created_by` (not authenticated). Mutations run outside the GUI thread. Local Qt tests blocked by host libraries are **SKIPPED**, not PASS; Linux run `42381352913` includes the offscreen jobs. Windows run `42381353106` passed 29/29 offscreen UI tests on both Python runtimes; physical Windows display acceptance remains **NOT RUN**. The historical registration/refresh failure root cause remains unproven. |
| Ollama provider adapter: `drillintel ai status` probes `/api/tags`; completion and embedding calls return non-authoritative text or vectors | **implemented, not on the engineering path** — an embedding is accepted only when it is finite, one vector per input, and names the requested model; a path, query, or credential in the endpoint sends no request; `doctor` does not probe; `openai-compatible` has no adapter (`docs/DECISIONS.md` ADR-51, ADR-52) |
| Skills and additional engineering-calculation *engines* | planned — NPT roll-up V1 is the deliberately narrow exception; additional methods need an explicit capability contract and evidence policy, not a generic computation engine |
| Risk scoring methodology, a cost/AFE engine, plan-vs-actual dashboards | **deliberately not built** — what each one refuses to invent, and why, is in `docs/DOMAIN.md` |

The built-in application path needs no GPU, model download, or server. `mineru` is a separate,
optional runtime for PDF layout/OCR. The application defaults its model source to `local`, so ordinary
extraction only uses preinstalled model files and does not implicitly fetch them. An operator may
explicitly select a remote model source in configuration; that choice can contact its configured
provider during parsing and must be governed by the operator's network/data policy. Without the
runtime or local models, the built-in extractors carry on and report when a scanned PDF needs OCR. The Ollama adapter is also
optional and local. `drillintel doctor` names the configured provider and does not contact it.
`drillintel ai status` probes only when asked, and a successful probe does not write a record.
Installing Ollama changes that probe; it does not change extraction, promotion, search, or citation
audit.

## Quick start

Windows uses PowerShell, not the Bash block below. The install, smoke test,
optional workbench, and the unexecuted workstation checklist are in
[`docs/WINDOWS_TESTING.md`](docs/WINDOWS_TESTING.md). A green Linux job is
not a Windows acceptance.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                            # the editable install resolves the package; no PYTHONPATH needed
.venv/bin/ruff check src tests migrations --output-format=concise
.venv/bin/ruff format --check src tests migrations
.venv/bin/python -m compileall -q src tests migrations
```

For a local, human-governed intake pass, add a source reference, inspect its transient preview, record a disposition, and explicitly register only reviewed bytes:

```bash
drillintel intake add /path/to/handover.txt --workspace /path/to/workspace --by operator
drillintel intake list --workspace /path/to/workspace --status QUEUED --json
drillintel intake preview ITEM_ID --workspace /path/to/workspace
drillintel intake decide ITEM_ID --workspace /path/to/workspace --decision REVIEWED --by reviewer --reason "source inspected"
drillintel intake register ITEM_ID --workspace /path/to/workspace --by registrar --reason "register reviewed source" --well WELL_ID
```

Registration links the source through the existing Document Registry and does not promote domain records. The optional desktop workbench also exposes **Intake** and **Daily DDR** navigation pages. Daily DDR submission creates candidate operations; confirmation/rejection is a separate review action. See `docs/PROJECT_CONTEXT.md` and the relevant ADRs for the lifecycle contract.

The headless `ddr` command is a service adapter for manual authoring, not an import path. Create a strict UTF-8 JSON snapshot (max 1 MiB); `save` requires every field so omitted values are never silently cleared:

```json
{
  "report_date": "2026-11-07",
  "report_number": "DDR-2026-311",
  "shift": "Day",
  "summary": "Operator-entered shift summary",
  "activities": [
    {
      "operation_type": "drilling",
      "label": "Drilled the 12 1/4 in interval",
      "description": "Operator-entered wording",
      "period_text": "06:00-18:00"
    }
  ]
}
```

```bash
drillintel ddr create --well A-3 --content shift.json --by operator --reason "Start daily report" --workspace /path/to/workspace --json
drillintel ddr show REPORT_ID --workspace /path/to/workspace --json
drillintel ddr submit REPORT_ID --expected-updated-at TIMESTAMP --by operator --reason "Ready for review" --workspace /path/to/workspace --json
drillintel ddr actions REPORT_ID --workspace /path/to/workspace --json
# Decide only an action shown above; scope/status/revision/timestamp are checked again by ReviewActionService.
drillintel ddr decide REPORT_ID --well A-3 --action confirm --expected-status CANDIDATE --expected-revision 1 --expected-updated-at TIMESTAMP --by reviewer --reason "Reviewed shift evidence" --workspace /path/to/workspace --json
```

The timestamp and revision are read from `ddr show`; save also requires a complete replacement snapshot. Review actor IDs are declared audit values, not authenticated identities. Confirmation of a DDR report does not silently confirm each candidate operation.

The same gates run in CI on both supported runtimes - the CPython 3.11 floor and the 3.14 product
target named in `docs/DECISIONS.md` ADR-0002 - see `.github/workflows/ci.yml`. Required jobs pin
`ubuntu-24.04` so the 2026-10-19 `ubuntu-latest` move does not change the Qt image before that
image has been run (ADR-45). The desktop UI tests skip themselves when Qt cannot load, so a
headless run reports skips rather than pretending to have executed them. A Qt stability pass is
evidence of reliability, not proof that a native abort cannot recur. The release
verifier reads each stability attempt record and the Workbench retry log; a
checksum that matches a false record still fails (ADR-48).

To check the *installed* product rather than this source tree:

```bash
.venv/bin/python tools/release_smoke.py
```

That builds the wheel and the sdist, installs each into a fresh virtualenv outside the checkout,
and drives the installed CLI end to end - workspace creation, schema bootstrap with no `migrations/`
directory present, a well written and read back, the read surfaces, and `doctor`.  An installed
wheel bootstraps its schema from the ORM metadata and stamps it at the current revision, because it
deliberately does not carry the Alembic scripts; `doctor` reports that mode as healthy rather than
as drift.

### Optional MinerU 4 PDF runtime

MinerU is an optional, separately managed parser for complex/scanned PDFs. It is **not** part of
Prog-Proc's core dependencies, and neither application startup nor `drillintel doctor` downloads
models or probes MinerU. Keep the parser in its own environment; use the path to its `mineru` entry
point and let the adapter resolve the adjacent `mineru-kit` parser:

```bash
python3 -m venv /opt/prog-proc-mineru
/opt/prog-proc-mineru/bin/python -m pip install "mineru==4.1.7"
export DRILLINTEL_MINERU_MODE=cli
export DRILLINTEL_MINERU_BINARY=/opt/prog-proc-mineru/bin/mineru
export DRILLINTEL_MINERU_TIER=flash
```

MinerU 4's `mineru-kit parse` writes the `docvortex.middle` contract, which Prog-Proc normalizes
into page-anchored text/table output with source locators. The regular HTTP adapter accepts only the
legacy multipart/JSON parse contract; MinerU 4's attachment/ZIP `/v1/file_parse` API is not treated as
compatible. The configured `model_source` defaults to `local`: parsing will not fetch missing models
implicitly, and may fail until the operator installs the selected models. Set an explicit remote
source only after reviewing the provider and data/network policy. The user-operable checks are:

```bash
drillintel mineru validate --json
# Explicit inference attempt against one deterministic synthetic PNG; still local-only:
drillintel mineru validate --run-synthetic-ocr --json
```

The default command checks the local CLI runtime only; it does not inspect model files, make an HTTP
request, download a model, upload a document, or run OCR. The optional synthetic command forces CLI,
local models, Basic tier and ONNX small backend; if inference does not complete it reports failure
without emitting OCR metrics. Successful CER/WER values apply only to the one fixture and are not a
general OCR qualification. The parser's text is extraction evidence, not engineering validation or an
automatic domain record. MinerU 4.1.7 is an optional external Python runtime; the PyPI metadata
checked for that release lists Python >=3.11,<3.15. Recheck exact runtime requirements before use;
model files and their terms are separate. Review the exact
upstream [MinerU license](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md), model terms,
and runtime requirements for the intended deployment. This note is not a legal determination.

Validate the effective configuration without connecting to services (JSON output is available):

```bash
drillintel config validate --json
```

The command applies the same defaults and environment overrides as startup, reports unknown keys,
and returns a nonzero exit code for invalid configuration. It never prints secret values.

Ingest a folder of documents into a workspace (this is the whole point of phase 0, so it
is worth running once on your own files).  From a terminal, nothing else is required:

```bash
drillintel workspace create /tmp/well-a3 --name "North Cormorant"
# A well is a durable identity, so it is registered explicitly rather than guessed from a
# filename: `ingest --well` resolves a well, it never invents one.
drillintel wells create --name A-3 --project "North Cormorant" --field "North Cormorant" \
    --workspace /tmp/well-a3
drillintel ingest /data/projects --well A-3 --workspace /tmp/well-a3
drillintel records promote --workspace /tmp/well-a3       # idempotent; re-runs report `unchanged`
drillintel index rebuild  --workspace /tmp/well-a3        # the sidecar is disposable
drillintel records summary --well A-3 --workspace /tmp/well-a3
drillintel doctor --workspace /tmp/well-a3                # what is healthy, and what to run next
```

### Workspace backup and restore

This command creates a **database snapshot only**, not a full workspace or source backup. It
snapshots the SQLite system-of-record database with SQLite's online backup API, records SHA-256
hashes and the migration revision, and runs SQLite integrity and foreign-key checks against the
snapshot. The destination must be new; existing files are never overwritten. Archives are **not
encrypted** and contain sensitive data: store them accordingly. The database limit is 8 GiB, the
archive overhead limit is 16 MiB, metadata is limited to 1 MiB, and an archive must contain exactly
three regular members.

```bash
mkdir -p /secure/backups
drillintel workspace backup --workspace /tmp/well-a3 \
    --output /secure/backups/well-a3-2026-11-03.dibak
```

Restore is deliberately non-destructive: the target directory must not exist, and when the
registry has a workspace identity row the target must be the exact original workspace path.
Re-homing is refused because changing that identity would break workspace scoping and provenance.
Only the SQLite database and `workspace.toml` are archived; source corpus files and the disposable
search index are not. This is **not a complete workspace recovery**: separately protect only the
source assets you are authorized to retain, using an independently approved process; this restore
will not recover them. Restore verifies archive members, hashes, SQLite page integrity, foreign
keys, Alembic revision, and workspace identity before it creates the target. A snapshot with failed
health checks is preserved for investigation but cannot be restored as a usable workspace. Custom
data-directory layouts are not qualified for this workflow.

```bash
# Run only after the original workspace path is absent; no existing directory is replaced.
drillintel workspace restore /secure/backups/well-a3-2026-11-03.dibak /tmp/well-a3
# Then restore the source corpus separately and inspect the workspace.
drillintel doctor --workspace /tmp/well-a3
```

For workspace-owned files as well as the registry snapshot, use the separate **full workspace-owned-files**
archive format. This includes eligible regular files under the workspace root (including configured
corpus files) and a transaction-consistent SQLite snapshot. It excludes the rebuildable search index,
runtime cache/logs, VCS/environment state, credentials, and transient files. Configured sources outside
the workspace are reported, never followed or copied; URL user-info, query strings, and fragments are
redacted in that report. Symlinks and special files in the included scope are rejected rather than
followed. Files are SHA-256 checked, archives are unencrypted, and files up to 8 GiB / total included
content up to 32 GiB are bounded. This scope is broader than `workspace backup`, but is still not a
copy of external sources or every machine-level dependency.

```bash
drillintel workspace archive --workspace /tmp/well-a3 \
    --output /secure/backups/well-a3-2026-11-04.diwa
# Restore only to the exact recorded workspace root, after that path is absent.
drillintel workspace restore-archive \
    /secure/backups/well-a3-2026-11-04.diwa /tmp/well-a3
```

Restore checks the complete manifest, all member hashes, SQLite integrity/foreign keys, schema and
workspace identity in a staging directory before atomically publishing the new workspace. Existing
paths and symlink destinations are refused; re-homing is not supported. Preserve a separate copy of
the original workspace before testing restore. Neither archive command constitutes a production
backup policy, encryption, external-source retention approval, or physical Windows acceptance.

`doctor` runs read-only SQLite integrity, foreign-key, and storage preflight checks on an existing
database and repeats the preflight after creating or upgrading a database. On failure it stops before
claiming domain health or running domain/search checks; it does not silently repair storage.
Domain-level invariants and disposable-index drift are checked only after storage passes.

### Desktop Review Workbench (optional)

The desktop consumer is an operator review surface with a governed action boundary. Install it only where a Qt
workbench is wanted:

```bash
.venv/bin/pip install -e ".[ui]"
# Open an existing workspace; --well accepts the registered well id or name.
drillintel-ui --workspace /tmp/well-a3 --well A-3
```

The workbench opens the existing `workspace.toml`, loads the registered well hierarchy through the
existing repositories, and requests `DomainReviewRequest` values from `DomainReviewService` — the
read-only authoritative review. The Current/History selector changes the service lifecycle request; it
does not hide rows in the client. `Verify citations` is an explicit opt-in call to the existing
citation auditor. Tables use Qt model/view adapters and presentation-only filters, while the review
result remains authoritative for scope, provenance, missing values, conflicts, calculation status and
plan/actual semantics. The distinct mutation boundary is `ReviewActionService` (`src/drilling_intelligence/review/actions.py`): the UI derives its permitted actions from `available_actions()`, submits an immutable `ReviewActionRequest` carrying the displayed `expected_status`/revision/scope/`updated_at` preconditions, and hands it to `ReviewActionWorker` to execute outside the GUI thread. On success the window reloads a fresh `DomainReview`; on failure the displayed state is unchanged and the domain error is shown. No UI state is persisted beyond the window. The default installation does
not import Qt. On a host where Qt's native runtime libraries cannot load, `drillintel-ui` reports the
missing runtime dependency and `pytest -m ui` skips with the preserved loader reason; the UI tests run
offscreen on hosts with a working Qt installation. CI retries a workbench process only when the
attempt log carries the recorded Qt/glibc abort signature and no assertion or collection failure;
both attempt logs are kept, collection must account for every declared test, and the attempt-log
artifact is required (ADR-40, ADR-41). The headless jobs publish explicit pytest totals
(collected, passed, failed, errors, skipped, xfailed, xpassed, shell exit, session exit,
runtime) even when the suite fails. A supplied exit file must exist and agree with the
session exit; an unexpected pass, or a skip that is not a headless UI test missing Qt,
fails the job (ADR-42, ADR-43). The workbench job also runs a no-retry stability
qualification of both nodes that have aborted in `destroyQCoreApplication`. A refused close
keeps the window. An external quit still leaves the event loop, and the production shutdown
budget is one 120s clock, not a fresh wait per poll (ADR-45). The real-thread proof owns a
`QThread`; a synthetic worker is not that proof. A queued callback cannot outrun the
shutdown deadline into a hang, and exit 4 remains a failed shutdown (ADR-46). A cleanup
that reaches the deadline is not a success, including when the monitor thread has not
yet been scheduled (ADR-47). The release
image stays `ubuntu-24.04`. Ubuntu 26.04 is a separate required compatibility lane, not a silent
move of `ubuntu-latest`; its Qt-enabled CPython 3.14 full suite, clean-install smoke, and identity-bound
pytest report passed on SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` (run `41940002906`); its full-suite lane collected 2,776, passed 2,775, skipped 1, and had zero failures/errors. This is CI evidence for that lane, not physical Windows acceptance or a general release certification.
Operator-facing errors redact prefixed and quoted credential assignments without discarding a
connection host.

`drillintel wells list` shows what is registered.  The same bootstrap through the Python API,
for callers embedding the library rather than driving the CLI:

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
# workspace_id is optional: omit it and the pipeline resolves the folder's own registry row.
# Supplying it is an assertion, not an override - it is checked against that resolution and
# refused if it disagrees, so a corpus can never be filed under a workspace it did not come from.
result = pipe.run(root=Path("/data/projects"), workspace_id=workspace_row.id, well_id=well.id)
print(result.counts)  # NEW / MODIFIED / UNCHANGED / DUPLICATE / REMOVED
for item in result.results:  # every file: what changed, what we read, how sure we are
    print(item.filename, item.change.value, item.classification, item.fields)
```

Run it twice: the second run does no work at all. Edit a file and run again: it becomes a
new version that supersedes the old one and keeps its well link. Delete a file: it is
reported as removed, and its document, provenance and audit history stay queryable.

### Executable calculation: NPT roll-up V1

The only executable calculation method in this phase is the explicit NPT roll-up. It reads promoted,
accepted NPT rows for exactly one registered well, sums only finite duration hours, records rows without
duration as excluded evidence, and stores the result in the existing `calculation` and
`calculation_input` tables. It does not overwrite a historical result. Repeating the same run is an
idempotent no-op; a changed promoted evidence set creates a new content identity, and an explicit
`--supersedes` is required when that new run is a revision of an existing result.

```bash
drillintel records rollup --workspace /tmp/well-a3 --well A-3 --by engineer@example.com
# JSON is explicit when automation needs the stored identity and validation details:
drillintel records rollup --workspace /tmp/well-a3 --well A-3 --json
# Impact is read-only and reports CURRENT, STALE and UNRESOLVED dependency states:
drillintel records impact --workspace /tmp/well-a3 \\
    "well:<well-id>|property:npt_hours|state:ACTUAL"
```

A successful roll-up stores method `npt.hours_rollup`, version `1`, unit `h`, scope, execution actor,
input evidence, source/document-version navigation metadata, validation counts, and the existing
provenance chain. Evidence or scope failures are rejected before a calculation is persisted. Domain
Review and the optional UI classify the stored row as `EXECUTABLE`, `STORED_ONLY`, `INCOMPLETE`,
`STALE`, or `UNRESOLVED`; these are read-side observations, never triggers for recalculation.

## Layout

```
src/drilling_intelligence/
  core/           units, provenance and locators, hashing, errors, ids, logging
  config/         TOML settings with DRILLINTEL_<SECTION>_<KEY> overrides
  database/       ORM models, engine/session, Alembic integration
  extraction/     pdf_text, excel, docx, text, normalized artefact, field rules, router
  classification/ taxonomy and the deterministic classifier
  documents/      registry (identity, versions, revisions), repository, versioning
  knowledge/      facts and predicates, entity references, the item/edge/conflict repository,
                  derivation from stored artefacts, conflict detection and resolution
  search/         chunking (documents and facts), the structured record projection, index, ranking, the query service
  retrieval/      the verified-evidence boundary above search: locate via search, re-read from the
                  authoritative database, return deterministic provenance-carrying evidence (ADR-0013)
  evidence/       addressable, re-askable packages composed only from retrieval: content identity,
                  per-topic coverage, staleness as a named diff (ADR-0014), and the read-only citation
                  auditor that re-reads each item's source file (ADR-0015)
  wells/          workspace and well/project/company repositories
  operations/     the operational spine: reports, operations, events, NPT, problems; the promoter
  engineering/    programmes and targets, procedures, plan-vs-actual, risks, costs
  lessons/        lessons learned, best practices, recommendations and their decisions
  review/         deterministic read-only per-well Domain Review contract and service
  intelligence/   timelines, field rollups, recurring patterns and their snapshots
  ingestion/      scanner, planner (incremental decisions), pipeline
  integrations/   optional MinerU client (subprocess/legacy JSON HTTP); heavy runtime stays external
migrations/       Alembic chain; alembic.ini carries no URL by design
tests/            unit, engineering (real files), integration (real database)
docs/             DECISIONS.md - the ADRs the code cites
```

## What it knows

Ingestion answers "what did the corpus say". The knowledge layer answers "what does it
assert", and it keeps the two apart on purpose: a fact is a value plus the exact place it
came from, filed against the entity it belongs to (a well, a hole section, a bit, a
document version), and the index is written from those facts so a search hit and a fact
listing never disagree about the citation.

```bash
drillintel knowledge status                      # what is known, and whether it is behind the registry
drillintel knowledge facts --well A-3            # each value, its unit, its status and its citation
drillintel knowledge facts --well A-3 --predicate mud_weight --include-superseded
drillintel knowledge conflicts                   # values that disagree, with both sides
drillintel knowledge resolve kc-1f4a… --choose ki-9c2… --note "check weight was a dirty-line reading"
drillintel knowledge rebuild                     # re-derive everything from the stored artefacts
```

```
$ drillintel knowledge facts --well A-3 --predicate mud_weight
1 fact(s) for well A-3
  mud_weight = 10.2 ppg [UNVERIFIED]
    source: mud_report_well-a3.xlsx > Sheet: Summary > Cell: B9
```

That `UNVERIFIED` is the point of the line rather than a flaw in it: the value is stored,
cited and searchable, and the platform still refuses to present it as settled, because the
cell it came from was read by a rule the extractor did not vouch for. Status is a claim about
the record, and a reader is entitled to see it.

Three rules hold this together, and each is enforced by a test rather than by intention:

- **Nothing is stored without a source.** A field the extractor could not cite is reported
  and left out of the knowledge table; it is never quietly stored as a number, and it is
  never quietly dropped.
- **A disagreement between sources is recorded, not resolved by a rule.** Two documents
  stating different mud weights both stay visible and both are marked; the platform notes
  which source ranks higher and why, and waits. Deciding is `knowledge resolve`, which
  keeps the losing value as `RETIRED` history with the note and the name of whoever chose.
- **The knowledge rows are derived, so they are safe to rebuild - and only those.** A
  rebuild re-reads the stored artefacts (never the files, never a model) and re-writes what
  a parser produced. A note a person typed survives, which is the difference between a
  repair command and a data-loss command.

## The operational record

The domain layer answers the question a document search cannot: not "where is this written" but "what
happened, how long did it cost, and has it happened before". Its rows are promoted from stored artefacts by
a deterministic pass, they keep the citation of the version they came from, and they arrive as `CANDIDATE`
— a promotion proposes, a person confirms.

```bash
drillintel records promote --field "North Cormorant"      # idempotent: re-running reports `unchanged`
drillintel records list --table npt --field "North Cormorant" --json
drillintel records review --well A-3 --lifecycle current --json  # authoritative, read-only review
drillintel records review --well A-3 --lifecycle history --verify-citations --json
drillintel timeline --well A-3 --since 2025-06-13 --until 2025-06-14
drillintel fields summary --field "North Cormorant"
drillintel fields decision --field "North Cormorant"      # execution, economics, risk, learning, limitations
drillintel fields decision --well A-3 --json              # the machine-readable decision substrate
drillintel fields compare --well A-3 --well B-11          # the matrix: metric | A-3 | B-11 | state
drillintel fields compare --anchor A-3 --json             # candidates discovered through recorded overlap
drillintel fields report --well A-3 --output a3.html      # one-well report, offline HTML
drillintel fields report --well A-3 --well B-11 --json    # comparison report; JSON is the service document
drillintel fields report --anchor A-3 --offsets B-11 --output offsets.html
drillintel fields report --anchor A-3 --output discovered.html
drillintel analyze --list-questions                       # the fixed question catalog, with its contracts
drillintel analyze --question npt_summary --well A-3      # one typed answer, JSON == the service document
drillintel patterns find --field "North Cormorant"        # recurrence in the rows, not a prediction
drillintel patterns stale 3f7c1a…                         # what has moved since a snapshot was reviewed
drillintel lessons counts --project "North Cormorant"
```

```
$ drillintel fields summary --field "North Cormorant"
field: North Cormorant (2 well(s))
non-productive time: 59.25 h over 5 record(s); 2 undated, 0 without a duration
by category: equipment_failure 12 h in 1 row(s) on 1 well(s), other 18.5 h in 2 row(s) on 1 well(s), stuck_pipe 28.75 h in 2 row(s) on 2 well(s)
problems: 3 occurrence(s) of equipment_failure x1 (1 well(s)), stuck_pipe x2 (2 well(s))
events: 3, lessons: 0, reports: 2
hours are summed per record: if two files describe one event, both are counted, and the record that says so is `drillintel records list --table npt`
```

`drillintel fields decision` is the same philosophy composed across every decision surface: it prints
execution against plan, money per currency (never summed across currencies), risk as the source stated it,
lessons/practices/recommendations as the distinct things they are, observed patterns with their staleness,
stored calculations with their dependency state - and then the limitations and observations that fall out
of those facts. It never ranks, scores, predicts or converts.

`drillintel fields compare` takes that philosophy across wells: one row per metric, one column per well,
and a comparability verdict per row. Numbers in different units are shown side by side and labelled
`INCOMPARABLE` rather than converted; a value the source never stated stays missing rather than becoming
zero; severity bands are comparable only while the wells' recorded risk scales are the same identity.
Candidate wells come from the existing `offset_candidates` discovery - recorded problem types and hole
sizes, never a "best offset" - and every cell traces back through structured row ids or an exact
method+scope reference. `drillintel analyze --question ...` is the machine-facing door to the same
surface: a closed catalog of sixteen question ids with declared params, scope, evidence, lifecycle and
missing-value semantics, answered as typed JSON. It is AI-*ready* shape, not AI: no prompts, no models,
no confidence scores, and an unknown question fails with the list of real ids instead of a guess.

`drillintel fields report` composes those packs into one offline HTML file. A single `--well` uses the
decision pack; two or more wells, or `--anchor` / `--offsets`, use the comparison pack and its selection
rules. Charts are inline SVG of cells the pack already states. Differing units are not plotted on one
axis, a missing value is not drawn as zero, and the matrix in the file remains the authoritative
comparison. `--json` prints `ReportPack.to_dict()`. `--output` writes HTML and does not change the
report identity. There is no PDF, no remote asset, and no depth log: point-level curves are not in the
certified packs.

Read that last line as the layer's whole philosophy: the hours are what the records claim, each with the
basis it was stated on, and the tool tells you which row to open rather than smoothing the number for you.
`docs/DOMAIN.md` maps the tables, the promotion rules, the CLI surface and — most of it — the things this
layer refuses to compute: no invented risk scores, no inferred root causes, no currency conversion, no
stored timeline, no model in the numeric path.

## Search spans documents, facts and structured records

`drillintel search` is the one boundary for everything the platform has indexed, and a single query can
return three source types in one ranked list:

- **document** — a chunk of extracted text, cited to a page/sheet/cell and (optionally) re-verified against
  the file it came from;
- **fact** — a knowledge assertion rendered as a `knowledge_fact` chunk, weighted above the prose it was
  derived from;
- **structured** — one authoritative operational/domain record (`problem_definition`, `problem_occurrence`,
  `npt_record`, `well_event`, `lesson_learned`, `recommendation`), cited to its own row id
  (`structured:<type>:<id>`) rather than to a page.

Each result says which of the three it is (`source_type`), and the structured half carries its own
filters — `record_types`, `field`, and a problem/category token — alongside the well/project/date filters
the document half already understood. The database stays authoritative and the sidecar stays disposable:
structured rows are projected into the same index, rebuilt and pruned by the same `rebuild`/`prune` passes,
and a record that is rejected or superseded leaves the searchable state exactly as a superseded document
version does. Nothing in the projection is ever written back to the domain tables.

## Conventions this project keeps

- **Every extracted value cites a location.** A field without provenance is not stored.
  `core/provenance.verify_provenance()` re-reads the original file and says whether the
  record still matches.
- **Confidence is earned and doubt is visible.** A filename can hint, never prove; a page
  with no text layer reports that instead of guessing; a best score that is noise returns
  `OTHER`.
- **Deterministic where it matters.** Engineering numbers come from rules and
  calculations, not from a model. The Ollama adapter can return text or vectors.
  No current path treats that output as a result, or as an input to extraction,
  promotion, search, or citation audit.
- **The database is the record, the index is disposable.** Nothing in the search index is
  authoritative, so it is rebuilt rather than migrated - and nothing mutable (a fact's
  lifecycle status, for instance) is baked into indexed text, because the sidecar is not
  rewritten when the registry changes its mind.
- **An answer names its source, and a dispute names both sides.** `citation()` is rendered by
  the same locator code as every other reference; a fact, a search hit and a conflict
  candidate all cite the same way, so a reader can open the cell.
- **A record is not a document and not a fact.** Promotion writes rows that cite a version; it never
  edits a file's record, never confirms itself, and never fills a gap the source left open. A stated
  duration and a computed one are kept apart by `duration_basis` so they cannot be averaged together.
- **A workspace is the folder and its database, and nothing else may say otherwise.** One
  SQLite file under `.drillintel/database/` is the system of record for one workspace (ADR-0003).
  SQLite is the only currently supported and qualified database backend; PostgreSQL is NOT QUALIFIED
  and must not be used as a deployment target (ADR-0004). Documents are identified by
  `(workspace_id, identity_path)`, the identity is attached where
  documents are written rather than where commands are parsed, and a workspace moved to a new path
  is the same workspace because the database travels with it. An explicit `workspace_id` is
  validated against that binding, never trusted over it.
- **No fake implementations.** If a subsystem is not built, it is absent from the tree —
  there are no stubs that return plausible-looking data.

## Licensing

Proprietary: an internal engineering platform for the drilling team. No public
distribution, no sublicense, and no warranty of fitness is offered; see the `license`
field in `pyproject.toml`. Third-party components keep their own terms — notably MinerU,
which is not vendored or required by this repository. Upstream MinerU 4.1.7 supplies an
Apache-2.0 license text with additional conditions; review the exact upstream terms (including
service attribution/commercial-use conditions) for the intended deployment before use. This
repository's note is not legal advice. `wellpathpy` (LGPL) is planned for a later phase behind a
wrapper, per `docs/DECISIONS.md`.

## Production ingestion & domain promotion

The boundary from evidence to domain rows is inspectable. The authoritative matrix is
[`docs/DOCUMENT_DOMAIN_COVERAGE.md`](docs/DOCUMENT_DOMAIN_COVERAGE.md), and the machine-readable
registry is `drilling_intelligence.operations.contracts`. The registry covers every
`DocumentClassification`, and these are the handlers that can currently write source-derived domain
rows:

```text
DRILLING_PROGRAM   -> program
DDR / NPT / TIME_BREAKDOWN -> report
MUD_REPORT         -> mud_report
BHA_REPORT         -> bha_report
BIT_RECORD         -> bit_record
DIRECTIONAL_SURVEY -> directional_survey
COST               -> cost
CASING_REPORT      -> casing
CEMENT_REPORT      -> cement
WELL_CONTROL       -> well_control   (V7.2)
HSE                -> hse            (V7.2)
LESSON_LEARNED     -> lesson_learned (V9.0)
```

That is 12 named handlers across 14 `END_TO_END_CERTIFIED` classifications out of 26; 7 classes are
`KNOWLEDGE_SUPPORTED` and 5 are `EXTRACT_ONLY`. These counts are the current taxonomy snapshot, not
an expansion from MinerU, Intake, or the separate manual DDR-authoring workflow. See the preserved
historical matrix in [`docs/DOCUMENT_DOMAIN_COVERAGE.md`](docs/DOCUMENT_DOMAIN_COVERAGE.md).

No other classification is promoted merely because its file has a table, its filename contains a
keyword, a search hit exists, or an enum/knowledge entity exists. Evidence-only classes remain
extractable and, where justified, knowledge-supported. Run `records promote --include-unsupported`
when auditing a complete current corpus; the JSON/text report distinguishes `PROMOTED`, `UNCHANGED`,
`UNSUPPORTED`, `AMBIGUOUS`, `MISSING_ARTEFACT`, `MISSING_WELL`, `MISSING_PROVENANCE`,
`INVALID_FIELDS`, `CONFLICT`, and `ERROR`.

Promotion consumes stored extraction artefacts rather than re-reading arbitrary files. It requires
recognised headers, usable fields/units, explicit well linkage, and source provenance. It preserves
source versions, current/history semantics, human confirmation, planned-versus-actual state and
source-owned replacement boundaries. Search remains discovery-only. Review, indexing, ingestion and
stale detection never execute a calculation; the explicit NPT roll-up is still the only executable
calculation and is available in the optional workbench only behind an actor and confirmation action.
The deterministic miniature corpus and its manifest are in `tests/golden_corpus`.
