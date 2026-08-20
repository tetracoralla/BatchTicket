from __future__ import annotations

from data_transformer import DataTransformer


def _plan(rows, steps):
    return {"version": "1", "sources": {"rows": {"inline": rows}}, "steps": steps}


def test_composed_conditions_and_string_comparisons() -> None:
    result = DataTransformer().transform(
        _plan(
            [
                {"id": 1, "name": "Alice", "status": "paid", "score": 9},
                {"id": 2, "name": "Bob", "status": "trial", "score": 7},
                {"id": 3, "name": "Chen", "status": "blocked", "score": 10},
            ],
            [
                {
                    "op": "filter",
                    "where": {
                        "all": [
                            {"field": "status", "in": ["paid", "trial"]},
                            {
                                "any": [
                                    {"field": "name", "starts_with": "A"},
                                    {"field": "score", "gte": 8},
                                ]
                            },
                            {"not": {"field": "name", "contains": "zzz"}},
                        ]
                    },
                }
            ],
        )
    )

    assert result["status"] == "ok"
    assert [row["id"] for row in result["result"]["data"]] == [1]


def test_expression_ast_string_and_numeric_operations() -> None:
    result = DataTransformer().transform(
        _plan(
            [{"first": "ALICE", "last": None, "price": 2.345, "quantity": 3}],
            [
                {"op": "derive", "field": "first", "expr": {"lower": {"field": "first"}}},
                {
                    "op": "derive",
                    "field": "last",
                    "expr": {"coalesce": [{"field": "last"}, {"value": "unknown"}]},
                },
                {
                    "op": "derive",
                    "field": "label",
                    "expr": {
                        "concat": [
                            {"field": "first"},
                            {"value": "-"},
                            {"upper": {"field": "last"}},
                        ]
                    },
                },
                {
                    "op": "derive",
                    "field": "short",
                    "expr": {"substring": [{"field": "label"}, 1, 5]},
                },
                {
                    "op": "derive",
                    "field": "total",
                    "expr": {
                        "round": [
                            {
                                "multiply": [
                                    {"field": "price"},
                                    {"field": "quantity"},
                                ]
                            },
                            2,
                        ]
                    },
                },
                {
                    "op": "derive",
                    "field": "adjusted",
                    "expr": {
                        "subtract": [
                            {"add": [{"field": "total"}, {"value": 1}]},
                            {"value": 0.5},
                        ]
                    },
                },
            ],
        )
    )

    assert result["status"] == "ok", result
    row = result["result"]["data"][0]
    assert row["label"] == "alice-UNKNOWN"
    assert row["short"] == "alice"
    assert row["total"] == "7.04"
    assert row["adjusted"] == "7.54"


def test_drop_and_rename_record_field_changes() -> None:
    result = DataTransformer().transform(
        _plan(
            [{"id": 1, "internal": "secret", "displayName": "Alice"}],
            [
                {"op": "drop", "fields": ["internal"]},
                {"op": "rename", "fields": {"displayName": "name"}},
            ],
        )
    )

    assert result["result"]["data"] == [{"id": 1, "name": "Alice"}]
    assert result["execution_effects"]["steps"][0]["fields_removed"] == ["internal"]


def test_group_aggregate_variants() -> None:
    result = DataTransformer().transform(
        _plan(
            [
                {"group": "a", "user": 1, "value": 4},
                {"group": "a", "user": 1, "value": 6},
                {"group": "a", "user": 2, "value": 10},
            ],
            [
                {
                    "op": "group",
                    "by": ["group"],
                    "aggregates": [
                        {"op": "count", "as": "rows"},
                        {"op": "count_distinct", "field": "user", "as": "users"},
                        {"op": "avg", "field": "value", "as": "average"},
                        {"op": "min", "field": "value", "as": "minimum"},
                        {"op": "max", "field": "value", "as": "maximum"},
                        {"op": "first", "field": "value", "as": "first"},
                        {"op": "last", "field": "value", "as": "last"},
                    ],
                }
            ],
        )
    )

    assert result["result"]["data"] == [
        {
            "group": "a",
            "rows": 3,
            "users": 2,
            "average": 20 / 3,
            "minimum": 4,
            "maximum": 10,
            "first": 4,
            "last": 10,
        }
    ]


def test_invalid_cast_returns_stable_error() -> None:
    result = DataTransformer().transform(
        _plan([{"value": "abc"}], [{"op": "cast", "field": "value", "to": "integer"}])
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_CAST_FAILED"


def test_lossy_cast_is_recorded() -> None:
    result = DataTransformer().transform(
        _plan([{"value": 2.7}], [{"op": "cast", "field": "value", "to": "integer"}])
    )

    assert result["status"] == "ok"
    assert result["execution_effects"]["warnings"][0]["code"] == "W_LOSSY_CAST"
