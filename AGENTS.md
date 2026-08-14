# Agent Data Transformer Guidance

## Read current authority first

Before building or reviewing, read:

1. `docs/PRODUCT_MODEL.md` for the user, task, scope, and deliberate boundaries;
2. `docs/TRANSFORMATION_IR.md` and the live executable schemas for the current contract;
3. `docs/REVIEW_CONTRACT.md` for durable acceptance invariants and validation lanes;
4. current source, tests, plugin configuration, and installed runtime state.

Current code and runtime behavior outrank historical reports, generated artifacts, and earlier acceptance claims. Use the global `build-agent-native-utilities` skill for construction or review of this project.

## Preserve the product definition

This project is an Agent-native deterministic structured-data transformation runtime. Its owned value is the typed transformation contract, safe execution boundary, shape inspection, truthful loss/receipt accounting, and token-aware I/O. It is not a new jq, SQL, parser, database, or general code-execution surface.

Keep library, CLI, MCP, and plugin adapters on one semantic core. Reuse mature engines for established work, but do not let engine behavior silently define the public product contract.

## Build invariants

- Expose every Agent-authored nested input through a complete executable schema. Reject unknown, conflicting, or ambiguous shapes before execution, and keep transport schema, runtime validation, and published schema on one owned model or drift check.
- Resolve MCP resources against an explicit granted workspace. Do not use the installed plugin directory or ambient process directory as the user's data root, and do not permit path, symlink, or URI escape without a separate explicit grant.
- Apply cumulative input, source, row/item, nesting, time, memory, temporary-storage, and output budgets to the whole logical call. Keep every untrusted parser, validator, engine, and result phase inside the enforceable worker boundary.
- Bound the complete serialized response, including samples, shape metadata, receipts, diff snippets, validation failures, and error details.
- Preserve deterministic ordering and carrier-independent meaning. Inline and file inputs that represent the same value must not change kind or semantics accidentally.
- Never silently coerce, collide, drop, duplicate, round, truncate, or invent data. Make lossy behavior explicit and add the smallest negative regression test for every repaired edge case.
- Preflight destinations, overwrite authority, return policy, and response feasibility before mutation. Dry-run uses the same preflight and never publishes output. An ordinary error must not hide a completed side effect.
- Make receipts truthful for single- and multi-input operations. Record the input sides, matches, unmatched data, fan-out, new nulls, dropped data, type changes, assertions, and material warnings that affect downstream use.

## Work and review discipline

- Search for and extend the existing core, types, operations, and tests before adding a parallel module.
- A bug fix includes the smallest negative test that fails when the fix is reverted. Do not weaken assertions, fixtures, schemas, or checkers to obtain green output.
- Run the current development gates and the relevant real CLI/MCP sequences. Keep development regression, runtime Agent flow, runtime human flow, and owner experience acceptance as separate conclusions.
- A builder does not self-accept from a green suite. A later reviewer reopens the current source, introspects the live schemas, and independently reruns representative happy, boundary, hostile, and stateful sequences.
- Do not auto-commit. Present the current diff and actual validation results, then wait for owner or separate review authorization.

