# گزارش نهایی مأموریت مهندسی و توسعهٔ امن — Prog-Proc

**تاریخ اجرای ثبت‌شده:** 2026-10-20 UTC (تاریخ سیستم مأموریت)
**شاخهٔ مجاز:** `arena/5957b76d-prog-proc`
**وضعیت کلی:** کار مهندسی در محدودهٔ مصوب انجام، بازبینی، commit و push شد. **OCR واقعی، پذیرش فیزیکی Windows و تصمیم‌های owner-gated همچنان اجرا/تأیید نشده‌اند.** محصول **Pre-Alpha** باقی می‌ماند.

> فایل traceability پیش از پیاده‌سازی تهیه شد و بعداً فقط برای اصلاح خطای ثبت baseline به‌روزرسانی گردید: [`ENGINEERING_EXPANSION_TRACEABILITY_2026-11-08.md`](ENGINEERING_EXPANSION_TRACEABILITY_2026-11-08.md). گزارش‌های مأموریت پیشین تاریخی‌اند و شواهدشان به SHAهای قبلی محدود است.

## 1. خلاصهٔ مدیریتی و وضعیت پذیرش

| حوزه | نتیجه | مرز ادعا |
|---|---|---|
| MinerU transport و قرارداد HTTP | **DONE — کد و آزمون‌های مرزی** | Auto وقتی CLI معتبر انتخاب شده است به HTTP اضافی نمی‌رود؛ mode صریح جدا و fail-closed است؛ HTTP فقط قرارداد legacy multipart + JSON را می‌پذیرد. HTTP زنده با سرویس واقعی در این مأموریت اجرا نشد. |
| آثار MinerU، provenance و normalization | **DONE — کد و آزمون‌های مرزی** | خروجی موقت هر اجرا محدود و پاک‌سازی قابل‌گزارش است؛ مسیر scratch/فایل منبع در metadata ثبت نمی‌شود؛ confidence، bbox، متن خالی و خروجی ناقص جعل یا بی‌صدا پذیرفته نمی‌شوند. |
| readiness/qualification CLI و fixture OCR مصنوعی | **DONE — ابزار؛ inference واقعی NOT RUN** | `drillintel mineru validate --json` تفکیک READY/NOT_READY، مدل و OCR را نگه می‌دارد. Fixture/ground truth و CER/WER مصنوعی وجود دارد؛ هیچ‌کدام معیار دقت OCR واقعی یا میدانی نیست. |
| افزودن یک workflow امن و قابل‌استفاده | **DONE** | `drillintel config validate --json` پیکربندی مؤثر را بدون بازکردن workspace/database یا probe شبکه اعتبارسنجی می‌کند. |
| concurrency نویسندگی DDR | **DONE — process-level SQLite assurance** | آزمون‌ها process و connection مستقل، SQLite فایل‌محور موقت، timeout/join محدود، خروج تمیز worker و invariantهای persisted را به‌کار می‌برند. این کار authentication یا هویت مستقل actor را ایجاد نمی‌کند. |
| مقایسهٔ revision در DDR | **DONE — read-only service/data/UI path** | predecessor/successor و اختلاف فیلدها/فعالیت‌ها و provenance نمایش داده می‌شود؛ ترتیب/متن نویسنده حفظ است؛ نمایش، approval یا mutation نیست. اجرای UI محلی در sandbox به‌علت نصب‌نبودن PySide6 **SKIPPED** بود؛ CI Qt-enabled offscreen موفق شد. |
| CI Linux و Windows برای کد نهایی مأموریت | **PASS روی exact SHA `075d5919…`** | runها در جدول §3 آمده‌اند؛ هیچ evidence از SHA دیگر به این SHA منتقل نشده است. |
| OCR واقعی / دقت عمومی OCR | **NOT RUN / NOT QUALIFIED** | MinerU در host نصب نبود؛ مدل بررسی نشد؛ inference واقعی و metrics انجام نشد. |
| پذیرش فیزیکی Windows | **NOT RUN** | CI offscreen و stability automation معادل desktop/display فیزیکی یا چک‌لیست 16 مرحله‌ای مالک نیست. |
| PostgreSQL، M2–M5، O-02/O-03، auth/multi-user و تولید | **NOT QUALIFIED / OWNER-GATED / OUT OF SCOPE** | بدون قرارداد، مجوز و شواهد مالک تغییر یا ادعا نمی‌شوند. |

