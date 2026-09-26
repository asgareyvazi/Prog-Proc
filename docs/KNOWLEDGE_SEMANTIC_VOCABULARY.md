# The knowledge semantic vocabulary

**Date:** 2026-09-23 (Asia/Tehran)
**Applies to:** `src/drilling_intelligence/knowledge/facts.py`, `src/drilling_intelligence/extraction/fields.py`
**Status:** normative. `tests/integration/test_knowledge_semantic_vocabulary_v42.py` is its executable form.

---

## 1. The rule

> **A predicate represents an engineering assertion — not a field name, not a unit dimension, and
> not a numeric type.**

Everything else in this document follows from that sentence.

Two values may share a unit and a dimension and still be different quantities. A kick sheet's
12 bbl pill and a mud report's 1,450 bbl active system are both volumes in barrels. They are not the
same statement, and a system that treats them as the same statement will report a conflict between
two documents that agree about everything.

The corollary is about *when* context may be dropped:

> **Context must not be discarded before semantic identity is established.**

A label like `SIDPP` is not decoration on a number — it is the part that says what the number
*means*. Discarding it and keeping the number is the specific mistake this vocabulary exists to
prevent.

---

## 2. Why dimensional equality is not sufficient

The obvious test for "same property" is "same dimension and same unit". It fails constantly:

| Values | Dimension | Unit | Same quantity? |
|---|---|---|---|
| SIDPP 420 psi / SICP 610 psi | pressure | psi | **No** — drillpipe side vs annulus side, taken at the same instant; they differ by the annular friction loss |
| MAASP 1850 psi / SIDPP 420 psi | pressure | psi | **No** — one is a *limit*, the other an *observation*. Comparing them is a category error |
| active volume 1450 bbl / pill volume 12 bbl | volume | bbl | **No** — a circulating system vs a batch pumped on purpose |
| rotary speed 120 rpm / rheometer speed 300 rpm | rotational speed | rpm | **No** — the string turning vs a laboratory instrument's setting |
| MD 9940 ft / TVD 9850 ft | length | ft | **No** — along-hole distance vs vertical distance. These differ by design in any deviated well |
| bit size 12¼ in / hole size 12½ in | length | in | **No** — the bit is deliberately smaller, and a washed hole is larger still |
| `mud_weight` / `MW (ppg)` | mud weight | ppg | **Yes** — one property, two spellings |
| `hole_size` / `hole_size_in` / `Hole Size, in` | length | in | **Yes** — a unit suffix is not a property |

Dimensional equality answers "could these be compared?" It does not answer "are these the same
question?" Only the second question decides whether two values belong in one conflict group, and
only the *label* answers it.

Note the last two rows: the rule cuts both ways. Aliasing must still merge, or every report that
writes `MW` instead of `mud_weight` becomes a separate unanswerable question.

---

## 3. Where identity is decided — two layers

Identity is decided twice, and the two layers fail in different ways, so they are tested separately.

### Layer 1 — extraction: phrase → canonical field name

`extraction/fields.py`. A `FieldRule` matches source text and emits a field name. **This is where
the context is lost if it is ever lost**, because after this point only `name`, `value`, `unit` and
`note` survive.

Rule order inside `_RULES` is load-bearing. `FieldExtractor.scan_text` runs two passes —
label-anchored rules first, then unit-only fallbacks — and a span claimed by a label-anchored rule
cannot be re-reported by a fallback. A specific rule must therefore precede the generic one it
refines:

```
sidpp / sicp / maasp        before  surface_pressure
pill_volume / kick_volume / trip_tank_volume   before  mud_volume_bbl
rheometer_speed             before  rpm
```

Two regex lessons are encoded in the rules and worth repeating:

* **An optional unit lets the engine take the wrong number.** With the unit optional,
  `500/300 rpm` matched `500` and split one reading across two predicates. Requiring the unit forces
  the correct capture.
* **A context gate is what makes a rule safe.** An ungated `rheometer_speed` rule would claim any
  `300 rpm` as a rheometer reading, which is the mirror image of the bug being fixed.

### Layer 2 — vocabulary: field name → predicate

`knowledge/facts.py`. `predicate_for_field(name)` maps a canonical field name onto a
`PredicateSpec`. It tries four lookups in order:

1. the name as written;
2. the name with a parenthesised unit removed (`Hole Size, in` → `hole size`);
3. the snake-cased name;
4. the longest prefix whose remainder is a registered *unit* token (`mud_weight_ppg` → `mud_weight`).

