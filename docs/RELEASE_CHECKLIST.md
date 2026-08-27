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

The macOS arm64 builder emits a self-contained plugin with complete copied license texts,
`legal/THIRD_PARTY_NOTICES.md`, and a CycloneDX 1.5 SBOM. The legal inventory covers the locked
Python runtime dependency closure, PyInstaller bootloader, CPython runtime, and the native
libraries carried with that runtime. Build and probe checks verify those files in the copied
directory and generated archive; the archive has normalized order, ownership, timestamps, and
gzip metadata for repeatable SHA-256 output from the same source, lock, and build environment.

This does not authorize publication. Before attaching an artifact to a GitHub Release, separately
authorize the upload and review the exact target-platform archive, source/lock versions, checksum,
and any required signing/notarization policy. A macOS arm64 artifact makes no claim about another
platform.

Record transient validation evidence outside the tracked source tree. Public documentation
states the durable commands, contracts, and distribution boundary; it must not become a log of
Agent sessions, model versions, token counts, local cache hashes, or historical review claims.
