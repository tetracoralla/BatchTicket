# BatchTicket delivery roadmap

## Target state

Produce a public-source-ready release candidate of the Agent-native deterministic
structured-data runtime. The candidate keeps one core and the four public task tools,
passes the current safety and carrier contract, is selected and used through a fresh
Codex host within the route budget, and can deterministically compare a source shape
with a target JSON Schema to produce an explicit draft Transformation Plan without
guessing business meaning.

Creating the public GitHub repository, pushing source, enabling repository security
settings, tagging a release, and the final business-value verdict remain owner actions.

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
  exactness, and loss effects remain bounded and truthful.
- A clean release-candidate package, checksum, current documentation, and rerunnable
  validation commands are available locally. Apache-2.0 source publication and distribution
  of the self-contained binary are judged separately. No external release is performed.

## Current state

- Review baseline: `5962591`; the current candidate contains the owner-authorized
  optimization and final hardening changes on top of it.
- Kernel hardening and its negative regressions are implemented in the existing core;
  the full current test collection passes.
- The product brand is BatchTicket while stable technical identifiers remain unchanged.
  Transform results expose first-party runtime observations as `execution_effects`; the
  retired public key is rejected by regression and installed-bundle probes.
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
- Public repository metadata, contribution/security guidance, CI, dependency updates,
  dependency auditing, exact-license validation, and wheel/sdist inspection are defined in
  current source. `docs/RELEASE_CHECKLIST.md` is the publication handoff.
- Apache-2.0 source publication is the intended initial GitHub route. The self-contained
  plugin archive remains explicitly blocked from public upload until its incorporated Python,
  package, and native-library license materials are complete and mechanically checked.
- Final preparation reran 159 tests on Python 3.11 and 3.14. A Python 3.14 warning exposed the
  POSIX library's direct multithreaded `fork`; the default now uses `forkserver`, retains
  `python -c` behavior, and passes both versions without that warning.
- The final rebuilt and normally reinstalled macOS arm64 plugin executables match at SHA-256
  `e8bdf2e234f66d5facb19ceb9bb3fd13a71d5077cb1ffb29b9d667aff29e963c`.
- A fresh isolated host selected `data_validate` exactly once, passed the non-null and unique
  assertions, and used no retry or generic fallback. The final publication rerun used 74,154
  input tokens (54,528 cached); the host-dependent context cost remains a release risk rather
  than a hidden success condition.

## Completed work map

1. Re-run the complete current test, lint, schema, package, CLI, and self-contained plugin
   ladder against the final source.
2. Build and install the versioned final bundle from stable `dist/plugin`, then run the
   bounded cold routing matrix under the correct workspace and approval policies.
3. Record route/call/fallback/token evidence in `docs/ROUTING_EVAL.md`, verify archive
   checksum and repeatable/symlink-safe replacement, and inspect final artifact contents.
4. Report development, Agent runtime, human runtime, and owner business/experience
   acceptance independently. Publication remains a separate, explicitly authorized action.

## Rerunnable validation

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen python scripts/generate_plan_schema.py --check
uv run --frozen python scripts/check_release_hygiene.py
uv build
uv run --frozen python scripts/check_release_artifacts.py
uv run --frozen python scripts/build_plugin.py --output-root <temporary-directory>
uv run --frozen python scripts/probe_plugin.py <built-plugin-directory>
```

On macOS, standard temporary directories resolve through the `/var` system symlink,
which the plugin build's symlink guard rejects; substitute the default `dist/plugin`
output or another directory without symlinked components.

## Publication boundary

The reviewed Apache-2.0 source is published at `tetracoralla/BatchTicket`. No tag, GitHub
Release, or self-contained plugin artifact is part of this source publication. The next owner
decision is business/experience acceptance; public binary distribution remains blocked on the
third-party license inventory described in `docs/RELEASE_CHECKLIST.md`.
