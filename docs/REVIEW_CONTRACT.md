# Review contract

This contract gives later construction and review Agents a stable way to judge the current Data Transformer. It is not a feature checklist or completion receipt. Exact operations and fields come from the current executable schemas; every reviewer must reopen the current source and runtime.

## Acceptance invariants

| Area | Pass condition | Failure signal |
|---|---|---|
| Agent contract | The installed live tool schema fully describes every dominant request after tool selection; unknown and conflicting shapes fail before execution. | Opaque objects, duplicated schema prose, runtime-only required fields, or trial-and-error needed for an ordinary call. |
| One semantic core | Library, CLI, MCP, plugin, inline, and file carriers preserve the same promised data meaning, ordering, errors, and limits. | Adapter-specific business logic or the same value changes kind or result by carrier. |
| Resource authority | MCP relative resources resolve inside an explicit granted workspace; escape and destructive authority are explicit. | Server/plugin cwd becomes the data root, or Agent input can reach ungranted paths, links, URIs, or outputs. |
| Whole-call execution boundary | One cumulative deadline, RSS ceiling, and size/count/depth budget covers parsing, validation, engine work, hashing, receipts, and serialization; breached workers are terminated and rebuilt. | Per-step limits multiply, or non-engine phases can consume unbounded CPU or memory. |
| Determinism and loss | Repeated execution is stable; ordering, collisions, coercion, precision, null creation, truncation, and dropped/duplicated data are explicit. | Silent value change, invented field, carrier-dependent inference, or an unreported lossy conversion. |
| Response economy | The complete serialized success or error envelope stays inside the declared byte budget for huge values, wide shapes, many failures, and receipt-heavy work. | A bounded mode still returns the original large value through sample, schema, receipt, diff, or error detail. |
| Side-effect truth | Destination, format, overwrite authority, return policy, and response feasibility are preflighted before publication; dry-run makes the same decision without writing. | A call reports ordinary failure after mutation, or dry-run passes a target the real call must reject. |
| Receipt integrity | Receipts state observed effects without claiming semantic correctness; fan-in reports each input, matches, unmatched data, fan-out, nulls, loss, and assertions. | Missing measurements are rendered as misleading null/zero values or every output field is reported as newly added. |
| Installed Agent route | A cold supported prompt selects the intended installed tool within the route budget, executes it, and continues through a bounded result or output reference without generic fallback. | Skill/tool version drift, missing tools, extra discovery calls, retries, or shell/model rewriting of the data. |
| Schema adaptation | Exact structural evidence and explicit mappings can produce a normal executable Plan v1; ambiguity, incompatibility, unsupported schema composition, and semantic choices remain unresolved. | A fuzzy/synonym guess, implicit cast/default, ambiguous source selection, or plan before required mappings are explicit. |
| Human route | The real CLI supports inspect, plan execution, dry-run, output continuation, validation/diff, and recovery with stable machine-readable results. | Only direct library tests pass, or a failed action leaves stale or hidden output. |

## Required adversarial families

Keep happy-path tests, then add focused negative regression for every repaired issue. Across the suite and runtime checks, cover these families:

- nested valid plans plus misspelled, unknown, wrong-variant, and conflicting fields;
- relative workspace access plus absolute, parent, symlink, URI, missing, existing, and unauthorized output targets;
- cumulative multi-source work, deep structures, hostile validation patterns, engine-heavy work, worker timeout/memory breach, and post-breach recovery;
- one huge scalar, one very wide record, many validation failures, a large diff value, and a multi-step receipt under the total response budget;
- exact numeric boundaries, temporal/encoding/structural loss, field collisions, duplicate output names, null parents, and deterministic ordering;
- unmatched and many-to-many joins, fan-out, new nulls, dropped rows/fields, and receipt/assertion behavior;
- empty schema-bearing CSV/Parquet, empty schema-less inputs, inline/file equivalence, and empty diff/transform paths;
- return-policy and destination failures before publication, followed by the same dry-run and real-run preflight decision.
- adapter exact/normalized candidates, tied record sets, unselectable paths, unknown mappings, nullable/type mismatch, source duplication, array bounds, and unsupported schema composition.

Exact regressions belong in executable tests. Do not turn this section into a growing narrative inventory of old bugs.

## Validation lanes

### Development regression — Agent/Reviewer reports PASS, FAIL, or BLOCKED

At minimum, run the current equivalents of:

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
uv build
uv run python scripts/build_plugin.py
uv run python scripts/probe_plugin.py <built-plugin-directory>
```

Also validate schema generation/drift, package contents, public tool names and annotations, and every changed negative regression. A checker changed in the same task is only a narrow veto until its expectation is independently justified.

### Runtime Agent flow — Agent/Reviewer reports PASS, FAIL, or BLOCKED

Use the self-contained built plugin in a fresh host session without the source checkout,
project virtual environment, `uv`, or network access. Introspect the live tool schemas,
issue ordinary supported prompts, record selected tool/call count/retries/fallbacks, and
execute happy, ambiguity, invalid, resource-boundary, large-result/reference,
timeout/recovery, and side-effect-preflight sequences.

Record workspace-root grant and approval policy separately from routing. A host that does
not grant MCP roots must receive one explicit `ADT_WORKSPACE_ROOT` compatibility grant.
Because `data_transform` can write when `output.path` is present, a host may require
approval for the tool as a whole even for an inline-only call. `approval=never` is valid
evidence for read-only routing and for the authorization boundary, but it cannot establish
successful transform execution. Never count shell/jq/model fallback as a plugin pass.

Direct Python calls or a repo-root stdio test do not establish installed-host activation or relative workspace behavior.

### Runtime human flow — Agent/Reviewer reports PASS, FAIL, or BLOCKED

Use the real CLI to complete the primary sequence:

```text
inspect source -> author/review plan -> dry-run -> transform/output -> consume -> validate/diff -> recover from failure
```

Verify exit codes, stable structured errors, file continuation, overwrite protection, and absence of stale or hidden output after failure.

### Business and experience acceptance — Owner reports OK, Not OK, or Pending

The owner judges whether the current utility is useful, understandable, and worth adopting. Green technical lanes cannot issue or override that verdict.

## Reviewer result

Report actionable findings first and keep code ranges tight. Then state the four validation lanes independently, the exact blocked route, and residual risk. Do not accept a prior report, receipt, screenshot, package, or aggregate green gate as authority; reopen and rerun the current facts.
