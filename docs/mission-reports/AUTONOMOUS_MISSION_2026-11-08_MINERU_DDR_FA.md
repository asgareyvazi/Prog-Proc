# گزارش اختتام مأموریت audit و qualification — Prog-Proc

**تاریخ گزارش:** 2026-11-08 (UTC)<br>
**شاخهٔ مجاز:** `arena/5957b76d-prog-proc`<br>
**نوع نتیجه:** ممیزی و شواهد محدود؛ نه پذیرش production و نه رفع owner gateها

## خلاصهٔ اجرایی

مأموریت در مسیرهای مستندات current-facing، رقابت‌پذیری authoring/review در DDR، مرز MinerU 4، SQLite/backup و CI انجام شد. نتیجهٔ کلی همچنان **PARTIALLY QUALIFIED — PRE-ALPHA** است: SQLite تنها backend واجد qualification است؛ محصول local-first و single-user است؛ PostgreSQL، احراز هویت/چندکاربره، کاربری عمومی و production qualification ادعا نمی‌شوند.

دو commit مستقل برای تغییر کد ثبت شد:

| SHA | تغییر | exact-SHA CI |
|---|---|---|
| `43afa1f2873356e2c5e73d1b8b0491374d3b6cc5` (C1) | پشتیبانی مرز MinerU 4 با `mineru-kit`/`docvortex.middle` و تست رقابت DDR بین processهای SQLite | Linux run [`42381352913`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42381352913): 9/9 job موفق؛ Windows run [`42381353106`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42381353106): 14/14 job موفق. اعداد پایین‌تر ثبت شده‌اند. |
| `8cc4c0fec923329f9371ddd1ce222a5e8ca0df80` (C2) | رفع ثبت کاذب `layout_pdf="."` با nullable کردن artifact اختیاری و assertion مرزی | Linux run [`42449344642`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42449344642) و Windows run [`42449344633`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42449344633) برای همین SHA هر دو **Success** شدند: Linux 9/9 job و Windows 14/14 job. اعداد full-suite و پلتفرم‌ها در بخش CI ثبت شده‌اند. |

C1 و C2 شواهد جدا دارند؛ هیچ نتیجه‌ای از SHA دیگری به آن‌ها منتقل نشده است. Commit مستندات و همین گزارش نیز جداگانه exact-SHA CI خواهد داشت. CI به‌تنهایی تصمیم مالک، قانونی، مهندسی یا پذیرش محیط فیزیکی را نمی‌گیرد.

## محدوده و قواعد شواهد

- تغییرات current-facing به README، مرجع زمینه، capability matrix، تصمیم‌های محصول، راهنمای Windows و افزودهٔ current-runtime در `DECISIONS.md` محدود شد. گزارش‌های تاریخی و ردیف‌های taxonomy قدیمی بازنویسی نشدند.
- عددهای taxonomy همچنان دقیقاً **26 class: 14 `END_TO_END_CERTIFIED`, 7 `KNOWLEDGE_SUPPORTED`, 5 `EXTRACT_ONLY` و 12 handler نام‌دار** هستند. MinerU/Intake/DDR کلاس یا handler ارتقای document جدید اضافه نمی‌کنند.
- mock و stand-in فقط boundary test هستند، نه اجرای MinerU واقعی. آزمون‌های grouped، single-process full-suite، و CI full-suite به‌عنوان شواهد متفاوت ثبت شده‌اند. Skipها PASS محسوب نشده‌اند.
- برای هر code SHA فقط CI همان SHA معتبر است. اجرای محلی timeout‌شده موفق شمرده نشده و از آن هیچ تعداد نهایی test استخراج نمی‌شود.

## DDR concurrency و چرخهٔ review

C1 یک رقابت adversarial برای file-backed SQLite اضافه کرد. دو process با `multiprocessing` از نوع `spawn` راه‌اندازی می‌شوند؛ هرکدام URL پایگاه‌داده را جداگانه باز کرده و engine/session factory و `DdrAuthoringService` مستقل می‌سازند. هر دو یک freshness token واحد را پیش از آزادشدن process barrier می‌خوانند. نتیجهٔ مورد انتظار دقیق است: یک save برنده و یک save با stale-token conflict بازمی‌گردد؛ نه دو commit.

