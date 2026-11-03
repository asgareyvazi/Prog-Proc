# گزارش نهایی مأموریت تکمیل محصول و بستن Prog-Proc

| مشخصه | مقدار |
|---|---|
| تاریخ | 2026-11-03 (Asia/Tehran) |
| مخزن | `asgareyvazi/Prog-Proc` |
| شاخهٔ مجاز | `arena/5957b76d-prog-proc` |
| تصمیم | **PARTIALLY QUALIFIED — SPECIFIC OPEN ITEMS** |
| مرحلهٔ محصول | **PRE-ALPHA**؛ بدون ادعای production، آمادگی انتشار عمومی یا تأیید عملیاتی |

## خلاصهٔ تصمیم

کد محصول در SHA `be5d76fb93172613d66d6b3be7707e919461b2d9` از ماتریس full-suite لینوکس و ویندوز، آزمون‌های Qt واقعی/offscreen، clean-install و کنترل‌های qualification عبور کرد. گزارش‌های matrix با شناسهٔ پلتفرم، job، artifact و SHA تطبیق داده شدند؛ نتایج تاریخی SHA قبلی در این قبولی دخیل نیستند. اصلاحات نهایی این گزارش و اسناد canonical باید ــ و در بسته‌شدن مأموریت، واقعاً می‌بایست ــ روی SHA نهایی خودشان نیز با CI exact-SHA بررسی شوند.

این نتیجه **جزئی** است، چون PostgreSQL فاقد سرویس و qualification واقعی است، Ubuntu 26.04 با CPython 3.14 اصلاً matrix اجرایی ندارد، نمایش واقعی روی میزکار Windows توسط انسان انجام نشده، و تصمیم‌های M2–M5 و قرارداد محاسبات trajectory به مالک وابسته‌اند. هیچ‌یک را با skip یا فرض بسته‌شده تلقی نمی‌کنیم.

## کار محصولی انجام‌شده

### 1. انتساب شواهد full-suite

`tools/pytest_matrix.py` و workflowهای Linux/Windows، شواهد `pytest-totals.json`، JUnit، stdout، exit/state و sidecar را به **پلتفرم، نسخهٔ Python، job، artifact و `GITHUB_SHA`** مقید می‌کنند. جمعیت/نتایج از ترتیب ردیف‌ها یا annotationهای دستی استنتاج نمی‌شوند. دادهٔ مفقود، خراب، تکراری، مبهم یا ناسازگار fail-closed می‌شود؛ `SKIPPED` از `PASSED` جداست و گزارش صفر/all-skipped را PASS نمی‌شمارد. Regressionها حالت‌های اختلاط Linux/Windows، SHA ناسازگار، totals گمشده، و ورودی تکراری/مبهم را می‌پوشانند.

ماتریس قطعیِ code SHA بالا:

| پلتفرم و Python | GitHub job ID | pytest artifact (ID؛ SHA-256 آرشیو) | جمع‌آوری‌شده | Passed | Skipped | Failed | Errors | Exit |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| Linux CPython 3.11 | `125935123806` | `pytest-cp3.11-a1` (`13234949325`; `3dc40684a20cba87b2bc11437ecd7fcb8fe97cf0d0b85599e04f0d7fcba0557e`) | 2763 | 2702 | 61 | 0 | 0 | 0 |
| Linux CPython 3.14 | `125935124014` | `pytest-cp3.14-a1` (`13235590350`; `ad323ea1b4b4d1481b6b27d5866c6d8d12458625e0576627174a8c76461dc32b`) | 2763 | 2702 | 61 | 0 | 0 | 0 |
| Windows CPython 3.11 | `125935374180` | `pytest-windows-cp3.11-a1` (`13236241042`; `42c962d8d2fd19693046a79f3717ffdcf421bac89e9e96aecf3070bda0d5d11b`) | 2763 | 2700 | 63 | 0 | 0 | 0 |
| Windows CPython 3.14 | `125935374569` | `pytest-windows-cp3.14-a1` (`13236334526`; `75aff04002bbafbd589e83b482eb8c80e11623f82f1a0864b32418ee4533dc9d`) | 2763 | 2700 | 63 | 0 | 0 | 0 |

در تمام چهار ردیف: `xfailed=0`، `xpassed=0`؛ شمارش‌های skip جداگانه‌اند. گزارش‌های مستقلِ تطبیق identity موفق شدند: Linux job `125961642122`، artifact `pytest-matrix-report-linux-a1` ID `13236372181`، SHA-256 `1ce1b4472413306307a60971af79b40c8231c906fed53a91d0e2da648b7c6ccf`; Windows job `125967333852`، artifact `pytest-matrix-report-windows-a1` ID `13236898424`، SHA-256 `77b0a8bda30c61c150d267c10ed1614ebec27b9a17c9c2a2b9fecef1fdee1eca`. هر دو artifact در runهای متناظر با `head_sha=be5d76fb93172613d66d6b3be7707e919461b2d9` ثبت شده‌اند.

