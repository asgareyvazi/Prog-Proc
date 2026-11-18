# گزارش مأموریت مهندسی Program و Procedure — ۱۶ نوامبر ۲۰۲۶

## خلاصهٔ اجرایی

در clone کاری `/tmp/Prog-Proc-mission` یک برش نخستِ authoring ساختاریافته و headless برای Drilling Program و Procedure ساخته شده است. این برش شامل migration، persistence و مدل‌های مربوط، سرویس authoring، دستورات CLI، کنترل نسخهٔ جاری و optimistic concurrency، اعتبارسنجی ساختاری، submission/review/decision، revision و مقایسهٔ فقط‌خواندنی است. این خروجی **پلتفرم کامل مهندسی یا تأیید طراحی نیست** و تا اجرای CI روی SHA نهایی، qualified محسوب نمی‌شود.

## آنچه این برش فراهم می‌کند

- Programهای نسخه‌دار با بخش‌های ساختاریافته و targetهای برنامه‌ریزی‌شده؛ و Procedureهای نسخه‌دار با گام‌های ساختاریافته.
- افزودن و ویرایش Program target، جابه‌جایی کامل و یکتای targetها؛ و افزودن/ویرایش/مرتب‌سازی sectionها و گام‌ها در draft جاری.
- JSON محدود و سخت‌گیرانه در CLI، جلوگیری از کلید تکراری/ناشناخته، بررسی ورودی‌های عددی finite و غیرمنفی، و الزام واحد صریح برای مقدار عمق، وزن گل و هزینه. هیچ تبدیل یا استنتاج واحدی صورت نمی‌گیرد.
- stale-write token برای ویرایش، checklist ساختاری برای submission، ثبت سابقهٔ review و تفکیک attribution مؤلف/بازبین. رشتهٔ actor هویت احراز‌شده نیست.
- revision جدید DRAFT با حفظ revision پیشین، و مقایسهٔ revisionها به‌صورت فقط‌خواندنی.
- migration جدید با دو جدول فرزند section و step در inventory تست migration.

## تست‌های اجراشده

پس از آخرین اصلاحات authoring/CLI و migration، `ruff check src tests migrations` موفق بود، `ruff format --check src tests migrations` اعلام کرد ۳۶۹ فایل قالب‌بندی‌شده‌اند، و `compileall` موفق بود. تست‌های هدفمند authoring/CLI/migration و snapshot با دستور `pytest -o addopts='' -q ...` موفق شدند: **۲۳ passed در ۷٫۶۶ ثانیه**. همچنین ۹ suite مرتبط با CLI، migration، engineering lessons، plan/actual، program promotion و domain review در اجرای جداگانه بدون failure پایان یافتند.

- `tests/integration/test_program_procedure_authoring.py`
- `tests/integration/test_program_procedure_cli.py`
- `tests/integration/test_migrations.py`
- `tests/unit/test_cli_surface.py`

اجرای کامل repository با `pytest -q` پس از ۳۰ دقیقه در حدود ۵۳٪ timeout شد؛ خروجی میانی چند failure marker داشت اما چون اجرای کامل به summary نرسید، شناسه و علت آن‌ها مشخص نیست و pass کامل ادعا نمی‌شود. تلاش‌های محدود برای تشخیص از cache و `-x` نیز در محدودیت زمانی محیط به summary نرسیدند. CI لینوکس/ویندوز و replay روی SHA نهایی هنوز لازم‌اند. در این نوبت migration روی دیتابیس واقعی کاربر اجرا نشد و پذیرش فیزیکی Windows نیز انجام نشده است.

## محدودیت‌ها و وضعیت qualification

- UI قابل‌ویرایش Qt برای Program/Procedure، templateها و export کنترل‌شدهٔ DOCX/PDF هنوز پیاده‌سازی نشده‌اند.
- اتصال دقیق نسخهٔ Program به نسخهٔ Procedure در عملیات اجرایی و نمایش applicability هنوز پیاده‌سازی نشده است.
- تنها محاسبهٔ اجرایی مهندسی مجاز همچنان NPT roll-up V1 است. محاسبات trajectory، hydraulics، ریسک/AFE و روش‌های دیگر qualified نیستند.
- SQLite تنها backend qualified است؛ PostgreSQL **NOT QUALIFIED**.
- authentication، امضای دیجیتال و هویت واقعی actor موجود نیست. ثبت attribution یا technical review به‌تنهایی approval معتبر سازمانی یا تأیید مهندسی نیست.
- OCR/MinerU واقعی **NOT RUN / NOT QUALIFIED**؛ پذیرش فیزیکی Windows **NOT RUN**؛ موارد قراردادی M2–M5 و O-02/O-03 همچنان owner-gated هستند.

