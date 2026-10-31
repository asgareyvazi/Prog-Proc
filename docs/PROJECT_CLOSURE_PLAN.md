# Project Closure Plan — PROG-PROC V9.0

As of 2026-10-31. Branch `arena/5957b76d-prog-proc`. Head `332f8fc` + V9.0 delta (lesson writer).  
Closes PR `arena/01a0c936-prog-proc` track; next merge target `main` is `85122e1` (owner merges).

## 1  What V9.0 closes

| Milestone | Scope | State |
|---|---|---|
| M1 read-only boundary fix | README contradiction, DOCUMENT_DOMAIN_COVERAGE alignment | **CLOSED** (README governed-workbench fix, WINDOWS_TESTING boundary) |
| M2–M5 product decisions | openai-compatible, embedding sidecar, Skills runtime, NPT calc catalogue | **BLOCKED — NOT PUBLISHED** (fail-closed stubs, ADRs pending owner) |
| M6 source-grounded writer | `LESSON_LEARNED` lesson tally → `lesson_learned` handler `v8`, 12 forensic tests, migration `0020` | **DELIVERED — END_TO_END_CERTIFIED** |
| Workbench coherence | `ReviewActionService.available_actions()` façade, Qt offscreen suite green | **CLOSED** (25-run repetition evidence retained) |
| E2E disposable workflow | `tests/integration/test_e2e_workflow.py` covers §3.1–3.8 on disposable SQLite | **CLOSED** |
| Windows acceptance | PowerShell run-card + JSON template + sanitized logs | **CLOSED (automated) / NOT RUN (manual checklist)** |

Counts: 26 document classes / 14 END_TO_END_CERTIFIED / 7 KNOWLEDGE_SUPPORTED / 5 EXTRACT_ONLY / 12 handlers / head `0020`.

## 2  Engineering state (what is true in code)

* **Implemented & tested:** 14 certified domains (incl. LESSON_LEARNED v8), BHA/Bit/Survey/Cost/Casing/Cement/WellControl/HSE etc., with unit/repo/integration/golden-corpus/negative/Qt/subprocess/clean-install/query-count coverage.
* **Implemented but limited:** knowledge extraction (semantic predicate hardening, non-authoritative Ollama optional), BM25 structured search (SQLite + InMemory), evidence package freshness — limited by design, proven by forensic suite.
* **Architecture-only:** Skills/VectorStore/embedding sidecar interfaces exist, fail-closed, no provider.
* **Planned, blocked:** openai-compatible adapter, embedding sidecar, Skills runtime, NPT-only calc catalogue (ADR required).
* **Excluded, by ADR:** risk scores, currency conversion, AFE rules, plan-vs-actual, inferred survey geometry, auth/multi-user (local-first, single-user).
* **Provenance & governance:** page/table/sheet/cell, file hash, `identity_key`, `document_id`/`document_version_id`, `DRAFT→REVIEW→APPROVED` with independent-actor, stale-revision, evidence gate via `ReviewActionService`.
* **Data integrity:** Alembic 0020 (`batch_alter_table` for SQLite), upgrade/downgrade path tested (see §4).

## 3  Product state (what the owner can sell / report)

* Product is **local-first, single-user, fail-closed**. No cloud, no inference beyond source, no invented rows.
* Certified report is produced from promoted rows only; knowledge-supported types appear as cited evidence, not as operational facts.
* Workspace is portable (path with spaces/Ø), canonical `workspace.toml`, relative `identity_path` resolves after move.
* Decision/comparison/report are composition over certified reads (DecisionIntelligence/ComparisonIntelligence), not LLMs.
* Windows build is operator-accepted via two figures: offscreen CI (gate) and real-display manual tick (owner). Manual checklist is `NOT RUN` until a physical workstation is exercised.

## 4  Workstation / acceptance state (what was run where)

| Figure | Where | How | Result |
|---|---|---|---|
| CI Linux Ubuntu | GitHub Actions `ubuntu-latest` | `.venv`, `pytest -q`, offscreen Qt not required | PASS (gate) — `python -m ruff check`, `ruff format --check`, `mypy`, `compileall`, `pytest -q`, `release_smoke` on 3.11/3.14 |
| CI Windows offscreen | `windows-latest` | `QT_QPA_PLATFORM=offscreen`, Qt suite, stability fixture | PASS (offscreen gate) — 25-run repetition, native abort evidence retained |
| Pilot (local Linux) | developer | `docs/WINDOWS_ACCEPTANCE_RUN_CARD.ps1 -Offscreen` or `pytest` | PASS (this branch) |
| Manual Windows display | owner workstation | `docs/WINDOWS_ACCEPTANCE_RUN_CARD.ps1` on real display, tick 16 items in `docs/WINDOWS_TESTING.md` | **NOT RUN** — intentional; do not infer from offscreen |

Logs are sanitized (`<workdir>`), stored in `out/windows-acceptance/<stamp>/` and uploaded as artifact.

## 5  Classification & evidence matrix

See `docs/DOCUMENT_DOMAIN_COVERAGE.md` V9.0 matrix (26 rows). Highlights:

* 14 certified rows have a `v*` handler, a `document:` contract, a DB model with `identity_key`/`document_version_id`, and ≥1 forensic case in `tests/golden_corpus/manifest.json` + provider tests.
* 7 knowledge-supported rows are `KNOWLEDGE_SUPPORTED` with derivation citations, no writer.
* 5 extract-only rows are `EXTRACT_ONLY` (parsers only, no promotion).
* Lesson row (LESSON_LEARNED) is the V9.0 delta: `v8`, contract `document:LESSON_LEARNED:promotion:v8`, model `lesson_learned`, migration `0020`, 12 tests in `tests/integration/test_lesson_promotion.py` + E2E tail.

## 6  Open decisions & next owner actions

1. Tick manual Windows checklist on a real display, fill `docs/WINDOWS_ACCEPTANCE_RESULT.json` per template, attach logs artifact.
2. Decide M2–M5 (four product decisions in `docs/PRODUCT_DECISIONS_REQUIRED.md`) — each needs an ADR and a new domain-free test before code.
3. Merge `arena/5957b76d-prog-proc` to `main` (no force, HEAD==origin==ls-remote, CI on exact SHA).

## 7  How to verify closure

```bash
git rev-parse HEAD && git ls-remote origin arena/5957b76d-prog-proc  # must match
gh run list --branch arena/5957b76d-prog-proc --limit 3             # exact SHA must be green, gate PASS
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check . && .venv/bin/python -m mypy src && python -m compileall src
.venv/bin/python -m pytest -q
.venv/bin/python tools/release_smoke.py
```

All must be PASS on both Python 3.11 and 3.14. Advisory-network failures are not a clean audit.

## 8  Risks & non-goals

* No installer/signing/branding contract is invented; Linux wheel/sdist is editable/headless only.
* No `is_fail_closed_shutdown_exit` weakening (`4` only); no `os._exit` fallback on Windows; no retries hiding failures.
* No new document-class writer, AI provider, embedding, Skills, or auth without owner ADR — boundary stays fail-closed.
