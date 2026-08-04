# QueryFlow command reference

Use this file when the task needs exact flags, output artifacts, or a decision
about whether a command can contact or mutate Google Cloud.

## Contents

- [Conventions](#conventions)
- [Install and configure](#install-and-configure)
- [Discover resources](#discover-resources)
- [Create and validate tasks](#create-and-validate-tasks)
- [Review, sample, and publish](#review-sample-and-publish)
- [Diagnostics and profiling](#diagnostics-and-profiling)

## Conventions

`TASK` is the absolute task directory returned by `start`. `RESOURCE` is the
canonical catalog name, not a display name. `DIGEST` is a complete hexadecimal
digest; never abbreviate it when passing it to `sample` or `publish`. Add
`--json` when another tool or the agent must parse the result.

| Command family | Reads local state | Contacts GCP | Can mutate a remote resource |
| --- | ---: | ---: | ---: |
| `version`, `config`, `policy` | yes | no | no |
| `doctor` | yes | no | no |
| `catalog refresh` | yes | yes | no |
| `catalog search`, `catalog show` | yes | no | no |
| `start` | yes | only with `--resource` | no |
| `validate` | yes | with `--backend workbench` or non-static local dry-run | no |
| `review` | yes | no | no |
| `sample` | yes | yes, Workbench only | no |
| `publish` | yes | yes | yes, only after digest approval |
| `profile --execute` | yes | yes | no, read-only profiling only |

## Install and configure

Install the CLI and plugin from the same Git reference, then restart Codex:

```bash
uvx --from git+https://github.com/3458Robyt/QueryFlow.git@v0.1.0 queryflow install
queryflow init --profile pilot \
  --source-projects source-project \
  --destination-projects destination-project \
  --workbench-project workbench-project \
  --workbench-location us-east1-b \
  --workbench-instance workbench-instance \
  --workbench-job-project workbench-project
```

`queryflow init` writes a credential-free TOML profile. Keep gcloud and GitHub
credentials in their native stores. Use `queryflow init --profile team` only
when the organization has approved the team policy.

Use these commands to inspect local settings:

```bash
queryflow config path --json
queryflow config validate --json
queryflow config list --json
queryflow config get profiles.pilot.destination_projects --json
queryflow config set preferences.review_mode unified --json
queryflow policy show --config ~/.config/queryflow/config.toml --json
queryflow policy check --config ~/.config/queryflow/config.toml \
  --operation publish --resource-kind shared_query --mode copy \
  --source-project source-project --destination-project destination-project \
  --location us --json
```

`queryflow config set` rejects credential-like keys and values. If a profile is
invalid, stop before catalog or task operations.

## Discover resources

Refresh only when the local catalog is stale or incomplete:

```bash
queryflow catalog refresh --account analyst@example.com --json
queryflow catalog search "sales" --kind notebook
queryflow catalog show CANONICAL_RESOURCE
```

Always copy the exact `name` returned by `search` or `show` into the next
command. A display name is not a safe resource identifier.

## Create and validate tasks

Existing notebook or Shared Query:

```bash
queryflow start --resource CANONICAL_RESOURCE \
  --account analyst@example.com \
  --destination-project destination-project \
  --config ~/.config/queryflow/config.toml --json
```

New query or notebook:

```bash
queryflow start --mode new --kind shared_query \
  --name monthly_sales --project destination-project --location us \
  --content-file query.sql --task-id monthly-sales-001 --json
```

`start` creates the isolated workspace and manifest. For an existing notebook,
edit the generated cell workspace or the notebook content through the normal
task workflow; do not edit the remote Dataform repository.

Validate locally without network only to inspect extraction and static safety:

```bash
queryflow validate --task TASK --backend local --static-only --json
```

For a publishable result, use the configured Workbench backend:

```bash
queryflow validate --task TASK \
  --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

Use `--max-bytes N` only to lower the configured ceiling. Use
`--execute-read-only --confirm-execution` only when the analyst explicitly
requested a bounded read-only check and the selected backend allows it; the
pilot Workbench backend remains dry-run only.

## Review, sample, and publish

Generate a static review or keep the Web Preview live while editing:

```bash
queryflow review --task TASK --json
queryflow review --task TASK --serve --watch
```

The preview is read-only. Confirm file/cell counts, green additions, red
deletions, validation state, and the publication digest before asking for
approval.

For a sample, use the execution digest produced from the current SQL and limit:

```bash
queryflow sample --task TASK --limit 3 \
  --approved-digest SAMPLE_DIGEST \
  --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

The command requires a publishable validation, verifies the content hash and
execution digest, executes inside Workbench, returns at most five rows, and
writes only row-free receipt metadata. A multi-query notebook requires
`--fragment CELL_INDEX`.

After the analyst approves the publication digest exactly:

```bash
queryflow publish --task TASK --approved-digest DIGEST \
  --destination-project destination-project \
  --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

Publishing performs the configured audit and remote read-back. The pilot
creates a new copy; it does not update or delete an existing resource.

## Diagnostics and profiling

```bash
queryflow doctor --config ~/.config/queryflow/config.toml --json
queryflow profile --table source-project.dataset.table \
  --schema-file schema.json --location us --json
```

`doctor` checks local executables and the selected profile. `profile` produces
aggregate schema statistics; it does not replace validation and does not grant
permission to run mutating SQL. Run `queryflow self-update --dry-run --json`
before changing the installed reference.
