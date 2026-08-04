# QueryFlow

QueryFlow is a small Python CLI and Codex plugin for making SQL and notebook changes reviewable in Google Cloud. It creates an isolated task, validates SQL with a dry-run, shows a PR-style web diff, supports an explicitly approved sample of at most five rows inside Workbench, and publishes only after an exact digest is approved.

## Install

The repository is public and the first release is distributed from GitHub:

```bash
uvx --from git+https://github.com/3458Robyt/QueryFlow.git@v0.1.0 queryflow install
queryflow init
```

Restart Codex after the installer registers the plugin. The installer never stores gcloud or GitHub credentials; it uses the authenticated local tools.

## Daily workflow

```bash
queryflow catalog refresh --account ACCOUNT --json
queryflow catalog search "sales" --kind notebook
queryflow start --resource RESOURCE --account ACCOUNT --destination-project DESTINATION --json
queryflow validate --task TASK --account ACCOUNT --config ~/.config/queryflow/config.toml --json
queryflow review --task TASK --serve
```

After the analyst reviews the digest, publishing remains an explicit action:

```bash
queryflow publish --task TASK --approved-digest DIGEST --destination-project DESTINATION --account ACCOUNT
```

To inspect values, approve the execution digest returned by the agent and run:

```bash
queryflow sample --task TASK --limit 3 --approved-digest SAMPLE_DIGEST --account ACCOUNT --json
```

QueryFlow stores configuration under `~/.config/queryflow/config.toml`, keeps credentials in gcloud, and never writes sample rows to a task or audit record. The migration dictionary and route-rewrite scripts are deliberately outside this project.

## Development

```bash
uv run --with 'sqlglot>=25,<30' --with 'websockets>=15,<16' python -m unittest test_queryflow.py tests
uv build
```

The package currently targets Python 3.11 and 3.12. GCP integration tests must remain explicit and read-only; no mutating SQL is run automatically.
