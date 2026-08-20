from __future__ import annotations

import email.parser
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_ROOT = REPO_ROOT / "dist"


def _version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def _only(paths: list[Path], label: str) -> Path:
    if len(paths) != 1:
        rendered = ", ".join(path.name for path in paths) or "none"
        raise ValueError(f"expected one {label}, found: {rendered}")
    return paths[0]


def _is_forbidden_member(name: str) -> bool:
    path = PurePosixPath(name)
    parts = set(path.parts)
    filename = path.name
    return bool(
        parts & {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "dist"}
        or filename == ".DS_Store"
        or filename == "probe_users.json"
        or filename == ".coverage"
        or filename.startswith(".coverage.")
        or filename == ".env"
        or filename.startswith(".env.")
        or filename.endswith((".pem", ".key"))
    )


def _check_wheel(wheel: Path, license_text: bytes) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        forbidden = sorted(name for name in names if _is_forbidden_member(name))
        if forbidden:
            raise ValueError(f"wheel contains forbidden paths: {', '.join(forbidden)}")
        bundled_binaries = sorted(
            name
            for name in names
            if PurePosixPath(name).suffix.lower() in {".dll", ".dylib", ".pyd", ".so"}
        )
        if bundled_binaries:
            raise ValueError(
                "source-release wheel unexpectedly contains binaries: "
                + ", ".join(bundled_binaries)
            )
        metadata_name = _only(
            [Path(name) for name in names if name.endswith(".dist-info/METADATA")],
            "wheel METADATA",
        ).as_posix()
        metadata = email.parser.Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
        if metadata.get("License-Expression") != "Apache-2.0":
            raise ValueError("wheel metadata is missing License-Expression: Apache-2.0")
        if set(metadata.get_all("License-File", [])) != {"LICENSE", "NOTICE"}:
            raise ValueError("wheel metadata must list LICENSE and NOTICE")
        license_name = _only(
            [Path(name) for name in names if name.endswith(".dist-info/licenses/LICENSE")],
            "wheel LICENSE",
        ).as_posix()
        if archive.read(license_name) != license_text:
            raise ValueError("wheel LICENSE differs from repository LICENSE")
        schema_suffix = "data_transformer/schemas/transformation-plan.schema.json"
        if not any(name.endswith(schema_suffix) for name in names):
            raise ValueError("wheel does not contain the published Transformation Plan schema")


def _check_sdist(sdist: Path, version: str, license_text: bytes) -> None:
    expected_root = f"agent_data_transformer-{version}"
    with tarfile.open(sdist, mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"sdist contains an escaping path: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"sdist contains a link: {member.name}")
        forbidden = sorted(name for name in names if _is_forbidden_member(name))
        if forbidden:
            raise ValueError(f"sdist contains forbidden paths: {', '.join(forbidden)}")
        required = {
            f"{expected_root}/CHANGELOG.md",
            f"{expected_root}/CONTRIBUTING.md",
            f"{expected_root}/LICENSE",
            f"{expected_root}/NOTICE",
            f"{expected_root}/README.md",
            f"{expected_root}/SECURITY.md",
            f"{expected_root}/schemas/transformation-plan.schema.json",
        }
        missing = sorted(required - set(names))
        if missing:
            raise ValueError(f"sdist is missing public files: {', '.join(missing)}")
        license_member = archive.extractfile(f"{expected_root}/LICENSE")
        if license_member is None or license_member.read() != license_text:
            raise ValueError("sdist LICENSE differs from repository LICENSE")


def main() -> None:
    version = _version()
    wheel = _only(sorted(DIST_ROOT.glob(f"agent_data_transformer-{version}-*.whl")), "wheel")
    sdist = _only(sorted(DIST_ROOT.glob(f"agent_data_transformer-{version}.tar.gz")), "sdist")
    license_text = (REPO_ROOT / "LICENSE").read_bytes()
    _check_wheel(wheel, license_text)
    _check_sdist(sdist, version, license_text)
    print(f"release artifacts: PASS ({wheel.name}, {sdist.name})")


if __name__ == "__main__":
    main()
