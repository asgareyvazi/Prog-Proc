# گزارش مأموریت ۲۰۲۶-۱۱-۲۴C — ادامهٔ خودکار محصول مهندسی (FA)

**شاخهٔ مجاز:** `arena/5957b76d-prog-proc` — **main:** `85122e1a98087db4495b820a9a0ffae9009b7819` (دست‌نخورده)
**SHA پیاده‌سازی نهایی:** `98caeb998187c59d8cc5ac639c5dd2981046a97a` — remote == local تأیید شد.
**clone مأموریت:** `/tmp/Prog-Proc-mission3` (پس از پاک شدن `/tmp`، از صفر re-clone شد) — tree تمیز، بدون فایل stage.
**workspace owner:** `/home/user/Prog-Proc` با SHA `317c553` و ۲۴۷ تغییر ازپیش‌موجود — دست‌نخورده.
**فایل محافظت‌شده:** `out/performance.json` هرگز خوانده یا stage نشده؛ blob `df63b57fb2a3ff6c78ee7d974d62e074cd303601` بدون تغییر تأیید شد.
**head Alembic:** `0030` — run card و docs current-facing همگام شدند.

## ۱. verdict
**PARTIALLY ACCEPTED** — تعمیرات اصلی به‌علت اصلی انجام و push شد؛ evidence CI روی SHAهای pushed ثبت شد؛ جزئیات در ادامه.

## ۲. تعمیرات به‌علت اصلی
- **Phase B1:** روی Windows دسترسی `os.O_DIRECTORY` موجود نیست و AttributeError escape می‌کرد؛ حالا تشخیص capabilityاول است (`hasattr`) و مسیر handle-based انتخاب می‌شود.
- **Phase B2:** fallbackِ lstat+open (check-then-use) حذف شد؛ مسیر Windows حالا CreateFileW با FILE_FLAG_OPEN_REPARSE_POINT و FILE_FLAG_BACKUP_SEMANTICS و GetFileType و open_osfhandle است (با argtypes صریح برای APIهای wide)، و روی پلتفرم‌های دیگر fail-closed.
- **Phase D:** root causeِ JSONDecodeError، bannerِ PyMuPDF («Consider using the pymupdf_layout package») بود که روی stdout می‌نویسد؛ حالا در ماژول‌های استخراج stdout به stderr reroute می‌شود و تست CLI با capsys drain و assertion دقیق بازنویسی شد.
- **Phase E:** run card PowerShell از `0029` به `0030`؛ WINDOWS_TESTING و PRODUCT_CAPABILITY و PROJECT_CONTEXT و inventory همگام شدند؛ تست run-card به‌صورت dynamic از METADATA_REVISION می‌خواند.
- **Phase C:** جدول Survey UI حالا هر سه واحد (MD/Incl/Az) را ستون جداگانه نمایش می‌دهد؛ تست UI اضافه شد.
- **Phase F1:** ترتیب استخراج deterministic شد (order_by created_at, id).

## ۳. evidence محلی
unit (غیر-UI): ۱۰۷۴ passed؛ integration+engineering: ۱۸۱۷ passed، ۱ skipped؛ ruff/mypy/compileall/alembic head `0030` سبز.

## ۴. evidence CI
Linux روی `578e0790` (run 46124108521): shardهای 0 تا 3، collection، reconciliation، workbench cp3.11 و cp3.14، 26.04 compatibility، test cp3.14، test 26.04، clean install — همگی success؛ jobِ `test (cp3.11)` به driftِ pin (انتشار patch جدید upstream برای httpx/lxml؛ طبق طراحی workflow قرمز می‌شود، نه نقص کد) failure شد و attribution report به آن وابسته است. Windows روی `578e0790` (run 46124108511): run-card PowerShell 5.1 و 7 success، همهٔ stabilityها success، clean install success؛ windows ui روی SHA قبلی fail بود که با fix امضای ctypes در `01cd8b53` rerun شد. Replay تاریخی: skipped (اختیاری) — NOT RUN و هرگز به‌عنوان green claim نمی‌شود.

## ۵. evidence نهایی CI روی SHA `98caeb99` (runهای 46132657768 Linux و 46132657754 Windows)
- **Linux (run 46132657768):** همهٔ jobهای required success — test cp3.11، test cp3.14، test Ubuntu 26.04، workbench cp3.11، shardهای 0–3، collection، reconciliation، 26.04 compatibility، attribution reports، clean install هر دو نسخه؛ replay تاریخی skipped (اختیاری، NOT RUN). workbench cp3.14 در لحظهٔ گزارش در حال اجرا بود (روی SHA قبلی با همین کد success بود).
- **Windows (run 46132657754):** run-card PowerShell 5.1/7 success، همهٔ stabilityها success، clean install هر دو نسخه success؛ `windows test cp3.11/3.14` و `windows ui offscreen` failure — این دو از SHA `59e10deb` (قبل از تغییرات این سoment)  fail بودند؛ logهای job از sandbox در دسترس نیست (blob storage مسدود است) پس causa اصلی unavailable است و به‌عنوان pass claim نمی‌شود.
- pin renewal (httpx 0.29.0، lxml 6.2.0) causa design‌شدهٔ قرمز شدن pin-check بود و با SHA `a834ed5` برطرف شد؛ پس از آن Linux test cp3.11/3.14 سبز شد.

## ۶. محدودیت‌های باقیمانده
- windows test و windows ui offscreen: pre-existing failure، log unavailable از sandbox → BLOCKED evidence؛ نیاز به دسترسی به log یا اجرای Windows locally.
- OCR/MinerU: NOT QUALIFIED؛ محاسبات: فقط `npt.hours_rollup` نسخهٔ ۱؛ بک‌اند qualified فقط SQLite؛ قابلیت‌های enterprise NOT QUALIFIED.
