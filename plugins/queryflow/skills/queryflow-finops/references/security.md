# FinOps security boundary

- The scope is an explicit project allowlist.  QueryFlow does not enumerate
  the organization to discover additional projects.
- Cloud Asset Inventory and Recommender calls use reviewed asset/recommender
  allowlists and the configured gcloud identity.
- BigQuery aggregate queries are fixed templates executed in the approved
  Workbench/Jupyter boundary with dry-run, maximum-bytes, and bounded output
  checks.  The CLI does not run arbitrary SQL for an assessment.
- Local artifacts retain aggregate metrics, resource IDs needed for follow-up,
  resolved business context, source status, and provenance.  They do not
  retain raw Billing rows, raw provider responses, query text, emails, or
  the complete business-context map.
- Provider recommendation impacts remain per finding.  QueryFlow does not
  claim a total saving unless a source supplies a directly attributable value.
- All findings are `plan_only`; a person must validate ownership, criticality,
  accounting treatment, and operational risk before using any later change
  workflow.
- If IAM, VPC Service Controls, Workbench, Billing Export, or region coverage
  blocks a source, preserve the status and limitation and escalate to the
  administrator.  Do not bypass the perimeter or substitute invented data.
