# الحاقیهٔ qualification دقیق SHA `e76d4d8`

- **تاریخ:** ۲۰۲۶-۱۱-۰۷ (UTC)
- **شاخه:** `arena/5957b76d-prog-proc`
- **SHA کد، تست و مستندات اجراشده:** `e76d4d8c6aab0d8da5b9b11ba656bd80a67d3e51`
- **حکم qualification این SHA:** **PARTIAL** — Linux CI سبز؛ Windows workflow به‌علت سقف زمان یک job لغو شد.
- **حکم محصول:** **PARTIALLY QUALIFIED — PRE-ALPHA**؛ این الحاقیه release readiness یا پذیرش فیزیکی Windows را اعلام نمی‌کند.

> تمام نتایج CI این فایل فقط به SHA `e76d4d8c6aab0d8da5b9b11ba656bd80a67d3e51` مربوط‌اند. نتایج runهای SHAهای دیگر به آن منتقل یا با آن جمع نشده‌اند. افزودن همین الحاقیه commit بعدی می‌سازد؛ نتیجهٔ آن commit باید جداگانه ثبت شود.

## نتیجهٔ دقیق GitHub Actions

| Workflow | Run | SHA تأییدشده | وضعیت نهایی | نتیجهٔ jobها |
|---|---:|---|---|---:|
| Linux (`ci.yml`) | [42348862362](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42348862362) | `e76d4d8c6aab0d8da5b9b11ba656bd80a67d3e51` | `completed / success` | **۹/۹ success** |
| Windows (`ci-windows.yml`) | [42348862485](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42348862485) | `e76d4d8c6aab0d8da5b9b11ba656bd80a67d3e51` | `completed / cancelled` | **۱۳ success، ۱ cancelled، ۰ failure** |

GitHub Actions metadata با `gh run view` به‌طور مستقل run، SHA، conclusion و تعداد jobها را نشان داد. Run ویندوز به‌علت لغو job `windows stability (history, cp3.14)` با شناسهٔ `128332475918` سبز نیست. annotation صریح آن می‌گوید job از **حداکثر زمان ۴۵ دقیقه** عبور کرد؛ step مربوط به repeated workbench qualification لغو و step بررسی مستقل evidence با exit code 1 خاتمه یافت. این یک **timeout/cancellation** است، نه مدرک یک assertion شکست‌خورده یا علت‌یابی failure تاریخی. سقف workflow در این مأموریت تغییر نکرد.

### شمارش‌های full-suite، بدون تجمیع بین laneها

| سیستم / runtime | Job | Collected | Passed | Skipped | Failed | Errors | Runtime (s) |
|---|---|---:|---:|---:|---:|---:|---:|
| Linux / CPython 3.11 | `128332475492` | 2,781 | 2,720 | 61 | 0 | 0 | 2,808.819 |
| Linux / CPython 3.14 | `128332475500` | 2,781 | 2,720 | 61 | 0 | 0 | 1,821.707 |
| Ubuntu 26.04 compatibility / CPython 3.14 | `128332475314` | 2,781 | 2,780 | 1 | 0 | 0 | 1,228.098 |
| Windows / CPython 3.11 | `128332476141` | 2,781 | 2,718 | 63 | 0 | 0 | 3,405.854 |
| Windows / CPython 3.14 | `128332475969` | 2,781 | 2,718 | 63 | 0 | 0 | 3,318.393 |

این جدول شمارش‌های مستقل هر lane را حفظ می‌کند؛ skipها PASS نیستند و totals laneها با هم جمع نشده‌اند. هر دو Windows full-suite job با نتیجهٔ success تمام شدند؛ لغو workflow ناشی از job stability جداگانه بود.

### سایر نتایج قابل انتساب به همین SHA

