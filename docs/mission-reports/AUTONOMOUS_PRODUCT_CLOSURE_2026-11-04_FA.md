# گزارش نهایی تکمیل و سخت‌سازی Prog-Proc — ۲۰۲۶-۱۱-۰۴

- **مخاطب:** مالک محصول و مهندسان عملیات حفاری
- **تصمیم جاری:** **PARTIALLY QUALIFIED — PRE-ALPHA**
- **شاخهٔ مجاز:** `arena/5957b76d-prog-proc`
- **آخرین SHA کد منتشرشده:** `ff4afddba0d5f319b0f43341dcaed13991b4dd3d`
- **CI دقیق روی SHA جاری:** [Linux run 41841625094](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41841625094) با **SUCCESS** و [Windows run 41841625041](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41841625041) با **FAILURE** پایان یافتند. هر پنج full-suite lane موفق‌اند؛ Windows offscreen UI check-run `126614417177` به‌علت یک test failure workflow را قرمز کرد. Totals و artifact ID/digestهای هر دو run در بخش ۹.۱ ثبت شده‌اند. شکست‌های SHA قبلی `94d698ffcb2c5e5c2a36ac164dba222e17d65cc1` در بخش ۹ با totals دقیق آمده‌اند.
- **مبنای قبلی جداگانه:** کد SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882` با [Linux run 41804712405](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41804712405) و [Windows run 41804712330](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41804712330) موفق بود.

> جدول‌های ماتریس و artifact در بخش‌های تاریخی، شواهد SHA قبلی `894161a...` هستند و به SHA جاری منتقل نمی‌شوند. کد backup/archive روی `94d698f...` در CI شکست خورد؛ اصلاح inventory و عبارت parser در code SHA `ff4afdd...` commit و push شده‌اند. هر پنج full-suite lane در دو run exact-SHA آن موفق‌اند؛ Windows workflow به‌علت یک failure در UI offscreen ناموفق پایان یافت. شواهد و artifactهای جاری جداگانه در بخش ۹.۱ ثبت می‌شود. هر commit مستنداتی نیز SHA و CI مستقل خود را خواهد داشت؛ هیچ total یا artifact بین SHAها مخلوط نمی‌شود.

## ۱. جمع‌بندی تصمیم

Prog-Proc در این موج از حالت صرفاً طرح/اسکیما جلوتر است: چرخه‌های واقعی ثبت و بازبینی سند و DDR، workbench مبتنی بر سرویس‌ها، کنترل stale-write، گزارش‌دهی، ذخیره‌سازی محلی و مسیر نصب/اجرا در CI پوشش دارند. در baseline تاریخی SHA `894161a...`، پنج full-suite lane Linux/Windows و Ubuntu 26.04 × CPython 3.14 روی **یک SHA مشخص** موفق بودند؛ گزارش‌های انتساب artifact نیز موفق بودند. follow-up روی SHA `94d698f...` شکست خورد؛ روی code SHA `ff4afdd...` هر پنج full-suite lane Linux/Windows/Ubuntu موفق‌اند، اما Windows offscreen UI یک failure دارد و Windows workflow در مجموع **FAILED** شده است.

این نتیجه **تأیید عمومی یا production نیست**. وضعیت کلی **PARTIALLY QUALIFIED / PRE-ALPHA** می‌ماند، چون پذیرش انسانی روی نمایشگر فیزیکی Windows انجام نشده، PostgreSQL هدف و CI واقعی ندارد، قراردادهای M2–M5 و محاسبات trajectory تصویب نشده‌اند، و احراز هویت/چندکاربره/installer امضاشده اثبات نشده‌اند.

## ۲. Git و مبنای شواهد

- در موج قبلی، remote با `git ls-remote` روی `112efee5469383fcf3449af3ff09d26370896bbb` بررسی شد؛ upstream محلی تنظیم نبود و SHA محلی قدیمی به‌تنهایی مرجع remote تلقی نشد. این سابقه مربوط به آن موج است، نه baseline پیگیری حاضر.
- تمام کار روی `arena/5957b76d-prog-proc` انجام شد؛ `main` تغییر نکرد. SHA محافظت‌شدهٔ آن `85122e1a98087db4495b820a9a0ffae9009b7819` است.
- کد اجرایی و تست‌های مورد استناد این گزارش دقیقاً `894161a2f3e296e001c03a7b20b0eeb4f57d5882` هستند. هر دو run زیر همین SHA را به‌عنوان `headSha` ثبت کرده‌اند:
  - Linux: `41804712405` — completed / success، **۹ از ۹ job موفق**.
  - Windows: `41804712330` — completed / success، **۱۴ از ۱۴ job موفق**.
- هیچ run یا artifact از SHA دیگری برای پرکردن نتیجهٔ این جدول استفاده نشده است. مجموعاً ۲۳ job در دو run موفق شدند؛ این عدد جای totals هر suite را نمی‌گیرد.
- در پیگیری جاری، remote شاخه پیش از commit backup/archive روی SHA `c42a760ce209e12ce3ee090e4cb33f3609d91175` بود؛ آن commit کد `94d698ffcb2c5e5c2a36ac164dba222e17d65cc1` روی شاخهٔ مجاز ساخته و push شد. CI آن در هر پنج full-suite lane شکست خورد.
- در ادامه، checkout محلی به‌طور shallow روی `317c553...` بود، در حالی که remote روی `94d698f...` قرار داشت و ۱۹۶ مسیر dirty/untracked دیده می‌شد. بازرسی فقط‌خواندنی ancestry/worktree انجام شد؛ هیچ reset، clean، stash، branch-switch یا rewrite انجام نشد. تغییرات محلی با remote tree تطبیق داده شد و code fix commit `ff4afddba0d5f319b0f43341dcaed13991b4dd3d` به‌صورت فرزند مستقیم `94d698f...` ساخته و بدون force push منتشر شد. فقط سه فایل مرتبط با CI در آن commit هستند؛ اصلاحات README، PRODUCT_CAPABILITY، PROJECT_CONTEXT و این گزارش در یک change-set مستنداتی جدا از code/test SHA نگهداری شده‌اند.
- `out/performance.json` در checkout حاضر وجود نداشت و به‌صورت deletion unstaged دیده شد؛ چون خروجی artifact موجود در checkout نبود، آن را بازیابی، stage یا commit نکردیم. stash خالی بود و تغییرات مستنداتی از code/test SHA جدا نگه داشته شدند. CI code SHA `ff4afdd...`: Linux run `41841625094` با success و Windows run `41841625041` با failure پایان یافتند؛ پنج full-suite lane موفق و یک Windows UI test ناموفق بود. هیچ total یا artifact بین SHAها منتقل نمی‌شود.

## ۳. آنچه در محصول و کد تثبیت شد

### ۳.۱ قابلیت‌ها و مرزهای مهندسی

- فهرست قابلیت‌ها از کد، handlerها و test manifest نگهداری می‌شود: ۲۶ کلاس سند (۱۴ `END_TO_END_CERTIFIED`، ۷ `KNOWLEDGE_SUPPORTED`، ۵ `EXTRACT_ONLY`). این موج کلاس یا promotion writer جدیدی به taxonomy اضافه نکرد.
- Intake همچنان صف و lifecycle بازبینی انسانی است: پیش‌نمایش به‌تنهایی registry/domain row نمی‌سازد؛ ثبت سند نیز promotion خودکار نیست. actor، reason، source provenance و مرز promotion جدا باقی مانده‌اند.
- Workbench از `DomainReviewService` و `ReviewActionService` استفاده می‌کند؛ عملیات تغییردهنده از worker خارج از GUI thread اجرا می‌شوند. مقایسهٔ revisionها متنی و مبتنی بر داده/provenance ثبت‌شده است، نه محاسبهٔ مهندسی.
- DDR چرخهٔ draft → submit → candidate → تصمیم انسانی را حفظ می‌کند. submit، گزارش/عملیات را تأیید نمی‌کند. تصمیم confirm/reject دلیل می‌خواهد و actor ID اعلام‌شده باید از `created_by` جدا باشد؛ این actor شناسهٔ احرازشده نیست و **authentication محسوب نمی‌شود**.
- `updated_at` در writeهای DDR حتی با تکرار clock tick به‌صورت strictly monotonic جلو می‌رود؛ stale-write guard و revision/activity provenance حفظ شده‌اند.
- dashboardها و reportهای موجود از read serviceها و read modelهای دامنه استفاده می‌کنند؛ renderer مستقیماً SQL صادر نمی‌کند. NPT roll-up تنها روش محاسباتی ثبت‌شده است. هزینهٔ منبع با ارز اصلی نگهداری می‌شود؛ تبدیل ارز، AFE، risk score و plan-vs-actual مهندسی اضافه نشده‌اند.
- ایستگاه‌های survey فقط به‌صورت دادهٔ گزارش‌شده باقی می‌مانند. interpolation/trajectory، واحدها یا فرضیات محاسباتی تازه اضافه نشده‌اند.

### ۳.۲ SQLite و دو محدودهٔ مستقل backup

- SQLite تنها backend مورد اتکای این نسخه است. preflight سلامت/foreign-key و migration تا head `0026` در CI تاریخی آزموده شده است؛ PostgreSQL از آن نتیجه استنتاج نمی‌شود.
- `workspace backup/restore` همچنان **database snapshot only** است: SQLite online backup، manifest/hash و بررسی integrity/FK/revision؛ archive محدود و non-overwriting است و restore مقصد موجود را جایگزین نمی‌کند. فقط `workspace.toml` و snapshot دیتابیس را دارد؛ source corpus و search sidecar بیرون‌اند. شواهد پاس آن فقط به SHA تاریخی `894161a...` تعلق دارد.
- در follow-up، commit کد `94d698ffcb2c5e5c2a36ac164dba222e17d65cc1` یک قالب جداگانهٔ versioned `.diwa` و CLIهای `workspace archive` و `workspace restore-archive` افزوده است: فایل‌های regular واجد شرایط زیر root به‌علاوهٔ online snapshot پایگاه‌داده، hash جداگانه برای هر عضو، حدود منابع، گزارش exclusion و source reference، redaction اطلاعات URL حساس در گزارش، staging و بررسی کامل hash/SQLite قبل از publish اتمی بدون جایگزینی. index، cache/log، VCS، credential/env و transient files حذف می‌شوند؛ فایل‌های منبع بیرون workspace گزارش می‌شوند اما دنبال یا کپی نمی‌شوند. restore فقط به ریشهٔ ثبت‌شده مجاز است؛ re-home رد می‌شود. آرشیو رمزگذاری نشده است.
- focused local tests روی SHA `94d698f...` پاس شدند، اما هر پنج full-suite CI lane روی همان SHA هرکدام ۲ failure داشتند. code fix SHA `ff4afdd...` آن دو قرارداد تست را اصلاح می‌کند و هر پنج full-suite lane این SHA موفق شدند؛ Windows offscreen UI یک failure مستقل دارد و workflow Windows را fail کرد. هیچ‌یک از این‌ها backup policy/DR کامل، encryption، منبع بیرونی یا پذیرش فیزیکی Windows را اثبات نمی‌کند.

### ۳.۳ علت‌یابی و اصلاح CI

- شکست قبلی Windows در آزمون DDR دربارهٔ به‌روزرسانی stale token با timestamp بود؛ زمان ثبت write اکنون monotonic است و regression در Windows CPython 3.11/3.14 روی SHA نهایی سبز است.
- خطای قبلی audit pin drift با تجدید pins از ابزار مجاز `tools/constraints.py` اصلاح شد: `filelock==4.2.8` و `platformdirs==4.14.4`؛ فحص constraints و dependency audit تمیز است.
- mismatch پیشین در SHA-256 گزارش Ubuntu به اجرای nested pytest در workbench مربوط بود که به‌صورت پیش‌فرض `pytest-totals.json` ریشه را بازنویسی می‌کرد. در یک پروژهٔ موقت محلی این رفتار با `--collect-only` بازتولید شد؛ سپس collection و هر تلاش pytest فرزند به `PYTEST_TOTALS_PATH` جداگانه در `qt-logs` هدایت شدند. Regression آزموده شد و reporterهای Linux، Ubuntu و Windows روی SHA نهایی موفق شدند. Hash/sidecar validation تضعیف نشد.

## ۴. CI دقیق روی SHA کد `894161a2f3e296e001c03a7b20b0eeb4f57d5882`

Totals زیر از annotationهای check-run هر suite گرفته شده‌اند؛ skipped جدا از pass است. همهٔ suiteها exit `0`، صفر failure، صفر error، صفر xfailed و صفر xpassed دارند.

| پلتفرم / Python | Check-run | جمع‌آوری‌شده | Passed | Skipped | Failed / errors | Runtime ثانیه |
|---|---:|---:|---:|---:|---:|---:|
| Linux / CPython 3.11 | `126488762659` | 2,770 | 2,709 | 61 | 0 / 0 | 2,848.499 |
| Linux / CPython 3.14 | `126488762620` | 2,770 | 2,709 | 61 | 0 / 0 | 1,611.808 |
| Ubuntu 26.04 / CPython 3.14 | `126488762978` | 2,770 | 2,769 | 1 | 0 / 0 | 1,817.713 |
| Windows / CPython 3.11 | `126488762554` | 2,770 | 2,707 | 63 | 0 / 0 | 3,241.131 |
| Windows / CPython 3.14 | `126488762999` | 2,770 | 2,707 | 63 | 0 / 0 | 2,830.841 |

- `Ubuntu 26.04 × CPython 3.14` یک **lane واقعی full-suite** است، نه استنتاج از Ubuntu 24.04؛ job `126488762978`، clean-install smoke و totals attribution آن در Linux run موفق‌اند. این مورد، همان ماتریس CI را تأیید می‌کند؛ به‌تنهایی انتشار عمومی یا نصب روی همهٔ Ubuntuها را تأیید نمی‌کند.
- Linux run تمام jobهایش را با success بست و job گزارش attribution آن `126509548709` بود. Windows نیز تمام jobها را با success بست و job گزارش attribution آن `126511686930` بود.
- Qt offscreen UI/workbench در CI اجرا شد. UI-package coverage در job `126488762602`: **76.43% خطوط، 54.21% شاخه‌ها** (2,416/3,161 statement و 399/736 branch)، بالاتر از حداقل 70%/40%. این شاخص پوشش همان Qt/UI package است؛ با coverage کل محصول یکی نیست.
- روی Linux CPython 3.11، full-suite coverage annotation برابر 82.02% خطوط / 70.8% شاخه و کل 79% بود؛ headless UI-package annotation مجزا 26.51%/12.91% است و جایگزین نتیجهٔ Qt-enabled coverage بالا نیست.
- dependency audit در Linux، `no known vulnerabilities` گزارش کرد؛ datetime-adapter warning count در دو Linux و دو Windows suite برابر صفر بود.
- گزارش‌های attribution دو warning غیرمسدودکننده از GitHub runner ثبت کردند که `actions/download-artifact` با Node.js 20 تعریف شده ولی runner آن را با Node.js 24 اجرا می‌کند. این warning موفقیت jobها را تغییر نداد و در این موج به‌عنوان defect نرم‌افزار Prog-Proc تعبیر نشده است.

### ۴.۱ Artifactهای suite — ID و digest کامل

Digestها همان مقدار GitHub Actions artifact API هستند و به SHA اجرای همین موج مربوط‌اند.

| Artifact | ID | SHA-256 digest |
|---|---:|---|
| `pytest-cp3.11-a1` (Linux) | `13311607981` | `sha256:0e0abec845dae861cb247b74d30e8c4a37a744d13453f2f9f832bdfb8cab6cc8` |
| `pytest-cp3.14-a1` (Linux) | `13311670319` | `sha256:a8fa32a33d718c959fee1b573bb706cba35ac79015516c40456b1f6d3b8999c3` |
| `pytest-ubuntu-2604-cp3.14-a1` | `13310917801` | `sha256:3b9a790c6fd2c2d8fbf9deff193bafc0c9308c30886284ac7872dafbbc71d018` |
| `pytest-windows-cp3.11-a1` | `13312043472` | `sha256:ecadd6e3770b3fe269acfed9ee6081697e8a4f1e2c7236ab2fde6a81b93b9e56` |
| `pytest-windows-cp3.14-a1` | `13312067518` | `sha256:d675de20f34a7d81e9e865d06b3511d2f3006d03c1d7d5c3de16b9eacd499414` |

### ۴.۲ Artifactهای گزارش انتساب

| گزارش | ID | SHA-256 digest |
|---|---:|---|
| `pytest-matrix-report-linux-a1` | `13312715966` | `sha256:5f1050fd9a193e8c1ff8c1b0a59d9125606e45c7b128c5d3ef955845f5d4fc22` |
| `pytest-matrix-report-ubuntu-2604-cp3.14-a1` | `13312421750` | `sha256:340ae11d2c7752904cbe6408dcfc1746d8398d25f1713297bee1c1ced9fc538e` |
| `pytest-matrix-report-windows-a1` | `13311839332` | `sha256:becc15cadb4997c94a90f81fb5e62235b327db9cdbc8396c09ea5caa70fa3bdc` |

این جدول digest مربوط به خود archive artifactهاست؛ با checksum فایل totals/JUnit داخل archive یکی نیست. نبودن totals یا artifact هرگز PASS محسوب نشده است.

## ۵. بررسی‌های محلی و فحوص غیر CI

- ۵۱ آزمون در مجموعه‌های regression مربوط به Qt workbench policy، گزارش CI، authoring DDR و exception policy موفق شدند.
- Ruff، بررسی format، mypy روی ۴۳ فایل منبع، `compileall` و `git diff --check` موفق شدند.
- `tools/constraints.py --check` برای پنج گروه `runtime`, `development`, `ui`, `build`, `audit` موفق شد؛ dependency audit محلی `clean` و بدون finding بود.
- اجرای Qt روی همین محیط محلی به‌علت نبودن `libGL.so.1` قابل قبول نبود؛ آن را **SKIPPED** ثبت کردیم، نه PASS. evidence مثبت Qt فقط از laneهای Qt-enabled CI می‌آید.
- در code SHA منتشرشدهٔ `94d698f...`: focused archive/TOML/backup/CLI tests برابر **27 passed**؛ Ruff، Ruff format، mypy روی **44** source file و Alembic head `0026` موفق بودند. CI همان SHA بعداً دو test contract failure در 3 suite لینوکس را افشا کرد.
- پیش از commit `ff4afdd...`، پس از اصلاح exception inventory و command-word false positive، focused regression run **29 passed** شد؛ Ruff و Ruff format کامل، mypy (44 files) و Alembic check نیز موفق شدند. این run روی همان source contents اصلاحی بود، اما پیش از commit و بیرون از CI.
- در checkout جاری pytest package/venv موجود نبود؛ تلاش برای rerun focused tests به‌علت `No module named pytest` **NOT RUN** شد. محیط dependency/cache را برای سبزکردن مصنوعی نتیجه تغییر ندادیم. این محدودیت، نتیجهٔ exact-SHA GitHub Actions را جایگزین نمی‌کند.
- یک اجرای کامل pytest در محیط محلی روی working tree پیشین به‌مدت 30 دقیقه ادامه یافت اما در 54% بدون پایان/exit code قطعی متوقف شد؛ آن را **NOT COMPLETED / NOT PASS** می‌شماریم و هیچ total از آن استخراج نمی‌کنیم.

## ۶. موارد خارج از تأیید و شروط ادامه

| مورد | وضعیت دقیق | شرط بستن |
|---|---|---|
| پذیرش Windows روی نمایشگر فیزیکی | **NOT RUN** | انسان باید checklist `docs/WINDOWS_TESTING.md` را روی Windows واقعی اجرا کند؛ offscreen CI جانشین آن نیست. |
| PostgreSQL | **NOT QUALIFIED** | مالک باید target/version/مهاجرت را تعیین کند و CI با service واقعی و matrix آزمون اضافه شود. |
| M2: OpenAI-compatible provider | **OWNER-GATED** | قرارداد endpoint/auth/secret/network و تست عدم ارسال بدون اجازه لازم است. |
| M3: vector sidecar / semantic ranking | **OWNER-GATED** | dimension، metric، staleness و عدم تغییر retrieval در نبود sidecar باید تصویب شود. |
| M4: Skills runtime | **OWNER-GATED** | رفتار اپراتور و مرز محاسبه/تغییر دامنه باید تعریف شود. |
| M5: محاسبهٔ مهندسی اضافه | **OWNER-GATED** | روش، ورودی‌ها، واحدها، assumptions و refusal cases باید پیش از کدنویسی تصویب شوند. |
| Survey trajectory | **OWNER-GATED؛ محاسبه خاموش** | هیچ interpolation/trajectory بدون قرارداد، units، assumptions، reference و verification مستقل ساخته نشود. |
| archive فایل‌های owned داخل workspace | پیاده‌سازی روی SHA `94d698f...`؛ CI آن **FAILED** بود. اصلاح دو تست‌قرارداد روی SHA `ff4afdd...` commit/push شده؛ هر پنج full-suite lane **SUCCESS** شدند، اما Windows workflow **FAILED** شد به‌علت یک UI offscreen test failure. | محدوده/exclusion/source بیرونی صریح است؛ منابع بیرونی کپی نمی‌شوند و restore به root اصلی مقید است. encrypted/DR/production backup ادعا نشده؛ physical Windows پذیرش جدا می‌خواهد. |
| backup پایگاه‌داده | **PASS تاریخی** فقط روی SHA `894161a...` | `workspace backup/restore` فقط marker+SQLite snapshot است؛ این نتیجه به کد `94d698f...` منتقل نشده است. |
| Production/public release | **NOT CLAIMED** | installer/signing، auth، multi-user، Windows physical acceptance، PostgreSQL و security/release gates هنوز اثبات نشده‌اند. |

هیچ‌یک از این شکاف‌ها با default، score، duration، quantity، واحد یا فرمول ساختگی پر نشده است. `database_snapshot_only` را full workspace backup معرفی نمی‌کنیم؛ actor ID را هویت تأییدشده نمی‌خوانیم؛ ثبت سند یا submit DDR را approval تلقی نمی‌کنیم.

## ۷. نتیجهٔ نهایی

**PARTIALLY QUALIFIED — PRE-ALPHA.**

محدودهٔ مثبتِ **baseline تاریخی** این تصمیم: code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882`، Linux CPython 3.11/3.14، Windows CPython 3.11/3.14، Ubuntu 26.04 × CPython 3.14، Qt-enabled offscreen CI، clean-install smoke، SQLite/DDR regression، dependency audit و attribution artifacts بخش‌های قبلی. این شواهد به SHA `94d698f...` یا `ff4afdd...` منتقل نمی‌شوند؛ SHA `94d698f...` پنج full-suite lane با شکست داشت. روی `ff4afdd...` هر پنج full-suite lane موفق‌اند، اما Windows run با یک UI offscreen failure پایان یافت؛ این SHA بنابراین full-suite success را دارد، نه overall CI pass.