If none matches, **it returns the snake-cased name with `spec=None`**. It never guesses a nearby
predicate. An unknown field is kept as its own property — see §8.

Step 4 is why `UNIT_SUFFIX_TOKENS` must contain *units only*. `md` and `tvd` were listed there, so
`depth_md` and `depth_tvd` were both reduced to `hole_depth` — a measured depth and a true vertical
depth became one property, and 9,850 ft of TVD sat among measured depths looking like a conflict.
They are depth *roles*, not units, and they are now registered as aliases of `measured_depth` and
`true_vertical_depth`.

---

## 4. Predicate identity

A `PredicateSpec` is `(name, label, dimension, value_type, fields, planned_by_default)`.

* `name` is the identity. It appears in `lookup_key` and in `KnowledgeItem.predicate`.
* `dimension` is what makes unit checking possible. **Every new predicate must declare one** — a
  predicate with `dimension=None` accepts any unit, which is how a barrel slips in beside a psi.
* `fields` are the canonical field names that *are* this assertion. No two predicates may claim the
  same field name; that is a silent merge waiting to happen and the registry makes it a test
  failure.

### Adding a predicate safely

1. **Name the assertion, not the label.** `sidpp`, not `shut_in_pressure_reading`.
2. **Declare the dimension.** `PRESSURE`, `VOLUME`, `ROTARY_SPEED`, `LENGTH`.
3. **List every canonical field name** that denotes it, including the extractor's own output and any
   unit-suffixed variant (`pill_volume`, `pill_volume_bbl`).
4. **Add the extraction rule that emits the name, ahead of any generic rule it refines**, with the
   unit required and the context gated.
5. **Check it did not steal anything.** Re-run `predicate_for_field` over the collision matrix. In
   particular confirm that names which *used* to resolve elsewhere still do, and that unknown names
   are still unknown.
6. **Add the row to the collision matrix** in `test_knowledge_semantic_vocabulary_v42.py`.

Steps 5 and 6 are the ones that get skipped, and skipping them is how `md`/`tvd` survived as units.

---

## 5. Aliasing: how to decide

Ask one question: **if two sources gave different values for these two labels, would an engineer
call that a disagreement about one thing?**

* **Yes → alias.** `mud_weight` and `MW (ppg)` are one property. A unit suffix, a plural, an
  abbreviation and a spelling variant are all aliases.
* **No → distinct predicate.** SIDPP and SICP are distinct. So are a limit and a reading, a system
  volume and a batch volume, and a measured depth and a vertical depth.

Three traps:

* **A suffix that is itself a predicate is not a unit.** `mud_weight_in` is inlet density, a
  different measurement from `mud_weight`. The longest-registered-name rule exists precisely so the
  two do not merge.
* **Numerical agreement is not evidence of identity.** "12¼ in bit in a 12¼ in hole" is normal, and
  so is a 12¼ in bit in a 12½ in washed hole. `bit_size` used to be an alias of
  `hole_section_size`; where the two differed, that produced a conflict between a bit and a hole.
* **Do not infer a quantity from a value.** `300` does not mean "rheometer". No predicate may be
  chosen on the basis of a number.

---

## 6. Unit normalisation

Separating quantities is not a licence to normalise values.

* The source value and the source unit are preserved (`original_value`, `original_unit`).
* A normalised pair (`normalized_value`, `normalized_unit`) is recorded *alongside*, never instead.
* A unit the vocabulary does not recognise is kept as given, not converted.
* A `dimension` on the predicate is what lets a wrong unit be *detected*. Detection is not
  conversion: the value is reported, flagged, and left alone.

---

## 7. Ambiguity

Ambiguity is a legitimate outcome and must stay visible.

* **An unqualified number stays generic.** `600/300 rpm` with no instrument named resolves to the
  generic `rpm` predicate and carries the extractor note *"field inferred from the unit alone (no
  label in the source text)"*. It is not promoted to `rheometer_speed` and it is not split into two
  assertions. One reading is reported, as the generic quantity it is.
* **A labelled value is never marked inferred.** The note is the evidence that the label was absent.
* **A documented gap:** a table header literally spelled `Depth (ft MD)` reduces to `depth` and
  lands on `hole_depth`, because parenthesised-unit stripping is a general rule. Widening that would
  mean guessing from a unit, which is prohibited. The repair is for the extractor to emit
  `depth_md`/`depth_tvd`, which it does for labelled prose.

An unqualified `RPM = 300` may legitimately remain generic and unverified. That is a correct answer,
not a gap to be papered over.

