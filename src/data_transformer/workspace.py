from __future__ import annotations

import csv
import decimal
import re
import tempfile
import threading
from collections.abc import Iterable, Iterator
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
        return self.table_from_rows(rows, name, data_format, byte_size, limits)

    def table_from_rows(
        self,
        rows: Iterable[Any],
        name: str,
        data_format: str,
        byte_size: int,
        limits: Limits,
        empty_schema: dict[str, str] | None = None,
    ) -> DataSet:
        table_name = self.next_table_name(name)
        temp_path = Path(self._temporary.name) / f"{table_name}.jsonl"
        row_count = 0
        exact_decimals: dict[str, list[tuple[int, decimal.Decimal]]] = {}
        numeric_kinds: dict[str, set[str]] = {}
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
                        if isinstance(value, decimal.Decimal):
                            exact_decimals.setdefault(str(field), []).append(
                                (row_count - 1, value)
                            )
                            numeric_kinds.setdefault(str(field), set()).add("decimal")
                        elif isinstance(value, float):
                            numeric_kinds.setdefault(str(field), set()).add("float")
                        elif isinstance(value, int) and not isinstance(value, bool):
                            numeric_kinds.setdefault(str(field), set()).add("integer")
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
        try:
            return self._create_table_from_reader(
                name,
                data_format,
                byte_size,
                table_name,
                "read_json_auto(?, format='newline_delimited')",
                [str(temp_path)],
                limits,
                exact_decimals=exact_decimals,
                numeric_kinds=numeric_kinds,
                declared_schema=empty_schema,
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

        return self.table_from_rows(rows(), name, "jsonl", byte_size, limits)

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
        exact_decimals: dict[str, list[tuple[int, decimal.Decimal]]] | None = None,
        numeric_kinds: dict[str, set[str]] | None = None,
        declared_schema: dict[str, str] | None = None,
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
            self._restore_exact_decimals(
                table_name,
                exact_decimals or {},
                numeric_kinds or {},
            )
            self.connection.execute(f"DROP TABLE {quote_identifier(raw_name)}")
        except DataTransformerError:
            self.connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(raw_name)}")
            raise
        except duckdb.Error as exc:
            self.connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(raw_name)}")
            raise DataTransformerError(
                "E_PARSE", "could not parse tabular source", {"source": name, "format": data_format}
            ) from exc
        return DataSet("table", name, data_format, byte_size, table_name=table_name)

    def _apply_declared_schema(self, table_name: str, schema: dict[str, str]) -> None:
        table = quote_identifier(table_name)
        for field, data_type in schema.items():
            self.connection.execute(
                f"ALTER TABLE {table} ALTER {quote_identifier(field)} TYPE {data_type}"
            )

    def _restore_exact_decimals(
        self,
        table_name: str,
        exact_decimals: dict[str, list[tuple[int, decimal.Decimal]]],
        numeric_kinds: dict[str, set[str]],
    ) -> None:
        for field, cells in exact_decimals.items():
            if "float" in numeric_kinds.get(field, set()):
                raise DataTransformerError(
                    "E_TYPE_MISMATCH",
                    "a field cannot mix exact decimals with binary floating-point values",
                    {"field": field},
                )
            precision = 1
            scale = 0
            for _, value in cells:
                sign, digits, exponent = value.as_tuple()
                del sign
                value_scale = max(-exponent, 0)
                integer_digits = max(len(digits) + exponent, 0)
                precision = max(precision, integer_digits + value_scale)
                scale = max(scale, value_scale)
            for kind in numeric_kinds.get(field, set()):
                if kind == "integer":
                    precision = max(precision, 19 + scale)
            if precision > 38 or scale > 38:
                raise DataTransformerError(
                    "E_NUMBER_PRECISION",
                    "exact decimal exceeds the runtime precision limit",
                    {"field": field, "precision": precision, "scale": scale, "maximum": 38},
                )
            column = quote_identifier(field)
            table = quote_identifier(table_name)
            self.connection.execute(
                f"ALTER TABLE {table} ALTER {column} TYPE DECIMAL({precision}, {scale})"
            )
            for order, value in cells:
                self.connection.execute(
                    f"UPDATE {table} SET {column} = ? "
                    f"WHERE {quote_identifier(INTERNAL_ORDER)} = ?",
                    [value, order],
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
        self, dataset: DataSet, limit: int | None = None, batch_size: int = 1000
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
                        column: json_safe(value)
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
            null_sql = ", ".join(
                f"count(*) FILTER (WHERE {quote_identifier(column)} IS NULL)" for column in columns
            )
            null_counts = self.connection.execute(
                f"SELECT {null_sql} FROM {quote_identifier(dataset.table_name)}"
            ).fetchone()
            for column, null_count in zip(columns, null_counts, strict=True):
                fields[column].update({"nullable": bool(null_count), "null_count": int(null_count)})
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
        elif isinstance(value, list):
            result["items"] = len(value)
        if sample_rows:
            if isinstance(value, list):
                result["sample"] = json_safe(value[:sample_rows])
            else:
                result["sample"] = json_safe(value)
        return result

    def _check_bytes(self, byte_size: int, source: str, limits: Limits) -> None:
        if byte_size > limits.max_input_bytes:
            raise DataTransformerError(
                "E_INPUT_TOO_LARGE",
                "source exceeds byte limit",
                {"source": source, "bytes": byte_size, "maximum": limits.max_input_bytes},
            )

    def _check_structure(self, value: Any, source: str, limits: Limits) -> None:
        pending: list[tuple[Any, int]] = [(value, 1)]
        containers: set[int] = set()
        items = 0
        while pending:
            current, depth = pending.pop()
            items += 1
            self._consumed_items += 1
            if self._consumed_items > limits.max_items:
                raise DataTransformerError(
                    "E_ITEM_LIMIT",
                    "combined sources exceed the cumulative structural item limit",
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

    def check_dataset_structure(self, dataset: DataSet, label: str) -> None:
        if dataset.is_tree:
            self._check_structure(dataset.value, label, self.limits)
            return
        for row in self.iter_rows(dataset):
            self._check_structure(row, label, self.limits)

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
        return _DelimitedConverter("integer")
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
