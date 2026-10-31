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

### Enforcement layers

| Layer | Mechanism | Where |
|-------|-----------|-------|
| Application | ``LessonRepository._ensure_code_unique`` — queries ``is_current`` rows and compares ``strip().casefold()`` in Python; raises ``ValidationError`` with ``conflicting_id`` | ``src/drilling_intelligence/lessons/repository.py`` |
| Storage | Partial unique index ``uq_lesson_code_current_ci`` on ``lower(code) WHERE is_current=1 AND code IS NOT NULL`` (SQLite & PostgreSQL) | ``src/drilling_intelligence/database/models.py`` + migration ``20261031_0021_lesson_code_casefold_unique.py`` |
| Promotion | ``doc_promotion`` writer uses ``func.lower(code) == content["code"].casefold()`` + per-batch ``code_key`` set | ``src/drilling_intelligence/operations/promote.py`` |

If application and DB disagree (e.g. ``ß``), application wins — request is rejected before the DB sees it.

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
- **Store canonical column:** Rejected for now — an extra ``code_canonical`` column would duplicate data and require backfill; a functional index achieves the same invariant with less migration risk.  Can be revisited if ``casefold`` divergence becomes material.
- **DB-only enforcement:** Rejected — ``LOWER()`` alone does not handle ``ß`` and trimming; application check must remain.

## Consequences

- New captures/updates/revisions that collide case-insensitively fail fast with ``ValidationError(code already in use)``.
- Migration ``0021`` is offline-renderable (``alembic upgrade --sql``) and live-safe (collision check only when a connection is present).
- Existing tests that previously tolerated case-divergent duplicates must use distinct canonical codes.
