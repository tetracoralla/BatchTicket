from __future__ import annotations

import csv
import dataclasses
import decimal
import re
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import duckdb

from .dataset import DataSet
from .errors import DataTransformerError
from .formats import infer_format, parse_json, parse_yaml
from .json_values import internal_json, json_safe, json_size, normalize_source_numbers
from .limits import Limits
from .selectors import select_value

INTERNAL_ORDER = "__adt_internal_order__"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_]")
_NESTED_TYPE_BASES = {"STRUCT", "LIST", "MAP", "JSON", "UNION"}
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
SHAPE_FIELD_CAP = 1_000
_QUALITY_COLUMN_CHUNK = 64
_TREE_SAMPLE_FIELD_CAP = 50
_TREE_SAMPLE_DEPTH_CAP = 8
_RECORD_SET_CAP = 32
_SELECTOR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


@dataclasses.dataclass
class _JsonType:
    kind: str
    minimum_integer: int | None = None
    maximum_integer: int | None = None
    integer_digits: int = 0
    scale: int = 0
    fields: dict[str, _JsonType | None] = dataclasses.field(default_factory=dict)
    element: _JsonType | None = None


def bound_shape(shape: dict[str, Any]) -> dict[str, Any]:
    """Bound the field listing inside a public shape description.

    Totals stay exact; only the per-field listing is capped so a wide record
    cannot flood the response envelope.
    """
    fields = shape.get("fields")
    if not isinstance(fields, dict) or len(fields) <= SHAPE_FIELD_CAP:
        return shape
    bounded = {**shape}
    bounded["fields"] = dict(list(fields.items())[:SHAPE_FIELD_CAP])
    bounded["fields_truncated"] = True
    if "field_count" not in bounded:
        bounded["field_count"] = len(fields)
    return bounded


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def field_expression(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise DataTransformerError(
            "E_FIELD_INVALID", "field must be a non-empty string", {"field": path}
        )
    parts = path.split(".")
    if any(not part for part in parts):
        raise DataTransformerError(
            "E_FIELD_INVALID", "field path contains an empty segment", {"field": path}
        )
    return ".".join(quote_identifier(part) for part in parts)


def resolve_resource_path(
    raw_path: str,
    base_dir: Path,
    *,
    resource_root: Path | None = None,
    restricted_paths: bool = False,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise DataTransformerError("E_PATH_INVALID", "path must be a non-empty local path")
    if "://" in raw_path or raw_path.startswith(("data:", "file:")):
        raise DataTransformerError(
            "E_NETWORK_FORBIDDEN",
            "URLs and URI sources are not supported",
            {"path": raw_path},
        )
    if restricted_paths:
        if resource_root is None:
            raise DataTransformerError(
                "E_WORKSPACE_REQUIRED",
                "file resources require an explicitly granted MCP workspace",
            )
        root = resource_root.resolve()
        unresolved = Path(raw_path)
        windows_absolute = bool(re.match(r"^[A-Za-z]:[\\/]", raw_path)) or raw_path.startswith(
            "\\\\"
        )
        if (
            unresolved.is_absolute()
            or windows_absolute
            or raw_path.startswith("~")
            or ".." in unresolved.parts
        ):
            raise DataTransformerError(
                "E_PATH_OUTSIDE_WORKSPACE",
                "MCP file paths must be relative to the granted workspace",
                {"path": raw_path},
            )
        candidate = (root / unresolved).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise DataTransformerError(
                "E_PATH_OUTSIDE_WORKSPACE",
                "file path escapes the granted workspace",
                {"path": raw_path},
            ) from exc
        return candidate
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


class Workspace:
    def __init__(
        self,
        limits: Limits | None = None,
        *,
        resource_root: Path | None = None,
        restricted_paths: bool = False,
    ) -> None:
        self.limits = limits or Limits()
        self.resource_root = resource_root.resolve() if resource_root is not None else None
        self.restricted_paths = restricted_paths
        self._temporary = tempfile.TemporaryDirectory(prefix="adt-")
        temp_directory = Path(self._temporary.name) / "duckdb-temp"
        temp_directory.mkdir()
        raw_connection = duckdb.connect(
            ":memory:",
            config={
                "memory_limit": f"{self.limits.max_memory_mb}MB",
                "max_temp_directory_size": f"{self.limits.max_temp_bytes}B",
                "temp_directory": str(temp_directory),
            },
        )
        self.connection = BoundedConnection(raw_connection, self.limits.timeout_ms)
        self._counter = 0
        self._shape_cache: dict[tuple[str, bool], dict[str, Any]] = {}
        self._consumed_items = 0

    def close(self) -> None:
        self.connection.close()
        self._temporary.cleanup()

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def next_table_name(self, hint: str) -> str:
        self._counter += 1
        safe = _SAFE_NAME.sub("_", hint)[:40] or "data"
        return f"adt_{self._counter}_{safe}"

    def resolve_path(self, raw_path: str, base_dir: Path) -> Path:
        return resolve_resource_path(
            raw_path,
            base_dir,
            resource_root=self.resource_root,
            restricted_paths=self.restricted_paths,
        )

    def load_source(
        self,
        spec: dict[str, Any],
        name: str,
        base_dir: Path,
        limits: Limits,
    ) -> DataSet:
        if not isinstance(spec, dict):
            raise DataTransformerError(
                "E_SOURCE_INVALID", "source specification must be an object", {"source": name}
            )
        has_path = "path" in spec
        has_inline = "inline" in spec
        if has_path == has_inline:
            raise DataTransformerError(
                "E_SOURCE_INVALID",
                "source must contain exactly one of path or inline",
                {"source": name},
            )

        if has_inline:
            value = spec["inline"]
            self._check_structure(value, name, limits)
            value = normalize_source_numbers(value)
            byte_size = json_size(value)
            self._check_bytes(byte_size, name, limits)
            if "select" in spec:
                value = select_value(value, spec["select"])
            return self._dataset_from_value(
                value,
                name,
                spec.get("format", "json"),
                byte_size,
                spec.get("kind"),
                limits,
            )

        path = self.resolve_path(spec["path"], base_dir)
        if not path.exists():
            raise DataTransformerError(
                "E_SOURCE_NOT_FOUND", "source file does not exist", {"path": str(path)}
            )
        if not path.is_file():
            raise DataTransformerError(
                "E_SOURCE_INVALID", "source path is not a regular file", {"path": str(path)}
            )
        try:
            byte_size = path.stat().st_size
        except OSError as exc:
            raise DataTransformerError(
                "E_IO", "could not inspect source file", {"path": str(path)}
            ) from exc
        self._check_bytes(byte_size, name, limits)
        data_format = infer_format(path, spec.get("format"))

        if "select" in spec or data_format in {"json", "yaml"} or spec.get("kind") == "tree":
            value = self._read_tree(path, data_format, limits)
            if "select" in spec:
                value = select_value(value, spec["select"])
            return self._dataset_from_value(
                value, name, data_format, byte_size, spec.get("kind"), limits
            )

        return self._load_table_path(path, data_format, name, byte_size, limits)

    def _read_tree(self, path: Path, data_format: str, limits: Limits) -> Any:
        if data_format not in {"json", "yaml"}:
            raise DataTransformerError(
                "E_TYPE_MISMATCH",
                "selectors and tree mode require JSON or YAML",
                {"format": data_format},
            )
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise DataTransformerError(
                "E_IO", "could not read source as UTF-8", {"path": str(path)}
            ) from exc
        if len(text.encode("utf-8")) > limits.max_input_bytes:
            raise DataTransformerError("E_INPUT_TOO_LARGE", "source exceeds byte limit")
        value = (
            parse_json(text, str(path)) if data_format == "json" else parse_yaml(text, str(path))
        )
        self._check_structure(value, str(path), limits)
        return normalize_source_numbers(value)

    def _dataset_from_value(
        self,
        value: Any,
        name: str,
        data_format: str,
        byte_size: int,
        kind: str | None,
        limits: Limits,
    ) -> DataSet:
        inferred_table = isinstance(value, list) and all(isinstance(item, dict) for item in value)
        if kind == "tree" or (kind is None and not inferred_table):
            return DataSet("tree", name, data_format, byte_size, value=value)
        if kind not in {None, "table"}:
            raise DataTransformerError("E_SOURCE_INVALID", "unknown source kind", {"kind": kind})
        if isinstance(value, list):
            rows = value
        elif isinstance(value, dict):
            rows = [value]
        else:
            rows = [{"value": value}]
        return self.table_from_rows(
            rows, name, data_format, byte_size, limits, structure_checked=True
        )

    def table_from_rows(
        self,
        rows: Iterable[Any],
        name: str,
        data_format: str,
        byte_size: int,
        limits: Limits,
        empty_schema: dict[str, str] | None = None,
        structure_checked: bool = False,
    ) -> DataSet:
        table_name = self.next_table_name(name)
        temp_path = Path(self._temporary.name) / f"{table_name}.jsonl"
        row_count = 0
        column_types: dict[str, _JsonType | None] = {}
        encoded_bytes = 0
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                for row in rows:
                    row_count += 1
                    if row_count > limits.max_rows:
                        raise DataTransformerError(
                            "E_ROW_LIMIT",
                            "source exceeds row limit",
                            {"rows": row_count, "maximum": limits.max_rows},
                        )
                    normalized = row if isinstance(row, dict) else {"value": row}
                    for field, value in normalized.items():
                        key = str(field)
                        column_types[key] = _merge_json_type(
                            column_types.get(key), _infer_json_type(value, key), key
                        )
                    encoded = internal_json(normalized)
                    encoded_bytes += len(encoded.encode("utf-8")) + 1
                    if encoded_bytes > limits.max_temp_bytes:
                        raise DataTransformerError(
                            "E_TEMP_LIMIT",
                            "data operation exceeded the cumulative temporary-storage limit",
                            {"bytes": encoded_bytes, "maximum": limits.max_temp_bytes},
                        )
                    handle.write(encoded)
                    handle.write("\n")
        except DataTransformerError:
            temp_path.unlink(missing_ok=True)
            raise
        except (OSError, ValueError) as exc:
            temp_path.unlink(missing_ok=True)
            raise DataTransformerError("E_PARSE", "could not encode tabular input") from exc
        if row_count == 0:
            columns = [
                f"{quote_identifier(field)} {data_type}"
                for field, data_type in (empty_schema or {}).items()
            ]
            columns.append(f"{quote_identifier(INTERNAL_ORDER)} BIGINT")
            self.connection.execute(
                f"CREATE TABLE {quote_identifier(table_name)} ({', '.join(columns)})"
            )
            temp_path.unlink(missing_ok=True)
            return DataSet("table", name, data_format, byte_size, table_name=table_name)
        has_exact_decimal = any(
            _json_type_has_decimal(data_type) for data_type in column_types.values()
        )
        explicit_types = {
            field: _json_type_sql(data_type, field)
            for field, data_type in column_types.items()
        }
        if has_exact_decimal:
            _quote_decimal_values(temp_path, limits)
        columns_sql = ", ".join(
            f"'{field.replace(chr(39), chr(39) * 2)}': "
            f"'{data_type.replace(chr(39), chr(39) * 2)}'"
            for field, data_type in explicit_types.items()
        )
        reader = f"read_json(?, format='newline_delimited', columns={{{columns_sql}}})"
        try:
            return self._create_table_from_reader(
                name,
                data_format,
                byte_size,
                table_name,
                reader,
                [str(temp_path)],
                limits,
                declared_schema=None,
                structure_checked=structure_checked,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    def _load_table_path(
        self,
        path: Path,
        data_format: str,
        name: str,
        byte_size: int,
        limits: Limits,
    ) -> DataSet:
        table_name = self.next_table_name(name)
        if data_format in {"csv", "tsv"}:
            return self._load_delimited_path(
                path, data_format, name, byte_size, limits
            )
        if data_format == "json":
            reader = "read_json_auto(?, format='array')"
        elif data_format == "jsonl":
            return self._load_jsonl_path(path, name, byte_size, limits)
        elif data_format == "parquet":
            reader = "read_parquet(?)"
        else:
            raise DataTransformerError(
                "E_FORMAT_UNSUPPORTED", "format is not a tabular input", {"format": data_format}
            )
        return self._create_table_from_reader(
            name, data_format, byte_size, table_name, reader, [str(path)], limits
        )

    def _load_jsonl_path(
        self,
        path: Path,
        name: str,
        byte_size: int,
        limits: Limits,
    ) -> DataSet:
        def rows() -> Iterator[Any]:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        value = parse_json(line, f"{path}:{line_number}")
                        self._check_structure(value, name, limits)
                        yield normalize_source_numbers(value)
            except DataTransformerError:
                raise
            except (OSError, UnicodeDecodeError) as exc:
                raise DataTransformerError(
                    "E_IO", "could not read JSONL source", {"source": name}
                ) from exc

        return self.table_from_rows(
            rows(), name, "jsonl", byte_size, limits, structure_checked=True
        )

    def _load_delimited_path(
        self,
        path: Path,
        data_format: str,
        name: str,
        byte_size: int,
        limits: Limits,
    ) -> DataSet:
        delimiter = "," if data_format == "csv" else "\t"
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle, delimiter=delimiter, strict=True)
                header = next(reader, None)
                if header is None:
                    raise DataTransformerError("E_PARSE", "delimited input has no header")
                if any(not field for field in header) or len(header) != len(set(header)):
                    raise DataTransformerError(
                        "E_DUPLICATE_FIELD",
                        "delimited input requires unique non-empty headers",
                        {"headers": header},
                    )
                if len(header) > limits.max_items:
                    raise DataTransformerError(
                        "E_ITEM_LIMIT",
                        "delimited header exceeds the structural item limit",
                        {"columns": len(header), "maximum": limits.max_items},
                    )
                raw_rows: list[list[str | None]] = []
                for row_number, row in enumerate(reader, start=2):
                    if len(row) != len(header):
                        raise DataTransformerError(
                            "E_PARSE",
                            "delimited row width does not match the header",
                            {"row": row_number, "expected": len(header), "actual": len(row)},
                        )
                    raw_rows.append([_decode_delimited_value(value) for value in row])
                    if len(raw_rows) > limits.max_rows:
                        raise DataTransformerError(
                            "E_ROW_LIMIT",
                            "source exceeds row limit",
                            {"rows": len(raw_rows), "maximum": limits.max_rows},
                        )
        except DataTransformerError:
            raise
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise DataTransformerError(
                "E_PARSE", "could not parse delimited source", {"source": name}
            ) from exc
        converters = [
            _delimited_converter(row[index] for row in raw_rows)
            for index in range(len(header))
        ]
        rows = (
            {
                field: converters[index](row[index])
                for index, field in enumerate(header)
            }
            for row in raw_rows
        )
        schema = {field: converters[index].sql_type for index, field in enumerate(header)}
        return self.table_from_rows(
            rows, name, data_format, byte_size, limits, empty_schema=schema
        )

    def _create_table_from_reader(
        self,
        name: str,
        data_format: str,
        byte_size: int,
        table_name: str,
        reader: str,
        parameters: list[Any],
        limits: Limits,
        *,
        declared_schema: dict[str, str] | None = None,
        structure_checked: bool = False,
    ) -> DataSet:
        raw_name = f"{table_name}_raw"
        try:
            self.connection.execute(
                f"CREATE TABLE {quote_identifier(raw_name)} AS SELECT * FROM {reader}", parameters
            )
            raw_columns = self.columns(raw_name, include_internal=True)
            if INTERNAL_ORDER in raw_columns:
                raise DataTransformerError(
                    "E_RESERVED_FIELD",
                    "source contains a reserved internal field",
                    {"field": INTERNAL_ORDER},
                )
            row_count = self.row_count(raw_name)
            if row_count > limits.max_rows:
                raise DataTransformerError(
                    "E_ROW_LIMIT",
                    "source exceeds row limit",
                    {"rows": row_count, "maximum": limits.max_rows},
                )
            if row_count == 0:
                self.connection.execute(
                    f"CREATE TABLE {quote_identifier(table_name)} AS SELECT *, "
                    f"CAST(NULL AS BIGINT) AS {quote_identifier(INTERNAL_ORDER)} "
                    f"FROM {quote_identifier(raw_name)} WHERE false"
                )
            else:
                self.connection.execute(
                    f"CREATE TABLE {quote_identifier(table_name)} AS "
                    f"SELECT *, row_number() OVER () - 1 AS {quote_identifier(INTERNAL_ORDER)} "
                    f"FROM {quote_identifier(raw_name)}"
                )
            self._apply_declared_schema(table_name, declared_schema or {})
            self.connection.execute(f"DROP TABLE {quote_identifier(raw_name)}")
        except DataTransformerError:
            self.connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(raw_name)}")
            raise
        except duckdb.Error as exc:
            self.connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(raw_name)}")
            raise DataTransformerError(
                "E_PARSE", "could not parse tabular source", {"source": name, "format": data_format}
            ) from exc
        if not structure_checked:
            self._enforce_loaded_structure(table_name, name, limits)
        self.check_temp_budget()
        return DataSet("table", name, data_format, byte_size, table_name=table_name)

    def _apply_declared_schema(self, table_name: str, schema: dict[str, str]) -> None:
        table = quote_identifier(table_name)
        for field, data_type in schema.items():
            self.connection.execute(
                f"ALTER TABLE {table} ALTER {quote_identifier(field)} TYPE {data_type}"
            )

    def _enforce_loaded_structure(
        self, table_name: str, name: str, limits: Limits
    ) -> None:
        """Charge the structural item/depth budget once for a loaded table.

        Rows that were already structure-checked by the producing layer pass
        ``structure_checked`` and skip this entirely. Columns that can hold
        containers are walked once in Python; scalar tables are accounted
        analytically without materializing rows.
        """
        types = self.column_types(table_name)
        rows = self.row_count(table_name)
        if not rows:
            if len(types) > limits.max_items:
                raise DataTransformerError(
                    "E_ITEM_LIMIT",
                    "schema-bearing source exceeds the structural item limit",
                    {
                        "source": name,
                        "items": len(types),
                        "maximum": limits.max_items,
                    },
                )
            return
        nested = any(
            data_type.upper().split("(", 1)[0].strip() in _NESTED_TYPE_BASES
            or "[]" in data_type
            for data_type in types.values()
        )
        if nested:
            dataset = DataSet("table", name, "", 0, table_name=table_name)
            for row in self.iter_rows(dataset):
                self._check_structure(row, name, limits)
            return
        if limits.max_depth < 2:
            raise DataTransformerError(
                "E_DEPTH_LIMIT",
                "source exceeds nesting depth limit",
                {"source": name, "maximum": limits.max_depth},
            )
        self._consumed_items += rows * (len(types) + 1)
        if self._consumed_items > limits.max_items:
            raise DataTransformerError(
                "E_ITEM_LIMIT",
                "combined sources exceed the cumulative structural item limit",
                {"source": name, "maximum": limits.max_items},
            )

    def columns(self, table_name: str, include_internal: bool = False) -> list[str]:
        rows = self.connection.execute(
            f"PRAGMA table_info({quote_identifier(table_name)})"
        ).fetchall()
        columns = [row[1] for row in rows]
        if not include_internal:
            columns = [column for column in columns if column != INTERNAL_ORDER]
        return columns

    def column_types(self, table_name: str) -> dict[str, str]:
        rows = self.connection.execute(
            f"PRAGMA table_info({quote_identifier(table_name)})"
        ).fetchall()
        return {row[1]: row[2] for row in rows if row[1] != INTERNAL_ORDER}

    def field_type(self, dataset: DataSet, field: str) -> str:
        if not dataset.is_table or dataset.table_name is None:
            raise DataTransformerError("E_TYPE_MISMATCH", "field typing requires a table")
        row = self.connection.execute(
            f"DESCRIBE SELECT {field_expression(field)} AS value "
            f"FROM {quote_identifier(dataset.table_name)}"
        ).fetchone()
        if row is None:
            raise DataTransformerError("E_FIELD_NOT_FOUND", "field does not exist")
        return str(row[1])

    def expression_type(
        self,
        dataset: DataSet,
        sql: str,
        parameters: list[Any],
    ) -> str:
        if not dataset.is_table or dataset.table_name is None:
            raise DataTransformerError("E_TYPE_MISMATCH", "expression typing requires a table")
        row = self.connection.execute(
            f"DESCRIBE SELECT {sql} AS value FROM {quote_identifier(dataset.table_name)}",
            parameters,
        ).fetchone()
        if row is None:
            raise DataTransformerError("E_EXPRESSION_INVALID", "expression has no result type")
        return str(row[1])

    def expression_has_non_null(
        self,
        dataset: DataSet,
        sql: str,
        parameters: list[Any],
    ) -> bool:
        if not dataset.is_table or dataset.table_name is None:
            raise DataTransformerError("E_TYPE_MISMATCH", "expression typing requires a table")
        row = self.connection.execute(
            f"SELECT count(*) FILTER (WHERE ({sql}) IS NOT NULL) "
            f"FROM {quote_identifier(dataset.table_name)}",
            parameters,
        ).fetchone()
        return bool(row and row[0])

    def row_count(self, table_name: str) -> int:
        return int(
            self.connection.execute(
                f"SELECT count(*) FROM {quote_identifier(table_name)}"
            ).fetchone()[0]
        )

    def rows(self, dataset: DataSet, limit: int | None = None) -> list[dict[str, Any]]:
        return list(self.iter_rows(dataset, limit=limit))

    def iter_rows(
        self,
        dataset: DataSet,
        limit: int | None = None,
        batch_size: int = 1000,
        *,
        safe: Callable[[Any], Any] = json_safe,
    ) -> Iterator[dict[str, Any]]:
        if not dataset.is_table or dataset.table_name is None:
            raise DataTransformerError("E_TYPE_MISMATCH", "operation requires a table")
        columns = self.columns(dataset.table_name)
        select_list = ", ".join(quote_identifier(column) for column in columns) or "NULL"
        query = (
            f"SELECT {select_list} FROM {quote_identifier(dataset.table_name)} "
            f"ORDER BY {quote_identifier(INTERNAL_ORDER)}"
        )
        parameters: list[Any] = []
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        cursor = self.connection.execute(query, parameters)
        while True:
            records = cursor.fetchmany(batch_size)
            if not records:
                break
            for record in records:
                if not columns:
                    yield {}
                else:
                    yield {
                        column: safe(value)
                        for column, value in zip(columns, record, strict=True)
                    }

    def shape(
        self,
        dataset: DataSet,
        sample_rows: int = 0,
        *,
        include_quality: bool = False,
    ) -> dict[str, Any]:
        if dataset.is_tree:
            return self._tree_shape(dataset.value, sample_rows)
        if dataset.table_name is None:
            raise DataTransformerError("E_INTERNAL", "table dataset has no table name")
        cache_key = (dataset.table_name, include_quality)
        cached = self._shape_cache.get(cache_key)
        if cached is not None:
            result = {**cached, "fields": dict(cached["fields"])}
            if sample_rows:
                result["sample"] = self.rows(dataset, sample_rows)
            return result
        columns = self.columns(dataset.table_name)
        types = self.column_types(dataset.table_name)
        row_count = self.row_count(dataset.table_name)
        fields: dict[str, Any] = {column: {"type": types[column].lower()} for column in columns}
        if columns and include_quality:
            # One aggregate per column in a single query makes DuckDB allocate
            # per-aggregate state that explodes on wide tables, so null counts
            # are collected in bounded column chunks.
            null_counts: dict[str, int] = {}
            for start in range(0, len(columns), _QUALITY_COLUMN_CHUNK):
                chunk = columns[start : start + _QUALITY_COLUMN_CHUNK]
                null_sql = ", ".join(
                    f"count(*) FILTER (WHERE {quote_identifier(column)} IS NULL)"
                    for column in chunk
                )
                counts = self.connection.execute(
                    f"SELECT {null_sql} FROM {quote_identifier(dataset.table_name)}"
                ).fetchone()
                null_counts.update(zip(chunk, counts, strict=True))
            for column, null_count in null_counts.items():
                fields[column].update(
                    {"nullable": bool(null_count), "null_count": int(null_count)}
                )
        result: dict[str, Any] = {
            "kind": "table",
            "rows": row_count,
            "columns": len(columns),
            "fields": fields,
        }
        if sample_rows:
            result["sample"] = self.rows(dataset, sample_rows)
        self._shape_cache[cache_key] = {
            key: (dict(value) if key == "fields" else value)
            for key, value in result.items()
            if key != "sample"
        }
        return result

    def _tree_shape(self, value: Any, sample_rows: int) -> dict[str, Any]:
        type_name = _tree_type(value)
        result: dict[str, Any] = {"kind": "tree", "type": type_name}
        if isinstance(value, dict):
            result["fields"] = {
                str(key): {"type": _tree_type(item), "nullable": item is None}
                for key, item in value.items()
            }
            result["field_count"] = len(value)
            record_sets, record_sets_truncated = _discover_record_sets(value)
            if record_sets:
                result["record_sets"] = record_sets
            if record_sets_truncated:
                result["record_sets_truncated"] = True
        elif isinstance(value, list):
            result["items"] = len(value)
        if sample_rows:
            sample, sample_truncated = _bounded_tree_sample(value, sample_rows)
            result["sample"] = sample
            if sample_truncated:
                result["sample_truncated"] = True
        return result

    def _check_bytes(self, byte_size: int, source: str, limits: Limits) -> None:
        if byte_size > limits.max_input_bytes:
            raise DataTransformerError(
                "E_INPUT_TOO_LARGE",
                "source exceeds byte limit",
                {"source": source, "bytes": byte_size, "maximum": limits.max_input_bytes},
            )

    def _check_structure(
        self,
        value: Any,
        source: str,
        limits: Limits,
        *,
        charge_source_budget: bool = True,
    ) -> None:
        pending: list[tuple[Any, int]] = [(value, 1)]
        containers: set[int] = set()
        items = 0
        while pending:
            current, depth = pending.pop()
            items += 1
            if charge_source_budget:
                self._consumed_items += 1
                if self._consumed_items > limits.max_items:
                    raise DataTransformerError(
                        "E_ITEM_LIMIT",
                        "combined sources exceed the cumulative structural item limit",
                        {"source": source, "maximum": limits.max_items},
                    )
            elif items > limits.max_items:
                raise DataTransformerError(
                    "E_ITEM_LIMIT",
                    "generated output exceeds the structural item limit",
                    {"source": source, "maximum": limits.max_items},
                )
            if depth > limits.max_depth:
                raise DataTransformerError(
                    "E_DEPTH_LIMIT",
                    "source exceeds nesting depth limit",
                    {"source": source, "maximum": limits.max_depth},
                )
            if isinstance(current, dict):
                identity = id(current)
                if identity in containers:
                    raise DataTransformerError(
                        "E_SOURCE_INVALID", "source contains a cycle or repeated container"
                    )
                containers.add(identity)
                for key in current:
                    if not isinstance(key, str):
                        raise DataTransformerError(
                            "E_NON_STRING_KEY",
                            "structured-data object keys must be strings",
                            {"source": source, "key_type": type(key).__name__},
                        )
                pending.extend((item, depth + 1) for item in current.values())
            elif isinstance(current, list):
                identity = id(current)
                if identity in containers:
                    raise DataTransformerError(
                        "E_SOURCE_INVALID", "source contains a cycle or repeated container"
                    )
                containers.add(identity)
                pending.extend((item, depth + 1) for item in current)
        return items

    def check_structure(self, value: Any, label: str) -> None:
        self._check_structure(
            value, label, self.limits, charge_source_budget=False
        )

    def check_dataset_structure(self, dataset: DataSet, label: str) -> None:
        """Enforce the structural limit on one generated step result.

        Source structures are charged cumulatively when they are loaded. Step
        results are checked independently so shape-preserving operations do not
        spend the same budget repeatedly, while fan-out still cannot create a
        result larger than ``max_items``.
        """
        if dataset.is_tree:
            self.check_structure(dataset.value, label)
            return
        if dataset.table_name is None:
            raise DataTransformerError("E_INTERNAL", "table has no backing relation")

        types = self.column_types(dataset.table_name)
        rows = self.row_count(dataset.table_name)
        nested = any(
            data_type.upper().split("(", 1)[0].strip() in _NESTED_TYPE_BASES
            or "[]" in data_type
            for data_type in types.values()
        )
        if not nested:
            if rows and self.limits.max_depth < 2:
                raise DataTransformerError(
                    "E_DEPTH_LIMIT",
                    "generated output exceeds the nesting depth limit",
                    {"source": label, "maximum": self.limits.max_depth},
                )
            items = rows * (len(types) + 1) if rows else len(types)
            if items > self.limits.max_items:
                raise DataTransformerError(
                    "E_ITEM_LIMIT",
                    "generated output exceeds the structural item limit",
                    {
                        "source": label,
                        "items": items,
                        "maximum": self.limits.max_items,
                    },
                )
            return

        items = 0
        for row in self.iter_rows(dataset):
            items += self._check_structure(
                row,
                label,
                self.limits,
                charge_source_budget=False,
            )
            if items > self.limits.max_items:
                raise DataTransformerError(
                    "E_ITEM_LIMIT",
                    "generated output exceeds the structural item limit",
                    {
                        "source": label,
                        "items": items,
                        "maximum": self.limits.max_items,
                    },
                )

    def check_temp_budget(self, extra_paths: Iterable[Path] = ()) -> None:
        paths = [path for path in Path(self._temporary.name).rglob("*") if path.is_file()]
        paths.extend(path for path in extra_paths if path.exists() and path.is_file())
        total = 0
        for path in {path.resolve() for path in paths}:
            try:
                total += path.stat().st_size
            except OSError as exc:
                raise DataTransformerError("E_IO", "could not account temporary storage") from exc
        if total > self.limits.max_temp_bytes:
            raise DataTransformerError(
                "E_TEMP_LIMIT",
                "data operation exceeded the cumulative temporary-storage limit",
                {"bytes": total, "maximum": self.limits.max_temp_bytes},
            )


