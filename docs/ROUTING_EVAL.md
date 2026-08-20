# Installed-host routing evaluation

## Scope and environment

This evaluation covers the locally installed, self-contained Data Transformer 0.2.0
release candidate. It does not use the source checkout, project virtual environment,
`uv`, or network access to execute data operations.

- Date: 2026-08-19
- Host: Codex CLI 0.148.0-alpha.15, `gpt-5.6-sol`
- Plugin: `data-transformer@data-transformer-local`, installed and enabled from the
  generated stable marketplace under `dist/plugin`
- Public surface: exactly `data_inspect`, `data_transform`, `data_validate`, and
  `data_diff`
- File authority: `ADT_WORKSPACE_ROOT` set to one isolated test workspace because this
  host does not grant MCP roots to local plugin servers
- Tool input-schema payload: 35,740 bytes total (`data_transform`: 23,449;
  `data_validate`: 5,790; `data_inspect`: 3,299; `data_diff`: 3,202)

The acceptance budget is one data-tool call for a known task and one inspection call for
an unknown shape. Skill loading and host startup are recorded separately; retries,
generic web/shell fallback, or model-side data rewriting fail the route.

## Cold routing matrix

| Prompt family | Policy / authority | Selected route | Calls | Result | Host token evidence |
|---|---|---:|---:|---|---:|
| Known non-null + unique validation | `approval=never`, compatibility root | `data_validate` | 1 | PASS; valid | 75,432 input, 880 output |
| Known keyed comparison | `approval=never`, compatibility root | `data_diff` | 1 | PASS; added 1, removed 0, changed 1 | 75,009 input, 678 output |
| Unknown envelope + explicit target mappings | `approval=never`, compatibility root | `data_inspect` | 1 | PASS; selected `data.users[*]`, ready draft | 75,235 input, 1,617 output |
| Known filter + projection | automatic approval review, compatibility root | `data_transform` | 1 | PASS; exact two-row inline JSON | 262,969 input (224,512 cached), 1,832 output |
| Known transform with no approval path | `approval=never`, compatibility root | `data_transform` | 0 executed | EXPECTED BLOCK; host requires authorization for a tool that can conditionally write | not counted as execution |
| File-backed call with no root or compatibility grant | `approval=never`, no authority | intended data tool | 1 | EXPECTED `E_WORKSPACE_REQUIRED`; no retry or fallback | not a product success |

The final known transform returned, in its first and only data call:

```json
[{"country":"US","id":123,"name":"Alice"},{"country":"CN","id":789,"name":"Chen"}]
```

The complete host token totals include the host's global instructions, memory, installed
skills, and other plugin schemas; they are not the marginal Data Transformer cost. They
are retained because cold-host cost is still an operational concern. The transform run's
large cached baseline is a host-level residual risk even though its product route met the
one-call budget.

## Negative discovery and compatibility finding

One pre-final run exposed a host carrier defect: the installed Skill was listed but the
session had no usable local-file reader for its `SKILL.md`, while the host's callable
declaration collapsed the complete live Plan schema to `plan: unknown`. The model first
sent only `{"version":1}`, then searched the web and guessed an obsolete plan shape.
That run is FAIL evidence and is not counted as acceptance.

The release candidate now includes one compact, canonical Plan v1 shape directly in the
`data_transform` tool description. A fresh session then produced the correct plan, made
one approved call, consumed the bounded text result, and stopped. The runtime continues
to publish and validate the full executable schema; the description is a carrier
compatibility aid, not a second semantic contract.

## Reproduction outline

1. Build the self-contained plugin and install it from the generated local marketplace.
2. Launch a fresh Codex host with an isolated `ADT_WORKSPACE_ROOT`.
3. Use `approval=never` for read-only validation, diff, and inspection prompts.
4. Use an approval-capable policy for `data_transform`, even when the specific plan is
   inline-only.
5. Inspect the persisted JSON event stream for selected tool, exact arguments, call count,
   returned content, structured result, retries, and fallback actions.

Passing read-only routes do not establish transform authorization. Direct repository MCP
calls do not establish installed-host routing. The final evidence requires both the
self-contained installed binary and a fresh natural-language host session.

## Final 2026-08-20 cache and validation recheck

The final review found that rebuilding the stable marketplace path does not itself refresh
Codex's installed plugin cache when the version remains `0.2.0`. The rebuilt bundle and the
installed cache initially had different executable hashes; two cold attempts therefore ran
stale code. Those attempts are FAIL evidence and are not counted as final validation.

Running the normal `codex plugin add data-transformer@data-transformer-local` installation
path refreshed the cache. The installed and marketplace-bundle executable hashes then
matched. In a new read-only cold session, the ordinary Chinese request “检查 userId 非空且
唯一” selected `data_validate` once with the canonical arguments:

```json
{
  "source": {"path": "examples/users.json", "select": "data.users[*]"},
  "assertions": [
    {"type": "not_null", "field": "userId"},
    {"type": "unique", "field": "userId"}
  ]
}
```

The tool returned `valid: true`; both assertions passed. There was no retry, shell, search,
generic fallback, or model-side data rewrite. The host reported 139,455 input tokens
(114,432 cached) and 1,749 output tokens, reinforcing that host-level context cost remains
high even though the product route met its one-call budget.
