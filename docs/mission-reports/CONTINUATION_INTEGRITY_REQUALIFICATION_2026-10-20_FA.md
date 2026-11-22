# گزارش مأموریت — ادامهٔ مأموریت: Requalification یکپارچگی (۲۰۲۶-۱۰-۲۰)

## A. verdict — PARTIALLY DONE

این بخش از مأموریت تعمیرهای اصلی یکپارچگی (Phaseهای B/C/D/E/F/G2) را پیاده‌سازی و push کرد،
اسناد current-facing را با SHA جاری به‌روزرسانی کرد، و CI exact-SHA روی `9a9e89f` سبز ثبت شد.
بخش‌های بزرگِ گسترش کامل (Phaseهای H تا Q شامل authoring workspace کامل، 추천 synthetic E2E،
و acceptance فیزیکی Windows) هنوز NOT RUN / NOT QUALIFIED هستند؛ verdict کلی PARTIALLY DONE است.

## B. Git و workspace

- شاخهٔ مجاز: `arena/5957b76d-prog-proc` — کار فقط روی همین شاخه.
- SHA شروع: `9a9e89f639e2de661f595b81a1056194ad3a5324` (remote tip در preflight).
- SHA پایانی این بخش: `4896285252911532b0052091120795109df1b55b` به‌علاوهٔ commit گزارش.
- `main` قبل و بعد: `85122e1a98087db4495b820a9a0eafe467…9009b7819` — دست‌نخورده.
- `out/performance.json`: blob `df63b57f…` دست‌نخورده (verification با `git ls-files -s`).
- workspace اصلی `/home/user/Prog-Proc` (HEAD `317c553`، ۲۴۷ فایل owner-owned modified) دست‌نخورده؛ clone ایزوله `/tmp/Prog-Proc-mission2` محل کار.

## C. CI

- Linux run `44922560690` و Windows run `44922560673` روی SHA `9a9e89f639e2de661f595b81a1056194ad3a5324`: هر دو **success** (همهٔ shardها + reconciliation).
- Runهای جدید روی `9c86ef6`، `4896285` و SHA نهایی این گزارش پس از push آغاز می‌شوند.

## D. نقص‌های تعمیرشده در این بخش

| نقص | علت ریشه‌ای | Fix | Regression test |
| --- | --- | --- | --- |
| Procedure accept/edit仍然 PENDING_REVIEW نشان می‌داد | Program «accepted» و Procedure «accept» می‌نوشت؛ reader فقط یکی از آن‌ها را می‌شناخت | schema event واحد `extraction_disposition` با decisionهای canonical (ACCEPTED/EDITED/REJECTED/SUPERSEDED)، policy transition (SUPERSEDED terminal؛ rejected → `EXTRACTED_UNVERIFIED`) | `test_procedure_disposition_uses_canonical_events`, `test_repeat_decision_policy` |
| Version resolution تکراری و خطر StopIteration | scan روی `versions_for`+`is_current` به‌جای pointer | `_resolve_version` واحد مبتنی بر pointer `current_version` | `test_resolver_refuses_document_without_any_version`, `test_resolver_pointer_wins_over_is_current_disagreement`, `test_resolver_rejects_version_of_other_document` |
| پنل source در UI bản sao از متن candidate بود | source_view از متن ذخیره‌شده ساخته می‌شد | `read_source_excerpt` با re-read + SHA verify + line range؛ حالت BLOCKED | `test_source_pane_blocked_when_bytes_tampered` + UI E2E |
| مسیر fallback انتشار test نشده بود | race test فقط hard-link را می‌آزمود | `_rename_noreplace` (renameat2 NOREPLACE / Windows rename / fail-closed) + ۵ تست forced | `test_publish_fallback_*`, `test_publish_fails_closed_*` |
| برخورد نام نام artifact بین runها | name-only download | `run-id: github.run_id` (segment قبلی) + تست plugin | `test_shard_plugin_rejects_manifest_from_another_run` |
| Template mutation silent | — | hash stability + detectable mutation | `test_template_definitions_are_immutable_and_hash_stable` |

## E. Test totals این segment (local)

- `tests/integration/test_source_extraction_integrity.py`: 15 passed
- `tests/integration/test_authoring_export_templates.py`: 23 passed
- `tests/ui/test_authoring_pages.py`: 13 passed
- `tests/unit/test_ci_reporting.py`: 22 passed
- ruff check/format: clean (350 فایل)؛ exception review: 133 suppression مستند.

## F. Remaining boundaries (NOT RUN / NOT QUALIFIED / OWNER-GATED)

- Physical Windows display acceptance — NOT RUN.
- OCR/MinerU — NOT QUALIFIED.
- PostgreSQL، auth، multi-user، signatures — NOT QUALIFIED.
- محاسبات directional/hydraulics/casing/cement/BHA/torque-drag/risk/AFE — OWNER-GATED (فقط `npt.hours_rollup` V1 قابل اجرا).
- گسترش کامل Phaseهای H–Q (authoring workspace کامل، plan-vs-actual، corpus End-to-End E2E) — در این segment اولویت‌بندی نشد؛ در roadmap Phase T.
