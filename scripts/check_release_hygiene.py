from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APACHE_2_LICENSE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
CANONICAL_REPOSITORY = "https://github.com/tetracoralla/BatchTicket"
REQUIRED_PUBLIC_FILES = {
    ".github/dependabot.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".gitattributes",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "docs/RELEASE_CHECKLIST.md",
}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b"),
}


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _is_forbidden_release_path(raw_path: str) -> bool:
    path = Path(raw_path)
    parts = set(path.parts)
    if parts & {
        ".batchticket-plugin-build",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "dist",
        "build",
    }:
        return True
    name = path.name
    return (
        name == ".DS_Store"
        or name == "probe_users.json"
        or name == ".env"
        or name.startswith(".env.")
        or name == ".coverage"
        or name.startswith(".coverage.")
        or name.endswith((".pem", ".key"))
    )


def _release_refs() -> list[str]:
    if not (REPO_ROOT / ".git").exists():
        return []
    return [
        ref
        for ref in _git(
            "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags"
        ).splitlines()
        if ref
    ]


def _check_paths() -> list[str]:
    failures: list[str] = []
    missing = sorted(path for path in REQUIRED_PUBLIC_FILES if not (REPO_ROOT / path).is_file())
    if missing:
        failures.append(f"required public files are missing: {', '.join(missing)}")

    if not (REPO_ROOT / ".git").exists():
        return failures
    tracked = [path for path in _git("ls-files").splitlines() if path]
    forbidden = sorted(path for path in tracked if _is_forbidden_release_path(path))
    if forbidden:
        failures.append(f"forbidden paths are tracked: {', '.join(forbidden)}")

    refs = _release_refs()
    if refs:
        history_paths = [
            path
            for path in _git("log", "--format=", "--name-only", *refs, "--").splitlines()
            if path
        ]
        forbidden_history = sorted(
            {path for path in history_paths if _is_forbidden_release_path(path)}
        )
        if forbidden_history:
            failures.append(
                "forbidden paths occur in branch/tag history: " + ", ".join(forbidden_history)
            )
    return failures


def _check_metadata() -> list[str]:
    failures: list[str] = []
    license_digest = hashlib.sha256((REPO_ROOT / "LICENSE").read_bytes()).hexdigest()
    if license_digest != APACHE_2_LICENSE_SHA256:
        failures.append("LICENSE is not the exact Apache License 2.0 reference text")

    with (REPO_ROOT / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source)["project"]
    if project.get("license") != "Apache-2.0":
        failures.append("project license expression must be Apache-2.0")
    if set(project.get("license-files", [])) != {"LICENSE", "NOTICE"}:
        failures.append("project license-files must contain LICENSE and NOTICE")
    urls = project.get("urls", {})
    if urls.get("Repository") != CANONICAL_REPOSITORY:
        failures.append("project Repository URL does not match the planned public repository")

    version_source = (REPO_ROOT / "src/data_transformer/__init__.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^__version__ = "([^"]+)"$', version_source, flags=re.MULTILINE)
    if match is None or match.group(1) != project.get("version"):
        failures.append("package __version__ does not match pyproject.toml")

    plugin = (REPO_ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
    if f'"version": "{project.get("version")}"' not in plugin:
        failures.append("plugin version does not match pyproject.toml")
    if '"displayName": "BatchTicket"' not in plugin:
        failures.append("plugin display name is not BatchTicket")
    return failures


def _check_workflows() -> list[str]:
    failures: list[str] = []
    workflows = sorted((REPO_ROOT / ".github/workflows").glob("*.yml"))
    for workflow in workflows:
        content = workflow.read_text(encoding="utf-8")
        relative = workflow.relative_to(REPO_ROOT).as_posix()
        if "pull_request_target:" in content:
            failures.append(f"{relative} must not use pull_request_target")
        if "permissions:" not in content:
            failures.append(f"{relative} must declare explicit permissions")
        for line_number, line in enumerate(content.splitlines(), start=1):
            match = re.search(r"\buses:\s*([^@\s]+)@([^\s#]+)", line)
            if match is None or match.group(1).startswith("./"):
                continue
            if re.fullmatch(r"[0-9a-f]{40}", match.group(2)) is None:
                failures.append(
                    f"{relative}:{line_number} action is not pinned to a full commit SHA"
                )
    return failures


def _check_current_secrets() -> list[str]:
    failures: list[str] = []
    candidate_paths = set(REQUIRED_PUBLIC_FILES)
    if (REPO_ROOT / ".git").exists():
        candidate_paths.update(_git("ls-files").splitlines())
        candidate_paths.update(
            _git("ls-files", "--others", "--exclude-standard").splitlines()
        )
    for raw_path in sorted(candidate_paths):
        path = REPO_ROOT / raw_path
        if not path.is_file() or _is_forbidden_release_path(raw_path):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                failures.append(f"possible {label} in {raw_path}")
    return failures


def _check_release_history_secrets() -> list[str]:
    if not (REPO_ROOT / ".git").exists():
        return []
    failures: list[str] = []
    refs = _release_refs()
    if not refs:
        return failures
    commits = _git("rev-list", *refs).splitlines()
    seen_blobs: set[str] = set()
    for commit in commits:
        paths = _git("ls-tree", "-r", "--name-only", commit).splitlines()
        for raw_path in paths:
            object_name = f"{commit}:{raw_path}"
            blob = _git("rev-parse", object_name).strip()
            if blob in seen_blobs:
                continue
            seen_blobs.add(blob)
            size = int(_git("cat-file", "-s", blob).strip())
            if size > 1_000_000:
                continue
            completed = subprocess.run(
                ["git", "cat-file", "blob", blob],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
            )
            try:
                content = completed.stdout.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(content):
                    failures.append(
                        f"possible {label} in branch/tag history at {commit[:12]}:{raw_path}"
                    )
    return failures


def main() -> None:
    failures = (
        _check_paths()
        + _check_metadata()
        + _check_workflows()
        + _check_current_secrets()
        + _check_release_history_secrets()
    )
    if failures:
        raise SystemExit("release hygiene failed:\n- " + "\n- ".join(failures))
    print("release hygiene: PASS")


if __name__ == "__main__":
    main()
