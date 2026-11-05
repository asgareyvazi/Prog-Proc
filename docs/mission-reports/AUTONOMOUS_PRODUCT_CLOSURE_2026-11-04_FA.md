# گزارش نهایی مهندسی و qualification محصول Prog-Proc

- **تاریخ گزارش:** ۲۰۲۶-۱۱-۰۶ (Asia/Tehran)
- **شاخه:** `arena/5957b76d-prog-proc`
- **SHA کد/آزمون qualification‌شده:** `b3163bfef511fc93bd0fb57972f0cd672ab6c66c`
- **حکم محصول:** **PARTIALLY QUALIFIED — PRE-ALPHA**
- **حکم انتشار عمومی/production:** **NOT READY / NOT AUTHORIZED**

> این گزارش نتایج را به SHA و run مشخص محدود می‌کند. CI نهاییِ کد/آزمون روی `b3163bf...`، دو workflow موفق و ۲۳/۲۳ job موفق دارد. در این گزارش نتیجهٔ SHAهای پیشین فقط به‌عنوان سابقهٔ شکست/مقایسه آمده و با نتیجهٔ `b3163bf...` ادغام نشده است. پس از ثبت این گزارش، تغییرات مستندسازی جدا از کد/آزمون خواهند بود؛ هرگز ادعا نمی‌شود که runهای `b3163bf...` روی SHA مستنداتی بعدی اجرا شده‌اند.

## ۱. حکم نهایی به تفکیک معیار

| محور | نتیجه | آنچه نتیجه ثابت می‌کند / نمی‌کند |
|---|---|---|
| پیاده‌سازی | **PASS در محدودهٔ مستقل و مصوب مأموریت؛ موارد owner-gated باز** | اصلاح lifecycle درس در supersession منبع، یکتایی کد درس و آزمون‌های regression موجود است. قابلیت جدیدِ فاقد تصمیم مالک ساخته نشده است. این حکم، همهٔ نیازهای محصول آینده را پوشش نمی‌دهد. |
| آزمون‌ها و quality gates | **PASS روی SHA `b3163bf...`** | Linux، Ubuntu 26.04 و Windows full-suiteها، static checks و attribution موفق‌اند؛ totals دقیق در بخش ۵ آمده است. PASS به‌معنی حذف شمارش‌های SKIPPED نیست. |
| Windows خودکار | **PASS (offscreen/CI)؛ نمایشگر فیزیکی NOT RUN** | Windows UI دو runtime، stability suites، clean-install و run-card behavioral tests سبزند. این‌ها پذیرش دستی روی دسکتاپ/نمایشگر واقعی Windows نیستند. |
| Qualification محصول | **PARTIALLY QUALIFIED — PRE-ALPHA** | SQLite و مسیرهای مشخص local-first شواهد اجرایی دارند؛ PostgreSQL، production operations، چندکاربره و پذیرش فیزیکی Windows تأیید نشده‌اند. |
| آمادگی انتشار | **NOT READY / NOT AUTHORIZED برای production یا انتشار عمومی** | نصب wheel/sdist و CLI smoke سبز است؛ اما installer امضاشده، عملیات/DR کامل، رمزگذاری آرشیو، PostgreSQL، authentication، multi-user و پذیرش دستی Windows اثبات نشده‌اند. |

این حکم عمداً میان «کد/آزمون سبز» و «صلاحیت محصول/انتشار» تمایز می‌گذارد. سبز شدن CI، تصمیم مالک یا تأیید operational را جایگزین نمی‌کند.

## ۲. مبنای Git، حفاظت تغییرات و شواهد

