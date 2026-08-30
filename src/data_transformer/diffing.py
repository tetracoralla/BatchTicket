from __future__ import annotations

from collections.abc import Iterator
from itertools import islice
from typing import Any

from .dataset import DataSet
from .errors import DataTransformerError
from .json_values import json_safe
from .workspace import INTERNAL_ORDER, Workspace, _tree_type, quote_identifier


def diff_datasets(
    workspace: Workspace,
    left: DataSet,
    right: DataSet,
    key_fields: list[str] | None,
    sample_rows: int,
) -> dict[str, Any]:
    if left.kind != right.kind:
        return {
            "identical": False,
            "kind_change": {"from": left.kind, "to": right.kind},
            "row_comparison": "not_comparable",
        }
    if left.is_tree:
        scan_limit = 10_000
        changes = list(islice(_iter_tree_diff(left.value, right.value, "$"), scan_limit + 1))
        scan_truncated = len(changes) > scan_limit
        visible_changes = changes[:scan_limit]
        result: dict[str, Any] = {
            "identical": not changes,
            "changes": visible_changes[:sample_rows],
            "changes_truncated": scan_truncated or len(visible_changes) > sample_rows,
            "scan_truncated": scan_truncated,
        }
        if scan_truncated:
            result["change_count"] = None
            result["count_lower_bound"] = scan_limit + 1
        else:
            result["change_count"] = len(visible_changes)
        return result
    return _table_diff(workspace, left, right, key_fields or [], sample_rows)


def _table_diff(
    workspace: Workspace,
    left: DataSet,
    right: DataSet,
    key_fields: list[str],
    sample_rows: int,
) -> dict[str, Any]:
    left_table = left.table_name or ""
    right_table = right.table_name or ""
    left_columns = workspace.columns(left_table)
    right_columns = workspace.columns(right_table)
    left_types = workspace.column_types(left_table)
    right_types = workspace.column_types(right_table)
    added_fields = [field for field in right_columns if field not in left_columns]
    removed_fields = [field for field in left_columns if field not in right_columns]
    type_changes = [
        {"field": field, "from": left_types[field], "to": right_types[field]}
        for field in left_columns
        if field in right_types and left_types[field] != right_types[field]
    ]
    result: dict[str, Any] = {
        "left_rows": workspace.row_count(left_table),
        "right_rows": workspace.row_count(right_table),
        "schema": {
            "added_fields": added_fields,
            "removed_fields": removed_fields,
            "type_changes": type_changes,
        },
    }
    if added_fields or removed_fields or type_changes:
        result["identical"] = False
        result["row_comparison"] = "not_comparable"
        return result

    if key_fields:
        missing = sorted(set(key_fields) - set(left_columns))
        if missing:
            raise DataTransformerError(
                "E_FIELD_NOT_FOUND", "diff key fields do not exist", {"fields": missing}
            )
        _require_unique_keys(workspace, left_table, key_fields, "left")
        _require_unique_keys(workspace, right_table, key_fields, "right")
        result.update(
            _keyed_diff(workspace, left_table, right_table, left_columns, key_fields, sample_rows)
        )
    else:
        result.update(_unkeyed_diff(workspace, left_table, right_table, left_columns, sample_rows))
    result["identical"] = (
        not result["added_rows"]
        and not result["removed_rows"]
        and not result.get("changed_rows", 0)
    )
    return result


