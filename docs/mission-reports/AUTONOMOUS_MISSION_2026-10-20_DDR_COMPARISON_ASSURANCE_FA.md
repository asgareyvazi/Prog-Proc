# گزارش مأموریت: تضمین مقایسهٔ DDR و مرزهای ارزیابی

> **به‌روزرسانی ادامهٔ مأموریت — وضعیت پس از CI روی SHA `3a904746832a612b3a133ea30af12212dd8037e2`:** این بلوک بر هر وضعیت قدیمی‌تر مقدم است. branch مجاز `arena/5957b76d-prog-proc` است؛ `main` بدون تغییر. GitHub API دو run همین SHA را برگرداند: [Linux CI `43578750939`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/43578750939) و [Windows CI `43578750947`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/43578750947)؛ هر دو **failure** هستند، نه qualification. API timestamp آن‌ها را 2026-11-14 نشان می‌دهد که از تاریخ سیستم مأموریت 2026-10-20 جلوتر است؛ این تناقض زمانی حل‌نشده ثبت می‌شود و تاریخ به‌عنوان مبنای نتیجه‌گیری به کار نمی‌رود.
>
> **Linux:** full suite در CPython 3.14 برابر 2,873 collected / 2,808 passed / 62 skipped / 3 failed؛ Ubuntu 26.04/CPython 3.14 برابر 2,873 / 2,869 / 1 / 3. CPython 3.11 suite اجرا نشد، چون pins-check پیش‌نیاز شکست خورد. سه خطای تکرارشده: `test_the_inventory_on_disk_matches_the_tree`، `test_required_jobs_stay_on_pinned_ubuntu_images_and_actions_are_pinned` و `test_pytest_plugin_emits_exact_once_real_execution_records`. چهار shard، به‌ترتیب 0..3: (690: 689 pass, 1 fail)، (739: 738 pass, 1 fail)، (730: 728 pass, 1 skip, 1 fail)، (714: 714 pass). مجموع‌ها 2,873 / 2,869 / 1 / 3؛ shard 3 تنها shard کاملاً سبز بود. Reconciliation job `132401878465` هم failure شد و گزارش نهایی artifact تولید نشد؛ بنابراین exact-once پوشش روی API/payload **اثبات نشده**.
>
> **Windows:** CPython 3.11 و 3.14 هرکدام 2,873 collected / 2,806 passed / 64 skipped / 3 failed؛ همان سه خطا. Qt UI، شش lane پایداری، clean-install و PowerShell run-card jobs موفق‌اند، اما این‌ها full-suite green را جبران نمی‌کنند. Linux compatibility/clean-install و CPython 3.14 workbench موفق‌اند؛ CPython 3.11 workbench به علت policy test شکست خورد.
>
> GitHub API پنج artifact metadata اصلی sharding را برگرداند (collection + چهار shard؛ شناسه، اندازه و digest کامل در فایل traceability). Payloadها و JUnit ZIPها در sandbox قابل‌دسترسی نبودند؛ دریافت log با EOF روی `results-receiver.actions.githubusercontent.com` شکست خورد، پس محتوای payload و failure bodyها بازبینی نشده‌اند. نتیجه فقط بر اساس API job annotations/totals و artifact metadata است.
>
> اصلاحات محدود روی همان چهار مسیر مجاز اعمال شد: identity محلی در manifest fixture صریح شد؛ evidence job به Ubuntu 26.04 منتقل شد تا policy موجودِ دقیقاً چهار runner Ubuntu 24.04 حفظ شود؛ و گزارش اولیهٔ `INCOMPLETE` پیش از reconciliation نوشته می‌شود تا در خطای غیرمنتظره هم artifact fail-closed باقی بماند. آزمون محلی شبیه‌سازی‌شدهٔ دو regression و YAML/Ruff/format/diff checks موفق‌اند؛ تغییرها هنوز برای SHA تازه push/CI نشده‌اند. **exception inventory و constraints بیرون از مسیرهای مجاز می‌مانند و دست‌کاری/تضعیف نمی‌شوند**؛ تا رفع owner-authorized blockers هیچ DONE ادعا نمی‌شود.
>
> Snapshot forensic قبلی: `/tmp/prog-proc-forensic-backup-20261020-current.tar` (254 عضو، SHA-256 `09c16105af92f2bb81add99cad2a9acdd9a06955071ae2c6119ca52d48202ee4`)؛ snapshot چهار فایل مجاز برای اصلاح بعدی: `/tmp/prog-proc-shard-ci-fix-backup-20261020.tar` (SHA-256 `17783cbae086d98f0b56804c94a076874492d0c17f414aad6151898a40121c3c`). وضعیت نهایی فایل محافظت‌شده: `out/performance.json` همچنان غایب و unstaged.

