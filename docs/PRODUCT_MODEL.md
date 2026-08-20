# Product model

BatchTicket is the product brand. Stable technical identifiers remain `adt`,
`agent-data-transformer`, `data-transformer`, and the four `data_*` MCP tools.

## Users and tasks

- A human developer or operator uses the CLI to inspect, reshape, validate, compare, and convert structured data without writing a one-off script.
- An Agent uses the same deterministic core to move data between tools without reading a large payload, inventing a program, or regenerating records token by token.

The first release targets tool-to-tool data adaptation and bounded local file processing. It does not make semantic mappings or business decisions on the user's behalf.

## Related flows

Human flow:

```text
inspect source -> author or review plan -> dry-run -> transform -> consume output
```

Agent flow:

```text
known shape --------------------------> data_transform -> downstream tool
unknown shape -> data_inspect ----------------^              |
target schema -> candidates -> explicit mapping -> draft plan
                                         execution effects + compact sample
```

Validation and diff are independent entry points and can also be used after transformation.

## Existing engines and shared shapes

- DuckDB owns tabular scanning, filtering, projection, joins, grouping, aggregation, sorting, casting, and file conversion.
- `jsonschema` owns JSON Schema validation.
- PyYAML owns YAML parsing and rendering.
- Python's standard JSON and CSV libraries own their standard formats.
- The MCP Python SDK owns the transport.

The product's original layer is the versioned Transformation Plan, safe expression AST, data-shape inspection, stable result and error contracts, explicit execution effects, and token-aware return policy.

## One deterministic core

`data_transformer.runtime.DataTransformer` is the only implementation of product behavior. CLI and MCP are adapters. Plans contain no Python, SQL, jq, shell, template, or regex execution escape hatch in v1.

## Agent route budget

The public MCP surface is four task-level tools:

- `data_inspect`
- `data_transform`
- `data_validate`
- `data_diff`

An ordinary known task should require one tool call. Inspect is used only when source shape is genuinely unknown. Invalid input returns one stable error and must not invite speculative retries. The weakest intended caller only needs to construct JSON objects and choose from documented enum values.

`data_inspect` also owns the deterministic Schema Adapter profile. It compares one source
record set with an object-record JSON Schema, reports exact and normalized-name candidates,
and produces a normal Plan v1 only after required mappings are explicit and compatible.
Ambiguous record sets, semantic synonyms, composed schemas, casts, defaults, and missing
required fields remain unresolved; they are not guessed by the runtime.

## Deliberate v1 boundaries

- Inputs: inline values or explicit local paths in JSON, JSONL, CSV, TSV, YAML, and Parquet.
- Outputs: JSON, JSONL, CSV, TSV, YAML, and Parquet.
- No network sources, URLs, database connections, arbitrary source code, raw SQL, jq, or JSONPath.
- The Schema Adapter v1 maps top-level record fields only. It supports object schemas and arrays of one object schema, but does not resolve `$ref`/composition or choose business-semantic synonyms.
- Default behavior is pure: no file is written unless `output.path` is explicit.
- Existing output files are protected unless `output.overwrite` is explicitly true.
- CLI local paths express direct user authority. MCP paths use explicit client-granted MCP roots, with `ADT_WORKSPACE_ROOT` only as a compatibility grant for clients without roots support. A single root is automatic and multiple roots require the tool's named `workspace` selection. Paths remain relative to the selected root; absolute, parent, URI, and symlink escapes are rejected.
- Each logical call runs in a terminable worker. Parsing and nested contract validation occur there too. CLI stdin is acquired through a byte- and time-capped temporary resource rather than an unbounded parent-process string. Source count, cumulative bytes and rows/items, nesting, steps, wall time, RSS, all temporary files (including DuckDB spill and Python staging), inline data, samples, and the complete serialized response are bounded.
- Output destinations and return feasibility are preflighted before publication. Real execution writes to a same-directory staging file and publishes only after the worker returns a successful bounded response; dry-run performs the same destination checks without staging or publication. MCP cancellation is carried to the worker and serialized with the publication gate, so a cancelled call cannot publish a pending output afterward.

## Carrier semantics

- Exact JSON and Parquet decimals remain exact internally. Finite fractional values received through inline Python or JSON-RPC input enter the same decimal path as file JSON. Public JSON envelopes and JSON/JSONL outputs encode exact decimals as decimal strings, because arbitrary-precision JSON numbers cannot be represented reliably by every downstream parser.
- CSV and TSV require unique, non-empty headers. `\N` is the explicit null sentinel; an empty field, quoted or unquoted, is an empty string. Scalar inference is deterministic and never renames a duplicate column.
- A schema-bearing empty table is rejected for JSON, JSONL, or YAML output because those carriers cannot preserve its fields. CSV, TSV, and Parquet remain schema-bearing.