---

## 8. Unknown-field safety

`predicate_for_field` is conservative by design: a name it has never seen returns its own
snake-cased name with `spec=None`.

This is a **negative invariant**, and it is the one that keeps the vocabulary from quietly growing:

* `torque_on_bit`, `shoe_test_pressure`, `yield_point`, `total_mud_volume_bbl`,
  `surface_pressure_reading`, `bit_size_nominal` all keep their own names.
* Appending a unit to an unknown name leaves it unknown. `surface_pressure_reading_ft` is not
  `surface_pressure`.

The alternative — snapping an unknown name to the nearest registered predicate — is exactly the
failure that merged MD with TVD. It produces plausible-looking answers and destroys distinctions
that were never recorded, so nothing downstream can recover them.

---

## 9. Lookup-key identity

`KnowledgeFact.lookup_key` is built from `subject_key(well_id, section_id,
property_name=self.predicate, record_state)`.

**The predicate is the discriminator in the identity key.** Values are deliberately *not* part of
the key, so two sources that disagree collide here instead of coexisting as unrelated rows.

That is what makes predicate separation sufficient on its own: give two quantities different
predicates and they get different lookup keys, and conflict detection — which groups by lookup key —
separates them with no further change. It also makes predicate *merging* dangerous in the same
measure, which is why `test_folding_two_quantities_back_together_restores_a_phantom_conflict`
exists: folding `rheometer_speed` onto `rpm` re-creates a conflict between a 300 rpm rheometer
setting and a 120 rpm rotary speed, with no change to any source document.

---

## 10. Conflict detection

`detect_conflicts` groups statements by lookup key and distinguishes three things a naive
implementation would all call "a conflict":

* **conflict** — two or more *sources* state different values. Stays `OPEN`; a human decides.
* **agreement** — two or more sources state the *same* value. Corroboration, not an argument.
* **ambiguous** — two values inside one revision of one file. The knowledge layer will not claim a
  document contradicts itself in the sense an engineer would mean.

Only the first raises the number `doctor` reports. Every voice in a real conflict is marked
`CONFLICTED`, including the majority one: flagging only the odd value out would imply the majority
*is* the answer, which is choosing a side by counting instead of by deciding.

Nothing here picks a winner. `resolve_conflict` records a human decision with attribution
(`by`, `note`, `at`, `candidates_at_resolution`); the resolution kind goes to `conflict.status`.

**Three of the five V4 conflicts were not disagreements.** They were different quantities sharing a
predicate. The correct repair was to separate the quantities, not to suppress the report, rename the
rows, delete them, or weaken detection. Two genuine disagreements remain and stay open by design —
`doctor` exits 1 over them, and a green `doctor` there would be the actual defect.

---

## 11. Provenance

Every fact keeps `document_version_id`, a `lookup_key`, and a `provenance` entry carrying
`document_id`, `document_version_id`, `filename`, `parser`, `excerpt` and `source_sha256`, plus a
`locator_ref`. Splitting a predicate must not blur which document said what: each separated quantity
is still individually citable, and both sides of a real disagreement remain searchable.

Search indexes every fact including `CONFLICTED` ones. Hiding one side of a dispute would make the
knowledge base look more settled than it is.

---

## 12. Search

`tokenize.term_counts` splits on underscores and does no stemming. `sidpp` and `shut in drillpipe
pressure` therefore share no terms, and **no synonym or abbreviation expansion is performed** — that
would be inference in a deterministic retrieval path. Retrieval finds what the source said; the
predicate layer is what decides what it meant.

Both are needed: separation must not cost retrievability, so every separated quantity is asserted to
remain findable.

---

## 13. Backward compatibility and retroactive repair

The vocabulary change itself needs **no migration**: no column, table or index changed, and new
predicate *names* are data rather than schema.

V4.2 reported the repair as **not retroactive**, reasoning that `KnowledgeFact.from_field` derives
the predicate from the stored field name, so a rebuild reproduces whatever the extractor emitted at
ingest time. **That conclusion was wrong, and V4.3 corrects it.**

A stored entry is not only a name and a number. It also carries `provenance.excerpt` — the span of
source text the value was read from:

```
name="hole_depth"        value=10125  unit=ft   excerpt="MD 10125 ft"
name="surface_pressure"  value=420    unit=psi  excerpt="SIDPP 420 psi"
```

The context was therefore **recorded, not lost**; it was simply never consulted. Re-running the same
deterministic extractor over that recorded span recovers the field name an entry would have today —
without re-reading the source document and without inventing anything, because the excerpt is the
source's own words and the extractor is the same pure function that ran at ingest.

