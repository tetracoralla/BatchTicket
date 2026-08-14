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

An ordinary known task should take one tool call. Treat a stable validation failure or input error as the result; do not retry with guessed fields or generic code.

## Build transformation plans

- Use Transformation Plan version `1` and high-level `steps[].op` operations.
- Use the safe condition and expression objects accepted by the tool.
- Never place Python, JavaScript, shell, raw SQL, jq, regex programs, templates, or other source code in a plan.
- Use explicit `cast` steps for type changes. Do not assume that a numeric-looking string is a number.
- Add assertions for downstream requirements that can be checked mechanically.
- Use `dry_run` before a materially lossy or file-writing operation when the impact is not already established.

If a mapping changes business meaning rather than only structure, ask the user to choose the mapping before execution.

## Control returned data

- Prefer `return.mode: auto` for small results.
- For potentially large results, provide an explicit `output.path` and use a compact sample and receipt.
- Use `summary` only when the transformed payload does not need to continue to another tool.
- Never set `output.overwrite: true` unless the user explicitly authorized replacement of that exact file.

## Present the result

State the row and field changes, output reference when present, failed assertions, and warnings that affect use. Do not dump the full receipt unless requested. A receipt describes what execution changed; it does not prove that a semantic mapping or business decision was correct.