- Linux run: **۹/۹ job موفق**؛ Windows run: ۱۳ job موفق از جمله run-card PowerShell، UI offscreen برای CPython 3.11/3.14، clean-install smoke و full-suiteهای هر دو runtime. یک Windows history stability job به علت سقف زمان لغو شد.
- Windows run-card behavioral tests در Windows PowerShell 5.1.20348.5773 و PowerShell 7.6.9 موفق بودند. این رفتار آزمایشگاهی است، نه اجرای دستی **run-card شانزده‌مرحله‌ای** روی workstation.
- Windows offscreen UI هر دو runtime: **۲۹/۲۹ passed، صفر failed/error/skip**. Qt offscreen معادل پذیرش فیزیکی نمایشگر Windows نیست.
- در full-suiteهای CI برای Windows هر دو runtime، annotationها نشان می‌دهند dependency audit بدون آسیب‌پذیری شناخته‌شده گزارش شد. Advisory dependency audit محلی اجرا نشده است.
- اجرای کامل محلی روی همین source tree: **۲٬۷۱۸ passed، ۶۳ skipped، صفر failed/error از ۲٬۷۸۱**. پس از آخرین reconcile مستندات، مجموعهٔ کنترل مستندات/CLI نیز **۴۲ passed، صفر failed/error** داشت.
- پس از آخرین ویرایش‌ها Ruff check/format (۴۳۴ فایل)، mypy، inventory exception با generator رسمی و `--check`، constraints audit check، CLI surface check و `git diff --check` موفق بودند.
- دانلود ZIP artifactهای این run از GitHub Actions در این محیط تکمیل نشد؛ مقصد download به `productionresultssa17.blob.core.windows.net` با `EOF` شکست خورد. بنابراین فقط annotationها و metadata صفحه/API به‌عنوان مدرک استفاده شده‌اند؛ محتوای ZIP به‌صورت مستقل ادعا یا بازبینی نشده است.

## مرز ادعا دربارهٔ Windows تاریخی

Failure تاریخی test `test_intake_review_registration_and_rejection_run_as_distinct_ui_transitions` روی SHA `ff4afddba0d5f319b0f43341dcaed13991b4dd3d` و run `41841625041` در run حاضر بازتولید نشد؛ regression هدف در suite جدید اجرا شده و ۲۹/۲۹ UI offscreen موفق شد. عدم بازتولید، علت ریشه‌ای را اثبات نمی‌کند. وضعیت دقیق آن همچنان:

**NOT REPRODUCED — ROOT CAUSE UNKNOWN**

هیچ نتیجه‌ای از timeout در `windows stability (history, cp3.14)` به آن failure نسبت داده نمی‌شود. این دو مورد جدا هستند. علت‌یابی مستقل دسترسی غیرمجاز native در مسیر shutdown فقط در محدوده و با شواهد ثبت‌شدهٔ ADR-61 توصیف شده است؛ آن سابقه، اثبات علت failure مربوط به intake/review نیست.

## ثبت قابلیت‌ها و مرز تصمیم‌ها

- Commit `e76d4d8` قابلیت manual DDR CLI و revision freshness guard را با تست و مستندات مرتبط اضافه کرد.
- milestoneهای M2 provider، M3 vector/embedding، M4 Skills runtime، M5 calculation جدید و O-03 مربوط به حذف/تغییر source/code برای Lesson همچنان owner-gated هستند؛ این commit آن‌ها را پیاده‌سازی یا policy محصول جدیدی اختراع نمی‌کند.
- PostgreSQL، auth/multi-user، production DR، risk/currency/AFE و physical acceptance خارج از qualification این runها هستند.
- MinerU اجرا/اثبات نشده و mutation score تولید نشده است.
- آزمون محلی Qt به‌علت نبود `libGL.so.1` قابل اجرا نبود؛ شواهد UI فوق فقط از runnerهای Windows offscreen است.
- `out/performance.json` حذف محافظت‌شدهٔ محلی است و restore، stage یا commit نشده؛ stash list خالی ماند.

## خاتمهٔ مهندسی و انتشار

با شواهد فعلی، **Linux CI دقیق SHA سبز است**؛ **Windows CI دقیق SHA سبز نیست**، چون workflow در نتیجهٔ timeout یک job cancelled شد، هرچند ۱۳ job دیگر از جمله دو full-suite ویندوز موفق‌اند. بنابراین qualification بین‌سیستمی کامل/سبز اعلام نمی‌شود. این نتیجه به‌تنهایی release readiness نیست: محصول همچنان **PARTIALLY QUALIFIED — PRE-ALPHA** و انتشار production/public **NOT READY / NOT AUTHORIZED** است. برای رفع وضعیت Windows، شواهد لازم artifact همان job و تصمیم مجاز دربارهٔ زمان‌بندی/اجراست؛ این الحاقیه timeout را به‌عنوان علت ریشه‌ای تست یا Windows failure تاریخی تعبیر نمی‌کند.
