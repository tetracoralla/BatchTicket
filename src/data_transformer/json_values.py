from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import decimal
import json
import math
from pathlib import Path
from typing import Any

from .errors import DataTransformerError


def _require_string_key(key: Any) -> str:
    if not isinstance(key, str):
        raise DataTransformerError(
            "E_NON_STRING_KEY",
            "structured-data object keys must be strings",
            {"key_type": type(key).__name__},
        )
    return key


def normalize_source_numbers(value: Any) -> Any:
    """Give structured inputs the same exact-decimal semantics.

    JSON files are decoded with ``Decimal`` while JSON-RPC and Python callers
    normally hand us binary floats. Converting a finite received float through
    its shortest JSON spelling makes ordinary inline JSON values such as 0.1
    follow the exact-decimal path used by file inputs.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DataTransformerError(
                "E_NUMBER_INVALID", "non-finite numbers are not valid structured data"
            )
        return decimal.Decimal(str(value))
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise DataTransformerError(
                "E_NUMBER_INVALID", "non-finite numbers are not valid structured data"
            )
        return value
    if isinstance(value, dict):
        return {
            _require_string_key(key): normalize_source_numbers(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalize_source_numbers(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_source_numbers(item) for item in value]
    return value


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise DataTransformerError(
                "E_NUMBER_INVALID", "non-finite numbers are not valid structured data"
            )
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {_require_string_key(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def internal_json(value: Any) -> str:
    """Encode exact decimals as JSON numbers for the private DuckDB ingestion boundary."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal is not valid JSON")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not valid JSON")
        return json.dumps(value, allow_nan=False)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{json.dumps(_require_string_key(key), ensure_ascii=False)}:{internal_json(item)}"
            for key, item in value.items()
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(internal_json(item) for item in value) + "]"
    return internal_json(json_safe(value))


def json_size(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))