هیچ‌یک از این نتایج مجوز production، installer امضاشده، PostgreSQL، Windows display acceptance، حساب کاربری/مجوزدهی، multi-user یا محاسبات مهندسی جدید نیست.

## ۹. CI follow-up دقیق روی SHA `94d698ffcb2c5e5c2a36ac164dba222e17d65cc1`

این runها فقط به همان SHA تعلق دارند؛ شکست‌ها ثبت می‌شوند و totals baseline تاریخی یا commit اصلاحی را جایگزین نمی‌کنند.

| Platform / Python | Check-run | Collected | Passed | Skipped | Failed | Errors | Exit / runtime (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Linux / CPython 3.11 | `126565464516` | 2,776 | 2,713 | 61 | 2 | 0 | 1 / 2,687.198 |
| Linux / CPython 3.14 | `126565464329` | 2,776 | 2,773 | 1 | 2 | 0 | 1 / 1,840.836 |
| Ubuntu 26.04 / CPython 3.14 | `126565464510` | 2,776 | 2,713 | 61 | 2 | 0 | 1 / 1,274.754 |
| Windows / CPython 3.11 | `126565464858` | 2,776 | 2,711 | 63 | 2 | 0 | 1 / 3,281.650 |
| Windows / CPython 3.14 | `126565464983` | 2,776 | 2,711 | 63 | 2 | 0 | 1 / 3,576.037 |

- Linux workflow `41827377959` و Windows workflow `41827378042` هر دو **failure** هستند؛ هر پنج test lane دقیقاً ۲ failed و صفر errors دارند. CI annotation در Linux 3.11/3.14 و Windows 3.11/3.14 نام دو failure یکسان را نشان می‌دهد: (۱) `tests/unit/test_exception_policy.py::test_the_inventory_on_disk_matches_the_tree` به‌علت inventory تولیدنشده پس از افزودن handlerها؛ (۲) `tests/unit/test_cli.py::TestCliEndToEnd::test_no_message_prompts_for_a_command_the_cli_does_not_have` چون phrase «drillintel data» به‌عنوان command اشتباه برداشت شد. Ubuntu lane نیز دو failure ثبت کرده، اما نام آن‌ها در annotation آن job موجود نبود؛ علت را برای آن lane جداگانه تأیید نمی‌کنیم.
- خطای نسبت‌دادن matrix report در هر دو workflow پیامد failure test lanes است؛ successful smoke/Qt/stability jobs در همان run، full-suite failures را خنثی نمی‌کنند.
- در working tree اصلاح‌شده، `docs/exception_inventory.md` با generator رسمی دوباره تولید شد و phrase متداخل به «workspace storage directory» تغییر یافت. قبل از commit، regressionهای مربوط به این دو مورد و archive/backup/TOML/CLI **29 passed** شدند؛ Ruff کامل، Ruff format کامل و mypy (44 source files) نیز موفق بودند. این‌ها local-only هستند، نه full-suite CI.

### ۹.۱ کد اصلاحی `ff4afddba0d5f319b0f43341dcaed13991b4dd3d`

این code/test-fix commit، فرزند مستقیم `94d698f...`، روی شاخهٔ مجاز push شده است. آن inventory تولیدشده را به‌روز می‌کند و wording تست CLI را از الگوی اشتباه `drillintel data` دور می‌سازد. CI exact-SHA:

| Lane | Check-run | Collected | Passed | Skipped | Failed | Errors | Runtime (s) / status |
|---|---:|---:|---:|---:|---:|---:|---|
| Linux / CPython 3.14 | `126614416407` | 2,776 | 2,715 | 61 | 0 | 0 | 1,904.800 / **SUCCESS** |
| Ubuntu 26.04 / CPython 3.14 | `126614416020` | 2,776 | 2,775 | 1 | 0 | 0 | 1,729.176 / **SUCCESS** |
| Linux / CPython 3.11 | `126614416484` | 2,776 | 2,715 | 61 | 0 | 0 | 2,975.663 / **SUCCESS** |
| Windows / CPython 3.11 | `126614417059` | 2,776 | 2,713 | 63 | 0 | 0 | 3,087.797 / **SUCCESS** |
| Windows / CPython 3.14 | `126614416967` | 2,776 | 2,713 | 63 | 0 | 0 | 3,248.718 / **SUCCESS** |
| Windows / UI offscreen Qt / CPython 3.11 | `126614417177` | 29 | 28 | 0 | 1 | 0 | 74.505 / **FAILURE** |

Runهای کلی: Linux `41841625094` **SUCCESS**؛ Windows `41841625041` **FAILURE**. هر پنج full-suite lane Linux/Ubuntu/Windows موفق‌اند؛ UI offscreen job علت workflow failure است.

- Check-run `126614417177`، Windows offscreen UI / CPython 3.11، 29 collected، 28 passed، 1 failed، 0 skipped/errors. Failure: `tests/ui/test_intake_workbench.py::test_intake_review_registration_and_rejection_run_as_distinct_ui_transitions`, annotation: «registration and pending refresh did not finish». این یک failure جدا از دو contract failure روی SHA `94d...` است؛ علت ریشه‌ای/تکرارپذیری آن اثبات نشده و هیچ retry یا اصلاح آزمون کورکورانه انجام نشده است. این offscreen Qt نتیجهٔ پذیرش فیزیکی نمایشگر Windows محسوب نمی‌شود.
- Artifactهای تمام‌شدهٔ runها و ID/digest دقیق در جدول‌های بعدی ثبت شده‌اند. Runهای موفق smoke، stability، full-suite یا artifact report نمی‌توانند UI failure را خنثی کنند.
- CPython 3.11 Linux annotationهای همین check-run: full-suite coverage **81.82% lines / 70.37% branches**، UI-package headless **26.51% / 12.91%**، total coverage annotation **79%**؛ datetime-adapter warnings **0** و dependency audit `no known vulnerabilities`. این موارد نیز به SHA `ff4afdd...` و همین job محدودند.
- نتیجهٔ دقیق `ff4afdd...`: پنج full-suite lane موفق، اما workflow Windows به‌علت UI failure ناموفق؛ بنابراین ماتریس/CI کلی **FAILURE**, نه PASS. هیچ baseline قدیمی یا focused local test این failure را پاک نمی‌کند.

### ۹.۲ Artifactهای exact-SHA — run Linux `41841625094` و Windows `41841625041`

این شناسه‌ها و digestها از artifact list هر دو run بعد از تکمیل استخراج شده‌اند؛ همه متعلق به code SHA `ff4afddba0d5f319b0f43341dcaed13991b4dd3d` هستند. Digest، SHA-256 archive artifact GitHub است، نه checksum محتوای داخل آن.

| Run / artifact | ID | SHA-256 digest |
|---|---:|---|
| Linux `pytest-cp3.11-a1` | `13333805671` | `sha256:3f4e5ec62724a7f54004b9a1c0ebc8bd89c4981003cdc17ee5efb2c9ea8715a3` |
| Linux `pytest-cp3.14-a1` | `13332096988` | `sha256:feb1aa17cdf4c82274f23f5142094bf85319d4b1410ec90e7bb55a3fab5e9002` |
| Linux `pytest-ubuntu-2604-cp3.14-a1` | `13330728666` | `sha256:c438fb16c2d5a59825f91ad89d383382a56b1148f9aa8b9f04e713f9e727756b` |
| Windows `pytest-windows-cp3.11-a1` | `13334366847` | `sha256:840df58ed35f13464ccb93f94b9ed367763e4861d177d289065b7221d5ec7255` |
| Windows `pytest-windows-cp3.14-a1` | `13335225684` | `sha256:a3359b0531836782b6724b10a45c4f63e28e0488b66f0d74aa944e51cff5460b` |
| Linux `coverage-cp3.11` | `13333485979` | `sha256:0bf76ae5b1712f59d773d3d5ab60c05040984f644ad41e7ee37175c02aacbd4b` |
| Linux `coverage-ui-cp3.11` | `13329781712` | `sha256:cc709793cbd59a55e4d0fd0c106ff3ee6cbc462b2b7f31e1ef619eba77d742bc` |
| Linux `pytest-matrix-report-linux-a1` | `13335100050` | `sha256:2d259399bdb92d26688207572cb11b41bb4c2acfcb0bc6f1e5848ec341863079` |
| Linux `pytest-matrix-report-ubuntu-2604-cp3.14-a1` | `13333311001` | `sha256:4598506f333b7b080758e6753c35c473858c1b9194daf88972c6f4e1fbcf9d45` |
| Windows `pytest-matrix-report-windows-a1` | `13336220429` | `sha256:a5f75155e41c4329dd1dcd330ab3c6d6502b06fc9f00aeb5df3220d493379e58` |

#### Log artifacts جانبی

| Run / artifact | ID | SHA-256 digest |
|---|---:|---|
| Linux `qt-ubuntu-2604-logs` | `13329787016` | `sha256:bc734970484900b40a05419e1cf35e01e09322300b49bd3ea9bdbddb312a33e5` |
| Linux `qt-workbench-logs-cp3.11` | `13328962784` | `sha256:d34b54b6b3fb4e611bf141d238672f3032ff90d1423590c49743a8e7502825d3` |
| Linux `qt-workbench-logs-cp3.14` | `13328503990` | `sha256:413362723f975121e7917a6260bf9e74d786bc212d2a280254f24813f3edd0ca` |
| Windows `qt-windows-stability-logs-cp3.11-history` | `13328623318` | `sha256:6fe3e74be01132a7bdb448f632fa4eea2ec5a024bf896e24289c438ec397197b` |
| Windows `qt-windows-stability-logs-cp3.11-intake` | `13328423365` | `sha256:572ed75c3dab813f355e8cc945ee281fa495eddd95364d5c0c67978d0d6ea704` |
| Windows `qt-windows-stability-logs-cp3.11-presentation` | `13328253418` | `sha256:18c10625d3ade8de537bf8e53476953ddc0b807807f8bd9e9cd4b7fb50978beb` |
| Windows `qt-windows-stability-logs-cp3.14-history` | `13329546639` | `sha256:6230c40c17e7f8bc56343970def8f29d3aec1289e84f654f1a9a0fead82b28f8` |
| Windows `qt-windows-stability-logs-cp3.14-intake` | `13329341829` | `sha256:45a843027d0e4dd5b1efa39d73d643c1e314402283870a7bf6ca6d198706fe2d` |
| Windows `qt-windows-stability-logs-cp3.14-presentation` | `13329806462` | `sha256:02dea97894a161feb60d536301581f349a3e364ee92fbb4889b55cf1fd39567e` |
| Windows `qt-windows-ui-logs-cp3.11` | `13329052420` | `sha256:301ffb0e8bbeb4598d0c02741e16bcaaa1375aca87d64595eba01554af0c3a7a` |
| Windows `qt-windows-ui-logs-cp3.14` | `13329751951` | `sha256:97201defd1bbbc2a9f7a174dfc9cb6aaca93d57874d525f6da709470850cd919` |
| Windows `windows-run-card-powershell-logs` | `13329156789` | `sha256:e32d0c4c5f59c48d9c3ae1d729c0552554452395b6dfbbad3b86ecd618af05f9` |

## ۱۰. شاخص‌های راهنمای نگهداری

- وضعیت فعلی محصول: [`README.md`](../../README.md)
- context اجرایی: [`PROJECT_CONTEXT.md`](../PROJECT_CONTEXT.md)
- فهرست قابلیت‌های مبتنی بر کد: [`PRODUCT_CAPABILITY.md`](../PRODUCT_CAPABILITY.md)
- تصمیم‌های مالک و قراردادهای باز: [`PRODUCT_DECISIONS_REQUIRED.md`](../PRODUCT_DECISIONS_REQUIRED.md)
- ماتریس ۲۶ کلاس: [`DOCUMENT_DOMAIN_COVERAGE.md`](../DOCUMENT_DOMAIN_COVERAGE.md)
- پذیرش انسانی Windows: [`WINDOWS_TESTING.md`](../WINDOWS_TESTING.md)
- شواهد GitHub — baseline تاریخی `894161a...`: [Linux run](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41804712405)، [Windows run](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41804712330).
- شواهد follow-up شکست‌خورده روی SHA `94d698f...`: [Linux run](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41827377959)، [Windows run](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41827378042).
- شواهد code/test-fix SHA `ff4afdd...`: [Linux run](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41841625094)، [Windows run](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41841625041).