تست state پایدار را نیز پس از رقابت دوباره می‌خواند: summary و activity متعلق به برنده است، activity تکراری ساخته نشده، فقط یک audit event از نوع save ثبت شده، و `PRAGMA foreign_key_check` خالی است. سناریوهای submit، revision و review نیز با preconditionهای snapshot و serviceهای مستقل آزموده می‌شوند؛ review race فعلی از دو `ReviewActionService` مستقل استفاده می‌کند. عملیات غیر-DDR همچنان در تست‌های `ReviewActionService` پوشش داده شده‌اند. actor string صرفاً مقدار audit اعلامی است و احراز هویت یا استقلال reviewer را ثابت نمی‌کند.

آزمون‌های SQLite موجود نیز WAL، foreign keys، busy timeout، خواندن هم‌زمان، transaction rollback و invariant نسخهٔ current را بررسی می‌کنند. برای workspace backup/restore، publication race و جلوگیری از overwrite نیز تست‌ها وجود دارند. این شواهد محلی و CI همچنان به SQLite محدودند؛ پشتیبانی PostgreSQL استنتاج نمی‌شود. آزمون‌های migration/doctor/destructive در test databaseهای موقت و workspaceهای disposable ماندند؛ هیچ پایگاه‌دادهٔ مشترک یا تولیدی تغییر داده نشد. Backup/archive محلی، رمزگذاری و disaster recovery تولیدی نیست.

## MinerU 4: قرارداد، اجرای واقعی و provenance

### قرارداد و مرز اجرایی

C1 مسیر CLI را به نسل 4 (`mineru-kit`) و خروجی/ورودی `docvortex.middle` محدود می‌کند. اعتبارسنجی شکل قرارداد و تنظیمات در boundary انجام می‌شود؛ پیاده‌سازی به‌طور silent به HTTP attachment API نامتوافقِ نسل دیگر fallback نمی‌کند. HTTP تنها در صورت قرارداد legacy JSON قابل اثبات از OpenAPI قابل‌قبول است. Runtime سنگین MinerU به dependency core تبدیل نشده است و این integration در startup/doctor به‌صورت غیرمنتظره مدل، شبکه یا runtime را probe نمی‌کند.

خروجی استخراج‌شده به‌تنهایی operational record نمی‌سازد. منطق دامنه، provenance، source locator (صفحه، block/table و مختصات)، independence review و fail-closed حفظ می‌شوند. هیچ گزارش یا مدل محرمانه‌ای upload نشد.

### اجرای runtime واقعی

MinerU `4.1.7` جدا از محیط اصلی، روی CPU با `flash` backend نصب و از مسیر واقعی `MinerUExtractor` روی دو PDF مصنوعی اجرا شد:

1. PDF دیجیتال سه‌صفحه‌ای: 898 نویسه استخراج‌شده؛
2. PDF جدولی یک‌صفحه‌ای: یک جدول 3×3 با locator صفحه/block و bounding box fraction.

زمان‌های مشاهده‌شده حدود 1.5s و 1.9s در sandbox بوده‌اند؛ این benchmark یا تضمین کارایی نیستند. این اجرا، smoke/سازگاری parser است، نه OCR یا engineering validation. دریافت مدل OCR از ModelScope پیش از inference شکست خورد؛ بنابراین OCR inference، accuracy و کیفیت روی corpus نماینده **NOT RUN / NOT VALIDATED** هستند.

اجرای واقعی همچنین نشان داد مقدار پیش‌فرض `Path()` به‌اشتباه به metadata به‌شکل `layout_pdf="."` تبدیل می‌شد، درحالی‌که artifactی وجود نداشت. C2 مقدار را nullable کرد و boundary assertion اضافه شد تا کلید metadata کاذب ثبت نشود. اجرای واقعی پس از اصلاح نیز نبود آن metadata و پاک‌سازی scratch directory را تأیید کرد. این اصلاح در C2 قرار دارد؛ هر دو exact-SHA workflow برای همان commit بعداً با conclusion موفق خاتمه یافتند (Linux `42449344642`، Windows `42449344633`).

