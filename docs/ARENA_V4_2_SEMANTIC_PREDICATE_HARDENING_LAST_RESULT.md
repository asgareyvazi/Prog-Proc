# Arena V4.2 semantic predicate hardening — last result

**Date:** 2026-09-23 (Asia/Tehran)
**Repository:** `asgareyvazi/Prog-Proc`
**Branch:** `arena/01a0c936-prog-proc`
**HEAD before this mission:** `ddfe093da721ca631509674e02d4e255b555ea56`
**V4.2 work commit:** the commit carrying this file — `git log -1 -- docs/ARENA_V4_2_SEMANTIC_PREDICATE_HARDENING_LAST_RESULT.md`
**V3 baseline:** `b7703baf35abd84881432a93264a979c33751c6c` (verified present in the object database,
and verified **not** an ancestor of HEAD)
**Authority:** the checked-out repository. Every figure below comes from a command run during this
mission. The "before" figures were re-derived from a `git worktree` at `ddfe093`, not quoted from any
prior report.
**Design document:** `docs/KNOWLEDGE_SEMANTIC_VOCABULARY.md`

---

## 1. Verdict

**The knowledge layer could not distinguish engineering quantities that shared a lexical field name.**
Four of the five conflicts the V4 forensic corpus produced were not disagreements — they were
different quantities collapsed onto one predicate before the context that separated them was
discarded. The layer now separates them at the point where the context is lost, and the corpus
reports **two** conflicts, both genuine.

No conflict was renamed, deleted, suppressed or resolved. Conflict detection was not weakened.
`doctor` still exits 1.

---

## 2. What was wrong

The mission named three suspected collapses. All three were confirmed, and a **fourth** was found
that the mission did not flag:

| Quantity group | Before | Root cause |
|---|---|---|
| SIDPP / SICP / MAASP | one `surface_pressure` rule with a 7-way label alternation | extraction emitted one field name for four assertions |
| system / pill / kick volume | `mud_volume_bbl` alternated `active/total/mud/trip volume` **and** bare `volume` | same |
| rotary / rheometer speed | two rules both named `rpm`; the unit-only fallback had no context gate | same |
| **MD / TVD** | `md` and `tvd` were listed in `UNIT_SUFFIX_TOKENS` | **vocabulary**: the unit-suffix rule stripped them, reducing `depth_md` and `depth_tvd` to `hole_depth` |

`predicate_for_field` itself was **not** the culprit — it was already conservative, and
`sidpp`/`maasp`/`pill_volume`/`rheometer_rpm` each kept their own names. Every collapse except
MD/TVD was created upstream in extraction, where one `FieldRule` emitted one field name for several
distinct quantities. The MD/TVD collapse was a genuine predicate-layer defect: two depth *roles*
registered as *units*, violating that set's own contract.

---

## 3. Exact before / after

Both states measured by running the 14-file V4 forensic corpus through
ingest → promote → `detect_conflicts`. "Before" from a `git worktree` at `ddfe093`.

### Before — 5 open conflicts

| property | note | values |
|---|---|---|
| `hole_depth` | 6 different values stated by 5 sources | 9000, 9100, 9780, **9850 (TVD)**, 9940, 10125 ft |
| `hole_section_size` | 2 different values stated by 3 sources | 8.5, 12.25 in |
| `mud_volume` | 2 different values stated by 3 sources | 12 bbl (pill), 1450 bbl (system) |
| `rpm` | 2 different values stated by 2 sources | 120 rpm (rotary), 300 rpm (rheometer) |
| `surface_pressure` | 3 different values stated by 2 sources | 420 (SIDPP), 610 (SICP), 1850 (MAASP) psi |

Knowledge items: `ACTIVE 50, CONFLICTED 20, UNVERIFIED 13` = **83**. Vocabulary: **25** predicates,
**57** registered aliases.

### After — 2 open conflicts

| property | note | values |
|---|---|---|
| `hole_section_size` | 2 different values stated by 3 sources | 8.5, 12.25 in |
| `measured_depth` | 5 different values stated by 5 sources | 9000, 9100, 9780, 9940, 10125 ft |

Knowledge items: `ACTIVE 59, CONFLICTED 11, UNVERIFIED 11` = **81**. Vocabulary: **34** predicates,
**89** registered aliases.

The 9,850 ft true vertical depth is no longer counted among the measured depths. It is now its own
predicate, `true_vertical_depth`, and it conflicts with nothing.

