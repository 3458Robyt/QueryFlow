# QueryFlow Codex plugin

This plugin supplies the repeatable agent workflow. Install the Python CLI separately with the repository installer:

```text
uvx --from git+https://github.com/3458Robyt/QueryFlow.git@v0.2.0-beta.1 queryflow install
```

Start a new Codex session after installation so the skill is loaded.

The skill is the agent-facing source of truth. It links to focused references
for commands, workflows, troubleshooting, and security. Human-facing manuals
live in the repository under [`docs/`](../../docs/): [user guide](../../docs/USER_GUIDE.md),
[command reference](../../docs/COMMAND_REFERENCE.md), and
[troubleshooting](../../docs/TROUBLESHOOTING.md).
