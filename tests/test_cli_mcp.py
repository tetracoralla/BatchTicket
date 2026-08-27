from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.context import RequestContext

from data_transformer.mcp_server import mcp


def test_cli_transform_real_entrypoint() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "data_transformer.cli", "transform", "examples/adults.plan.yaml"],
        check=False,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert result["status"] == "ok"
    assert result["summary"]["rows_out"] == 2
    assert "receipt" not in result
    assert "execution_effects" in result


def test_mcp_registry_exposes_only_four_task_level_tools() -> None:
    assert mcp.name == "BatchTicket"
    tools = mcp._tool_manager.list_tools()
    names = {tool.name for tool in tools}

    assert names == {"data_inspect", "data_transform", "data_validate", "data_diff"}
    for tool in tools:
        assert "ctx" not in tool.parameters["properties"]
        assert "workspace" in tool.parameters["properties"]
        # Codex rejects a structured-result server that advertises no output
        # contract.  The real adapter returns CallToolResult, so this also
        # ensures FastMCP validates each structuredContent object at runtime.
        assert tool.output_schema is not None
        assert tool.output_schema["type"] == "object"
        assert len(tool.output_schema["anyOf"]) == 2
    inspect = next(tool for tool in tools if tool.name == "data_inspect")
    assert "target_schema" in inspect.parameters["properties"]
    assert "mappings" in inspect.parameters["properties"]
    transform = next(tool for tool in tools if tool.name == "data_transform")
    assert '"sources":{"input"' in transform.description
    assert '"field":"userId","as":"id"' in transform.description
    assert "never a path" in transform.description
    assert "Do not use this tool for a validation-only request" in transform.description
    validate = next(tool for tool in tools if tool.name == "data_validate")
    assert "choose data_validate, not data_transform" in validate.description
    assert '"type":"not_null","field":"userId"' in validate.description
    assert '"type":"unique","field":"userId"' in validate.description


def test_mcp_stdio_activation_and_real_tool_call() -> None:
    async def exercise() -> None:
        async def list_roots(
            context: RequestContext[ClientSession, object],
        ) -> types.ListRootsResult:
            del context
            return types.ListRootsResult(
                roots=[types.Root(uri=Path.cwd().resolve().as_uri(), name="repository")]
            )

        config = json.loads(Path(".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
            "data-transformer"
        ]
        environment = {
            **os.environ,
            "UV_CACHE_DIR": "/tmp/data-transformer-test-uv-cache",
        }
        environment.pop("ADT_WORKSPACE_ROOT", None)
        server = StdioServerParameters(
            command=config["command"],
            args=config["args"],
            cwd=Path.cwd(),
            env=environment,
        )
        async with (
            stdio_client(server) as (read_stream, write_stream),
            ClientSession(
                read_stream,
                write_stream,
                list_roots_callback=list_roots,
            ) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "data_inspect",
                "data_transform",
                "data_validate",
                "data_diff",
            }
            for tool in tools.tools:
                assert tool.outputSchema is not None
                assert tool.outputSchema["type"] == "object"
                assert len(tool.outputSchema["anyOf"]) == 2
            call = await session.call_tool(
                "data_inspect",
                {"source": {"inline": [{"id": 1}, {"id": 2}]}, "sample_rows": 1},
            )
            assert call.isError is not True
            assert call.structuredContent["status"] == "ok"
            assert call.structuredContent["shape"]["rows"] == 2

            file_call = await session.call_tool(
                "data_inspect",
                {"source": {"path": "examples/users.json", "select": "data.users[*]"}},
            )
            assert file_call.isError is not True
            assert file_call.structuredContent["status"] == "ok"
            assert file_call.structuredContent["shape"]["rows"] == 3
            assert "rows=3" in file_call.content[0].text
            assert "fields=" in file_call.content[0].text

            envelope_call = await session.call_tool(
                "data_inspect",
                {"source": {"path": "examples/users.json"}, "sample_rows": 2},
            )
            assert "record_set=data.users[*]" in envelope_call.content[0].text
            assert "profile.name" in envelope_call.content[0].text

            adapted = await session.call_tool(
                "data_inspect",
                {
                    "source": {"inline": [{"source_id": 1}]},
                    "target_schema": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}},
                        "required": ["id"],
                    },
                    "mappings": {"id": "source_id"},
                },
            )
            assert adapted.structuredContent["adaptation"]["status"] == "ready"
            assert adapted.structuredContent["adaptation"]["draft_plan"]["steps"][0][
                "fields"
            ] == [{"field": "source_id", "as": "id"}]

            transformed = await session.call_tool(
                "data_transform",
                {
                    "plan": {
                        "version": "1",
                        "sources": {"rows": {"inline": [{"id": 1, "keep": True}]}},
                        "steps": [
                            {"op": "filter", "where": {"field": "keep", "eq": True}},
                            {"op": "select", "fields": ["id"]},
                        ],
                    }
                },
            )
            assert transformed.structuredContent["status"] == "ok"
            assert transformed.structuredContent["result"]["data"] == [{"id": 1}]
            assert transformed.content[0].text == (
                'transform: ok; rows_out=1; data=[{"id":1}]'
            )

            invalid = await session.call_tool(
                "data_transform",
                {
                    "plan": {
                        "version": "1",
                        "sources": {"rows": {"inline": [{"id": 1}]}},
                        "steps": [
                            {
                                "op": "derive",
                                "field": "bad",
                                "expr": {"python": "__import__('os')"},
                            }
                        ],
                    }
                },
            )
            assert invalid.isError is True
            assert invalid.structuredContent is None

    asyncio.run(exercise())