### Per-property forensic classification of the original five

| property | classification | disposition |
|---|---|---|
| `surface_pressure` | **pure semantic false conflict** — SIDPP (observation), SICP (observation, other side), MAASP (a *limit*) | removed by separation into `sidpp` / `sicp` / `maasp` / `surface_pressure` |
| `mud_volume` | **pure semantic false conflict** — circulating system vs a batch pumped on purpose | removed by separation into `mud_volume` / `pill_volume` / `kick_volume` / `trip_tank_volume` |
| `rpm` | **pure semantic false conflict** — string speed vs a laboratory instrument setting | removed by separation into `rpm` / `rheometer_speed` |
| `hole_depth` | **mixed** — five genuine measured depths plus one contaminating TVD | de-contaminated: now `measured_depth` with 5 values; the TVD moved to `true_vertical_depth`. The disagreement is real and stays open |
| `hole_section_size` | **genuine conflict** — an 8½ in section beside a 12¼ in section | **unchanged**, stays `OPEN` |

Net: 3 pure semantic false conflicts eliminated, 1 real conflict de-contaminated, 1 real conflict
untouched.

---

## 4. What changed

Five source changes plus eight new predicate registrations. No schema change.

1. **`knowledge/facts.py`** — removed `md`/`tvd` from `UNIT_SUFFIX_TOKENS` (21 → 19) and expanded
   that set's docstring to state why roles are not units. Added `depth_md*` / `depth_tvd*` as
   explicit aliases of the existing `measured_depth` / `true_vertical_depth`.
2. **`extraction/fields.py`** — split the `surface_pressure` rule into `sidpp`, `sicp`, `maasp` and a
   generic `surface_pressure`. This also fixed a pre-existing inconsistency where `MAASP` and
   "Maximum allowable annular surface pressure" resolved to *different* predicates.
3. **`extraction/fields.py`** — new `pill_volume`, `kick_volume`, `trip_tank_volume` rules ordered
   before `mud_volume_bbl`, which lost `trip volume` and gained `system volume|pit volume`.
4. **`extraction/fields.py`** — new label-anchored `rheometer_speed` rule before the two `rpm`
   rules, **with the unit required**.
5. **`knowledge/facts.py`** — new `bit_size` predicate; removed from `hole_section_size.fields`.
6. **`knowledge/facts.py`** — registered `PredicateSpec`s for `sidpp`, `sicp`, `maasp`,
   `surface_pressure`, `pill_volume`, `kick_volume`, `trip_tank_volume`, `rheometer_speed`, each with
   a dimension. **Found during this audit:** the first five fixes added extractor rules that emitted
   names with no matching `PredicateSpec`, so those quantities existed only as bare strings with no
   dimension and therefore no unit checking. Registering them is what makes the separation
   checkable rather than merely nominal.

`MASP` was added as an alias of `maasp` — a common field abbreviation for the same quantity that was
previously not recognised at all.

Rule order inside `_RULES` is the classification mechanism, not an implementation detail: a specific
label-anchored rule must precede the generic one so its claimed span wins.

---

## 5. Why the fix is load-bearing

`test_folding_two_quantities_back_together_restores_a_phantom_conflict` monkeypatches
`knowledge.facts.predicate_for_field` to fold `rheometer_speed` back onto `rpm` and
`true_vertical_depth` back onto `measured_depth`, then runs the **whole** pipeline under it. Two
conflicts appear — `rpm` with `{120 rpm, 300 rpm}` and `measured_depth` with `{9940 ft, 9850 ft}` —
between documents that never disagreed, with no change to any source file.

`KnowledgeFact.from_field` resolves that name through the module globals, so the patch changes what
the real promotion path writes. There is no stand-in implementation in the test.

---

## 6. Lookup-key identity

`KnowledgeFact.lookup_key` is built from `subject_key(well_id, section_id,
property_name=self.predicate, record_state)`. **The predicate is the discriminator in the identity
key**, and values are deliberately excluded so that disagreement collides rather than coexisting.

That is what makes predicate separation sufficient on its own: different predicates ⇒ different
lookup keys ⇒ conflict detection, which groups by lookup key, separates them with no further
change. `test_a_separated_quantity_keeps_its_own_document_and_locator` asserts
`property:<predicate>` appears in every separated item's key.

---

## 7. Backward compatibility and migration

