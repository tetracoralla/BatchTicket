# BatchTicket roadmap

BatchTicket is a public Apache-2.0 project. This roadmap describes product direction rather
than past build sessions or release-review evidence. Current behavior is defined by the
executable schemas and source; acceptance invariants live in `docs/REVIEW_CONTRACT.md`.

## Current release line

- Keep one deterministic core behind the library, CLI, MCP server, and plugin.
- Keep the public MCP surface to `data_inspect`, `data_transform`, `data_validate`, and
  `data_diff`.
- Preserve explicit workspace authority, cumulative whole-call limits, bounded complete
  responses, destination preflight, cancellation safety, exact values, and truthful
  `execution_effects`.
- Keep schema adaptation structural and deterministic. Required semantic mappings remain an
  explicit caller decision.

## Next priorities

- Reduce installed-host schema and context cost without weakening the executable contract.
- Track MCP host support for granted roots and retain `ADT_WORKSPACE_ROOT` only as a documented
  compatibility grant where roots are unavailable.
- Expand carrier-equivalence and hostile-input coverage as new formats or operations are
  proposed.
- Complete the component manifest and third-party license inventory required before any
  self-contained plugin binary is publicly distributed.

## Deliberate non-goals

- No arbitrary Python, JavaScript, shell, SQL, jq, regex, or template execution.
- No network fetches, databases, semantic mapping guesses, or silent coercion.
- No fifth public tool for schema adaptation; ready drafts remain normal Transformation Plan
  v1 inputs to `data_transform`.

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

The Apache-2.0 source is published at `tetracoralla/BatchTicket`. Source publication does not
authorize a tag, GitHub Release, package-registry upload, or self-contained plugin asset.
Public binary distribution remains blocked on the third-party inventory described in
`docs/RELEASE_CHECKLIST.md`.
