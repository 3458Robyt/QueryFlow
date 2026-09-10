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
- In normal `pilot`, `team`, `full-access`, and `migration-pilot` workflows,
  treat unknown, dynamic, multi-statement, and mutating SQL as blocked.
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
- For new route migrations use the official `migration-batch` profile and
  `queryflow migration batch`. It accepts an explicit selection of
  `shared_query`/`notebook` resources, preserves visible names, rewrites code
  locally from the private dictionary, and accepts only these review-required
  code-copy warnings: unknown routes, dynamic SQL, mutating SQL, and SQL that
  cannot be classified. With `operation=copy` it creates new repositories;
  with `operation=update` it updates the current destination repository in
  place after checking its head commit. Both operations require one global
  digest approval. It never runs SQL or a dry-run. Embedded secrets,
  empty/malformed assets, conflicts, drift, authentication, transport, audit,
  and read-back failures remain hard blocks. Resources with accepted warnings receive
  `queryflow_review=required` and `queryflow_state=pending`; those labels are
  metadata for human review, not an execution permission. `queryflow pilot` is
  a deprecated compatibility alias for the old 10+10 pilot.
- For stored procedures use `queryflow migration routines` with the explicit
  `migration-batch` profile and `allow_routine_migration=true`. This workflow
  inventories BigQuery `routines` through REST (or the configured Workbench
  gateway), rewrites table routes and calls to the destination `functions`
  dataset, and creates a copy-only manifest. It never submits a SQL job, runs
  `CALL`, or performs a dry-run. It includes GoogleSQL procedures and
  recursively referenced SQL/JavaScript routines; Spark procedures and other
  unsupported types are reported. Missing dependencies, conflicts, secrets,
  authentication, VPC, transport, source drift, and read-back failures stay
  blocked. Dynamic/mutating SQL and unknown routes may be copied only as
  explicitly labelled human-review warnings. Publication requires the exact
  routine digest; `--lot` uses that lot's digest. See
  [references/routines.md](references/routines.md).
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
the digest is stale/mismatched, remote head changed, or validation returns
VPC, permission, authentication, transport, SQL, audit, conflict, or
integrity errors. In normal workflows, also stop when SQL is not provably
read-only. The sole code-copy exception is `migration-batch`: its manifest may
contain the four review-required warning classes above, but never a hard
block. Explain the rule that blocked the action and the next safe diagnostic;
do not suggest a bypass.

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

## Explicit migration batch

Use this flow for a new, explicit list of resources. For a complete notebook
campaign, generate region-aware selections from a fresh catalog first; do not
hardcode a historical resource count in the agent instructions:

```bash
queryflow permissions use migration-batch
queryflow migration batch inventory --selection-file SELECTION.json \
  --dictionary PRIVATE/routes.json --catalog ~/.queryflow/catalog.json \
  --account ACCOUNT --json
queryflow migration batch prepare --manifest PRIVATE/manifest.json \
  --dictionary PRIVATE/routes.json --account ACCOUNT --json
queryflow migration batch review --manifest PRIVATE/manifest.json --serve
```

Present `publication_digest` and wait for a single explicit approval. Then run
`migration batch run` with `--execute-migration --approved-digest DIGEST`.
For an in-place correction, set `operation=update` in the selection, use the
same source and destination project, and enable `allow_update_existing=true`
in `team` or `full-access`; the command edits the existing repository and
never creates a second copy. `resume` retries only pending resources with the
same digest. Inspect
`migration-report.md` for every unmatched route (resource, file/cell and line),
static statement class, review reason, and hard blocker. Accepted warnings
are copied as code only and remain labelled pending human review. Do not
execute a BigQuery query or dry-run as part of this flow.

If an operator explicitly marks one record `pending` (for example after a
destination collision), pass `--skip-pending` to publish the other records
without overwriting it. The manifest keeps the pending record and the same
digest for a later decision.

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
- Stored procedure inventory, review, lots and publication:
  [references/routines.md](references/routines.md)
- New, existing, notebook, sample, and publish paths: [references/workflows.md](references/workflows.md)
- VPC, IAM, authentication, Workbench, SQL, digest, and plugin failures:
  [references/troubleshooting.md](references/troubleshooting.md)
- Trust boundaries, profiles, approvals, and data retention:
  [references/security.md](references/security.md)