## Git و CI

اولین پیاده‌سازی به‌صورت commit `8add9067fd3d4565edd964146bc8cfc330a9e5aa` به branch مجاز push شد. Exact-SHA workflow Linux `44031899454` شکست خورد: چند تست migration/doctor و Windows run-card هنوز head `0026` را انتظار داشتند و pin-check به‌علت انتشار نسخه‌های جدید در PyPI گزارش drift داد (ابتدا `SQLAlchemy 2.1.8` در برابر metadata `2.1.9`). Historical replay job در همان dispatch موفق شد؛ این موفقیت، CI محصول را سبز نمی‌کند. Windows run همان SHA در زمان این گزارش شکست run-card داشت که علتش stale expected revision بود.

اصلاحات جاری migration assertions، `METADATA_REVISION`، `WINDOWS_ACCEPTANCE_RUN_CARD.ps1` و docs به `0027` منتقل شده‌اند. Lockهای `runtime`, `development`, `ui`, و `audit` از طریق ابزار رسمی `tools/constraints.py --set` به metadata جاری تجدید و همهٔ پنج `--check`ها پاس شده‌اند؛ این یک تصمیم صریح pin refresh است. اصلاحات هنوز commit/push و CI exact-SHA نهایی نشده‌اند؛ نتایج SHA قبلی نباید به آن‌ها منتقل شوند.

در clone این مرحله `out/performance.json` حاضر است و به‌عنوان فایل tracked در base commit دیده می‌شود؛ با انتظار پیشین مبنی بر حذف محلی unstaged مطابقت ندارد. این فایل عمداً تغییر/حذف، stage یا commit نشده و محتوایش باز نشده است. وضعیت clone اصلی از این مشاهده استنتاج نمی‌شود.

## اسناد

- نقشهٔ اصلی و مرجع برنامه: [`ENGINEERING_PROGRAM_PROCEDURE_MASTER_PLAN.md`](../ENGINEERING_PROGRAM_PROCEDURE_MASTER_PLAN.md)
- راهنمای CLI و محدودیت‌های اجرایی: [`PROGRAM_PROCEDURE_AUTHORING_CLI.md`](../PROGRAM_PROCEDURE_AUTHORING_CLI.md)
- مرور کلی README اصلاح شده تا وضعیت این برش را از baseline تأییدشده جدا کند.

این گزارش عمداً ادعای کامل‌بودن workflow، اجرای migration عملیاتی، qualification نهایی، یا تولید برنامه/دستور حفاری ایمن را مطرح نمی‌کند.

## Addendum: انتقال به checkout branch فعال و Windows stale-token fix

برای ادامهٔ کار روی checkout الزامیِ همین branch (base `317c55365319af61138000664293ae2d84e27a6e`)، feature files و migration `0027` از implementation commit `8add906` به worktree منتقل شدند؛ schema/test/constraint corrections مرتبط از `b0adfe6` نیز گزینشی اعمال شدند. Branch عوض نشد و تغییرات قبلیِ متعدد در worktree حفظ شدند.

Windows CPython 3.11 failure در test مربوط به stale-write از یک فرض نامعتبر دربارهٔ پیشروی wall-clock می‌آمد. اکنون `_touch` در `EngineeringAuthoringService`، اگر `datetime.now(UTC)` از `updated_at` موجود جلوتر نباشد، token را دقیقاً دست‌کم یک microsecond جلو می‌برد؛ همهٔ مسیرهای content/section/target/step، submit، review و decision از آن استفاده می‌کنند. Regression test یک timestamp آینده‌دار نسبت به ساعت اجرای تست قرار می‌دهد تا clock non-advancement/backward را قطعی شبیه‌سازی کند؛ انتظار exception را حذف یا skip نکرده است.

نتیجهٔ انتخابی روی checkout فعال: **107 passed, 1 skipped** در Program/Procedure authoring، CLI، migration/history expectations، backup/intake smoke و CLI-surface tests. Skip برابر pass نیست. Ruff check روی فایل‌های feature/test منتخب نیز موفق شد. این نتایج local هستند؛ exact-SHA Linux/Windows CI برای این worktree هنوز **NOT RUN** است. Physical Windows acceptance نیز **NOT RUN**. هنوز stage/commit/push انجام نشده است.
