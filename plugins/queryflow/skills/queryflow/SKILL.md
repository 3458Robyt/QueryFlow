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
- In the normal `pilot`, create copies only. Do not update, delete, enable
  schedules, or bypass a policy failure.
- The separate `migration-pilot` profile is a bounded batch exception: it
  accepts only a saved campaign manifest with 10 Shared Queries and 10
  notebooks, rewrites routes locally from a private dictionary, never runs
  SQL/table checks, and creates new copies only after the analyst explicitly
  runs `queryflow pilot run --execute-migration`. It never updates source
  assets. Cleanup is a separate digest-gated command.
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

## Migration pilot (bounded exception)

Use the migration tools only for the approved source/destination pair and a
private dictionary that is kept outside Git:

```bash
queryflow migration dictionary validate --dictionary PRIVATE/routes.json --json
queryflow pilot inventory --dictionary PRIVATE/routes.json --catalog catalog.json \
  --source-project SOURCE_PROJECT --destination-project DESTINATION_PROJECT \
  --account ACCOUNT --dataform-requests-per-minute 180 --json
queryflow pilot review --manifest PRIVATE/manifest.json
queryflow pilot run --manifest PRIVATE/manifest.json --dictionary PRIVATE/routes.json
```

The inventory reads code for classification, selects a deterministic 5/3/2
sample per kind, and records only hashes, route mappings, and unknown-route
incidents. The Dataform project quota is 300 requests/minute in `us-east1`;
the client stays at 180, retries `429` reads with bounded backoff, and writes a
checkpoint that can be resumed with `--resume-inventory`. The run command above
is planning-only. To create local tasks and red/green Web Previews without SQL
or publication, use:

```bash
queryflow pilot prepare --manifest PRIVATE/manifest.json \
  --dictionary PRIVATE/routes.json --account ACCOUNT \
  --dataform-requests-per-minute 180 --json
```

Publication requires the explicit switch, profile, and approved campaign
digest:

```bash
queryflow permissions use migration-pilot
queryflow pilot run --manifest PRIVATE/manifest.json \
  --dictionary PRIVATE/routes.json --execute-migration \
  --approved-digest PUBLICATION_DIGEST --account ACCOUNT --json
```

Unknown routes remain unchanged and produce `published_with_incidents`; they
are never guessed. To remove only copies created by the campaign, prepare and
approve a separate cleanup digest:

```bash
queryflow pilot cleanup-plan --manifest PRIVATE/manifest.json --json
queryflow pilot cleanup --manifest PRIVATE/manifest.json \
  --approved-digest CLEANUP_DIGEST --account ACCOUNT --json
```

This exception does not change the normal `pilot`, `team`, or `full-access`
approval rules and does not execute SQL.

## Load details only when needed

- Command flags and artifact meanings: [references/commands.md](references/commands.md)
- New, existing, notebook, sample, and publish paths: [references/workflows.md](references/workflows.md)
- VPC, IAM, authentication, Workbench, SQL, digest, and plugin failures:
  [references/troubleshooting.md](references/troubleshooting.md)
- Trust boundaries, profiles, approvals, and data retention:
  [references/security.md](references/security.md)
