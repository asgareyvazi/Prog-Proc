# مرجع زمینهٔ پروژه — Prog-Proc

## آخرین وضعیت مأموریت مهندسی

- **وضعیت سند:** مرجع جاری محصول؛ گزارش مأموریت و شواهد exact-SHA معیار نتیجه‌اند، نه این خلاصه به‌تنهایی.
- **تاریخ بازبینی:** 2026-10-20 (UTC، تاریخ سیستم مأموریت).
- **شاخهٔ مجاز:** `arena/5957b76d-prog-proc`؛ `main` تغییر نکرد.
- **reconciliation شروع:** checkout محلی با `HEAD=317c55365319af61138000664293ae2d84e27a6e` شروع شد، درحالی‌که remote feature ref از قبل روی descendant `d4b2fc4256b7107ae1c857e64eb871d3dec50b7c` بود. پس از تأیید ancestry، branch ref و index فقط fast-forward شدند؛ فایل‌های worktree دست‌نخورده ماندند. `main` و `origin/main` هر دو `85122e1a98087db4495b820a9a0ffae9009b7819` باقی ماندند.
- **حذف محافظت‌شده:** `out/performance.json` در remote baseline ثبت بود ولی در worktree آغازین غایب بود. هرگز restore، stage یا commit نشد؛ اکنون همچنان غایب و به‌صورت ` D out/performance.json` unstaged دیده می‌شود. Stash در ابتدا و انتها خالی بود؛ stash ساخته نشد.
- **implementation tip:** `075d5919ebd8a5e600e3c806b73bc560203f52c5`، روی branch مجاز push شده است. Linux exact-SHA run [`42466023039`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42466023039) و Windows exact-SHA run [`42466023072`](https://github.com/asgareyvazi/Prog-Proc/actions/runs/42466023072) هر دو تمام jobهای خود را با موفقیت گذراندند.
- **شمارش full-suite روی همان SHA:** Linux CPython 3.11 و 3.14 هرکدام 2,824 collected / 2,763 passed / 61 skipped / 0 failed؛ Ubuntu 26.04 + CPython 3.14 برابر 2,824 / 2,823 / 1 / 0؛ Windows CPython 3.11 و 3.14 هرکدام 2,824 / 2,761 / 63 / 0. Skipها passed نیستند؛ جزئیات reason همهٔ skipهای full suite از artifacts/log endpoint به‌طور کامل بازیابی نشد. Windows Qt offscreen دو نسخه 29/29 و شش stability lane هرکدام 15/15 بودند. این‌ها شواهد CI هستند، نه پذیرش display فیزیکی Windows.
- **اصلاحی که exact-SHA CI کشف کرد:** SHA قبلی `85f6a825d08ce10431f5d4e8081114b65bfab331` در Linux موفق شد اما Windows CPython 3.14 یک تفاوت بایت‌های zlib در PNG مصنوعی را کشف کرد. DEFLATE ثابت با stored-blockها جایگزین شد؛ SHA `075d5919…` هر دو workflow Linux و Windows را سبز کرد. این تاریخچه و runهای شکست/رفع در گزارش نهایی ثبت شده‌اند.
- **آزمون محلی:** suite هدفمند 169 passed / 8 skipped / 0 failed؛ هر 8 مورد UI به‌دلیل نصب‌نبودن PySide6 در sandbox skip شدند. Ruff lint و format (445 فایل)، mypy (44 فایل)، compileall، constraints پنج‌گانه، `tools/exception_review.py --check` (107 broad handlers، همه مستند)، CLI snapshot و Alembic `0026` موفق بودند. Sdist و wheel ساخته شدند؛ فقط هشدار deprecation موجود برای `project.license` باقی است. این جمع، full-suite محلی نیست؛ full-suite exact CI بالاست.
- **CLI و OCR:** `drillintel config validate --json` با `database_opened=false`, `workspace_opened=false`, `services_probed=false` موفق شد. `drillintel mineru validate --json` روی این host به‌درستی exit 1 داد: runtime `NOT_READY` چون MinerU نصب نیست، مدل `NOT_CHECKED`، OCR `NOT_RUN` و metrics برابر null. نه inference واقعی و نه صلاحیت accuracy OCR ادعا نمی‌شود؛ مدل دانلود یا سند آپلود نشد.
- **تغییرات این مأموریت:** MinerU transport/HTTP/artifact/provenance/normalization hardening؛ readiness و fixture tooling محلی؛ concurrency نویسندگی DDR با processهای مستقل و file-backed SQLite؛ read-only revision comparison؛ و config validation side-effect-free. Taxonomy و statusهای owner-gated تغییر نکردند.
- **باقی‌مانده:** product همچنان local-first, single-user, **Pre-Alpha** است؛ SQLite تنها DB واجد qualification است. PostgreSQL, M2–M5, O-02/O-03, authentication/multi-user, risk/AFE/currency, production DR, archive encryption/public deployment, physical Windows acceptance و real OCR accuracy **NOT QUALIFIED / NOT RUN / OWNER-GATED** مطابق gate مربوطه باقی می‌مانند.
- **گزارش و ادامه:** [`AUTONOMOUS_MISSION_2026-11-08_ENGINEERING_EXPANSION_FA.md`](mission-reports/AUTONOMOUS_MISSION_2026-11-08_ENGINEERING_EXPANSION_FA.md), [`CONTINUATION_PROMPT_AUTONOMOUS_ENGINEERING_FA.md`](mission-reports/CONTINUATION_PROMPT_AUTONOMOUS_ENGINEERING_FA.md), و ماتریس pre-implementation [`ENGINEERING_EXPANSION_TRACEABILITY_2026-11-08.md`](mission-reports/ENGINEERING_EXPANSION_TRACEABILITY_2026-11-08.md). SHA/run مربوط به commit نهایی اسناد، پس از ساخت همان commit از GitHub بررسی و در delivery record اعلام می‌شود؛ شواهد به SHA دیگر منتقل نمی‌شوند.

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
- UI ابزار مقایسهٔ revisionهای صریحاً linked را با predecessor/successor، تغییر فیلدها، فعالیت‌های اضافه/حذف/تغییر‌یافته، متن نویسنده و ترتیب provenance نشان می‌دهد؛ تاریخچهٔ malformed/missing/بزرگ را محدود می‌کند و هیچ مقدار مهندسی یا approval مشتق نمی‌کند. تست محلی UI روی implementation SHA `075d5919…` به‌دلیل نصب‌نبودن PySide6 **SKIPPED** است. Exact-SHA Linux run `42466023039` و Windows run `42466023072` موفق شدند؛ Windows offscreen UI برابر 29/29 در هر دو runtime بود. این شواهد physical Windows display acceptance نیستند.
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
- stability workflow سقف 45 دقیقه و 15 تلاش هر lane را حفظ می‌کند؛ selectorها و assertion/classifierها در این مأموریت تغییر یا تضعیف نشدند. در exact-SHA Windows run `42466023072` هر شش stability lane، هرکدام 15/15، موفق شدند. timeoutهای تاریخی Windows درج‌شده در گزارش‌های پیشین فقط برای همان SHAها معتبرند؛ علت ریشه‌ای آن‌ها همچنان **UNRESOLVED** است و runهای سبز جدید علت قبلی را اثبات نمی‌کنند. تمام CI offscreen است و پذیرش display فیزیکی نیست.

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
