# Data Transformer delivery roadmap

## Target state

Produce a locally installable release candidate of the Agent-native deterministic
structured-data runtime. The candidate keeps one core and the four public task tools,
passes the current safety and carrier contract, is selected and used through a fresh
Codex host within the route budget, and can deterministically compare a source shape
with a target JSON Schema to produce an explicit draft Transformation Plan without
guessing business meaning.

External publication and the final business-value verdict remain owner decisions.

## Finish line

- The current kernel hardening is independently reviewed against current source and
  the adversarial families in `docs/REVIEW_CONTRACT.md`.
- Library, CLI, MCP, and the self-contained plugin use one current contract and pass
  their development and runtime sequences.
- A fresh installed host exposes the matching plugin Skill and exactly four tools;
  ordinary supported prompts stay within the route budget without generic fallback.
- Schema adaptation is deterministic: exact and normalized structural matches are
  explained, ambiguous or semantic choices remain unresolved, and any executable
  plan requires explicit mappings.
- Complete serialized responses, side effects, cancellation, resource authority,
  exactness, and loss receipts remain bounded and truthful.
- A clean release-candidate package, checksum, current documentation, and rerunnable
  validation commands are available locally. No external release is performed.

## Current state

- Review baseline: `5962591`; the current candidate contains the owner-authorized
  optimization and final hardening changes on top of it.
- Kernel hardening and its negative regressions are implemented in the existing core;
  the full current test collection passes.
- Nested record-set inspection and deterministic `record-schema-v1` adaptation are
  implemented inside `data_inspect`; ready drafts execute through `data_transform`.
- The self-contained 0.2.0 release-candidate bundle is installed through the generated
  local marketplace. The final source is rebuilt and directly probed against exactly four
  tools. After refreshing the installed cache and verifying its executable hash against the
  rebuilt bundle, a 2026-08-20 cold validation prompt selected `data_validate` once with
  canonical arguments and passed both assertions; the full evidence is in
  `docs/ROUTING_EVAL.md`.
- Codex CLI 0.148 requires `ADT_WORKSPACE_ROOT` for file-backed plugin calls because it
  does not grant MCP roots. Read-only inspect routes correctly. `data_transform` is blocked
  under `approval=never` because the tool has conditional file-output capability; that
  authorization result must be tested separately under an approval-capable policy.
- Final cold validation, keyed diff, unknown-envelope adaptation, and approved transform
  each use one intended data call without generic fallback. The host collapses the nested
  Plan schema to `unknown` and cannot read the listed plugin Skill in some sessions, so the
  transform tool description now carries one compact canonical Plan v1 shape; a fresh
  rerun used it successfully and returned exact inline data without repetition.
- Current live tool-schema size is 35,740 bytes total; `data_transform` remains 23,449 bytes.
- Full cold-host token totals remain high and host-dependent (about 75k input for the
  read-only routes and 263k, mostly cached, for the final transform); this is recorded as
  an operational cost risk rather than hidden behind the one-call route result.

## Completed work map

1. Re-run the complete current test, lint, schema, package, CLI, and self-contained plugin
   ladder against the final source.
2. Build and install the versioned final bundle from stable `dist/plugin`, then run the
   bounded cold routing matrix under the correct workspace and approval policies.
3. Record route/call/fallback/token evidence in `docs/ROUTING_EVAL.md`, verify archive
   checksum and repeatable/symlink-safe replacement, and inspect final artifact contents.
4. Report development, Agent runtime, human runtime, and owner business/experience
   acceptance independently. The owner authorized one local source commit for this review;
   no external publication is authorized.

## Rerunnable validation

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
uv build
uv run --frozen python scripts/generate_plan_schema.py --check
uv run --frozen python scripts/build_plugin.py --output-root <temporary-directory>
uv run --frozen python scripts/probe_plugin.py <built-plugin-directory>
```

## Next owner action

After the authorized local source commit, review the four independent acceptance lanes and
decide whether to publish, keep the candidate local for dogfood, or request another
technical iteration. The commit does not imply external publication.
