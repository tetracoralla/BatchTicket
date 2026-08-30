from __future__ import annotations

import os
import sys
import threading
from _thread import LockType
from io import TextIOWrapper
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, TextContent

from .contracts import (
    DiffMcpOutput,
    DiffToolInput,
    InspectMcpOutput,
    InspectToolInput,
    TransformMcpOutput,
    TransformToolInput,
    ValidateMcpOutput,
    ValidateToolInput,
)
from .errors import DataTransformerError
from .json_values import canonical_json
from .limits import Limits
from .runtime import DataTransformer

FastMCPSettings.model_rebuild()

mcp = FastMCP(
    "BatchTicket",
    instructions=(
        "Use these tools for deterministic JSON, JSONL, CSV, TSV, YAML, or Parquet "
        "inspection, reshaping, validation, and comparison. Inspect only when the shape "
        "is unknown. Never invent raw SQL or source code; data_transform accepts Plan v1."
    ),
)

_MCP_INLINE_TEXT_MAX_BYTES = 4096

InspectMcpResult = Annotated[CallToolResult, InspectMcpOutput]
TransformMcpResult = Annotated[CallToolResult, TransformMcpOutput]
ValidateMcpResult = Annotated[CallToolResult, ValidateMcpOutput]
DiffMcpResult = Annotated[CallToolResult, DiffMcpOutput]


