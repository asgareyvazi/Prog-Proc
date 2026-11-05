# گزارش نهایی مهندسی و qualification محصول Prog-Proc

- **تاریخ گزارش:** ۲۰۲۶-۱۱-۰۶ (Asia/Tehran)
- **شاخهٔ کار:** `arena/5957b76d-prog-proc`
- **SHA دقیق qualification کد/آزمون/constraint:** `279449e611b7645d48c0796b6792a0b946d7542c`
- **حکم محصول:** **PARTIALLY QUALIFIED — PRE-ALPHA**
- **حکم انتشار عمومی/production:** **NOT READY / NOT AUTHORIZED**

> شواهد CI و artifactهای جدول‌های این گزارش فقط به SHA دقیق `279449e...` تعلق دارند؛ هیچ نتیجه‌ای از SHA دیگری به آن منتقل نشده است. پیاده‌سازی و regressionهای محصول پیش‌تر در `b3163bf...` ثبت شده بودند؛ commitهای بعدی تا `279449e...` گزارش/تصمیم‌های محصول و سپس pinهای resolver را تغییر دادند. بنابراین `279449e...` همان کد و تست محصول را همراه با constraintهای اصلاح‌شده در بر دارد و هر دو workflow روی همین SHA دقیق اجرا و کامل شدند. هر نتیجهٔ جداگانه‌ای برای commit بعدیِ خود این گزارش، اگر اجرا شود، باید جداگانه بر همان commit ثبت شود.

## ۱. حکم به تفکیک معیار

| محور | نتیجه | آنچه نتیجه ثابت می‌کند / نمی‌کند |
|---|---|---|
| پیاده‌سازی | **PASS در محدودهٔ مستقل و مصوب مأموریت؛ موارد owner-gated باز** | اصلاح چرخهٔ lifecycle درس در supersession منبع، یکتایی کد درس و regressionهای مرتبط موجودند. قابلیت فاقد تصمیم مالک ساخته نشده است؛ این حکم همهٔ نیازهای آینده را پوشش نمی‌دهد. |
| آزمون‌ها و quality gates | **PASS روی SHA `279449e...`** | Linux، Ubuntu 26.04 و Windows full-suiteها و gateهای تنظیم‌شده موفق‌اند. totals و skipها جداگانه در بخش ۴ آمده‌اند؛ skipped به‌معنی PASS نیست. |
| Windows خودکار | **PASS (offscreen/CI)؛ نمایشگر فیزیکی NOT RUN** | full-suite دو runtime، UIهای Qt offscreen، stability suites، clean-install و run-card behavioral tests موفق‌اند. این‌ها پذیرش دستی روی workstation/display واقعی Windows نیستند. |
| Qualification محصول | **PARTIALLY QUALIFIED — PRE-ALPHA** | SQLite و مسیرهای مشخص local-first شواهد اجرایی دارند؛ PostgreSQL، عملیات production، چندکاربره و پذیرش فیزیکی Windows تأیید نشده‌اند. |
| آمادگی انتشار | **NOT READY / NOT AUTHORIZED برای production یا انتشار عمومی** | wheel/sdist clean-install و CLI smoke موفق‌اند، اما installer امضاشده، DR/عملیات کامل، رمزگذاری archive، PostgreSQL، authentication، multi-user و پذیرش دستی Windows اثبات نشده‌اند. |

CI سبز، تصمیم مالک، پذیرش انسانی یا operational qualification را جایگزین نمی‌کند.

## ۲. مبنای Git، حفاظت تغییرات و محدودهٔ شواهد

