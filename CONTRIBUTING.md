# Contributing to BatchTicket

BatchTicket is an Agent-native deterministic structured-data runtime. Contributions should
preserve one semantic core across the library, CLI, MCP server, and plugin rather than add a
parallel execution path.

## Set up the checkout

Install [uv](https://docs.astral.sh/uv/), then run:

```bash
uv sync --frozen --extra dev
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen python scripts/generate_plan_schema.py --check
uv run --frozen python scripts/check_release_hygiene.py
```

If a contract change is intentional, update the owned Pydantic model first and regenerate the
published schema with `uv run python scripts/generate_plan_schema.py`. Review the generated diff;
do not hand-edit the schema to bypass the runtime contract.

## Submit a focused change

- Add the smallest negative regression for every repaired edge case.
- Keep unknown or conflicting Agent-authored shapes rejected before execution.
- Preserve deterministic ordering, explicit loss/effect accounting, and whole-call limits.
- Avoid raw code, shell, SQL, jq, regex, or template execution in Transformation Plan v1.
- Do not include credentials, private datasets, generated bundles, coverage files, or local state.

Before opening a pull request, also build and inspect the source distributions:

```bash
uv build
uv run --frozen python scripts/check_release_artifacts.py
```

By submitting a contribution, you agree that it is licensed under the Apache License 2.0 unless
you clearly state otherwise in the contribution.
