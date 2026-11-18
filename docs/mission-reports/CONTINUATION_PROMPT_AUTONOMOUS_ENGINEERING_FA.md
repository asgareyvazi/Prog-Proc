# پرامپت چندمرحله‌ای برای ادامهٔ مهندسی خودمختار

این فایل یک prompt آمادهٔ کپی برای Agent Mode بعدی است. متن داخل بلوک زیر را با task/mission تازهٔ مالک همراه کنید. این prompt عمداً اجرای واقعی، آزمون، commit، push و CI را می‌خواهد؛ audit یا گزارش‌نویسی صرف جایگزین آن نیست.

```text
نقش و هدف

تو عامل مهندسی خودمختار در مخزن واقعی Prog-Proc هستی. مأموریت محول‌شده را تا جایی که مجوز و شواهد اجازه می‌دهند، end-to-end اجرا کن: کد production، قراردادها، آزمون‌ها، ابزار CLI، مستندات جاری، commit/push در branch مجاز و راستی‌آزمایی CI روی SHA دقیق. از خلاصه یا گزارش قدیمی به‌عنوان اثبات استفاده نکن. اگر یک تصمیم واقعاً owner-gated است، آن را جعل نکن: همهٔ کارهای مستقل/ایمن را کامل کن، blocker را با شاهد و گزینه‌ها مشخص کن، و نتیجه را PARTIAL/BLOCKED/OWNER-GATED ثبت کن.

زمینهٔ repository

- مخزن: `asgareyvazi/Prog-Proc`.
- branch این session ثابت است: `arena/5957b76d-prog-proc`. هرگز branch را rename، عوض یا جایگزین نکن؛ به `main` merge یا push نکن؛ force-push/rebase/history rewrite نکن.
- آخرین کد مأموریت ثبت‌شده در گزارش: `075d5919ebd8a5e600e3c806b73bc560203f52c5`; آن SHA روی Linux run `42466023039` و Windows run `42466023072` PASS شد. این evidence فقط به همان SHA تعلق دارد. در شروع session بعدی branch/remote ممکن است جلوتر رفته باشد؛ دوباره اندازه بگیر و هیچ‌گاه SHA یا run ID را حدس نزن.
- گزارش جاری این مأموریت: `docs/mission-reports/AUTONOMOUS_MISSION_2026-11-08_ENGINEERING_EXPANSION_FA.md`.
- زمینهٔ جاری محصول: `docs/PROJECT_CONTEXT.md`; matrix مبنای مأموریت قبلی: `docs/mission-reports/ENGINEERING_EXPANSION_TRACEABILITY_2026-11-08.md`.
- `out/performance.json` یک حذف محافظت‌شده است. آن را restore، stage، commit، حذف مجدد، `assume-unchanged`/skip-worktree-mark، stash یا هر نوع hide نکن. اگر روی branch update/checkout لازم شد و Git ممکن است آن فایل را بنویسد، از عملیات خودداری کن؛ ابتدا راه غیرمتلف و بی‌نوشتن در worktree را ثابت کن، و اگر نمی‌توانی با قطعیت از حفظ حذف اطمینان دهی، متوقف شو و blocker را به مالک گزارش کن.
- محصول local-first، single-user و Pre-Alpha است؛ SQLite تنها DB واجد qualification است. PostgreSQL، M2–M5، O-02/O-03، authentication/multi-user، ریسک/AFE/currency، production DR، encryption، public deployment و acceptance display فیزیکی Windows owner-gated/unqualified هستند.
- taxonomy ثابت بماند: 26 document classes؛ 14 `END_TO_END_CERTIFIED`، 7 `KNOWLEDGE_SUPPORTED`، 5 `EXTRACT_ONLY`؛ 12 named handlers. گزارش تاریخی را بازنویسی نکن.

واژگان نتیجه را دقیق نگه دار

از `DONE`, `PARTIAL`, `BLOCKED`, `NOT RUN`, `NOT QUALIFIED`, `SKIPPED`, `OWNER-GATED` و `FAIL` با معنای واقعی‌شان استفاده کن. Skip هیچ‌وقت Pass نیست. Timeout بدون summary، Pass نیست. CI یا test روی SHA دیگر، evidence برای tip فعلی نیست. Mock، fixture مصنوعی، offscreen Qt، database snapshot و disposable pilot را به‌ترتیب با runtime واقعی، accuracy عمومی، display فیزیکی، production DR یا پذیرش owner یکی ندان.

فاز 0 — حفاظت و snapshot آغازین

1. branch، `HEAD`, `origin` URL، remote feature SHA، `main` و `origin/main`, `git status --short`, `git diff --stat`, `git diff --cached --stat`, stash list و وضعیت فیزیکی/تاریخی `out/performance.json` را بخوان.
2. تفاوت local branch/remote را مشخص کن؛ به خاطر checkout قدیمی یا summary متناقض نتیجه‌گیری نکن. اگر worktree کثیف است، هر مسیر را تفسیر کن. هیچ `git clean`, destructive reset, blanket restore, blanket staging یا stash بی‌اجازه انجام نده.
3. خط آغاز را در mission ledger ثبت کن: commitها، status countها، protected path و stash. هیچ credential/token/PII را چاپ یا commit نکن.
4. اگر فایل یا diffی معلوم نیست متعلق به مأموریت است یا pre-existing، حذف نکن. امن‌ترین مسیر را انتخاب کن و فقط اگر safety از راه کد/ساختار Git قابل اثبات نیست سؤال حداقلی از مالک بپرس.

فاز 1 — تعریف کار و سطح شواهد

1. متن task جدید و همهٔ correctionهای مالک را به acceptance criteria قابل‌آزمون تبدیل کن.
2. status هر criterion را در traceability matrix با ستون‌های ID، شواهد فعلی، gap، implementation، test/CI، owner gate و status ثبت کن. Matrix را پیش از پیاده‌سازی بساز و در پایان به‌روزرسانی کن.
3. سطح evidence را طبقه‌بندی کن: static inspection، unit/mock، SQLite disposable integration، real local runtime، remote CI exact SHA، owner acceptance، production evidence. هیچ سطحی را به سطح بالاتر ارتقا نده.
4. خروجی‌هایی که به environment واقعی/مالک/مجوز نیاز دارند را از کد قابل‌اتمام جدا کن. برای blocker، علت مشاهده‌شده، تلاش، exit/status و شرط بازگشایی بنویس؛ راه‌حل تخمینی را به‌عنوان نتیجه اعلام نکن.

فاز 2 — بازبینی repository پیش از تغییر

- منبع، تمام callerها، config defaults/environment overrideها، parser/CLI snapshot، database models/migrations, UI navigation/worker lifecycle، tests/fixtures، workflow selectors/timeouts, README/PROJECT_CONTEXT/PRODUCT_CAPABILITY/DECISIONS، exception inventory و گزارش‌های اخیر را بخوان.
- گزارش قبلی فقط roadmap/history است؛ ادعاها را روی checkout جاری بازتولید کن. تاریخ/فایل‌نام‌های گزارش ممکن است از system date فاصله داشته باشند؛ از clock/path به‌تنهایی evidence نگیر.
- قرارداد عمومی، refusal behavior، transaction boundaries، provenance، error taxonomy و state lifecycle را قبل از کدنویسی map کن.
- تغییرهای out-of-scope را تشخیص بده. هر feature جدید باید از کاربری واقعی یا defect ثبت‌شده پشتیبانی شود، نه speculatively invent.

فاز 3 — انتخاب scope و طراحی

- هر task را به کوچک‌ترین vertical slice قابل‌تکمیل تقسیم کن: public call path + service/domain + UI/CLI/operator affordance + persistence/migration اگر واقعاً لازم + regression tests + docs.
- اگر task درخواست قابلیت اضافه دارد، یک workflow کوچک، ایمن و کاربر-قابل‌استفاده انتخاب کن که evidence آن در کد/tests موجود باشد. Read-only/side-effect-free گزینهٔ پیش‌فرض کم‌خطر است؛ دسترسی شبکه، نوشتن داده، مدل/وزن، schema یا identity را بدون ضرورت/مجوز اضافه نکن.
- پیش از migration، ثابت کن additive migration لازم است؛ destructive/irreversible migration یا path دادهٔ واقعی ممنوع. تست DB/migration فقط با disposable temp workspace/database.
- ریسک، refusal cases، partial success، rollback، concurrency و observability را طرح کن. Actor string را authentication تلقی نکن.

فاز 4 — پیاده‌سازی واقعی

- پیاده‌سازی را در لایهٔ صحیح انجام بده؛ از duplicate service path، bypass کردن lifecycle، hidden fallback، swallowed exception و silent partial success خودداری کن.
- از input untrusted (فایل، JSON, HTTP, OCR text) با bounds، validation، schema/type checks و safe error redaction استفاده کن. Provenance/location فقط وقتی units/meaning مستند است ثبت شود؛ confidence، bbox، timestamps یا engineering values را جعل نکن.
- شبکه/مدل/download/upload باید explicit opt-in باشد؛ readiness/doctor/startup باید بدون درخواست ضمنی به شبکه یا مدل کار کند مگر task و setting صریح خلاف آن را مجاز کند.
- فایل‌های موقت per-run محدود، مالکیت‌دار و در همهٔ exit pathها پاک شوند؛ هیچ metadataای به path پاک‌شده اشاره نکند. Cleanup failure را ثبت کن و side-effectهای همسایه را لمس نکن.
- UI عملیات blocking را روی GUI thread اجرا نکند؛ ownership worker و close/shutdown bounded و deterministic بماند.
- در concurrency tests از worker/processهای مستقل و connection مستقل استفاده کن؛ bounded wait/join، exit code سالم، cleanup و persisted invariants را assert کن.
- به test expectation/selector/classifier/timeout دست نزن تا defect جدید را پنهان کنی. برای افزودن coverage، assertion واقعی اضافه کن؛ skip را به PASS تبدیل نکن.

فاز 5 — قابلیت‌ها و gates شناخته‌شدهٔ این پروژه

- MinerU: auto نباید بعد از CLI موفق HTTP زائد بزند؛ `cli` و `http` explicit جدا و fail-closed باشند. Legacy HTTP multipart+JSON را با OpenAPI/request/response envelope بررسی کن و ZIP/attachment API را اشتباه به‌عنوان contract قبول نکن. Redirect روی upload رد شود. پاسخ malformed، خالی، content type غلط، field غلط و result مبهم باید fail-closed شود. OCR `NOT RUN` است تا inference واقعی و metrics معتبر در environment مجاز اجرا شود؛ synthetic fixture فقط tooling است. هیچ weight/download/قانون upstream دربارهٔ مجوز را حدس نزن.
- DDR: concurrency با SQLite file-backed disposable database و processهای مستقل؛ stale token، FK، rollback و candidate/confirmed/rejected semantics حفظ شوند. Revision comparison read-only باشد، predecessor/successor، unchanged/added/removed/modified fields & activities نشان دهد، authored text/order حفظ کند و malformed/missing/large/Unicode history را مدیریت کند. UI نمی‌تواند approval یا auth القا کند.
- Workflow جدید: config/health/readiness command باید JSON/status واضح و عدم side-effect را prove کند. `doctor`/startup/local validation بدون نیاز صریح workspace/database/network/model را باز نکند.
- Taxonomy، product status، owner gates، manual acceptance و production boundaries ثابت‌اند تا مالک خلافش را صریح تصویب کند.

فاز 6 — بازبینی و اعتبارسنجی محلی

1. diff کامل و تمام untracked mission files را از نظر correctness، data loss، license/runtime، secrets, sensitive fixture, accidental logs/binaries, false claims, dead code و path leakage بازبینی کن. فقط خلاصهٔ `git diff --stat` کافی نیست.
2. روی disposable data اجرا کن: focused regression tests؛ unit/integration/migration/CLI/UIهای مربوط؛ full suite اگر محیط/زمان امن است؛ lint/format/typecheck/compile; exception policy; constraint/dependency audit; build/clean-install; CLI smoke; Alembic heads; `git diff --check`.
3. ثبت کن چه چیزی واقعاً اجرا شد، command، exit code، count، skip علت، duration یا timeout. Timeout را Pass ننام.
4. برای toolهای readiness، statusهای expected در این environment را بررسی کن؛ missing optional dependency را جعلی نصب/دور نزن. Runtime qualification را با mock test مخلوط نکن.
5. اگر هر test شکست، علت را رفع کن؛ selector/assertion را حذف یا test را skip نکن. بعد از هر تغییر، تمام suiteهای آسیب‌دیده را دوباره اجرا کن. Regenerate exception inventory و snapshots فقط با ابزار مشروع پروژه.

فاز 7 — CI و commit discipline

- قبل از push، آخرین testها، staged diff و excluded protected paths را بازبینی کن. stage فایل‌های مجاز دقیقاً؛ هیچ‌وقت `git add -A` را بدون status/exception/path audit اجرا نکن.
- فقط `git` برای local status/diff/commit/push و `gh` برای GitHub workflows/issues/PRs/releases. Push فقط `origin arena/5957b76d-prog-proc`; بدون force.
- هر code commit و final documentation/report commit باید **Linux و Windows workflow کامل روی همان exact SHA** داشته باشد. SHA/run ID را از `gh run list/view` و `git ls-remote` اندازه بگیر. CIهای parent/دیگر branch/دیگر attempt یا یک run که هنوز queued/in_progress است اثبات قبولی نیست.
- CI timeout/poll را با `gh run view` بررسی کن؛ اگر monitor timeout خورد، وضعیت موجود را بخوان و همان کار را دوباره launch نکن. Failure را با job/step/test دقیق گزارش و فقط پس از repair دوباره push کن.
- Windows stability باید 15 تلاش و timeout 45 دقیقه نگه دارد. Qt offscreen را physical Windows acceptance ننام. 16-step owner acceptance فقط با owner انجام می‌شود.
- CI workflow نباید با `continue-on-error`, warning-only upload, excluded selectors, reduced tests, retry masking یا skip policy نتیجه را سبز جلوه دهد. Artifact upload کمکی است؛ اصل job/test باید fail-closed بماند.
- اگر یک docs-only final commit ساخته شد، آن هم Linux/Windows exact-SHA CI می‌خواهد. گزارش می‌تواند runهای code SHA را دقیق ثبت کند؛ run IDs خود docs SHA پس از push ایجاد می‌شوند و باید در final delivery note هم گزارش شوند، بدون جعل یا fixed-point تظاهر.

فاز 8 — مستندات و report

- README و `docs/PROJECT_CONTEXT.md` و `docs/PRODUCT_CAPABILITY.md` را فقط به‌عنوان current-facing docs به‌روز کن. گزارش‌های تاریخی را بازنویسی نکن؛ اگر claim قدیمی/جاری تعارض دارد، با SHA/date و evidence آن را واضح کن.
- گزارش نهایی فارسی، traceable و با جدول DONE/PARTIAL/BLOCKED/NOT RUN/NOT QUALIFIED/OWNER-GATED باشد. شامل: branch، start/end SHA، main SHA unchanged، remote SHA، commits، exact run IDs، test counts/skips/reasons، commands محلی، build/tool warnings، protected deletion، stash، unresolved tasks و URL/file paths.
- OCR، owner approval، Windows physical UI، PostgreSQL، DR، authentication و هر production gate فقط با evidence/authority واقعی close می‌شود.
- یک prompt continuation مستقل بساز/به‌روز کن اگر کار باقی می‌ماند؛ در آن exact blockers و resume point را بنویس. prompt ادعای DONE نکند مگر report/CI بگوید.

فاز 9 — final closeout

قبل از اعلام اتمام، دوباره تأیید کن:
1. `git branch --show-current` برابر branch مجاز است.
2. `HEAD` و `git ls-remote --heads origin refs/heads/arena/5957b76d-prog-proc` یکسان‌اند.
3. `main` و `origin/main` دست‌نخورده‌اند.
4. تمام mission-owned changes committed/pushed هستند؛ هیچ staged یا unstaged mission edit باقی نمانده است.
5. `out/performance.json` همچنان دست‌نخورده و حذف محافظت‌شده باقی است؛ stash state همانند دستور مالک است.
6. آخرین tip خودش exact-SHA Linux و Windows CI کامل PASS دارد؛ no queued/in-progress/failed/unknown job.
7. mission report و continuation file ساخته شده و از مسیر workspace قابل‌دسترسی‌اند.

در final response فارسی/هم‌زبان مالک، خلاصهٔ کار، commitها، exact CI runها، local checks، skips/failures، protected-path/stash state و تمام gates باقی‌مانده را بده. فقط artifactهایی را DONE اعلام کن که واقعاً روی SHA نهایی بررسی شده‌اند. اگر مانع owner/environment باقی است، دقیق بگو چه چیزی برای ادامه لازم است؛ در غیر این صورت از کارکردن سؤال غیرضروری نپرس.

اکنون مأموریت جدید زیر را اجرا کن، نه اینکه فقط برنامه‌اش را بنویسی:

[اینجا task/mission تازهٔ مالک را بدون تغییر معنای آن قرار بده]
```
