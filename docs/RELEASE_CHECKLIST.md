# Open-source release checklist

This checklist separates publication of the Apache-2.0 source repository from distribution of
self-contained binaries. A green source lane does not authorize a binary upload.

## Source repository gate

- [ ] `LICENSE` is the unmodified Apache License 2.0 text; ownership appears in `NOTICE`.
- [ ] `pyproject.toml`, `src/data_transformer/__init__.py`, plugin metadata, and changelog use the
  same version.
- [ ] Current branch and tag history contain no credentials, private data, local paths, coverage,
  virtual environments, build outputs, probe data, private keys, or Agent session records.
- [ ] `uv.lock` is current, `uv sync --frozen --extra dev` succeeds, and the locked dependency
  vulnerability audit is clean.
- [ ] Tests, lint, schema drift, release hygiene, wheel/sdist inspection, CLI flow, plugin build,
  plugin probe, and installed-host checks pass against the same source.
- [ ] GitHub Actions use minimum permissions, immutable full-SHA action references, current
  supported releases, CodeQL, and Dependabot updates.
- [ ] Private vulnerability reporting, dependency alerts, secret scanning, push protection, and
  protected-branch review requirements are enabled on the public repository.
- [ ] The release date replaces `Unreleased` only when tag `v0.2.0` is created.

Rerun the local mechanical gates:

```bash
uv sync --frozen --extra dev
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen python scripts/generate_plan_schema.py --check
uv run --frozen python scripts/check_release_hygiene.py
uv build
uv run --frozen python scripts/check_release_artifacts.py
batchticket_audit_file="$(mktemp)"
uv export --frozen --all-extras --no-emit-project --no-hashes --no-header --no-annotate > "$batchticket_audit_file"
uvx --from pip-audit==2.10.1 pip-audit --requirement "$batchticket_audit_file" --no-deps --disable-pip --strict --progress-spinner off
rm -f "$batchticket_audit_file"
```

When updating the public repository, push only the reviewed branch or tag. Do not use
`git push --all` or `git push --mirror`, because local tool-owned refs are not release inputs.

## Prebuilt plugin binary gate

**Current status: NO-GO for public upload.** The PyInstaller bundle contains a Python runtime,
Python packages, and platform-native libraries. The local `LICENSE` and `NOTICE` cover this
project, not every incorporated third-party work.

Local builds and probes may continue, but do not attach the generated plugin directory, archive,
or checksum to a GitHub Release until all of the following are implemented and reviewed on every
target platform:

- complete third-party license and notice collection for Python, PyInstaller, Python packages,
  OpenSSL, compression/decimal libraries, and other bundled native code;
- a manifest tying each shipped component to its version, source, license, and included notice;
- a mechanical artifact check proving those files are present in both the directory and archive;
- a platform-specific legal/license review of the final bundle contents.

This restriction does not block publishing the source repository or building the plugin locally.

Record transient validation evidence outside the tracked source tree. Public documentation
states the durable commands, contracts, and distribution boundary; it must not become a log of
Agent sessions, model versions, token counts, local cache hashes, or historical review claims.
