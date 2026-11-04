# گزارش نهایی موج تکمیل خودکار Prog-Proc — ۲۰۲۶-۱۱-۰۴

- **مخاطب:** مالک محصول و مهندسان عملیات حفاری
- **تصمیم:** **PARTIALLY QUALIFIED — PRE-ALPHA**
- **شاخهٔ مجاز:** `arena/5957b76d-prog-proc`
- **SHA کدِ آزموده‌شده:** `894161a2f3e296e001c03a7b20b0eeb4f57d5882`
- **گزارش‌ها:** [Linux run 41804712405](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41804712405) · [Windows run 41804712330](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41804712330)

> این گزارش، نتیجهٔ موج پیاده‌سازی و evidence موجود روی SHA کد بالا را ثبت می‌کند. تغییرات مستنداتی این گزارش روی همان شاخه جداگانه push و روی SHA خودشان دوباره در CI آزموده می‌شوند؛ نتیجهٔ آن بازبینی مستنداتی در وضعیت نهایی Git/CI همین تحویل ثبت می‌شود.

## ۱. جمع‌بندی تصمیم

Prog-Proc در این موج از حالت صرفاً طرح/اسکیما جلوتر است: چرخه‌های واقعی ثبت و بازبینی سند و DDR، workbench مبتنی بر سرویس‌ها، کنترل stale-write، گزارش‌دهی، ذخیره‌سازی محلی و مسیر نصب/اجرا در CI پوشش دارند. پنج job اجرای کامل Linux/Windows و Ubuntu 26.04 × CPython 3.14 روی **یک SHA مشخص** موفق‌اند؛ گزارش‌های انتساب artifact نیز موفق‌اند.

این نتیجه **تأیید عمومی یا production نیست**. وضعیت کلی **PARTIALLY QUALIFIED / PRE-ALPHA** می‌ماند، چون پذیرش انسانی روی نمایشگر فیزیکی Windows انجام نشده، PostgreSQL هدف و CI واقعی ندارد، قراردادهای M2–M5 و محاسبات trajectory تصویب نشده‌اند، و احراز هویت/چندکاربره/installer امضاشده اثبات نشده‌اند.

## ۲. Git و مبنای شواهد

- در آغاز موج، remote با `git ls-remote` روی `112efee5469383fcf3449af3ff09d26370896bbb` بررسی شد؛ upstream محلی تنظیم نبود و SHA محلی قدیمی به‌تنهایی مرجع remote تلقی نشد.
- تمام کار روی `arena/5957b76d-prog-proc` انجام شد؛ `main` تغییر نکرد. SHA محافظت‌شدهٔ آن `85122e1a98087db4495b820a9a0ffae9009b7819` است.
- کد اجرایی و تست‌های مورد استناد این گزارش دقیقاً `894161a2f3e296e001c03a7b20b0eeb4f57d5882` هستند. هر دو run زیر همین SHA را به‌عنوان `headSha` ثبت کرده‌اند:
  - Linux: `41804712405` — completed / success، **۹ از ۹ job موفق**.
  - Windows: `41804712330` — completed / success، **۱۴ از ۱۴ job موفق**.
- هیچ run یا artifact از SHA دیگری برای پرکردن نتیجهٔ این جدول استفاده نشده است. مجموعاً ۲۳ job در دو run موفق شدند؛ این عدد جای totals هر suite را نمی‌گیرد.
- commit مستنداتی این گزارش پس از SHA آزموده‌شده ساخته می‌شود و خودش نیز باید با CI exact-SHA بررسی شود؛ شماره و نتیجهٔ آن run را نباید با runهای کد بالا یکی دانست.

## ۳. آنچه در محصول و کد تثبیت شد

### ۳.۱ قابلیت‌ها و مرزهای مهندسی

