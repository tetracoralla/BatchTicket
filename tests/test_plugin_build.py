from __future__ import annotations

import json
import tomllib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


def _build_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_plugin.py"
    spec = spec_from_file_location("data_transformer_build_plugin", script)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_build_writes_installable_local_marketplace(tmp_path) -> None:
    bundle = tmp_path / "data-transformer-0.1.0-darwin-arm64"
    bundle.mkdir()

    marketplace = _build_module()._write_marketplace(tmp_path, bundle)

    payload = json.loads(marketplace.read_text(encoding="utf-8"))
    assert payload == {
        "name": "data-transformer-local",
        "interface": {"displayName": "BatchTicket Local"},
        "plugins": [
            {
                "name": "data-transformer",
                "source": {
                    "source": "local",
                    "path": "./data-transformer-0.1.0-darwin-arm64",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }
    assert not list(marketplace.parent.glob(".marketplace.*.tmp"))


def test_plugin_build_rejects_symlink_output_root(tmp_path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValueError, match="must not contain symlinks"):
        _build_module().build(linked, replace=True)


def test_plugin_build_rejects_symlink_bundle_before_replacement(tmp_path) -> None:
    module = _build_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / module._bundle_name(module._project_version())
    destination.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink|must not contain symlinks"):
        module.build(tmp_path, replace=True)

    assert outside.is_dir()


def test_plugin_archive_rejects_symlink_before_replacement(tmp_path) -> None:
    module = _build_module()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside.tar.gz"
    outside.write_text("keep", encoding="utf-8")
    archive = tmp_path / "bundle.tar.gz"
    archive.symlink_to(outside)

    with pytest.raises(ValueError, match="must not be a symlink|must not contain symlinks"):
        module._archive_bundle(bundle, replace=True)

    assert outside.read_text(encoding="utf-8") == "keep"


def test_marketplace_rejects_symlink_before_replacement(tmp_path) -> None:
    module = _build_module()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    marketplace.symlink_to(outside)

    with pytest.raises(ValueError, match="must not be a symlink|must not contain symlinks"):
        module._write_marketplace(tmp_path, bundle)

    assert outside.read_text(encoding="utf-8") == "keep"


def test_marketplace_does_not_replace_an_unrelated_file(tmp_path) -> None:
    module = _build_module()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    original = '{"name":"another-marketplace","plugins":[]}'
    marketplace.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="not owned by this build"):
        module._write_marketplace(tmp_path, bundle, replace=True)

    assert marketplace.read_text(encoding="utf-8") == original


def test_marketplace_requires_replace_authority_for_an_existing_owned_file(tmp_path) -> None:
    module = _build_module()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    marketplace = module._write_marketplace(tmp_path, bundle)
    original = marketplace.read_text(encoding="utf-8")

    with pytest.raises(FileExistsError, match="output already exists"):
        module._write_marketplace(tmp_path, bundle)

    assert marketplace.read_text(encoding="utf-8") == original


def test_sdist_excludes_local_probe_and_coverage_artifacts() -> None:
    with Path("pyproject.toml").open("rb") as source:
        configuration = tomllib.load(source)

    excluded = configuration["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert ".coverage*" in excluded
    assert "probe_users.json" in excluded


def test_generated_output_publish_restores_previous_outputs_on_failure(
    tmp_path, monkeypatch
) -> None:
    module = _build_module()
    staged_bundle = tmp_path / "stage-bundle"
    staged_bundle.mkdir()
    (staged_bundle / "marker").write_text("new", encoding="utf-8")
    staged_archive = tmp_path / "stage-archive"
    staged_archive.write_text("new archive", encoding="utf-8")

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "marker").write_text("old", encoding="utf-8")
    archive = tmp_path / "archive"
    archive.write_text("old archive", encoding="utf-8")

    real_replace = module.os.replace
    failed = False

    def fail_once(source, target):
        nonlocal failed
        if Path(source) == staged_archive and not failed:
            failed = True
            raise OSError("simulated publication failure")
        return real_replace(source, target)

    monkeypatch.setattr(module.os, "replace", fail_once)

    with pytest.raises(OSError, match="simulated publication failure"):
        module._publish_generated_outputs(
            [(staged_bundle, bundle), (staged_archive, archive)],
            tmp_path / "previous",
            replace=True,
        )

    assert (bundle / "marker").read_text(encoding="utf-8") == "old"
    assert archive.read_text(encoding="utf-8") == "old archive"
