# مرجع زمینهٔ پروژه — Prog-Proc

## Snapshot جاری — Program / Procedure روی branch فعال

- Branch ثابت و فعال: `arena/5957b76d-prog-proc`، checkout پایه `317c55365319af61138000664293ae2d84e27a6e`; به branch دیگری سوئیچ نشده است. Remote tip فقط برای انتقال گزینشی feature خوانده شد.
- Slice headless Program/Procedure و migration `0027` به همین worktree منتقل شده‌اند. اصلاح optimistic-concurrency نیز `_touch` را strictly monotonic می‌کند؛ token پس از mutation حتی در clock tick برابر/عقب‌رفته جلو می‌رود. هیچ محاسبهٔ مهندسی جدید یا تأیید ضمنی اضافه نشده است.
- تست‌های انتخابی روی همین worktree: **107 passed, 1 skipped**. Skip pass نیست. Linux/Windows CI برای این worktree نهایی **اجرا نشده**؛ physical Windows acceptance نیز **NOT RUN** است. هیچ stage، commit یا push انجام نشده است.
- Worktree پیش از انتقال شامل تغییرات متعدد متعلق به کار قبلی بود؛ آن‌ها حفظ شده‌اند و scopeهای نامرتبط عمداً stage نشده‌اند.
- `out/performance.json` در checkout فعال غایب است؛ آن را restore/recreate/stage/commit/push نمی‌کنیم. SQLite تنها backend qualified است؛ PostgreSQL همچنان **NOT QUALIFIED**. NPT roll-up V1 تنها calculation اجرایی مجاز است؛ M2–M5/O-02/O-03 و trajectory owner-gated هستند.

## Historical snapshot — DDR assurance و وضعیت پیشین