def _infer_json_type(value: Any, path: str) -> _JsonType | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return _JsonType("boolean")
    if isinstance(value, int):
        digits = len(str(abs(value))) if value else 1
        return _JsonType(
            "integer",
            minimum_integer=value,
            maximum_integer=value,
            integer_digits=digits,
        )
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise DataTransformerError(
                "E_NUMBER_INVALID", "non-finite numbers are not valid structured data"
            )
        _, digits, exponent = value.as_tuple()
        scale = max(-exponent, 0)
        integer_digits = max(len(digits) + exponent, 0)
        return _JsonType("decimal", integer_digits=integer_digits, scale=scale)
    if isinstance(value, float):
        return _JsonType("float")
    if isinstance(value, str):
        return _JsonType("string")
    if isinstance(value, dict):
        return _JsonType(
            "object",
            fields={
                str(field): _infer_json_type(item, f"{path}.{field}")
                for field, item in value.items()
            },
        )
    if isinstance(value, (list, tuple)):
        element: _JsonType | None = None
        for index, item in enumerate(value):
            element = _merge_json_type(
                element, _infer_json_type(item, f"{path}[{index}]"), f"{path}[]"
            )
        return _JsonType("array", element=element)
    return _infer_json_type(json_safe(value), path)


def _merge_json_type(
    left: _JsonType | None, right: _JsonType | None, path: str
) -> _JsonType | None:
    if left is None:
        return right
    if right is None:
        return left
    if left.kind == right.kind:
        if left.kind == "integer":
            return _JsonType(
                "integer",
                minimum_integer=min(left.minimum_integer or 0, right.minimum_integer or 0),
                maximum_integer=max(left.maximum_integer or 0, right.maximum_integer or 0),
                integer_digits=max(left.integer_digits, right.integer_digits),
            )
        if left.kind == "decimal":
            return _JsonType(
                "decimal",
                integer_digits=max(left.integer_digits, right.integer_digits),
                scale=max(left.scale, right.scale),
            )
        if left.kind == "object":
            fields = dict(left.fields)
            for field, data_type in right.fields.items():
                fields[field] = _merge_json_type(
                    fields.get(field), data_type, f"{path}.{field}"
                )
            return _JsonType("object", fields=fields)
        if left.kind == "array":
            return _JsonType(
                "array",
                element=_merge_json_type(left.element, right.element, f"{path}[]"),
            )
        return left
    if {left.kind, right.kind} == {"integer", "decimal"}:
        integer = left if left.kind == "integer" else right
        exact = left if left.kind == "decimal" else right
        return _JsonType(
            "decimal",
            integer_digits=max(integer.integer_digits, exact.integer_digits),
            scale=exact.scale,
        )
    raise DataTransformerError(
        "E_TYPE_MISMATCH",
        "tabular fields cannot mix incompatible value types; use tree data or cast explicitly",
        {"field": path, "left_type": left.kind, "right_type": right.kind},
    )