**تاریخ مأموریت:** 2026-10-20 UTC (تاریخ سیستم مأموریت)<br>
**زمان‌های CI ثبت‌شده در GitHub:** 2026-11-08 UTC؛ این اختلاف زمانی حفظ شده است.<br>
**شاخهٔ مجاز:** `arena/5957b76d-prog-proc`<br>
**وضعیت گزارش در این نسخه:** کار پیاده‌سازی و آزمون‌های هدفمند انجام شده؛ گزارش نهایی هنوز منتظر نتیجهٔ Ubuntu CI روی SHA کد و CI دقیق tip نهایی اسناد است. هیچ run معلقی PASS محسوب نشده است.<br>
**نتیجهٔ محصول:** همچنان **Local-first, single-user, Pre-Alpha**؛ این گزارش به معنی DONE بودن محصول یا پذیرش production نیست.

## خلاصهٔ نتیجه

مقایسهٔ revisionهای DDR بر پایهٔ محتوای authored و ترتیب اطراف آن انجام می‌شود؛ sequence، row ID یا timestamp هویت فعالیت نیست. تکرار متن و pairingهای چندمعنا صریحاً ambiguous باقی می‌مانند و متن اپراتور دست‌نخورده است. CLI و UI روی جفت predecessor/successor صریح، read-only هستند؛ خروجی شامل متن/تغییرات، ambiguity، provenance و omitted count است و review، approval، audit، status یا DB write ایجاد نمی‌کند. CLI JSON پایدار است و نتیجهٔ قطعی، ambiguity، operational/scope error و usage error exit codeهای متمایز دارند.

آزمون‌های service-generated و process-level SQLite با processهای `spawn`، engine/session مستقل، هماهنگی محدود، بررسی persisted invariantها، cleanup و integrity/FK در کد حاضرند. MinerU همچنان optional/local-only است؛ corpus هفت‌حالته مصنوعی است و inference/دقت واقعی ادعا نشده است. `metadata.path` عمومی provenance می‌تواند مسیر منبع را حفظ کند؛ این با command/error metadata پاک‌سازی‌شده فرق دارد. License metadata اصلاح شده، بدون تفسیر حقوقی جدید.

## معیارها و شواهد

| حوزه | نتیجهٔ این مأموریت | مرز ادعا |
|---|---|---|
| Content alignment | پیاده‌سازی و unit/service-generated tests | alignment محافظه‌کارانه بر authored content و ترتیب؛ pairing چندمعنا مبهم است، نه حدس‌زده. |
| CLI comparison | پیاده‌سازی و integration tests | جفت revision صریح، JSON پایدار، exit codeهای جدا، فقط‌خواندنی؛ actor string احراز هویت نیست. |
| UI comparison | پیاده‌سازی و Qt-enabled exact-SHA jobs | Windows offscreen در `b1deab1` برابر 30/30 هر runtime؛ offscreen، پذیرش display فیزیکی Windows نیست. محلی به‌علت نبودن PySide6 اجرا نشد (SKIPPED، نه PASS). |
| SQLite concurrency | آزمون‌های process-level روی DB فایل‌محور disposable | `multiprocessing.get_context("spawn")`، connection مستقل، wait/join محدود، cleanup و persisted state؛ این evidence فقط SQLite است. |
| MinerU | boundary tests و corpus مصنوعی | هفت fixture کنترل‌شده؛ inference محلی NOT RUN، مدل NOT CHECKED، OCR NOT RUN، metrics `null`. بدون دانلود مدل یا upload پنهانی. |
| License/package | metadata با `LicenseRef-Proprietary`; build/install evidence | تغییر، نظر حقوقی دربارهٔ مجوز نیست. CI clean-install jobs روی SHA `b1deab1` پاس شدند. |
| Readiness/operator workflow | CLI محلی و tests | `config validate --json`: valid، بدون بازکردن DB/workspace و بدون service probe. MinerU runtime در این host `NOT_READY`. |

