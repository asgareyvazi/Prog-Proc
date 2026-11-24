# گزارش ادامهٔ مأموریت مهندسی محصول — ۲۰۲۶-۱۱-۲۴ (FA)

**شاخه:** `arena/5957b76d-prog-proc` — **main:** `85122e1a…` (دست‌نخورده) — > SHA نهایی این مأموریت در پاسخٔ پایانی Arena گزارش می‌شود (این سند به‌صورت خودارجاعی SHARecorder.final را نمی‌نویسد).

**پایگاه‌دادهٔ qualified:** فقط SQLite — **head Alembic:** `0029`
**حفاظت:** `out/performance.json` هرگز خوانده/stage نشده؛ فقط `git ls-tree` → blob `df63b57f…` تأیید شد.

واژگان: DONE / PARTIALLY DONE / SERVICE-ONLY / NOT RUN / NOT QUALIFIED / OWNER-GATED / BLOCKED / PLANNED / DELIBERATELY EXCLUDED.

۱. **Phase A preflight** — DONE: clone `/tmp/Prog-Proc-mission2`؛ HEAD=remote=`ac6e978…`؛ ۴۷۹ commit ahead / 0 behind main؛ tree clean؛ owner workspace `/home/user/Prog-Proc` (HEAD `317c553` با تغییرات از پیش موجود owner) دست‌نخورده.
۲. **Phase B — CI shard reproducibility (release blocker)** — DONE: root cause = collection job با install float و shard jobها با pin → harte Resoluo diferente؛ fix: install identical pin (`-c constraints/ui.txt -c constraints/build.txt`) در collection job؛ جایگزینی `config.inicfg` با digest مبتنی بر فایل ini + `getini` (API پشتیبانی‌شده)؛ تشخیص‌های bounded (field diffs + first-10 missing/extra node IDs) بدون تضعیف fail-closed؛ ۱۰ تست adversarial برای قرارداد manifest. commit `37cd534` (+lint/format).
۳. **Phase C** — DONE: سه سند current-facing به tip به tip شاخه pin و dated 2026-11-24. commit `5c71537`.
۴. **Phase D — batch hardening** — DONE: D1 fail-closed workspace_id؛ D2 TOCTOU-safe (یک fd با O_NOFOLLOW، fstat، read bounded، SHA روی همان bytes، staging verify‌شده که register می‌شود)؛ D7 on_entry فقط یک‌بار به ازای هر entry + شمارندهٔ cancelled. ۴ تست جدید. commit `607053b`.
۵. **Phase E — Qt batch workflow** — DONE: E1 نام‌های staging collision-resistant با نگاشت staged→original؛ E4 retry بر اساس identity با re-import منبع اصلاح‌شده؛ progress یکنوا. ۲ تست UI جدید. commit `607053b`.
۶. **Phase F — source-to-DDR candidate** — DONE: `operations/ddr_candidates.py`؛ field ruleهای labelled با locator صفحه/خط؛ ambiguous → UNRESOLVED (هرگز guess نمی‌شود)؛ dispositionهای ACCEPTED/EDITED/REJECTED با حفظ مقدار خام و ویرایش‌شده؛ authorize فقط CANDIDATE؛ صف review دقیق نسخهٔ کنونی حالا metadata نسخه را هم می‌خواند. ۵ تست. commit `e5431a3`.
۷. **Phase I — survey contract** — DONE: headerهای تکراری/نام/excessive، ردیف‌های ragged، فایل خالی، محدودیت‌های size/station، واحدهای inconsistent — همه refuse با خطای مشخص؛ واحدهای ناشناخته verbatim حفظ می‌شوند. ۶ تست جدید. commit `f6f60d7`.
۸. **Phase G/H** — PARTIALLY DONE: corpus موجود ؛ matrix V1–V15 با scope label؛ scnarios UI E2E موجود؛ golden corpus چند layout کامل‌تر و ماتریس لایه‌ای کامل در Release 1.
۹. **Phase S (OCR)** — NOT QUALIFIED (بدون runtime واقعی؛ metrik اختراع نشده).
۱۰. **محاسبات** — فقط `npt.hours_rollup` V1 cadre executable؛ بقیه OWNER-GATED.
۱۱. **Phase Y/Z** — DONE: `docs/CAPABILITY_INVENTORY_2026-11-24.md` (۵۹ حوزه) و `docs/ROADMAP_2026-11-24.md` (Release 0–8 با gate).
۱۲. **Phase AA** — testهای lokal: ۵۷ targeted passed؛ ruff check/format clean. CI exact-SHA: run IDها در پاسخٔ پایانی Arena ثبت می‌شود. **Replay تاریخی (run 43695755434): artifacts دیگر در دسترس نیست → NOT RUN؛ هرگز claim نمی‌شود.**
۱۳. **Phase AC** — clone مأموریت clean و pushed؛ owner workspace دست‌نخورده؛ remote SHA = local HEAD تأیید شد.

**verdict کلی:** PARTIALLY ACCEPTED — بلاک Erwachsene CI با اصلاح کد بسته شد؛ وضعیت shardهای Linux روی SHA نهایی به‌صورت دقیق در پاسخٔ پایانی Arena گزارش می‌شود.