- تمام کار روی شاخهٔ مجاز `arena/5957b76d-prog-proc` انجام شد؛ `main` و branchهای دیگر تغییر نکردند و branch یا commitی بازنویسی نشد.
- پیش از این گزارش، HEAD محلی و remote شاخه روی `279449e...` بودند؛ `main` روی `85122e1a98087db4495b820a9a0ffae9009b7819` بود.
- وضعیت محافظت‌شدهٔ checkout: فقط `D out/performance.json`، unstaged و خارج از commit. این deletion در طول مأموریت restore، stage یا commit نشده است. فایل در tree ریموت موجود است؛ حذف فقط تغییر محلی محافظت‌شده است.
- staging خالی بود و stashهای قبلی دست‌نخورده ماندند. در commit گزارش فقط همین فایل گزارش stage/commit خواهد شد؛ deletion محافظت‌شده وارد index نمی‌شود.
- commits مرتبط: `b3163bf...` شامل اصلاحات کد/آزمون محصول؛ `6df276d...` و `6512d96...` شامل مستندسازی/تصمیم owner gate؛ `279449e...` شامل به‌روزرسانی رسمی Ruff pinها پس از مشاهدهٔ resolver drift. اجرای qualification نهایی روی `279449e...` انجام شده است.
- Linux CI دقیق SHA `279449e...`: [run 42117361649](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42117361649)، `completed / success`، **۹/۹ job موفق**.
- Windows CI دقیق همان SHA: [run 42117361615](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42117361615)، `completed / success`، **۱۴/۱۴ job موفق**. در مجموع این دو run، **۲۳/۲۳ job** موفق‌اند.
- `git diff --check` پس از اصلاح pinها موفق بود. پس از انتشار این گزارش، نتیجهٔ CI برای commit گزارش‌محور احتمالی جدا از qualification دقیق `279449e...` گزارش خواهد شد؛ totals و artifactهای اینجا به SHA دیگری نسبت داده نمی‌شوند.

## ۳. کار مهندسی و ممیزی

### ۳.۱ چرخهٔ درس و supersession منبع

- وقتی نسخهٔ تازهٔ سند، درس مشتق‌شدهٔ قبلی را supersede می‌کند، predecessor به `SUPERSEDED` می‌رود و `prior_status` وضعیت lifecycle قبلی را حفظ می‌کند؛ صرفاً `is_current=False` کردن برای اعتبار زنجیره کافی نیست.
- successor یک lesson تازه در `DRAFT` است؛ `approved_by` یا `approved_at` predecessor را به ارث نمی‌برد. تغییر منبع، approval قدیمی را approval جدید نمی‌کند.
- regressionهای predecessor با وضعیت‌های قبلی متفاوت و successor فاقد metadata تأیید موروثی افزوده/حفظ شدند. full-suiteهای دقیق `279449e...` سبزند و focused suite محلی نیز جداگانه اجرا شد.
- پیاده‌سازی در `src/drilling_intelligence/operations/promote.py`، قرارداد زنجیره در `src/drilling_intelligence/lessons/repository.py` و regressionها در `tests/integration/test_lesson_promotion.py` هستند.

### ۳.۲ یکتایی کد درس و مسیرهای نوشتن

- کد درس به‌صورت global، پس از strip و Unicode casefold، یکتا است؛ یکتایی per-well نیست.
- repository مسیرهای ایجاد، ویرایش و ساخت revision را پیش از write بررسی می‌کند؛ partial unique index روی کلید canonical و current rows لایهٔ نهایی دیتابیس است. مسیر promotion duplicate جاری را به `CONFLICT` تبدیل می‌کند؛ promotion تکراری همان identity idempotent است.
- کد predecessor تاریخی با successor مجاز است؛ تاریخچهٔ زنجیره حفظ می‌شود و unique rule روی lesson جاری اعمال می‌شود. قید پایگاه‌داده در برابر race بین دو writer نیز باقی است.
- migrationهای مرتبط `0020` تا `0023` linkage/identity، کد casefoldشده، canonical key و lifecycle قبلی را پوشش می‌دهند؛ head schema `0026` است.
- اجرای متمرکز محلی روی SHA `279449e...` شامل پنج فایل lesson integration، `tests/unit/test_exception_policy.py` و `tests/unit/test_constraints.py` برابر **۱۲۰ passed** بود. فرمان:
  `python -m pytest -q tests/integration/test_engineering_lessons.py tests/integration/test_lesson_code_key.py tests/integration/test_lesson_deletion.py tests/integration/test_lesson_lifecycle_forensic.py tests/integration/test_lesson_promotion.py tests/unit/test_exception_policy.py tests/unit/test_constraints.py`.
