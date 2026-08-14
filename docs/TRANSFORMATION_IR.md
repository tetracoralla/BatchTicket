# Transformation Plan v1

A plan is declarative JSON or YAML:

```yaml
version: "1"
sources:
  users:
    path: ./users.json
    select: data.users[*]
steps:
  - id: adults
    op: filter
    source: users
    where: {field: age, gte: 18}
  - id: output
    op: select
    source: adults
    fields:
      - {field: userId, as: id}
      - {field: profile.name, as: name}
assert:
  - {id: id_required, type: not_null, field: id}
return:
  mode: auto
  sample_rows: 5
  max_inline_bytes: 10000
```

Every step reads an explicit `source`, or the preceding step when omitted. A step result is addressable through its `id`. The last step is the final result.

Every nested object is closed: unknown fields, conflicting source forms, ambiguous condition/expression shapes, and operation fields that do not belong to the selected `op` are rejected. The executable contract in `contracts.py` generates both the live MCP schema and `schemas/transformation-plan.schema.json`.

## Operators

Table operators:

- `filter`, `select`, `drop`, `rename`, `sort`, `limit`, `dedupe`, `cast`, `derive`, `explode`
- `join`, `group`, `pivot`, `unpivot`
- `flatten`, `unflatten`

Tree operators:

- `set`, `delete`, `merge`, `flatten`, `unflatten`

Contract operators:

- final `assert` entries: `field_exists`, `not_null`, `unique`, `row_count`, `type`
- JSON Schema validation through `data_validate` or a plan's top-level `schema`

## Conditions

A leaf condition names one field and one comparison:

```json
{"field":"status","eq":"paid"}
```

Comparisons are `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `contains`, `starts_with`, `ends_with`, `is_null`, and `not_null`. Conditions compose through `all`, `any`, and `not`.

Comparison operands must have compatible types. Numeric-looking strings are strings, not numbers; use an explicit `cast` step before a numeric comparison. The same rule applies to filter conditions, join keys, and pivot values. String operations accept string fields rather than silently stringifying other types.

## Expression AST

`derive.expr` is data, not source code:

```json
{"multiply":[{"field":"price"},{"field":"quantity"}]}
```

Leaves are `field` or `value`. Operations are `add`, `subtract`, `multiply`, `divide`, `concat`, `coalesce`, `lower`, `upper`, `substring`, and `round`. Unknown shapes are rejected before execution. `coalesce` does not choose a type by silently widening operands: non-null operands from different type families require an explicit cast step first.

`group.by` and `pivot.by` accept either a field string or `{field, as}`. If two nested field paths have the same leaf name, explicit distinct aliases are required. `flatten` escapes separators inside source field names and preserves empty objects so `flatten` followed by `unflatten` is reversible, including for ordinary snake_case fields and schema-bearing empty tables inside one plan.

## Return policy

- `auto`: return data inline when it fits; otherwise return the explicit output reference.
- `inline`: require the full result to fit `max_inline_bytes`.
- `summary`: return summary, sample, and receipt only.
- `reference`: require `output.path` and return its reference.

`sample_rows` and `max_inline_bytes` are hard-capped even if a caller asks for more.

Execution is also bounded by `max_input_bytes`, `max_sources`, `max_rows`,
`max_items`, `max_depth`, `max_steps`, `max_memory_mb`, `max_temp_bytes`,
`timeout_ms`, and `max_response_bytes`. `timeout_ms` and `max_memory_mb` apply to the
whole isolated call, including parsing, schema validation, execution, receipts,
hashing, and serialization. `max_response_bytes` applies to the complete serialized
result rather than only `result.data`. Requested values cannot exceed the runtime's
hard safety ceilings.

## Receipt

The receipt records current input/output shapes, per-step row and field changes, value-level lossy-cast warnings, and assertion outcomes. Tree mutations record added and removed JSON Pointer paths, changed values, and type changes. Join receipts separately report both inputs, matched pairs and sides, unmatched rows, fan-out, output rows, and newly introduced nulls. It is an execution account, not a claim that a business mapping was correct.
