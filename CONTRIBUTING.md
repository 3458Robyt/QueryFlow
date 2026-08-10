# Contributing

Run the full read-only suite before opening a pull request:

```bash
uv run --with 'sqlglot>=25,<30' --with 'websockets>=15,<16' python -m unittest test_queryflow.py tests
python3 scripts/build_release.py
```

Do not add real project IDs, table names, notebooks, query results, credentials or task artifacts. Use synthetic fixtures such as `source-project`, `analytics-project` and `workbench-project`.

Changes to publication, policy or sample execution need a regression test and an explanation of the digest and audit behavior.