Taxonomy تغییر نکرده: 26 کلاس، 14 `END_TO_END_CERTIFIED`، 7 `KNOWLEDGE_SUPPORTED`، 5 `EXTRACT_ONLY`، 12 handlers. PostgreSQL واجد qualification نیست. M2–M5، O-02/O-03، authentication/multi-user، risk/AFE/currency، محاسبات جدید، production DR، enterprise archive، deployment و پذیرش فیزیکی Windows همچنان owner-gated/unqualified هستند. Backup/archive با DR یکی نیست.

## اعتبارسنجی محلی

- `tests/integration/test_ddr_authoring.py`, `tests/integration/test_ddr_cli.py`, `tests/unit/test_ddr_comparison.py`, `tests/integration/test_mineru_boundary.py`, `tests/unit/test_mineru_qualification.py`, `tests/unit/test_exception_policy.py`: **89 passed / 89** (15.74s).
- `tests/unit/test_pytest_totals.py` و `tests/unit/test_qt_stability_manifest.py`: **26 passed / 26**.
- Docs/CLI/policy regression set (product decisions, Windows acceptance doc, CLI surface/CLI, exception policy): پس از ایجاد گزارش و اصلاحات فعلی، **63 passed in 42.22s** در همان مجموعه؛ این شامل regressionهای product/Windows docs است (زیرمجموعهٔ 16تایی جداگانه شمرده نمی‌شود). `exception_review.py --check` نیز جداگانه موفق بود.
- Ruff lint/format (449 files)، mypy (44 source files)، compileall، Alembic single head `0026` و `tools/exception_review.py --check` موفق‌اند. Inventory: 108 broad handlers (13 re-raised، 67 reported، 28 suppressed). `git diff --check` موفق است.
- Full local suite اجرا نشد. Qt محلی اجرا نشد چون PySide6 نصب نیست.
- `drillintel mineru validate --json` در این محیط exit 1 و `NOT_READY` گزارش کرد؛ مدل `NOT_CHECKED`، OCR `NOT_RUN` و metrics `null`. این رفتار مورد انتظار برای نبود runtime است، نه تست شکست‌خورده.

## CI دقیق SHAها

### پیش‌درآمد

- Code SHA `512aa333bbdafe59bd035ea558e6bbfa1f2ab237`: Linux run [`42486809667`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42486809667) و Windows run [`42486809647`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42486809647) با conclusion **cancelled** تمام شدند؛ PASS نیستند. همان SHA یک UI fixture با 261 فعالیت داشت، بیش از سقف service برابر 200؛ fixture در SHA بعد اصلاح شد.
- SHA `41f04d6a6ddd2abe4339c410b7e688004eb62c4a`: Linux run [`42488614446`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42488614446) **failure**؛ CPython 3.11 و 3.14 هرکدام 2,847 collected / 2,784 passed / 62 skipped / 1 failed، هر دو failure همان `tests/unit/test_exception_policy.py::test_the_inventory_on_disk_matches_the_tree`. Ubuntu 26.04/3.14: 2,847 / 2,845 / 1 skipped / 1 failed؛ identity شکست در annotation نبود.
- همان SHA در Windows run [`42488614499`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42488614499) **cancelled**؛ هر دو runtime همان inventory failure داشتند (2,847 / 2,782 / 64 / 1). دو Windows history stability lane از سقف حفظ‌شدهٔ 45 دقیقه گذشتند. Qt UI هر runtime 30/30؛ intake/presentation هر runtime 15/15؛ clean-install و PowerShell run-card pass. علت ریشه‌ای timeout قدیمی اثبات نشده است.

### Diagnostics code SHA `b1deab1782d6c17552ca4334606b5521c84c44ef`

Runها دقیقاً روی همین SHA اجرا شدند: Linux [`42501577186`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42501577186)، Windows [`42501577174`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42501577174).

