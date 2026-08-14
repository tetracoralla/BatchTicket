from __future__ import annotations

import copy
import decimal
from collections.abc import Mapping
from typing import Any

import duckdb

from .dataset import DataSet
from .errors import DataTransformerError
from .expressions import compile_condition, compile_expression
from .limits import Limits
from .selectors import parse_selector
from .workspace import INTERNAL_ORDER, Workspace, field_expression, quote_identifier

_CAST_TYPES = {
    "string": "VARCHAR",
    "varchar": "VARCHAR",
    "integer": "BIGINT",
    "int64": "BIGINT",
    "bigint": "BIGINT",
    "number": "DOUBLE",
    "float64": "DOUBLE",
    "double": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "json": "JSON",
}


def _type_family(raw: str) -> str:
    base = raw.upper().split("(", 1)[0].strip()
    if base in {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
    }:
        return "integer"
    if base in {"DECIMAL", "NUMERIC"}:
        return "decimal"
    if base in {"DOUBLE", "FLOAT", "REAL"}:
        return "number"
    if base in {"VARCHAR", "TEXT", "CHAR", "BPCHAR"}:
        return "string"
    if base in {"BOOLEAN", "BOOL"}:
        return "boolean"
    return base.lower()


class OperationExecutor:
    def __init__(self, workspace: Workspace, limits: Limits) -> None:
        self.workspace = workspace
        self.limits = limits

    def execute(
        self,
        step: dict[str, Any],
        source: DataSet | None,
        datasets: Mapping[str, DataSet],
        step_id: str,
    ) -> DataSet:
        operation = step.get("op")
        try:
            if operation == "join":
                return self._join(step, datasets, step_id)
            if source is None:
                raise DataTransformerError(
                    "E_SOURCE_REFERENCE", "step has no source", {"step_id": step_id}
                )
            if source.is_tree:
                return self._tree_operation(operation, step, source, step_id)
            return self._table_operation(operation, step, source, step_id)
        except DataTransformerError as exc:
            if exc.step_id is None:
                exc.step_id = step_id
            raise
        except duckdb.ConversionException as exc:
            raise DataTransformerError(
                "E_CAST_FAILED", "value could not be cast to the requested type", step_id=step_id
            ) from exc
        except duckdb.BinderException as exc:
            raise DataTransformerError(
                "E_FIELD_NOT_FOUND", "step references a field that does not exist", step_id=step_id
            ) from exc
        except duckdb.OutOfMemoryException as exc:
            raise DataTransformerError(
                "E_MEMORY", "transformation exceeded the execution memory limit", step_id=step_id
            ) from exc
        except duckdb.Error as exc:
            message = str(exc)
            code = "E_EXPRESSION_DOMAIN" if "division by zero" in message else "E_EXECUTION"
            raise DataTransformerError(code, "tabular operation failed", step_id=step_id) from exc

    def _table_operation(
        self, operation: str, step: dict[str, Any], source: DataSet, step_id: str
    ) -> DataSet:
        handlers = {
            "filter": self._filter,
            "select": self._select,
            "drop": self._drop,
            "rename": self._rename,
            "sort": self._sort,
            "limit": self._limit,
            "dedupe": self._dedupe,
            "cast": self._cast,
            "derive": self._derive,
            "explode": self._explode,
            "group": self._group,
            "pivot": self._pivot,
            "unpivot": self._unpivot,
            "flatten": self._flatten_table,
            "unflatten": self._unflatten_table,
        }
        handler = handlers.get(operation)
        if handler is None:
            raise DataTransformerError(
                "E_UNKNOWN_OPERATION",
                "operation is not supported for table data",
                {"operation": operation},
            )
        return handler(step, source, step_id)

    def _filter(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        self._preflight_condition(source, step.get("where"))
        condition = compile_condition(step.get("where"))
        return self._query_dataset(
            source,
            step_id,
            f"SELECT * FROM {self._table(source)} WHERE {condition.sql}",
            condition.parameters,
        )

    def _select(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        fields = step.get("fields")
        if not isinstance(fields, list) or not fields:
            raise DataTransformerError("E_STEP_INVALID", "select requires a non-empty fields array")
        expressions: list[str] = []
        aliases: list[str] = []
        for item in fields:
            if isinstance(item, str):
                field, alias = item, item.split(".")[-1]
            elif isinstance(item, dict) and isinstance(item.get("field"), str):
                field = item["field"]
                alias = item.get("as", field.split(".")[-1])
            else:
                raise DataTransformerError(
                    "E_STEP_INVALID", "select field must be a string or {field, as}"
                )
            self._validate_output_field(alias)
            if alias in aliases:
                raise DataTransformerError(
                    "E_DUPLICATE_FIELD", "select creates duplicate output fields", {"field": alias}
                )
            aliases.append(alias)
            expressions.append(f"{field_expression(field)} AS {quote_identifier(alias)}")
        expressions.append(quote_identifier(INTERNAL_ORDER))
        return self._query_dataset(
            source,
            step_id,
            f"SELECT {', '.join(expressions)} FROM {self._table(source)}",
        )

    def _drop(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        fields = self._field_array(step.get("fields"), "drop")
        columns = self._columns(source)
        missing = sorted(set(fields) - set(columns))
        if missing:
            raise DataTransformerError(
                "E_FIELD_NOT_FOUND", "drop fields do not exist", {"fields": missing}
            )
        remaining = [column for column in columns if column not in fields]
        select_list = [quote_identifier(column) for column in remaining]
        select_list.append(quote_identifier(INTERNAL_ORDER))
        result = self._query_dataset(
            source,
            step_id,
            f"SELECT {', '.join(select_list)} FROM {self._table(source)}",
        )
        result.warnings.append({"code": "W_FIELDS_REMOVED", "fields": fields})
        return result

    def _rename(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        fields = step.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise DataTransformerError("E_STEP_INVALID", "rename requires a fields object")
        columns = self._columns(source)
        missing = sorted(set(fields) - set(columns))
        if missing:
            raise DataTransformerError(
                "E_FIELD_NOT_FOUND", "rename fields do not exist", {"fields": missing}
            )
        output_names = [fields.get(column, column) for column in columns]
        for name in output_names:
            self._validate_output_field(name)
        if len(output_names) != len(set(output_names)):
            raise DataTransformerError(
                "E_DUPLICATE_FIELD", "rename creates duplicate output fields"
            )
        expressions = [
            f"{quote_identifier(column)} AS {quote_identifier(fields.get(column, column))}"
            for column in columns
        ]
        expressions.append(quote_identifier(INTERNAL_ORDER))
        return self._query_dataset(
            source,
            step_id,
            f"SELECT {', '.join(expressions)} FROM {self._table(source)}",
        )

    def _sort(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        by = step.get("by")
        if not isinstance(by, list) or not by:
            raise DataTransformerError("E_STEP_INVALID", "sort requires a non-empty by array")
        order_parts: list[str] = []
        for item in by:
            if isinstance(item, str):
                field, direction, nulls = item, "asc", "last"
            elif isinstance(item, dict) and isinstance(item.get("field"), str):
                field = item["field"]
                direction = item.get("direction", "asc")
                nulls = item.get("nulls", "last")
            else:
                raise DataTransformerError("E_STEP_INVALID", "invalid sort descriptor")
            if direction not in {"asc", "desc"} or nulls not in {"first", "last"}:
                raise DataTransformerError("E_STEP_INVALID", "invalid sort direction or null order")
            order_parts.append(
                f"{field_expression(field)} {direction.upper()} NULLS {nulls.upper()}"
            )
        old_order = quote_identifier(INTERNAL_ORDER)
        order_parts.append(f"{old_order} ASC")
        columns = ", ".join(quote_identifier(column) for column in self._columns(source))
        order_sql = ", ".join(order_parts)
        query = (
            f"SELECT {columns}, row_number() OVER (ORDER BY {order_sql}) - 1 "
            f"AS {old_order} FROM {self._table(source)} ORDER BY {order_sql}"
        )
        return self._query_dataset(source, step_id, query)

    def _limit(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        count = step.get("count")
        offset = step.get("offset", 0)
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
        ):
            raise DataTransformerError(
                "E_STEP_INVALID", "limit count and offset must be non-negative integers"
            )
        return self._query_dataset(
            source,
            step_id,
            f"SELECT * FROM {self._table(source)} ORDER BY {quote_identifier(INTERNAL_ORDER)} "
            "LIMIT ? OFFSET ?",
            [count, offset],
        )

    def _dedupe(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        fields = self._field_array(step.get("fields"), "dedupe")
        keep = step.get("keep", "first")
        if keep not in {"first", "last"}:
            raise DataTransformerError("E_STEP_INVALID", "dedupe keep must be first or last")
        partition = ", ".join(field_expression(field) for field in fields)
        direction = "ASC" if keep == "first" else "DESC"
        marker = "__adt_dedupe_rank__"
        order = quote_identifier(INTERNAL_ORDER)
        rank = quote_identifier(marker)
        query = (
            f"SELECT * EXCLUDE ({rank}) FROM ("
            f"SELECT *, row_number() OVER (PARTITION BY {partition} "
            f"ORDER BY {order} {direction}) AS {rank} "
            f"FROM {self._table(source)}) WHERE {rank} = 1 ORDER BY {order}"
        )
        before = self.workspace.row_count(source.table_name or "")
        result = self._query_dataset(source, step_id, query)
        after = self.workspace.row_count(result.table_name or "")
        if before != after:
            result.warnings.append(
                {"code": "W_ROWS_DEDUPED", "removed_rows": before - after, "fields": fields}
            )
        return result

    def _cast(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        raw_fields = step.get("fields")
        if raw_fields is None and "field" in step and "to" in step:
            raw_fields = {step["field"]: step["to"]}
        if not isinstance(raw_fields, dict) or not raw_fields:
            raise DataTransformerError("E_STEP_INVALID", "cast requires fields or field/to")
        columns = self._columns(source)
        missing = sorted(set(raw_fields) - set(columns))
        if missing:
            raise DataTransformerError(
                "E_FIELD_NOT_FOUND", "cast fields do not exist", {"fields": missing}
            )
        cast_types: dict[str, str] = {}
        for field, target in raw_fields.items():
            if not isinstance(target, str) or target.lower() not in _CAST_TYPES:
                raise DataTransformerError(
                    "E_CAST_TYPE", "unsupported cast type", {"field": field, "type": target}
                )
            cast_types[field] = _CAST_TYPES[target.lower()]
        expressions = [
            (
                f"CAST({quote_identifier(column)} AS {cast_types[column]}) "
                f"AS {quote_identifier(column)}"
                if column in cast_types
                else quote_identifier(column)
            )
            for column in columns
        ]
        expressions.append(quote_identifier(INTERNAL_ORDER))
        result = self._query_dataset(
            source,
            step_id,
            f"SELECT {', '.join(expressions)} FROM {self._table(source)}",
        )
        source_types = self.workspace.column_types(source.table_name or "")
        for field, target in cast_types.items():
            original = source_types[field].upper()
            lossy_predicate: str | None = None
            source_field = quote_identifier(field)
            if target == "DOUBLE" and _type_family(original) in {"integer", "decimal"}:
                lossy_predicate = (
                    f"TRY_CAST(CAST({source_field} AS DOUBLE) AS {original}) "
                    f"IS DISTINCT FROM {source_field}"
                )
            elif target == "BIGINT" and _type_family(original) in {"number", "decimal"}:
                lossy_predicate = (
                    f"TRY_CAST(CAST({source_field} AS BIGINT) AS {original}) "
                    f"IS DISTINCT FROM {source_field}"
                )
            elif "TIMESTAMP" in original and target == "DATE":
                lossy_predicate = (
                    f"CAST(CAST({source_field} AS DATE) AS TIMESTAMP) "
                    f"IS DISTINCT FROM {source_field}"
                )
            if lossy_predicate is not None:
                affected = int(
                    self.workspace.connection.execute(
                        f"SELECT count(*) FROM {self._table(source)} WHERE {lossy_predicate}"
                    ).fetchone()[0]
                )
            else:
                affected = 0
            if affected:
                result.warnings.append(
                    {
                        "code": "W_LOSSY_CAST",
                        "field": field,
                        "from": original.lower(),
                        "to": target.lower(),
                        "affected_rows": affected,
                    }
                )
        return result

    def _derive(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        field = step.get("field")
        self._validate_output_field(field)
        prepared_expression = self._preflight_expression_types(source, step.get("expr"))
        expression = compile_expression(prepared_expression)
        columns = [column for column in self._columns(source) if column != field]
        expressions = [quote_identifier(column) for column in columns]
        expressions.append(f"{expression.sql} AS {quote_identifier(field)}")
        expressions.append(quote_identifier(INTERNAL_ORDER))
        return self._query_dataset(
            source,
            step_id,
            f"SELECT {', '.join(expressions)} FROM {self._table(source)}",
            expression.parameters,
        )

    def _preflight_expression_types(self, source: DataSet, expression: Any) -> Any:
        if not isinstance(expression, dict) or len(expression) != 1:
            return expression
        operation, value = next(iter(expression.items()))
        if operation == "coalesce" and isinstance(value, list):
            families: set[str] = set()
            raw_types: set[str] = set()
            prepared_items: list[Any] = []
            non_null_items: list[bool] = []
            source_is_empty = self.workspace.row_count(source.table_name or "") == 0
            for item in value:
                prepared = self._preflight_expression_types(source, item)
                prepared_items.append(prepared)
                if (
                    isinstance(prepared, dict)
                    and prepared.get("value") is None
                    and set(prepared) == {"value"}
                ):
                    non_null_items.append(False)
                    continue
                fragment = compile_expression(prepared)
                has_non_null = source_is_empty or self.workspace.expression_has_non_null(
                    source, fragment.sql, fragment.parameters
                )
                non_null_items.append(has_non_null)
                if not has_non_null:
                    continue
                raw_type = self.workspace.expression_type(
                    source, fragment.sql, fragment.parameters
                )
                raw_types.add(raw_type.lower())
                families.add(_type_family(raw_type))
            if len(families) > 1:
                raise DataTransformerError(
                    "E_TYPE_MISMATCH",
                    "coalesce operands have different types; cast explicitly before coalescing",
                    {"types": sorted(raw_types)},
                )
            if any(non_null_items):
                prepared_items = [
                    item if has_non_null else {"value": None}
                    for item, has_non_null in zip(
                        prepared_items, non_null_items, strict=True
                    )
                ]
            return {operation: prepared_items}
        if operation in {"add", "subtract", "multiply", "divide", "concat"}:
            if isinstance(value, list):
                return {
                    operation: [
                        self._preflight_expression_types(source, item) for item in value
                    ]
                }
            return expression
        if operation in {"lower", "upper"}:
            return {operation: self._preflight_expression_types(source, value)}
        if operation in {"round", "substring"}:
            items = value if isinstance(value, list) else [value]
            if items:
                prepared = [self._preflight_expression_types(source, items[0]), *items[1:]]
                return {operation: prepared if isinstance(value, list) else prepared[0]}
        return expression

    def _explode(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        field = step.get("field")
        if not isinstance(field, str) or field not in self._columns(source):
            raise DataTransformerError("E_FIELD_NOT_FOUND", "explode field does not exist")
        columns = [column for column in self._columns(source) if column != field]
        inner_columns = [quote_identifier(column) for column in columns]
        inner_columns.append(f"unnest({quote_identifier(field)}) AS {quote_identifier(field)}")
        inner_columns.append(quote_identifier(INTERNAL_ORDER))
        visible = [*columns, field]
        outer_columns = ", ".join(quote_identifier(column) for column in visible)
        order = quote_identifier(INTERNAL_ORDER)
        query = (
            f"SELECT {outer_columns}, row_number() OVER (ORDER BY {order}) - 1 AS {order} FROM ("
            f"SELECT {', '.join(inner_columns)} FROM {self._table(source)})"
        )
        return self._query_dataset(source, step_id, query)

    def _join(self, step: dict[str, Any], datasets: Mapping[str, DataSet], step_id: str) -> DataSet:
        left = self._referenced_dataset(step.get("left"), datasets, "left")
        right = self._referenced_dataset(step.get("right"), datasets, "right")
        if not left.is_table or not right.is_table:
            raise DataTransformerError("E_TYPE_MISMATCH", "join requires two table sources")
        join_type = step.get("type", "inner")
        if join_type not in {"inner", "left", "right", "full"}:
            raise DataTransformerError("E_STEP_INVALID", "unsupported join type")
        on = step.get("on")
        if not isinstance(on, list) or not on:
            raise DataTransformerError("E_STEP_INVALID", "join requires a non-empty on array")
        conditions: list[str] = []
        for pair in on:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(item, str) for item in pair)
            ):
                raise DataTransformerError(
                    "E_STEP_INVALID", "join keys must be [left, right] pairs"
                )
            self._require_compatible_types(
                self.workspace.field_type(left, pair[0]),
                self.workspace.field_type(right, pair[1]),
                {"left": pair[0], "right": pair[1]},
            )
            conditions.append(f"l.{field_expression(pair[0])} = r.{field_expression(pair[1])}")
        prefix = step.get("right_prefix", "right_")
        if not isinstance(prefix, str):
            raise DataTransformerError("E_STEP_INVALID", "right_prefix must be a string")
        left_columns = self._columns(left)
        right_columns = self._columns(right)
        output_names = list(left_columns)
        selections = [f"l.{quote_identifier(column)}" for column in left_columns]
        for column in right_columns:
            output = f"{prefix}{column}" if column in output_names else column
            self._validate_output_field(output)
            if output in output_names:
                raise DataTransformerError("E_DUPLICATE_FIELD", "join creates duplicate fields")
            output_names.append(output)
            selections.append(f"r.{quote_identifier(column)} AS {quote_identifier(output)}")
        order = (
            f"coalesce(l.{quote_identifier(INTERNAL_ORDER)}, 9223372036854775807), "
            f"coalesce(r.{quote_identifier(INTERNAL_ORDER)}, 9223372036854775807)"
        )
        query = (
            f"SELECT {', '.join(selections)}, row_number() OVER (ORDER BY {order}) - 1 "
            f"AS {quote_identifier(INTERNAL_ORDER)} FROM {self._table(left)} l "
            f"{join_type.upper()} JOIN {self._table(right)} r ON {' AND '.join(conditions)} "
            f"ORDER BY {order}"
        )
        left_table = self._table(left)
        right_table = self._table(right)
        condition_sql = " AND ".join(conditions)
        matched_pairs, matched_left, matched_right = self.workspace.connection.execute(
            f"SELECT count(*), count(DISTINCT l.{quote_identifier(INTERNAL_ORDER)}), "
            f"count(DISTINCT r.{quote_identifier(INTERNAL_ORDER)}) "
            f"FROM {left_table} l INNER JOIN {right_table} r ON {condition_sql}"
        ).fetchone()
        left_rows = self.workspace.row_count(left.table_name or "")
        right_rows = self.workspace.row_count(right.table_name or "")
        result = self._query_dataset(left, step_id, query)
        unmatched_left = left_rows - int(matched_left)
        unmatched_right = right_rows - int(matched_right)
        fanout_left = max(0, int(matched_pairs) - int(matched_left))
        fanout_right = max(0, int(matched_pairs) - int(matched_right))
        new_nulls: list[dict[str, Any]] = []
        if join_type in {"left", "full"} and unmatched_left:
            new_nulls.append(
                {
                    "side": "right",
                    "rows": unmatched_left,
                    "fields": output_names[len(left_columns) :],
                }
            )
        if join_type in {"right", "full"} and unmatched_right:
            new_nulls.append({"side": "left", "rows": unmatched_right, "fields": left_columns})
        result.effects = {
            "inputs": {
                "left": {"name": step["left"], "rows": left_rows, "fields": left_columns},
                "right": {"name": step["right"], "rows": right_rows, "fields": right_columns},
            },
            "matches": {
                "pairs": int(matched_pairs),
                "left_rows": int(matched_left),
                "right_rows": int(matched_right),
            },
            "unmatched": {"left_rows": unmatched_left, "right_rows": unmatched_right},
            "fan_out": {"extra_left_copies": fanout_left, "extra_right_copies": fanout_right},
            "new_nulls": new_nulls,
            "left_fields": left_columns,
            "right_output_fields": output_names[len(left_columns) :],
        }
        if unmatched_left or unmatched_right:
            result.warnings.append({"code": "W_JOIN_UNMATCHED", **result.effects["unmatched"]})
        if fanout_left or fanout_right:
            result.warnings.append({"code": "W_JOIN_FANOUT", **result.effects["fan_out"]})
        if new_nulls:
            result.warnings.append({"code": "W_NULLS_INTRODUCED", "groups": new_nulls})
        return result

    def _group(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        by = step.get("by", [])
        if not isinstance(by, list):
            raise DataTransformerError("E_STEP_INVALID", "group by must be an array of fields")
        aggregates = step.get("aggregates")
        if not isinstance(aggregates, list) or not aggregates:
            raise DataTransformerError("E_STEP_INVALID", "group requires aggregates")
        dimensions = self._dimensions(by, "group")
        group_exprs = [field_expression(field) for field, _ in dimensions]
        selections = [
            f"{field_expression(field)} AS {quote_identifier(alias)}" for field, alias in dimensions
        ]
        aliases = [alias for _, alias in dimensions]
        for aggregate in aggregates:
            sql, alias = self._aggregate_expression(aggregate)
            if alias in aliases:
                raise DataTransformerError("E_DUPLICATE_FIELD", "group creates duplicate fields")
            aliases.append(alias)
            selections.append(f"{sql} AS {quote_identifier(alias)}")
        group_clause = f" GROUP BY {', '.join(group_exprs)}" if group_exprs else ""
        order_clause = f" ORDER BY {', '.join(group_exprs)}" if group_exprs else ""
        inner = (
            f"SELECT {', '.join(selections)} FROM {self._table(source)}{group_clause}{order_clause}"
        )
        visible = ", ".join(quote_identifier(alias) for alias in aliases)
        query = (
            f"SELECT {visible}, row_number() OVER () - 1 AS {quote_identifier(INTERNAL_ORDER)} "
            f"FROM ({inner})"
        )
        return self._query_dataset(source, step_id, query)

    def _pivot(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        by = step.get("by", [])
        pivot_field = step.get("field")
        value_field = step.get("value")
        values = step.get("values")
        aggregate = step.get("aggregate", "sum")
        if (
            not isinstance(by, list)
            or not isinstance(pivot_field, str)
            or not isinstance(value_field, str)
            or not isinstance(values, list)
            or not values
            or len(values) > 100
            or aggregate not in {"sum", "avg", "min", "max", "count"}
        ):
            raise DataTransformerError("E_STEP_INVALID", "invalid pivot definition")
        aliases = step.get("aliases", {})
        if not isinstance(aliases, dict):
            raise DataTransformerError("E_STEP_INVALID", "pivot aliases must be an object")
        dimensions = self._dimensions(by, "pivot")
        pivot_type = self.workspace.field_type(source, pivot_field)
        for value in values:
            self._require_compatible_types(
                pivot_type,
                _literal_type(value),
                {"field": pivot_field, "value": value},
            )
        parameters: list[Any] = []
        selections = [
            f"{field_expression(field)} AS {quote_identifier(alias)}" for field, alias in dimensions
        ]
        output_names = [alias for _, alias in dimensions]
        for value in values:
            alias = aliases.get(str(value), str(value))
            self._validate_output_field(alias)
            if alias in output_names:
                raise DataTransformerError("E_DUPLICATE_FIELD", "pivot creates duplicate fields")
            output_names.append(alias)
            target = "1" if aggregate == "count" else field_expression(value_field)
            selections.append(
                f"{aggregate}(CASE WHEN {field_expression(pivot_field)} = ? THEN {target} END) "
                f"AS {quote_identifier(alias)}"
            )
            parameters.append(value)
        group_clause = ", ".join(field_expression(field) for field, _ in dimensions)
        query_inner = f"SELECT {', '.join(selections)} FROM {self._table(source)}"
        if by:
            query_inner += f" GROUP BY {group_clause} ORDER BY {group_clause}"
        visible = ", ".join(quote_identifier(name) for name in output_names)
        query = (
            f"SELECT {visible}, row_number() OVER () - 1 AS {quote_identifier(INTERNAL_ORDER)} "
            f"FROM ({query_inner})"
        )
        return self._query_dataset(source, step_id, query, parameters)

    def _unpivot(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        fields = self._field_array(step.get("fields"), "unpivot")
        default_keep = [column for column in self._columns(source) if column not in fields]
        keep = step.get("keep", default_keep)
        if not isinstance(keep, list) or not all(isinstance(item, str) for item in keep):
            raise DataTransformerError("E_STEP_INVALID", "unpivot keep must be an array")
        if len(keep) != len(set(keep)):
            raise DataTransformerError("E_DUPLICATE_FIELD", "unpivot keep repeats a field")
        columns = self._columns(source)
        missing = sorted((set(fields) | set(keep)) - set(columns))
        if missing:
            raise DataTransformerError(
                "E_FIELD_NOT_FOUND", "unpivot fields do not exist", {"fields": missing}
            )
        overlap = sorted(set(fields) & set(keep))
        if overlap:
            raise DataTransformerError(
                "E_DUPLICATE_FIELD", "unpivot keep overlaps value fields", {"fields": overlap}
            )
        name_field = step.get("name_field", "field")
        value_field = step.get("value_field", "value")
        self._validate_output_field(name_field)
        self._validate_output_field(value_field)
        if name_field in keep or value_field in keep or name_field == value_field:
            raise DataTransformerError("E_DUPLICATE_FIELD", "unpivot creates duplicate fields")
        unions: list[str] = []
        parameters: list[Any] = []
        keep_sql = ", ".join(quote_identifier(field) for field in keep)
        multiplier = len(fields)
        for index, field in enumerate(fields):
            prefix = f"{keep_sql}, " if keep_sql else ""
            unions.append(
                f"SELECT {prefix}? AS {quote_identifier(name_field)}, "
                f"{quote_identifier(field)} AS {quote_identifier(value_field)}, "
                f"{quote_identifier(INTERNAL_ORDER)} * {multiplier} + {index} "
                f"AS {quote_identifier(INTERNAL_ORDER)} FROM {self._table(source)}"
            )
            parameters.append(field)
        return self._query_dataset(source, step_id, " UNION ALL ".join(unions), parameters)

    def _aggregate_expression(self, aggregate: dict[str, Any]) -> tuple[str, str]:
        if not isinstance(aggregate, dict):
            raise DataTransformerError("E_STEP_INVALID", "aggregate must be an object")
        operation = aggregate.get("op")
        field = aggregate.get("field")
        alias = aggregate.get("as")
        allowed = {"sum", "avg", "min", "max", "count", "count_distinct", "first", "last"}
        if operation not in allowed:
            raise DataTransformerError("E_STEP_INVALID", "unsupported aggregate operation")
        if operation != "count" and not isinstance(field, str):
            raise DataTransformerError("E_STEP_INVALID", "aggregate requires a field")
        if not isinstance(alias, str):
            alias = f"{operation}_{field or 'rows'}"
        self._validate_output_field(alias)
        if operation == "count":
            expression = "count(*)" if field is None else f"count({field_expression(field)})"
        elif operation == "count_distinct":
            expression = f"count(DISTINCT {field_expression(field)})"
        elif operation in {"first", "last"}:
            direction = "ASC" if operation == "first" else "DESC"
            order = quote_identifier(INTERNAL_ORDER)
            expression = f"first({field_expression(field)} ORDER BY {order} {direction})"
        else:
            expression = f"{operation}({field_expression(field)})"
        return expression, alias

    def _flatten_table(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        separator = step.get("separator", "_")
        if (
            not isinstance(separator, str)
            or not separator
            or len(separator) > 4
            or "\\" in separator
        ):
            raise DataTransformerError(
                "E_STEP_INVALID", "flatten separator must be 1 to 4 characters"
            )
        source_schema = self.workspace.column_types(source.table_name or "")
        empty_schema = {
            _escape_path_part(field, separator): data_type
            for field, data_type in source_schema.items()
        }
        rows = (_flatten_mapping(row, separator) for row in self.workspace.iter_rows(source))
        source_is_empty = self.workspace.row_count(source.table_name or "") == 0
        result = self.workspace.table_from_rows(
            rows,
            step_id,
            source.source_format,
            source.byte_size,
            self.limits,
            empty_schema=empty_schema if source_is_empty else None,
        )
        if source_is_empty:
            result.effects["flatten_original_schema"] = source_schema
        return result

    def _unflatten_table(self, step: dict[str, Any], source: DataSet, step_id: str) -> DataSet:
        separator = step.get("separator", "_")
        if (
            not isinstance(separator, str)
            or not separator
            or len(separator) > 4
            or "\\" in separator
        ):
            raise DataTransformerError(
                "E_STEP_INVALID", "unflatten separator must be 1 to 4 characters"
            )
        rows = (_unflatten_mapping(row, separator) for row in self.workspace.iter_rows(source))
        original_schema = source.effects.get("flatten_original_schema")
        empty_schema = original_schema if isinstance(original_schema, dict) else None
        source_is_empty = self.workspace.row_count(source.table_name or "") == 0
        return self.workspace.table_from_rows(
            rows,
            step_id,
            source.source_format,
            source.byte_size,
            self.limits,
            empty_schema=empty_schema if source_is_empty else None,
        )

    def _tree_operation(
        self, operation: str, step: dict[str, Any], source: DataSet, step_id: str
    ) -> DataSet:
        value = copy.deepcopy(source.value)
        before = _tree_inventory(value)
        if operation == "set":
            path = self._tree_path(step.get("path"))
            value = _tree_set(
                value,
                path,
                copy.deepcopy(step.get("value")),
                step.get("create", False),
            )
        elif operation == "delete":
            path = self._tree_path(step.get("path"))
            value = _tree_delete(value, path)
        elif operation == "merge":
            incoming = step.get("value")
            if not isinstance(value, dict) or not isinstance(incoming, dict):
                raise DataTransformerError("E_TYPE_MISMATCH", "tree merge requires two objects")
            value = (
                _deep_merge(value, incoming) if step.get("deep", True) else {**value, **incoming}
            )
        elif operation == "flatten":
            if not isinstance(value, dict):
                raise DataTransformerError("E_TYPE_MISMATCH", "flatten requires an object")
            separator = step.get("separator", "_")
            value = _flatten_mapping(value, separator)
        elif operation == "unflatten":
            if not isinstance(value, dict):
                raise DataTransformerError("E_TYPE_MISMATCH", "unflatten requires an object")
            separator = step.get("separator", "_")
            value = _unflatten_mapping(value, separator)
        else:
            raise DataTransformerError(
                "E_UNKNOWN_OPERATION",
                "operation is not supported for tree data",
                {"operation": operation},
            )
        after = _tree_inventory(value)
        changes = _tree_changes(before, after)
        result = DataSet("tree", step_id, source.source_format, source.byte_size, value=value)
        result.effects["tree_changes"] = changes
        if changes["paths_removed"]:
            result.warnings.append(
                {"code": "W_TREE_PATHS_REMOVED", "count": len(changes["paths_removed"])}
            )
        return result

    def _preflight_condition(self, source: DataSet, condition: Any) -> None:
        if not isinstance(condition, dict):
            return
        for key in ("all", "any"):
            if isinstance(condition.get(key), list):
                for item in condition[key]:
                    self._preflight_condition(source, item)
                return
        if "not" in condition:
            self._preflight_condition(source, condition["not"])
            return
        field = condition.get("field")
        if not isinstance(field, str):
            return
        operator = next((key for key in condition if key != "field"), None)
        if operator in {"is_null", "not_null"} or operator is None:
            return
        field_type = self.workspace.field_type(source, field)
        value = condition[operator]
        values = value if operator in {"in", "not_in"} and isinstance(value, list) else [value]
        for item in values:
            if item is not None:
                self._require_compatible_types(
                    field_type,
                    _literal_type(item),
                    {"field": field, "comparison": operator, "value": item},
                    require_string=operator in {"contains", "starts_with", "ends_with"},
                )

    def _require_compatible_types(
        self,
        left: str,
        right: str,
        details: dict[str, Any],
        *,
        require_string: bool = False,
    ) -> None:
        left_family = _type_family(left)
        right_family = _type_family(right)
        numeric = {"integer", "decimal", "number"}
        compatible = (
            left_family == right_family
            or (left_family in numeric and right_family in numeric)
        )
        if require_string:
            compatible = left_family == right_family == "string"
        if not compatible:
            raise DataTransformerError(
                "E_TYPE_MISMATCH",
                "comparison operands require matching types; add an explicit cast",
                {**details, "left_type": left_family, "right_type": right_family},
            )

    def _tree_path(self, path: Any) -> list[str | int]:
        if not isinstance(path, str) or not path:
            raise DataTransformerError("E_STEP_INVALID", "tree operation requires a path")
        tokens = parse_selector(path)
        if "*" in tokens:
            raise DataTransformerError("E_STEP_INVALID", "tree mutation paths cannot use wildcard")
        return tokens

    def _query_dataset(
        self,
        source: DataSet,
        step_id: str,
        query: str,
        parameters: list[Any] | None = None,
    ) -> DataSet:
        table_name = self.workspace.next_table_name(step_id)
        self.workspace.connection.execute(
            f"CREATE TABLE {quote_identifier(table_name)} AS {query}", parameters or []
        )
        if INTERNAL_ORDER not in self.workspace.columns(table_name, include_internal=True):
            raise DataTransformerError("E_INTERNAL", "operation dropped the internal order field")
        rows = self.workspace.row_count(table_name)
        if rows > self.limits.max_rows:
            self.workspace.connection.execute(f"DROP TABLE {quote_identifier(table_name)}")
            raise DataTransformerError(
                "E_ROW_LIMIT",
                "step output exceeds row limit",
                {"rows": rows, "maximum": self.limits.max_rows},
            )
        return DataSet(
            "table",
            step_id,
            source.source_format,
            source.byte_size,
            table_name=table_name,
        )

    def _columns(self, dataset: DataSet) -> list[str]:
        if dataset.table_name is None:
            raise DataTransformerError("E_INTERNAL", "table has no backing relation")
        return self.workspace.columns(dataset.table_name)

    def _table(self, dataset: DataSet) -> str:
        if dataset.table_name is None:
            raise DataTransformerError("E_INTERNAL", "table has no backing relation")
        return quote_identifier(dataset.table_name)

    def _field_array(self, value: Any, operation: str) -> list[str]:
        valid = (
            isinstance(value, list) and bool(value) and all(isinstance(item, str) for item in value)
        )
        if not valid:
            raise DataTransformerError(
                "E_STEP_INVALID", f"{operation} requires a non-empty fields array"
            )
        if len(value) != len(set(value)):
            raise DataTransformerError("E_DUPLICATE_FIELD", f"{operation} repeats a field")
        return value

    def _validate_output_field(self, field: Any) -> None:
        if not isinstance(field, str) or not field or field == INTERNAL_ORDER or "\x00" in field:
            raise DataTransformerError(
                "E_FIELD_INVALID", "invalid output field name", {"field": field}
            )

    def _dimensions(self, value: list[Any], operation: str) -> list[tuple[str, str]]:
        dimensions: list[tuple[str, str]] = []
        aliases: set[str] = set()
        for item in value:
            if isinstance(item, str):
                field, alias = item, item.split(".")[-1]
            elif isinstance(item, dict) and isinstance(item.get("field"), str):
                field = item["field"]
                alias = item.get("as", field.split(".")[-1])
            else:
                raise DataTransformerError(
                    "E_STEP_INVALID", f"{operation} dimension must be a field or {{field, as}}"
                )
            self._validate_output_field(alias)
            if alias in aliases:
                raise DataTransformerError(
                    "E_DUPLICATE_FIELD",
                    f"{operation} creates duplicate dimension fields",
                    {"field": alias},
                )
            aliases.add(alias)
            dimensions.append((field, alias))
        return dimensions

    def _referenced_dataset(
        self, reference: Any, datasets: Mapping[str, DataSet], side: str
    ) -> DataSet:
        if not isinstance(reference, str) or reference not in datasets:
            raise DataTransformerError(
                "E_SOURCE_REFERENCE", f"join {side} source does not exist", {"source": reference}
            )
        return datasets[reference]


def _flatten_mapping(
    value: dict[str, Any], separator: str, prefix: tuple[str, ...] = ()
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        key = str(key)
        path_parts = (*prefix, key)
        if isinstance(item, dict):
            if not item:
                path = separator.join(
                    _escape_path_part(part, separator) for part in path_parts
                )
                result[path] = {}
                continue
            nested = _flatten_mapping(item, separator, path_parts)
            for nested_key, nested_value in nested.items():
                if nested_key in result:
                    raise DataTransformerError(
                        "E_FIELD_COLLISION", "flatten creates a duplicate field"
                    )
                result[nested_key] = nested_value
        else:
            path = separator.join(_escape_path_part(part, separator) for part in path_parts)
            if path in result:
                raise DataTransformerError("E_FIELD_COLLISION", "flatten creates a duplicate field")
            result[path] = item
    return result


def _literal_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, decimal.Decimal):
        return "decimal"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null" if value is None else type(value).__name__.lower()


def _tree_inventory(value: Any, path: str = "") -> dict[str, tuple[str, Any]]:
    escaped_path = path or "/"
    inventory = {escaped_path: (_literal_type(value), copy.deepcopy(value))}
    if isinstance(value, dict):
        for key, item in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            child = f"{path}/{token}" if path else f"/{token}"
            inventory.update(_tree_inventory(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child = f"{path}/{index}" if path else f"/{index}"
            inventory.update(_tree_inventory(item, child))
    return inventory


def _tree_changes(
    before: dict[str, tuple[str, Any]], after: dict[str, tuple[str, Any]]
) -> dict[str, Any]:
    before_paths = set(before)
    after_paths = set(after)
    common = before_paths & after_paths
    return {
        "paths_added": sorted(after_paths - before_paths),
        "paths_removed": sorted(before_paths - after_paths),
        "values_changed": sorted(
            path for path in common if before[path][1] != after[path][1]
        ),
        "type_changes": [
            {"path": path, "from": before[path][0], "to": after[path][0]}
            for path in sorted(common)
            if before[path][0] != after[path][0]
        ],
    }


def _unflatten_mapping(value: dict[str, Any], separator: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for flat_key, item in value.items():
        parts = _split_escaped_path(str(flat_key), separator)
        if any(not part for part in parts):
            raise DataTransformerError("E_FIELD_COLLISION", "unflatten field has an empty segment")
        current = result
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            elif not isinstance(current[part], dict):
                raise DataTransformerError("E_FIELD_COLLISION", "unflatten paths collide")
            current = current[part]
        if parts[-1] in current:
            raise DataTransformerError("E_FIELD_COLLISION", "unflatten creates a duplicate field")
        current[parts[-1]] = item
    return result


def _split_escaped_path(value: str, separator: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\":
            if index + 1 >= len(value):
                raise DataTransformerError(
                    "E_FIELD_COLLISION", "unflatten field has an invalid escape"
                )
            if value.startswith(separator, index + 1):
                current.append(separator)
                index += 1 + len(separator)
            elif value[index + 1] == "\\":
                current.append("\\")
                index += 2
            else:
                raise DataTransformerError(
                    "E_FIELD_COLLISION", "unflatten field has an invalid escape"
                )
        elif value.startswith(separator, index):
            parts.append("".join(current))
            current = []
            index += len(separator)
        else:
            current.append(value[index])
            index += 1
    parts.append("".join(current))
    return parts


def _escape_path_part(value: str, separator: str) -> str:
    return value.replace("\\", "\\\\").replace(separator, "\\" + separator)


def _tree_set(root: Any, path: list[str | int], value: Any, create: bool) -> Any:
    if not path:
        return value
    current = root
    for index, token in enumerate(path[:-1]):
        next_token = path[index + 1]
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                raise DataTransformerError("E_PATH_NOT_FOUND", "tree path does not exist")
            current = current[token]
        else:
            if not isinstance(current, dict):
                raise DataTransformerError("E_PATH_NOT_FOUND", "tree path does not exist")
            if token not in current:
                if not create:
                    raise DataTransformerError("E_PATH_NOT_FOUND", "tree path does not exist")
                current[token] = [] if isinstance(next_token, int) else {}
            current = current[token]
    final = path[-1]
    if isinstance(final, int):
        if not isinstance(current, list) or final >= len(current):
            raise DataTransformerError("E_PATH_NOT_FOUND", "tree path does not exist")
        current[final] = value
    else:
        if not isinstance(current, dict):
            raise DataTransformerError("E_PATH_NOT_FOUND", "tree path does not exist")
        if final not in current and not create:
            raise DataTransformerError("E_PATH_NOT_FOUND", "tree path does not exist")
        current[final] = value
    return root


def _tree_delete(root: Any, path: list[str | int]) -> Any:
    if not path:
        raise DataTransformerError("E_STEP_INVALID", "cannot delete the root value")
    current = root
    for token in path[:-1]:
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                raise DataTransformerError("E_PATH_NOT_FOUND", "tree path does not exist")
            current = current[token]
        else:
            if not isinstance(current, dict) or token not in current:
                raise DataTransformerError("E_PATH_NOT_FOUND", "tree path does not exist")
            current = current[token]
    final = path[-1]
    if isinstance(final, int):
        if not isinstance(current, list) or final >= len(current):
            raise DataTransformerError("E_PATH_NOT_FOUND", "tree path does not exist")
        current.pop(final)
    else:
        if not isinstance(current, dict) or final not in current:
            raise DataTransformerError("E_PATH_NOT_FOUND", "tree path does not exist")
        del current[final]
    return root


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(left)
    for key, value in right.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result