def _json_type_has_decimal(data_type: _JsonType | None) -> bool:
    if data_type is None:
        return False
    if data_type.kind == "decimal":
        return True
    if data_type.kind == "object":
        return any(_json_type_has_decimal(item) for item in data_type.fields.values())
    if data_type.kind == "array":
        return _json_type_has_decimal(data_type.element)
    return False


def _json_type_sql(data_type: _JsonType | None, path: str) -> str:
    if data_type is None:
        return "VARCHAR"
    if data_type.kind == "boolean":
        return "BOOLEAN"
    if data_type.kind == "string":
        return "VARCHAR"
    if data_type.kind == "float":
        return "DOUBLE"
    if data_type.kind == "integer":
        if (
            data_type.minimum_integer is not None
            and data_type.maximum_integer is not None
            and _INT64_MIN <= data_type.minimum_integer <= data_type.maximum_integer <= _INT64_MAX
        ):
            return "BIGINT"
        precision = data_type.integer_digits
        if precision > 38:
            raise DataTransformerError(
                "E_NUMBER_PRECISION",
                "exact integer exceeds the runtime precision limit",
                {"field": path, "precision": precision, "maximum": 38},
            )
        return f"DECIMAL({precision},0)"
    if data_type.kind == "decimal":
        precision = data_type.integer_digits + data_type.scale
        if precision > 38 or data_type.scale > 38:
            raise DataTransformerError(
                "E_NUMBER_PRECISION",
                "exact decimal exceeds the runtime precision limit",
                {
                    "field": path,
                    "precision": precision,
                    "scale": data_type.scale,
                    "maximum": 38,
                },
            )
        return f"DECIMAL({precision},{data_type.scale})"
    if data_type.kind == "object":
        if not data_type.fields:
            return "MAP(VARCHAR, JSON)"
        fields = ", ".join(
            f"{quote_identifier(field)} {_json_type_sql(item, f'{path}.{field}')}"
            for field, item in data_type.fields.items()
        )
        return f"STRUCT({fields})"
    if data_type.kind == "array":
        return f"{_json_type_sql(data_type.element, f'{path}[]')}[]"
    raise DataTransformerError(
        "E_TYPE_MISMATCH", "tabular field has an unsupported value type", {"field": path}
    )