`knowledge rebuild` is therefore genuinely retroactive:

* the **artefact is never rewritten** — `document_json` stays a record of what the extractor produced
  at the time, so "a rebuild reads what was recorded and gets the same answer years later" still
  holds, and the evidence of what the old extractor did is preserved;
* recovery happens on the way **out**, in the derivation, which makes it **idempotent by
  construction** rather than by bookkeeping;
* it is **selective** — see §16. A row whose excerpt carries no label keeps the generic name it has,
  and a row with no excerpt is reported as needing re-extraction rather than silently kept or
  silently guessed at.

---

## 14. The extraction / vocabulary boundary

Two rules, and the difference between them is the whole architecture:

> **If semantic information exists in the source, the extraction layer must preserve it.**
>
> **The vocabulary layer must not reconstruct source context that extraction discarded.**

The converse also holds: the vocabulary layer *may* normalise known aliases, once semantic identity
is already established.

`"Depth (ft MD)"` is the case that draws the line. `tableshape.without_units` used to delete every
parenthetical, so `"Depth (ft MD)"` and `"Depth (ft TVD)"` both became `"depth"` — the qualifier was
gone before any contract could read it, and no downstream layer could recover it. That is an
extraction-boundary defect and it was fixed there: a parenthetical is now dropped only when it
carries no declared **semantic qualifier**.

```python
SEMANTIC_QUALIFIERS = ("md", "tvd")
```

`"Depth (ft MD)"` → `"depth md"` → `depth_md` → `measured_depth`.
`"Depth (ft)"`, `"Depth"`, `"Depth (m)"` → `"depth"` — and stay there. A bare depth is ambiguous and
manufacturing `md` for it would be inventing a measurement.

The qualifier list is deliberately tiny and declared. `header_unit` already applied exactly this rule
to units — "an unrecognised parenthetical is a clarification, not a unit" — and `"Remarks
(optional)"`, `"Qty (approx)"` and `"Serial No (S/N)"` are still discarded rather than folded into a
column's name. The new part is only that a *qualifier* is meaning and survives.

### Canonical field vs canonical predicate

```
field      how the source measurement was extracted     "depth_md"
predicate  what engineering assertion it represents     "measured_depth"
```

The field name is not the final semantic identity. `"Depth (ft MD)"` and `"MD"` are different fields
in different sources and the same assertion; `"bit_size"` and `"hole_size"` may hold the same number
and are different assertions.

---

## 15. Adding to the vocabulary — governance

**Add an alias** only when all three hold:

* the same engineering quantity;
* the same semantic role;
* a different source expression.

`total_mud_volume` became an alias of `mud_volume` on that basis: the mud contract's own
`SUMMARY_ALIASES` already maps "total mud volume", "mud volume" and "active system volume" to one
property, and the golden report states the same 1,450 bbl three ways. Sharing the unit `bbl` is not
evidence and was not the reason.

**Add a predicate** when the engineering assertion is different. SIDPP, SICP and MAASP are three.

**Preserve ambiguity** when source context is insufficient. An unqualified `RPM = 300` stays the
generic `rpm`, marked as inferred from the unit alone.

**Change the extraction layer** when semantic context exists in the source but is lost before the
canonical field is created. Never patch that downstream: the vocabulary layer cannot recover what
extraction threw away, and pretending otherwise produces plausible answers with no trace in the
data.

**Never** add an alias merely because two labels denote volumes, or pressures, or depths.

---

## 16. The semantic repair contract

`knowledge/recovery.py` recovers the canonical field name of an already-stored entry. It is a
closed, auditable table — not a heuristic.

```python
SPLIT_PREDICATES = {
    "surface_pressure":  ("sidpp", "sicp", "maasp"),
    "mud_volume":        ("pill_volume", "kick_volume", "trip_tank_volume"),
    "rpm":               ("rheometer_speed",),
    "hole_depth":        ("measured_depth", "true_vertical_depth"),
    "hole_section_size": ("bit_size",),
}
```

