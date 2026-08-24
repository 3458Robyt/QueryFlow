# FinOps command reference

## Assess

```bash
queryflow finops assess \
  --config ~/.config/queryflow/config.toml \
  --projects PROJECT_ID [PROJECT_ID ...] \
  --window-days 30 \
  --billing-table BILLING_PROJECT.DATASET.TABLE \
  --business-context business-context.toml \
  --output-root ~/.queryflow/assessments \
  --json
```

Only `--projects` values already present in `finops_projects`,
`source_projects`, or `destination_projects` are accepted.  If omitted, the
configured FinOps list is used.  The assessment uses the profile's account,
gcloud configuration, Workbench settings, and optional Billing Export table.

## Show and review

```bash
queryflow finops show --assessment ASSESSMENT_ID --json
queryflow finops review --assessment ASSESSMENT_ID --serve --port 8080
```

`ASSESSMENT_ID` may be the generated ID under the default output root or an
absolute assessment directory.  Both commands verify `manifest.json`, every
artifact hash, and the overall assessment digest before returning data.

## Artifacts

Each assessment directory contains:

- `manifest.json`: scope, source status, action mode, artifact hashes, and
  `assessment_digest`.
- `evidence.json`: bounded resources, recommendations, aggregate metrics,
  provenance, and context coverage.
- `findings.json`: stable rule IDs, severity, evidence references, and the
  governed `plan_only` steps.
- `report.json`, `report.md`, and `report.html`: executive and technical
  readings of the same deterministic snapshot.

The report is read-only; the review server rejects non-GET write attempts.