- [Linux CI run 41641205203](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41641205203) — conclusion `success`.
- [Windows CI run 41641205205](https://github.com/asgareyvazi/Prog-Proc/actions/runs/41641205205) — conclusion `success`.

این انتساب جایگزینِ اشتباه ردیف‌های گزارش قبلی است؛ اعداد قدیمی SHA `e7a1f074f387f0e6636f065466e1594e083bce0d` فقط سابقه‌اند و در جدول بالا به هیچ پلتفرم تازه‌ای نسبت داده نشده‌اند.

### 2. Migration و یکتایی کلید درس

بازرسی schema/catalog برای `uq_lesson_code_current_ci` اصلاح شد تا مسیر introspection با dialect و indexهای موجود سازگار باشد. قرارداد unique index تضعیف یا حذف نشده، warning به‌صورت blanket مخفی نشده و رفتار با regressionهای migration آزموده شده است.

### 3. SQLite doctor و recovery

Doctor پیش از bootstrap/upgrade و پس از آن storage preflight خواندنی انجام می‌دهد؛ integrity/FK/sidecar و lock/permissionهای مربوط بررسی می‌شوند. اگر preflight شکست بخورد، doctor سلامت domain/search را ادعا نمی‌کند و تعمیر خودکار انجام نمی‌دهد.

Backup/restore عمداً **database snapshot** است، نه full workspace backup:

- شامل `workspace.toml` و snapshot آنلاین SQLite است؛ دقیقاً سه عضو archive.
- سقف DB برابر 8 GiB، overhead برابر 16 MiB و metadata برابر 1 MiB است.
- SHA-256، schema revision، integrity/FK، مسیر/عضوهای امن، publication اتمیک و مقصدِ ازپیش‌موجودِ ممنوع کنترل می‌شوند.
- source corpus/document files و search index داخل snapshot نیستند. بازیابی آن منابع باید فقط با سیاست مجاز و فرایندی جداگانه انجام شود.
- archive رمزگذاری نشده است؛ encryption یا key management ادعا نمی‌شود.
- restore در مقصد تازه انجام می‌شود و identity فعلی workspace را به root ثبت‌شده مقید نگه می‌دارد؛ re-home یا overwrite مجاز نیست.

آزمون‌های race، permission، traversal/symlink، خرابی archive، محدودیت منابع و انتشار/restore در ماتریس code SHA قبول شدند. این قابلیت را نباید «full backup» نامید.

### 4. Daily DDR و review انسانی

گردش‌کار دستی DDR از UI تا persist/reopen/revision و review گسترش یافت: draft، save، submit، تاریخچه و مقایسه با predecessor مشخص، confirm/reject، provenance و بررسی وضعیت stale. Submit همچنان رکوردها را `CANDIDATE` می‌گذارد؛ draft تأیید نمی‌شود و revision approval را به ارث نمی‌برد. actionهای DDR به reason غیرخالی و **شناسهٔ actor اعلام‌شده** متفاوت از `created_by` نیاز دارند؛ این صرفاً مقایسهٔ متن شناسه است، نه authentication و نه اثبات اینکه دو انسان متفاوت‌اند.

UI محتوای صریح گزارش و فعالیت‌ها، provenance، زمان‌ها و تغییرات را نشان می‌دهد؛ از متن آزاد duration/depth/NPT یا مقدار مهندسی مشتق نمی‌شود. تست UI محلی به‌دلیل نبود `libGL.so.1` **SKIPPED** بود، اما jobهای Qt-enabled Linux/Windows روی code SHA فوق موفق شدند.

### 5. حفظ مرزهای دامنه و بسته‌بندی

Taxonomy اسناد یا writerهای promotion عمومی نشدند. قابلیت‌های منبع‌محور well/project، BHA، bit، mud، casing/cement، well-control/HSE و programs فقط در قراردادها/handlerهای موجود و آزمون‌های همان دامنه باقی می‌مانند؛ CRUD عمومی یا مقادیر مشتق‌شده اضافه نشد. NPT roll-up قطعی تنها محاسبهٔ executable تأییدشده باقی است. Trajectory/directional calculations بدون روش، واحد، فرض، مرجع و refusal contract مستقل فعال نشدند؛ survey station storage به‌تنهایی مجوز interpolation نیست.

clean-install smoke روی Linux و Windows برای CPython 3.11/3.14 موفق شد. PowerShell 5.1 و 7 run-card tests موفق شدند. matrix Qt-enabled Linux/Windows و stability/shutdown qualification موفق شدند؛ Qt coverage gate روی Linux CPython 3.11 به **76.33% line** و **54.21% branch** رسید (حداقل gate: 70%/40%). Ubuntu 26.04 compatibility job روی CPython 3.11 موفق شد.

## تغییرات مستندات (جدا از پیاده‌سازی محصول)

README، `PROJECT_CONTEXT.md`، `PRODUCT_CAPABILITY.md`، `DOCUMENT_DOMAIN_COVERAGE.md`، `HUMAN_REVIEW_ACTION_INVENTORY.md` و `WINDOWS_TESTING.md` با semantics جاری، تاریخ و شواهد جدید همگام شدند. exception inventory تولیدشده دوباره ساخته شد و lockهای توسعه/UI پس از مشاهدهٔ drift واقعی `coverage==7.17.1` به‌روزرسانی شدند. این‌ها ادعاهای مستقل از کد نیستند؛ هرکدام به آزمون یا artifact اشاره دارند.

## آزمون‌های محلی تکمیلی

- Static: Ruff check/format، mypy روی مرز پیکربندی‌شدهٔ 43 فایل، byte-compile و `git diff --check` موفق شدند.
- targeted suite محلی: 182 collected، 173 passed، 0 failed/errors، 9 skipped؛ یک skip PostgreSQL فاقد سرویس و 8 تست UI محلی به‌دلیل `libGL.so.1`.
- full suite محلی تکمیلی: 2763 collected، 2718 passed، 0 failed/errors، 45 skipped، exit 0، runtime `2019.857s`. stdout، JUnit، totals، shell exit و run-state با هم cross-check شدند. اجرای محلی مکمل است؛ جدول چهارپلتفرمی CI بالا شاهد exact-SHA اصلی است.
- clean install محلی wheel و sdist هر دو موفق شدند؛ clean-install CI exact-code SHA نیز در هر چهار محیط موفق است.

## موارد باز و مرز qualification

| مورد | وضعیت | علت/اقدام لازم |
|---|---|---|
| PostgreSQL | **NOT QUALIFIED** | SQLite تنها backend qualified است. product target و سرویس PostgreSQL واقعی در CI برای migration/integration لازم‌اند؛ skip مجوز ادعای پشتیبانی نیست. |
| Ubuntu 26.04 × CPython 3.14 | **NOT QUALIFIED** | job واقعی برای این ترکیب وجود ندارد؛ job CPython 3.11 روی Ubuntu 26.04 موفق، جایگزین آن نیست. |
| نمایش واقعی Windows | **NOT RUN** | نیازمند operator انسانی روی workstation واقعی؛ offscreen و disposable pilot معادل نیستند. |
| M2–M5 | **OWNER-GATED** | adapterهای AI، vector sidecar، Skills runtime و روش محاسباتی جدید به تصمیم/قرارداد مالک نیاز دارند. |
| trajectory/directional calculation | **OWNER-GATED؛ غیرفعال** | تا تصویب روش، واحد، فرض، reference، refusal cases و verification مستقل محاسبه‌ای فعال نمی‌شود. |
| authentication/multi-user | **خارج از دامنه؛ وجود ندارد** | actor string در review، احراز هویت یا audit هویت واقعی محسوب نمی‌شود. |
| installer/signing/public release/AI-vector service | **NOT CLAIMED** | نه اجازه و نه شواهد qualification این دامنه‌ها وجود ندارد. |

## Git و نتیجهٔ بستن

کار فقط روی `arena/5957b76d-prog-proc` انجام شد؛ `main` دست‌نخورده است و hash محافظت‌شدهٔ آن `85122e1a98087db4495b820a9a0ffae9009b7819` باقی می‌ماند. code SHA اندازه‌گیری‌شده `be5d76fb93172613d66d6b3be7707e919461b2d9` روی remote همان branch ثبت شد. گزارشی که اینک به اسناد افزوده می‌شود باید خودش هم در CI روی exact SHA نهایی branch بررسی شود؛ انجام این شرط بخشی از بستن نهایی است، نه فرض ضمنی.

### تصمیم نهایی مجاز

**PARTIALLY QUALIFIED — SPECIFIC OPEN ITEMS**. کد و ماتریس فعلی شواهد قابل اتکایی برای پیش‌مرحلهٔ Pre-Alpha فراهم می‌کنند؛ موارد باز بالا مانع ادعای qualification کامل، آمادگی production یا public release هستند.
