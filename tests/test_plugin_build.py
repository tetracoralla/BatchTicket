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


def test_plugin_archive_is_byte_reproducible_for_identical_bundle_content(tmp_path) -> None:
    module = _build_module()
    first = tmp_path / "first" / "bundle"
    second = tmp_path / "second" / "bundle"
    for bundle in (first, second):
        (bundle / "nested").mkdir(parents=True)
        (bundle / "nested" / "payload.txt").write_text("same content\n", encoding="utf-8")

    first_archive, first_checksum = module._archive_bundle(first, replace=False)
    second_archive, second_checksum = module._archive_bundle(second, replace=False)

    assert first_archive.read_bytes() == second_archive.read_bytes()
    assert first_checksum.read_text(encoding="utf-8") == second_checksum.read_text(encoding="utf-8")


def test_legal_material_is_complete_and_machine_readable(tmp_path) -> None:
    module = _build_module()
    module._write_legal_material(tmp_path)

    legal_root = tmp_path / "legal"
    notices = (legal_root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    sbom = json.loads((legal_root / "sbom.cdx.json").read_text(encoding="utf-8"))

    assert "CPython" in notices
    assert "PyInstaller" in notices
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert {component["name"] for component in sbom["components"]} >= {
        "CPython",
        "PyInstaller",
        "duckdb",
        "mcp",
    }
    for component in sbom["components"]:
        for property_ in component.get("properties", []):
            if property_["name"] == "batchticket:license-files":
                for path in property_["value"].split(","):
                    assert (legal_root / path).is_file()


def test_archive_verification_requires_legal_material(tmp_path) -> None:
    module = _build_module()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    archive, _ = module._archive_bundle(bundle, replace=False)

    with pytest.raises(ValueError, match="required legal material"):
        module._verify_archive(bundle, archive)


def test_plugin_build_writes_immutable_capability_bindings(tmp_path) -> None:
    module = _build_module()

    module._write_capability_material(tmp_path)

    manifest = json.loads(
        (tmp_path / "capabilities" / "provider.json").read_text(encoding="utf-8")
    )
    implementation = manifest["implementations"][0]
    assert implementation["adapter"] == {
        "protocol": "openadam.capability-jsonl.v0.1",
        "command": "./runtime/adt-capability",
        "args": [],
        "cwd": ".",
    }
    assert implementation["transportSchemaProbe"] == {
        "protocol": "openadam.transport-schema-jsonl.v0.1",
        "command": "./runtime/adt-transport-schema-probe",
        "args": [],
        "cwd": ".",
    }
    assert sorted(path.name for path in (tmp_path / "capabilities" / "schemas").iterdir()) == [
        "structured-data.inspect.input.schema.json",
        "structured-data.inspect.output.schema.json",
        "structured-data.validate.input.schema.json",
        "structured-data.validate.output.schema.json",
    ]


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
