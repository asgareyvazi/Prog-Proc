# DDR Revision Comparison — Operator Guide

DDR comparison is a read-only view of two explicitly scoped revisions. It compares authored values and preserves uncertain activity alignment as **AMBIGUOUS**; it does not evaluate review, approve a report, authenticate an actor string, or change a DDR, audit record, or database row. Submission and independent review remain separate lifecycle actions.

## CLI

Name both the direct predecessor and its recorded successor:

```text
drillintel ddr compare <predecessor-report-id> <successor-report-id>
```

The command refuses cross-well pairs and pairs where the successor does not name the supplied predecessor. Its concise default output is unchanged. Use `--json` for the stable machine-oriented comparison response.

For an operator-facing export, select JSON or Markdown:

```text
drillintel ddr compare <predecessor-id> <successor-id> --format json

drillintel ddr compare <predecessor-id> <successor-id> --format markdown
```

To write the export atomically:

```text
drillintel ddr compare <predecessor-id> <successor-id> \
  --format json --output ./ddr-comparison.json
```

Existing files are not replaced by default. Choose another path, or explicitly request replacement with `--overwrite`. A failed/invalid destination does not silently fall back to stdout or alter the DDR workspace. `--overwrite` requires `--output`.

Exit codes: `0` = conclusive comparison; `3` = comparison completed but is ambiguous or malformed; `1` = operational/scope failure; `2` = command-line usage error. Ambiguous outputs retain candidate groups and reasons; they are not silently paired. JSON is deterministic and includes exact report IDs, revision numbers, predecessor link, declared provenance, authored fields and activity text, status, limitations, and explicit truncation counts. The current service accepts at most 200 activity rows per revision; the export includes all comparison items and records zero omitted rows.

## Desktop workbench

Select a DDR revision with its recorded predecessor in the loaded history, then choose **Compare with prior revision**. The comparison panel is bounded for display and states the exact number of comparison items not expanded. Choose **Export comparison** to save the complete JSON or Markdown comparison, including every activity item, source revision metadata, declared provenance, and ambiguity limitations. If a chosen file already exists, the workbench asks before replacing it. Export errors are reported in the workbench status area.

The display and export use loaded authored revision snapshots; they do not submit, revise, approve, or otherwise mutate DDR state. The default export is **full fidelity** and retains declared provenance, including local source paths when present.

### Full-fidelity and shareable exports

A shareable copy is an explicit opt-in; the default is never silently redacted. In the CLI, add `--redact-source-paths` with `--format json|markdown` or `--output PATH`. In the desktop export flow, choose either **Full fidelity — include recorded local source paths** or **Shareable copy — redact local source paths** after choosing the destination. The JSON shareable artifact records `redaction.applied: true`, policy `prog-proc.ddr-local-source-paths/v1`, and a count of redacted fields; Markdown states that the path redaction was applied. Comparison outcomes, ambiguity, truncation counts, authored DDR text, actor/reason declarations, IDs, and other non-path provenance are retained. The operation edits only the export copy and does not alter DDR, audit, or provenance records.

The v1 path-key allowlist is exact: `path`, `source_path`, `source_relative_path`, `file_path`, `source_uri`, and `file_uri`, including the same keys nested in provenance mappings/lists. Values under those keys are replaced with `[REDACTED LOCAL SOURCE PATH]`; exact duplicate references elsewhere in provenance strings and comparison diagnostics are also scrubbed. The policy covers POSIX absolute and relative paths, Windows drive paths, UNC paths, and `file://` URIs (including percent-encoded URI paths). Keys that merely contain the word `path` are not selected. Do not use redaction as a substitute for reviewing the final artifact before sharing it.