def _require_unique_keys(workspace: Workspace, table: str, keys: list[str], side: str) -> None:
    group = ", ".join(quote_identifier(key) for key in keys)
    duplicate = workspace.connection.execute(
        f"SELECT 1 FROM {quote_identifier(table)} GROUP BY {group} HAVING count(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate:
        raise DataTransformerError(
            "E_DIFF_KEY_NOT_UNIQUE",
            "diff keys are not unique",
            {"side": side, "keys": keys},
        )


def _keyed_diff(
    workspace: Workspace,
    left: str,
    right: str,
    columns: list[str],
    keys: list[str],
    sample_rows: int,
) -> dict[str, Any]:
    join = " AND ".join(
        f"l.{quote_identifier(key)} IS NOT DISTINCT FROM r.{quote_identifier(key)}" for key in keys
    )
    left_missing = f"l.{quote_identifier(INTERNAL_ORDER)} IS NULL"
    right_missing = f"r.{quote_identifier(INTERNAL_ORDER)} IS NULL"
    non_keys = [column for column in columns if column not in keys]
    changed = (
        " OR ".join(
            f"l.{quote_identifier(column)} IS DISTINCT FROM r.{quote_identifier(column)}"
            for column in non_keys
        )
        or "FALSE"
    )
    base = f"FROM {quote_identifier(left)} l FULL JOIN {quote_identifier(right)} r ON {join}"
    counts = workspace.connection.execute(
        f"SELECT count(*) FILTER (WHERE {left_missing}), "
        f"count(*) FILTER (WHERE {right_missing}), "
        f"count(*) FILTER (WHERE NOT ({left_missing}) AND NOT ({right_missing}) AND ({changed})) "
        f"{base}"
    ).fetchone()
    samples: list[dict[str, Any]] = []
    if sample_rows:
        key_select = ", ".join(
            f"coalesce(l.{quote_identifier(key)}, r.{quote_identifier(key)}) "
            f"AS {quote_identifier(key)}"
            for key in keys
        )
        rows = workspace.connection.execute(
            f"SELECT {key_select}, CASE WHEN {left_missing} THEN 'added' "
            f"WHEN {right_missing} THEN 'removed' ELSE 'changed' END AS change "
            f"{base} WHERE {left_missing} OR {right_missing} OR ({changed}) LIMIT ?",
            [sample_rows],
        ).fetchall()
        samples = [
            {
                **{key: json_safe(value) for key, value in zip(keys, row[:-1], strict=True)},
                "change": row[-1],
            }
            for row in rows
        ]
    return {
        "added_rows": int(counts[0]),
        "removed_rows": int(counts[1]),
        "changed_rows": int(counts[2]),
        "sample": samples,
    }


def _unkeyed_diff(
    workspace: Workspace,
    left: str,
    right: str,
    columns: list[str],
    sample_rows: int,
) -> dict[str, Any]:
    if not columns:
        left_rows = workspace.row_count(left)
        right_rows = workspace.row_count(right)
        return {
            "added_rows": max(0, right_rows - left_rows),
            "removed_rows": max(0, left_rows - right_rows),
            "sample": [],
        }
    select = ", ".join(quote_identifier(column) for column in columns)
    added_query = (
        f"SELECT {select} FROM {quote_identifier(right)} EXCEPT ALL "
        f"SELECT {select} FROM {quote_identifier(left)}"
    )
    removed_query = (
        f"SELECT {select} FROM {quote_identifier(left)} EXCEPT ALL "
        f"SELECT {select} FROM {quote_identifier(right)}"
    )
    added = int(workspace.connection.execute(f"SELECT count(*) FROM ({added_query})").fetchone()[0])
    removed = int(
        workspace.connection.execute(f"SELECT count(*) FROM ({removed_query})").fetchone()[0]
    )
    samples: list[dict[str, Any]] = []
    if sample_rows:
        rows = workspace.connection.execute(
            f"SELECT *, 'added' AS __change FROM ({added_query}) LIMIT ?", [sample_rows]
        ).fetchall()
        samples.extend(_row_samples(columns, rows))
        remaining = sample_rows - len(samples)
        if remaining:
            rows = workspace.connection.execute(
                f"SELECT *, 'removed' AS __change FROM ({removed_query}) LIMIT ?", [remaining]
            ).fetchall()
            samples.extend(_row_samples(columns, rows))
    return {"added_rows": added, "removed_rows": removed, "sample": samples}


def _row_samples(columns: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            "row": {
                column: json_safe(value) for column, value in zip(columns, row[:-1], strict=True)
            },
            "change": row[-1],
        }
        for row in rows
    ]


def _iter_tree_diff(left: Any, right: Any, path: str) -> Iterator[dict[str, Any]]:
    if type(left) is not type(right):
        yield {
            "path": path,
            "change": "type",
            "from": _tree_type(left),
            "to": _tree_type(right),
        }
        return
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}"
            if key not in left:
                yield {"path": child, "change": "added", "value": json_safe(right[key])}
            elif key not in right:
                yield {"path": child, "change": "removed", "value": json_safe(left[key])}
            else:
                yield from _iter_tree_diff(left[key], right[key], child)
    elif isinstance(left, list):
        for index in range(max(len(left), len(right))):
            child = f"{path}[{index}]"
            if index >= len(left):
                yield {"path": child, "change": "added", "value": json_safe(right[index])}
            elif index >= len(right):
                yield {"path": child, "change": "removed", "value": json_safe(left[index])}
            else:
                yield from _iter_tree_diff(left[index], right[index], child)
    elif left != right:
        yield {
            "path": path,
            "change": "changed",
            "from": json_safe(left),
            "to": json_safe(right),
        }
