# QueryFlow security boundaries

Use this file when deciding whether an operation is permitted, what evidence
can be retained, or which environment should execute a query.

## Trust boundaries

1. **Catalog:** read-only inventory of canonical resource names and fingerprints.
2. **Task workspace:** isolated proposed content, manifest, validation, and
   row-free receipts.
3. **Cloud Shell:** edits, static checks, review server, and orchestration; it
   is not a row-execution boundary for the pilot.
4. **Workbench:** in-perimeter location for real BigQuery dry-runs and bounded
   samples.
5. **GCP publication:** the only write boundary, reached only by `publish`
   after exact digest approval, allowlist checks, audit, and remote read-back.

Do not collapse these boundaries to make a failing step pass.

## Immutable guardrails

- SQL must be a single, provably read-only statement. Unknown, dynamic,
  multi-statement, DML, DDL, transaction, export, load, and procedure content
  is blocked.
- The pilot creates new copies only. Deletion is never available. Existing
  updates require the separately approved `team` profile and concurrency check.
- Scheduled queries are disabled in the v1 policy. No schedule is enabled or
  executed automatically.
- A validation digest is tied to task content and validation evidence. A sample
  execution digest is separately tied to SQL, fragment index, and row limit.
- A user-supplied `--max-bytes` value can only reduce the profile ceiling.
- Sample limits default to three and cannot exceed five. The durable receipt
  contains count/columns/limit/digest/timestamp/truncation/errors, never row
  values.
- A remote read-back must match the approved content before publication is
  reported as successful.

## Profiles and allowlists

The `pilot` profile is the default restrictive mode. It allows notebook and
Shared Query workflows, read-only validation, new copies, configured source
projects, configured destination projects, and configured locations.

The `team` profile is not a bypass. It is a separately reviewed configuration
that may permit an existing-resource update only when `allow_update_existing`
is true and the task has a remote commit base. Keep profiles credential-free;
gcloud supplies identity and tokens.

## Evidence and retention

Keep manifests, validation state, digests, hashes, byte estimates, audit
receipts, and error classifications. Do not keep query result rows in task
files, validation JSON, audit archives, GitHub, the plugin, or durable chat
transcripts. Show a bounded sample only in the explicit analyst interaction.

Do not place production project IDs, table names, customer identifiers,
notebook contents, credentials, or tokens in public documentation or tests.

## Deliberately separate workflows

Dictionary-driven route rewriting and migration scripts are not QueryFlow
features. They must not be silently folded into a task, sample, validation, or
publication. Make route changes explicit in the reviewed SQL diff and use the
separate migration process when required.