مجوز upstream در این audit به‌عنوان Apache 2.0 همراه با شروط افزوده ثبت شد؛ این متن **نظر حقوقی نیست**. شروط نسخهٔ دقیق توزیع‌شده و intended deployment باید جداگانه توسط مسئول مناسب بازبینی شوند. وابستگی یا مدل سنگین به core اضافه نشده است.

## آزمون‌های محلی و CI

### C1 — exact-SHA کامل

- Linux run `42381352913`: **9/9 job موفق**. CPython 3.11 و 3.14 هرکدام 2,795 collected، 2,734 passed، 61 skipped، 0 failed. Ubuntu 26.04 × CPython 3.14: 2,795 collected، 2,794 passed، 1 skipped، 0 failed.
- Windows run `42381353106`: **14/14 job موفق**. CPython 3.11 و 3.14 هرکدام 2,795 collected، 2,732 passed، 63 skipped، 0 failed. Windows offscreen UI در هر دو runtime 29/29؛ شش stability lane همگی 15/15 تلاش را گذراندند. Clean-install و PowerShell 5.1/7 run-card checks موفق شدند.
- دانلود log/artifactهای pytest ویندوز EOF داد؛ علت همهٔ skipها مستقل بازیابی نشد. شمارش‌ها ثبت‌اند، اما skip reason ناشناخته حدس زده نمی‌شود.
- این شش stability lane با 15 تلاش و timeout 45 دقیقه حفظ شدند. selector، assertion، classifier و روش retry برای سبزکردن تغییر نکردند.

### C2 — exact-SHA مستقل

Runهای Linux `42449344642` و Windows `42449344633` برای `8cc4c0fec923329f9371ddd1ce222a5e8ca0df80` هر دو از GitHub به وضعیت terminal **Success** رسیدند. Linux مجموعاً 9/9 job و Windows 14/14 job موفق داشت؛ این verdict فقط به C2 تعلق دارد.

- Linux CPython 3.11، job [`128635009217`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42449344642/job/128635009217)، و 3.14، job [`128635009240`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42449344642/job/128635009240)، هرکدام 2,795 collected، 2,734 passed، 61 skipped، 0 failed. Ubuntu 26.04 / CPython 3.14 full-suite job [`128635009255`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42449344642/job/128635009255): 2,795 collected، 2,794 passed، 1 skipped، 0 failed. Ubuntu compatibility job [`128635009198`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42449344642/job/128635009198) نیز موفق بود. این job-ID تفکیک، full-suite عادی CPython 3.14 را از lane جداگانهٔ Ubuntu 26.04 متمایز می‌کند.
- Windows CPython 3.11، job [`128635009309`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42449344633/job/128635009309)، و 3.14، job [`128635009314`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42449344633/job/128635009314)، هرکدام 2,795 collected، 2,732 passed، 63 skipped، 0 failed. Windows offscreen Qt: 29/29 در هر دو runtime؛ شش stability lane هرکدام 15/15 تلاش؛ clean-install و PowerShell 5.1/7 behavioral tests موفق.
- Windows run-card behavioral checks نیز موفق بودند. Skipها به PASS تبدیل نمی‌شوند؛ annotationها شمارش کل را نشان می‌دهند، نه تشریح مستقل هر skip reason.
- هر دو run ضمیمهٔ aggregation با یک non-blocking هشدار deprecation داشتند: `actions/download-artifact` به Node.js 24 هدایت شده با وجود هدف‌گیری Node.js 20. این هشدار علت شکست نبود و در نتیجهٔ run اختلال ایجاد نکرد.

### اجرای محلی پس از C2

گروه آزمون‌های زیر اجرا شد: MinerU boundary، settings، DDR authoring، SQLite concurrency، review service، SQLite health، doctor storage، backup، workspace boundary، product decisions، Windows testing docs و report integrity. نتیجه **157 passed, 2 skipped, 0 failed** بود. هر دو skip فقط shallow-history check در `test_report_integrity.py` بودند، چون SHAهای مبنا قبل از مرز fetch قرار دارند؛ skipها PASS نیستند. اجرای مجزای MinerU boundary برابر 18 passed بود.

