# Agent Data Transformer

Agent Data Transformer (`adt`) is a deterministic structured-data transducer for Agent workflows. It replaces model-generated data搬运 with a versioned plan, bounded execution, compact results, and a change receipt.

It is not another jq or SQL dialect. DuckDB, JSON Schema, PyYAML, and standard parsers own established execution work. This project owns the Agent contract around them.

## What is implemented

- Inspect JSON, JSONL, CSV, TSV, YAML, and Parquet without returning full payloads.
- Safe Transformation Plan v1 with no raw code, SQL, shell, jq, regex, or template execution.
- Filter, project, drop, rename, sort, limit, deduplicate, cast, derive, explode, join, group, pivot, unpivot, flatten, unflatten, and tree mutation.
- JSON Schema plus non-null, unique, row-count, field, and type assertions.
- Schema-aware keyed or unkeyed diff.
- Dry-run with real destination preflight, staged atomic publication, and overwrite protection.
- Whole-call worker isolation with cumulative source/byte/row/item/depth/time/RSS/temp limits.
- A byte ceiling over the complete serialized response, including samples, shapes, receipts, diffs, validation failures, and errors.
- One strict typed Plan v1 model generates runtime validation, the published JSON Schema, and the live MCP schema.
- One shared core with CLI and four task-level MCP tools.

## Install and run

Development checkout:

```bash
uv sync --extra dev
uv run adt inspect examples/users.json --select 'data.users[*]'
uv run adt transform examples/adults.plan.yaml
uv run adt transform examples/adults.plan.yaml --dry-run
uv run adt validate examples/users.json --select 'data.users[*]' \
  --assertions examples/users.assertions.json
ADT_WORKSPACE_ROOT=/absolute/granted/workspace uv run adt mcp
```

Build a platform-specific plugin that does not need the repository, `uv`, a Python
installation, or a network connection at runtime:

```bash
uv run python scripts/build_plugin.py
uv run python scripts/probe_plugin.py \
  dist/plugin/data-transformer-0.1.0-darwin-arm64
```

The build produces a plugin directory, a `.tar.gz` archive, and a SHA-256 checksum in
`dist/plugin/`. Install or copy the complete generated directory; the repository-root
`.mcp.json` is development configuration, while the generated plugin's `.mcp.json` invokes
its bundled executable directly.

The example transformation returns two records inline and reports that one input row was removed. File output is opt-in through `output.path`; existing files are never replaced unless `output.overwrite` is explicitly true.

CLI paths are explicit user paths and may be absolute. MCP file paths are a narrower
capability: the server first uses workspaces granted through the MCP roots protocol. A
single root is selected automatically; when several roots are granted, pass their exact
root `name` in the tool's optional `workspace` field. Hosts without roots support may pass
an explicit `ADT_WORKSPACE_ROOT` as a compatibility grant. Tool paths remain relative to
the selected root, and absolute, parent, URI, and symlink escapes are rejected. Inline-only
MCP calls do not require a workspace grant.

## Library API

```python
from data_transformer import DataTransformer

result = DataTransformer().transform(
    {
        "version": "1",
        "sources": {"rows": {"inline": [{"x": 2}, {"x": 4}]}},
        "steps": [
            {
                "op": "derive",
                "field": "doubled",
                "expr": {"multiply": [{"field": "x"}, {"value": 2}]},
            }
        ],
    }
)
```

Public calls return `status: ok`, `status: dry_run`, or `status: error` with a stable `error.code`. They do not leak stack traces or DuckDB internals.

See [the product model](docs/PRODUCT_MODEL.md) and [Transformation Plan v1](docs/TRANSFORMATION_IR.md).
