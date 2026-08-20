# QueryFlow workflows

Use this file to choose the correct lifecycle for a request. All workflows
share the same sequence: discover (when editing existing content), isolate,
edit, validate, review, optionally sample, then publish only after approval.

## Contents

- [Existing asset](#existing-asset)
- [New query](#new-query)
- [Notebook](#notebook)
- [Optional sample](#optional-sample)
- [Publication](#publication)
- [Agent handoff](#agent-handoff)

## Existing asset

Use this path for a notebook or Shared Query already in Google Cloud:

1. Refresh/search the catalog if the local catalog is missing or stale.
2. Capture the canonical `name`, kind, project, location, and fingerprint.
3. Run `start --resource` with the analyst account; use the saved context or
   pass the intended destination explicitly.
4. Edit only the task workspace.
5. Run Workbench validation and open the Web Preview.

Use `queryflow status --task TASK --json` to report the current state and
`queryflow diagnose --task TASK --format markdown` if validation is blocked.

Do not reconstruct a resource name from a display name, copy a remote file into
an unrelated directory, or edit the original while the task is open.

## New query

Use `start --mode new --kind shared_query` with a local `.sql` file for a new
query. The task starts with a local-input fingerprint, so validation still
checks SQL safety and the publication path still requires a digest. Keep the
query read-only; a `CREATE`, DML statement, transaction, dynamic SQL, or
multiple statements must be split or rejected before any sample.

Recommended sequence:

```text
start --mode new → edit content.sql → validate → review → approve digest → publish
```

## Notebook

For an existing notebook, `start` exports the canonical notebook into the task
and QueryFlow extracts literal SQL fragments. Each fragment receives a hash and
its own dry-run evidence. Python-built SQL, f-strings that change the query,
unresolved variables, and cells that cannot be classified safely remain
blocked.

Use `review --serve --watch` to keep one Web Preview open during edits. The
review groups cells, reports added/removed lines, and marks the validation and
sample steps without exposing row values.

If the notebook has more than one SQL fragment, select one explicitly for a
sample with `--fragment CELL_INDEX`; never silently choose the first query.

## Optional sample

A sample is a separate, explicit operation, not part of validation:

1. Validate the current task and copy the execution digest derived from the
   SQL, fragment index, and row limit.
2. Show the digest and intended limit to the analyst.
3. Run `sample` only after the analyst supplies that exact digest.
4. Report rows transiently and retain only receipt metadata: digest, count,
   columns, limit, timestamp, truncation, and errors.

The default limit is three and the hard maximum is five. The query is wrapped
with an outer `LIMIT`, and Workbench runs it with a bytes ceiling. If the SQL
changes, discard the old sample approval and generate a new digest.

## Publication

The pilot publication creates a new Dataform/Shared Query copy in the allowed
destination project. Before publishing, verify:

- validation is publishable and current;
- the exact publication digest matches the task;
- the destination project is on the profile allowlist;
- the remote resource head has not changed;
- the audit root is configured;
- the analyst has approved the digest in the current conversation.

Team update mode is a separate policy decision. It requires the team profile,
an existing canonical resource, a remote head check, and a fresh digest. Never
convert a pilot copy task into an update task.

In a reviewed `full-access` profile, the analyst may explicitly order a
notebook or Shared Query publication without validation/dry-run evidence:

```text
permissions use full-access → publish --force-publish --reason REASON
```

This route still requires the canonical resource, destination, remote head,
audit package, content hash, and read-back, and records
`force-authorization.json`. It never runs SQL, enables schedules, or deletes.

Scheduled queries, route rewriting, dictionary-based migration, deletion, and
automatic execution are outside this workflow. Explain that boundary instead
of improvising a parallel path.

If a reviewed team profile explicitly enables the static-exception path, it is
still a separate approval boundary: prove static syntax first, prepare an
exception with a reason and ticket, present its independent digest, then use
`publish --approved-exception-digest`. It only saves code and preserves remote
head, policy, audit, and read-back checks.

## Agent handoff

Return a compact handoff with these fields in this order:

1. **Estado:** draft, prechecked, ready, blocked, approved, or published.
2. **Tarea:** absolute task path and resource kind/display name.
3. **Cambios:** files/cells and added/removed counts.
4. **Validación:** backend, status, read-only classification, bytes, warnings,
   and references.
5. **Revisión:** Web Preview URL and whether the analyst inspected the diff.
6. **Muestra:** optional execution digest, limit, row count, and truncation; do
   not paste durable row data into the handoff.
7. **Publicación:** publication digest, destination, read-back/audit receipt,
   or the exact blocker and next safe action.