def _stringify_decimals(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        # Fixed-point keeps sub-1e-7 magnitudes castable to DECIMAL; str() would
        # emit scientific notation the ingestion boundary rejects.
        return format(value, "f")
    if isinstance(value, dict):
        return {field: _stringify_decimals(item) for field, item in value.items()}
    if isinstance(value, list):
        return [_stringify_decimals(item) for item in value]
    return value


def _quote_decimal_values(path: Path, limits: Limits) -> None:
    """Quote every JSON decimal before explicit nested DECIMAL ingestion."""
    rewritten = path.with_name(path.name + ".quoted")
    try:
        total = path.stat().st_size
        with (
            path.open("r", encoding="utf-8") as source,
            rewritten.open("w", encoding="utf-8", newline="\n") as target,
        ):
            for line in source:
                encoded = internal_json(_stringify_decimals(parse_json(line, str(path))))
                total += len(encoded.encode("utf-8")) + 1
                if total > limits.max_temp_bytes:
                    raise DataTransformerError(
                        "E_TEMP_LIMIT",
                        "data operation exceeded the cumulative temporary-storage limit",
                        {"bytes": total, "maximum": limits.max_temp_bytes},
                    )
                target.write(encoded)
                target.write("\n")
        rewritten.replace(path)
    except DataTransformerError:
        rewritten.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as exc:
        rewritten.unlink(missing_ok=True)
        raise DataTransformerError("E_PARSE", "could not encode tabular input") from exc


class _DelimitedConverter:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.sql_type = {
            "integer": "BIGINT",
            "decimal": "DECIMAL(38, 18)",
            "boolean": "BOOLEAN",
            "string": "VARCHAR",
        }[kind]

    def __call__(self, value: str | None) -> Any:
        if value is None:
            return None
        if self.kind == "integer" and value:
            return int(value)
        if self.kind == "decimal" and value:
            return decimal.Decimal(value)
        if self.kind == "boolean" and value:
            return value.lower() == "true"
        return value


def _delimited_converter(values: Iterable[str | None]) -> _DelimitedConverter:
    present = [value for value in values if value not in {None, ""}]
    if present and all(re.fullmatch(r"[+-]?(?:0|[1-9][0-9]*)", value) for value in present):
        # Deterministic inference: integers that overflow BIGINT keep their
        # exact digits through the decimal path instead of failing the cast.
        if all(_INT64_MIN <= int(value) <= _INT64_MAX for value in present):
            return _DelimitedConverter("integer")
        return _DelimitedConverter("decimal")
    if present and all(
        re.fullmatch(
            r"[+-]?(?:(?:0|[1-9][0-9]*)\.[0-9]+|"
            r"(?:0|[1-9][0-9]*)[eE][+-]?[0-9]+)",
            value,
        )
        for value in present
    ):
        return _DelimitedConverter("decimal")
    if present and all(value.lower() in {"true", "false"} for value in present):
        return _DelimitedConverter("boolean")
    return _DelimitedConverter("string")


def _decode_delimited_value(value: str) -> str | None:
    if value == r"\N":
        return None
    if value.startswith("\\\\"):
        return value[1:]
    return value


def _bounded_tree_sample(value: Any, item_limit: int) -> tuple[Any, bool]:
    def visit(current: Any, depth: int) -> tuple[Any, bool]:
        if isinstance(current, list):
            if depth >= _TREE_SAMPLE_DEPTH_CAP:
                return [], bool(current)
            selected = current[:item_limit]
            values: list[Any] = []
            truncated = len(current) > len(selected)
            for item in selected:
                preview, child_truncated = visit(item, depth + 1)
                values.append(preview)
                truncated = truncated or child_truncated
            return values, truncated
        if isinstance(current, dict):
            if depth >= _TREE_SAMPLE_DEPTH_CAP:
                return {}, bool(current)
            field_limit = item_limit if depth == 0 else _TREE_SAMPLE_FIELD_CAP
            selected = list(current.items())[:field_limit]
            values: dict[str, Any] = {}
            truncated = len(current) > len(selected)
            for key, item in selected:
                preview, child_truncated = visit(item, depth + 1)
                values[str(key)] = preview
                truncated = truncated or child_truncated
            return values, truncated
        return json_safe(current), False

    return visit(value, 0)


def _discover_record_sets(value: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    candidates: list[dict[str, Any]] = []
    truncated = False

    def walk(current: dict[str, Any], segments: list[str]) -> None:
        nonlocal truncated
        for key in sorted(current):
            item = current[key]
            path = [*segments, key]
            if isinstance(item, list) and all(isinstance(row, dict) for row in item):
                if len(candidates) >= _RECORD_SET_CAP:
                    truncated = True
                    continue
                candidates.append(_record_set_profile(item, path))
            elif isinstance(item, dict):
                walk(item, path)

    walk(value, [])
    return candidates, truncated


def _record_set_profile(rows: list[dict[str, Any]], segments: list[str]) -> dict[str, Any]:
    names = sorted({str(field) for row in rows for field in row})
    fields: dict[str, Any] = {}
    field_budget = [0]
    for name in names:
        if field_budget[0] >= SHAPE_FIELD_CAP:
            break
        field_budget[0] += 1
        present = [row[name] for row in rows if name in row]
        fields[name] = _tree_field_profile(present, len(rows), 1, field_budget)
    selectable = all(_SELECTOR_NAME.fullmatch(segment) for segment in segments)
    result: dict[str, Any] = {
        "path": "/" + "/".join(_json_pointer_segment(segment) for segment in segments),
        "select": ".".join(segments) + "[*]" if selectable else None,
        "selectable": selectable,
        "rows": len(rows),
        "columns": len(names),
        "fields": fields,
    }
    if len(names) > SHAPE_FIELD_CAP:
        result["fields_truncated"] = True
    return result


def _tree_field_profile(
    present: list[Any],
    total: int,
    depth: int,
    field_budget: list[int],
) -> dict[str, Any]:
    types = sorted({_tree_type(item) for item in present})
    profile: dict[str, Any] = {
        "type": types[0] if len(types) == 1 else "mixed",
        "nullable": len(present) < total or any(item is None for item in present),
    }
    if len(types) > 1:
        profile["types"] = types
    missing = total - len(present)
    if missing:
        profile["missing_count"] = missing
    null_count = sum(item is None for item in present)
    if null_count:
        profile["null_count"] = null_count
    non_null = [item for item in present if item is not None]
    non_null_types = {_tree_type(item) for item in non_null}
    if depth >= _TREE_SAMPLE_DEPTH_CAP:
        if any(isinstance(item, (dict, list)) for item in non_null):
            profile["nested_truncated"] = True
        return profile
    if non_null_types == {"object"}:
        objects = [item for item in non_null if isinstance(item, dict)]
        names = sorted({str(field) for item in objects for field in item})
        nested: dict[str, Any] = {}
        for name in names:
            if field_budget[0] >= SHAPE_FIELD_CAP:
                break
            field_budget[0] += 1
            values = [item[name] for item in objects if name in item]
            nested[name] = _tree_field_profile(
                values, len(objects), depth + 1, field_budget
            )
        profile["field_count"] = len(names)
        profile["fields"] = nested
        if len(nested) < len(names):
            profile["fields_truncated"] = True
    elif non_null_types == {"array"}:
        arrays = [item for item in non_null if isinstance(item, list)]
        lengths = [len(item) for item in arrays]
        elements = [element for item in arrays for element in item]
        profile["items_min"] = min(lengths, default=0)
        profile["items_max"] = max(lengths, default=0)
        if elements:
            profile["items"] = _tree_field_profile(
                elements, len(elements), depth + 1, field_budget
            )
    return profile


def _json_pointer_segment(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _tree_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, decimal.Decimal):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


class BoundedConnection:
    """DuckDB connection proxy with a wall-clock interrupt around each statement."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, timeout_ms: int) -> None:
        self._connection = connection
        self._timeout_seconds = timeout_ms / 1000

    def execute(self, query: str, parameters: list[Any] | None = None) -> duckdb.DuckDBPyConnection:
        finished = threading.Event()

        def interrupt() -> None:
            if not finished.is_set():
                self._connection.interrupt()

        timer = threading.Timer(self._timeout_seconds, interrupt)
        timer.daemon = True
        timer.start()
        try:
            return self._connection.execute(query, parameters or [])
        except duckdb.InterruptException as exc:
            raise DataTransformerError(
                "E_TIMEOUT",
                "data operation exceeded the wall-clock limit",
                {"timeout_ms": int(self._timeout_seconds * 1000)},
            ) from exc
        finally:
            finished.set()
            timer.cancel()

    def close(self) -> None:
        self._connection.close()
