# QueryFlow Codex plugin

This plugin supplies the repeatable agent workflow. Install the Python CLI separately with the repository installer:

```text
uvx --from git+https://github.com/3458Robyt/QueryFlow.git@v0.3.0-beta.1 queryflow install
```

Start a new Codex session after installation so the skill is loaded.

The skill is the agent-facing source of truth. It links to focused references
for commands, workflows, troubleshooting, and security. Human-facing manuals
live in the repository under [`docs/`](../../docs/): [user guide](../../docs/USER_GUIDE.md),
[command reference](../../docs/COMMAND_REFERENCE.md), and
[troubleshooting](../../docs/TROUBLESHOOTING.md).

The `queryflow-finops` skill adds the governed, read-only FinOps/cloud-health
assessment workflow for business, finance, and platform users. It complements
the original SQL/notebook skill; it does not replace or mutate that workflow.
