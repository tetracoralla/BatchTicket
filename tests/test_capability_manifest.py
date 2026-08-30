from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from data_transformer.mcp_server import mcp


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_capability_manifest_digests_match_contract_and_live_mcp() -> None:
    root = Path.cwd()
    manifest = _load(root / "capabilities" / "provider.json")
    bindings = {
        binding["operationId"]: binding
        for binding in manifest["implementations"][0]["bindings"]
    }
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    expected = {
        "inspect": {
            "contract.input": _digest(
                _load(
                    root
                    / "capabilities"
                    / "schemas"
                    / "structured-data.inspect.input.schema.json"
                )
            ),
            "contract.output": _digest(
                _load(
                    root
                    / "capabilities"
                    / "schemas"
                    / "structured-data.inspect.output.schema.json"
                )
            ),
            "transport.input": _digest(tools["data_inspect"].parameters),
        },
        "validate": {
            "contract.input": _digest(
                _load(
                    root
                    / "capabilities"
                    / "schemas"
                    / "structured-data.validate.input.schema.json"
                )
            ),
            "contract.output": _digest(
                _load(
                    root
                    / "capabilities"
                    / "schemas"
                    / "structured-data.validate.output.schema.json"
                )
            ),
            "transport.input": _digest(tools["data_validate"].parameters),
        },
    }
    for operation, values in expected.items():
        binding = bindings[operation]
        assert binding["contractSchemaDigests"]["input"] == values["contract.input"]
        assert binding["contractSchemaDigests"]["output"] == values["contract.output"]
        assert binding["transportSchemaDigests"]["input"] == values["transport.input"]
