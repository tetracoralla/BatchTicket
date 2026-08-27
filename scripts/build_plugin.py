from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
from email.parser import BytesParser
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT_DISTRIBUTIONS = (
    "duckdb",
    "jsonschema",
    "mcp",
    "pydantic",
    "PyYAML",
    "setuptools",
)
ARCHIVE_EPOCH = 0
LICENSE_OVERRIDES = {"duckdb": "MIT"}


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _assert_no_symlink_components(path: Path) -> None:
    absolute = _absolute_without_resolving(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if not _lexists(current):
            continue
        if current.is_symlink():
            raise ValueError(f"output path must not contain symlinks: {current}")


def _validate_generated_target(path: Path, *, directory: bool | None = None) -> None:
    _assert_no_symlink_components(path.parent)
    if not _lexists(path):
        return
    if path.is_symlink():
        raise ValueError(f"generated output must not be a symlink: {path}")
    if directory is True and not path.is_dir():
        raise ValueError(f"expected generated directory: {path}")
    if directory is False and not path.is_file():
        raise ValueError(f"expected generated file: {path}")


def _project_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def _bundle_name(version: str) -> str:
    system = platform.system().lower().replace(" ", "-")
    machine = platform.machine().lower().replace(" ", "-")
    return f"data-transformer-{version}-{system}-{machine}"


def _runtime_distributions() -> list[importlib.metadata.Distribution]:
    """Return the locked runtime dependency closure, never development tools."""
    environment = default_environment()
    environment["extra"] = ""
    pending = list(RUNTIME_ROOT_DISTRIBUTIONS)
    resolved: dict[str, importlib.metadata.Distribution] = {}
    while pending:
        requested = pending.pop()
        normalized = canonicalize_name(requested)
        if normalized in resolved:
            continue
        distribution = importlib.metadata.distribution(requested)
        resolved[normalized] = distribution
        for raw_requirement in distribution.requires or []:
            requirement = Requirement(raw_requirement)
            if requirement.marker is None or requirement.marker.evaluate(environment):
                pending.append(requirement.name)
    return sorted(
        resolved.values(),
        key=lambda item: canonicalize_name(item.metadata["Name"]),
    )


def _distribution_license_files(distribution: importlib.metadata.Distribution) -> list[Path]:
    files: list[Path] = []
    for relative in distribution.files or []:
        parts = [part.lower() for part in relative.parts]
        name = relative.name.lower()
        if (
            "licenses" in parts
            or "license" in name
            or "copying" in name
            or "notice" in name
        ):
            candidate = Path(distribution.locate_file(relative))
            if candidate.is_file():
                files.append(candidate)
    return sorted(set(files))


def _project_url(distribution: importlib.metadata.Distribution) -> str | None:
    project_urls = distribution.metadata.get_all("Project-URL") or []
    for value in project_urls:
        _, separator, url = value.partition(",")
        if separator and url.strip().startswith("https://"):
            return url.strip()
    homepage = distribution.metadata.get("Home-page")
    return homepage if homepage and homepage.startswith("https://") else None


def _copy_file(source: Path, destination: Path, *, relative_to: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination.relative_to(relative_to).as_posix()


def _copy_distribution_licenses(
    distribution: importlib.metadata.Distribution,
    licenses_root: Path,
) -> list[str]:
    component_slug = f"{canonicalize_name(distribution.metadata['Name'])}-{distribution.version}"
    installation_root = Path(distribution.locate_file(""))
    copied: list[str] = []
    for source in _distribution_license_files(distribution):
        copied.append(
            _copy_file(
                source,
                licenses_root / component_slug / source.relative_to(installation_root),
                relative_to=licenses_root.parent,
            )
        )
    return copied


def _setuptools_vendored_components(
    distribution: importlib.metadata.Distribution,
    copied_licenses: list[str],
) -> list[dict[str, object]]:
    if canonicalize_name(distribution.metadata["Name"]) != "setuptools":
        return []
    installation_root = Path(distribution.locate_file(""))
    components: list[dict[str, object]] = []
    for relative in distribution.files or []:
        if relative.name != "METADATA" or "_vendor" not in relative.parts:
            continue
        metadata_path = Path(distribution.locate_file(relative))
        if not metadata_path.is_file():
            continue
        metadata = BytesParser().parsebytes(metadata_path.read_bytes())
        name = metadata.get("Name")
        version = metadata.get("Version")
        if not name or not version:
            continue
        vendor_root = metadata_path.parent
        vendor_licenses = [
            copied
            for copied in copied_licenses
            if (installation_root / copied.split("/", 2)[-1]).is_relative_to(vendor_root)
        ]
        if not vendor_licenses:
            raise ValueError(f"setuptools vendored dependency has no copied license: {name}")
        license_expression = (
            metadata.get("License-Expression") or metadata.get("License") or "NOASSERTION"
        )
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{canonicalize_name(name)}@{version}",
                "licenses": [{"license": {"id": license_expression}}],
                "properties": [
                    {"name": "batchticket:license-files", "value": ",".join(vendor_licenses)},
                    {
                        "name": "batchticket:vendored-by",
                        "value": f"setuptools@{distribution.version}",
                    },
                ],
            }
        )
    return components


