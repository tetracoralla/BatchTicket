from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


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
            str(REPO_ROOT / "src" / "data_transformer" / "frozen_entry.py"),
        ]
        environment = {
            **os.environ,
            "PYINSTALLER_CONFIG_DIR": str(work / "config"),
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

    with tarfile.open(archive, mode="w:gz") as target:
        target.add(bundle, arcname=bundle.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive, checksum


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
        "interface": {"displayName": "Data Transformer Local"},
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
    parser = argparse.ArgumentParser(description="Build a self-contained Data Transformer plugin")
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
