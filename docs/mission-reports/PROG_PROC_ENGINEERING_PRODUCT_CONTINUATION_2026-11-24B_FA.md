# گزارش ادامهٔ مأموریت مهندسی محصول — ۲۰۲۶-۱۱-۲۴B (FA)

**شاخه:** `arena/5957b76d-prog-proc` — **main:** `85122e1a…` (دست‌نخورده)
**نوک شاخه در پایان این گزارش:** `16b5a2d2…` — SHA نهایی پس از آخرین push در پاسخٔ پایانی Arena گزارش می‌شود؛ این سند به‌صورت خودارجاعی SHA نمی‌نویسد.

**پایگاه‌دادهٔ qualified:** فقط SQLite — **head Alembic:** `0030` (جدول append-only `ddr_disposition`)
**حفاظت:** `out/performance.json` هرگز خوانده یا stage نشده؛ فقط با `git ls-tree HEAD` blob `df63b57f…` تأیید شد.

واژگان: DONE / PARTIALLY DONE / SERVICE-ONLY / NOT RUN / NOT QUALIFIED / OWNER-GATED / BLOCKED / PLANNED.

۱. **Phase A — preflight** — DONE: clone تازهٔ `/tmp/Prog-Proc-mission2`؛ HEAD = remote = `0f72875…`؛ tree تمیز؛ owner workspace `/home/user/Prog-Proc` ( با SHA `317c553` و تغییرات ازپیش‌موجود) دست‌نخورده؛ venv تازه و stubهای Qt بازسازی‌شده؛ `QApplication` به‌صورت offscreen سبز.
۲. **Phase B — قرارداد نصب مشترک Qt** — DONE: اسکریپت `tools/ci_install_qt.sh` به‌عنوان قرارداد واحد نصب با pin (`constraints/ui.txt` + `constraints/build.txt`) برای jobهای collection و shard؛ هیچ نصب شناور `pip install -e ".[dev,ui]"` در ci.yml باقی نمانده؛ `tests/unit/test_ci_contract.py` (۳ تست) این قرارداد را enforce می‌کند. commit `0f3b442`.
۳. **Phase C — replay تاریخی** — DONE (ر تأیید verification): API مربوط به run `43695755434` دقیقاً ۱۰ artifact با `expired=false` نشان می‌دهد؛ ادعای قبلی «منقضی بودن» با evidence agencia رد شد و کنار گذاشته شد؛ job مربوط به replay حالا `continue-on-error: true` است و هرگز یک run سبزِ SHA جاری را fail نمی‌کند؛ تست policy به‌روز شد. commit `0f3b442`.
۴. **Phase D — pin کردن docهای stale** — DONE: `PRODUCT_CAPABILITY.md`، `PROJECT_CONTEXT.md` و `ENGINEERING_PROGRAM_PROCEDURE_MASTER_PLAN.md` از SHA قدیمی `a61b8b7` به نوک کنونی شاخه re-pin شدند. commit `0f3b442`.
۵. **Phase E — hardeningِ batch import** — DONE: `_open_entry_fd` (باز کردن با openat و O_NOFOLLOW|O_DIRECTORY روی هر مؤلفهٔ مسیر — امن در برابر TOCTOU، نه فقط O_NOFOLLOWِ مؤلفهٔ پایانی)، `_stage_atomic` (O_EXCL + fsync + rename — اتمیک و بدون collision)، هر entry در یک SAVEPOINT (تراکنش جداگانهٔ هر entry)، و تست‌های adversarial برای symlink در هر مؤلفه، collision در staging، ایزولاسیون savepoint و field-Version قدیمی. commit `074767c`.
۶. **Phase F — صفحهٔ Qt batch** — DONE: نوشتن durableِ `outcome.json` (فایل موقت + fsync + rename)، reopen/history واقعی از batchهای قبلی، و shutdown امن thread؛ ۳ تست UI. commit `a08c8fb`.
۷. **Phase G — workflow واقعی source-to-DDR** — DONE: جدول append-only `ddr_disposition` (model + migration `0030` با triggerهای no-update/no-delete در سطح پایگاه‌داده؛ `metadata_json` هرگز برای یک تصمیم mutate نمی‌شود)؛ locatorهای صفحهٔ واقعی (page از form-feed، نه page=1ِ ثابت)؛ field contract با واحد صریح (واحد نادرست یاناموجود missing → UNRESOLVED و هرگز تبدیل نمی‌شود)؛ state سطح candidate؛ gateهای authorize تقویت‌شده (فیلدهای required باید ACCEPTED باشند و well تطابق داشته باشد)؛ سطوح عمومی Qt (صفحهٔ «DDR Candidates») + CLI (`drillintel ddr-candidates`) + service (`DdrWorkflowService`). commit `cf2f65a`.
۸. **Phase H — صف review در سطح candidate** — DONE: `review_queue` حالا برای هر نسخه detail سطح candidate (field، decision، locator، actor) را نیز می‌آورد؛ تست Phase H. commit `16b5a2d`.
۹. **Phase I — survey با SHA سطح byte** — DONE: SHA-256 روی byteهای اصلی فایل (نه متن re-encode‌شده)، bounded read قبل از size check، وrefuse symlink؛ ۳ تست. commit `074767c`.
۱۰. **Phase J/K — corpus چند layout و ماتریس لایه‌ای** — DONE (re-verify): `test_multi_layout_corpus.py` و `test_e2e_matrix_v1_v15.py` سبز (۱۶ تست؛ head Alembic حالا `0030`).
۱۱. **Phase L–S — builderها، traceability، عملیات DDR، NPT، ماژول‌ها، search، گزارش‌ها** — موجود و در suiteٔ کامل سبز؛ گسترش بیشتر در Release 1.
۱۲. **Phase T — governance محاسبات** — فقط `npt.hours_rollup` نسخهٔ ۱ قابل اجراست؛ بقیه OWNER-GATED (decision packet).
۱۳. **Phase U — OCR/MinerU** — NOT QUALIFIED (بدون runtime واقعی؛ متریک اختراع نشده).
۱۴. **Phase AA/AB — docs** — DONE: `docs/CAPABILITY_INVENTORY_2026-11-24.md` و `docs/ROADMAP_2026-11-24.md` با ردیف‌های 2026-11-24B به‌روز شدند.
۱۵. **تست‌های local** — DONE: واحد ۱۱۰۶ passed؛ integration+ui ۱۹۲۰ passed و ۱ skipped (پس از repair تست‌های migration برای جدول 0030).
۱۶. **Phase AC — CI با SHA دقیق** — runهای Linux و Windows روی SHA نهایی در حال اجرا هستند؛ نتیجهٔ دقیق در پاسخٔ پایانی Arena ثبت می‌شود (بدون evidence، claim سبز داده نمی‌شود).
۱۷. **Phase AE — closure** — remote SHA = local HEAD پس از هر push تأیید شد؛ owner workspace دست‌نخورده.

**verdict اولیه:** PARTIALLY ACCEPTED — replay تاریخی NOT RUN (ادعای «منقضی بودن» با evidence API رد شد؛ replay اختیاری است و هرگز green بودن run جاری را fail نمی‌کند).
