# Agent Data Transformer

Agent Data Transformer (`adt`) is a deterministic structured-data transducer for Agent workflows. It replaces model-generated data搬运 with a versioned plan, bounded execution, compact results, and a change receipt.

It is not another jq or SQL dialect. DuckDB, JSON Schema, PyYAML, and standard parsers own established execution work. This project owns the Agent contract around them.

## What is implemented

- Inspect JSON, JSONL, CSV, TSV, YAML, and Parquet without returning full payloads.
- Discover bounded nested record sets in JSON/YAML envelopes and profile their logical fields.
- Compare a source record shape with a target JSON Schema, surface only structural mapping candidates, and emit an executable draft plan only after explicit compatible mappings.
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
uv run adt inspect examples/users.json --select 'data.users[*]' \
  --target-schema target.schema.json --mappings mappings.json
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
  dist/plugin/data-transformer-0.2.0-darwin-arm64
codex plugin marketplace add dist/plugin
codex plugin add data-transformer@data-transformer-local
```

The build produces a plugin directory, a `.tar.gz` archive, and a SHA-256 checksum in
`dist/plugin/`. Install or copy the complete generated directory; the repository-root
`.mcp.json` is development configuration, while the generated plugin's `.mcp.json` invokes
its bundled executable directly. The generated local marketplace points Codex at that
self-contained directory. Rebuilding with `--replace` refuses symlinked output roots or
generated targets before deleting or overwriting anything.

The example transformation returns two records inline and reports that one input row was removed. File output is opt-in through `output.path`; existing files are never replaced unless `output.overwrite` is explicitly true.

CLI paths are explicit user paths and may be absolute. MCP file paths are a narrower
capability: the server first uses workspaces granted through the MCP roots protocol. A
single root is selected automatically; when several roots are granted, pass their exact
root `name` in the tool's optional `workspace` field. Hosts without roots support may pass
an explicit `ADT_WORKSPACE_ROOT` as a compatibility grant. Tool paths remain relative to
the selected root, and absolute, parent, URI, and symlink escapes are rejected. Inline-only
MCP calls do not require a workspace grant.

Codex CLI 0.148 does not currently pass MCP roots to local plugin servers. For a cold CLI
host test, launch Codex with `ADT_WORKSPACE_ROOT` set to the exact test workspace. Read-only
tools can run under `approval=never`; `data_transform` has conditional file-output
capability and therefore requires an approval-capable host policy even when a particular
call returns data inline. A blocked call is an authorization result, not permission to
fall back to shell or model-side data rewriting.

## Deterministic schema adaptation

`data_inspect` accepts an optional `target_schema` and `mappings` object. It can select a
unique nested record set by structural evidence, suggest exact or normalized-name matches,
and report incompatible, missing, duplicated, omitted, or dropped fields. It does not use
fuzzy synonyms, casts, or defaults. The v1 adapter maps top-level record fields and supports
an object schema or an array whose `items` is one object schema. Composed/reference schemas
and ambiguous record sets remain explicitly unresolved.

When every required field has an explicit compatible mapping,
`adaptation.status` is `ready` and `adaptation.draft_plan` is a normal Transformation Plan
v1. Run that plan through `data_transform`; the adapter does not create a separate execution
path or fifth public tool.

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
