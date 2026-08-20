from __future__ import annotations

import json

from data_transformer import DataTransformer


def plan(rows, steps, **extra):
    return {
        "version": "1",
        "sources": {"rows": {"inline": rows}},
        "steps": steps,
        **extra,
    }


def test_conversation_projection_flow_is_deterministic() -> None:
    source = {
        "status": 200,
        "data": {
            "users": [
                {"userId": 123, "age": 34, "profile": {"name": "Alice"}},
                {"userId": 456, "age": 16, "profile": {"name": "Bob"}},
            ]
        },
    }
    transformation = {
        "version": "1",
        "sources": {"users": {"inline": source, "select": "data.users[*]"}},
        "steps": [
            {"id": "adults", "op": "filter", "where": {"field": "age", "gte": 18}},
            {
                "id": "shaped",
                "op": "select",
                "fields": [
                    {"field": "userId", "as": "id"},
                    {"field": "profile.name", "as": "name"},
                ],
            },
        ],
        "assert": [
            {"type": "not_null", "field": "id"},
            {"type": "unique", "field": "id"},
        ],
    }

    first = DataTransformer().transform(transformation)
    second = DataTransformer().transform(transformation)

    assert first["status"] == "ok"
    assert first["result"]["data"] == [{"id": 123, "name": "Alice"}]
    assert "receipt" not in first
    assert first["execution_effects"]["steps"][0]["row_delta"] == -1
    assert (
        first["execution_effects"]["result_sha256"]
        == second["execution_effects"]["result_sha256"]
    )


def test_filter_derive_cast_sort_and_limit() -> None:
    result = DataTransformer().transform(
        plan(
            [{"price": "2.5", "quantity": 3}, {"price": "10", "quantity": 2}],
            [
                {"op": "cast", "fields": {"price": "number"}},
                {
                    "op": "derive",
                    "field": "total",
                    "expr": {"multiply": [{"field": "price"}, {"field": "quantity"}]},
                },
                {"op": "sort", "by": [{"field": "total", "direction": "desc"}]},
                {"op": "limit", "count": 1},
            ],
        )
    )

    assert result["status"] == "ok"
    assert result["result"]["data"] == [{"price": 10.0, "quantity": 2, "total": 20.0}]


def test_dedupe_records_loss() -> None:
    result = DataTransformer().transform(
        plan(
            [{"id": 1, "v": "a"}, {"id": 1, "v": "b"}, {"id": 2, "v": "c"}],
            [{"op": "dedupe", "fields": ["id"], "keep": "first"}],
        )
    )

    assert result["result"]["data"] == [{"id": 1, "v": "a"}, {"id": 2, "v": "c"}]
    assert result["execution_effects"]["warnings"][0]["code"] == "W_ROWS_DEDUPED"


def test_join_and_group_use_named_sources() -> None:
    transformation = {
        "version": "1",
        "sources": {
            "orders": {
                "inline": [
                    {"order_id": 1, "customer_id": 10, "amount": 5},
                    {"order_id": 2, "customer_id": 10, "amount": 7},
                    {"order_id": 3, "customer_id": 20, "amount": 11},
                ]
            },
            "customers": {
                "inline": [
                    {"id": 10, "country": "US"},
                    {"id": 20, "country": "CN"},
                ]
            },
        },
        "steps": [
            {
                "id": "joined",
                "op": "join",
                "left": "orders",
                "right": "customers",
                "type": "left",
                "on": [["customer_id", "id"]],
            },
            {
                "id": "revenue",
                "op": "group",
                "by": ["country"],
                "aggregates": [{"op": "sum", "field": "amount", "as": "revenue"}],
            },
        ],
    }

    result = DataTransformer().transform(transformation)

    assert result["status"] == "ok"
    assert result["result"]["data"] == [
        {"country": "CN", "revenue": 11},
        {"country": "US", "revenue": 12},
    ]


def test_pivot_and_unpivot_round_trip_shape() -> None:
    pivoted = DataTransformer().transform(
        plan(
            [
                {"country": "CN", "quarter": "Q1", "revenue": 2},
                {"country": "CN", "quarter": "Q2", "revenue": 3},
                {"country": "US", "quarter": "Q1", "revenue": 5},
            ],
            [
                {
                    "id": "wide",
                    "op": "pivot",
                    "by": ["country"],
                    "field": "quarter",
                    "value": "revenue",
                    "values": ["Q1", "Q2"],
                },
                {
                    "op": "unpivot",
                    "fields": ["Q1", "Q2"],
                    "keep": ["country"],
                    "name_field": "quarter",
                    "value_field": "revenue",
                },
            ],
        )
    )

    assert pivoted["status"] == "ok"
    assert pivoted["summary"]["rows_out"] == 4
    assert pivoted["result"]["data"][0] == {
        "country": "CN",
        "quarter": "Q1",
        "revenue": 2,
    }


