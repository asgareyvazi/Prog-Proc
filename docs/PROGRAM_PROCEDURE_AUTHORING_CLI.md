# Program and Procedure Authoring CLI (first headless slice)

> **Qualification notice:** these commands are present in the current mission worktree and are not yet qualified on a pushed final SHA. They provide a structured authoring path, not a complete engineering-design suite. Examples below are synthetic syntax examples only; they are not drilling instructions or operational defaults.

The current CLI persists `DRAFT` Programs, Program sections/targets, `DRAFT` Procedures and structured Procedure steps. Content files are strict UTF-8 JSON objects (maximum 1 MiB); duplicate keys, unknown keys and invalid root types are refused. Every edit requires the latest exact `updated_at` token from `show`. Use the latest token returned after each mutation. These are optimistic-concurrency guards, not multi-user authentication.

## Program authoring

Create a draft from an operator-entered JSON file. The `well_id` must resolve to a registered well; project/field/well scopes are checked against the hierarchy.

```json
{
  "title": "<operator-entered program title>",
  "code": "<controlled program identifier>",
  "author": "<declared author attribution>",
  "well_id": "<registered well id>",
  "summary": "<source-backed purpose/summary>"
}
```

```sh
drillintel programs create --content program.json --workspace /path/to/workspace --json
drillintel programs show <PROGRAM_ID> --workspace /path/to/workspace --json
```

Use `programs update` or `procedures update` to change only descriptive top-level DRAFT fields (not identity or scope); the command also requires the exact latest token. Add a section and target. Section keys are stable identifiers; values and source references are user-entered. Numeric depth, mud-weight and cost values require an explicit corresponding unit/currency. No unit conversion is performed.

```json
{"section_key":"design_basis","title":"Design basis","content":"<recorded text and references>"}
```

```json
{"name":"<source-named section>","planned_depth_md_value":null}
```

For a target with a depth value, replace `null` with the explicit source-backed JSON number and add `"planned_depth_md_unit":"<explicit unit>"`. The command refuses a non-null value without its explicit unit.

```sh
drillintel programs section-add <PROGRAM_ID> --content section.json \
  --expected-updated-at <LATEST_UPDATED_AT> --workspace /path/to/workspace --json
drillintel programs target-add <PROGRAM_ID> --content target.json \
  --expected-updated-at <LATEST_UPDATED_AT> --workspace /path/to/workspace --json
drillintel programs validate <PROGRAM_ID> --workspace /path/to/workspace --json
```

`update`, `section-update`, `target-update`, `sections-reorder` and `targets-reorder` update only the current DRAFT. Reorder JSON is an array that must include every child ID exactly once.

## Procedure authoring

A Procedure draft contains scope and a purpose/description, then individually addressable ordered steps. A step has instruction text, responsible-role attribution, acceptance criteria, and optional equipment, preconditions, explicit parameters/units, evidence, hold point, safety controls, contingency and source reference.

```json
{
  "title": "<operator-entered procedure title>",
  "code": "<controlled procedure identifier>",
  "procedure_type": "<configured procedure category>",
  "description": "<purpose and scope entered from approved sources>",
  "created_by": "<declared author attribution>",
  "well_id": "<registered well id>"
}
```

```json
{
  "title": "<step title>",
  "instructions": "<reviewable source-backed instruction text>",
  "responsible_role": "<role>",
  "acceptance_criteria": "<explicit criteria from the controlled source>",
  "hold_point": false,
  "source_reference": "<exact source reference>"
}
```

```sh
drillintel procedures create --content procedure.json --workspace /path/to/workspace --json
drillintel procedures step-add <PROCEDURE_ID> --content step.json \
  --expected-updated-at <LATEST_UPDATED_AT> --workspace /path/to/workspace --json
drillintel procedures step-update <PROCEDURE_ID> <STEP_ID> --content step-change.json \
  --expected-updated-at <LATEST_UPDATED_AT> --workspace /path/to/workspace --json
drillintel procedures steps-reorder <PROCEDURE_ID> --order step-order.json \
  --expected-updated-at <LATEST_UPDATED_AT> --workspace /path/to/workspace --json
drillintel procedures validate <PROCEDURE_ID> --workspace /path/to/workspace --json
```

The validation result is structural completeness only. It is not an engineering safety assessment, calculation, approval, or assurance claim.

## Review, decision, revision, comparison

A Program needs a structured section and planned target before the current structural checklist allows submission. A Procedure needs purpose/scope and at least one step with instructions, responsible role and acceptance criteria. Submission, review and decision operate on the exact current revision. A declared author cannot submit as the reviewer; approval requires the recorded distinct reviewer attribution and a reasoned note. A rejection returns the exact revision to DRAFT and clears its active reviewer attribution. Revisions create a new DRAFT and copy children; approval is not inherited.

```sh
drillintel programs submit <ID> --expected-updated-at <LATEST> --by <AUTHOR> --workspace <WORKSPACE> --json
drillintel programs review <ID> --expected-updated-at <LATEST> --by <REVIEWER> --note "<review note>" --workspace <WORKSPACE> --json
drillintel programs approve <ID> --expected-updated-at <LATEST> --by <REVIEWER> --note "<decision reason>" --workspace <WORKSPACE> --json
drillintel programs revise <ID> --expected-updated-at <LATEST> --by <AUTHOR> --workspace <WORKSPACE> --json
drillintel programs compare <OLD_ID> <NEW_ID> --workspace <WORKSPACE> --json
```

Use the analogous `procedures` command group for Procedure revisions. `show` includes append-only technical-review history. Actor strings are not authenticated identities; the current local-first product has no authentication or digital-signature claim. Review attribution cannot qualify an engineering method or certify a design.

## Not yet provided

No writable Qt Program/Procedure editor, templates, section/step removal policy, controlled DOCX/PDF publishing, source-document-to-draft mapper, typed exact-revision Program-to-Procedure implementation link, full conflict impact analysis, or operational applicability view is included in this first headless slice. The database has no arbitrary calculation engine; only `npt.hours_rollup` V1 executes. See [`ENGINEERING_PROGRAM_PROCEDURE_MASTER_PLAN.md`](ENGINEERING_PROGRAM_PROCEDURE_MASTER_PLAN.md) for capability status, required evidence and release sequence.
