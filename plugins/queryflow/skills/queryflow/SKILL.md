---
name: queryflow
description: Use when an analyst asks to create, edit, validate, review, sample, or publish SQL or notebooks in Google Cloud with QueryFlow, especially when a canonical resource, Workbench dry-run, Cloud Shell diff, digest approval, or policy decision is involved.
---

# QueryFlow

Use QueryFlow as the source of truth. Keep assets untouched until exact digest
approval.

## Non-negotiable contract

- Search the canonical resource before editing an existing asset.
- Create a new isolated task before editing any content.
- Edit only the task workspace; never edit a remote notebook, Shared Query, or
  Dataform repository directly.
- Validate before asking for publication approval. `--static-only` is local
  prevalidation and never creates a publishable digest.
- Treat unknown, dynamic, multi-statement, and mutating SQL as blocked.
- Use Workbench as the boundary for real dry-runs and samples. Never move row
  values to Cloud Shell, task files, audit archives, Git, or chat outside the
  approved bounded-sample interaction.
- In the pilot, create copies only. Do not update, delete, enable schedules, or
  bypass a policy failure.
- Never publish without the exact digest supplied by the analyst.

## Required lifecycle

1. Discover: run `queryflow catalog search "text" --kind notebook` (or the
   matching kind) and use the canonical resource name returned by the catalog.
2. Start: run `queryflow start --resource RESOURCE --account ACCOUNT
   --destination-project DESTINATION` for an existing asset, or use
   `--mode new --kind ... --content-file ...` for a new local asset.
3. Edit: modify only the task file or notebook cells. Preserve the task path.
4. Validate: run `queryflow validate --task TASK --config CONFIG --json` and
   report static classification, backend, dry-run status, bytes, warnings,
   references, and the publication digest.
5. Review: run `queryflow review --task TASK --serve`; direct the analyst to
   the Cloud Shell Web Preview. Confirm the red/green diff and changed cells.
6. Sample (optional): show the separate execution digest, obtain explicit
   approval, then run `queryflow sample --task TASK --limit 3
   --approved-digest SAMPLE_DIGEST --account ACCOUNT --config CONFIG --json`.
   Use `--fragment` when a notebook contains multiple SQL fragments. Never
   exceed five rows.
7. Publish: show the current publication digest and wait for exact approval.
   Then run `queryflow publish --task TASK --approved-digest DIGEST
   --destination-project DESTINATION --account ACCOUNT --config CONFIG` and
   report read-back/audit.

## Stop and report

Stop before any write when the catalog is stale, policy denies the operation,
SQL is not provably read-only, Workbench configuration is incomplete, the
digest is stale/mismatched, remote head changed, or validation returns VPC,
permission, authentication, transport, or SQL errors. Explain the rule that
blocked the action and the next safe diagnostic; do not suggest a bypass.

When handing work back, include task path, resource, changed files/cells,
validation backend/status, bytes/warnings, review URL, sample metadata,
publication digest, and any blocker.

## Load details only when needed

- Command flags and artifact meanings: [references/commands.md](references/commands.md)
- New, existing, notebook, sample, and publish paths: [references/workflows.md](references/workflows.md)
- VPC, IAM, authentication, Workbench, SQL, digest, and plugin failures:
  [references/troubleshooting.md](references/troubleshooting.md)
- Trust boundaries, profiles, approvals, and data retention:
  [references/security.md](references/security.md)
