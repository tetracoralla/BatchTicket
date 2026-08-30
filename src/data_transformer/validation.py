from __future__ import annotations

import decimal
import math
import re
from collections.abc import Iterator
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from jsonschema.validators import extend

from .dataset import DataSet
from .errors import DataTransformerError
from .json_values import normalize_source_numbers, validation_safe
from .workspace import Workspace, quote_identifier

MAX_VALIDATION_ERRORS = 20


def _is_integer(checker: Any, instance: Any) -> bool:
    # The default draft checker accepts int and integral float; exact decimals
    # must validate identically so file and inline carriers agree.
    if isinstance(instance, bool):
        return False
    if isinstance(instance, int):
        return True
    if isinstance(instance, float):
        return instance.is_integer()
    if isinstance(instance, decimal.Decimal):
        return instance.is_finite() and instance == instance.to_integral_value()
    return False


def _multiple_of(
    validator: Any, multiple: Any, instance: Any, schema: dict[str, Any]
) -> Iterator[ValidationError]:
    # Schema numbers normalize to exact decimals, while DOUBLE-lane table
    # values arrive as Python floats; the default keyword does the modulo
    # directly and raises TypeError. Normalize both sides to Decimal so every
    # lane yields a verdict instead of an internal error.
    if isinstance(instance, bool) or not validator.is_type(instance, "number"):
        return
    if isinstance(instance, float) and not math.isfinite(instance):
        yield ValidationError(f"{instance!r} is not a multiple of {multiple!r}")
        return
    value = (
        instance if isinstance(instance, decimal.Decimal) else decimal.Decimal(str(instance))
    )
    if multiple == 0 or value % multiple != 0:
        yield ValidationError(f"{instance!r} is not a multiple of {multiple!r}")


_VALIDATOR = extend(
    Draft202012Validator,
    {"multipleOf": _multiple_of},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_integer),
)


def validate_schema(
    workspace: Workspace, dataset: DataSet, schema: dict[str, Any]
) -> dict[str, Any]:
    normalized_schema = prepare_schema(schema)

    validator = _VALIDATOR(normalized_schema)
    values = (
        workspace.iter_rows(dataset, safe=validation_safe)
        if dataset.is_table
        else iter([validation_safe(dataset.value)])
    )
    failures: list[dict[str, Any]] = []
    failures_truncated = False
    checked = 0
    for row_index, value in enumerate(values):
        checked += 1
        for error in sorted(
            validator.iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.path),
        ):
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


def prepare_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise DataTransformerError("E_SCHEMA_INVALID", "schema must be an object")
    normalized_schema = normalize_source_numbers(schema)
    try:
        _VALIDATOR.check_schema(normalized_schema)
    except SchemaError as exc:
        raise DataTransformerError(
            "E_SCHEMA_INVALID", "invalid JSON Schema", {"message": exc.message}
        ) from exc
    return normalized_schema


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
        # A parameterized exact type such as DECIMAL(2,1) satisfies its
        # unparameterized name, while the family alias still accepts storage
        # variants (for example "number" covering DECIMAL and DOUBLE).
        passed = (
            actual == normalized_expected
            or actual.startswith(f"{normalized_expected}(")
            or actual_family == normalized_expected
        )
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
