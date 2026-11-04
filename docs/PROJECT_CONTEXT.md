# مرجع زمینهٔ پروژه — Prog-Proc

- **وضعیت سند:** مرجع زمینهٔ اجرایی؛ نه گواهی پذیرش یا جایگزین شواهد CI
- **تاریخ بازبینی:** 2026-11-04 (Asia/Tehran)
- **مأموریت:** تکمیل مستقل قابلیت‌های محصول و qualification نهایی پس از V10.3
- **شاخهٔ مجاز:** `arena/5957b76d-prog-proc`
- **baseline زندهٔ مأموریت:** remote در آغاز با `112efee5469383fcf3449af3ff09d26370896bbb` تأیید شد. upstream محلی تنظیم نبود؛ HEAD اولیهٔ محلی به‌تنهایی مرجع remote تلقی نشد.
- **SHA کدِ CI-تأییدشده:** `894161a2f3e296e001c03a7b20b0eeb4f57d5882`؛ Linux و Windows روی همین SHA موفق شدند (runهای `41804712405` و `41804712330`).
- **baseline محافظت‌شدهٔ `main`:** `85122e1a98087db4495b820a9a0ffae9009b7819`؛ در این مأموریت تغییر نکرد.
- **migration head در کد این مأموریت:** `0026`؛ باید با Alembic و schema-diff آزموده شود.

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
- چرخهٔ authoring گزارش: `DRAFT → SUBMITTED`. نسخهٔ draft فقط با optimistic freshness check و تا وقتی current و `CANDIDATE` است قابل ویرایش است. هر write توکن `updated_at` را حتی در clock tick تکراری به‌صورت strictly monotonic جلو می‌برد؛ stale-write regression و Windows failure موردی پوشش داده شده‌اند.
- Submit گزارش را خودکار تأیید نمی‌کند. `DdrReport.status` و `WellOperation.status` روی `CANDIDATE` می‌مانند تا review مستقل و action قانونی انجام شود.
- هر activity دستی، provenance با actor/reason/sequence/revision می‌گیرد؛ در submit به `WellOperation` متناظر وصل می‌شود. نبود ORM relationship برای ترتیب flush مجوز کنارگذاشتن FK یا transaction نیست.
- Revision نسخهٔ قبلی را تاریخی (`is_current=False`) می‌کند، عملیات آن نسخه را نیز historical می‌کند و revision تازه را draft نگه می‌دارد. Review روی draft در دسترس نیست؛ superseding revision خود به معنی approval نیست.
- Review actionهای `confirm`/`reject` فقط برای گزارش `SUBMITTED` و current در دسترس‌اند، با re-read و preconditionهای status/current/scope/updated_at؛ reason اجباری است و actor اعلام‌شده باید با `created_by` فرق کند. این مقایسهٔ شناسهٔ ورودی، احراز هویت یا اثبات استقلال شخصی نیست؛ authentication در دامنهٔ این محصول نیست.
- UI ابزار مقایسهٔ revisionهای صریحاً linked را با مقادیر قبلی/جاری و provenance ثبت‌شده نشان می‌دهد؛ این مقایسه متنی است و هیچ مقدار مهندسی مشتق نمی‌کند. اجرای محلی UI در این sandbox به‌دلیل فقدان `libGL.so.1` **SKIPPED** است؛ بااین‌حال Qt-enabled offscreen، stability، full suite، clean-install و گزارش‌های identity-bound روی SHA `894161a2f3e296e001c03a7b20b0eeb4f57d5882` در Linux/Windows موفق شدند (runهای `41804712405`/`41804712330`). Full suite جداگانهٔ Ubuntu 26.04 × CPython 3.14 نیز در run `41804712405` موفق شد؛ این نتایج physical Windows display acceptance نیستند.
- migration `0026`، جدول activity و ستون‌های authoring/revision/currentness را اضافه می‌کند. SQLite self-reference و authoring-state invariant در migration به‌وسیلهٔ trigger محافظت می‌شوند، زیرا SQLite نمی‌تواند self-FK را با `ALTER TABLE ADD CONSTRAINT` ساده اضافه کند. metadata/bootstrap نیز باید همین guardها را ایجاد کند. رفتار PostgreSQL از این کد استنباط یا qualify نمی‌شود.