- حذف lesson با دادهٔ آزمایشی در `TemporaryDirectory` بررسی شد: predecessor حذف‌شده `SUPERSEDED`، `is_current=False`، `prior_status=DRAFT` و بدون successor می‌شود؛ validator زنجیره با `chain has no current or withdrawn disposition; destructive action refused` عملیات زنجیره‌ای بعدی را fail-closed می‌کند.
- **شکاف owner-gated:** حذف منبع یا تغییر `code` در منبع هنوز disposition/linking کامل و مصوبی برای lesson جاری ندارد. تصمیم حذف، rename، انتقال هویت یا حفظ/withdrawal درس ساخته نشده است؛ پرسش در `PRODUCT_DECISIONS_REQUIRED.md` و O-03 در inventory قابلیت ثبت شده است.

### ۳.۳ علت‌یابی Windows UI و Intake / Review

- failure تاریخی هدف `test_intake_review_registration_and_rejection_run_as_distinct_ui_transitions` در Windows offscreen بود. annotation آن فقط می‌گفت registration و pending refresh به پایان نرسیده‌اند؛ علت ریشه‌ای از آن قابل اثبات نبود.
- assertion timeout در `tests/ui/test_intake_workbench.py` اکنون `lifecycle_snapshot()` را در diagnostic درج می‌کند تا اگر failure بازتولید شد وضعیت lifecycle در log معلوم شود. این instrumentation علت failure تاریخی را ثابت نمی‌کند.
- در Windows run دقیق `279449e...`، full-suiteهای CPython 3.11 و 3.14 هرکدام **2,776 collected، 2,713 passed، 63 skipped، 0 failed، 0 errors** شدند. UIهای Qt offscreen هرکدام **29/29 passed، صفر skip/failure/error** بودند؛ از جمله regression هدف که suite آن را اجرا می‌کند.
- stability suiteهای Windows برای history، intake و presentation در هر دو runtime و run-card behavioral tests زیر PowerShell 5.1 و 7 موفق شدند. آزمون‌های run-card رفتاری‌اند؛ run-card موجود همچنان **۱۶ مرحله‌ای** است.
- failure تاریخی روی SHA `ff4afdd...` در این runها **بازتولید نشد**؛ root cause آن همچنان **نامعلوم/اثبات‌نشده** است. assertion تضعیف، skip/xfail، retry کورکورانه، sleep تصادفی یا broad exception suppression برای سبزکردن UI به‌کار نرفت.
- نتیجه فقط Qt-enabled **offscreen CI** است؛ **physical Windows display acceptance = NOT RUN**. رفتار PowerShell در CI، اجرای workstation واقعی محسوب نمی‌شود.

### ۳.۴ SQLite، migration و نصب واقعی بسته

- `/tmp/prog-proc-mission-venv/bin/python tools/release_smoke.py` روی SHA `279449e...` با exit 0 تمام شد: wheel و sdist ساخته شدند، هرکدام در venv پاک و بیرون checkout نصب شدند، سپس CLI نصب‌شده اجرا شد.
- smoke مسیر import از `site-packages` بدون source-checkout leakage، console scripts، `drillintel --help/--version`، ساخت workspace، metadata schema bootstrap بدون Alembic scripts داخل package، doctor، records/search/timeline، decision/compare/analyze/report و خروجی HTML/SVG را اجرا کرد. bootstrap head `0026` و ۵۲ جدول را ایجاد کرد؛ well با نام غیر ASCII و مسیر دارای فاصله round-trip شد. wheel و sdist هیچ DBی را حمل نمی‌کنند؛ wheel migration scripts را نیز حمل نمی‌کند، بنابراین metadata bootstrap مسیر نصب واقعی است.
- full-suite CI دقیق `279449e...` نیز clean-install smoke را در Linux برای CPython 3.11 و 3.14، در compatibility job اوبونتو و در Windows برای هر دو runtime با موفقیت اجرا کرد.
- SQLite تنها backend دارای qualification است. PostgreSQL **NOT QUALIFIED** است؛ از تست SQLite نتیجه‌ای برای PostgreSQL استنتاج نمی‌شود. smoke، installer Windows، رمزگذاری archive، recovery از منبع خارجی یا DR production را qualify نمی‌کند.