- تمام کار روی شاخهٔ مجاز `arena/5957b76d-prog-proc` انجام شد؛ branch دیگر ساخته یا انتخاب نشد.
- remote branch پیش از این گزارش روی `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` تأیید شد؛ `main` همچنان `85122e1a98087db4495b820a9a0ffae9009b7819` است و تغییر نکرده است.
- checkout محلی ابتدا روی ancestor `317c55365319af61138000664293ae2d84e27a6e` بود، درحالی‌که remote روی `b3163bf...` قرار داشت. با مقایسهٔ blob همهٔ ۴۵۵ مسیر remote، تمام فایل‌های حاضر دقیقاً با remote برابر بودند به‌جز `out/performance.json` که عمداً غایب است. سپس فقط ref محلی به‌صورت fast-forward به همان remote SHA برده و index با همان tree همگام شد؛ هیچ فایل working tree بازیابی یا جابه‌جا نشد.
- وضعیت کار محلی مورد حفاظت: `D out/performance.json`، unstaged و خارج از commit. این deletion در سراسر مأموریت restore، stage یا commit نشده است. فایل در tree ریموت وجود دارد؛ deletion فقط تغییر محلی کاربر است.
- staging پیش از تهیهٔ این گزارش خالی بود. stashها دست‌نخورده ماندند. `main`، branchهای دیگر و commitهای remote بازنویسی نشدند.
- Runهای qualification دقیقاً به commit `b3163bf...` تعلق دارند: Linux [41940002906](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41940002906) و Windows [41940002911](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41940002911). هر دو `completed / success` شدند. در مجموع ۲۳ job (۹ Linux/Ubuntu و ۱۴ Windows، شامل گزارش‌های انتساب) موفق‌اند.
- اجرای محلی release smoke و focused regression نیز با checkout کد `b3163bf...` انجام شد؛ جزئیات در بخش‌های ۴ و ۶ آمده است.

## ۳. کار مهندسی و ممیزی انجام‌شده

### ۳.۱ چرخهٔ درس و supersession منبع

- وقتی نسخهٔ تازهٔ سند، درس مشتق‌شدهٔ قبلی را supersede می‌کند، predecessor اکنون `SUPERSEDED` می‌شود و `prior_status` مقدار lifecycle قبلی را نگه می‌دارد؛ صرفاً `is_current=False` کردن برای اعتبار زنجیره کافی نیست.
- successor یک lesson تازه در `DRAFT` است؛ `approved_by` یا `approved_at` predecessor را به ارث نمی‌برد. تغییر منبع، approval قدیمی را به approval جدید تبدیل نمی‌کند.
- regressionهای predecessor با وضعیت‌های قبلی متفاوت و successor فاقد metadata تأیید موروثی افزوده/حفظ شدند. full-suite دقیق SHA نهایی سبز است و focused lesson suite نیز محلی جداگانه اجرا شد.
- این تغییر در `src/drilling_intelligence/operations/promote.py`، قراردادهای زنجیره در `src/drilling_intelligence/lessons/repository.py` و `tests/integration/test_lesson_promotion.py` دیده می‌شود.

### ۳.۲ یکتایی کد درس و مسیرهای نوشتن

- کد درس به‌صورت global، پس از strip و Unicode casefold، یکتا است؛ یکتایی per-well نیست.
- repository مسیرهای ایجاد، ویرایش و ساخت revision را پیش از write بررسی می‌کند؛ partial unique index روی کلید canonical و current rows لایهٔ نهایی دیتابیس است. مسیر promotion نیز duplicate جاری را به `CONFLICT` تبدیل می‌کند؛ promotion تکراری همان identity را idempotent تأیید می‌کند.
- کد predecessor تاریخی با successor مجاز است؛ تاریخچهٔ زنجیره حفظ می‌شود و unique rule روی lesson جاری اعمال می‌شود. قید پایگاه‌داده در برابر race بین دو writer نیز باقی می‌ماند.
- migrationهای مرتبط `0020` تا `0023` linkage/identity، کد casefoldشده، canonical key و lifecycle قبلی را پوشش می‌دهند. head schema در این نسخه `0026` است.
- پنج فایل درس و سیاست exception با هم به‌صورت هدفمند اجرا شدند: **۹۵ passed**؛ فرمان دقیق:
  `pytest -q tests/integration/test_engineering_lessons.py tests/integration/test_lesson_code_key.py tests/integration/test_lesson_deletion.py tests/integration/test_lesson_lifecycle_forensic.py tests/integration/test_lesson_promotion.py tests/unit/test_exception_policy.py`.
- حذف lesson با دادهٔ آزمایشی در `TemporaryDirectory` بررسی شد: predecessor حذف‌شده به `SUPERSEDED`، `is_current=False`، `prior_status=DRAFT` و بدون successor می‌رسد؛ validator زنجیره با خطای `chain has no current or withdrawn disposition; destructive action refused` عملیات زنجیره‌ای بعدی را fail-closed می‌کند.
- **شکاف owner-gated:** حذف منبع یا تغییر `code` در منبع هنوز disposition/linking کامل و مصوبی برای lesson جاری ندارد. این وضعیت با uniqueness/supersession رفع نشده است. سیاست حذف، rename، انتقال هویت یا حفظ/withdrawal lesson ساخته نشد؛ تصمیم مالک لازم است. پرسش در `PRODUCT_DECISIONS_REQUIRED.md` ثبت می‌شود.