- Linux CPython 3.11 و 3.14 هرکدام **2,850 collected / 2,787 passed / 62 skipped / 1 failed**؛ sole failure همان exception inventory قدیمی است. Runtimeهای pytest: 2,317.756s و 1,930.477s. Custom diagnostic test identity را بدون failure body گزارش کرد؛ annotation عادی pytest همچنان excerpt خودش را دارد.
- Windows CPython 3.11 و 3.14 هرکدام **2,850 / 2,785 / 64 / 1** با همان sole inventory failure؛ pytest runtimeها 2,431.548s و 2,533.139s. Windows workflow پس از 44m22s با **failure** پایان یافت؛ attribution report نیز به‌دلیل non-success بودن دو test job fail شد، نه یک failure محصول جدا.
- Qt offscreen UI در Windows برابر **30/30** برای هر دو runtime. شش Windows stability lane (history/intake/presentation × CPython 3.11/3.14) هرکدام **15/15 PASS**؛ history دو lane 2m11s و 2m20s. Windows clean-install smoke، PowerShell 5.1/7 run-card، Linux clean-install و Linux/Ubuntu workbench jobs موفق شدند. Linux UI coverage: 76.54% lines / 55.13% branches. این‌ها full-suite green یا پذیرش فیزیکی Windows نیستند.
- Ubuntu 26.04 full-suite job [`128795266228`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42501577186/job/128795266228) در آخرین بررسی (2026-11-08 20:31 UTC) هنوز `in_progress` بود؛ full-suite step از 16:47:07Z اجرا می‌شد (حدود 3h44) و run API `updated_at` برابر 16:46:32Z نشان می‌داد. نه completion، نه exit، نه test identity/skip details در دسترس بود. این وضعیت **PENDING / NOT PASS** است؛ workflow Linux را green نمی‌خوانیم.

Run `42501577174` UI، 12 artifact metadata مورد (نام/اندازه/SHA-256) ارائه می‌کرد؛ جزئیات آنها در traceability آمده است. Payloadها/JUnit ZIPها از `productionresultssa*.blob.core.windows.net` به sandbox قابل‌دسترسی نبودند؛ بنابراین artifact bytes، skip reasonهای دقیق و attempt logهای payloadی inspect نشده‌اند. GitHub UI و annotations برای نتایج بالا و metadata شاهد هستند، نه payload.

## وضعیت اسناد و تحویل

فایل traceability: [`DDR_COMPARISON_ASSURANCE_TRACEABILITY_2026-10-20.md`](DDR_COMPARISON_ASSURANCE_TRACEABILITY_2026-10-20.md). Exception inventory دوباره تولید شده و در worktree هست. Current-facing docs به نتایج قابل‌مشاهدهٔ فوق به‌روزرسانی شده‌اند. **گزارش/اسناد هنوز commit نشده‌اند و exact-SHA CI روی tip نهایی اسناد هنوز اجرا نشده است.** نتیجهٔ Ubuntu روی `b1deab1` را نیز باید تا پایان انتظار داشت یا به‌صراحت pending ثبت کرد؛ آن را سبز فرض نکنید.

`out/performance.json` حذف محافظت‌شده باقی می‌ماند: restore/stage/commit/recreate نمی‌شود. `main` بدون تغییر. پیش از commit نهایی باید status/stash را ثبت کرد، فقط فایل‌های مجاز را stage کرد، روی branch مجاز commit/push کرد و Linux/Windows را روی SHA دقیق docs tip اجرا و ثبت کرد.

## ثبت baseline این continuation

در 2026-11-14 05:15 UTC، branch مجاز درست بود اما `HEAD` محلی `317c55365319af61138000664293ae2d84e27a6e` (shallow/grafted و بدون parent ثبت‌شده در این checkout) و upstream تنظیم‌نشده بود؛ index پاک، stash خالی، 98 فایل tracked تغییرکرده و 123 فایل untracked بود. پس از fetch فقط refها، remote feature برابر `b1deab1782d6c17552ca4334606b5521c84c44ef` و `main` برابر `85122e1a98087db4495b820a9a0ffae9009b7819` تأیید شد؛ HEAD محلی ancestor شاخهٔ remote است، نه شاخه‌ای واگرا. مقایسهٔ بایتی نشان داد 209 مسیر materialized با feature tip عیناً یکسان‌اند، 10 سند مأموریت/جاری تفاوت دارند و دو گزارش محلی جدیدند؛ source/tests/workflows تغییری نسبت به feature tip ندارند. `out/performance.json` در filesystem غایب است و در HEAD پایه track نمی‌شود، اما در tree شاخهٔ remote وجود دارد؛ به‌عنوان حذف محافظت‌شده نگه داشته شد و هیچ restore/stage/commit انجام نشد. در این baseline، `git diff --cached` خالی و main دست‌نخورده است. جزئیات و hash patch در traceability ثبت شده‌اند.

