from __future__ import annotations

import json
import sys
from typing import Any

from .mcp_server import mcp

MAX_REQUEST_BYTES = 1024 * 1024


def _strict_json(source: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = item
        return value

    return json.loads(source, object_pairs_hook=reject_duplicates)


def main() -> None:
    source = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 2)
    if len(source) > MAX_REQUEST_BYTES or not source.endswith(b"\n"):
        raise SystemExit(2)
    try:
        request = _strict_json(source.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise SystemExit(2) from None
    if not isinstance(request, dict) or set(request) != {
        "id",
        "capabilityId",
        "capabilityVersion",
    }:
        raise SystemExit(2)
    if (
        request["id"] != "transport-schema"
        or request["capabilityId"] != "org.openadam.structured-data.analyze"
        or request["capabilityVersion"] != "0.1.0"
    ):
        raise SystemExit(2)
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    response = {
        "id": request["id"],
        "ok": True,
        "bindings": [
            {
                "operationId": "inspect",
                "transport": "mcp-tool",
                "target": "data_inspect",
                "inputSchema": tools["data_inspect"].parameters,
            },
            {
                "operationId": "validate",
                "transport": "mcp-tool",
                "target": "data_validate",
                "inputSchema": tools["data_validate"].parameters,
            },
        ],
    }
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