- **وضعیت سند:** مرجع جاری محصول؛ گزارش مأموریت و شواهد exact-SHA معیار نتیجه‌اند، نه این خلاصه به‌تنهایی.
- **تاریخ مأموریت:** 2026-10-20 UTC (تاریخ سیستم مأموریت). صفحه‌های GitHub برای رویدادهای CI جاری timestamp برابر 2026-11-08 نشان می‌دهند؛ این ناهمخوانی حفظ شده است.
- **شاخه:** `arena/5957b76d-prog-proc`، فعال؛ implementation جاری روی remote و checkout برابر `b1deab1782d6c17552ca4334606b5521c84c44ef`. `main` و `origin/main` هر دو `85122e1a98087db4495b820a9a0ffae9009b7819` هستند و تغییر نکرده‌اند.
- **حذف محافظت‌شده:** `out/performance.json` از worktree آغازین غایب بود و همچنان فقط به‌صورت ` D out/performance.json` دیده می‌شود؛ restore، recreate، stage یا commit نشده است. Stash خالی بود.
- **آخرین اجرای feature code پیش از diagnostics:** SHA `41f04d6a6ddd2abe4339c410b7e688004eb62c4a` روی Linux run [`42488614446`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42488614446) با failure و روی Windows run [`42488614499`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42488614499) با conclusion `cancelled` پایان یافت. Linux CPython 3.11/3.14 هرکدام 2,847 collected / 2,784 passed / 62 skipped / 1 failed؛ هر دو failure، generated exception inventory قدیمی بودند. Ubuntu 26.04/CPython 3.14 برابر 2,847 / 2,845 / 1 skipped / 1 failed؛ identity تست شکست‌خورده در annotation نبود.
- **Windows همان SHA:** CPython 3.11 و 3.14 هرکدام 2,847 / 2,782 / 64 / 1؛ failure هر دو همان inventory است. دو history stability job پس از سقف دست‌نخوردهٔ 45 دقیقه cancel شدند؛ skipهای CI را پاس حساب نکنید. Qt UI برابر 30/30 برای هر نسخه، intake/presentation stability هرکدام 15/15 برای هر نسخه، PowerShell 5.1/7 run-card و هر دو clean-install smoke موفق شدند. Linux history stability برابر 15/15 برای هر نسخه بود و Qt coverage برابر 76.54% lines / 55.13% branches گزارش شد. این‌ها پذیرش display فیزیکی Windows نیستند.
- **محدودیت artifacts:** GitHub API metadata/digests را نشان داد، اما دانلود JUnit و Windows history ZIPها از `gh run download` به EOF روی `productionresultssa*.blob.core.windows.net` خورد؛ این host خارج از allowlist شبکهٔ sandbox است. بنابراین نام failure در Ubuntu، reason جزئی همهٔ skipها و attempt-by-attempt Windows history **UNKNOWN / NOT INSPECTED** هستند؛ از حجم artifact علت‌سازی نشده است. شناسه‌ها و digestها در traceability report ثبت‌اند.
- **diagnostics بعدی:** SHA `b1deab1782d6c17552ca4334606b5521c84c44ef` فقط گزارش identity آزمون‌های failure/error از JUnit (بدون failure body) و ثبت افزایشی شروع/نتیجهٔ هر stability attempt در GitHub step summary را اضافه می‌کند؛ selector، 15 تلاش و timeout 45 دقیقه تغییر نکرده‌اند. Linux run [`42501577186`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42501577186) هنوز به‌خاطر Ubuntu lane در جریان است؛ Windows run [`42501577174`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42501577174) پس از 44m22s با failure روی دو full-suite inventory test پایان یافت. تا 2026-11-08 17:31 UTC، Windows UI هرکدام 30/30 و هر شش stability lane نیز 15/15 موفق بودند؛ Linux/Ubuntu offscreen workbench jobs هم با stability evidence پاس شدند. Linux CPython 3.11/3.14 هرکدام 2,850 / 2,787 / 62 / 1 با یک failure روی generated exception inventory قدیمی داشتند؛ Windows هر دو runtime 2,850 / 2,785 / 64 / 1 با همان failure. Diagnostics identity آزمون شکست‌خورده را ثبت کرد. Ubuntu 26.04 full-suite job `128795266228` در run `42501577186` تا 2026-11-08 20:31 UTC هنوز `in_progress` بود، پس از حدود 3h44 از شروع step در 16:47:07Z؛ run `updated_at` روی 16:46:32Z بود. این را pending ثبت کنید، نه pass یا fail؛ هنوز exit/test identity/skip detail آن lane موجود نیست. Linux و Windows workflow کامل green نیستند.
- **آزمون محلی جاری:** suite هدفمند DDR/MinerU/exception policy برابر **89 passed**؛ suite diagnostics (`test_pytest_totals.py`, `test_qt_stability_manifest.py`) برابر **26 passed**؛ `tools/exception_review.py --check`، `ruff check .`, `ruff format --check .` (449 files)، mypy (44 source files)، compileall و Alembic single-head موفق‌اند. این full-suite محلی نیست؛ Qt در venv محلی نصب نیست. Inventory از tree بازتولید شده و 108 broad handlers دارد: 13 re-raised، 67 reported، 28 suppressed.
- **قابلیت‌های مأموریت:** مقایسهٔ DDR با محتوای authored و ambiguity محافظه‌کارانه، CLI/UI read-only، concurrency آزمون‌شده با processهای مستقل و SQLite فایل‌محور، corpus مصنوعی MinerU و transport/privacy/cleanup boundary و license metadata در implementation حاضرند؛ readiness مدل/inference محلی همچنان NOT_READY/NOT_CHECKED/NOT RUN با metrics null است.
- **مرز ثابت محصول:** Local-first، single-user، **Pre-Alpha**؛ SQLite تنها backend qualified است. Taxonomy دقیق 26 کلاس / 14 `END_TO_END_CERTIFIED` / 7 `KNOWLEDGE_SUPPORTED` / 5 `EXTRACT_ONLY` / 12 handlers ثابت می‌ماند. M2–M5, O-02/O-03, PostgreSQL, auth/multi-user, risk/AFE/currency, new calculations, production DR, enterprise archive و public/production deployment همچنان **OWNER-GATED / NOT QUALIFIED** هستند.
- **مستندات مأموریت:** [گزارش فارسی](mission-reports/AUTONOMOUS_MISSION_2026-10-20_DDR_COMPARISON_ASSURANCE_FA.md)، [traceability/baseline](mission-reports/DDR_COMPARISON_ASSURANCE_TRACEABILITY_2026-10-20.md)، و [README](../README.md). Commit نهایی docs باید روی همین branch باشد؛ CI بعد از ساخت commit جداگانه با همان SHA ارزیابی می‌شود.

