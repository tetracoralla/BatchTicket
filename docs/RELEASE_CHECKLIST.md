# Open-source release checklist

This checklist separates publication of the Apache-2.0 source repository from distribution of
self-contained binaries. A green source lane does not authorize a binary upload.

## Source repository gate

- [ ] `LICENSE` is the unmodified Apache License 2.0 text; ownership appears in `NOTICE`.
- [ ] `pyproject.toml`, `src/data_transformer/__init__.py`, plugin metadata, and changelog use the
  same version.
- [ ] Current branch and tag history contain no credentials, private data, local paths, coverage,
  virtual environments, build outputs, probe data, or private keys.
- [ ] `uv.lock` is current, `uv sync --frozen --extra dev` succeeds, and the locked dependency
  vulnerability audit is clean.
- [ ] Tests, lint, schema drift, release hygiene, wheel/sdist inspection, CLI flow, plugin build,
  plugin probe, and installed-host checks pass against the same source.
- [ ] GitHub Actions use minimum permissions, immutable full-SHA action references, current
  supported releases, CodeQL, and Dependabot updates.
- [ ] Create the empty public repository, immediately enable private vulnerability reporting,
  dependency alerts, secret scanning, and push protection, then push the reviewed source.
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

When publishing, push only the reviewed branch history needed for the public repository. Do not
use `git push --all` or `git push --mirror`, because local tool-owned refs are not release inputs.

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

## Current local evidence — 2026-08-20

- Python 3.11 and 3.14: 159 tests passed; the 3.14 run no longer emits multithreaded-fork
  deprecation warnings after the library default moved to `forkserver` on POSIX.
- Lint, schema drift, exact Apache-2.0 text, tracked/history hygiene, wheel/sdist inspection,
  and the complete locked dependency vulnerability audit: PASS.
- Human CLI primary sequence (inspect, dry-run, publish, consume, validate, keyed diff, expected
  overwrite failure, unchanged prior output): PASS.
- Self-contained macOS arm64 build, archive checksum, ten-sequence direct probe, normal plugin
  reinstall, and installed-cache executable hash equivalence: PASS.
- Independent release re-review on 2026-08-20 reproduced the gates above, verified current
  checkout/setup-uv/pip-audit versions externally, and fixed the macOS plugin-smoke CI temp
  path (workspace path instead of `mktemp -d`, which resolves through the `/var` symlink the
  build's guard rejects), documented that macOS constraint for local plugin builds, and added
  Python 3.11–3.14 trove classifiers. Every local gate was rerun after the changes.
- Final publication review pinned every remote GitHub Action to an immutable full commit SHA,
  added CodeQL analysis, and corrected the security-setting order to create an empty public
  repository, enable its public-only reporting and scanning controls, then push source.
- Fresh isolated Codex host: one `data_validate` call, both assertions passed, no retry or generic
  fallback. The final publication rerun used 74,154 input tokens (54,528 cached); host-level
  context cost remains high and host-dependent.
- Public source repository: ready for owner publication step. Prebuilt plugin upload: NO-GO as
  stated above. Business/experience acceptance: Pending owner verdict.