**No migration is required.** No column, table, index or Alembic revision changed —
`git diff --name-only` touches no `models.py` or `alembic/` file. New predicate *names* are data,
not schema.

**But the repair is not retroactive**, and this is a real limitation:

`KnowledgeFact.from_field` derives the predicate from the *stored* field name, not from the source
text — deliberately, so a rebuild reads what was recorded and gets the same answer years later.
Documents ingested before this change stored the collapsed name. The discriminating label survives
in `provenance.excerpt`, but `from_field` does not re-read it.

* A `knowledge rebuild` will **not** repair pre-existing rows; it faithfully reproduces the old
  predicate.
* **Re-ingesting the source documents will**, because the extractor now emits the separated names.
* Existing rows do not break. Old predicate names remain valid and conflict detection over them is
  unchanged; they are simply less precise than newly ingested rows.

A schema migration would be the wrong instrument: there is no schema change to express, and
rewriting stored predicates without re-reading sources would invent distinctions the stored data
does not carry.

---

## 8. Documentation integrity

**Defect found and corrected.** `docs/ARENA_V4_1_PRODUCTION_HARDENING_LAST_RESULT.md` claimed
"HEAD after this mission: `f4b45fb…`" while the branch was at `ddfe093…`.

Root cause is structural, not a typo: committing the sentence that names a commit necessarily
produces a *different* commit, so a document can never contain the SHA of the commit that carries
it. Restating the figure would repeat the error, so the un-holdable claim was **removed** rather
than corrected — the work commit (a static fact) is kept, and the carrying commit is left to
`git log`. History was not rewritten; the correction states what it did and why.

**Regression check added:** `tests/unit/test_report_integrity.py` (4 tests) fails any report that
makes the claim at all, fails any 40-hex SHA a report names that is not in the object database
(`git cat-file -e`, not `rev-parse --verify`, which succeeds for absent objects), and verifies the
V3 baseline really is not an ancestor of HEAD.

Mutation-verified: re-introducing the bad header line and a zeroed SHA produced **2 failures**;
restoring the file returned **4 passed**.

Other reports' SHA claims were enumerated and all resolve to present objects.

---

## 9. Tests

New: `tests/integration/test_knowledge_semantic_vocabulary_v42.py` — **24 tests**, covering the
collision matrix (both layers), the three conflict categories, registry integrity, unknown-field
safety, the mutation proof, provenance, retrieval and unit preservation.

New: `tests/unit/test_report_integrity.py` — **4 tests**.

Updated (not weakened):
* `tests/unit/test_knowledge_facts.py` — the assertion `predicate_for_field("Bit Size (in)") ==
  "hole_section_size"` was deliberate and is now `bit_size`, with the reasoning recorded inline.
* `tests/integration/test_knowledge_conflict_semantics_v4.py` — `EXPECTED_CONFLICTS` reduced from 5
  to the 2 genuine disagreements, with the four collapsed quantities documented in the comment, and
  a `REQUIRED_DISTINCT_PREDICATES` guard added. The tests that exercised a since-separated conflict
  were repointed at a conflict that still exists; no assertion was deleted or loosened.
* `tests/unit/test_cli.py::TestKnowledgeCommands` — 4 tests pinned exact corpus counts. They were
  **not** retyped blindly: the delta was traced row by row against a `git worktree` at `ddfe093`
  (§9.1) before any number was changed.

### 9.1 The CLI count change, traced

`knowledge status` over the CLI corpus reported **64** facts and now reports **62**. Both missing
rows are duplicates, established by dumping every depth row's predicate, value, unit, source file,
record state and status in both trees:

| | before (`ddfe093`) | after |
|---|---|---|
| `hole_depth` | **10 rows**, including 9,850 ft from `mud_report_well-a3.xlsx` and 10,125 ft from the same file | **0** |
| `measured_depth` | 1 row — 10,125 ft, `mud_report_well-a3.xlsx`, **UNVERIFIED** | **9 rows**, all ACTIVE |
| `true_vertical_depth` | 1 row — 9,850 ft, **UNVERIFIED** | 1 row — 9,850 ft, **ACTIVE** |

The mud report states 10,125 ft MD and 9,850 ft TVD. Because `md`/`tvd` were registered as units,
each was written **twice**: once collapsed under `hole_depth`, and once under its own predicate. The
collapsed copy was ACTIVE and the correct copy was UNVERIFIED — so the layer held a shadow row that
outranked the real one. Each quantity is now recorded once, under the predicate that names it:

* the 9,850 ft shadow under `hole_depth` is gone and the `true_vertical_depth` row is corroborated;
* the 10,125 ft shadow under `hole_depth` merged with the `measured_depth` row of the same value
  from the same file.

That is the whole delta, and it accounts for every other number that moved: `UNVERIFIED` 9 → 7,
`quantity` 45 → 43, `by_entity_type.well` 18 → 16, `relations` 64 → 62,
`index.knowledge_chunks` 64 → 62, and the `--well A-3` narrow count 18 → 16. **No evidence was
dropped** — two records that described the same assertion twice were merged, and the survivors went
from uncorroborated to corroborated. `open_conflicts` stays 0 on this corpus.

Full suite result: see §10.

---

## 10. Verification actually run

| Check | Command | Result |
|---|---|---|
| New V4.2 suite | `pytest tests/integration/test_knowledge_semantic_vocabulary_v42.py` | **24 passed** |
| Report integrity | `pytest tests/unit/test_report_integrity.py` | **4 passed** |
| Conflict semantics | `pytest tests/integration/test_knowledge_conflict_semantics_v4.py` | **6 passed** |
| Knowledge + extraction focused set (7 files) | `pytest …` | **129 passed** before the new suites landed |
| Full suite | `pytest -p no:cacheprovider -o addopts="--strict-markers"` | **1338 passed, 3 skipped, exit 0** (730.83 s) |
| Lint | `./.venv/bin/ruff check src tests` | clean |
| Compile | `./.venv/bin/python -m compileall -q src` | clean |

The suite was 1310 passed / 3 skipped before this mission; the 28 added are the 24 semantic
vocabulary tests and the 4 report-integrity tests, so the arithmetic closes exactly.

An intermediate run surfaced 4 failures in `tests/unit/test_cli.py::TestKnowledgeCommands`. They were
traced to their cause and fixed at the cause (§9.1) rather than by retyping the constants first; the
suite above is the run *after* that fix.

Performance, measured on the 14-file corpus after the change: ingest **0.72 s**, promote **0.16 s**,
`detect_conflicts` **0.03 s**, and 20,000 `predicate_for_field` calls in **0.013 s** (~0.65 µs each).
Growing the alias table from 57 to 89 entries is not measurable.

---

## 11. Known limitations

Stated plainly; none are hidden by a passing test.

1. **Not retroactive.** Pre-existing rows keep their collapsed predicates until their sources are
   re-ingested (§7).
2. **An unqualified reading stays generic.** `600/300 rpm` with no instrument named resolves to the
   generic `rpm` predicate, reports one value (300) and carries the note *"field inferred from the
   unit alone (no label in the source text)"*. It is not promoted to `rheometer_speed` and not split
   into two assertions. The leading `500` is not captured. This is the conservative answer, and
   inferring the instrument from the number is prohibited.
3. **A parenthesised unit in a header is stripped.** A table header literally spelled
   `Depth (ft MD)` reduces to `depth` and lands on `hole_depth`. The repair is for the extractor to
   emit `depth_md`/`depth_tvd`, which it does for labelled prose. Pinned by
   `test_a_header_that_strips_its_unit_still_resolves_conservatively` so it stays a known gap rather
   than a surprise.
4. **A mud workbook's summary total and a prose total can take different predicates.**
   `total_mud_volume_bbl` is unregistered and keeps its own name, while prose `Total system volume`
   becomes `mud_volume`. Pre-existing, unchanged by this mission, and it means those two would not
   conflict if they disagreed. Recorded here rather than fixed by widening an alias on a guess.
5. **`doctor` exits 1.** Two genuine disagreements remain open. A green `doctor` over a disputed
   measured depth would be the actual defect.

---

## 12. What was deliberately not done

* No conflict row was renamed, deleted or resolved.
* Conflict detection was not weakened; `detect_conflicts` is unchanged.
* No winner was chosen by counting; every voice in a real conflict stays `CONFLICTED`.
* `doctor` was not made to exit 0.
* No quantity was inferred from a numeric value.
* No filename-, corpus- or value-specific conditional was added anywhere.
* No synonym or abbreviation expansion was added to search; that would be inference in a
  deterministic retrieval path.
* No LLM or AI dependency, and no new third-party dependency of any kind.
* No history rewrite: no reset, rebase, squash or force-push.
