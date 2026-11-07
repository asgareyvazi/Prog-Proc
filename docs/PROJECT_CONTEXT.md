# مرجع زمینهٔ پروژه — Prog-Proc

- **وضعیت سند:** مرجع زمینهٔ اجرایی؛ نه گواهی پذیرش یا جایگزین شواهد CI
- **تاریخ بازبینی:** 2026-11-07 (Asia/Tehran)
- **مأموریت:** closure/qualification محصول؛ مرجع نتیجهٔ نهایی همان گزارش فارسی و شواهد exact-SHA است.
- **شاخهٔ مجاز:** `arena/5957b76d-prog-proc`
- **remote parent هنگام آغاز بازبینی:** `8626d61fe9a40db2b560507c79db5872f5ad1e12`؛ `main` محافظت‌شده `85122e1a98087db4495b820a9a0ffae9009b7819` بود و دست‌نخورده مانده است.
- **commit کد/تست push‌شده:** `be8f66cb0456feeff8ab2109322575f410e26571` روی branch مجاز.
- **CI exact-SHA برای `be8f66c...`:** Linux [run `42367459096`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42367459096) موفق، 9/9 job؛ Windows [run `42367459043`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42367459043) موفق، 14/14 job. Linux/Windows CPython 3.11 هرکدام 2,724 PASS / 63 SKIP / 0 fail از 2,787؛ CPython 3.14 هرکدام 2,726 / 61 / 0؛ Ubuntu 26.04 CPython 3.14: 2,786 / 1 / 0. Windows offscreen UI هر دو نسخه 29/29، PowerShell 5.1/7 run-card tests، clean-install و همهٔ شش stability lane با 15 تلاش هرکدام PASS شدند.
- **artifact limitations:** GitHub check annotations مجموع‌ها را ارائه می‌کنند، اما دریافت log و pytest artifact از `results-receiver`/Azure به EOF ختم شد؛ بنابراین skip reasonهای runner-specific از artifactها مستقلاً استخراج نشدند. هیچ‌یک از این نتیجه‌ها به SHA دیگری منتقل نمی‌شود.
- **timeout history:** parent `8626d61...` در Linux [42353817465] موفق بود؛ Windows [42353817436] لغو شد (13 موفق، history/CPython 3.14 نزدیک سقف 45 دقیقه). دو اجرای قبل نیز کند بودند. CI موفق روی `be8f66c...` علت شکست قدیمی را اثبات نمی‌کند.
- **تغییرات push‌شده بر پایهٔ `8626d61...`:** run-card/checklist migration head روی `0026`؛ strict DDR JSON parse پیش از DB bootstrap؛ atomic snapshot claim برای save/submit/revision و DDR review. regressionها concurrency, duplicate prevention و no-side-effect را می‌سنجند.
- **آزمون محلی گروه‌بندی‌شده:** 2,740 PASS / 47 SKIP / 0 FAIL از 2,787؛ skipها شامل 1 PostgreSQL intentionally unqualified، 2 shallow-clone history و 44 Qt به‌علت `libGL.so.1`. Ruff, format, mypy (44 source files), compileall, exception inventory, Alembic `0026` و wheel/sdist clean-install PASS شدند. یک `pytest -q` یکپارچه پس از 20 دقیقه در 49% متوقف شد؛ تمام laneها سپس batch شدند. PowerShell محلی در دسترس نبود؛ Qt workstation فیزیکی اجرا نشد.
- **مأموریت‌گزارش:** `docs/mission-reports/AUTONOMOUS_MISSION_2026-11-07_FA.md` شامل audit matrix، verdictها و owner/manual gates است. افزودن این گزارش، README و اصلاح وضعیت فعلی مستندات یک follow-up مستنداتی جداگانه می‌سازد؛ برای SHA آن CI جدید لازم است و نتایج `be8f66c...` منتقل نمی‌شوند.
- **migration head تأییدشده:** `0026`; فقط SQLite qualification دارد.

