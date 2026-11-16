# Forensic replay procedure — historical Linux shard run 43695755434 (attempt 1)

## Purpose and interpretation

Replay the original failed run's collection manifest, four shard artifacts, and failed reconciliation-report artifact using the current read-only GitHub evidence tools. The replay must verify bytes against GitHub API size/SHA-256, run/job/artifact identity, exact-once assignment/execution, JUnit/process skip reasons, and original totals. It must preserve the historical result as **FAIL**: successful forensic reproduction does not convert the old workflow into a pass. No source/audit/DB data is changed.

The sandbox cannot fetch GitHub Actions storage redirects: direct downloads ended with `EOF` at `productionresultssa*.blob.core.windows.net`, outside the sandbox egress allowlist. To finish the replay from a network-capable authorized runner without loosening egress policy, the current CI workflow contains a manually selected, read-only replay job, guarded to this branch and fixed run identity. On the authorized branch, trigger it with:

```bash
gh workflow run ci.yml \
  --ref arena/5957b76d-prog-proc \
  -f replay_historical_shards=true
```

GitHub runs the normal Linux CI jobs as well as the opt-in replay job for this workflow-dispatch event. The `GH_TOKEN` is the scoped workflow token with `actions: read`; it is not printed or stored. The replay downloads six bounded artifacts (collection, four shards, and original reconciliation report), rechecks API digest/size, aggregates current evidence, validates the original failed report, and uploads only `downloads.json`, replay JSON/Markdown, and combined stats. Raw archives and extracted test payloads stay in the ephemeral runner and are not re-uploaded. The replay additionally pins every expected artifact ID, byte count, and SHA-256 to the recorded historical inventory before fetching any bytes. Artifact API redirects are followed manually for at most two hops, only to HTTPS `productionresultssa*.blob.core.windows.net` hosts, without forwarding the GitHub token; each ZIP is capped at 512 MiB and extraction rejects more than 1,024 members.

The replay workflow job returns success only when the artifacts were fetched and verified, the offline report preserves the historical `FAIL`, and the known skip wrapper mismatch is reproduced. This job result means **forensic replay complete**, not that the historical CI passed or the old skip was a passing test.

## Immutable run identity

- Repository: `asgareyvazi/Prog-Proc`
- Branch: `arena/5957b76d-prog-proc`
- SHA: `114173a489e8a0d5b56cdfe6f63efd14701e9d65`
- Workflow: `CI`; workflow file: `.github/workflows/ci.yml`
- Run/attempt: `43695755434` / `1`
- Expected workflow ref: `asgareyvazi/Prog-Proc/.github/workflows/ci.yml@refs/heads/arena/5957b76d-prog-proc`

Artifact metadata from GitHub API:

| Artifact | ID | Bytes | API SHA-256 |
|---|---:|---:|---|
| `ubuntu2604-qt-collection-a1` | `14153934832` | `55,789` | `57cc861a16b177bddcc52b81e8e78350fd02f7a496751cf9de4f39cf06d2deb9` |
| `ubuntu2604-qt-shard-00-of-04-a1` | `14154442461` | `139,067` | `f1506bd476e606261892b1bb4d2bf9c97f1f99e9497f5feb6b505d9f802cb36f` |
| `ubuntu2604-qt-shard-01-of-04-a1` | `14155155156` | `144,870` | `7265b15a3a61557df1bdee2653c8a902828186e6d13a42f44b22860308d2dd27` |
| `ubuntu2604-qt-shard-02-of-04-a1` | `14154935699` | `143,338` | `4204e0461e2187162e8b19afa7e2f8ef961e3c09d34765fd56f2ff7d1aaec5a3` |
| `ubuntu2604-qt-shard-03-of-04-a1` | `14154004233` | `142,003` | `062f3f34389f16c1f3341b88f36ce8df2fb97d4492e2e48f4b901a0c434d254a` |
| `ubuntu2604-qt-shard-report-a1` | `14154880825` | `81,488` | `7852477463a5425edc4693a1c82da109fa5253823d5f483c559bfd09ef67e5d9` |

The historical report said 2,893 node IDs, 2,892 passed, one skip, and zero failures/errors/xfails/xpasses. Expected skip identity: `tests.integration.test_phase1_finalization::test_postgresql_claim_is_qualified_or_withdrawn`. Canonical JUnit reason: `PostgreSQL is NOT QUALIFIED: SQLite is the only currently qualified backend; no disposable PostgreSQL service job exists.` The original process sidecar retained exactly one pytest display wrapper, `Skipped: `, before that reason. Replay verifies this mismatch and exact-once coverage; it must not normalize the authored JUnit reason or infer qualification for PostgreSQL.

## Expected machine-checked replay assertions

The dedicated `tools.pytest_shard_replay` command verifies:

- All six artifact names occur exactly once, are unexpired, and downloaded bytes match each API size and SHA-256.
- The run is exactly repository `asgareyvazi/Prog-Proc`, workflow `CI`, branch above, run/attempt `43695755434/1`, SHA above, completed with original conclusion `failure`.
- Collection is 2,893 IDs, all assigned and executed exactly once; aggregate totals are 2,892 passed, 1 skipped, 0 failed/errors/xfails/xpasses.
- JUnit contains exactly the documented PostgreSQL policy skip and canonical reason; the sidecar contains the same node ID/call phase but the single display prefix mismatch.
- The archived original report is itself checksum-verified, safely extracted, and bound to the same run/SHA/attempt; both archived and regenerated reports retain `FAIL` and the known issue. The workflow run's later conclusion remains separately verified from the original report.

## Execution record requirements

The mission closeout must record the workflow-dispatch run ID and attempt, the exact head SHA and branch, the replay job conclusion, and the metadata-only output artifact ID. The replay is complete only when that exact workflow-dispatch replay job succeeds and all six artifact payloads have been fetched and verified; local tests or API metadata alone are insufficient. Keep the closeout bound to the SHA actually dispatched; do not copy evidence to another SHA.
