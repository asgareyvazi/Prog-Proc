# گزارش ادامهٔ مأموریت مهندسی محصول — ۲۰۲۶-۱۱-۲۳ (FA)

**شاخه:** `arena/5957b76d-prog-proc` — **tip این مأموریت:** در §۳۰ (SHA نهایی پس از commit این سند)
**پایگاه‌دادهٔ qualified:** فقط SQLite — **head Alembic:** `0029`
**حفاظت:** فایل `out/performance.json` هرگز خوانده یا stage نشده است؛ فقط با `git ls-tree` تأیید شد. `main` (`85122e1a…`) دست‌نخورده؛ workspace اصلی `/home/user/Prog-Proc` دست‌نخورده.

واژگان verdict: DONE / PARTIALLY DONE / BLOCKED / NOT RUN / NOT QUALIFIED / OWNER-GATED / PLANNED / DELIBERATELY EXCLUDED — هیچClaimی بدون evidence.

۱. **scope** — مأموریت ۲۰۲۶-۱۱-۲۳: reopen défauts D-12..D-23 و اجرای فازهای A تا AB روی شاخهٔ مجاز.
۲. **Phase A preflight** — DONE: clone `/tmp/Prog-Proc-mission2`؛ HEAD=remote=`3b5afba2aad…`; main=`85122e1a…`; blob protected `df63b57f…` intact؛ tree clean؛ بدون stash.
۳. **Phase C — hardening batch batch import** — DONE: D-15 (path containment: resolve+parents،refusal مسیرهای `..`/symlink/absolute، ایزوله‌سازی per-entry)، D-16 (duplicateهای workspace-scoped با `version_by_sha_in_workspace`؛ ۷ تست)، D-17 (صف review فقط برای exact current version با اسکن provenance JSON)، D-18 (limitهای manifest: ۵۰۰ entry، ۶۴ MiB/file، ۵۱۲ MiB batch، PDF signature + hookهای cancel/progress). `tests/integration/test_ddr_batch_import.py`: ۶ passed.
۴. **Phase D — صفحهٔ Qt واقعی batch import (D-14)** — DONE: `ui/batch_import_page.py` (انتخاب فایل، staging در `.drillintel/batch_imports/<id>`، QThread worker، progress، cancel، retry failed، نمای صف review، persistenceِ outcome.json) + nav «Batch DDR Import»؛ تست UI واقعی `test_batch_import_page_runs_a_real_batch_through_qt` green.
۵. **D-21 — survey Qt workbench** — DONE: `operations/survey_import.py` (اعتبارسنجی CSV با واحدهای اجباری، MD صعودی، بدون duplicate، idempotent، record فقط CANDIDATE با provenance) + `ui/survey_page.py` + nav «Survey Import»؛ ۴ تست integration + ۱ تست UI.
۶. **D-19 — تقویت matrice V1–V15** — DONE: scope label per-scenario روی هر ۱۵ سناریو + UI E2E navigation test؛ ۱۵ passed + ۱۸ UI passed.
۷. **D-20 — چرخهٔ DDR PDF→record** — DONE: `test_ddr_lifecycle.py` — نسخهٔ import‌شده به DdrReportِ CANDIDATE لینک می‌شود، idempotent، refusal برای version ناموجود.
۸. **D-22 — golden corpus چند layout** — DONE: `test_multi_layout_corpus.py` — سه layout مصنوعی DDR (prose/key-value/grid) همه DDR classify می‌شوند و report token هر کدام استخراج می‌شود.
۹. **D-12 — SHAهای stale در docs** — DONE: سه سند current-facing به tip `c7fce65…` و تاریخ ۲۰۲۶-۱۱-۲۳ به‌روز شد.
۱۰. **D-23 — بازسازی honest inventory/roadmap** — DONE: `docs/CAPABILITY_INVENTORY_2026-11-23.md` (۵۸ حوزه) و `docs/ROADMAP_2026-11-23.md`.
۱۱. **D-13 — این گزارش** — DONE (همین سند؛ بدون SHA خودارجاعی — SHA نهایی در §۳۰).
۱۲. **OCR / MinerU** — NOT QUALIFIED (بدون runtime واقعی inference؛ بدون تغییر).
۱۳. **محاسبات** — فقط `npt.hours_rollup` نسخهٔ ۱ executable؛ سایر methodها OWNER-GATED.
۱۴. **Phase W — quality suite** — در حال اجرا در زمان نوشتن این سند؛ نتیجهٔ نهایی در §۳۰.
۱۵. **Phase AA — CI exact-SHA** — workflow_dispatch روی SHA نهایی پس از commit این سند؛ replay باید صریحاً enabled شود (run قبلی 45137410944 replay skip شده بود و هرگز claim نمی‌شود).
۱۶. **Phase AB — git discipline** — commit با pathهای صریح فقط؛ بدون `add -A`/force/reset/clean.
۱۷. **exclusions** — بدون دادهٔ واقعی چاه، بدون مقادیر مهندسی اختراعی، submission ≠ approval، actor ≠ authentication.
۳۰. **evidence** — commitهای این مأموریت: `3fc7b18` (Phase C/D)، `6407d81` (lint)، `36b042d` (D-21)، `c7fce65` (D-19)، `64463dc` (D-12)، `31daf9a` (D-20)، `a701609` (D-22)، به‌علاوهٔ commit docs/گزارش. SHA نهایی = SHA commit شامل این سند؛ CI run IDها در یادداشت‌های شاخه/PR ثبت می‌شود (نه به‌صورت خودارجاعی در این سند).
³¹. **verdict کلی** — DONE برای D-12..D-23؛ NOT QUALIFIED: OCR، PostgreSQL، auth، physical Windows، DR؛ OWNER-GATED: performance blob و محاسبات غیر npt.hours_rollup.
