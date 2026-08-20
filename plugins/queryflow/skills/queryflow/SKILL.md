---
name: queryflow
description: Use when an analyst asks to create, edit, validate, review, sample, or publish SQL or notebooks in Google Cloud with QueryFlow, especially when a canonical resource, Workbench dry-run, Cloud Shell diff, digest approval, or policy decision is involved.
---

# QueryFlow

Use QueryFlow as the source of truth. Keep assets untouched until exact digest
approval, except for the separately auditable `full-access --force-publish`
route below.

## Non-negotiable contract

- Search the canonical resource before editing an existing asset.
- Create a new isolated task before editing any content.
- Edit only the task workspace; never edit a remote notebook, Shared Query, or
  Dataform repository directly.
- Validate before asking for publication approval. `--static-only` is local
  prevalidation and never creates a publishable digest.
- Use `queryflow status --task TASK --json` after each meaningful transition;
  use `queryflow diagnose --task TASK --format markdown` for a safe support
  bundle when a step is blocked.
- Use `queryflow context show --json` to confirm the source/destination pair;
  every new task snapshots the resolved project IDs.
- Use `queryflow doctor --probe-remote --json` when an administrator needs a
  read-only check of enabled APIs and the Workbench instance; the default
  `doctor` remains local-only.
- Use the configured persistent gcloud directory (`~/.config/gcloud`). A
  temporary inherited `CLOUDSDK_CONFIG` does not replace it unless the profile
  explicitly sets another directory.
- Treat unknown, dynamic, multi-statement, and mutating SQL as blocked.
- Use Workbench as the boundary for real dry-runs and samples. Never move row
  values to Cloud Shell, task files, audit archives, Git, or chat outside the
  approved bounded-sample interaction.
- In the pilot, create copies only. Do not update, delete, enable schedules, or
  bypass a policy failure.
- In `pilot` and `team`, never publish without the exact digest supplied by the
  analyst.
- In `full-access`, an analyst may explicitly order a notebook or Shared Query
  publication with `--force-publish --reason REASON` when validation/dry-run is
  unavailable. This still requires the canonical resource, allowed
  destination, remote-head check, audit archive, content hash, and remote
  read-back. It never enables SQL execution, schedules, or deletes and writes
  `force-authorization.json`.

## Required lifecycle

1. Discover: run `queryflow catalog search "text" --kind notebook` (or the
   matching kind) and use the canonical resource name returned by the catalog.
2. Start: run `queryflow start --resource RESOURCE --account ACCOUNT` and let
   the saved context supply the destination (or pass
   `--destination-project DESTINATION`) for an existing asset, or use
   `--mode new --kind ... --content-file ...` for a new local asset.
3. Edit: modify only the task file or notebook cells. Preserve the task path.
4. Validate: run `queryflow validate --task TASK --config CONFIG --json` and
   report static classification, backend, dry-run status, bytes, warnings,
   references, and the publication digest.
5. Review: run `queryflow review --task TASK --serve --watch`; direct the
   analyst to the single dark Cloud Shell Web Preview. Confirm the red/green
   diff and changed cells without requiring copy/paste into BigQuery Studio.
6. Sample (optional): show the separate execution digest, obtain explicit
   approval, then run `queryflow sample --task TASK --limit 3
   --approved-digest SAMPLE_DIGEST --account ACCOUNT --config CONFIG --json`.
   Use `--fragment` when a notebook contains multiple SQL fragments. Never
   exceed five rows.
7. Publish: in normal profiles show the current publication digest and wait for
   exact approval, then run `queryflow publish --task TASK
   --approved-digest DIGEST --account ACCOUNT --config CONFIG` and report
   read-back/audit. In `full-access`, use `--force-publish --reason` only when
   the analyst explicitly orders publication without dry-run evidence.

## Controlled static exception

If real validation is blocked by an approved perimeter dependency, first prove
that static SQL validation succeeded. In the reviewed `team` profile only, run:

```bash
queryflow exception prepare --task TASK --reason REASON \
  --reference TICKET --config CONFIG --json
```

Present the independent exception digest and wait for explicit approval.
Publish with `--approved-exception-digest`, never with a normal digest. This
path only saves a code asset; it never executes SQL, enables schedules, or
overrides syntax, policy, conflict, not-found, or integrity blocks.

## Stop and report

Stop before any write when the catalog is stale, policy denies the operation,
SQL is not provably read-only, Workbench configuration is incomplete, the
digest is stale/mismatched, remote head changed, or validation returns VPC,
permission, authentication, transport, or SQL errors. Explain the rule that
blocked the action and the next safe diagnostic; do not suggest a bypass.

An explicit force publication is not a generic bypass: it is limited to the
full-access profile and still stops on a wrong resource, destination, remote
conflict, audit failure, or failed read-back.

When handing work back, include task path, resource, changed files/cells,
validation backend/status, bytes/warnings, review URL, sample metadata,
publication digest, and any blocker.

Codex owns the conversation end to end: it discovers the canonical resource,
starts/resumes the task, edits isolated content, opens the preview, explains
diagnostics, presents the digest or records the explicit force authorization,
publishes through QueryFlow, and reports read-back. QueryFlow remains
deterministic; the agent must never invent a digest or silently select force
mode.

## Load details only when needed

- Command flags and artifact meanings: [references/commands.md](references/commands.md)
- New, existing, notebook, sample, and publish paths: [references/workflows.md](references/workflows.md)
- VPC, IAM, authentication, Workbench, SQL, digest, and plugin failures:
  [references/troubleshooting.md](references/troubleshooting.md)
- Trust boundaries, profiles, approvals, and data retention:
  [references/security.md](references/security.md)
