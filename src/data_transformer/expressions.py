from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import DataTransformerError
from .workspace import field_expression


@dataclass
class SQLFragment:
    sql: str
    parameters: list[Any] = field(default_factory=list)


def compile_condition(condition: Any) -> SQLFragment:
    if not isinstance(condition, dict) or not condition:
        raise DataTransformerError("E_CONDITION_INVALID", "condition must be a non-empty object")

    if "all" in condition or "any" in condition:
        key = "all" if "all" in condition else "any"
        if set(condition) != {key}:
            raise DataTransformerError(
                "E_CONDITION_INVALID", f"{key} cannot be combined with sibling keys"
            )
        values = condition[key]
        if not isinstance(values, list) or not values:
            raise DataTransformerError(
                "E_CONDITION_INVALID", f"{key} must contain at least one condition"
            )
        fragments = [compile_condition(value) for value in values]
        operator = " AND " if key == "all" else " OR "
        return SQLFragment(
            "(" + operator.join(fragment.sql for fragment in fragments) + ")",
            [parameter for fragment in fragments for parameter in fragment.parameters],
        )
    if "not" in condition:
        if set(condition) != {"not"}:
            raise DataTransformerError(
                "E_CONDITION_INVALID", "not cannot be combined with sibling keys"
            )
        fragment = compile_condition(condition["not"])
        return SQLFragment(f"NOT ({fragment.sql})", fragment.parameters)

    field = condition.get("field")
    if not isinstance(field, str) or not field:
        raise DataTransformerError("E_CONDITION_INVALID", "condition leaf requires a field")
    operators = [key for key in condition if key != "field"]
    if len(operators) != 1:
        raise DataTransformerError(
            "E_CONDITION_INVALID",
            "condition leaf requires exactly one comparison",
            {"comparisons": operators},
        )
    operator = operators[0]
    value = condition[operator]
    column = field_expression(field)
    binary = {
        "eq": "=",
        "ne": "!=",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
    }
    if operator in binary:
        if value is None and operator in {"eq", "ne"}:
            return SQLFragment(f"{column} IS {'NOT ' if operator == 'ne' else ''}NULL")
        return SQLFragment(f"{column} {binary[operator]} ?", [value])
    if operator in {"in", "not_in"}:
        if not isinstance(value, list) or not value:
            raise DataTransformerError(
                "E_CONDITION_INVALID", f"{operator} requires a non-empty array"
            )
        if len(value) > 10_000:
            raise DataTransformerError(
                "E_CONDITION_INVALID", f"{operator} contains too many values"
            )
        placeholders = ", ".join("?" for _ in value)
        keyword = "NOT IN" if operator == "not_in" else "IN"
        return SQLFragment(f"{column} {keyword} ({placeholders})", list(value))
    if operator in {"is_null", "not_null"}:
        if value is not True:
            raise DataTransformerError("E_CONDITION_INVALID", f"{operator} must be true")
        return SQLFragment(f"{column} IS {'NOT ' if operator == 'not_null' else ''}NULL")
    if operator in {"contains", "starts_with", "ends_with"}:
        if not isinstance(value, str):
            raise DataTransformerError("E_CONDITION_INVALID", f"{operator} requires a string")
        function = {
            "contains": "contains",
            "starts_with": "starts_with",
            "ends_with": "ends_with",
        }[operator]
        return SQLFragment(f"{function}({column}, ?)", [value])
    raise DataTransformerError(
        "E_CONDITION_INVALID", "unknown comparison", {"comparison": operator}
    )


def compile_expression(expression: Any) -> SQLFragment:
    if not isinstance(expression, dict) or len(expression) != 1:
        raise DataTransformerError(
            "E_EXPRESSION_INVALID", "expression must be an object with exactly one operation"
        )
    operation, value = next(iter(expression.items()))
    if operation == "field":
        if not isinstance(value, str):
            raise DataTransformerError("E_EXPRESSION_INVALID", "field expression requires a string")
        return SQLFragment(field_expression(value))
    if operation == "value":
        return SQLFragment("?", [value])

    if operation in {"add", "subtract", "multiply", "divide"}:
        items = _expression_list(operation, value, minimum=2)
        fragments = [compile_expression(item) for item in items]
        if operation == "divide" and len(fragments) != 2:
            raise DataTransformerError(
                "E_EXPRESSION_INVALID", "divide requires exactly two operands"
            )
        symbol = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}[operation]
        parameters = [item for fragment in fragments for item in fragment.parameters]
        if operation == "divide":
            left, right = fragments
            sql = (
                f"CASE WHEN ({right.sql}) = 0 THEN error('division by zero') "
                f"ELSE ({left.sql}) / ({right.sql}) END"
            )
            # The right expression appears twice in SQL, so its parameters do too.
            parameters = right.parameters + left.parameters + right.parameters
            return SQLFragment(sql, parameters)
        return SQLFragment(
            "(" + f" {symbol} ".join(fragment.sql for fragment in fragments) + ")",
            parameters,
        )

    if operation in {"concat", "coalesce"}:
        items = _expression_list(operation, value, minimum=1)
        fragments = [compile_expression(item) for item in items]
        function = "concat" if operation == "concat" else "coalesce"
        return SQLFragment(
            f"{function}(" + ", ".join(fragment.sql for fragment in fragments) + ")",
            [item for fragment in fragments for item in fragment.parameters],
        )
    if operation in {"lower", "upper"}:
        fragment = compile_expression(value)
        return SQLFragment(f"{operation}({fragment.sql})", fragment.parameters)
    if operation == "round":
        items = value if isinstance(value, list) else [value]
        if len(items) not in {1, 2}:
            raise DataTransformerError(
                "E_EXPRESSION_INVALID", "round requires an expression and optional digits"
            )
        fragment = compile_expression(items[0])
        digits = items[1] if len(items) == 2 else 0
        if isinstance(digits, bool) or not isinstance(digits, int) or not -30 <= digits <= 30:
            raise DataTransformerError(
                "E_EXPRESSION_INVALID", "round digits must be an integer from -30 to 30"
            )
        return SQLFragment(f"round({fragment.sql}, {digits})", fragment.parameters)
    if operation == "substring":
        if not isinstance(value, list) or len(value) not in {2, 3}:
            raise DataTransformerError(
                "E_EXPRESSION_INVALID",
                "substring requires [expression, start] or [expression, start, length]",
            )
        fragment = compile_expression(value[0])
        start = value[1]
        length = value[2] if len(value) == 3 else None
        if isinstance(start, bool) or not isinstance(start, int):
            raise DataTransformerError("E_EXPRESSION_INVALID", "substring start must be an integer")
        sql = f"substring({fragment.sql} FROM {start})"
        if length is not None:
            if isinstance(length, bool) or not isinstance(length, int) or length < 0:
                raise DataTransformerError(
                    "E_EXPRESSION_INVALID", "substring length must be a non-negative integer"
                )
            sql = f"substring({fragment.sql} FROM {start} FOR {length})"
        return SQLFragment(sql, fragment.parameters)
    raise DataTransformerError(
        "E_EXPRESSION_INVALID", "unknown expression operation", {"operation": operation}
    )


def _expression_list(operation: str, value: Any, minimum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) < minimum:
        raise DataTransformerError(
            "E_EXPRESSION_INVALID",
            f"{operation} requires at least {minimum} operands",
        )
    return value