- فهرست قابلیت‌ها از کد، handlerها و test manifest نگهداری می‌شود: ۲۶ کلاس سند (۱۴ `END_TO_END_CERTIFIED`، ۷ `KNOWLEDGE_SUPPORTED`، ۵ `EXTRACT_ONLY`). این موج کلاس یا promotion writer جدیدی به taxonomy اضافه نکرد.
- Intake همچنان صف و lifecycle بازبینی انسانی است: پیش‌نمایش به‌تنهایی registry/domain row نمی‌سازد؛ ثبت سند نیز promotion خودکار نیست. actor، reason، source provenance و مرز promotion جدا باقی مانده‌اند.
- Workbench از `DomainReviewService` و `ReviewActionService` استفاده می‌کند؛ عملیات تغییردهنده از worker خارج از GUI thread اجرا می‌شوند. مقایسهٔ revisionها متنی و مبتنی بر داده/provenance ثبت‌شده است، نه محاسبهٔ مهندسی.
- DDR چرخهٔ draft → submit → candidate → تصمیم انسانی را حفظ می‌کند. submit، گزارش/عملیات را تأیید نمی‌کند. تصمیم confirm/reject دلیل می‌خواهد و actor ID اعلام‌شده باید از `created_by` جدا باشد؛ این actor شناسهٔ احرازشده نیست و **authentication محسوب نمی‌شود**.
- `updated_at` در writeهای DDR حتی با تکرار clock tick به‌صورت strictly monotonic جلو می‌رود؛ stale-write guard و revision/activity provenance حفظ شده‌اند.
- dashboardها و reportهای موجود از read serviceها و read modelهای دامنه استفاده می‌کنند؛ renderer مستقیماً SQL صادر نمی‌کند. NPT roll-up تنها روش محاسباتی ثبت‌شده است. هزینهٔ منبع با ارز اصلی نگهداری می‌شود؛ تبدیل ارز، AFE، risk score و plan-vs-actual مهندسی اضافه نشده‌اند.
- ایستگاه‌های survey فقط به‌صورت دادهٔ گزارش‌شده باقی می‌مانند. interpolation/trajectory، واحدها یا فرضیات محاسباتی تازه اضافه نشده‌اند.

### ۳.۲ SQLite و backup

- SQLite تنها backend مورد اتکای این نسخه است. preflight سلامت/foreign-key و migration تا head `0026` در CI آزموده شده است؛ PostgreSQL از این نتیجه استنتاج نمی‌شود.
- قابلیت backup، **database snapshot only** است: SQLite online backup، manifest/hash و بررسی integrity/FK/revision؛ archive محدود و non-overwriting است و restore مقصد موجود را جایگزین نمی‌کند.
- archive شامل `workspace.toml` و snapshot دیتابیس است؛ source corpus و search sidecar داخل آن نیستند. Archive رمزگذاری نشده است. root identity guard در restore حفظ شده و re-home انجام نمی‌شود. بنابراین این قابلیت **full workspace/source backup یا disaster recovery کامل نیست**.

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
| Backup کامل workspace | **در این محصول موجود نیست** | snapshot موجود فقط DB و marker است؛ حفاظت از sourceها مسیر مستقلی می‌خواهد، و root identity guard نباید دور زده شود. |
| Production/public release | **NOT CLAIMED** | installer/signing، auth، multi-user، Windows physical acceptance، PostgreSQL و security/release gates هنوز اثبات نشده‌اند. |

هیچ‌یک از این شکاف‌ها با default، score، duration، quantity، واحد یا فرمول ساختگی پر نشده است. `database_snapshot_only` را full workspace backup معرفی نمی‌کنیم؛ actor ID را هویت تأییدشده نمی‌خوانیم؛ ثبت سند یا submit DDR را approval تلقی نمی‌کنیم.

## ۷. نتیجهٔ نهایی

**PARTIALLY QUALIFIED — PRE-ALPHA.**

محدودهٔ مثبت این تصمیم: code SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882`، Linux CPython 3.11/3.14، Windows CPython 3.11/3.14، Ubuntu 26.04 × CPython 3.14، Qt-enabled offscreen CI، clean-install smoke، SQLite/DDR regression، dependency audit و attribution artifacts فوق.

این نتیجه فقط همان ماتریس و همان SHA را پوشش می‌دهد. شواهد آن مجوز production، installer امضاشده، PostgreSQL، Windows display acceptance، حساب کاربری/مجوزدهی، multi-user یا محاسبات مهندسی جدید نیست.

## ۸. شاخص‌های راهنمای نگهداری

- وضعیت فعلی محصول: [`README.md`](../../README.md)
- context اجرایی: [`PROJECT_CONTEXT.md`](../PROJECT_CONTEXT.md)
- فهرست قابلیت‌های مبتنی بر کد: [`PRODUCT_CAPABILITY.md`](../PRODUCT_CAPABILITY.md)
- تصمیم‌های مالک و قراردادهای باز: [`PRODUCT_DECISIONS_REQUIRED.md`](../PRODUCT_DECISIONS_REQUIRED.md)
- ماتریس ۲۶ کلاس: [`DOCUMENT_DOMAIN_COVERAGE.md`](../DOCUMENT_DOMAIN_COVERAGE.md)
- پذیرش انسانی Windows: [`WINDOWS_TESTING.md`](../WINDOWS_TESTING.md)
- شواهد GitHub: [Linux run](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41804712405)، [Windows run](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41804712330)
