from __future__ import annotations

import json
import os
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any, TextIO

from jsonschema import Draft202012Validator

from .runtime import DataTransformer

MAX_REQUEST_LINE_BYTES = 64 * 1024
MAX_RESPONSE_LINE_BYTES = 1024 * 1024
def _load_schema(name: str) -> dict[str, Any]:
    packaged = files("data_transformer").joinpath("capability_schemas", name)
    if packaged.is_file():
        source = packaged.read_text(encoding="utf-8")
    else:
        source = (
            Path(__file__).resolve().parents[2] / "capabilities" / "schemas" / name
        ).read_text(encoding="utf-8")
    return json.loads(source)


SCHEMAS = {
    "inspect": {
        "input": _load_schema("structured-data.inspect.input.schema.json"),
        "output": _load_schema("structured-data.inspect.output.schema.json"),
    },
    "validate": {
        "input": _load_schema("structured-data.validate.input.schema.json"),
        "output": _load_schema("structured-data.validate.output.schema.json"),
    },
}
VALIDATORS = {
    operation: {
        kind: Draft202012Validator(schema)
        for kind, schema in schemas.items()
    }
    for operation, schemas in SCHEMAS.items()
}


def _failure(identifier: Any, code: str, message: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def _canonical_error_code(provider_code: str, operation: str) -> str:
    if provider_code in {
        "E_REQUEST_INVALID",
        "E_SOURCE_INVALID",
        "E_SELECTOR_SYNTAX",
        "E_SELECTOR_TYPE",
        "E_LIMIT_INVALID",
    }:
        return "INVALID_INPUT"
    if provider_code in {"E_PATH_NOT_FOUND", "E_SOURCE_NOT_FOUND"}:
        return "SOURCE_NOT_FOUND"
    if provider_code in {
        "E_NETWORK_FORBIDDEN",
        "E_PATH_INVALID",
        "E_PATH_OUTSIDE_WORKSPACE",
        "E_WORKSPACE_INVALID",
        "E_WORKSPACE_REQUIRED",
    }:
        return "PATH_FORBIDDEN"
    if provider_code in {"E_FORMAT_REQUIRED", "E_FORMAT_UNSUPPORTED"}:
        return "FORMAT_UNSUPPORTED"
    if provider_code in {
        "E_DUPLICATE_KEY",
        "E_NON_STRING_KEY",
        "E_NUMBER_INVALID",
        "E_NUMBER_PRECISION",
        "E_PARSE",
    }:
        return "PARSE_FAILED"
    if provider_code in {
        "E_DEPTH_LIMIT",
        "E_INPUT_TOO_LARGE",
        "E_ITEM_LIMIT",
        "E_MEMORY",
        "E_RESPONSE_TOO_LARGE",
        "E_ROW_LIMIT",
        "E_TEMP_LIMIT",
        "E_TIMEOUT",
    }:
        return "LIMIT_EXCEEDED"
    if operation == "validate" and provider_code in {
        "E_ASSERTION_INVALID",
        "E_SCHEMA_INVALID",
        "E_VALIDATION_REQUIRED",
    }:
        return "VALIDATION_INVALID"
    return "PROVIDER_FAILED"


def _project_assertion(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": value["id"],
        "type": value["type"],
        "status": value["status"],
        "details": {
            key: item
            for key, item in value.items()
            if key not in {"id", "type", "status"}
        },
    }


def _project_result(operation: str, value: dict[str, Any]) -> dict[str, Any]:
    if operation == "inspect":
        return {
            "source": value["source"],
            "shape": value["shape"],
            "sample": value.get("sample", []),
        }
    return {
        "valid": value["valid"],
        "shape": value["shape"],
        "schema_validation": value["schema_validation"],
        "assertions": [_project_assertion(item) for item in value["assertions"]],
        "sample": value["sample"],
    }


def _workspace_root() -> Path:
    raw = os.environ.get("OPENADAM_CAPABILITY_WORKSPACE_ROOT")
    if raw is None:
        raw = os.environ.get("OPENADAM_PROVIDER_ROOT")
    if raw is None:
        raise ValueError("A Capability workspace root is required")
    root = Path(raw).resolve()
    if not root.is_dir():
        raise ValueError("The Capability workspace root is unavailable")
    return root


def _execute(operation: str, input_value: dict[str, Any]) -> dict[str, Any]:
    transformer = DataTransformer(
        _workspace_root(),
        restrict_paths=True,
        worker_start_method="spawn",
    )
    if operation == "inspect":
        return transformer.inspect(
            input_value["source"],
            sample_rows=input_value.get("sample_rows", 5),
            limits=input_value.get("limits"),
        )
    return transformer.validate(
        input_value["source"],
        schema=input_value.get("schema"),
        assertions=input_value.get("assertions"),
        sample_rows=input_value.get("sample_rows", 5),
        limits=input_value.get("limits"),
    )


def _handle_request(line: str) -> dict[str, Any]:
    identifier: Any = None
    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        if set(request) != {"id", "operationId", "input"}:
            raise ValueError("request fields are invalid")
        identifier = request["id"]
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            raise ValueError("request id is invalid")
        operation = request["operationId"]
        if operation not in VALIDATORS:
            raise ValueError("operation is unsupported")
        input_value = request["input"]
        errors = sorted(
            VALIDATORS[operation]["input"].iter_errors(input_value),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            return _failure(
                identifier,
                "INVALID_INPUT",
                "Input does not satisfy the portable Capability contract.",
            )
        provider_result = _execute(operation, input_value)
        if provider_result.get("status") != "ok":
            provider_error = provider_result.get("error", {})
            provider_code = str(provider_error.get("code", "E_EXECUTION"))
            return _failure(
                identifier,
                _canonical_error_code(provider_code, operation),
                str(
                    provider_error.get(
                        "message", "The provider could not complete the operation."
                    )
                ),
            )
        result = _project_result(operation, provider_result)
        output_errors = list(VALIDATORS[operation]["output"].iter_errors(result))
        if output_errors:
            return _failure(
                identifier,
                "PROVIDER_FAILED",
                "The provider result violates the portable output contract.",
            )
        return {"id": identifier, "ok": True, "result": result}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return _failure(identifier, "INVALID_INPUT", "Invalid Capability request envelope.")
    except Exception:
        return _failure(identifier, "PROVIDER_FAILED", "The provider adapter failed safely.")


def serve(input_stream: TextIO, output_stream: TextIO) -> int:
    while True:
        line = input_stream.readline(MAX_REQUEST_LINE_BYTES + 2)
        if line == "":
            return 0
        if len(line.encode("utf-8")) > MAX_REQUEST_LINE_BYTES or not line.endswith("\n"):
            return 2
        if not line.strip():
            continue
        response = _handle_request(line)
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_RESPONSE_LINE_BYTES:
            encoded = json.dumps(
                _failure(
                    response.get("id"),
                    "PROVIDER_FAILED",
                    "The portable response exceeds its fixed bound.",
                ),
                separators=(",", ":"),
            )
        output_stream.write(encoded + "\n")
        output_stream.flush()


def main() -> None:
    raise SystemExit(serve(sys.stdin, sys.stdout))


if __name__ == "__main__":
    main()