> این سند مرزها و واژگان وضعیت را حفظ می‌کند. هر ادعای قابلیت باید به مسیر کد و آزمون/شاهد exact-SHA متصل باشد؛ متن به‌تنهایی گواهی پذیرش نیست.

## 1. محصول و مرزهای ثابت

Prog-Proc یک سکوی محلی، workspace-centric و single-user برای ثبت اسناد حفاری، provenance، مرور انسانی و تعداد محدودی قابلیت مهندسی قطعی است. توسعه در مرحلهٔ **Pre-Alpha** می‌ماند تا معیارهای مأموریت و شواهد پذیرش واقعی تکمیل شوند.

- SQLite تنها backend دارای شواهد qualification در این مخزن است. PostgreSQL **NOT QUALIFIED** می‌ماند تا migration، introspection قیود/indexها و گردش‌کار integration مبتنی بر PostgreSQL service واقعاً اجرا شوند.
- جست‌وجو و retrieval سطح اکتشاف‌اند؛ منبع authoritative همان رکوردهای عملیاتی و نسخهٔ سند است.
- Review، ثبت سند، تأیید عملیاتی، promotion و مهندسی محاسبه‌شده مفاهیم جدا هستند. یک کلیک review به‌تنهایی domain fact، مجوز یا approval نمی‌سازد.
- مقدار مهندسی، تبدیل واحد، فرمول، فرض، default یا inference جدید اضافه نمی‌شود مگر با روش/واحد/فرض/refusal case و شواهد verification مصوب.
- محاسبهٔ اجرایی تأییدشدهٔ مستندات فعلی، NPT roll-up قطعی موجود است؛ این واقعیت مجوز اضافه‌کردن محاسبهٔ دیگری نیست.
- هیچ provider هوش مصنوعی، embedding/vector store، Skills runtime، authentication چندکاربره، installer، امضا یا branding در این مأموریت فرض یا ادعا نمی‌شود. موارد نیازمند مالک همچنان **OWNER-GATED** هستند.

## 2. شواهد و taxonomy اسناد

در baseline V10.3، registry مستنداتی 26 کلاس داشت: 14 `END_TO_END_CERTIFIED`، 7 `KNOWLEDGE_SUPPORTED` و 5 `EXTRACT_ONLY`. این مأموریت taxonomy را گسترش نداد؛ Intake ورودی/صف مشترک است، نه کلاس بیست‌وهفتم. شمارش و قرارداد هر کلاس در [`DOCUMENT_DOMAIN_COVERAGE.md`](DOCUMENT_DOMAIN_COVERAGE.md) نگهداری و در این بستهٔ نهایی با handlerها و test manifest تطبیق داده شده است؛ وضعیت فعلی هر قابلیت در [`PRODUCT_CAPABILITY.md`](PRODUCT_CAPABILITY.md) است.

مرزهای مهم:

1. استخراج و preview می‌توانند متن و provenance قابل‌مشاهده فراهم کنند؛ به‌تنهایی سند را در registry ثبت یا دادهٔ domain ایجاد نمی‌کنند.
2. Registration سند را از مسیر Document Registry ثبت می‌کند و promotion انجام نمی‌دهد.
3. Promotion فقط از handler/contract ایستا و مجاز همان کلاس می‌آید؛ reader عمومی اسناد writer عمومی نیست.
4. متن مبهم، classification، recommendation یا review context به‌تنهایی عملیاتی/مهندسیِ تأییدشده محسوب نمی‌شود.

## 3. Intake: lifecycle و provenance

پیاده‌سازی Intake باید در این ترتیب و با تفکیک روشن باقی بماند:

`QUEUED → REVIEWED | REJECTED → (فقط REVIEWED) REGISTERED`