> این مرجع، مرزهای محصول و واژگان وضعیت را یک‌جا نگه می‌دارد. هر ادعای قابلیت باید با مسیر کد و آزمون/شاهد اجرایی پشتیبانی شود. متن این سند به‌تنهایی اثبات پیاده‌سازی یا قبولی نیست. گزارش نهایی مأموریت و CI روی SHA نهایی، وضعیت پذیرش را تعیین می‌کنند.

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
- UI ابزار مقایسهٔ revisionهای صریحاً linked را با مقادیر قبلی/جاری و provenance ثبت‌شده نشان می‌دهد؛ این مقایسه متنی است و هیچ مقدار مهندسی مشتق نمی‌کند. اجرای محلی UI در این sandbox به‌دلیل فقدان `libGL.so.1` **SKIPPED** است؛ Qt-enabled offscreen، stability و full-suite روی SHA `b3163bfef511fc93bd0fb57972f0cd672ab6c66c` در Linux/Windows موفق شدند (runهای `41940002906`/`41940002911`). Windows UI روی هر دو runtime 29/29 است؛ failure تاریخی registration/refresh بازتولید نشد اما علت آن اثبات نشده است. این نتایج physical Windows display acceptance نیستند.
- migration `0026`، جدول activity و ستون‌های authoring/revision/currentness را اضافه می‌کند. SQLite self-reference و authoring-state invariant در migration به‌وسیلهٔ trigger محافظت می‌شوند، زیرا SQLite نمی‌تواند self-FK را با `ALTER TABLE ADD CONSTRAINT` ساده اضافه کند. metadata/bootstrap نیز باید همین guardها را ایجاد کند. رفتار PostgreSQL از این کد استنباط یا qualify نمی‌شود.

مسیرهای اصلی: `operations/ddr.py`, `review/actions.py`, `review/service.py`, CLI در `cli/app.py`, UI در `ui/main_window.py`, migration `0026`, `tests/integration/test_ddr_authoring.py`, `tests/integration/test_ddr_cli.py`, CLI snapshot tests و migration tests.

## 4.1. هویت و lifecycle درس

- کد درس پس از strip و Unicode casefold به‌صورت global یکتا است؛ repository create/update/revision، promotion و partial unique index دیتابیس این قرارداد را enforce می‌کنند.
- در source supersession، predecessor به `SUPERSEDED` می‌رود و `prior_status` حفظ می‌شود؛ successor تازه `DRAFT` است و approval metadata را به ارث نمی‌برد.
- Focused lesson/exception suite روی SHA `b3163bf...`: 95 passed. حذف source بدون successor هنوز policy مصوب ندارد؛ audit زنجیرهٔ بدون current/withdrawn disposition را رد کرد. rename/removal/linking باید با تصمیم مالک انجام شود، نه حدس پیاده‌سازی.

## 5. Database، schema و بسته‌بندی

- `METADATA_REVISION` باید برابر تنها Alembic head باشد؛ اکنون کد `0026` را اعلام می‌کند.
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
- stability workflow همچنان سقف 45 دقیقه و 15 تلاش دارد؛ این مقادیر، selectorها و assertion/classifierها در این بررسی کاهش/تغییر نکردند. دو run متوالی، history/CPython 3.14 را پس از 44m10s و 44m25s لغو/timeout کردند؛ سه run تاریخی موفق همان node، repeated step را در 55–97s تمام کرده بودند. Artifact/logهای دو timeout با EOF دریافت نشدند. اندازه‌گیری محلی backend fixture (Linux، بدون Qt) 2.281s بود و علت کندی Windows را اثبات نمی‌کند؛ علت ریشه‌ای **UNRESOLVED**, بهینه‌سازی fixture انجام‌نشده و job سبز شمرده نمی‌شود.

## 7. مرجع اسناد و ترتیب تقدم

| سند | نقش |
|---|---|
| `docs/PROJECT_CONTEXT.md` | واژگان و مرزهای جاری محصول؛ مبنای هماهنگی بین اسناد |
| `docs/DECISIONS.md` و `docs/adr/` | تصمیم/محدودیت معماری؛ تصمیم مالک را از پیاده‌سازی جدا می‌کند |
| `docs/DOCUMENT_DOMAIN_COVERAGE.md` | taxonomy، handler و سطح evidence هر کلاس سند |
| `docs/PRODUCT_CAPABILITY.md` | inventory قابلیت‌ها با وضعیت و شواهد |
| `docs/HUMAN_REVIEW_ACTION_INVENTORY.md` | lifecycle actionهای انسانی و مرز UI |
| `docs/mission-reports/V10.3_*` | گزارش تاریخی V10.3، معتبر برای SHA/runهای درج‌شده؛ نه وضعیت مأموریت فعلی |
| `docs/mission-reports/AUTONOMOUS_PRODUCT_CLOSURE_2026-11-04_FA.md` | گزارش نهایی فارسی این مأموریت، شامل تصمیم qualification، exact-SHA evidence و open items |
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