پس از ایجاد همین گزارش، validation تکمیلی برای report integrity، product decisions و Windows testing docs نیز اجرا شد: **19 passed, 2 skipped, 0 failed**؛ همان دو skip، shallow-history checks گزارش‌اند. `ruff check`, `ruff format --check`, `mypy` (44 source files)، `compileall`, exception inventory check، Alembic head (`0026`)، `git diff --check` و `git diff --cached --check` موفق شدند. Doctor فقط در workspace/DB disposable بررسی شد. اجرای یک‌تکهٔ `pytest -q` به سقف 30 دقیقه رسید و حدود 90% بدون summary متوقف شد: **NOT PASS**؛ از آن نه موفقیت و نه عدد تست نهایی ادعا نمی‌شود. گروه آزمون 157تایی جایگزین آن نیست.

### UI و Windows فیزیکی

Qt محلی به‌علت نبود `libGL.so.1` قابل اجرا نبود و **SKIPPED** است، نه PASS. CI offscreen Qt صرفاً رفتار خودکار را پوشش می‌دهد و معادل display workstation نیست. چک‌لیست فیزیکی Windows شانزده‌مرحله‌ای **NOT RUN**؛ علت root cause timeout تاریخی Intake/registration/refresh هنوز اثبات نشده است.

## وضعیت مستندات و حفاظت از تاریخچه

- README و اسناد current-facing به SHA/run متصل شدند؛ وضعیت فعلی از سابقه جدا شده است.
- ردیف‌های matrix و گزارش‌های تاریخی حذف یا بازنویسی نشدند؛ فقط توضیح بالای matrix آن را صریحاً historical معرفی می‌کند و شمارش‌ها حفظ شده‌اند.
- در `PRODUCT_DECISIONS_REQUIRED.md` gateهای باز همچنان owner-gated هستند؛ green CI آن‌ها را نمی‌بندد.
- `out/performance.json` که از قبل حذف‌شده بود، عمداً حذف باقی ماند و stage/commit نشد. `stash@{0}` حفظ شد.
- کار فقط روی `arena/5957b76d-prog-proc` انجام شد. `origin/main` روی `85122e1a98087db4495b820a9a0ffae9009b7819` باقی ماند و تغییر داده نشد.

## موارد باز و مالک‌محور

این مأموریت موارد زیر را تصویب یا qualify نمی‌کند:

- M2–M5 و O-02/O-03؛
- PostgreSQL و migration/doctor/destructive test روی پایگاه‌دادهٔ واقعی؛
- auth، هویت reviewer و multi-user؛
- risk/AFE/currency و هر تصمیم مهندسی یا مالی؛
- OCR accuracy، تولیدی‌بودن MinerU، corpus validation و intended-use license review؛
- backup سازمانی/DR و archive رمزگذاری‌شده؛
- display acceptance فیزیکی Windows؛
- public launch، production deployment یا public/production qualification.

تا تعیین owner و اجرای gateهای مربوط، رفتار fail-closed حفظ می‌شود. هیچ extraction textای به‌طور خودکار operational record یا تأیید مهندسی نمی‌شود.

## وضعیت تحویل این نسخه

C1 و C2 روی branch مجاز push شده‌اند و هر دو exact-SHA CI کامل و سبز است؛ C2 Linux run `42449344642` و Windows run `42449344633` فقط به SHA `8cc4c0fec923329f9371ddd1ce222a5e8ca0df80` تعلق دارند. این گزارش و اسناد current-facing در commit مستنداتی جداگانه قرار می‌گیرند که exact-SHA CI مستقل خودش را می‌خواهد؛ نتیجهٔ C1/C2 به آن SHA منتقل نمی‌شود. وضعیت working tree، stash، remote SHA و protected deletion در پایان commit مستندات جداگانه کنترل می‌شود.