@mcp.tool(
    name="data_inspect",
    description=(
        "Inspect JSON, JSONL, CSV, TSV, YAML, or Parquet shape, types, counts, and a small "
        "sample without returning the full payload. Use for 'what fields are in this data?' "
        "or unknown tool output. Optionally compare record fields with target_schema and "
        "return deterministic mapping candidates; a draft plan is returned only after "
        "explicit mappings are supplied. One successful call is sufficient for its recorded "
        "observations; never repeat the same arguments to confirm it."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def data_inspect(
    source: Any,
    sample_rows: Any = 5,
    target_schema: Any = None,
    mappings: Any = None,
    limits: Any = None,
    workspace: Any = None,
    ctx: Context | None = None,
) -> InspectMcpResult:
    source = _transport_value(source)
    target_schema = _transport_value(target_schema)
    mappings = _transport_value(mappings)
    limits = _transport_value(limits)
    cancel_event = threading.Event()
    cancel_lock = threading.Lock()
    transformer = await _transformer_for_call(
        "inspect",
        ctx,
        workspace,
        needs_files=_source_uses_files(source),
        cancel_event=cancel_event,
        cancel_lock=cancel_lock,
    )
    if isinstance(transformer, dict):
        return _bounded_mcp_result("inspect", transformer, _response_limit(limits))
    inspect_kwargs = {"sample_rows": sample_rows, "limits": limits}
    if target_schema is not None:
        inspect_kwargs["target_schema"] = target_schema
    if mappings is not None:
        inspect_kwargs["mappings"] = mappings
    result = await _run_cancellable(
        lambda: transformer.inspect(source, **inspect_kwargs),
        cancel_event,
        cancel_lock,
    )
    return _bounded_mcp_result("inspect", result, _response_limit(limits))


@mcp.tool(
    name="data_transform",
    description=(
        "Transform or rewrite records: reshape, filter, join, aggregate, cast, flatten, or "
        "convert structured data with Transformation Plan v1. Do not use this tool for a "
        "validation-only request such as checking non-null or unique fields; use "
        "data_validate. Known transformation shape: "
        '{"version":"1","sources":{"input":{"path":"users.json","select":'
        '"data.users[*]"}},"steps":[{"id":"filtered","op":"filter","source":'
        '"input","where":{"field":"age","gte":18}},{"op":"select","source":'
        '"filtered","fields":[{"field":"userId","as":"id"}]}],"return":'
        '{"mode":"auto"}}. Omit workspace with ADT_WORKSPACE_ROOT; otherwise it is an exact '
        "granted root name, never a path. Returns a compact sample and explicit execution "
        "effects; large results require output.path."
    ),
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def data_transform(
    plan: Any,
    dry_run: Any = None,
    workspace: Any = None,
    ctx: Context | None = None,
) -> TransformMcpResult:
    plan = _transport_value(plan)
    cancel_event = threading.Event()
    cancel_lock = threading.Lock()
    transformer = await _transformer_for_call(
        "transform",
        ctx,
        workspace,
        needs_files=_plan_uses_files(plan),
        cancel_event=cancel_event,
        cancel_lock=cancel_lock,
    )
    if isinstance(transformer, dict):
        return _bounded_mcp_result("transform", transformer, _plan_response_limit(plan))
    result = await _run_cancellable(
        lambda: transformer.transform(plan, dry_run=dry_run),
        cancel_event,
        cancel_lock,
    )
    if result.get("status") == "error" and result.get("error", {}).get("code") in {
        "E_PLAN_INVALID",
        "E_CONDITION_INVALID",
        "E_EXPRESSION_INVALID",
    }:
        # Caller-construction failures stay protocol-level tool errors, but they
        # must still carry the stable code and bounded details instead of a bare
        # message string.
        bounded = _bounded_mcp_result("transform", result, _plan_response_limit(plan))
        return CallToolResult(
            content=bounded.content,
            structuredContent=bounded.structuredContent,
            isError=True,
        )
    return _bounded_mcp_result("transform", result, _plan_response_limit(plan))


@mcp.tool(
    name="data_validate",
    description=(
        "Validate or check requirements on existing structured data (校验/检查非空、唯一、类型、"
        "字段或行数); choose data_validate, not data_transform, for validation-only requests. "
        "It accepts JSON Schema and deterministic assertions and returns valid true or false "
        "without rewriting the source. Known validation shape: "
        '{"source":{"path":"users.json","select":"data.users[*]"},"assertions":'
        '[{"type":"not_null","field":"userId"},{"type":"unique","field":"userId"}]}.'
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def data_validate(
    source: Any,
    schema: Any = None,
    assertions: Any = None,
    sample_rows: Any = 5,
    limits: Any = None,
    workspace: Any = None,
    ctx: Context | None = None,
) -> ValidateMcpResult:
    source = _transport_value(source)
    schema = _transport_value(schema)
    assertions = _transport_value(assertions)
    limits = _transport_value(limits)
    cancel_event = threading.Event()
    cancel_lock = threading.Lock()
    transformer = await _transformer_for_call(
        "validate",
        ctx,
        workspace,
        needs_files=_source_uses_files(source),
        cancel_event=cancel_event,
        cancel_lock=cancel_lock,
    )
    if isinstance(transformer, dict):
        return _bounded_mcp_result("validate", transformer, _response_limit(limits))
    result = await _run_cancellable(
        lambda: transformer.validate(
            source,
            schema=schema,
            assertions=assertions,
            sample_rows=sample_rows,
            limits=limits,
        ),
        cancel_event,
        cancel_lock,
    )
    return _bounded_mcp_result("validate", result, _response_limit(limits))


@mcp.tool(
    name="data_diff",
    description=(
        "Compare two structured datasets by schema and rows, optionally using stable key "
        "fields. Returns compact added, removed, and changed counts and samples."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def data_diff(
    left: Any,
    right: Any,
    key_fields: Any = None,
    sample_rows: Any = 5,
    limits: Any = None,
    workspace: Any = None,
    ctx: Context | None = None,
) -> DiffMcpResult:
    left = _transport_value(left)
    right = _transport_value(right)
    limits = _transport_value(limits)
    cancel_event = threading.Event()
    cancel_lock = threading.Lock()
    transformer = await _transformer_for_call(
        "diff",
        ctx,
        workspace,
        needs_files=_source_uses_files(left) or _source_uses_files(right),
        cancel_event=cancel_event,
        cancel_lock=cancel_lock,
    )
    if isinstance(transformer, dict):
        return _bounded_mcp_result("diff", transformer, _response_limit(limits))
    result = await _run_cancellable(
        lambda: transformer.diff(
            left,
            right,
            key_fields=key_fields,
            sample_rows=sample_rows,
            limits=limits,
        ),
        cancel_event,
        cancel_lock,
    )
    return _bounded_mcp_result("diff", result, _response_limit(limits))


async def _run_cancellable(
    callback: Any,
    cancel_event: threading.Event,
    cancel_lock: LockType,
) -> dict[str, Any]:
    completed = False
    try:
        result = await anyio.to_thread.run_sync(callback, abandon_on_cancel=True)
        completed = True
        return result
    finally:
        # With abandon_on_cancel AnyIO releases the event loop immediately. The
        # worker thread observes this token, terminates its process, and removes
        # any reserved staging file instead of publishing after cancellation.
        if not completed:
            def cancel_under_publication_gate() -> None:
                with cancel_lock:
                    cancel_event.set()

            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(cancel_under_publication_gate)


def _response_limit(raw: Any) -> int:
    if isinstance(raw, dict):
        requested = raw.get("max_response_bytes")
        if (
            isinstance(requested, int)
            and not isinstance(requested, bool)
            and Limits.MIN_RESPONSE_BYTES <= requested <= Limits.HARD_MAX_RESPONSE_BYTES
        ):
            return requested
    return Limits.max_response_bytes


def _transport_value(value: Any) -> Any:
    """Dump FastMCP's typed arguments back to the core's JSON carrier.

    The core deliberately accepts ordinary Python dictionaries so its CLI and
    library adapters keep their existing behavior.  This MCP-only conversion
    gives tools/list a closed, executable catalog without adding a second
    transformation implementation.
    """
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python", by_alias=True, exclude_none=True)
    if isinstance(value, list):
        return [_transport_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _transport_value(item) for key, item in value.items()}
    return value


def _plan_response_limit(plan: Any) -> int:
    if isinstance(plan, dict):
        return _response_limit(plan.get("limits"))
    return Limits.max_response_bytes


def _mcp_result(
    operation: str,
    result: dict[str, Any],
    *,
    include_inline_data: bool = True,
) -> CallToolResult:
    summary = _mcp_summary(
        operation,
        result,
        include_inline_data=include_inline_data,
    )
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structuredContent=result,
        isError=False,
    )


def _mcp_summary(
    operation: str,
    result: dict[str, Any],
    *,
    include_inline_data: bool = True,
) -> str:
    status = str(result.get("status", "error"))
    if status == "error":
        code = result.get("error", {}).get("code")
        return f"{operation}: error" + (f" ({code})" if code else "")
    if operation == "inspect":
        adaptation = result.get("adaptation")
        if isinstance(adaptation, dict):
            adaptation_status = adaptation.get("status")
            text = f"inspect: {status}; adaptation={adaptation_status}"
            reason = adaptation.get("reason")
            if isinstance(reason, str) and reason:
                text += f"; reason={reason}"
            source = adaptation.get("source")
            if isinstance(source, dict) and source.get("select") is not None:
                text += f"; record_set={source.get('select')}"
            confirmed = adaptation.get("confirmed_mappings")
            unresolved = adaptation.get("unresolved")
            if isinstance(confirmed, list):
                text += f"; mappings={len(confirmed)}"
            if isinstance(unresolved, list):
                text += f"; unresolved={len(unresolved)}"
            candidates = adaptation.get("source_candidates")
            if isinstance(candidates, list) and adaptation_status == "needs_source_selection":
                text += f"; source_candidates={len(candidates)}"
            draft_plan = adaptation.get("draft_plan")
            if include_inline_data and isinstance(draft_plan, dict):
                draft_text = canonical_json(draft_plan)
                expanded = f"{text}; draft_plan={draft_text}"
                if len(expanded.encode("utf-8")) <= _MCP_INLINE_TEXT_MAX_BYTES:
                    text = expanded
            return text
        shape = result.get("shape", {})
        record_sets = shape.get("record_sets") if isinstance(shape, dict) else None
        if isinstance(record_sets, list) and record_sets:
            record_set = record_sets[0]
            fields = record_set.get("fields", {})
            names, fields_truncated = _summary_field_paths(fields)
            suffix = ", ..." if fields_truncated else ""
            return (
                f"inspect: {status}; record_set={record_set.get('select')}; "
                f"rows={record_set.get('rows')}; fields={','.join(names)}{suffix}"
            )
        if isinstance(shape, dict) and shape.get("kind") == "table":
            fields = shape.get("fields", {})
            names, fields_truncated = _summary_field_paths(fields)
            suffix = ", ..." if fields_truncated else ""
            return (
                f"inspect: {status}; rows={shape.get('rows')}; "
                f"fields={','.join(names)}{suffix}"
            )
    if operation == "transform":
        summary = result.get("summary", {})
        if isinstance(summary, dict):
            text = f"transform: {status}; rows_out={summary.get('rows_out')}"
            descriptor = result.get("result")
            if (
                include_inline_data
                and isinstance(descriptor, dict)
                and descriptor.get("kind") == "inline"
                and "data" in descriptor
            ):
                inline_text = canonical_json(descriptor["data"])
                if len(inline_text.encode("utf-8")) <= _MCP_INLINE_TEXT_MAX_BYTES:
                    text += f"; data={inline_text}"
            return text
    if operation == "validate":
        return f"validate: {status}; valid={result.get('valid')}"
    if operation == "diff":
        return (
            f"diff: {status}; added={result.get('added_rows')}; "
            f"removed={result.get('removed_rows')}; changed={result.get('changed_rows')}"
        )
    return f"{operation}: {status}"


def _summary_field_paths(raw_fields: Any, maximum: int = 10) -> tuple[list[str], bool]:
    if not isinstance(raw_fields, dict):
        return [], False
    paths: list[str] = []
    total = 0

    def visit(fields: dict[str, Any], prefix: str = "") -> None:
        nonlocal total
        for name, profile in fields.items():
            path = f"{prefix}.{name}" if prefix else name
            nested = profile.get("fields") if isinstance(profile, dict) else None
            if isinstance(nested, dict) and nested:
                visit(nested, path)
            else:
                total += 1
                if len(paths) < maximum:
                    paths.append(path)

    visit(raw_fields)
    return paths, total > len(paths)


def _bounded_mcp_result(
    operation: str,
    result: dict[str, Any],
    maximum: int,
) -> CallToolResult:
    candidate = _mcp_result(operation, result)
    encoded = canonical_json(
        candidate.model_dump(mode="json", by_alias=True, exclude_none=True)
    ).encode("utf-8")
    if len(encoded) <= maximum:
        return candidate
    # Rich text is a compatibility aid for hosts that do not surface
    # structuredContent to the model. It must never turn an otherwise valid
    # structured result into a response-budget error.
    compact_candidate = _mcp_result(operation, result, include_inline_data=False)
    encoded = canonical_json(
        compact_candidate.model_dump(mode="json", by_alias=True, exclude_none=True)
    ).encode("utf-8")
    if len(encoded) <= maximum:
        return compact_candidate
    bounded_error = DataTransformerError(
        "E_RESPONSE_TOO_LARGE",
        "complete serialized MCP response exceeds the response budget",
        {"bytes": len(encoded), "maximum": maximum},
    ).to_result(operation)
    candidate = _mcp_result(operation, bounded_error)
    encoded = canonical_json(
        candidate.model_dump(mode="json", by_alias=True, exclude_none=True)
    ).encode("utf-8")
    if len(encoded) <= maximum:
        return candidate
    minimal = DataTransformerError(
        "E_RESPONSE_TOO_LARGE", "MCP response exceeds the response budget"
    ).to_result(operation)
    return _mcp_result(operation, minimal)


async def _transformer_for_call(
    operation: str,
    ctx: Context | None,
    workspace: Any,
    *,
    needs_files: bool,
    cancel_event: threading.Event,
    cancel_lock: LockType,
) -> DataTransformer | dict[str, Any]:
    if workspace is not None and not isinstance(workspace, str):
        return DataTransformerError(
            "E_WORKSPACE_INVALID", "workspace must be a string name"
        ).to_result(operation)
    if not needs_files:
        return DataTransformer(
            None,
            restrict_paths=True,
            worker_start_method="spawn",
            _cancel_event=cancel_event,
            _cancel_lock=cancel_lock,
        )
    try:
        root = await _resolve_workspace(ctx, workspace)
        return DataTransformer(
            root,
            restrict_paths=True,
            worker_start_method="spawn",
            _cancel_event=cancel_event,
            _cancel_lock=cancel_lock,
        )
    except DataTransformerError as exc:
        return exc.to_result(operation)


async def _resolve_workspace(ctx: Context | None, requested: str | None) -> Path:
    roots = await _client_roots(ctx)
    if roots is not None:
        if not roots:
            raise DataTransformerError(
                "E_WORKSPACE_REQUIRED",
                "the MCP client did not grant a file workspace",
            )
        choices = _root_choices(roots)
        if requested is None:
            if len(choices) != 1:
                raise DataTransformerError(
                    "E_WORKSPACE_AMBIGUOUS",
                    "multiple MCP workspaces are granted; choose one by name",
                    {"available": sorted(choices)},
                )
            return next(iter(choices.values()))
        selected = choices.get(requested)
        if selected is None:
            raise DataTransformerError(
                "E_WORKSPACE_NOT_GRANTED",
                "requested workspace is not granted by the MCP client",
                {"workspace": requested, "available": sorted(choices)},
            )
        return selected

    fallback = os.environ.get("ADT_WORKSPACE_ROOT")
    if not fallback:
        raise DataTransformerError(
            "E_WORKSPACE_REQUIRED",
            "file resources require an MCP root or ADT_WORKSPACE_ROOT grant",
        )
    if requested is not None:
        raise DataTransformerError(
            "E_WORKSPACE_NOT_GRANTED",
            "named workspace selection requires MCP roots support",
            {"workspace": requested},
        )
    return _validate_root_path(Path(fallback), "ADT_WORKSPACE_ROOT")


async def _client_roots(ctx: Context | None) -> list[Any] | None:
    if ctx is None:
        return None
    try:
        session = ctx.session
        params = session.client_params
        if params is None or params.capabilities.roots is None:
            return None
        result = await session.list_roots()
        return list(result.roots)
    except (AttributeError, McpError, RuntimeError, ValueError) as exc:
        raise DataTransformerError(
            "E_WORKSPACE_UNAVAILABLE",
            "could not obtain granted workspaces from the MCP client",
        ) from exc


def _root_choices(roots: list[Any]) -> dict[str, Path]:
    choices: dict[str, Path] = {}
    named = {
        name.strip()
        for root in roots
        if isinstance((name := getattr(root, "name", None)), str) and name.strip()
    }
    fallback_index = 1
    for root in roots:
        name = getattr(root, "name", None)
        if isinstance(name, str) and name.strip():
            normalized = name.strip()
        else:
            while f"root-{fallback_index}" in named or f"root-{fallback_index}" in choices:
                fallback_index += 1
            normalized = f"root-{fallback_index}"
            fallback_index += 1
        if normalized in choices:
            raise DataTransformerError(
                "E_WORKSPACE_INVALID",
                "granted MCP root names must be unique",
                {"workspace": normalized},
            )
        choices[normalized] = _path_from_root_uri(str(root.uri), normalized)
    return choices


def _path_from_root_uri(uri: str, name: str) -> Path:
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise DataTransformerError(
            "E_WORKSPACE_INVALID",
            "granted MCP roots must be local file URIs",
            {"workspace": name},
        )
    if parsed.query or parsed.fragment:
        raise DataTransformerError(
            "E_WORKSPACE_INVALID",
            "granted MCP root URI cannot contain a query or fragment",
            {"workspace": name},
        )
    return _validate_root_path(Path(url2pathname(parsed.path)), name)


def _validate_root_path(path: Path, name: str) -> Path:
    if not path.is_absolute():
        raise DataTransformerError(
            "E_WORKSPACE_INVALID",
            "granted workspace must be an absolute local directory",
            {"workspace": name},
        )
    resolved = path.resolve(strict=False)
    if not resolved.exists() or not resolved.is_dir():
        raise DataTransformerError(
            "E_WORKSPACE_INVALID",
            "granted workspace directory does not exist",
            {"workspace": name},
        )
    return resolved


def _source_uses_files(source: Any) -> bool:
    if hasattr(source, "path"):
        return getattr(source, "path", None) is not None
    return isinstance(source, dict) and "path" in source


def _plan_uses_files(plan: Any) -> bool:
    if hasattr(plan, "sources"):
        sources = plan.sources.values()
        output = getattr(plan, "output", None)
    elif isinstance(plan, dict):
        raw_sources = plan.get("sources", {})
        sources = raw_sources.values() if isinstance(raw_sources, dict) else ()
        output = plan.get("output")
    else:
        return False
    return output is not None or any(_source_uses_files(source) for source in sources)


def _install_public_tool_schemas() -> None:
    models = {
        "data_inspect": InspectToolInput,
        "data_transform": TransformToolInput,
        "data_validate": ValidateToolInput,
        "data_diff": DiffToolInput,
    }
    for tool in mcp._tool_manager.list_tools():
        tool.parameters = models[tool.name].model_json_schema(by_alias=True)
        output_schema = tool.fn_metadata.output_schema
        if output_schema is None:  # pragma: no cover - each public tool is typed above.
            raise RuntimeError(f"public tool is missing an output contract: {tool.name}")
        # RootModel emits a root ``anyOf`` for the strict success/error union.
        # All branches are objects, and the MCP SDK requires that fact to be
        # explicit at the schema root before it will accept tools/list.
        object.__setattr__(
            tool.fn_metadata,
            "output_schema",
            {**output_schema, "type": "object"},
        )
        # FastMCP otherwise attempts json.loads() on string-valued non-string
        # arguments before calling the handler. Public schemas require objects,
        # so keep those strings opaque and let the isolated worker reject them.
        object.__setattr__(tool.fn_metadata, "pre_parse_json", _identity_arguments)


def _identity_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return arguments


_install_public_tool_schemas()


def main() -> None:
    anyio.run(_run_stdio_without_closing_process_handles)


async def _run_stdio_without_closing_process_handles() -> None:
    stdin_binary = os.fdopen(os.dup(sys.stdin.fileno()), "rb", closefd=True)
    stdout_binary = os.fdopen(os.dup(sys.stdout.fileno()), "wb", closefd=True)
    stdin = anyio.wrap_file(
        TextIOWrapper(stdin_binary, encoding="utf-8", errors="replace")
    )
    stdout = anyio.wrap_file(TextIOWrapper(stdout_binary, encoding="utf-8"))
    async with stdin, stdout, stdio_server(stdin=stdin, stdout=stdout) as streams:
        read_stream, write_stream = streams
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )


if __name__ == "__main__":
    main()