### ۳.۵ Constraints، exception، static و کیفیت آزمون

روی SHA دقیق `279449e...` شواهد محلی:

- drift در resolver metadata پیدا شد: metadata نسخهٔ `ruff==0.17.4` را گزارش می‌کرد، ولی `constraints/development.txt` و `constraints/ui.txt` هنوز `0.17.3` داشتند. generator رسمی `tools/constraints.py --set development` و `--set ui` اجرا شد؛ فقط pin Ruff و تاریخ تولید فایل‌ها تغییر کرد.
- هر پنج constraint set (`runtime`, `development`, `ui`, `build`, `audit`) با `tools/constraints.py --set <name> --check` up-to-date شدند؛ `tests/unit/test_constraints.py` در اجرای متمرکز موفق بود.
- Ruff `0.17.4`: `ruff check .` موفق؛ `ruff format --check .` گزارش کرد **433 files already formatted**. `git diff --check` موفق بود.
- `tools/exception_review.py --check`: **105 broad handlers** مستند و reviewed؛ `docs/exception_inventory.md` با generator رسمی بازسازی شده است. `tests/unit/test_exception_policy.py` در اجرای متمرکز موفق بود. broad `pytest.raises(Exception/BaseException)` در test tree به خطاهای domain/DB مشخص محدود شده بود.
- full-suiteهای Linux و Windows، type-check boundary، dependency audit، Ruff، byte-compile و Alembic single-head checks را طبق workflow اجرا کردند و jobهای مربوطه سبز شدند. در matrix، برخی gateهای عمدیِ فقط یک‌بار اجراشونده در runtime دیگر skipped هستند؛ آن skipهای مرحله‌ای failure نیستند و به‌جای totals آزمون گزارش نشده‌اند.
- Linux CPython 3.11 full-suite coverage: **81.82% line / 70.38% branch**. Linux Qt/UI package coverage در Qt-enabled workbench: **76.43% / 54.21%**، بالاتر از gate 70/40. جدای آن، headless-only UI package coverage در همان run **26.51% / 12.91%** بود و با Qt-enabled gate اشتباه نمی‌شود.
- red/green regression و coverage gate، mutation-testing framework نیستند. mutation engine/config این checkout در این مأموریت تأیید نشد؛ mutation score یا mutation qualification **ادعا نمی‌شود**.

## ۴. CI دقیق SHA `279449e...`: totals و checks

هر پنج full-suite lane دقیقاً `279449e611b7645d48c0796b6792a0b946d7542c` را checkout کردند. totals از annotation همان check-run گرفته شده‌اند؛ `skipped` جداست و هیچ laneای failure/error نداشت.