def _write_legal_material(destination: Path) -> None:
    """Create a bundle-local SBOM and complete license copies for shipped code."""
    legal_root = destination / "legal"
    licenses_root = legal_root / "licenses"
    components: list[dict[str, object]] = []
    notice_lines = [
        "# BatchTicket third-party notices",
        "",
        "This directory is generated during the macOS arm64 plugin build. It inventories the",
        "third-party code copied into this bundle; `sbom.cdx.json` is the machine-readable form.",
        "License texts named below are included under `legal/licenses/`.",
        "",
    ]

    for distribution in _runtime_distributions():
        name = distribution.metadata["Name"]
        version = distribution.version
        copied = _copy_distribution_licenses(distribution, licenses_root)
        if not copied:
            raise ValueError(f"runtime dependency has no distributable license text: {name}")
        declared_license = LICENSE_OVERRIDES.get(canonicalize_name(name)) or (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or "NOASSERTION"
        )
        notice_lines.extend(
            [
                f"## {name} {version}",
                "",
                f"- License: {declared_license}",
                f"- Source: {_project_url(distribution) or 'not declared in installed metadata'}",
                f"- Included license files: {', '.join(copied)}",
                "",
            ]
        )
        component: dict[str, object] = {
            "type": "library",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{canonicalize_name(name)}@{version}",
            "licenses": [{"license": {"id": declared_license}}],
            "properties": [
                {"name": "batchticket:license-files", "value": ",".join(copied)},
            ],
        }
        source_url = _project_url(distribution)
        if source_url:
            component["externalReferences"] = [{"type": "website", "url": source_url}]
        components.append(component)
        for vendored in _setuptools_vendored_components(distribution, copied):
            vendor_license = vendored["licenses"][0]["license"]["id"]
            vendor_paths = vendored["properties"][0]["value"]
            notice_lines.extend(
                [
                    f"## {vendored['name']} {vendored['version']} (vendored by setuptools)",
                    "",
                    f"- License: {vendor_license}",
                    f"- Included license files: {vendor_paths}",
                    "",
                ]
            )
            components.append(vendored)

    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if not python_license.is_file():
        raise ValueError(f"CPython license text is unavailable: {python_license}")
    python_component = f"cpython-{platform.python_version()}"
    python_copied = _copy_file(
        python_license,
        licenses_root / python_component / "LICENSE.txt",
        relative_to=legal_root,
    )
    python_runtime_files = ",".join(
        [
            "runtime/adt-mcp/_internal/Python",
            "runtime/adt-mcp/_internal/Python.framework",
            "runtime/adt-mcp/_internal/libcrypto.3.dylib",
            "runtime/adt-mcp/_internal/libssl.3.dylib",
            "runtime/adt-mcp/_internal/liblzma.5.dylib",
            "runtime/adt-mcp/_internal/libmpdec.4.dylib",
        ]
    )
    notice_lines.extend(
        [
            f"## CPython {platform.python_version()}",
            "",
            "- License: Python-2.0",
            "- Source: https://www.python.org/downloads/source/",
            f"- Included license file: {python_copied}",
            "- The copied CPython license text includes notices for its bundled runtime libraries.",
            "",
        ]
    )
    components.append(
        {
            "type": "framework",
            "name": "CPython",
            "version": platform.python_version(),
            "purl": f"pkg:generic/cpython@{platform.python_version()}",
            "licenses": [{"license": {"id": "Python-2.0"}}],
            "externalReferences": [
                {"type": "website", "url": "https://www.python.org/downloads/source/"}
            ],
            "properties": [
                {"name": "batchticket:license-files", "value": python_copied},
                {
                    "name": "batchticket:bundled-runtime-files",
                    "value": python_runtime_files,
                },
            ],
        }
    )

    pyinstaller = importlib.metadata.distribution("pyinstaller")
    pyinstaller_licenses = _distribution_license_files(pyinstaller)
    if not pyinstaller_licenses:
        raise ValueError("PyInstaller bootloader license text is unavailable")
    pyinstaller_copied = [
        _copy_file(
            source,
            licenses_root / f"pyinstaller-{pyinstaller.version}" / source.name,
            relative_to=legal_root,
        )
        for source in pyinstaller_licenses
    ]
    pyinstaller_license = pyinstaller.metadata.get("License") or "NOASSERTION"
    notice_lines.extend(
        [
            f"## PyInstaller {pyinstaller.version}",
            "",
            f"- License: {pyinstaller_license}",
            "- Source: https://github.com/pyinstaller/pyinstaller",
            f"- Included license files: {', '.join(pyinstaller_copied)}",
            "- The PyInstaller bootloader is incorporated in runtime/adt-mcp/adt-mcp.",
            "",
        ]
    )
    components.append(
        {
            "type": "application",
            "name": "PyInstaller",
            "version": pyinstaller.version,
            "purl": f"pkg:pypi/pyinstaller@{pyinstaller.version}",
            "licenses": [{"license": {"name": pyinstaller_license}}],
            "externalReferences": [
                {"type": "website", "url": "https://github.com/pyinstaller/pyinstaller"}
            ],
            "properties": [
                {"name": "batchticket:license-files", "value": ",".join(pyinstaller_copied)},
                {
                    "name": "batchticket:bundled-runtime-files",
                    "value": "runtime/adt-mcp/adt-mcp",
                },
            ],
        }
    )

    (legal_root / "THIRD_PARTY_NOTICES.md").write_text(
        "\n".join(notice_lines), encoding="utf-8"
    )
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:00000000-0000-0000-0000-000000000000",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "data-transformer",
                "version": _project_version(),
                "purl": f"pkg:generic/data-transformer@{_project_version()}",
            }
        },
        "components": components,
    }
    (legal_root / "sbom.cdx.json").write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _build_runtime(destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="adt-pyinstaller-") as temporary:
        work = Path(temporary)
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--name",
            "adt-mcp",
            "--paths",
            str(REPO_ROOT / "src"),
            "--distpath",
            str(work / "dist"),
            "--workpath",
            str(work / "work"),
            "--specpath",
            str(work / "spec"),
        ]
        for distribution in _runtime_distributions():
            command.extend(["--copy-metadata", distribution.metadata["Name"]])
        command.append(str(REPO_ROOT / "src" / "data_transformer" / "frozen_entry.py"))
        environment = {
            **os.environ,
            "PYINSTALLER_CONFIG_DIR": str(work / "config"),
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(ARCHIVE_EPOCH),
        }
        subprocess.run(command, cwd=REPO_ROOT, env=environment, check=True)
        shutil.copytree(work / "dist" / "adt-mcp", destination)