هیچ migration جدیدی لازم نبود؛ تنها Alembic head همچنان `0026` است. Taxonomy ثابت ماند: **26 کلاس، 14 `END_TO_END_CERTIFIED`، 7 `KNOWLEDGE_SUPPORTED`، 5 `EXTRACT_ONLY` و 12 handler نام‌دار**. SQLite تنها backend واجد qualification است. هیچ وزن مدل، credential، سند DDR محرمانه یا دادهٔ کاربر به commitها افزوده نشد.

## 2. تغییرات پیاده‌سازی

### 2.1 MinerU: transport، قرارداد HTTP و artifact lifecycle

- مسیر انتخاب transport به client منتقل شد تا نتیجهٔ discovery دوباره با تصمیم ناسازگار بازتفسیر نشود. در `auto`، وجود CLI سازگار به probe HTTP زائد منتهی نمی‌شود؛ انتخاب‌های صریح `cli` و `http` با هم fallback نمی‌کنند و conflict، خطا می‌دهد.
- endpoint HTTP دیگر مقدار پیش‌فرض loopback ندارد. مسیر parse قابل‌تنظیم است ولی تنظیمات، مسیر absolute، بدون query/fragment، backslash، control character یا dot-segment را الزام می‌کند؛ درصد-encoding نیز decode و بررسی می‌شود.
- OpenAPI discovery محدود و fail-closed است: `$ref`های داخلی با سقف حل می‌شوند، multipart باید فیلد فایل سازگار داشته باشد و پاسخ باید JSON object باشد. قرارداد این adapter همان legacy `/file_parse` multipart + JSON است، نه async/task APIای که ZIP attachment برمی‌گرداند.
- پاسخ HTTP با status نامعتبر، redirect، content type غیر JSON، envelope/result اشتباه، نتیجهٔ مبهم برای فایل ارسالی، JSON ناقص یا بدون محتوای قابل‌خواندن رد می‌شود. فایل/ZIP/attachment به‌عنوان parse result پذیرفته نمی‌شود. مقدارهای `middle_json`, `content_list` و Markdown از نظر نوع و تهی‌بودن بررسی می‌شوند.
- artifactهای CLI فقط در scratch اختصاصی همان اجرا ساخته می‌شوند. retention عمومی/دلخواه حذف شد. پاک‌سازی در success و failure انجام می‌شود و metadata وضعیت پاک‌سازی یا خطای آن را نگه می‌دارد، نه مسیر scratch را. مسیر منبع/temporary از command/stderr/error ثبت‌شده redaction می‌شود؛ `layout_pdf` جعلی یا dangling path ثبت نمی‌شود.
- normalization ورودی‌های ناقص را به دادهٔ «موفق» تبدیل نمی‌کند: confidence کالیبره‌نشده جعل نمی‌شود؛ مختصات non-finite، boolean یا خارج از قرارداد رد می‌شوند؛ خروجی خالی/خراب fail-closed است و partial parse diagnostics صریح دارد.
- آزمون‌های boundary، schema، media type، redirect و malformed/empty/attachment responses را پوشش می‌دهند. **هیچ MinerU HTTP server واقعی در این مأموریت اجرا یا upload نشده است.**

### 2.2 readiness محلی و OCR مصنوعی

- `drillintel mineru validate --json` readiness را فقط برای CLI محلی گزارش می‌کند و HTTP endpoint را probe نمی‌کند؛ مدل‌ها را inspect/download نمی‌کند و سند کاربر را upload نمی‌کند. Exit code نیز readiness را بازتاب می‌دهد.
- inference مصنوعی فقط با opt-in انجام می‌شود، فقط از مسیر محلی MinerU 4 `mineru-kit`، tier `basic` و `MINERU_MODEL_SOURCE=local`؛ اجرای نامناسب یا runtime غایب، به‌جای metrics ساختگی، `NOT_RUN`/`INFERENCE_FAILED` برمی‌گرداند.
- PNG و ground truth مصنوعی نسخه‌بندی شده‌اند. آزمون Linux/Windows نشان داد خروجی `zlib.compress` می‌تواند بایت‌های متفاوت بسازد؛ در commit اصلاحی از zlib DEFLATE stored-block قطعی استفاده شد و تست PNG حالا header/CRC/IDAT decompression را نیز بررسی می‌کند.
- در این sandbox فرمان واقعی چنین نتیجه‌ای داشت: runtime `NOT_READY` چون `mineru` نصب نبود؛ `model_readiness=NOT_CHECKED`؛ `ocr_qualification=NOT_RUN`؛ metrics برابر `null`. بنابراین OCR دقت‌سنجی نشده است. Mockها و fixture فقط contract/tooling را می‌آزمایند.