def test_explode_preserves_element_order() -> None:
    result = DataTransformer().transform(
        plan([{"id": 1, "tags": ["a", "b"]}], [{"op": "explode", "field": "tags"}])
    )

    assert result["result"]["data"] == [
        {"id": 1, "tags": "a"},
        {"id": 1, "tags": "b"},
    ]


def test_table_flatten_and_unflatten() -> None:
    result = DataTransformer().transform(
        plan(
            [{"id": 1, "profile": {"name": "A", "country": "CN"}}],
            [
                {"op": "flatten", "separator": "_"},
                {"op": "unflatten", "separator": "_"},
            ],
        )
    )

    assert result["status"] == "ok"
    assert result["result"]["data"] == [{"id": 1, "profile": {"country": "CN", "name": "A"}}]


def test_tree_set_delete_merge() -> None:
    transformation = {
        "version": "1",
        "sources": {"config": {"inline": {"a": {"b": 1}, "remove": True}}},
        "steps": [
            {"op": "set", "path": "a.c", "value": 2, "create": True},
            {"op": "delete", "path": "remove"},
            {"op": "merge", "value": {"a": {"d": 3}}},
        ],
    }

    result = DataTransformer().transform(transformation)

    assert result["result"]["data"] == {"a": {"b": 1, "c": 2, "d": 3}}


def test_unknown_expression_is_rejected_without_execution() -> None:
    result = DataTransformer().transform(
        plan(
            [{"x": 1}],
            [{"op": "derive", "field": "bad", "expr": {"python": "__import__('os')"}}],
        )
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_EXPRESSION_INVALID"


def test_division_by_zero_is_a_stable_domain_error() -> None:
    result = DataTransformer().transform(
        plan(
            [{"a": 1, "b": 0}],
            [
                {
                    "op": "derive",
                    "field": "ratio",
                    "expr": {"divide": [{"field": "a"}, {"field": "b"}]},
                }
            ],
        )
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_EXPRESSION_DOMAIN"


def test_assertion_failure_stops_transform() -> None:
    result = DataTransformer().transform(
        plan(
            [{"id": 1}, {"id": 1}],
            [{"op": "select", "fields": ["id"]}],
            **{"assert": [{"type": "unique", "field": "id"}]},
        )
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_ASSERTION_FAILED"


def test_schema_failure_stops_transform() -> None:
    result = DataTransformer().transform(
        plan(
            [{"id": "not-an-integer"}],
            [{"op": "select", "fields": ["id"]}],
            schema={
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        )
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_SCHEMA_FAILED"


def test_large_inline_result_requires_output_or_summary() -> None:
    result = DataTransformer().transform(
        plan(
            [{"text": "x" * 100}],
            [{"op": "select", "fields": ["text"]}],
            **{"return": {"mode": "auto", "max_inline_bytes": 10}},
        )
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_OUTPUT_REQUIRED"


def test_dry_run_never_writes_output(tmp_path) -> None:
    target = tmp_path / "result.json"
    result = DataTransformer(tmp_path).transform(
        plan(
            [{"id": 1}],
            [{"op": "select", "fields": ["id"]}],
            output={"path": str(target)},
            dry_run=True,
        )
    )

    assert result["status"] == "dry_run"
    assert not target.exists()


def test_output_is_atomic_and_protected_from_implicit_overwrite(tmp_path) -> None:
    target = tmp_path / "result.json"
    transformation = plan(
        [{"id": 1}],
        [{"op": "select", "fields": ["id"]}],
        output={"path": str(target)},
    )

    first = DataTransformer(tmp_path).transform(transformation)
    second = DataTransformer(tmp_path).transform(transformation)

    assert first["status"] == "ok"
    assert json.loads(target.read_text(encoding="utf-8")) == [{"id": 1}]
    assert second["status"] == "error"
    assert second["error"]["code"] == "E_OUTPUT_EXISTS"


def test_plan_rejects_unknown_operation() -> None:
    result = DataTransformer().transform(plan([{"id": 1}], [{"op": "run_shell"}]))

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_PLAN_INVALID"
