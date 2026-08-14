from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .errors import DataTransformerError

SUPPORTED_FORMATS = {"json", "jsonl", "csv", "tsv", "yaml", "parquet"}
_SUFFIX_FORMATS = {
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".csv": "csv",
    ".tsv": "tsv",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".parquet": "parquet",
}


def infer_format(path: Path, explicit: str | None = None) -> str:
    if explicit is not None:
        normalized = explicit.lower()
        if normalized not in SUPPORTED_FORMATS:
            raise DataTransformerError(
                "E_FORMAT_UNSUPPORTED",
                "unsupported data format",
                {"format": explicit, "supported": sorted(SUPPORTED_FORMATS)},
            )
        return normalized
    data_format = _SUFFIX_FORMATS.get(path.suffix.lower())
    if data_format is None:
        raise DataTransformerError(
            "E_FORMAT_REQUIRED",
            "format cannot be inferred from the path",
            {"path": str(path), "supported": sorted(SUPPORTED_FORMATS)},
        )
    return data_format


def parse_json(text: str, source: str) -> Any:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DataTransformerError(
                    "E_DUPLICATE_KEY",
                    "JSON object contains a duplicate key",
                    {"source": source, "key": key},
                )
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate,
            parse_float=Decimal,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except DataTransformerError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise DataTransformerError(
            "E_PARSE",
            "invalid JSON input",
            {"source": source, "line": getattr(exc, "lineno", None)},
        ) from exc


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise DataTransformerError(
                "E_DUPLICATE_KEY",
                "YAML mapping contains a duplicate key",
                {"key": str(key)},
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def parse_yaml(text: str, source: str) -> Any:
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)
    except DataTransformerError:
        raise
    except yaml.YAMLError as exc:
        raise DataTransformerError("E_PARSE", "invalid YAML input", {"source": source}) from exc