### 2.3 DDR concurrency و revision comparison

- آزمون‌های integration concurrency با database فایل‌محور موقت و processهای مستقل/connectionهای مستقل اجرا می‌شوند؛ نتیجه‌ها bounded هستند و workerها باید قبل از پایان test با exit code سالم join شوند. بعد از contention، persisted current revision، activity/operation uniqueness، audit و FK/integrity بررسی می‌شوند.
- مسیرهای مستقل review، freshness/stale-token، FK، rollback و semanticهای candidate/confirmed/rejected حفظ شده‌اند؛ actor string تنها دادهٔ اعلام‌شده است و احراز هویت محسوب نمی‌شود.
- comparison از قابلیت موجود توسعه یافته است، نه مسیر write جدید: predecessor/successor، unchanged/added/removed/modified fieldها و activityها با ترتیب/متن نویسنده نمایش داده می‌شوند. malformed/missing link، Unicode و history حجیم محدود/مشخص می‌شوند. هیچ state mutation، review action، approval indicator یا مقدار مهندسی مشتق‌شده اضافه نشده است.
- UI renderer در این sandbox اجرا نشد، چون `PySide6` نصب نبود؛ testها skip واقعی با دلیل روشن‌اند. GitHub Qt-enabled offscreen و Windows repeated workbench lanes روی exact SHA پاس شدند؛ این physical UI acceptance نیست.

### 2.4 workflow اضافه و مستندات

- `drillintel config validate --json` نتیجهٔ `valid`, overrideها، unknown keys و side-effect scope را چاپ می‌کند؛ اجرای محلی نشان داد `database_opened=false`, `workspace_opened=false`, `services_probed=false` و exit `0`.
- README، `PRODUCT_CAPABILITY.md`، `PROJECT_CONTEXT.md`، تصمیمات و inventory استثناها به وضعیت جاری و مرزهای qualification پیوند داده شدند. گزارش‌های قبلی بازنویسی نشدند؛ صرفاً به‌عنوان سابقهٔ همان SHA/run باقی می‌مانند.
- فایل‌های workflow در این مأموریت تغییر نکردند. هیچ test selector/assertion/classifier تضعیف یا به skip تبدیل نشد. Windows stability با 15 تلاش و 45 دقیقه حفظ شد.

## 3. Git، commits و شواهد exact-SHA

### 3.1 reconciliation و حفاظت از worktree

ابتدای اجرا، checkout محلی در branch مجاز روی `317c55365319af61138000664293ae2d84e27a6e` بود؛ `origin` feature branch از قبل روی `d4b2fc4256b7107ae1c857e64eb871d3dec50b7c` قرار داشت و descendant آن بود. به‌همین علت status اولیه به‌اشتباه 98 فایل modified و 123 path untracked نشان می‌داد. پس از بررسی ancestry، branch ref و index با fast-forward هماهنگ شد، بدون checkout یا نوشتن فایل‌های worktree؛ `main` تغییر نکرد.

`out/performance.json` در tree دوردست `d4b2fc4` tracked بود ولی در worktree آغازین غایب بود. این حذف محافظت‌شده هرگز restore، stage، commit یا پنهان نشد. پس از baseline هم‌تراز، status آن ` D out/performance.json` (unstaged) است. هیچ stashی ساخته نشد؛ stash ابتدا/انتها خالی است. در پایان، همهٔ تغییرات مأموریت commit/push شده‌اند و تنها همین حذف محافظت‌شده در status باقی می‌ماند.

### 3.2 ترتیب commit و CI

