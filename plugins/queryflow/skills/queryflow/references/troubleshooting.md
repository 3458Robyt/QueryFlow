# QueryFlow troubleshooting

Use this file when a command stops. Preserve the task, capture the diagnostic,
and fix the boundary that caused it; do not bypass policy or edit the remote
asset manually.

## Contents

- [Fast triage](#fast-triage)
- [Access and network](#access-and-network)
- [SQL and task state](#sql-and-task-state)
- [Sample and review](#sample-and-review)
- [Installation](#installation)

## Fast triage

Run these read-only checks first:

```bash
queryflow doctor --config ~/.config/queryflow/config.toml --json
gcloud auth list
queryflow config validate --json
queryflow policy show --config ~/.config/queryflow/config.toml --json
```

Then classify the failure from the command's `error_kind`, `status`, or exact
message. Keep the task directory so the analyst can inspect the diff and retry
after access is corrected.

## Access and network

| Symptom | Meaning | Safe action |
| --- | --- | --- |
| `blocked_vpc` or `VPC Service Controls` | The request crossed an organization perimeter or used the wrong execution boundary. | Capture project, location, Workbench instance, account, and request time for the GCP owner. Retry from the approved Workbench perimeter. |
| `blocked_permission`, `PERMISSION_DENIED`, or `Access Denied` | The active identity lacks an IAM/Dataform/BigQuery/Workbench permission. | Confirm `gcloud auth list`, the configured account, source/destination access, and Workbench service permissions. Ask the owner to grant the minimum role. |
| `authentication` or token failure | The requested account cannot produce a usable gcloud token. | Reauthenticate with the approved account and pass `--account` explicitly. Do not paste a token into a config, task, issue, or chat. |
| timeout, `ServerNotFoundError`, or transport error | Cloud Shell cannot reach the service or Workbench proxy. | Run `doctor`, confirm instance/location/job project, and retry later. Do not switch to an unapproved execution path to avoid the error. |
| Workbench fields missing | The modern profile cannot construct its in-perimeter runner. | Set `workbench_project`, `workbench_location`, `workbench_instance`, and `workbench_job_project` with `queryflow init` or a reviewed TOML profile. |
| Dataform `NOT_FOUND` for a query/notebook | The resource identifier is stale, display-name based, or from the wrong project/location. | Refresh the catalog and start again from the exact canonical `name`; do not invent a repository ID. |

## SQL and task state

| Symptom | Meaning | Safe action |
| --- | --- | --- |
| `mutating`, `unknown`, or dynamic SQL | QueryFlow cannot prove that the content is read-only. | Rewrite as a single literal read query or stop for analyst review. Never force a sample or execution. |
| multiple statements | One task contains more than one executable statement. | Split the work into separately reviewed tasks or select one notebook fragment explicitly for a sample. |
| `prechecked` with no digest | Validation used `--static-only`; it is not publishable. | Run the configured real backend validation after access is ready. |
| `changes_required`, `failed`, or SQL syntax error | Static analysis or backend validation rejected the current content. | Fix the task content, rerun `validate`, reopen `review`, and discard old approval digests. |
| stale content/digest | The task changed after validation or sample approval. | Do not reuse the digest. Validate again and request a new approval. |
| remote head changed | Someone changed the source after the task began. | Stop, refresh the catalog, create a new task, and reapply the reviewed change. |
| destination not allowed | The active profile allowlist does not include the requested project/location. | Use an approved destination or ask the policy owner to change the profile; do not pass a different project just to proceed. |

## Sample and review

| Symptom | Meaning | Safe action |
| --- | --- | --- |
| sample digest mismatch | SQL, fragment, or limit differs from what was approved. | Re-run validation, show the new sample digest, and ask for explicit approval. |
| notebook sample asks for `--fragment` | More than one SQL fragment was extracted. | Select the exact cell index; never sample all fragments implicitly. |
| sample backend rejected | The sample boundary is not Workbench or required settings are missing. | Configure the Workbench profile. Do not use Cloud Shell to retrieve rows. |
| review URL does not appear | The server was not started or the selected port is unavailable. | Run `queryflow review --task TASK --serve --port 8080` and open the printed Cloud Shell Web Preview URL. |
| preview shows stale state | The file changed after the snapshot. | Refresh/re-run `review`; if validation changed, request a new digest. |
| rows appear in a durable artifact | This violates the retention boundary. | Stop sharing the artifact, remove it through the approved incident process, and keep only the row-free receipt metadata. |

## Installation

Preview installer commands before running them:

```bash
queryflow install --ref v0.1.0 --dry-run --json
```

If the CLI works but Codex does not mention QueryFlow, confirm that the
marketplace/plugin installation completed and start a new Codex session. If
GitHub returns a permission error, ask the repository administrator for plugin
read access; never put a personal access token in a skill, README, or task.
