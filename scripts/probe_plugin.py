from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.context import RequestContext

EXPECTED_TOOLS = {"data_inspect", "data_transform", "data_validate", "data_diff"}


async def _probe(bundle: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="adt-plugin-probe-") as temporary:
        harness = Path(temporary).resolve()
        copied_bundle = harness / "copied-plugin"
        shutil.copytree(bundle, copied_bundle)
        bundle = copied_bundle
        required_legal_files = (
            "LICENSE",
            "NOTICE",
            "legal/THIRD_PARTY_NOTICES.md",
            "legal/sbom.cdx.json",
        )
        for required_file in required_legal_files:
            if not (bundle / required_file).is_file():
                raise FileNotFoundError(f"plugin legal file is missing: {required_file}")

        config = json.loads((bundle / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
            "data-transformer"
        ]
        command = (bundle / config["command"]).resolve()
        if not command.is_file():
            raise FileNotFoundError(f"plugin entrypoint is missing: {command}")
        capability_manifest = json.loads(
            (bundle / "capabilities" / "provider.json").read_text(encoding="utf-8")
        )
        implementation = capability_manifest["implementations"][0]
        if implementation["adapter"] != {
            "protocol": "openadam.capability-jsonl.v0.1",
            "command": "./runtime/adt-capability",
            "args": [],
            "cwd": ".",
        }:
            raise AssertionError("installed Capability adapter binding is not immutable")
        capability_command = (bundle / "runtime" / "adt-capability").resolve()
        schema_probe_command = (
            bundle / "runtime" / "adt-transport-schema-probe"
        ).resolve()
        workspace = harness / "primary"
        alternate = harness / "alternate"
        workspace.mkdir()
        alternate.mkdir()
        server_log_path = harness / "server.log"
        (workspace / "input.json").write_text(
            json.dumps(
                [
                    {"id": 1, "keep": True, "blob": "x" * 10_000},
                    {"id": 2, "keep": False, "blob": "y" * 10_000},
                ]
            ),
            encoding="utf-8",
        )
        (alternate / "input.json").write_text('[{"id":99}]', encoding="utf-8")
        capability_environment = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": tempfile.gettempdir(),
            "PYTHONNOUSERSITE": "1",
            "OPENADAM_CAPABILITY_WORKSPACE_ROOT": str(workspace),
        }
        capability_request = {
            "id": "installed-capability",
            "operationId": "inspect",
            "input": {"source": {"path": "input.json"}, "sample_rows": 1},
        }
        capability = subprocess.run(
            [str(capability_command)],
            cwd=bundle,
            env={**os.environ, **capability_environment},
            input=json.dumps(capability_request, separators=(",", ":")) + "\n",
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if capability.returncode != 0:
            raise AssertionError(
                f"installed Capability adapter failed: {capability.returncode}: {capability.stderr}"
            )
        capability_result = json.loads(capability.stdout)
        if (
            capability_result.get("ok") is not True
            or capability_result["result"]["shape"]["rows"] != 2
        ):
            raise AssertionError(f"installed Capability result is invalid: {capability_result}")

        schema_request = {
            "id": "transport-schema",
            "capabilityId": "org.openadam.structured-data.analyze",
            "capabilityVersion": "0.1.0",
        }
        schema_probe = subprocess.run(
            [str(schema_probe_command)],
            cwd=bundle,
            env={**os.environ, **capability_environment},
            input=json.dumps(schema_request, separators=(",", ":")) + "\n",
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if schema_probe.returncode != 0:
            raise AssertionError(
                "installed transport schema probe failed: "
                f"{schema_probe.returncode}: {schema_probe.stderr}"
            )
        schema_result = json.loads(schema_probe.stdout)
        if schema_result.get("ok") is not True or {
            binding["operationId"] for binding in schema_result["bindings"]
        } != {"inspect", "validate"}:
            raise AssertionError(f"installed transport schema result is invalid: {schema_result}")
        granted_roots = [types.Root(uri=workspace.as_uri(), name="probe-workspace")]

        async def list_roots(
            context: RequestContext[ClientSession, Any],
        ) -> types.ListRootsResult:
            del context
            return types.ListRootsResult(roots=list(granted_roots))

        server = StdioServerParameters(
            command=str(command),
            args=list(config.get("args", [])),
            cwd=bundle,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "TMPDIR": tempfile.gettempdir(),
                "PYTHONNOUSERSITE": "1",
            },
        )
        with server_log_path.open("w+", encoding="utf-8") as server_log:
            async with (
                stdio_client(server, errlog=server_log) as (read_stream, write_stream),
                ClientSession(
                    read_stream,
                    write_stream,
                    list_roots_callback=list_roots,
                ) as session,
            ):
                initialized = await session.initialize()
                if initialized.serverInfo.name != "BatchTicket":
                    raise AssertionError(
                        f"unexpected installed server brand: {initialized.serverInfo.name}"
                    )
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                if names != EXPECTED_TOOLS:
                    raise AssertionError(f"unexpected tools: {sorted(names)}")
                catalog = {
                    "tool_count": len(tools.tools),
                    "input_schema_bytes": sum(
                        len(json.dumps(tool.inputSchema, separators=(",", ":")).encode("utf-8"))
                        for tool in tools.tools
                    ),
                    "output_schema_bytes": sum(
                        len(
                            json.dumps(tool.outputSchema, separators=(",", ":")).encode("utf-8")
                        )
                        for tool in tools.tools
                    ),
                    "tool_definition_bytes": len(
                        json.dumps(
                            [
                                tool.model_dump(by_alias=True, exclude_none=True)
                                for tool in tools.tools
                            ],
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ),
                }
                for tool in tools.tools:
                    properties = tool.inputSchema.get("properties", {})
                    if "ctx" in properties or "workspace" not in properties:
                        raise AssertionError(
                            f"installed schema is not self-sufficient: {tool.name}"
                        )
                    output_schema = tool.outputSchema
                    if (
                        not isinstance(output_schema, dict)
                        or output_schema.get("type") != "object"
                        or len(output_schema.get("anyOf", [])) != 2
                    ):
                        raise AssertionError(
                            "installed tool is missing a typed success/error output schema: "
                            f"{tool.name}"
                        )

                inspected = await session.call_tool(
                    "data_inspect",
                    {"source": {"path": "input.json"}, "sample_rows": 1},
                )
                _require_ok(inspected, "inspect")
                if inspected.structuredContent["shape"]["rows"] != 2:
                    raise AssertionError("installed inspect returned the wrong row count")

                transformed = await session.call_tool(
                    "data_transform",
                    {
                        "plan": {
                            "version": "1",
                            "sources": {"rows": {"path": "input.json"}},
                            "steps": [
                                {"op": "filter", "where": {"field": "keep", "eq": True}},
                                {"op": "select", "fields": ["id", "blob"]},
                            ],
                            "output": {"path": "result.json"},
                            "return": {"mode": "reference"},
                        }
                    },
                )
                _require_ok(transformed, "transform")
                if "receipt" in transformed.structuredContent:
                    raise AssertionError(
                        "installed transform still exposes the retired receipt key"
                    )
                if "execution_effects" not in transformed.structuredContent:
                    raise AssertionError("installed transform omitted execution_effects")
                written = json.loads((workspace / "result.json").read_text(encoding="utf-8"))
                if written != [{"id": 1, "blob": "x" * 10_000}]:
                    raise AssertionError("installed transform wrote the wrong result")
                if transformed.structuredContent["result"]["kind"] != "file":
                    raise AssertionError("large installed transform did not continue by reference")

                before_failure = (workspace / "result.json").read_bytes()
                protected = await session.call_tool(
                    "data_transform",
                    {
                        "plan": {
                            "version": "1",
                            "sources": {"rows": {"path": "input.json"}},
                            "steps": [{"op": "select", "fields": ["id"]}],
                            "output": {"path": "result.json"},
                        }
                    },
                )
                _require_error(protected, "E_OUTPUT_EXISTS")
                if (workspace / "result.json").read_bytes() != before_failure:
                    raise AssertionError("failed overwrite preflight mutated the installed output")
                if list(workspace.glob(".result.json.adt-stage-*")):
                    raise AssertionError("failed overwrite preflight left a staging file")

                escaped = await session.call_tool(
                    "data_inspect",
                    {"source": {"path": "../outside.json"}},
                )
                if escaped.structuredContent["error"]["code"] != "E_PATH_OUTSIDE_WORKSPACE":
                    raise AssertionError("installed server did not reject a parent-path escape")

                timed_out = await session.call_tool(
                    "data_inspect",
                    {
                        "source": {"inline": [{"id": index} for index in range(10_000)]},
                        "limits": {"timeout_ms": 1},
                    },
                )
                _require_error(timed_out, "E_TIMEOUT")
                recovered = await session.call_tool(
                    "data_inspect",
                    {"source": {"inline": [{"id": 1}]}},
                )
                _require_ok(recovered, "post-timeout recovery")

                invalid = await session.call_tool(
                    "data_transform",
                    {
                        "plan": {
                            "version": "1",
                            "sources": {"rows": {"inline": [{"id": 1}]}},
                            "steps": [{"op": "select", "fields": ["id"], "misspelled": True}],
                        }
                    },
                )
                if invalid.isError is not True:
                    raise AssertionError("installed schema accepted an unknown operation field")

                granted_roots[:] = [types.Root(uri=workspace.as_uri(), name=None)]
                unnamed = await session.call_tool(
                    "data_inspect",
                    {"source": {"path": "input.json"}, "sample_rows": 1},
                )
                _require_ok(unnamed, "unnamed workspace root")

                granted_roots[:] = [
                    types.Root(uri=workspace.as_uri(), name="probe-workspace"),
                    types.Root(uri=alternate.as_uri(), name="alternate-workspace")
                ]
                ambiguous = await session.call_tool(
                    "data_inspect",
                    {"source": {"path": "input.json"}},
                )
                _require_error(ambiguous, "E_WORKSPACE_AMBIGUOUS")
                selected = await session.call_tool(
                    "data_inspect",
                    {
                        "source": {"path": "input.json"},
                        "workspace": "alternate-workspace",
                    },
                )
                _require_ok(selected, "named workspace selection")
                if selected.structuredContent["sample"] != [{"id": 99}]:
                    raise AssertionError("installed server selected the wrong granted workspace")

            server_log.seek(0)
            log_output = server_log.read()
            if "Traceback" in log_output or "I/O operation on closed file" in log_output:
                raise AssertionError(
                    f"installed server emitted a shutdown traceback:\n{log_output}"
                )

        no_grant_log_path = harness / "no-grant.log"
        with no_grant_log_path.open("w+", encoding="utf-8") as server_log:
            async with (
                stdio_client(server, errlog=server_log) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                denied = await session.call_tool(
                    "data_inspect",
                    {"source": {"path": "input.json"}},
                )
                _require_error(denied, "E_WORKSPACE_REQUIRED")
                inline = await session.call_tool(
                    "data_inspect",
                    {"source": {"inline": [{"id": 1}]}},
                )
                _require_ok(inline, "no-grant inline call")

            server_log.seek(0)
            no_grant_log = server_log.read()
            if "Traceback" in no_grant_log or "I/O operation on closed file" in no_grant_log:
                raise AssertionError(
                    f"installed no-grant server emitted a shutdown traceback:\n{no_grant_log}"
                )

        return {
            "status": "ok",
            "tools": sorted(names),
            "catalog": catalog,
            "workspace_authority": "mcp-roots",
            "entrypoint": str(command),
            "sequences": [
                "schema-introspection",
                "file-inspect",
                "large-result-reference",
                "overwrite-preflight",
                "resource-escape",
                "timeout-recovery",
                "invalid-contract",
                "unnamed-root",
                "multiple-root-selection",
                "missing-grant",
                "capability-adapter",
                "transport-schema-probe",
            ],
        }


def _require_ok(result: Any, operation: str) -> None:
    if result.isError is True or result.structuredContent.get("status") != "ok":
        raise AssertionError(f"installed {operation} failed: {result}")


def _require_error(result: Any, code: str) -> None:
    structured = result.structuredContent
    if result.isError is True or structured.get("error", {}).get("code") != code:
        raise AssertionError(f"installed server did not return {code}: {result}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe a built BatchTicket plugin")
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_probe(args.bundle.resolve())), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