### ۳.۳ علت‌یابی Windows UI

- failure تاریخی هدف، `test_intake_review_registration_and_rejection_run_as_distinct_ui_transitions` در Windows offscreen بود. annotation تنها می‌گفت registration و refresh pending به پایان نرسیده‌اند؛ از آن، علت ریشه‌ای قابل اثبات نبود.
- assertion timeout در `tests/ui/test_intake_workbench.py` اکنون `lifecycle_snapshot()` را در diagnostic درج می‌کند تا اگر failure بازتولید شد، وضعیت lifecycle در log معلوم باشد. این instrumentation برای تشخیص است؛ خودش علت failure تاریخی را ثابت نمی‌کند.
- روی `b3163bf...`, Windows UI CPython 3.11، check `126958804124`: **29/29 passed، صفر skip/failure/error**؛ Windows UI CPython 3.14، check `126958803901`: **29/29 passed، صفر skip/failure/error**. Stability suites intake/history/presentation نیز برای دو runtime سبزند.
- بنابراین failure قبلی در این runها **بازتولید نشد**؛ root cause آن همچنان **نامعلوم/اثبات‌نشده** است. assertion تضعیف، skip/xfail، retry کورکورانه، sleep تصادفی یا broad exception suppression برای سبزکردن UI به‌کار نرفت.
- نتیجه فقط Qt-enabled **offscreen CI** است؛ **physical Windows display acceptance = NOT RUN**. Run-card رفتاری PowerShell 5.1 و 7 با check `126958803948` موفق شد؛ run-card موجود همچنان ۱۶ مرحله‌ای است و به‌عنوان اجرای workstation/desktop واقعی انجام نشده است.

### ۳.۴ SQLite، migration و بسته‌بندی واقعی

- `tools/release_smoke.py` روی SHA `b3163bf...` با `/tmp/prog-proc-audit-venv/bin/python` موفق شد (exit 0): wheel و sdist ساخته شدند، هر کدام در venv پاک و بیرون checkout نصب شدند، و CLI نصب‌شده واقعاً اجرا شد.
- هر دو artefact، import از `site-packages` بدون source-checkout leakage، console scripts، `drillintel --help/--version`, workspace creation، metadata schema bootstrap بدون Alembic scripts داخل package، doctor، records/search/timeline، decision/compare/analyze/report و HTML/SVG را گذراندند. bootstrap head `0026` و ۵۲ جدول را ایجاد کرد؛ well با نام غیر ASCII/مسیر دارای فاصله round-trip شد. wheel و sdist هیچ DBی را حمل نمی‌کنند؛ wheel migration scripts را نیز حمل نمی‌کند، پس مسیر metadata bootstrap همان مسیر واقعی نصب است.
- smoke به‌معنی qualify کردن PostgreSQL، installer Windows، archive encryption، recovery از منبع خارجی یا production DR نیست.
- full-suite CI روی همین SHA clean-install smoke را برای Linux و Windows، CPython 3.11 و 3.14 اجرا کرد؛ هر چهار job سبز هستند.
- SQLite تنها backend دارای qualification است. PostgreSQL **NOT QUALIFIED** است؛ از تست SQLite نتیجه‌ای برای PostgreSQL استنتاج نمی‌شود.

### ۳.۵ exception، static و تست‌کیفیت

روی `b3163bf...` شواهد محلی ثبت‌شده:

- Ruff lint: موفق؛ Ruff format: **۴۳۳ فایل already formatted**.
- mypy: **۴۴ source files** بدون خطا؛ `compileall` موفق.
- Alembic: یک head، `0026`; `constraints/audit.txt` up-to-date.
- `tools/exception_review.py --check`: **۱۰۵ broad handlers**، همه مستند و reviewed. `docs/exception_inventory.md` با generator رسمی بازسازی شد. `tests/unit/test_exception_policy.py`: **9 passed**.
- broad `pytest.raises(Exception/BaseException)` در tests به خطاهای domain/DB مشخص محدود شد؛ جست‌وجوی test tree پس از اصلاح موردی نیافت.
- CI exact SHA نیز Ruff lint/format، type boundary، resolve pins، dependency audit، byte-compile، Alembic single-head و test totals/artifact identity را سبز کرد. Linux annotation: dependency audit «no known vulnerabilities» و datetime-adapter warning count صفر.
- پس از به‌روزرسانی اسناد qualification، `tests/unit/test_product_decisions.py`, `tests/unit/test_windows_acceptance_doc.py`, و `tests/unit/test_exception_policy.py` محلی **25 passed** شدند؛ `git diff --check` نیز موفق بود. این focused نتیجه، CI یک SHA مستنداتی نیست.
- coverage ثبت‌شده در Linux CPython 3.11 full-suite: **81.83% line / 70.37% branch**؛ Qt/UI package در job UI: **76.43% / 54.21%** (از حداقل gate 70/40 بالاتر). اجرای headless-only UI package جداگانه 26.51%/12.91% گزارش شده و با coverage gate Qt واقعی اشتباه نمی‌شود.
- red/green regressionها و coverage gate، mutation-testing framework نیستند. mutation engine/config این checkout در این مأموریت تأیید نشد؛ بنابراین mutation score یا mutation qualification **ادعا نمی‌شود**.

## ۴. CI exact-SHA و totals آزمون

