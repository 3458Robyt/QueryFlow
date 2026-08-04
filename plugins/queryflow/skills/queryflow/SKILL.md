---
name: queryflow
description: Use QueryFlow for SQL and notebook changes that must be catalogued, reviewed, validated in Workbench, sampled explicitly, and published with an approved digest.
---

# QueryFlow

Use the installed `queryflow` command as the source of truth for SQL work in Google Cloud.

## Required workflow

1. Search the canonical resource with `queryflow catalog search` before editing an existing notebook or shared query.
2. Create an isolated task with `queryflow start`.
3. Edit only the task content; never edit the remote asset directly.
4. Run `queryflow validate --task ...` and inspect the returned policy, dry-run and digest.
5. If values need checking, ask the analyst to approve the execution digest and run `queryflow sample --task ... --limit 3 --approved-digest ...`.
6. Open `queryflow review --task ... --serve` and review the red/green diff.
7. Publish only after the analyst gives the exact publication digest: `queryflow publish --approved-digest ...`.

## Safety rules

- Read-only SQL only. Treat unknown, dynamic, multi-statement or mutating SQL as blocked.
- Workbench is the validation and sample execution boundary; do not execute rows from Cloud Shell.
- A sample returns at most three rows by default and never more than five.
- Never store sample values in task files, audit files or Git.
- The pilot creates new copies. Updates require the team profile and remote concurrency checks.
- Keep scheduled queries, route dictionaries and migration scripts outside this workflow.
- If a policy or validation check fails, explain the rule and stop rather than bypassing it.
