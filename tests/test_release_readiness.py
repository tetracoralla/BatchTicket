from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from data_transformer import __version__


def test_all_public_version_surfaces_match() -> None:
    with Path("pyproject.toml").open("rb") as source:
        project_version = tomllib.load(source)["project"]["version"]
    plugin_version = json.loads(Path(".codex-plugin/plugin.json").read_text(encoding="utf-8"))[
        "version"
    ]

    assert __version__ == project_version == plugin_version


def test_binary_release_remains_explicitly_blocked_until_notices_are_complete() -> None:
    checklist = Path("docs/RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

    assert "Current status: NO-GO for public upload" in checklist
    assert "complete third-party license and notice collection" in checklist


def test_remote_github_actions_are_pinned_to_full_commit_shas() -> None:
    workflows = Path(".github/workflows").glob("*.yml")

    for workflow in workflows:
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = re.search(r"\buses:\s*([^@\s]+)@([^\s#]+)", line)
            if match is None or match.group(1).startswith("./"):
                continue
            assert re.fullmatch(r"[0-9a-f]{40}", match.group(2)), (
                f"{workflow}:{line_number} is not pinned"
            )
