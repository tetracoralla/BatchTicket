from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

from .dataset import DataSet
from .errors import DataTransformerError
from .workspace import Workspace, quote_identifier

MAX_VALIDATION_ERRORS = 20


def validate_schema(
    workspace: Workspace, dataset: DataSet, schema: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise DataTransformerError("E_SCHEMA_INVALID", "schema must be an object")
    try:
        validator = Draft202012Validator(schema)
        validator.check_schema(schema)
    except SchemaError as exc:
        raise DataTransformerError(
            "E_SCHEMA_INVALID", "invalid JSON Schema", {"message": exc.message}
        ) from exc

    values = workspace.iter_rows(dataset) if dataset.is_table else iter([dataset.value])
    failures: list[dict[str, Any]] = []
    failures_truncated = False
    checked = 0
    for row_index, value in enumerate(values):
        checked += 1
        for error in sorted(validator.iter_errors(value), key=lambda item: list(item.path)):
            failure = {
                "row": row_index if dataset.is_table else None,
                "path": list(error.absolute_path),
                "keyword": error.validator,
                "message": error.message,
            }
            if len(failures) < MAX_VALIDATION_ERRORS:
                failures.append(failure)
            else:
                failures_truncated = True
                break
        if failures_truncated:
            break
    return {
        "status": "failed" if failures else "passed",
        "records_checked": checked,
        "failures": failures,
        "failures_truncated": failures_truncated,
    }


def evaluate_assertions(
    workspace: Workspace,
    dataset: DataSet,
    assertions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if assertions is None:
        return []
    if not isinstance(assertions, list):
        raise DataTransformerError("E_ASSERTION_INVALID", "assert must be an array")
    results: list[dict[str, Any]] = []
    for index, assertion in enumerate(assertions):
        if not isinstance(assertion, dict):
            raise DataTransformerError("E_ASSERTION_INVALID", "assertion must be an object")
        assertion_type = assertion.get("type")
        assertion_id = assertion.get("id", f"assertion_{index + 1}")
        if not isinstance(assertion_id, str) or not assertion_id:
            raise DataTransformerError("E_ASSERTION_INVALID", "assertion id must be a string")
        if dataset.is_table:
            result = _evaluate_table_assertion(workspace, dataset, assertion_type, assertion)
        else:
            result = _evaluate_tree_assertion(dataset.value, assertion_type, assertion)
        results.append({"id": assertion_id, "type": assertion_type, **result})
    return results


def require_assertions_passed(results: list[dict[str, Any]]) -> None:
    failures = [result for result in results if result["status"] == "failed"]
    if failures:
        raise DataTransformerError(
            "E_ASSERTION_FAILED",
            "one or more data assertions failed",
            {"failures": failures[:MAX_VALIDATION_ERRORS]},
        )


def _evaluate_table_assertion(
    workspace: Workspace,
    dataset: DataSet,
    assertion_type: Any,
    assertion: dict[str, Any],
) -> dict[str, Any]:
    table_name = dataset.table_name or ""
    columns = workspace.columns(table_name)
    if assertion_type == "field_exists":
        field = _assertion_field(assertion)
        passed = field in columns
        return {"status": "passed" if passed else "failed", "field": field}
    if assertion_type == "not_null":
        field = _existing_field(assertion, columns)
        count = int(
            workspace.connection.execute(
                f"SELECT count(*) FROM {quote_identifier(table_name)} "
                f"WHERE {quote_identifier(field)} IS NULL"
            ).fetchone()[0]
        )
        return {
            "status": "passed" if count == 0 else "failed",
            "field": field,
            "null_rows": count,
        }
    if assertion_type == "unique":
        fields = assertion.get("fields")
        if fields is None:
            fields = [_assertion_field(assertion)]
        if (
            not isinstance(fields, list)
            or not fields
            or not all(isinstance(item, str) for item in fields)
        ):
            raise DataTransformerError("E_ASSERTION_INVALID", "unique requires fields")
        missing = sorted(set(fields) - set(columns))
        if missing:
            raise DataTransformerError(
                "E_FIELD_NOT_FOUND", "assertion fields do not exist", {"fields": missing}
            )
        group = ", ".join(quote_identifier(field) for field in fields)
        duplicates = int(
            workspace.connection.execute(
                f"SELECT count(*) FROM (SELECT {group}, count(*) AS n "
                f"FROM {quote_identifier(table_name)} GROUP BY {group} HAVING n > 1)"
            ).fetchone()[0]
        )
        return {
            "status": "passed" if duplicates == 0 else "failed",
            "fields": fields,
            "duplicate_groups": duplicates,
        }
    if assertion_type == "row_count":
        count = workspace.row_count(table_name)
        conditions: list[bool] = []
        expected: dict[str, int] = {}
        for key, compare in {
            "eq": lambda a, b: a == b,
            "min": lambda a, b: a >= b,
            "max": lambda a, b: a <= b,
        }.items():
            if key in assertion:
                value = assertion[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise DataTransformerError(
                        "E_ASSERTION_INVALID", "row_count bounds must be non-negative integers"
                    )
                expected[key] = value
                conditions.append(compare(count, value))
        if not conditions:
            raise DataTransformerError("E_ASSERTION_INVALID", "row_count requires eq, min, or max")
        return {
            "status": "passed" if all(conditions) else "failed",
            "actual": count,
            "expected": expected,
        }
    if assertion_type == "type":
        field = _existing_field(assertion, columns)
        expected = assertion.get("is")
        if not isinstance(expected, str):
            raise DataTransformerError("E_ASSERTION_INVALID", "type assertion requires is")
        actual = workspace.column_types(table_name)[field].lower()
        actual_family = _duckdb_type_family(actual)
        aliases = {
            "string": "string",
            "integer": "integer",
            "number": "number",
            "boolean": "boolean",
            "date": "date",
            "timestamp": "timestamp",
        }
        normalized_expected = aliases.get(expected.lower(), expected.lower())
        passed = actual == expected.lower() or actual_family == normalized_expected
        return {
            "status": "passed" if passed else "failed",
            "field": field,
            "actual": actual,
            "expected": expected.lower(),
        }
    raise DataTransformerError(
        "E_ASSERTION_INVALID", "unknown assertion type", {"type": assertion_type}
    )


def _evaluate_tree_assertion(
    value: Any, assertion_type: Any, assertion: dict[str, Any]
) -> dict[str, Any]:
    if assertion_type == "field_exists":
        field = _assertion_field(assertion)
        passed = isinstance(value, dict) and field in value
        return {"status": "passed" if passed else "failed", "field": field}
    if assertion_type == "not_null":
        field = _assertion_field(assertion)
        passed = isinstance(value, dict) and field in value and value[field] is not None
        return {"status": "passed" if passed else "failed", "field": field}
    if assertion_type == "row_count":
        count = len(value) if isinstance(value, list) else 1
        conditions: list[bool] = []
        expected: dict[str, int] = {}
        for key, compare in {
            "eq": lambda a, b: a == b,
            "min": lambda a, b: a >= b,
            "max": lambda a, b: a <= b,
        }.items():
            if key in assertion:
                bound = assertion[key]
                if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
                    raise DataTransformerError(
                        "E_ASSERTION_INVALID", "row_count bounds must be non-negative integers"
                    )
                expected[key] = bound
                conditions.append(compare(count, bound))
        if not conditions:
            raise DataTransformerError(
                "E_ASSERTION_INVALID", "tree row_count requires eq, min, or max"
            )
        return {
            "status": "passed" if all(conditions) else "failed",
            "actual": count,
            "expected": expected,
        }
    raise DataTransformerError(
        "E_ASSERTION_INVALID",
        "assertion is not supported for tree data",
        {"type": assertion_type},
    )


def _assertion_field(assertion: dict[str, Any]) -> str:
    field = assertion.get("field")
    if not isinstance(field, str) or not field:
        raise DataTransformerError("E_ASSERTION_INVALID", "assertion requires a field")
    return field


def _existing_field(assertion: dict[str, Any], columns: list[str]) -> str:
    field = _assertion_field(assertion)
    if field not in columns:
        raise DataTransformerError(
            "E_FIELD_NOT_FOUND", "assertion field does not exist", {"field": field}
        )
    return field


def _duckdb_type_family(raw: str) -> str:
    normalized = re.sub(r"\s+", " ", raw.strip().lower())
    base = normalized.split("(", 1)[0].strip()
    integers = {
        "tinyint",
        "smallint",
        "integer",
        "bigint",
        "hugeint",
        "utinyint",
        "usmallint",
        "uinteger",
        "ubigint",
        "uhugeint",
    }
    if base in integers:
        return "integer"
    if base in {"decimal", "numeric", "double", "float", "real"}:
        return "number"
    if base in {"varchar", "char", "bpchar", "text"}:
        return "string"
    if base in {"boolean", "bool"}:
        return "boolean"
    if base == "date":
        return "date"
    if base.startswith("timestamp") or base == "datetime":
        return "timestamp"
    return normalized
