# گزارش مأموریت — ادامهٔ خودمختار مهندسی Prog-Proc (۲۰۲۶-۱۱-۲۲)

## ۱. نتیجهٔ نهایی — PARTIALLY DONE

تعمیرهای defects D-01 تا D-05 در این بخش با موفقیت پیاده، تست و push شدند و CI exact-SHA روی SHA قبلی سبز بود.
بخش‌های بزرگ expansion محصول (Phaseهای H تا S کامل) هنوز NOT RUN هستند؛ verdict کلی PARTIALLY DONE.

## ۲. SHAها و Git

- SHA شروع تأییدشده: `7d95f29e7230f2daf0ad36ab092ff01becba35fe` (remote tip در preflight).
- commit sửa defects: `48040f81a895d4b7de372a03aa58e1c7214816e6`.
- `main` قبل و بعد: `85122e1a98087db4495b820a9a0ffae9009b7819` — دست‌نخورده.
- `out/performance.json`: blob `df63b57fb2a3ff6c78ee7d974d62e074cd303601` دست‌نخورده.
- workspace اصلی `/home/user/Prog-Proc` دست‌نخورده؛ clone ایزوله `/tmp/Prog-Proc-mission2`.

## ۳. نقص‌های تعمیرشده

| ID | نقص | علت ریشه‌ای | sửa | commit |
| --- | --- | --- | --- | --- |
| D-01 | Procedure decisions وضعیت completion را به‌روز نمی‌کرد | ProcedureStep فیلد completion_state نداشت و branchهای مختلف بود | migration `0029` + `_decision_fields` مشترک؛ REJECTED → EXTRACTED_UNVERIFIED برای هر دو kind | `48040f8` |
| D-02 | event با version_id خالی | استفاده از `locator.get("version_id")` به‌جای `document_version_id` | `exact_version_id` + validation سخت | `48040f8` |
| D-03 | docs stale/متناقض | snapshotهای قدیمی | به‌روزرسانی PRODUCT_CAPABILITY / PROJECT_CONTEXT / MASTER_PLAN + این گزارش | این commit |
| D-04 | assertion بی‌اثر `or True` | — | حذف و جایگزینی با assertions معنادار | `48040f8` |
| D-05 |E2E UI برای Procedure | فقط Program تست UI داشت | `test_procedure_source_extraction_ui_end_to_end` کامل | `48040f8` |

## ۴. نتایج آزمون (local)

- `tests/integration/test_source_extraction_integrity.py` + `test_authoring_export_templates.py` + `tests/ui/test_authoring_pages.py`: 52 passed.
- ruff check/format: clean؛ exception review: 133 suppression مستند؛ Alembic head: `0029` (منفرد).

## ۵. CI

- Linux run `44989734232` و Windows run `44989734184` روی `7d95f29e...`: هر دو success.
- runهای SHA جدید (`48040f8` و SHA نهایی این گزارش) پس از push اجرا می‌شوند.

## ۶. محدودیت‌های باقی‌مانده (NOT RUN / NOT QUALIFIED / OWNER-GATED)

- Physical Windows acceptance — NOT RUN.
- OCR/MinerU — NOT QUALIFIED.
- PostgreSQL / auth / multi-user / signatures — NOT QUALIFIED.
- محاسبات غیر از `npt.hours_rollup` V1 — OWNER-GATED.
- گسترش کامل Phaseهای H–S (Program/Procedure authoring workspace، DDR batch import، survey، E2E full) — PLANNED.