هر پنج full-suite lane دقیقاً head SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` را checkout کردند. totals از annotation همان check-run گرفته شده‌اند؛ skipها جداگانه باقی می‌مانند. هر lane `failed=0`, `errors=0`, `xfailed=0`, `xpassed=0`, `exit=0` دارد.

| پلتفرم / Python | Check-run | Collected | Passed | Skipped | Failed / errors | Runtime (s) |
|---|---:|---:|---:|---:|---:|---:|
| Linux / CPython 3.11 | `126958381847` | 2,776 | 2,715 | 61 | 0 / 0 | 3,585.005 |
| Linux / CPython 3.14 | `126958381807` | 2,776 | 2,715 | 61 | 0 / 0 | 1,444.794 |
| Ubuntu 26.04 / CPython 3.14 | `126958381778` | 2,776 | 2,775 | 1 | 0 / 0 | 1,427.792 |
| Windows / CPython 3.11 | `126958803874` | 2,776 | 2,713 | 63 | 0 / 0 | 3,585.005 |
| Windows / CPython 3.14 | `126958803945` | 2,776 | 2,713 | 63 | 0 / 0 | 3,592.503 |

جزئیات تکمیلی همین runها:

- Linux run `41940002906`: ۹/۹ job موفق؛ Windows run `41940002911`: ۱۴/۱۴ job موفق. attribution reportهای Linux/Ubuntu `126979874620` و Windows `126984025654` هر دو موفق‌اند.
- Windows Qt offscreen test checkها `126958804124` و `126958803901`: هر کدام 29/29، صفر skip/failure/error. Linux workbench Qt-enabled checkها `126958381198` و `126958381691` نیز موفق‌اند.
- Clean install smoke checks: Linux `126958381845` (3.11)، `126958381599` (3.14)؛ Windows `126958803987` (3.11)، `126958803635` (3.14) — همگی موفق.
- PowerShell run-card check `126958803948` موفق است. Checkهای stability ویندوز برای history، intake و presentation در 3.11/3.14 همگی موفق‌اند.
- یک skip در Ubuntu و ۶۱/۶۳ skip در suiteهای دیگر به‌عنوان skipped ثبت شده‌اند؛ این‌ها PASS تلقی نمی‌شوند. Qt-enabled jobها تست‌هایشان را واقعاً اجرا کردند و Qt-unavailable skip audit آن‌ها موفق بود.

### ۴.۱ سابقهٔ شکست‌ها، هرکدام محدود به SHA خودش

- SHA `a08f1e510fc62f468e986d2efd13768e17d4605d`, Linux run `41936249541`, CPython 3.14: **2,776 collected, 2,714 passed, 61 skipped, 1 failed**. failure فقط `test_the_inventory_on_disk_matches_the_tree` بود؛ inventory بعد از تغییر line numberهای `promote.py` تازه نشده بود. generator رسمی inventory را در `b3163bf...` بازساخت؛ تست محلی policy 9/9 و CI جدید suite سبز شد. این failure به regression lesson یا Windows نسبت داده نمی‌شود.
- SHA `94d698ffcb2c5e5c2a36ac164dba222e17d65cc1`: هر پنج full-suite lane دو test-contract failure داشتند؛ inventory stale و عبارت CLI مبهم. اصلاح آن‌ها در SHA `ff4afddba0d5f319b0f43341dcaed13991b4dd3d` ثبت شد. این دو مورد با failure UI بعدی یکی نیستند.
- SHA `ff4afdd...`, Windows run `41841625041`, check `126614417177`: پنج full-suite lane موفق، اما Windows CPython 3.11 offscreen UI **28 passed / 1 failed / 0 skipped**؛ failure همان test registration/rejection UI بود و annotation «registration and pending refresh did not finish» را ثبت کرد. root cause از annotation اثبات نشد.
- SHA `b3163bf...`: هر پنج full-suite lane و دو workflow کلی موفق‌اند؛ Windows UI 29/29 در هر دو runtime است. این عدم بازتولید، علت failure در `ff4afdd...` را اثبات نمی‌کند و نتایج SHAها باهم جمع یا جابه‌جا نشده‌اند.

## ۵. Intake / Review و آزمون محصول

- Intake preview، human review، registration و domain promotion از هم جدا هستند: preview خود domain writer نیست؛ registration برای bytes مطابق hash بررسی‌شده، registry evidence و audit می‌سازد، اما promotion انجام نمی‌دهد. Registration و rejection باید transitionهای متمایز بمانند.
- آزمون UI هدفمند روی SHA نهایی دو بار در Windows (CPython 3.11 و 3.14) سبز شد؛ full-suiteهای Linux/Windows و migration/registration tests نیز سبز هستند.
- stale/unreviewed registration، idempotency، audit/FK، persistence و نمایش `REGISTERED` از registration record در suiteهای پروژه آزموده می‌شوند؛ هیچ assertion برای سبز کردن run حذف یا شل نشد.
- این شواهد، دسترسی انسانی، actor authentication یا استفادهٔ چندکاربره را ثابت نمی‌کنند.

## ۶. Open items، محدودیت‌ها و موارد نیازمند مالک

### مالک/محصول

- Milestoneهای **M2–M5** همچنان **OWNER-GATED** هستند؛ PASS CI آن‌ها را تصویب یا پیاده‌سازی نمی‌کند.
- برداشت/تغییر `lesson.code` در منبع و disposition درس موجود در زمان حذف/rename هنوز policy ندارد. تصمیم مالک باید مشخص کند هویت و زنجیره چگونه حفظ یا بسته می‌شوند؛ تا آن زمان هیچ رفتار خودکار جدیدی اختراع نشده است.
- PostgreSQL، multi-user/authentication، Skills runtime، provider maturity، vector search، method/calculationهای دیگر، trajectory/survey derivation، risk scoring، currency conversion، AFE و plan-vs-actual خارج از تصمیم‌های مصوب و دروازه‌دار می‌مانند.

### پذیرش و عملیات

- **Physical Windows display acceptance: NOT RUN**؛ offscreen Qt معادل نمایشگر واقعی نیست. Run-card ۱۶ مرحله‌ای حفظ شده؛ behavioral tests آن در PowerShell 5.1 و 7 موفق‌اند اما کارت workstation فیزیکی اجرا نشده است.
- Qt محلی در host حاضر به‌علت فقدان `libGL.so.1` قابل load نبود؛ local Qt در این محیط **SKIPPED/BLOCKED** است، نه PASS. شواهد Qt-enabled به runnerهای CI مربوط‌اند.
- archive encryption، external-source backup، recovery exercise/DR واقعی، signed installer، production rollout و deployment qualification انجام نشده‌اند.
- `workspace backup/restore` دیتابیس‌محور است و باید database snapshot نامیده شود. `workspace archive/restore-archive` مسیر جداگانهٔ فایل‌های واجدشرایط زیر workspace همراه SQLite snapshot است؛ منابع بیرونی کپی نمی‌شوند و archive رمزگذاری نشده است. هیچ‌کدام ادعای backup سازمانی/DR کامل نیستند.
- mutation-testing engine وجود/اجرا اثبات نشد؛ coverage یا red/green regression معادل mutation score نیست.

## ۷. Defect / acceptance register نهایی

| شناسه | مورد | وضعیت | اقدام/شرط بستن |
|---|---|---|---|
| D-01 | stale exception inventory روی Linux CPython 3.14 | **FIXED; exact-SHA CI PASS** | generator رسمی اجرا؛ `b3163bf...` Linux suite موفق. |
| D-02 | lesson predecessor در supersession به disposition معتبر منتقل نشود | **FIXED; regression + exact-SHA CI PASS** | predecessor `SUPERSEDED` با `prior_status`؛ successor تازه `DRAFT` بدون approval metadata موروثی. |
| D-03 | Windows Intake registration/refresh transition failure تاریخی | **NOT REPRODUCED; root cause UNKNOWN** | diagnostics snapshot اضافه و دو Windows offscreen suite 29/29 شدند؛ برای بستن علت، failure باید با evidence کافی بازتولید/علت‌یابی شود. |
| D-04 | removal یا تغییر کد منبع در زنجیرهٔ lesson | **OWNER-GATED / OPEN** | تصمیم مالک دربارهٔ هویت/disposition/linking؛ policy جدید به‌طور خودکار ساخته نمی‌شود. |
| Q-01 | پذیرش فیزیکی Windows display | **NOT RUN** | اجرای ۱۶ مرحلهٔ run-card روی workstation واقعی و ثبت artifact انسانی. |
| Q-02 | PostgreSQL، multi-user/auth، production DR/release | **NOT QUALIFIED / OUT OF SCOPE UNTIL OWNER DECISION** | قرارداد، زیرساخت و پذیرش مستقل لازم است؛ از SQLite/offscreen نتیجه‌گیری نشود. |
| Q-03 | mutation score | **NOT RUN / NOT AVAILABLE** | engine/config مصوب و اجرای mutant واقعی لازم است؛ coverage جایگزین نیست. |

## ۸. Artifactهای CI دقیقاً متعلق به SHA `b3163bf...`

Artifact listهای زیر از runهای [41940002906](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41940002906) و [41940002911](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41940002911) گرفته شده‌اند. Digest، SHA-256 آرشیو artifact GitHub است، نه checksum محتویات. دانلود ZIP از محیط محلی به‌علت EOF در storage host انجام نشد؛ شناسه و digest از GitHub API قابل خواندن بود. هیچ artifact بین run/SHAها جابه‌جا نشده است.

| Run / artifact | Artifact ID | SHA-256 digest |
|---|---:|---|
| Linux pytest CPython 3.11 | `13379035949` | `sha256:7435b3f1f3398b25318ea2a81003d3d9d75f711cf8276519b476faed2cca9b48` |
| Linux pytest CPython 3.14 | `13376894685` | `sha256:753b4df11f4a5702086409b91a4c443576656ac4f94613fbc46fa712a7cdca88` |
| Ubuntu 26.04 pytest CPython 3.14 | `13378245191` | `sha256:10bfb4341af3f6d6672c287e9f21a507ef3e9f9ab6570ee849a619cf7edc0e37` |
| Windows pytest CPython 3.11 | `13378684713` | `sha256:3c22ce578bbf18c382911cb97f608b58ce834458038f1638500bd1033acd97b1` |
| Windows pytest CPython 3.14 | `13379820641` | `sha256:7ff61b976d55385f46db5ea08ea14423f32246337b29a0c162a495d0bbf52b66` |
| Linux coverage CPython 3.11 | `13379095785` | `sha256:cf28d01eb148a11ad9643b1af7f13e98097b0f362e9f2845fae639ccb994dfb7` |
| Linux UI coverage CPython 3.11 | `13374924446` | `sha256:3759862f4cc98224239a88deb7e923ceae1ec99172656f5b1aaf8f9a8ee6614d` |
| Linux pytest matrix report | `13378258718` | `sha256:f29b7ebc1b0b6a154e50ead0e51270b6c101f8d3885dbefd38269b1046c5b03a` |
| Ubuntu pytest matrix report | `13378253854` | `sha256:40fb3c5acd6fd4a35142ff0070326a7c1163f6c66b7d0b9c36ce892158e0cde0` |
| Windows pytest matrix report | `13379307510` | `sha256:93c6e666c14ec56405c400a72f95f20528aa9175c41dbca2844c3b0c6b5996ed` |
| Linux Qt logs CPython 3.11 | `13376841286` | `sha256:72501e19cee977d849128b6b4efb2e4b49c32ecd2fef94cfdbf1244065388283` |
| Linux Qt logs CPython 3.14 | `13376021787` | `sha256:553beee185e06a947a8fc3c017136a5ac8d3ec741c2f606261357b7f065cddb2` |
| Ubuntu Qt logs | `13374959606` | `sha256:bb99171338d824d7394ab7a51aa6d58660bdbc0dc6267150d8b662702458f08e` |
| Windows UI logs CPython 3.11 | `13376935776` | `sha256:8afb3538717418a673583ddb40bc44cc3d82ce33f1077070864e5230956a42da` |
| Windows UI logs CPython 3.14 | `13375672843` | `sha256:6d7ef3b19567c4f03a0b91dd72cf459004f3f9c7c2cdfae790465cae2de68620` |
| Windows stability history CPython 3.11 | `13376011643` | `sha256:610449cc922652c166d8e69b127696f87a5a82180498c63b3f6ef41698209120` |
| Windows stability history CPython 3.14 | `13375607897` | `sha256:9260973657c20ff5fd13eaaf0c8e2fac160c3ab5d1af1b5ea8b9c02f0dee57ec` |
| Windows stability intake CPython 3.11 | `13376695971` | `sha256:cc96d22006e5f74d0d1501605cf8692600669f7404c820d6c58093325c9784be` |
| Windows stability intake CPython 3.14 | `13376181553` | `sha256:420984343c99fba3d5fdbedfe804e6761d978d0298cba801bbcb7089c49f3a15` |
| Windows stability presentation CPython 3.11 | `13376436168` | `sha256:2717963db31a0a9dc3459632b4a14bd335eff35dc8c3843a722a7fec67adc050` |
| Windows stability presentation CPython 3.14 | `13376765862` | `sha256:6369617bec259a16df5c4360dfe34183c56c0fc6a50f7c8fb5257bba77d284a0` |
| Windows PowerShell run-card behavioral logs | `13375088881` | `sha256:165d25d5507db85bc5de0acbb9762d23f1a41d45c981743342a829ef0b92a606` |

## ۹. نتیجه و اقدام بعدی

**نتیجه:** اصلاحات مهندسیِ مستقل این موج روی SHA `b3163bf...` پیاده‌سازی و آزموده شده‌اند؛ Linux/Ubuntu/Windows CI کامل PASS است. وضعیت محصول همچنان **PARTIALLY QUALIFIED — PRE-ALPHA** و انتشار production/public **NOT READY** است. failure تاریخی Windows بازتولید نشد، اما root cause اثبات نشده؛ physical Windows display acceptance و تمام موارد owner-gated باز می‌مانند.

**اقدامات لازم برای ادامهٔ qualification:**

1. مالک محصول دربارهٔ حذف/rename کد منبع در lesson revision تصمیم مکتوب بدهد؛ تا آن زمان رفتار جدیدی اضافه نشود.
2. اگر failure تاریخی UI دوباره رخ داد، diagnostic snapshot و artifact همان SHA حفظ و از روی evidence علت‌یابی شود؛ از retry/sleep/skip برای سبزکردن استفاده نشود.
3. run-card ۱۶ مرحله‌ای روی نمایشگر فیزیکی Windows اجرا و با artifact انسانی تکمیل شود؛ offscreen را جایگزین نکنید.
4. M2–M5 و هر scope اضافه فقط پس از تصمیم مالک و acceptance contract مستقل باز شود.
5. پیش از هر release ادعاشده، remote ref، کد/تست SHA، workflowها و artifactها مجدداً روی همان SHA بررسی شوند؛ `out/performance.json` را در این مأموریت restore یا commit نکنید.

## مراجع

- README وضعیت محصول: [`../../README.md`](../../README.md)
- مرجع زمینهٔ پروژه: [`../PROJECT_CONTEXT.md`](../PROJECT_CONTEXT.md)
- inventory قابلیت‌ها: [`../PRODUCT_CAPABILITY.md`](../PRODUCT_CAPABILITY.md)
- تصمیم‌های نیازمند مالک: [`../PRODUCT_DECISIONS_REQUIRED.md`](../PRODUCT_DECISIONS_REQUIRED.md)
- قرارداد Windows: [`../WINDOWS_TESTING.md`](../WINDOWS_TESTING.md)
- Linux CI روی `b3163bf...`: [run 41940002906](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41940002906)
- Windows CI روی `b3163bf...`: [run 41940002911](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41940002911)
- failure UI تاریخی روی `ff4afdd...`: [run 41841625041](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41841625041)