def test_mcp_file_calls_require_an_explicit_grant_but_inline_does_not() -> None:
    async def exercise() -> None:
        config = json.loads(Path(".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
            "data-transformer"
        ]
        environment = {
            **os.environ,
            "UV_CACHE_DIR": "/tmp/data-transformer-test-uv-cache",
        }
        environment.pop("ADT_WORKSPACE_ROOT", None)
        server = StdioServerParameters(
            command=config["command"],
            args=config["args"],
            cwd=Path.cwd(),
            env=environment,
        )
        async with (
            stdio_client(server) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            inline = await session.call_tool(
                "data_inspect",
                {"source": {"inline": [{"id": 1}]}},
            )
            assert inline.structuredContent["status"] == "ok"

            denied = await session.call_tool(
                "data_inspect",
                {"source": {"path": "examples/users.json"}},
            )
            assert denied.isError is not True
            assert denied.structuredContent == {
                "status": "error",
                "operation": "inspect",
                "error": {
                    "code": "E_WORKSPACE_REQUIRED",
                    "message": "file resources require an MCP root or ADT_WORKSPACE_ROOT grant",
                },
            }

    asyncio.run(exercise())


def test_mcp_multiple_roots_require_explicit_named_selection(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "input.json").write_text('[{"id":1}]', encoding="utf-8")
    (second / "input.json").write_text('[{"id":2}]', encoding="utf-8")

    async def exercise() -> None:
        async def list_roots(
            context: RequestContext[ClientSession, object],
        ) -> types.ListRootsResult:
            del context
            return types.ListRootsResult(
                roots=[
                    types.Root(uri=first.resolve().as_uri(), name="first"),
                    types.Root(uri=second.resolve().as_uri(), name="second"),
                ]
            )

        config = json.loads(Path(".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
            "data-transformer"
        ]
        environment = {
            **os.environ,
            "UV_CACHE_DIR": "/tmp/data-transformer-test-uv-cache",
        }
        environment.pop("ADT_WORKSPACE_ROOT", None)
        server = StdioServerParameters(
            command=config["command"],
            args=config["args"],
            cwd=Path.cwd(),
            env=environment,
        )
        async with (
            stdio_client(server) as (read_stream, write_stream),
            ClientSession(
                read_stream,
                write_stream,
                list_roots_callback=list_roots,
            ) as session,
        ):
            await session.initialize()
            ambiguous = await session.call_tool(
                "data_inspect",
                {"source": {"path": "input.json"}},
            )
            assert ambiguous.structuredContent["error"] == {
                "code": "E_WORKSPACE_AMBIGUOUS",
                "message": "multiple MCP workspaces are granted; choose one by name",
                "details": {"available": ["first", "second"]},
            }

            selected = await session.call_tool(
                "data_inspect",
                {"source": {"path": "input.json"}, "workspace": "second"},
            )
            assert selected.structuredContent["sample"] == [{"id": 2}]

            denied = await session.call_tool(
                "data_inspect",
                {"source": {"path": "input.json"}, "workspace": "missing"},
            )
            assert denied.structuredContent["error"]["code"] == "E_WORKSPACE_NOT_GRANTED"

    asyncio.run(exercise())


def test_mcp_environment_grant_is_a_legacy_fallback_for_clients_without_roots(tmp_path) -> None:
    (tmp_path / "input.json").write_text('[{"id":7}]', encoding="utf-8")

    async def exercise() -> None:
        config = json.loads(Path(".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
            "data-transformer"
        ]
        server = StdioServerParameters(
            command=config["command"],
            args=config["args"],
            cwd=Path.cwd(),
            env={
                **os.environ,
                "UV_CACHE_DIR": "/tmp/data-transformer-test-uv-cache",
                "ADT_WORKSPACE_ROOT": str(tmp_path),
            },
        )
        async with (
            stdio_client(server) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "data_inspect",
                {"source": {"path": "input.json"}},
            )
            assert result.structuredContent["status"] == "ok"
            assert result.structuredContent["sample"] == [{"id": 7}]

    asyncio.run(exercise())


def test_release_plugin_config_uses_the_bundled_runtime() -> None:
    config = json.loads(Path("packaging/plugin.mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["data-transformer"]

    assert config["command"] == "./runtime/adt-mcp/adt-mcp"
    assert config["args"] == []
    assert config["cwd"] == "."
    assert "uv" not in json.dumps(config)


def test_mcp_stdio_shutdown_emits_no_closed_stream_traceback(tmp_path) -> None:
    async def exercise() -> None:
        config = json.loads(Path(".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
            "data-transformer"
        ]
        environment = {
            **os.environ,
            "UV_CACHE_DIR": "/tmp/data-transformer-test-uv-cache",
        }
        environment.pop("ADT_WORKSPACE_ROOT", None)
        server = StdioServerParameters(
            command=config["command"],
            args=config["args"],
            cwd=Path.cwd(),
            env=environment,
        )
        log_path = tmp_path / "server.log"
        with log_path.open("w+", encoding="utf-8") as server_log:
            async with (
                stdio_client(server, errlog=server_log) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                result = await session.call_tool(
                    "data_inspect",
                    {"source": {"inline": [{"id": 1}]}},
                )
                assert result.structuredContent["status"] == "ok"
            server_log.seek(0)
            log_output = server_log.read()
            assert "Traceback" not in log_output
            assert "I/O operation on closed file" not in log_output

    asyncio.run(exercise())


def test_cli_missing_plan_returns_stable_json_error(tmp_path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "data_transformer.cli", "transform", str(tmp_path / "missing.json")],
        check=False,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert result["error"]["code"] == "E_CLI_INPUT"
    assert "Traceback" not in completed.stderr


def test_cli_inspect_builds_schema_adapter_draft(tmp_path) -> None:
    target = tmp_path / "target.json"
    mappings = tmp_path / "mappings.json"
    target.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "age": {"type": "integer"},
                    "id": {"type": "integer"},
                },
                "required": ["age", "id"],
            }
        ),
        encoding="utf-8",
    )
    mappings.write_text(json.dumps({"age": "age", "id": "userId"}), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "data_transformer.cli",
            "--compact",
            "inspect",
            "examples/users.json",
            "--select",
            "data.users[*]",
            "--target-schema",
            str(target),
            "--mappings",
            str(mappings),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert result["adaptation"]["status"] == "ready"
    assert result["adaptation"]["draft_plan"]["steps"][0]["fields"] == [
        {"as": "age", "field": "age"},
        {"as": "id", "field": "userId"},
    ]


def test_cli_schema_documents_share_the_worker_depth_limit(tmp_path) -> None:
    source = tmp_path / "source.json"
    source.write_text('[{"id":1}]', encoding="utf-8")
    nested: dict[str, object] = {"type": "integer"}
    for index in range(110):
        nested = {"$defs": {f"level_{index}": nested}, "type": "integer"}
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"id": nested},
                "required": ["id"],
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "data_transformer.cli",
            "--compact",
            "inspect",
            str(source),
            "--target-schema",
            str(schema),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert result["status"] == "error"
    assert result["error"]["code"] == "E_DEPTH_LIMIT"