| Commit | محتوا / نتیجه | Linux exact-SHA | Windows exact-SHA |
|---|---|---|---|
| `85f6a825d08ce10431f5d4e8081114b65bfab331` | پیاده‌سازی اصلی + docs اولیه. Linux سبز؛ Windows CPython 3.14 با assertion بایت‌های PNG مصنوعی شکست خورد (اختلاف خروجی zlib، نه test skip). | [run `42461904262` — SUCCESS](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42461904262) | [run `42461904273` — FAILURE](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42461904273) |
| `075d5919ebd8a5e600e3c806b73bc560203f52c5` | اصلاح قطعی‌سازی PNG و test decompression؛ هیچ تغییر selector/skip ایجاد نشد. این SHA tip کد مأموریت است. | [run `42466023039` — SUCCESS، تمام jobها](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42466023039) | [run `42466023072` — SUCCESS، تمام jobها](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42466023072) |

شمارش annotationهای full-suite برای SHA `075d5919…`:

| Runner | Collected | Passed | Skipped | Failed |
|---|---:|---:|---:|---:|
| Linux CPython 3.11 | 2,824 | 2,763 | 61 | 0 |
| Linux CPython 3.14 | 2,824 | 2,763 | 61 | 0 |
| Ubuntu 26.04 / CPython 3.14 | 2,824 | 2,823 | 1 | 0 |
| Windows CPython 3.11 | 2,824 | 2,761 | 63 | 0 |
| Windows CPython 3.14 | 2,824 | 2,761 | 63 | 0 |

Skipها هرگز Pass شمرده نمی‌شوند. Annotationها شمارش را می‌دهند، اما تمام skip reasonهای full-suite از GitHub artifact/log endpoint در دسترس نشدند؛ بنابراین علت تک‌تک 61/63 skip ادعا نمی‌شود. UI offscreen Windows جداگانه 29/29 در هرکدام از CPython 3.11 و 3.14، با صفر skip، گزارش شد. هر شش lane پایداری Windows 15/15 تلاش را گذراندند؛ clean-install و PowerShell 5.1/7 run-card نیز موفق بودند. CI «owner pilot» از دادهٔ disposable استفاده می‌کند و جای پذیرش یا امضای واقعی مالک نیست.

### 3.3 وضعیت commit اسناد نهایی

این گزارش و prompt همراه آن در یک commit مستنداتِ جداگانه روی همان branch اضافه می‌شوند. دو workflow Linux و Windows آن commit نیز پس از push برای **همان SHA** بررسی می‌شوند. چون شناسهٔ run پس از ساخته‌شدن commit ایجاد می‌شود، SHA/run نهایی commit اسناد در delivery record نهایی کنار همین گزارش اعلام می‌شود؛ runهای `42466023039` و `42466023072` فقط به SHA کد `075d5919…` تعلق دارند.

## 4. اجرای محلی و کیفیت

روی فایل‌های کاری همین مأموریت:

- `ruff check .` — **PASS**؛ `ruff format --check .` — **PASS، 445 files already formatted**.
- `mypy` — **PASS، 44 source files**؛ `compileall` روی `src tests migrations tools` — **PASS**.
- `tools/exception_review.py --check` — **PASS**؛ 107 broad handlers، همهٔ suppressionها مستند؛ `tests/unit/test_exception_policy.py` برابر 9/9.
- CLI surface snapshot check — **PASS**؛ `alembic heads` — تنها `0026 (head)`.
- constraint audit هر پنج مجموعهٔ `runtime`, `development`, `ui`, `build`, `audit` — **PASS، up to date**.
- sdist و wheel با موفقیت ساخته شدند. setuptools برای شکل فعلی `project.license` هشدار deprecation می‌دهد؛ build fail نشد و این موضوع به‌عنوان cleanup جدا باقی است.
- گروه آزمون هدفمند شامل MinerU boundary/qualification، CLI config، DDR comparison/authoring/CLI، migration، offline migration، exception policy و testهای مرتبط: **169 passed, 8 skipped, 0 failed**. 8 skip محلی فقط testهای UI به‌دلیل `No module named 'PySide6'` بودند؛ آن‌ها pass محسوب نمی‌شوند.
- `drillintel config validate --json` — **PASS**, هیچ DB/workspace/service باز نشد.
- `drillintel mineru validate --json` — exit `1` به‌عنوان نتیجهٔ واقعی readiness: MinerU غایب، `NOT_READY`; models `NOT_CHECKED`; OCR `NOT_RUN`; metrics `null`. این readiness failure نیست که به test PASS تبدیل شده باشد؛ این وضعیت محصول host است.
- Full-suite محلی جداگانه اجرا نشد؛ به‌جای آن exact-SHA Linux/Windows full-suite CI در بالا اجرا و بررسی شد.

