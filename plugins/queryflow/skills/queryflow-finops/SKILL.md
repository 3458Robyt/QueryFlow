---
name: queryflow-finops
description: Use when a business, finance, platform, or analyst user asks for a governed FinOps or cloud-health assessment over authorized Google Cloud projects, cost trends, ownership gaps, recommendations, or a read-only improvement plan.
---

# QueryFlow FinOps

Use this skill for business-facing insight without weakening QueryFlow's
existing SQL/notebook controls.  An assessment is a deterministic snapshot,
not an autonomous cloud operator.

## Non-negotiable contract

- Confirm the configured project allowlist with `queryflow config list --json`.
- `--projects` may reduce the configured scope, never expand it; stop on an
  out-of-scope project or an empty allowlist.
- Run only `queryflow finops assess`; it is on demand and read-only.  Do not
  enable schedules, change resources, execute arbitrary SQL, or deploy a
  service from this skill.
- Treat Cloud Asset Inventory and Recommender as evidence sources, not as
  proof of savings.  Never sum provider recommendation amounts into a made-up
  total.
- BigQuery jobs, storage, and optional Billing Export aggregates run only in
  the configured Workbench boundary.  If Workbench or Billing Export is
  unavailable, report the source status and limitation instead of assuming
  zero.
- Use the optional business-context TOML map for business unit, owner, cost
  center, and criticality.  The artifact stores only resolved fields and a
  fingerprint, not the raw map or raw provider payloads.
- The assessment's action mode is always `plan_only`.  Present the evidence,
  confidence, assumptions, limitations, and proposed next steps for a human
  approval process.
- Verify the assessment digest before using `show` or opening `review`.

## Workflow

1. Confirm identity, profile, allowlist, Workbench configuration, and the
   requested reporting window.
2. Run an assessment, optionally narrowing projects or adding a Billing
   Export table:

   ```bash
   queryflow finops assess --config ~/.config/queryflow/config.toml \
     --projects allowed-project --window-days 30 --json
   ```

3. Summarize the executive report first: scope, source coverage, verified
   signals, context coverage, and opportunities.  State explicitly when a
   source is partial, unavailable, or unconfigured.
4. Use `queryflow finops show --assessment ASSESSMENT --json` for a verified
   machine-readable report, or `queryflow finops review --assessment
   ASSESSMENT --serve` for the local read-only Web Preview.
5. Keep the technical appendix with source provenance and finding IDs so the
   platform team can reproduce each proposed plan.  Do not turn a finding into
   a cloud write without a separate approved workflow.

See [references/commands.md](references/commands.md) for flags and artifact
semantics, and [references/security.md](references/security.md) for the data
boundary and escalation rules.