| Stored field | Old predicate | New predicate | Deterministic? | Action |
|---|---|---|---|---|
| `hole_depth` + `"MD 10125 ft"` | `hole_depth` | `measured_depth` | yes | recover |
| `hole_depth` + `"TVD (ft) 9850 ft"` | `hole_depth` | `true_vertical_depth` | yes | recover |
| `hole_depth` + `"9,000 ft"` | `hole_depth` | — | **no** | ambiguous, reported |
| `hole_depth` + *(no excerpt)* | `hole_depth` | — | **no** | requires re-extraction |
| `surface_pressure` + `"SIDPP 420 psi"` | `surface_pressure` | `sidpp` | yes | recover |
| `surface_pressure` + `"SICP 610 psi"` | `surface_pressure` | `sicp` | yes | recover |
| `surface_pressure` + `"MAASP 1850 psi"` | `surface_pressure` | `maasp` | yes | recover |
| `surface_pressure` + `"1850 psi"` | `surface_pressure` | — | **no** | ambiguous, reported |
| `rpm` + `"Rheometer reading at 500/300 rpm"` | `rpm` | `rheometer_speed` | yes | recover |
| `rpm` + `"with 120 rpm"` | `rpm` | `rpm` | n/a | unchanged — stays generic |
| `mud_volume` + `"kick volume 12 bbl"` | `mud_volume` | `kick_volume` | yes | recover |
| `mud_volume` + `"1,450 bbl total system volume"` | `mud_volume` | `mud_volume` | n/a | unchanged — confirmed |
| `hole_section_size` + `"12 1/4 in bit"` | `hole_section_size` | — | **no** | ambiguous, reported |
| `bit_size` + any | `bit_size` | `bit_size` | n/a | unchanged — already specific |
| `mud_balance` + `"2025-05-30"` | `mud_balance` | `mud_balance` | n/a | unchanged — **never demoted** |
| `depth` + any | `hole_depth` | — | **no** | ambiguous; a bare depth is not an MD |

Three invariants, each pinned by a test:

1.  **Refine, never demote.** Recovery is keyed to the predicates a fix actually split, so a name
    that was never collapsed is never a candidate. An early version matched *any* label in the
    excerpt and "recovered" a `mud_balance` calibration date stored beside `"2025-05-30"` into the
    generic `date_iso`, because a date pattern matched the bare span — trading a correct specific
    name for a worse one. That is the failure this rule exists to prevent.
2.  **The value locates the reading; the label decides the predicate.** In
    `"SIDPP 420 psi and SICP 610 psi"` the value selects which occurrence the stored row came from.
    It is never used to choose a quantity — no rule anywhere maps `300` to "rheometer".
3.  **Ambiguity is a result, not a failure.** A row that cannot be settled is reported with its
    reason and left alone. Nothing is silently skipped and nothing is silently guessed.

On a corpus ingested by the current extractor the repair is a **no-op** — zero deterministic rows —
which is itself asserted, because a non-zero count would mean either the matrix is too eager or the
extractor has started emitting collapsed names again.

## 17. The current vocabulary

34 predicates, 91 registered field aliases (verified: `len(PREDICATES)`, `len(PREDICATE_BY_FIELD)`). The separations introduced by V4.2, plus the V4.3 cross-source unification of `total_mud_volume` into `mud_volume`:

| Quantity group | Predicates |
|---|---|
| surface pressure | `sidpp`, `sicp`, `maasp`, `surface_pressure` (+ pre-existing `standpipe_pressure`) |
| volume | `mud_volume`, `pill_volume`, `kick_volume`, `trip_tank_volume` |
| rotational speed | `rpm`, `rheometer_speed` (+ pre-existing `rheometer_reading`, a dial reading) |
| depth | `measured_depth`, `true_vertical_depth`, `hole_depth` |
| diameter | `bit_size`, `hole_section_size`, `casing_size` |

Legitimate aliasing still merges: `mud_weight`/`mw`/`mw_out`/`MW (ppg)` → `mud_weight`;
`hole_size`/`hole_size_in`/`Hole Size, in` → `hole_section_size`; `depth_md_ft` → `measured_depth`.
And a suffix that is itself a predicate still does not: `mw_in` → `mud_weight_in`.

---

## 18. Repairing a semantic collapse

When two quantities are found sharing a predicate, fix the **earliest responsible layer**:

1. Is the extractor emitting one field name for both? → split the rule, most specific first, unit
   required, context gated.
2. Is the vocabulary mapping two distinct field names onto one predicate? → separate the
   `PredicateSpec`s, and check `UNIT_SUFFIX_TOKENS` for a role masquerading as a unit.
3. Only then consider whether the conflict report needs changing. **It usually does not** — if
   identity is right, the report is right.

What is never an acceptable repair: renaming the conflicting rows, deleting them, weakening
detection, choosing a winner by counting, making `doctor` green, or inferring the quantity from the
numeric value.
