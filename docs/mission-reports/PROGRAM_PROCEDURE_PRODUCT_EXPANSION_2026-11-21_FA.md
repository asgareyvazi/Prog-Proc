# گزارش نهایی مأموریت Program/Procedure Product Expansion — Prog-Proc

**تاریخ اجرای ثبت‌شده:** 2026-10-20 UTC (تاریخ سیستم مأموریت)
**شاخهٔ مجاز:** `arena/5957b76d-prog-proc` (baseline اولیهٔ تأییدشده: `f73f5d2180d79ef1d1f15f2d0f76caa3c4607ed1`)
**SHA نهایی این گزارش:** `405fafa354c642d667083605d832506d664995fa`
**وضعیت کلی:** **PARTIALLY DONE** — فازهای A–K، Q و مستندسازی انجام و push شدند؛ CI exact-SHA برای SHA نهایی در زمان نوشتن گزارش در حال اجراست؛ پذیرش فیزیکی Windows و OCR واقعی همچنان NOT RUN / NOT QUALIFIED.

> گزارش‌های مأموریت پیشین تاریخی‌اند و شواهدشان فقط به SHAهای همان گزارش‌ها محدود است.

## A. Forensic pre-flight — DONE
clone تازهٔ ایزوله در `/tmp/Prog-Proc-mission2` روی `f73f5d21` با tree تمیز و بدون stash؛ `main` (`85122e1a`) دست‌نخورده؛ `out/performance.json` بدون تغییر. محیط بازسازی شد (venv + stub libs برای libGL/libEGL/dbus/xkbcommon؛ Qt offscreen با «env OK»).

## B. Field semantics گزارش replay — DONE
`workflow_conclusion_verified` حالا یعنی «conclusion نهایی observed شد» نه «success»؛ مقدار واقعی در فیلد جداگانهٔ `workflow_conclusion` می‌آید؛ replay validator برای run تاریخی `43695755434` حالا `verified=True` و `conclusion='failure'` را می‌خواهد، پس موفقیت replay قابل‌تفسیر به‌عنوان تغییر failure تاریخی نیست. 65 تست سبز.

## C. Docs به SHA Alemanha — DONE
اسناد current-facing به tip شاخه اشاره می‌کنند؛ گزارش‌های تاریخی دست‌نخورده ماندند.

## D. Qt Program Target editor — DONE
targets tab با add/edit/save/reorder کاملاً از طریق کنترل‌های Qt؛ همهٔ فیلدهای ProgramTarget شامل hole size، casing، formation top، depth/duration/MW/NPT/cost با واحد صریح و provenance. `update_program_target` و `TARGET_FIELDS` حالا provenance را می‌پذیرند. تست‌ها کاملاً UI-driven هستند.

## E. Full Procedure step editor — DONE
همهٔ فیلدهای مدل (equipment، preconditions، parameters JSON، expected result، evidence، safety controls، contingency، hold point، source) در UI و با validation سخت سرویس.

## F. Lifecycle UI — DONE
دکمه‌های validate/submit/review/approve/reject/revise/compare در هر دو صفحه؛ گزارش صادقانهٔ success/error؛ revise revision جدید DRAFT را باز می‌کند؛ approval نسخهٔ قبلی حفظ می‌شود و ارث نمی‌رسد.

## G. Program با Procedure exact-revision link UI — DONE
نمایش زندهٔ NONE/LINKED/STALE با خط «link impact»؛ بدون auto-relink؛ تست UI برای LINKED به STALE و refusal لینک cross-well.

## H. Versioned template library — DONE
`TEMPLATE_FAMILIES` با versionهای immutable (CURRENT/SUPERSEDED)، `resolve_template_version` با پرچم superseded، provenance نسخه در create؛ صفحهٔ «Template Library» در workbench.

## I. Real source extraction — DONE
`extract_candidates` متن plain-text ثبت‌شده را واقعاً می‌خواند (binary/OCR همچنان NOT QUALIFIED) با locator دقیق (document/version/sha256/line range)، همه EXTRACTED_UNVERIFIED؛ `decide_extracted_item` با accept/edit/reject؛ صفحهٔ «Source Extraction» با side-by-side review؛ golden corpus `tests/fixtures/golden_corpus/field_note.md` + تست E2E.

## J. Export redaction در Qt — DONE
combo «full fidelity / redact source paths (path-redaction-v1)» در هر دو صفحه؛ تست ثابت می‌کند redaction فقط در کپی خروجی اعمال می‌شود و رکورد canonical دست‌نخورده می‌ماند.

## K. Design system + dashboard — DONE
`ui/design_system.py` منبع واحد theme؛ dashboard overview با counted زنده از repositoryها (programs/procedures/LINKED/STALE).

## L. Desktop acceptance screenshots — NOT RUN
host sandbox فاقد libGL است؛ PySide6 فقط با stub libs + `QT_QPA_PLATFORM=offscreen` لود می‌شود؛ screenshot فیزیکی گرفته نشد. Run-card Windows در `docs/WINDOWS_TESTING.md` همچنان NOT RUN است.

## M–O. Domain modules / method catalogue / operations integration — DONE (محدودهٔ موجود)
ماژول‌های domaine موجود بودند؛ only `npt.hours_rollup` V1 ejecutable است؛ lIEN Program با operations با exact revision در فاز G.

## P. Security/concurrency audit — DONE (محدودهٔ مصوب)
freshness token، DRAFT-only mutation، no self-approval، no approval inheritance، redaction غیرتخریبی، export اتمیک no-clobber.

## Q. Synthetic-well E2E through Qt only — DONE
`test_synthetic_well_end_to_end_through_qt_only` کل جریان را فقط با کنترل‌های Qt اجرا می‌کند.

## R. Quality matrix — DONE
Suite کامل روی SHA نزدیک نهایی: **2,968 collected / 2,966 passed / 1 failed / 1 skipped / 0 errors**؛ تنها failure (lint gate به‌خاطر فایل تست format‌نشده) در `fe81454` برطرف شد. UI offscreen: 42 passed. Authoring integration: 22 passed.

## S–T. Packaging / docs — DONE (محدودهٔ این مأموریت)

## U. Commit/push — DONE
فقط push به `arena/5957b76d-prog-proc` با fast-forward؛ `main` دست‌نخورده.

## V. Exact-final-SHA CI + replay — IN PROGRESS در زمان نوشتن
runهای Linux/Windows برای `405fafa` در حال اجرا هستند؛ pins با SHA renew (`ab29ca9`) green شد؛ replay تاریخی روی `f73f5d21` green بود (run `44374986517`).

## W. Closure
SHA نهایی: `405fafa354c642d667083605d832506d664995fa`.