| پلتفرم / Python | Job/check-run | Collected | Passed | Skipped | Failed / errors | xfailed / xpassed | Runtime (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Linux / CPython 3.11 | `127560562223` | 2,776 | 2,715 | 61 | 0 / 0 | 0 / 0 | 2,798.253 |
| Linux / CPython 3.14 | `127560562023` | 2,776 | 2,715 | 61 | 0 / 0 | 0 / 0 | 1,824.980 |
| Ubuntu 26.04 / CPython 3.14 | `127560561501` | 2,776 | 2,775 | 1 | 0 / 0 | 0 / 0 | 1,909.485 |
| Windows / CPython 3.11 | `127561950739` | 2,776 | 2,713 | 63 | 0 / 0 | 0 / 0 | 3,202.802 |
| Windows / CPython 3.14 | `127561950828` | 2,776 | 2,713 | 63 | 0 / 0 | 0 / 0 | 3,017.423 |

این‌ها per-lane شمارش‌اند؛ جمعشان تعداد اجرای test-case است، نه تعداد موارد یکتای محصول. در هر پنج lane `exit=0` و test failure/error صفر است.

جزئیات همان دو workflow و همان SHA:

- Linux run `42117361649`: **۹/۹ job success**؛ full-suiteهای Linux 3.11، Linux 3.14 و Ubuntu 26.04 3.14؛ compatibility، Qt workbench دو runtime و clean-install دو runtime؛ attribution report موفق. Checkهای full-suite CPython 3.11 شامل Ruff، type boundary، dependency/pin audit، byte-compile و Alembic single-head بودند. annotationهای هر سه full-suite totals را ثبت کردند.
- Windows run `42117361615`: **۱۴/۱۴ job success**؛ full-suiteهای 3.11 و 3.14، UI offscreen دو runtime، stability برای history/intake/presentation در دو runtime، behavioral run-card، clean-install دو runtime و attribution report. CPython 3.11 gateهای Ruff/type/dependency/byte-compile/Alembic و CPython 3.14 gateهای تنظیم‌شدهٔ خود را با موفقیت گذراندند.
- Windows offscreen UI checkها `127561950835` (3.11) و `127561950487` (3.14): هرکدام **29/29 passed، صفر skip/failure/error**.
- clean-install jobها در هر دو OS و runtime موفق‌اند؛ Ubuntu compatibility نیز clean-install smoke را با موفقیت اجرا کرد. اجرای محلی release smoke مستقل از این jobها نیز سبز است.
- coverage: Linux CPython 3.11 full-suite **81.82% / 70.38%**؛ Qt-enabled UI coverage check `127560561887` برابر **76.43% / 54.21%** و gate 70/40 موفق؛ headless-only UI coverage **26.51% / 12.91%** گزارش شده و Qt gate محسوب نمی‌شود.
- PowerShell run-card behavioral check `127561950674` موفق شد. **پذیرش فیزیکی نمایشگر Windows اجرا نشده است.**

### ۴.۱ شکست‌های تاریخی، هرکدام محدود به SHA خود

- SHA `a08f1e510fc62f468e986d2efd13768e17d4605d`, Linux run `41936249541`, CPython 3.14: **2,776 collected، 2,714 passed، 61 skipped، 1 failed**. failure فقط `test_the_inventory_on_disk_matches_the_tree` بود؛ inventory پس از تغییر line numberهای `promote.py` تازه نشده بود. generator رسمی در `b3163bf...` inventory را بازساخت و test بعدی سبز شد. این failure به regression lesson یا Windows نسبت داده نمی‌شود.
- SHA `94d698ffcb2c5e5c2a36ac164dba222e17d65cc1`: پنج full-suite lane دارای دو test-contract failure بودند؛ inventory stale و عبارت CLI مبهم. اصلاح آن‌ها در SHA `ff4afdd...` ثبت شد.
- SHA `ff4afddba0d5f319b0f43341dcaed13991b4dd3d`, Windows run `41841625041`, check `126614417177`: پنج full-suite lane موفق، اما Windows CPython 3.11 offscreen UI **28 passed / 1 failed / 0 skipped**؛ failure همان test registration/rejection بود و annotation گفت registration و pending refresh تمام نشدند. root cause از annotation اثبات نشد.
- SHA `b3163bf...`: در UI موردنظر Windows 29/29 در هر دو runtime اجرا شد، اما عدم بازتولید علت failure تاریخی را اثبات نکرد.
- SHA مستنداتی `6df276d680fab049643f48e26610ee9f37ce378c`, Linux run `42116701063`, job `127558127187`: resolver drift check به علت تفاوت metadata Ruff `0.17.4` با pinهای constraints برابر `0.17.3` شکست خورد؛ **pytest در آن job شروع نشد**. pinها با generator رسمی در `279449e...` اصلاح شدند؛ full CI دقیق `279449e...` موفق است. این dependency-pin failure هیچ ربط اثبات‌شده‌ای به failure تاریخی Windows ندارد.
- Windows run `42116701054` روی `6df276d...` پس از commit بعدی لغو شد؛ از آن نتیجه‌ای به `279449e...` نسبت داده نشده است.

## ۵. Intake / Review و semantics محصول

- Intake preview، human review، registration و domain promotion از هم جدا هستند: preview خود domain writer نیست؛ registration برای bytes مطابق hash بررسی‌شده registry evidence و audit می‌سازد، اما promotion انجام نمی‌دهد. Registration و rejection باید transitionهای متمایز باقی بمانند.
- Registration برای stale/unreviewed content، idempotency، audit/FK، persistence و نمایش `REGISTERED` از registration record در suiteهای پروژه آزموده می‌شود. regression هدف registration/rejection در Windows offscreen full UI suite دو runtime اجرا شد؛ هیچ assertion برای سبز کردن run حذف یا شل نشد.
- این شواهد دسترسی انسانی، actor authentication یا استفادهٔ چندکاربره را ثابت نمی‌کنند.

## ۶. موارد باز، محدودیت‌ها و تصمیم‌های لازم

### مالک/محصول

- Milestoneهای **M2–M5** همچنان **OWNER-GATED** هستند؛ PASS CI به‌معنی تصویب/پیاده‌سازی آن‌ها نیست.
- برداشت/تغییر `lesson.code` در منبع و disposition درس موجود هنگام حذف/rename هنوز policy ندارد. تا تصمیم مالک هیچ رفتار خودکار جدیدی اضافه نمی‌شود.
- PostgreSQL، multi-user/authentication، Skills runtime، provider maturity، vector search، method/calculationهای دیگر، trajectory/survey derivation، risk scoring، currency conversion، AFE و plan-vs-actual خارج از تصمیم‌های مصوب و دروازه‌دار می‌مانند.

### پذیرش و عملیات

- **Physical Windows display acceptance: NOT RUN**؛ offscreen Qt معادل نمایشگر واقعی نیست. Run-card ۱۶ مرحله‌ای حفظ شده؛ behavioral tests آن در PowerShell 5.1 و 7 موفق‌اند اما کارت workstation فیزیکی اجرا نشده است.
- Qt محلی در host حاضر به‌علت فقدان `libGL.so.1` قابل load نبود؛ local Qt در این محیط **SKIPPED/BLOCKED** است، نه PASS. شواهد Qt-enabled به runnerهای CI مربوط‌اند.
- archive encryption، external-source backup، recovery exercise/DR واقعی، signed installer، production rollout و deployment qualification انجام نشده‌اند.
- `workspace backup/restore` دیتابیس‌محور است و باید database snapshot نامیده شود. `workspace archive/restore-archive` مسیر جداگانهٔ فایل‌های واجدشرایط زیر workspace همراه SQLite snapshot است؛ منابع بیرونی کپی نمی‌شوند و archive رمزگذاری نشده است. هیچ‌کدام backup سازمانی/DR کامل نیستند.
- mutation-testing engine وجود/اجرا اثبات نشد؛ coverage یا red/green regression معادل mutation score نیست.

## ۷. Defect / acceptance register

| شناسه | مورد | وضعیت | اقدام/شرط بستن |
|---|---|---|---|
| D-01 | stale exception inventory روی Linux CPython 3.14 | **FIXED; historical exact-SHA CI PASS** | generator رسمی اجرا؛ suite بعدی روی SHAهای مربوط موفق شد. |
| D-02 | lesson predecessor در supersession به disposition معتبر منتقل نشود | **FIXED; regression + exact-SHA CI PASS** | predecessor `SUPERSEDED` با `prior_status`؛ successor تازه `DRAFT` و بدون approval metadata موروثی. |
| D-03 | Windows Intake registration/refresh transition failure تاریخی | **NOT REPRODUCED; root cause UNKNOWN** | diagnostic snapshot افزوده و ۲۹/۲۹ در دو runtime؛ برای بستن علت، failure باید با evidence کافی بازتولید/علت‌یابی شود. |
| D-04 | removal یا تغییر کد منبع در زنجیرهٔ lesson | **OWNER-GATED / OPEN** | تصمیم مالک دربارهٔ هویت/disposition/linking لازم است؛ policy جدید خودکار ساخته نمی‌شود. |
| Q-01 | پذیرش فیزیکی Windows display | **NOT RUN** | اجرای run-card ۱۶ مرحله‌ای روی workstation واقعی و ثبت artifact انسانی. |
| Q-02 | PostgreSQL، multi-user/auth، production DR/release | **NOT QUALIFIED / OUT OF SCOPE UNTIL OWNER DECISION** | قرارداد، زیرساخت و پذیرش مستقل لازم است؛ از SQLite/offscreen نتیجه‌گیری نشود. |
| Q-03 | mutation score | **NOT RUN / NOT AVAILABLE** | engine/config مصوب و اجرای mutant واقعی لازم است؛ coverage جایگزین نیست. |

## ۸. Artifactهای CI دقیقاً متعلق به SHA `279449e...`

Metadata از GitHub Actions API برای runهای [Linux `42117361649`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42117361649) و [Windows `42117361615`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42117361615) خوانده شد. Digest، SHA-256 آرشیو artifact GitHub است، نه checksum محتویات داخلی. در این مرحله ID/digest/size metadata ثبت شده؛ دانلود ZIP artifact ادعا نمی‌شود.

| Run | Artifact | Artifact ID | Size (bytes) | SHA-256 digest |
|---|---|---:|---:|---|
| Linux | `pytest-matrix-report-ubuntu-2604-cp3.14-a1` | `13460750087` | 1,343 | `ee7536dffc62c4d63b59c1fefaf77e8ee28e380d8c0d3bf28b9618be7c582c44` |
| Linux | `pytest-matrix-report-linux-a1` | `13460545360` | 1,485 | `d86cc57a7f942f7211a57af5458881238d3af8e71f20658387ab1b8ef3a0b841` |
| Linux | `pytest-cp3.14-a1` | `13460180761` | 64,689 | `9d99165bc9a9609dcc7024b3280e8baf8d4cfe49783c3ec1a324c9cc9cf72503` |
| Linux | `pytest-cp3.11-a1` | `13459748051` | 75,053 | `35c8473cea6d0df4829e51d57dae04c9c8918df3327fb9d20c33e4973e4ba7b0` |
| Linux | `coverage-cp3.11` | `13459663203` | 103,407 | `1784a7276bfe9943340a7a43686ca1ac391e98e48e5d72f6d586b17e3a34df47` |
| Linux | `pytest-ubuntu-2604-cp3.14-a1` | `13459616545` | 64,346 | `daa78b9801ed779254fa846cd1240dd3d093bcf9ee14254e6f55cfd96f1b3b7e` |
| Linux | `qt-workbench-logs-cp3.14` | `13458085314` | 120,407 | `e7b46d1f4eaff231881453bed16db32ac4d3d369580f3dbe6ae5d315c2300f4c` |
| Linux | `qt-ubuntu-2604-logs` | `13458035424` | 104,885 | `664a00d5ccd3b4397d782f7d5819c18bb788f12ba7ad8e8b03d9f2b2dcf8ec23` |
| Linux | `coverage-ui-cp3.11` | `13456983103` | 131,324 | `b615e7d4b75cfc550aad9a7fa6c6cef687f02cc898037716b5262a19cc9f33cd` |
| Linux | `qt-workbench-logs-cp3.11` | `13456073414` | 120,522 | `42eae2cd13a8096af40901424e885beafb468268d3e2dfac8c8da88f66b83023` |
| Windows | `pytest-windows-cp3.11-a1` | `13461250571` | 65,997 | `789689f617686f1576645ff8449ddbe2114f8baafc8348278b0653e1091a41a2` |
| Windows | `pytest-matrix-report-windows-a1` | `13460327538` | 1,526 | `8046075c21a4020e57b340868a49a7e8e5831926d480d2e5bc6a67ed315eb612` |
| Windows | `pytest-windows-cp3.14-a1` | `13459843833` | 65,830 | `e590e0432ebd25f929dc9d369753ff1f786b081702082fa444660ad3d29311b0` |
| Windows | `qt-windows-stability-logs-cp3.14-presentation` | `13457970606` | 19,941 | `8cd33c6ea37e344f95224f2f7146cc62f4d1e281f751582530da0d25f40637f1` |
| Windows | `qt-windows-ui-logs-cp3.11` | `13457612535` | 37,535 | `c95a933156110aca3661a7471328c51c165f13cc6045b4e5c3abdb2439272f2f` |
| Windows | `qt-windows-stability-logs-cp3.14-history` | `13457412849` | 20,508 | `10d36e0995d96d8e881d56b0de3d5a2beeb442170a893353fa26e20fca9daf58` |
| Windows | `qt-windows-ui-logs-cp3.14` | `13457142631` | 37,725 | `27a5dab770c3a25750b6da811b776e21a158a77f6061b2fda88cdba64a9e4b4d` |
| Windows | `qt-windows-stability-logs-cp3.11-history` | `13456998149` | 20,526 | `ef969e523f3afc508917734bb29c5225cc6177da863d9f71474b8f27220659ef` |
| Windows | `windows-run-card-powershell-logs` | `13456097880` | 15,719 | `1dcf544d065ea2d188c3915937402ee3c29ed60a3164f84b8431a2c8ca0c943d` |
| Windows | `qt-windows-stability-logs-cp3.11-intake` | `13456084007` | 16,600 | `886950f158fb37e15a1a8773920fce4d5415235daa1840b740f36a12a58e050f` |
| Windows | `qt-windows-stability-logs-cp3.11-presentation` | `13456018092` | 19,901 | `a293ef26d06b9e82fe0b5050d2b57e973247da060280bdc0692fd357783a28cf` |
| Windows | `qt-windows-stability-logs-cp3.14-intake` | `13455693454` | 16,522 | `aba93fdefb6f558a3224a5b4c67bb4ee17af9a7f2e26e9fa3ad6ee850d7f21c6` |

## ۹. نتیجه و اقدام بعدی

**نتیجه:** اصلاحات مهندسی مستقل این موج و refresh اصلاحی constraints روی SHA `279449e...` با release smoke محلی، اجرای متمرکز **۱۲۰ passed**، و Linux/Ubuntu/Windows CI کامل **۲۳/۲۳ job success** qualification شدند. محصول همچنان **PARTIALLY QUALIFIED — PRE-ALPHA** و انتشار production/public **NOT READY** است. failure تاریخی Windows بازتولید نشد ولی علتش اثبات نشده؛ physical Windows display acceptance و همهٔ موارد owner-gated باز می‌مانند.

**اقدامات لازم برای ادامهٔ qualification:**

1. مالک محصول دربارهٔ حذف/rename کد منبع در lesson revision تصمیم مکتوب بدهد؛ تا آن زمان رفتار جدیدی اضافه نشود.
2. اگر failure تاریخی UI دوباره رخ داد، diagnostic snapshot و artifact همان SHA حفظ و علت از evidence واقعی تعیین شود؛ retry/sleep/skip برای سبزکردن استفاده نشود.
3. run-card ۱۶ مرحله‌ای روی نمایشگر فیزیکی Windows اجرا و با artifact انسانی تکمیل شود؛ offscreen جایگزین آن نیست.
4. M2–M5 و هر scope اضافه فقط پس از تصمیم مالک و acceptance contract مستقل باز شود.
5. `out/performance.json` را در این مأموریت restore، stage یا commit نکنید. پیش از release ادعاشده، remote ref، source/test SHA، workflowها و artifactها را دوباره روی همان SHA بررسی کنید.

## مراجع

- README وضعیت محصول: [`../../README.md`](../../README.md)
- مرجع زمینهٔ پروژه: [`../PROJECT_CONTEXT.md`](../PROJECT_CONTEXT.md)
- inventory قابلیت‌ها: [`../PRODUCT_CAPABILITY.md`](../PRODUCT_CAPABILITY.md)
- تصمیم‌های نیازمند مالک: [`../PRODUCT_DECISIONS_REQUIRED.md`](../PRODUCT_DECISIONS_REQUIRED.md)
- قرارداد Windows: [`../WINDOWS_TESTING.md`](../WINDOWS_TESTING.md)
- Linux CI exact SHA `279449e...`: [run 42117361649](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42117361649)
- Windows CI exact SHA `279449e...`: [run 42117361615](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42117361615)
- Linux resolver-drift failure در `6df276d...`: [run 42116701063](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42116701063)
- Windows failure تاریخی روی `ff4afdd...`: [run 41841625041](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41841625041)
