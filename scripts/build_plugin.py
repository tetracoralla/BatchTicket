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
        if generated.exists():
            if not replace:
                raise FileExistsError(f"output already exists: {generated}")
            generated.unlink()

    with tarfile.open(archive, mode="w:gz") as target:
        target.add(bundle, arcname=bundle.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive, checksum


def build(output_root: Path, *, replace: bool) -> dict[str, str]:
    version = _project_version()
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / _bundle_name(version)
    archive = output_root / f"{destination.name}.tar.gz"
    checksum = Path(f"{archive}.sha256")

    existing = [path for path in (destination, archive, checksum) if path.exists()]
    if existing and not replace:
        raise FileExistsError(f"output already exists: {existing[0]}")

    stage_parent = Path(tempfile.mkdtemp(prefix=".adt-plugin-stage-", dir=output_root))
    stage = stage_parent / destination.name
    try:
        stage.mkdir()
        _assemble_bundle(stage, version)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(stage, destination)
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)

    archive, checksum = _archive_bundle(destination, replace=replace)
    return {
        "bundle": str(destination),
        "archive": str(archive),
        "checksum": str(checksum),
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