مسیرهای اصلی: `operations/ddr.py`, `review/actions.py`, `review/service.py`, UI در `ui/main_window.py`, migration `0026`, `tests/integration/test_ddr_authoring.py` و migration tests.

## 5. Database، schema و بسته‌بندی

- `METADATA_REVISION` باید برابر تنها Alembic head باشد؛ اکنون کد `0026` را اعلام می‌کند.
- Migrationهای جدید باید online و `alembic upgrade head --sql` را در SQLite isolated path پشتیبانی کنند. Offline generation نباید workspace file را تغییر دهد یا ایجاد کند.
- Migrationهای additive ممکن‌اند؛ downgrade با رکوردهای authored/registered/historical نباید داده را بی‌صدا حذف کند.
- `schema_diff()` فقط table/column presence را می‌سنجد؛ برای FKها، unique/partial indexها، triggers و رفتار deletion باید آزمون/introspection اختصاصی وجود داشته باشد.
- wheel/sdist clean-install و schema bootstrap از metadata مسیر واقعی محصول‌اند؛ وجود migration در source checkout به‌تنهایی clean install را ثابت نمی‌کند.
- package version فعلی `0.0.2a0` است؛ این شماره و `Development Status :: 2 - Pre-Alpha` تضمین انتشار یا آمادگی عملیاتی نیست.
- `doctor` پیش از migration روی SQLite موجود و دوباره پس از bootstrap/upgrade، `integrity_check`, `foreign_key_check` و storage preflight فقط‌خواندنی انجام می‌دهد؛ خطای storage مانع ادعای سلامت domain و مانع domain/search query می‌شود. بررسی‌های cross-row و drift شاخص فقط پس از preflight موفق انجام می‌شوند؛ repair خودکار نداریم.
- `workspace backup` از SQLite online backup API و manifest شامل SHA-256، UTC، schema revision و storage health استفاده می‌کند؛ مقصد باید تازه باشد. این **database snapshot** است، نه full workspace/source backup: فقط `workspace.toml` و snapshot پایگاه‌داده را دارد؛ corpus/source و search index خارج‌اند و باید جداگانه، با سیاست مجاز اپراتور، حفاظت شوند. archive رمزگذاری نشده است.
- Restore فقط در مسیر تازه و برای workspace دارای identity row فقط در همان absolute root ذخیره‌شده مجاز است؛ re-home، overwrite، migration پنهان و overwrite DB فعال وجود ندارد. بررسی path/member/hash/health و محدودیت منابع پیش از انتشار انجام می‌شود؛ سقف snapshot پایگاه‌داده 8 GiB، overhead آرشیو 16 MiB، metadata یک MiB و تعداد عضو دقیقاً سه است. فقط data dir پیش‌فرض `.drillintel` qualify شده است. هیچ encryption/key-management یا بازیابی source در این قابلیت ادعا نمی‌شود.

## 6. Workbench، CLI و آزمون‌پذیری

- UI باید authoring واقعی DDR، Intake queue، selection، preview، lifecycle controls، provenance و read-only/busy state را ارائه کند؛ جدول import/report یا لیست صرف جایگزین authoring نیست.
- عملیات blocking از طریق controller/worker خارج از GUI thread انجام شود. Qt event waits باید bounded/event-driven باشند؛ از sleep تصادفی، retry پنهان، termination اجباری یا `os._exit` برای سبزکردن تست استفاده نشود.
- مالکیت هر worker تا دریافت `QObject.destroyed` و خروج کامل `QThread.wait()` نگه داشته می‌شود؛ handlerهای نتیجه هنگام close بی‌اثرند و refreshهای معوق پس از خروج signal اجرا می‌شوند تا re-entrancy باعث ساخت worker هم‌زمان یا callback دیررس نشود.
- CLI parser snapshot بخشی از قرارداد عمومی است؛ تغییر عمدی Intake و دستورهای `workspace backup/restore` باید `tests/snapshots/cli_surface.txt` را با آزمون به‌روز کند.
- در این sandbox آزمون Qt محلی ممکن است به‌علت dependencyهای host مانند `libGL.so.1` بارگذاری نشود. چنین موردی **SKIPPED/BLOCKED** است و **PASS** نیست؛ فقط runner با Qt واقعاً اجراشده می‌تواند شواهد offscreen فراهم کند. Offscreen نیز معادل display فیزیکی Windows نیست.

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
