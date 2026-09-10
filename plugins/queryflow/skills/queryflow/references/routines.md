# Stored procedure workflow

Use this reference when the analyst asks to migrate BigQuery procedures or
stored routine dependencies. The implementation is intentionally separate
from Dataform/Shared Query migration because BigQuery routines are resources
read and created through the BigQuery REST API.

## Safe sequence

1. Confirm the persistent gcloud identity and configured source/destination.
   Pin `~/.config/gcloud`; do not inherit a temporary `CLOUDSDK_CONFIG`.
2. Activate `migration-batch` and enable `allow_routine_migration` explicitly.
3. Validate the private route dictionary.
4. Run `migration routines inventory` (read-only). It reads dataset metadata,
   routine summaries and complete definitions, then records hashes, ACL
   evidence and inventory errors.
5. Run `migration routines prepare` to re-read the remote source and produce
   proposal files, a manifest and reports. The preparation is the source of
   the publication digest.
6. Serve `migration routines review --serve`. Inspect the red/green diff,
   route incidents, dependencies and status for every routine.
7. Obtain the exact global or lot digest. Run `migration routines run` with
   `--execute-migration`; never invent, abbreviate or reuse a stale digest.
8. Inspect `report.json`, `report.md`, `review.html` and `routine-audit.json`.

## Backends and Workbench

`--backend direct` uses BigQuery REST from Cloud Shell. `--backend workbench`
executes the same REST call inside one ephemeral Python kernel in the approved
Workbench instance. It does not use `bq query`, submit a query job or call a
procedure. `auto` performs a read-only direct probe and switches to Workbench
only for a VPC/perimeter failure. The Workbench setting must identify the
instance project, region/zone, instance name and job project; the resource
project and zone must not be guessed from the source project.

The client paces requests locally at 120/minute by default and never exceeds
the configured 300/minute ceiling. GET reads may retry bounded throttling;
POST inserts are not retried automatically, preventing accidental duplicates.

## Selection and dependencies

The campaign selects SQL `PROCEDURE` resources. A referenced SQL or
JavaScript function/table function is added as a dependency and ordered before
the caller. Fully qualified and dataset-qualified references are rewritten to
`` `DEST_PROJECT.functions.ROUTINE_ID` ``. Missing or unsupported dependencies,
ambiguous calls and dependency cycles are hard blocks. Spark procedures and
other unsupported routine types appear under `unsupported` and are not
silently copied.

The route dictionary is applied after routine-reference normalization. This
prevents a broad table mapping from changing a procedure call into an
unrelated dataset. Unknown table routes remain unchanged and include
`definitionBody` line coordinates in the review.

## Publication contract

The destination dataset must already exist. The client performs a destination
existence check and calls `routines.insert` only when the destination routine
does not exist. A different existing definition is `destination_conflict`;
there is no overwrite, rename, update or delete operation. A successful insert
is read back and compared across all semantic routine fields before it is
labelled `published_verified`.

`dynamic_sql`, `mutating_sql` and `unknown_route` are review warnings. They can
be copied as code, but QueryFlow never executes them. Empty definitions,
embedded secrets, missing dependencies, source drift, authentication/VPC,
transport, quota, audit and read-back failures remain blocked. Sealed copies
require both publication and security digests plus a ticket reference; their
definition is never placed in the preview or audit file.

## Artifacts

The campaign directory contains:

- `manifest.json`: immutable inventory, proposals, hashes, statuses, lots and
  digests.
- `proposals/*.sql`: proposed definition for non-secret routines.
- `review.html`: local dark Web Preview with red/green line diff.
- `report.json` / `report.md`: resource-level status, dependencies, routes and
  incidents.
- `routine-audit.json`: content-free approval/publication checkpoint with
  hashes and receipts; it records no SQL, rows, tokens or credentials.

`report` is local-only and regenerates artifacts without contacting GCP. A
failed run may be retried with `resume` and the same manifest/digest after the
underlying incident is resolved.