def _assemble_bundle(destination: Path, version: str) -> None:
    plugin_manifest = json.loads(
        (REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    if plugin_manifest.get("version") != version:
        raise ValueError("plugin.json version does not match pyproject.toml")
    shutil.copytree(REPO_ROOT / ".codex-plugin", destination / ".codex-plugin")
    shutil.copytree(REPO_ROOT / "examples", destination / "examples")
    shutil.copytree(REPO_ROOT / "skills", destination / "skills")
    shutil.copy2(REPO_ROOT / "packaging" / "plugin.mcp.json", destination / ".mcp.json")
    shutil.copy2(REPO_ROOT / "README.md", destination / "README.md")
    shutil.copy2(REPO_ROOT / "LICENSE", destination / "LICENSE")
    shutil.copy2(REPO_ROOT / "NOTICE", destination / "NOTICE")
    _build_runtime(destination / "runtime" / "adt-mcp")
    _write_legal_material(destination)

    manifest = {
        "name": "data-transformer",
        "version": version,
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "entrypoint": "runtime/adt-mcp/adt-mcp",
        "transport": "stdio",
    }
    (destination / "bundle.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _archive_bundle(bundle: Path, *, replace: bool) -> tuple[Path, Path]:
    archive = bundle.parent / f"{bundle.name}.tar.gz"
    checksum = Path(f"{archive}.sha256")
    for generated in (archive, checksum):
        _validate_generated_target(generated, directory=False)
        if _lexists(generated):
            if not replace:
                raise FileExistsError(f"output already exists: {generated}")
            generated.unlink()

    with (
        archive.open("wb") as raw_archive,
        gzip.GzipFile(fileobj=raw_archive, mode="wb", mtime=ARCHIVE_EPOCH) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as target,
    ):
        for source in sorted(bundle.rglob("*")):
            if source.is_symlink():
                raise ValueError(f"bundle contains a symbolic link: {source}")
            info = target.gettarinfo(
                source,
                arcname=(Path(bundle.name) / source.relative_to(bundle)).as_posix(),
            )
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = ARCHIVE_EPOCH
            if source.is_file():
                with source.open("rb") as contents:
                    target.addfile(info, contents)
            else:
                target.addfile(info)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive, checksum


def _verify_archive(bundle: Path, archive: Path) -> None:
    root = bundle.name
    required = {
        f"{root}/LICENSE",
        f"{root}/NOTICE",
        f"{root}/legal/THIRD_PARTY_NOTICES.md",
        f"{root}/legal/sbom.cdx.json",
    }
    with tarfile.open(archive, mode="r:gz") as contents:
        members = contents.getmembers()
    names = {member.name for member in members}
    missing = sorted(required - names)
    if missing:
        raise ValueError(f"archive is missing required legal material: {', '.join(missing)}")
    if not any(name.startswith(f"{root}/legal/licenses/") for name in names):
        raise ValueError("archive is missing copied third-party license texts")
    if any(member.issym() or member.islnk() for member in members):
        raise ValueError("archive contains a symbolic link")


def _marketplace_path(output_root: Path) -> Path:
    return output_root / ".agents" / "plugins" / "marketplace.json"


def _is_owned_marketplace(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get("name") != "data-transformer-local":
        return False
    plugins = payload.get("plugins")
    if not isinstance(plugins, list) or len(plugins) != 1:
        return False
    plugin = plugins[0]
    source = plugin.get("source") if isinstance(plugin, dict) else None
    return (
        isinstance(plugin, dict)
        and plugin.get("name") == "data-transformer"
        and isinstance(source, dict)
        and source.get("source") == "local"
    )


def _preflight_marketplace(output_root: Path, *, replace: bool) -> Path:
    marketplace = _marketplace_path(output_root)
    _validate_generated_target(marketplace, directory=False)
    if not _lexists(marketplace):
        return marketplace
    if not replace:
        raise FileExistsError(f"output already exists: {marketplace}")
    try:
        existing = json.loads(marketplace.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"refusing to replace an unreadable marketplace: {marketplace}"
        ) from exc
    if not _is_owned_marketplace(existing):
        raise ValueError(
            f"refusing to replace a marketplace not owned by this build: {marketplace}"
        )
    return marketplace


def _marketplace_payload(bundle_name: str) -> dict[str, object]:
    return {
        "name": "data-transformer-local",
        "interface": {"displayName": "BatchTicket Local"},
        "plugins": [
            {
                "name": "data-transformer",
                "source": {
                    "source": "local",
                    "path": f"./{bundle_name}",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }


def _write_marketplace(output_root: Path, bundle: Path, *, replace: bool = False) -> Path:
    marketplace = _preflight_marketplace(output_root, replace=replace)
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(marketplace.parent)
    payload = _marketplace_payload(bundle.name)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=marketplace.parent,
        prefix=".marketplace.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as target:
            target.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        _validate_generated_target(marketplace, directory=False)
        os.replace(temporary, marketplace)
    finally:
        if _lexists(temporary):
            temporary.unlink()
    return marketplace


def _remove_generated_output(path: Path) -> None:
    if not _lexists(path):
        return
    _validate_generated_target(path, directory=path.is_dir())
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _publish_generated_outputs(
    generated: list[tuple[Path, Path]],
    backup_root: Path,
    *,
    replace: bool,
) -> None:
    for source, target in generated:
        _validate_generated_target(source, directory=source.is_dir())
        _validate_generated_target(target, directory=source.is_dir())
        if _lexists(target) and not replace:
            raise FileExistsError(f"output already exists: {target}")

    backup_root.mkdir()
    backups: list[tuple[Path, Path]] = []
    published: list[Path] = []
    try:
        for index, (_, target) in enumerate(generated):
            if _lexists(target):
                backup = backup_root / f"{index}-{target.name}"
                os.replace(target, backup)
                backups.append((target, backup))
        for source, target in generated:
            target.parent.mkdir(parents=True, exist_ok=True)
            _assert_no_symlink_components(target.parent)
            os.replace(source, target)
            published.append(target)
    except BaseException:
        for target in reversed(published):
            _remove_generated_output(target)
        for target, backup in reversed(backups):
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, target)
        raise


def build(output_root: Path, *, replace: bool) -> dict[str, str]:
    version = _project_version()
    output_root = _absolute_without_resolving(output_root)
    _assert_no_symlink_components(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(output_root)
    destination = output_root / _bundle_name(version)
    archive = output_root / f"{destination.name}.tar.gz"
    checksum = Path(f"{archive}.sha256")

    if destination.parent != output_root:
        raise ValueError("generated bundle escaped the output root")
    _validate_generated_target(destination, directory=True)
    _validate_generated_target(archive, directory=False)
    _validate_generated_target(checksum, directory=False)
    _preflight_marketplace(output_root, replace=replace)

    existing = [path for path in (destination, archive, checksum) if _lexists(path)]
    if existing and not replace:
        raise FileExistsError(f"output already exists: {existing[0]}")

    stage_parent = Path(tempfile.mkdtemp(prefix=".adt-plugin-stage-", dir=output_root))
    stage = stage_parent / destination.name
    try:
        stage.mkdir()
        _assemble_bundle(stage, version)
        staged_archive, staged_checksum = _archive_bundle(stage, replace=False)
        _verify_archive(stage, staged_archive)
        staged_marketplace = stage_parent / "marketplace.json"
        staged_marketplace.write_text(
            json.dumps(
                _marketplace_payload(destination.name), indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
        marketplace = _preflight_marketplace(output_root, replace=replace)
        _publish_generated_outputs(
            [
                (stage, destination),
                (staged_archive, archive),
                (staged_checksum, checksum),
                (staged_marketplace, marketplace),
            ],
            stage_parent / "previous",
            replace=replace,
        )
    finally:
        _validate_generated_target(stage_parent, directory=True)
        shutil.rmtree(stage_parent)

    return {
        "bundle": str(destination),
        "archive": str(archive),
        "checksum": str(checksum),
        "marketplace": str(marketplace),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a self-contained BatchTicket plugin")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / "dist" / "plugin",
        help="directory for the platform-specific plugin bundle",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing bundle for the same version and platform",
    )
    args = parser.parse_args()
    print(json.dumps(build(args.output_root, replace=args.replace), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
