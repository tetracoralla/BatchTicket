---
name: data-transformer
description: Use for deterministic inspection, reshaping, conversion, validation, or comparison of JSON, JSONL, CSV, TSV, YAML, and Parquet; for adapting one tool's structured output to another tool's input; or when a large payload should be summarized and transformed without model-generated data rewriting.
---

# Data Transformer

Move structured-data work from model reasoning into the deterministic Data Transformer tools. Keep semantic mapping choices with the user when field meaning is ambiguous.

## Route the task

- Call `data_inspect` when the source shape is genuinely unknown or the user asks what it contains.
- Call `data_transform` directly when the source shape and requested mapping are already known. Do not add a preliminary inspect call by habit.
- Call `data_validate` for JSON Schema, non-null, unique, type, field, or row-count checks.
- Call `data_diff` for schema and record comparison; provide stable key fields when records have a real unique identity.

An ordinary known task should take one tool call. A successful deterministic result is authoritative: never repeat a call with identical arguments for confirmation, and never rerun merely to change `sample_rows`, shorten presentation, or obtain a differently sized duplicate sample. If `result.data` is present, answer from it. Treat a stable validation failure or input error as the result; do not retry with guessed fields or generic code.

Omit optional sampling controls for ordinary small results. If the user actually
needs a bounded sample, use `sample_rows: 5` unless they requested another value;
the hard maximum is 100. Never substitute 100 or 1000 as a default merely because
a schema advertises a maximum.

Use the public tool schema directly; do not search the installed plugin, runtime bundle, README, source tree, or memory to discover arguments. Common source forms are:

```json
{"source":{"path":"users.json","select":"data.users[*]"},"sample_rows":2}
```

```json
{"source":{"inline":[{"id":1},{"id":2}]},"sample_rows":2}
```

`select` belongs inside `source`. When an unselected JSON or YAML envelope contains nested record arrays, one `data_inspect` call reports them in `shape.record_sets` with their selector, row count, recursively profiled fields, and logical JSON types; the separate bounded tree `sample` retains examples at those paths. Those logical types are authoritative for user-facing field/type questions. Answer from that result; do not make a second selector call merely to replace logical JSON types with DuckDB storage types unless the user explicitly asks for engine-level types.

For a client that does not grant MCP roots, `E_WORKSPACE_REQUIRED` is terminal for that call. Do not guess `workspace` names, retry absolute paths, inspect plugin files, or bypass the tool with shell code. The host must be launched with an explicit `ADT_WORKSPACE_ROOT` compatibility grant.

When the host supplies `ADT_WORKSPACE_ROOT`, omit the `workspace` argument. A
`workspace` value is only an exact root **name** returned by MCP when the client
grants multiple roots; it is never a filesystem path.

For a known validation task, use assertion objects with the discriminator named
`type`:

```json
{
  "source":{"path":"users.json","select":"data.users[*]"},
  "assertions":[
    {"type":"not_null","field":"userId"},
    {"type":"unique","field":"userId"}
  ],
  "sample_rows":2
}
```

For a known keyed comparison, call `data_diff` once:

```json
{
  "left":{"path":"before.json"},
  "right":{"path":"after.json"},
  "key_fields":["id"],
  "sample_rows":2
}
```

## Adapt to a target schema

Pass `target_schema` to `data_inspect` to compare source record fields with a JSON Schema. The adapter reports only exact and normalized-name candidates; it never treats synonyms as equivalent. It returns no executable plan until every required target field has an explicit entry in `mappings`, whose shape is `{target_field: source_field}`.

```json
{
  "source":{"path":"users.json","select":"data.users[*]"},
  "target_schema":{
    "type":"object",
    "properties":{"id":{"type":"integer"},"name":{"type":"string"}},
    "required":["id","name"]
  },
  "mappings":{"id":"userId","name":"full_name"}
}
```

Use the returned `adaptation.draft_plan` only when `adaptation.status` is `ready`. A missing, ambiguous, incompatible, or semantic mapping remains unresolved for the user; do not add a guessed cast or default value.

## Build transformation plans

- Use Transformation Plan version `1` and high-level `steps[].op` operations.
- Use the safe condition and expression objects accepted by the tool.
- Never place Python, JavaScript, shell, raw SQL, jq, regex programs, templates, or other source code in a plan.
- Use explicit `cast` steps for type changes. Do not assume that a numeric-looking string is a number.
- Add assertions for downstream requirements that can be checked mechanically.
- Use `dry_run` before a materially lossy or file-writing operation when the impact is not already established.

For a known filter-and-map task, construct the plan directly from the public
schema and call `data_transform` once. The top-level return policy is named
`return` (assertions are supplied as `assert`):

```json
{
  "plan": {
    "version": "1",
    "sources": {
      "input": {"path": "users.json", "select": "data.users[*]"}
    },
    "steps": [
      {
        "id": "filtered",
        "op": "filter",
        "source": "input",
        "where": {"field": "age", "gte": 18}
      },
      {
        "id": "adapted",
        "op": "select",
        "source": "filtered",
        "fields": [
          {"field": "userId", "as": "id"},
          {"field": "profile.name", "as": "name"},
          {"field": "profile.country", "as": "country"}
        ]
      }
    ],
    "return": {"mode": "auto"}
  }
}
```

Do not search memory, documentation, or source code to reconstruct this common
shape. If the host blocks a correctly routed tool call for approval, report the
authorization boundary; do not silently replace the deterministic runtime with
shell, jq, SQL, or model-side rewriting.

If a mapping changes business meaning rather than only structure, ask the user to choose the mapping before execution.

## Control returned data

- Prefer `return.mode: auto` for small results.
- For potentially large results, provide an explicit `output.path` and use a compact sample and receipt.
- Use `summary` only when the transformed payload does not need to continue to another tool.
- Never set `output.overwrite: true` unless the user explicitly authorized replacement of that exact file.

## Present the result

State the row and field changes, output reference when present, failed assertions, and warnings that affect use. Do not dump the full receipt unless requested. A receipt describes what execution changed; it does not prove that a semantic mapping or business decision was correct.