- صف محلی نگه‌دارندهٔ مسیر، نام، پسوند، اندازه، SHA-256 و actor/time است؛ فایل اصلی را کپی یا محتوای سند را به صف تبدیل نمی‌کند.
- preview hash را دوباره اندازه‌گیری می‌کند، از extractor/classifier محلی و static contract موجود استفاده می‌کند، و نتیجهٔ preview را به‌عنوان evidence نمایشی نگه می‌دارد؛ preview خودش domain writer نیست.
- `REVIEWED` یک disposition انسانی با actor و reason است، نه approval یا promotion.
- Registration فقط برای محتوای byte-for-byte مطابق hash بررسی‌شده مجاز است. سرویس موجود Document Registry را فراخوانی می‌کند و لینک رجیستری و audit را در همان UoW ثبت می‌کند؛ `promotion_performed` باید `False` بماند.
- جدول `intake_registration`، لینک registration را جدا از status ثابت صف نگه می‌دارد. status ذخیره‌شدهٔ صف همچنان `REVIEWED` می‌ماند؛ DTO/CLI/UI فقط با وجود registration record آن را `REGISTERED` نمایش می‌دهد. یکتایی لینک، FKها، idempotency و refusalهای stale/unreviewed بخشی از آزمون‌اند.
- حذف/rollback نباید لینک یا audit ثبت‌شده را بی‌صدا دور بریزد؛ downgrade باید با دادهٔ ثبت‌شده fail-closed شود.

مسیرهای اصلی: `review/intake.py`, `documents/registry.py`, `database/models.py`, migration `0025`, `tests/integration/test_intake_registration.py` و `test_intake_e2e.py`.

## 4. Daily DDR: نویسندگی، عملیات و review

DDR نویسندگی دستی، محصول authoring جدا از ingestion است:

- گزارش با تاریخ canonical `YYYY-MM-DD` و واژگان operation ثبت‌شده ساخته می‌شود. برچسب/شرح/period متن اپراتور است؛ سیستم مدت، depth، NPT یا مقدار دیگری را از متن آزاد حدس نمی‌زند.
- چرخهٔ authoring گزارش: `DRAFT → SUBMITTED`. نسخهٔ draft فقط با optimistic freshness check و تا وقتی current و `CANDIDATE` است قابل ویرایش است. هر write توکن `updated_at` را حتی در clock tick تکراری strictly monotonic جلو می‌برد. Save/submit/revision پیش از چندردیفی‌نویسی، snapshot سطر را با conditional no-op UPDATE به‌صورت اتمیک claim می‌کنند؛ تست‌های دو نویسنده اثبات می‌کنند فقط یک درخواست با همان token commit می‌شود.
- Submit گزارش را خودکار تأیید نمی‌کند. `DdrReport.status` و `WellOperation.status` روی `CANDIDATE` می‌مانند تا review مستقل و action قانونی انجام شود.
- هر activity دستی، provenance با actor/reason/sequence/revision می‌گیرد؛ در submit به `WellOperation` متناظر وصل می‌شود. نبود ORM relationship برای ترتیب flush مجوز کنارگذاشتن FK یا transaction نیست.
- Revision نسخهٔ قبلی را تاریخی (`is_current=False`) می‌کند، عملیات آن نسخه را نیز historical می‌کند و revision تازه را draft نگه می‌دارد. Review روی draft در دسترس نیست؛ superseding revision خود به معنی approval نیست.
- Review actionهای `confirm`/`reject` فقط برای گزارش `SUBMITTED` و current در دسترس‌اند، با re-read و preconditionهای status/current/scope/revision/updated_at؛ پیش از route شدن، report یا DDR-authored operation با conditional update اتمیک claim می‌شود تا دو reviewer از snapshot هم‌زمان status را عوض نکنند. reason اجباری است و actor اعلام‌شده باید با `created_by` فرق کند. `ReviewActionService` مقدار `DdrReport.revision_number` را نیز به‌عنوان revision precondition می‌خواند. این مقایسهٔ شناسهٔ ورودی، احراز هویت یا اثبات استقلال شخصی نیست؛ authentication در دامنهٔ این محصول نیست.
- CLI مستقل `ddr` برای create/list/show/actions/save/submit/revise/decide همان `DdrAuthoringService` و `ReviewActionService` را مصرف می‌کند. فایل UTF-8 JSON حداکثر 1 MiB است؛ کلید تکراری/ناشناخته و shape مبهم رد می‌شود، save snapshot کامل و `expected_updated_at` می‌خواهد، و review همهٔ پیش‌شرط‌ها را دوباره در سرویس اعمال می‌کند. JSON create/save پیش از بازکردن lazy database خوانده و اعتبارسنجی می‌شود؛ malformed input حتی bootstrap schema را trigger نمی‌کند. CLI محاسبه، import یا خودکارکردن review اضافه نمی‌کند.
- UI ابزار مقایسهٔ revisionهای صریحاً linked را با predecessor/successor، تغییر فیلدها، فعالیت‌های اضافه/حذف/تغییر‌یافته، متن نویسنده و ترتیب provenance نشان می‌دهد؛ تاریخچهٔ malformed/missing/بزرگ را محدود می‌کند و هیچ مقدار مهندسی یا approval مشتق نمی‌کند. UI محلی اجرا نشد چون PySide6 در venv نیست (SKIPPED، نه PASS). روی SHA `41f04d6…` Qt offscreen UI در Linux/Windows هر runtime 30/30 بود، اما کل CI green نبود و Windows history laneها timeout شدند. روی code SHA `b1deab1…` Windows offscreen UI هرکدام 30/30 و history stability هر دو نسخه 15/15 پاس شدند؛ Linux و Windows full suites در هر دو runtime فقط به‌علت inventory قدیمی یک failure داشتند؛ Ubuntu 26.04 full suite هنگام ثبت هنوز در جریان بود. هیچ‌یک physical Windows display acceptance نیست.
- migration `0026`، جدول activity و ستون‌های authoring/revision/currentness را اضافه می‌کند. SQLite self-reference و authoring-state invariant در migration به‌وسیلهٔ trigger محافظت می‌شوند، زیرا SQLite نمی‌تواند self-FK را با `ALTER TABLE ADD CONSTRAINT` ساده اضافه کند. metadata/bootstrap نیز باید همین guardها را ایجاد کند. رفتار PostgreSQL از این کد استنباط یا qualify نمی‌شود.

