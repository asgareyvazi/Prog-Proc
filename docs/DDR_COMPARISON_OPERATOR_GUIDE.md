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

The display and export use loaded authored revision snapshots; they do not submit, revise, approve, or otherwise mutate DDR state. Exported provenance is retained as source data, including paths when present; this is distinct from sanitized diagnostic text.