## ادامهٔ بررسی sharding و وضعیت فعلی (2026-11-14 UTC)

برای lane طولانی Ubuntu 26.04/Qt، ابزار آزمایشی `tools/pytest_shard.py` تکمیل و یک aggregator fail-closed در `tools/pytest_shard_report.py` اضافه شد. تست‌های تازه در `tests/unit/test_pytest_shards.py`، deterministic/disjoint assignment، digest و run identity، تطبیق Python/platform و runner label، artifacts و job statuses، JUnit/totals/process records، skipهای Qt، پوشش دقیق یک‌بارهٔ هر node ID و رفتار CLI در حالت INCOMPLETE را بررسی می‌کنند. این ابزارها **به workflowهای GitHub Actions متصل نشده‌اند**؛ artifact payload/API integration واقعی نیز انجام نشده است.

- Collection-only محلی: **2,860 تست**؛ هشت shard به‌طور متوازن دقیقاً تمام node IDها را یک‌بار پوشش می‌دهند (manifest hash محلی `54dc0072db3b8396480ffb86ace6e3bfcf0d09d8f14625ddb408d365d6719332`). این شناسه، run یا محیط CI نیست.
- `tests/unit/test_pytest_shards.py`: **10 passed**؛ pytest totals ثبت‌شده: 10 passed، صفر failure/error/skip، exit 0. Ruff check/format و compileall برای سه فایل جدید موفق؛ `exception_review.py --check` نیز 108 handler و همهٔ suppressionها را تأیید کرد.
- یک اجرای واقعی shard محلی روی collection پیشین 2,856تایی، shard 0/8 را 361.42 ثانیه اجرا کرد: 337 passed، 5 skipped، **1 failed** و 2,513 deselected. شکست `tests/unit/test_exception_policy.py::test_the_inventory_on_disk_matches_the_tree` در آن اجرا به broad `BaseException` cleanup موقتی مربوط بود که plugin تازه اضافه کرده و inventory ثبت نکرده بود؛ آن handler بعداً حذف شد و `exception_review.py --check` فعلی موفق است. اجرای shard همچنان failed است، نه PASS، و از آن نمی‌توان علت شکست historical CI را نتیجه گرفت. به‌علت `-o addopts=''` totals plugin خودکار پروژه غیرفعال بود؛ فقط sidecarهای shard/JUnit تولید شدند.
- Inventory بازنویسی نشد. مسیر محلی `docs/exception_inventory.md` untracked و owner-owned بالقوه تلقی شد؛ مقایسهٔ read-only نشان داد نسخهٔ remote و inventory محلی در line numberهای source فرق دارند. Inventory محلی بعد از حذف handler موقت با 108-handler check هماهنگ است؛ علت چهار شکست تاریخی remote CI هنوز اثبات/حل نشده است. Ubuntu timeout/cancel هم کماکان PASS نیست.

وضعیت فعلی: branch مجاز `arena/5957b76d-prog-proc` است؛ local HEAD=`317c55365319af61138000664293ae2d84e27a6e` و remote feature=`b1deab1782d6c17552ca4334606b5521c84c44ef`؛ main=`85122e1a98087db4495b820a9a0ffae9009b7819`. Index خالی، stash خالی، 98 modified tracked و 131 untracked path; no upstream configured. `out/performance.json` disk-absent مانده و به آن دست زده نشده است. چون source/workflowها و بسیاری از اسناد موجود در worktree owner-owned بالقوه‌اند و نسبت به HEAD محلی uncommitted، فقط فایل‌های تازهٔ sharding را جداگانه commit کردن روی ancestry فعلی قابل‌تحویل/سازگار نیست؛ هیچ staging/commit/push یا GitHub run جدیدی انجام نشد. ماموریت **PARTIALLY DONE / NOT QUALIFIED** است: CI workflow integration، inventory provenance/repair ایمن، full-suite کامل Linux/Windows در SHA نهایی و سایر gates/end-to-end موارد همچنان باز هستند.
