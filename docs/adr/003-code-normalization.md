# ADR 003 — Lesson Code Normalization & Global Uniqueness

- **Status:** Accepted (2026-10-31)
- **Context:** V10.1 §4
- **Decides:** How ``LessonLearned.code`` is normalized, enforced globally, and how legacy collisions are resolved.
- **Supersedes:** implicit case-sensitive ``uq_lesson_one_current``; ad-hoc per-well checks in promotion writer.

## Decision

### Canonical form
``canonical(code) = str(code).strip().casefold()`` — whitespace-trimmed then Unicode ``casefold``.
- ``strip()`` normalizes accidental leading/trailing spaces and ``" LL-001 "`` → ``"LL-001"``.
- ``casefold()`` not ``lower()`` so ``"ß"`` and ``"SS"`` collide consistently at the Python layer; the DB index uses ``LOWER()`` (SQLite/PostgreSQL) which agrees for the 7-bit codes in production (``LL-…``, ``BP-…``) and is a safe superset for ASCII.  Python is authoritative for the ``ß`` edge case: the DB may accept ``ß`` vs ``SS`` as distinct, but the application rejects them as the same canonical code.

Empty and null codes are distinct from any non-empty code and are exempt (no constraint).

### Scope
- **Global** among *current* lessons (``is_current = 1`` and ``code IS NOT NULL``).  Not per well/field/project.
- Superseded revisions (``is_current = 0``) keep their code but do not participate; this allows ``revise()`` to create a successor with the same canonical code after marking the predecessor not current.
- Both ``LessonLearned`` and promotion writer enforce the same rule; manual ``capture``/``update_lesson``/``revise`` and CLI/UI share ``LessonRepository._ensure_code_unique``.

### Enforcement layers (amended 2026-11-01 for V10.2 §4)

| Layer | Mechanism | Where |
|-------|-----------|-------|
| Application | ``LessonRepository._ensure_code_unique`` — queries ``is_current`` rows and compares ``strip().casefold()`` in Python (via ``code_key`` when present, fallback to ``code``); raises ``ValidationError`` with ``conflicting_id`` | ``src/drilling_intelligence/lessons/repository.py`` |
| Storage (0021 legacy) | Partial unique index ``uq_lesson_code_current_ci`` on ``lower(code) WHERE is_current=1 AND code IS NOT NULL`` (SQLite & PostgreSQL) — retained for history, superseded by ``code_key`` | ``src/drilling_intelligence/database/models.py`` + migration ``20261031_0021_lesson_code_casefold_unique.py`` |
| Storage (0022 authoritative) | Persisted ``code_key = strip().casefold()`` + partial unique index ``uq_lesson_code_key_current`` on ``code_key WHERE is_current=1 AND code_key IS NOT NULL`` — matches ADR exactly, including ``ß`` vs ``SS`` | ``src/drilling_intelligence/database/models.py`` + migration ``20261032_0022_lesson_code_key_canonical.py`` |
| Promotion | ``doc_promotion`` writer computes ``code_key`` from ``code`` and checks ``LessonLearned.code_key == normalized`` + per-batch ``code_key`` set | ``src/drilling_intelligence/operations/promote.py`` |

If application and DB disagree (e.g. ``ß``), application wins — request is rejected before the DB sees it.  From 0022 onward the DB's ``code_key`` index agrees with ``casefold`` exactly (SQLite backfill uses ``lower(trim)`` as approximation then Python corrects ``ß``).

## Legacy Collision Procedure

If migration ``0021`` detects two current lessons with the same canonical code:

1. Query (as in ``upgrade()``):
   ```sql
   SELECT lower(trim(code)), group_concat(id), group_concat(code)
   FROM lesson_learned WHERE is_current=1 AND code IS NOT NULL AND trim(code)!=''
   GROUP BY lower(trim(code)) HAVING count(*)>1;
   ```
2. For each colliding canonical value, choose one survivor to remain ``is_current=1``.  For the others:
   - Either rename: ``UPDATE lesson_learned SET code = '<new-distinct-code>' WHERE id = '<losing-id>'`` and re-run migration; or
   - Supersede via governed revision: ``LessonRepository.revise(losing_id, by=actor, changes={"code": "<distinct>"})`` then approve, or mark superseded if the duplicate is genuinely the same lesson: set ``is_current=0, status='SUPERSEDED'`` and re-point evidence if needed.
3. Re-run ``alembic upgrade head``.  The migration raises ``RuntimeError`` with the canonical value and colliding ids/codes until resolved, so the operator has a diagnostic without guessing.

Historical (non-current) collisions need no action.

## Alternatives Considered

- **Per-well uniqueness:** Rejected — codes are organization-wide identifiers (``LL-2025-014``) printed on reports; allowing ``LL-014`` twice in two wells creates ambiguity when cited.
- **Store canonical column:** Initially rejected for 0021 (functional index lower risk), adopted for 0022 because ``lower()`` ≠ ``casefold()`` (trim and ``ß``).  ``code_key`` is the persisted canonical; backfill is idempotent and collision-checked.
- **DB-only enforcement:** Rejected — ``LOWER()`` alone does not handle ``ß`` and trimming; application check must remain, now backed by ``code_key`` unique index for concurrency.

## Consequences

- New captures/updates/revisions that collide case-insensitively fail fast with ``ValidationError(code already in use)``; concurrent duplicates are rejected at the DB via ``uq_lesson_code_key_current`` (IntegrityError).
- Migration ``0021`` is offline-renderable and live-safe (collision check only when live); ``0022`` backfills ``code_key`` and enforces the exact ``strip().casefold()`` canonical.
- Existing tests that previously tolerated case-divergent duplicates must use distinct canonical codes; ``ß`` vs ``SS`` now collide (casefold) as intended.
