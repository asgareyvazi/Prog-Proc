# Forensic replay procedure — shard run 43695755434 (attempt 1)

**Purpose:** offline re-evaluation of the original Linux shard artifacts using the corrected local `tools.pytest_shard_report` code. This is not a rerun of GitHub Actions and must not be reported as the original workflow passing.

## Immutable run identity

- Repository: `asgareyvazi/Prog-Proc`
- Branch: `arena/5957b76d-prog-proc`
- SHA: `114173a489e8a0d5b56cdfe6f63efd14701e9d65`
- Workflow: `CI`; workflow file: `.github/workflows/ci.yml`
- Run/attempt: `43695755434` / `1`
- Expected workflow ref: `asgareyvazi/Prog-Proc/.github/workflows/ci.yml@refs/heads/arena/5957b76d-prog-proc`

Artifact metadata from the GitHub API:

| Artifact | ID | Bytes | API SHA-256 |
|---|---:|---:|---|
| `ubuntu2604-qt-collection-a1` | `14153934832` | `55,789` | `57cc861a16b177bddcc52b81e8e78350fd02f7a496751cf9de4f39cf06d2deb9` |
| `ubuntu2604-qt-shard-00-of-04-a1` | `14154442461` | `139,067` | `f1506bd476e606261892b1bb4d2bf9c97f1f99e9497f5feb6b505d9f802cb36f` |
| `ubuntu2604-qt-shard-01-of-04-a1` | `14155155156` | `144,870` | `7265b15a3a61557df1bdee2653c8a902828186e6d13a42f44b22860308d2dd27` |
| `ubuntu2604-qt-shard-02-of-04-a1` | `14154935699` | `143,338` | `4204e0461e2187162e8b19afa7e2f8ef961e3c09d34765fd56f2ff7d1aaec5a3` |
| `ubuntu2604-qt-shard-03-of-04-a1` | `14154004233` | `142,003` | `062f3f34389f16c1f3341b88f36ce8df2fb97d4492e2e48f4b901a0c434d254a` |
| `ubuntu2604-qt-shard-report-a1` (historical failed report; retain separately) | `14154880825` | `81,488` | `7852477463a5425edc4693a1c82da109fa5253823d5f483c559bfd09ef67e5d9` |

The expected original aggregate was 2,893 node IDs assigned exactly once; 2,892 passed, one policy skip, zero failures/errors/xfails/xpasses. The single skip is `tests.integration.test_phase1_finalization::test_postgresql_claim_is_qualified_or_withdrawn`, with canonical reason `PostgreSQL is NOT QUALIFIED: SQLite is the only currently qualified backend; no disposable PostgreSQL service job exists.` The original process sidecar included the pytest display prefix `Skipped: `; this replay must determine whether the corrected plugin/report accepts the original payload. Keep the original failed report artifact and this result distinct.

## Reproducible command sequence

Run from the reviewed commit containing the normalization correction. `GH_TOKEN` must be the already-authorized read-only GitHub Actions token; do not print or store it in a report. The repository's artifact tool verifies each raw ZIP against API byte size and digest before aggregation.

```bash
set -euo pipefail
REPLAY=/tmp/prog-proc-replay-43695755434-a1
mkdir -p "$(dirname "$REPLAY")"
GH_TOKEN="${GH_TOKEN:-$(gh auth token)}" python -m tools.pytest_shard_artifacts \
  --repository asgareyvazi/Prog-Proc \
  --run-id 43695755434 \
  --attempt 1 \
  --sha 114173a489e8a0d5b56cdfe6f63efd14701e9d65 \
  --workflow CI \
  --shard-count 4 \
  --output-dir "$REPLAY"

python -m tools.pytest_shard_report \
  --shard-root "$REPLAY" \
  --archive-root "$REPLAY/archives" \
  --extracted-root "$REPLAY/extracted" \
  --run "$REPLAY/run.json" \
  --jobs "$REPLAY/jobs.json" \
  --artifacts "$REPLAY/artifacts.json" \
  --sha 114173a489e8a0d5b56cdfe6f63efd14701e9d65 \
  --repository asgareyvazi/Prog-Proc \
  --workflow CI \
  --branch arena/5957b76d-prog-proc \
  --workflow-ref asgareyvazi/Prog-Proc/.github/workflows/ci.yml@refs/heads/arena/5957b76d-prog-proc \
  --run-id 43695755434 \
  --attempt 1 \
  --shard-count 4 \
  --allow-in-progress-run \
  --output-json "$REPLAY/replay-report.json" \
  --output-markdown "$REPLAY/replay-report.md" \
  --combined-junit "$REPLAY/replay-combined.xml" \
  --combined-stats "$REPLAY/replay-stats.json" \
  --combined-output "$REPLAY/replay-output.txt" \
  --combined-exit "$REPLAY/replay-exit.txt" \
  --combined-state "$REPLAY/replay-state.txt"
```

Before calling the replay PASS, independently confirm `downloads.json` has an empty failures array; check collection, all four shard node-ID assignments and execution reports, original artifact IDs/digests/sizes, exact policy identity and reason in both process and JUnit, and combined totals `passed=2892`, `skipped=1`, `failed=0`, `errors=0`. `run_completion_verified=false` and `workflow_conclusion_verified=false` are expected on an offline replay report; do not infer the original run's conclusion from replay success.

## Status in this continuation

**NOT RUN / BLOCKED:** the original continuation process identifier and its previous `/tmp` clone/report bundle were absent when resumed. The original owner checkout was not touched. Attempts to download the report artifact with `gh run download` failed with `EOF` after GitHub redirected to `productionresultssa*.blob.core.windows.net`, a host outside this sandbox's allowed outbound hosts. The API metadata above is available, but the raw artifact ZIP bytes were not present locally; no historical replay result is claimed. The procedure is retained for an authorized runner where the artifact redirect can complete.