## 5. کارهای باقی‌مانده و محدودیت‌ها

1. **OCR واقعی:** runtime و مدل باید با سیاست محلی مجاز و بدون fetch ضمنی در محیط owner/operator نصب شوند؛ inference واقعی و مجموعهٔ مرجع/metrics معتبر سپس جداگانه اجرا شوند. تا آن زمان **NOT RUN / NOT QUALIFIED**.
2. **پذیرش فیزیکی Windows:** چک‌لیست دستی 16 مرحله‌ای مالک هنوز **NOT RUN** است. Qt offscreen و Windows CI جای display واقعی، تعامل کاربر یا acceptance مالک را نمی‌گیرند.
3. **M2–M5 و O-02/O-03:** فقط پس از تصمیم/قرارداد مالک دربارهٔ provider، داده، امنیت و پذیرش؛ این مأموریت adapter یا multi-user auth جدید نمی‌سازد.
4. **PostgreSQL، production DR، رمزگذاری archive، public deployment و عملیات enterprise:** **NOT QUALIFIED**؛ snapshot/archive موجود را DR production یا حفاظت کامل source data تلقی نکنید.
5. **هویت DDR:** actorهای ورودی label هستند، نه identity provider. کنترل reviewer با actor متفاوت، احراز هویت/استقلال انسانی را اثبات نمی‌کند.
6. **محاسبات مهندسی:** ریسک، AFE، currency conversion و inferenceهای فاقد روش/واحد/فرض/رفیوزال owner-approved همچنان ساخته نمی‌شوند.
7. **Upstream/license/runtime:** اجرای محلی MinerU و تفسیر حقوقی/تضمین پشتیبانی ادعا نشده است؛ package/runtime و وزن‌ها خارج از dependency core می‌مانند و انتخاب remote model source تصمیم صریح operator است.

## 6. مسیرهای اصلی تغییر

- `src/drilling_intelligence/integrations/mineru/{client.py,adapter.py,discovery.py,normalize.py,qualification.py}` — transport، قرارداد، پاک‌سازی، normalization و readiness.
- `src/drilling_intelligence/config/settings.py`, `src/drilling_intelligence/cli/app.py` — پیکربندی و CLI.
- `src/drilling_intelligence/operations/ddr_comparison.py`, `src/drilling_intelligence/ui/main_window.py` — comparison و نمایش read-only.
- `tests/integration/test_mineru_boundary.py`, `tests/unit/test_mineru_qualification.py`, `tests/integration/test_ddr_authoring.py`, `tests/unit/test_ddr_comparison.py`, `tests/unit/test_cli_config_validate.py` — آزمون‌های اضافه/تغییریافته.
- `tests/fixtures/mineru_ocr/` — فقط تصویر/ground truth مصنوعی، نه دادهٔ کاربر یا OCR benchmark میدانی.
- [`PROJECT_CONTEXT.md`](../PROJECT_CONTEXT.md), [`PRODUCT_CAPABILITY.md`](../PRODUCT_CAPABILITY.md), [`README.md`](../../README.md) — وضعیت جاری؛ [`CONTINUATION_PROMPT_AUTONOMOUS_ENGINEERING_FA.md`](CONTINUATION_PROMPT_AUTONOMOUS_ENGINEERING_FA.md) — prompt ادامه.

---

**نتیجه:** scope مهندسی کد و ابزارهای محلی تکمیل و روی branch مجاز push شد. Exact-SHA tip کد `075d5919…` در Linux و Windows PASS است. هیچ‌یک از این نتایج OCR دقت، پذیرش فیزیکی Windows، production readiness، PostgreSQL یا تصمیم مالک را جانشین نمی‌کنند.
