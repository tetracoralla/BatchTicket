from __future__ import annotations

import json
import re
import runpy
import tomllib
from pathlib import Path

from data_transformer import __version__


def test_all_public_version_surfaces_match() -> None:
    with Path("pyproject.toml").open("rb") as source:
        project = tomllib.load(source)["project"]
    plugin = json.loads(Path(".codex-plugin/plugin.json").read_text(encoding="utf-8"))

    assert __version__ == project["version"] == plugin["version"]
    assert {author["name"] for author in project["authors"]} == {"openAdam"}
    assert plugin["author"]["name"] == "openAdam"
    assert plugin["interface"]["developerName"] == "openAdam"


def test_binary_release_materials_and_authorization_boundary_are_explicit() -> None:
    checklist = Path("docs/RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

    assert "legal/THIRD_PARTY_NOTICES.md" in checklist
    assert "CycloneDX 1.5 SBOM" in checklist
    assert "This does not authorize publication" in checklist


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


def test_release_hygiene_rejects_public_agent_session_records(tmp_path: Path) -> None:
    hygiene = runpy.run_path("scripts/check_release_hygiene.py", run_name="release_hygiene")
    is_trace_path = hygiene["_is_public_agent_trace_path"]

    assert is_trace_path("docs/ROUTING_EVAL.md")
    assert is_trace_path("docs/progress/2026-08-20.md")
    assert not is_trace_path("docs/REVIEW_CONTRACT.md")

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "report.md").write_text("Host token evidence: 75,000 input tokens\n")
    check_traces = hygiene["_check_public_agent_traces"]
    check_traces.__globals__["REPO_ROOT"] = tmp_path

    assert check_traces() == [
        "host token evidence must not be tracked in public docs: docs/report.md"
    ]
