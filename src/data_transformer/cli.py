from __future__ import annotations

import argparse
import io
import json
import os
import select
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .errors import DataTransformerError
from .limits import Limits
from .runtime import DataTransformer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adt",
        description="Inspect and transform structured data with deterministic plans.",
    )
    parser.add_argument(
        "--compact", action="store_true", help="emit compact JSON instead of indented JSON"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="inspect shape without dumping data")
    _add_source_arguments(inspect_parser, "source")
    inspect_parser.add_argument("--sample-rows", type=int, default=5)

    transform_parser = subparsers.add_parser("transform", help="run a Transformation Plan v1")
    transform_parser.add_argument("plan", help="JSON or YAML plan path, or - for stdin JSON")
    transform_parser.add_argument("--dry-run", action="store_true")

    validate_parser = subparsers.add_parser("validate", help="validate schema and assertions")
    _add_source_arguments(validate_parser, "source")
    validate_parser.add_argument("--schema", help="JSON or YAML Schema path")
    validate_parser.add_argument("--assertions", help="JSON or YAML assertion array path")
    validate_parser.add_argument("--sample-rows", type=int, default=5)

    diff_parser = subparsers.add_parser("diff", help="compare two structured data sources")
    diff_parser.add_argument("left")
    diff_parser.add_argument("right")
    diff_parser.add_argument("--left-format")
    diff_parser.add_argument("--right-format")
    diff_parser.add_argument("--key", action="append", dest="key_fields")
    diff_parser.add_argument("--sample-rows", type=int, default=5)

    subparsers.add_parser("mcp", help="run the MCP server over stdio")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return 0

    try:
        result = _run_command(args, parser)
    except DataTransformerError as exc:
        result = exc.to_result(args.command)
    except (OSError, UnicodeError, ValueError) as exc:
        result = DataTransformerError(
            "E_CLI_INPUT",
            "could not read command input",
            {"reason": str(exc)},
        ).to_result(args.command)

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":") if args.compact else None,
            indent=None if args.compact else 2,
        )
    )
    return 0 if result.get("status") in {"ok", "dry_run"} else 2


def _run_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict[str, Any]:
    temporary_paths: list[Path] = []
    acquisition_started = time.monotonic()
    try:
        request: dict[str, Any]
        if args.command == "transform":
            request = {
                "plan": _document_descriptor(args.plan, temporary_paths),
                "dry_run": True if args.dry_run else None,
            }
        elif args.command == "inspect":
            request = {
                "source": _source_descriptor(
                    args.source, args.format, args.select, args.kind, temporary_paths
                ),
                "sample_rows": args.sample_rows,
            }
        elif args.command == "validate":
            request = {
                "source": _source_descriptor(
                    args.source, args.format, args.select, args.kind, temporary_paths
                ),
                "schema": (
                    _document_descriptor(args.schema, temporary_paths) if args.schema else None
                ),
                "assertions": (
                    _document_descriptor(args.assertions, temporary_paths)
                    if args.assertions
                    else None
                ),
                "sample_rows": args.sample_rows,
            }
        elif args.command == "diff":
            left_path = Path(args.left).resolve()
            right_path = Path(args.right).resolve()
            left_source = {
                "path": str(left_path),
                **({"format": args.left_format} if args.left_format else {}),
            }
            right_source = {
                "path": str(right_path),
                **({"format": args.right_format} if args.right_format else {}),
            }
            request = {
                "left": left_source,
                "right": right_source,
                "key_fields": args.key_fields,
                "sample_rows": args.sample_rows,
                "base_dir": str(Path.cwd()),
            }
        else:
            parser.error("unknown command")
            return 2

        elapsed_ms = int((time.monotonic() - acquisition_started) * 1000)
        return DataTransformer(worker_start_method="spawn").run_cli_request(
            {"command": args.command, "request": request},
            acquisition_elapsed_ms=elapsed_ms,
        )
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)


def _add_source_arguments(parser: argparse.ArgumentParser, name: str) -> None:
    parser.add_argument(name, help="source path, or - for inline JSON from stdin")
    parser.add_argument("--format")
    parser.add_argument("--select", help="safe selector such as data.users[*]")
    parser.add_argument("--kind", choices=["tree", "table"])


def _source_descriptor(
    source: str,
    data_format: str | None,
    selector: str | None,
    kind: str | None,
    temporary_paths: list[Path],
) -> dict[str, Any]:
    if source == "-":
        source_format = data_format or "json"
        path = _spool_stdin(
            source_format,
            maximum=Limits.max_input_bytes,
            timeout_ms=Limits.timeout_ms,
        )
        temporary_paths.append(path)
        spec: dict[str, Any] = {
            "document": {
                "path": str(path),
                "format": source_format,
                "base_dir": str(Path.cwd()),
                "source": "stdin",
                "temporary": True,
            },
            "format": source_format,
        }
    else:
        path = Path(source).resolve()
        spec = {"path": str(path), "base_dir": str(path.parent)}
        if data_format:
            spec["format"] = data_format
    if selector:
        spec["select"] = selector
    if kind:
        spec["kind"] = kind
    return spec


def _document_descriptor(raw_path: str, temporary_paths: list[Path]) -> dict[str, Any]:
    if raw_path == "-":
        path = _spool_stdin(
            "json",
            maximum=Limits.HARD_MAX_INPUT_BYTES,
            timeout_ms=Limits.HARD_MAX_TIMEOUT_MS,
        )
        temporary_paths.append(path)
        return {
            "path": str(path),
            "format": "json",
            "base_dir": str(Path.cwd()),
            "source": "stdin",
            "temporary": True,
        }
    return {"path": str(Path(raw_path).resolve())}


def _spool_stdin(data_format: str, *, maximum: int, timeout_ms: int) -> Path:
    suffix = ".yaml" if data_format in {"yaml", "yml"} else f".{data_format}"
    descriptor, raw_path = tempfile.mkstemp(prefix="adt-stdin-", suffix=suffix)
    path = Path(raw_path)
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    written = 0
    started = time.monotonic()
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while True:
                remaining = timeout_ms / 1000 - (time.monotonic() - started)
                if remaining <= 0:
                    raise DataTransformerError(
                        "E_TIMEOUT", "stdin acquisition exceeded the wall-clock limit"
                    )
                try:
                    source_descriptor = stream.fileno()
                except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
                    source_descriptor = None
                if source_descriptor is None:
                    chunk = stream.read(64 * 1024)
                else:
                    try:
                        readable, _, _ = select.select(
                            [source_descriptor], [], [], remaining
                        )
                    except (OSError, TypeError, ValueError):
                        readable = [source_descriptor]
                    if not readable:
                        raise DataTransformerError(
                            "E_TIMEOUT", "stdin acquisition exceeded the wall-clock limit"
                        )
                    chunk = os.read(source_descriptor, 64 * 1024)
                if not chunk:
                    break
                encoded = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                written += len(encoded)
                if written > maximum:
                    raise DataTransformerError(
                        "E_INPUT_TOO_LARGE",
                        "stdin exceeds the bounded acquisition limit",
                        {"bytes": written, "maximum": maximum},
                    )
                if (time.monotonic() - started) * 1000 > timeout_ms:
                    raise DataTransformerError(
                        "E_TIMEOUT", "stdin acquisition exceeded the wall-clock limit"
                    )
                handle.write(encoded)
        return path
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