مسیرهای اصلی: `operations/ddr.py`, `review/actions.py`, `review/service.py`, CLI در `cli/app.py`, UI در `ui/main_window.py`, migration `0026`, `tests/integration/test_ddr_authoring.py`, `tests/integration/test_ddr_cli.py`, CLI snapshot tests و migration tests.

## 4.1. هویت و lifecycle درس

- کد درس پس از strip و Unicode casefold به‌صورت global یکتا است؛ repository create/update/revision، promotion و partial unique index دیتابیس این قرارداد را enforce می‌کنند.
- در source supersession، predecessor به `SUPERSEDED` می‌رود و `prior_status` حفظ می‌شود؛ successor تازه `DRAFT` است و approval metadata را به ارث نمی‌برد.
- Focused lesson/exception suite روی SHA `b3163bf...`: 95 passed. حذف source بدون successor هنوز policy مصوب ندارد؛ audit زنجیرهٔ بدون current/withdrawn disposition را رد کرد. rename/removal/linking باید با تصمیم مالک انجام شود، نه حدس پیاده‌سازی.

## 5. Database، schema و بسته‌بندی

- `METADATA_REVISION` باید برابر تنها Alembic head باشد؛ کد جاری `0027` را اعلام می‌کند.
- Migrationهای جدید باید online و `alembic upgrade head --sql` را در SQLite isolated path پشتیبانی کنند. Offline generation نباید workspace file را تغییر دهد یا ایجاد کند.
- Migrationهای additive ممکن‌اند؛ downgrade با رکوردهای authored/registered/historical نباید داده را بی‌صدا حذف کند.
- `schema_diff()` فقط table/column presence را می‌سنجد؛ برای FKها، unique/partial indexها، triggers و رفتار deletion باید آزمون/introspection اختصاصی وجود داشته باشد.
- wheel/sdist clean-install و schema bootstrap از metadata مسیر واقعی محصول‌اند؛ وجود migration در source checkout به‌تنهایی clean install را ثابت نمی‌کند.
- package version فعلی `0.0.2a0` است؛ این شماره و `Development Status :: 2 - Pre-Alpha` تضمین انتشار یا آمادگی عملیاتی نیست.
- `doctor` پیش از migration روی SQLite موجود و دوباره پس از bootstrap/upgrade، `integrity_check`, `foreign_key_check` و storage preflight فقط‌خواندنی انجام می‌دهد؛ خطای storage مانع ادعای سلامت domain و مانع domain/search query می‌شود. بررسی‌های cross-row و drift شاخص فقط پس از preflight موفق انجام می‌شوند؛ repair خودکار نداریم.
- `workspace backup` از SQLite online backup API و manifest شامل SHA-256، UTC، schema revision و storage health استفاده می‌کند؛ مقصد باید تازه باشد. این **database snapshot** است، نه full workspace/source backup: فقط `workspace.toml` و snapshot پایگاه‌داده را دارد؛ corpus/source و search index خارج‌اند و باید جداگانه، با سیاست مجاز اپراتور، حفاظت شوند. archive رمزگذاری نشده است.
- Restore پایگاه‌داده‌ای فقط در مسیر تازه و برای workspace دارای identity row فقط در همان absolute root ذخیره‌شده مجاز است؛ re-home، overwrite، migration پنهان و overwrite DB فعال وجود ندارد. بررسی path/member/hash/health انجام می‌شود؛ سقف snapshot 8 GiB، overhead آرشیو 16 MiB، metadata یک MiB و تعداد عضو دقیقاً سه است. این دستور فقط database snapshot است.
- دستورهای `workspace archive` و `workspace restore-archive` قالب versioned جداگانه‌ای برای فایل‌های واجدشرایط زیر workspace به‌علاوهٔ SQLite online snapshot دارند؛ search index، cache/log، VCS، credential و temporaryها مستثنا هستند و منابع بیرونی فقط گزارش می‌شوند، نه اینکه دنبال/کپی شوند. هر عضو hash می‌شود، مسیر symlink/special-file رد می‌شود، restore فقط به ریشهٔ ثبت‌شده و با publish اتمی بدون جایگزینی مجاز است. آرشیو رمزگذاری نشده و physical Windows acceptance انجام نشده است. روی SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` هر دو workflow Linux `41940002906` و Windows `41940002911` success شدند؛ پنج full-suite lane و clean-install jobs سبزند. سابقهٔ SHA `94d698f...` test-contract failures داشت و SHA `ff4afdd...` یک Windows UI offscreen failure داشت؛ جزئیات و exact-SHA totals فقط در گزارش فارسی‌اند. این نتایج به معنی صلاحیت DR/production یا پشتیبان‌گیری از منابع بیرونی نیست.

## 6. Workbench، CLI و آزمون‌پذیری

- UI باید authoring واقعی DDR، Intake queue، selection، preview، lifecycle controls، provenance و read-only/busy state را ارائه کند؛ جدول import/report یا لیست صرف جایگزین authoring نیست.
- عملیات blocking از طریق controller/worker خارج از GUI thread انجام شود. Qt event waits باید bounded/event-driven باشند؛ از sleep تصادفی، retry پنهان، termination اجباری یا `os._exit` برای سبزکردن تست استفاده نشود.
- مالکیت هر worker تا دریافت `QObject.destroyed` و خروج کامل `QThread.wait()` نگه داشته می‌شود؛ handlerهای نتیجه هنگام close بی‌اثرند و refreshهای معوق پس از خروج signal اجرا می‌شوند تا re-entrancy باعث ساخت worker هم‌زمان یا callback دیررس نشود.
- CLI parser snapshot بخشی از قرارداد عمومی است؛ تغییر عمدی Intake و دستورهای `workspace backup/restore` و `workspace archive/restore-archive` باید `tests/snapshots/cli_surface.txt` را با آزمون به‌روز کند.
- در این sandbox آزمون Qt محلی ممکن است به‌علت dependencyهای host مانند `libGL.so.1` بارگذاری نشود. چنین موردی **SKIPPED/BLOCKED** است و **PASS** نیست؛ فقط runner با Qt واقعاً اجراشده می‌تواند شواهد offscreen فراهم کند. Offscreen نیز معادل display فیزیکی Windows نیست.
- stability workflow سقف 45 دقیقه و 15 تلاش هر lane را حفظ می‌کند؛ selectorها و assertion/classifierها در این مأموریت تغییر یا تضعیف نشدند. در exact-SHA Windows run `42466023072` همهٔ شش lane 15/15 پاس شدند؛ در run بعدی `42501577174` دو history lane نیز 15/15 پاس شدند (2m11s و 2m20s). Linux و Ubuntu workbench stability در run `42501577186` هم موفق بود. timeoutهای تاریخی Windows روی SHA `41f04d6…` هنوز علت ریشه‌ای اثبات‌شده ندارند؛ runهای سبز جدید نشان می‌دهند مشکل در این اجرای جدید تکرار نشده، نه اینکه علت قبلی را توضیح دهند. تمام CI offscreen است و پذیرش display فیزیکی نیست.

## 7. مرجع اسناد و ترتیب تقدم

| سند | نقش |
|---|---|
| `docs/PROJECT_CONTEXT.md` | واژگان و مرزهای جاری محصول؛ مبنای هماهنگی بین اسناد |
| `docs/DECISIONS.md` و `docs/adr/` | تصمیم/محدودیت معماری؛ تصمیم مالک را از پیاده‌سازی جدا می‌کند |
| `docs/DOCUMENT_DOMAIN_COVERAGE.md` | taxonomy، handler و سطح evidence هر کلاس سند |
| `docs/PRODUCT_CAPABILITY.md` | inventory قابلیت‌ها با وضعیت و شواهد |
| `docs/HUMAN_REVIEW_ACTION_INVENTORY.md` | lifecycle actionهای انسانی و مرز UI |
| `docs/mission-reports/V10.3_*` | گزارش تاریخی V10.3، معتبر برای SHA/runهای درج‌شده؛ نه وضعیت مأموریت فعلی |
| `docs/mission-reports/AUTONOMOUS_MISSION_2026-11-08_MINERU_DDR_FA.md` | گزارش تاریخی مأموریت پیشین؛ ادعاها فقط برای SHA/runهای همان گزارش معتبرند |
| `docs/mission-reports/AUTONOMOUS_MISSION_2026-11-08_ENGINEERING_EXPANSION_FA.md` | گزارش جاری این مأموریت؛ status matrix، دقیق-SHA CI و owner/manual gates |
| `docs/mission-reports/CONTINUATION_PROMPT_AUTONOMOUS_ENGINEERING_FA.md` | prompt چندمرحله‌ای copy-ready برای ادامهٔ مهندسی خودمختار با همان branch/gates |
| `docs/mission-reports/AUTONOMOUS_PRODUCT_CLOSURE_2026-11-04_FA.md` | گزارش تاریخی مأموریت 2026-11-04؛ exact-SHA evidence و open items فقط برای runهای همان زمان |
| `docs/PROJECT_CLOSURE_PLAN.md` | snapshot تاریخی V9.0؛ برای وضعیت جاری باید به این سند/گزارش مأموریت جدید ارجاع شود |
| `docs/WINDOWS_TESTING.md` | run-card و پذیرش Windows؛ automated offscreen و physical display جدا هستند |

اگر سند قدیمی با کد/آزمون جاری تعارض داشت، تعارض باید به‌صراحت در defect register و گزارش ثبت شود؛ مستندات قدیمی نباید به‌عنوان اثبات قابلیت اجرا نقل شوند.

## 8. واژگان نتیجه

- **PASS:** gate واقعاً اجرا شد و به شرط مقرر رسید.
- **FAIL:** اجرا شد و شرط را نقض کرد؛ تا رفع و اجرای مجدد باز می‌ماند.
- **SKIPPED:** runner/test اجرا نشد؛ هرگز PASS نیست.
- **NOT RUN:** کاری انجام نشده یا اجرای آن در دسترس نبوده است.
- **BLOCKED:** مانع فنی/محیطی/مالک مشخص جلوی اجرا را گرفته است.
- **OWNER-GATED:** تصمیم/اجازهٔ مالک لازم است؛ خودکار توسعه داده نمی‌شود.
- **NOT QUALIFIED:** ادعای پشتیبانی بدون شواهد لازم مجاز نیست.

مأموریت تا وقتی criteria، package checks، regression tests و CI روی exact final SHA شواهد کافی ندهند، **Pre-Alpha** باقی می‌ماند. SHA نهایی، commit، push، Linux/Windows run IDs، شمارش تست‌ها و هر مانع unresolved باید فقط پس از اندازه‌گیری در گزارش فارسی همان مأموریت ثبت شود.
