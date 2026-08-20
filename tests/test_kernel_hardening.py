from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from data_transformer import DataTransformer
from data_transformer.limits import Limits
from data_transformer.workspace import BoundedConnection, Workspace


def test_mixed_scale_decimal_column_parses_and_stays_exact(tmp_path) -> None:
    # 100.0 needs 3 integer digits while a sibling value carries 14 decimals;
    # per-value precision maxima underestimate the uniform column precision.
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"price": 100.0}),
                json.dumps({"price": 212.33999999999997}),
                json.dumps({"price": 0.5}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = DataTransformer(tmp_path).inspect({"path": str(path)}, sample_rows=3)

    assert result["status"] == "ok"
    assert result["shape"]["fields"]["price"]["type"] == "decimal(17,14)"
    # A uniform column scale pads narrower values with trailing zeros; the
    # exact values are preserved.
    assert [Decimal(row["price"]) for row in result["sample"]] == [
        Decimal("100.0"),
        Decimal("212.33999999999997"),
        Decimal("0.5"),
    ]


def test_csv_integer_overflowing_bigint_keeps_exact_digits(tmp_path) -> None:
    path = tmp_path / "big.csv"
    path.write_text("id\n12345678901234567890\n42\n", encoding="utf-8")

    result = DataTransformer(tmp_path).inspect({"path": str(path)}, sample_rows=2)

    assert result["status"] == "ok"
    # Column-level inference: the overflow value makes the whole column decimal,
    # so exact digits survive and ordinary integers render as decimals.
    assert [Decimal(row["id"]) for row in result["sample"]] == [
        Decimal("12345678901234567890"),
        Decimal("42"),
    ]


def test_decimal_restore_uses_batched_statements(monkeypatch) -> None:
    calls = 0
    real_execute = BoundedConnection.execute

    def counted(self, query, parameters=None):
        nonlocal calls
        calls += 1
        return real_execute(self, query, parameters)

    monkeypatch.setattr(BoundedConnection, "execute", counted)

    with Workspace(Limits()) as workspace:
        dataset = workspace.load_source(
            {"inline": [{"price": index + 0.25} for index in range(3_000)]},
            "rows",
            Path.cwd(),
            Limits(),
        )
        assert workspace.row_count(dataset.table_name or "") == 3_000

    assert calls < 100


def test_many_step_plans_do_not_recharge_the_item_budget() -> None:
    rows = [{"a": index, "b": "x", "c": index % 7} for index in range(30_000)]
    steps = [{"op": "filter", "where": {"field": "b", "eq": "x"}} for _ in range(41)]

    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {"rows": {"inline": rows}},
            "steps": steps,
            "return": {"mode": "summary"},
        }
    )

    assert result["status"] == "ok"
    assert result["summary"]["rows_out"] == 30_000


def test_fan_out_step_cannot_exceed_the_structural_item_limit() -> None:
    row = {f"field_{index}": index for index in range(300)}

    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {"rows": {"inline": [row]}},
            "steps": [
                {
                    "id": "expanded",
                    "op": "unpivot",
                    "source": "rows",
                    "fields": list(row),
                }
            ],
            "limits": {"max_items": 700},
            "return": {"mode": "summary", "sample_rows": 0},
        }
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_ITEM_LIMIT"
    assert result["error"]["details"] == {
        "source": "expanded",
        "items": 900,
        "maximum": 700,
    }


def test_tabular_file_structures_are_depth_checked_at_load(tmp_path) -> None:
    nested = current = {}
    for _index in range(150):
        child: dict[str, dict] = {}
        current["n"] = child
        current = child
    current["leaf"] = 1
    path = tmp_path / "deep.json"
    path.write_text(json.dumps([nested]), encoding="utf-8")

    result = DataTransformer(tmp_path).inspect({"path": str(path)}, limits={"max_depth": 100})

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_DEPTH_LIMIT"


def test_scalar_table_sources_are_item_accounted_at_load(tmp_path) -> None:
    path = tmp_path / "rows.csv"
    path.write_text("a,b,c\n1,x,2\n", encoding="utf-8")

    # 1 row with 3 columns is 4 items; a 3-item budget must reject it.
    result = DataTransformer(tmp_path).inspect({"path": str(path)}, limits={"max_items": 3})

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_ITEM_LIMIT"


def test_delimited_header_width_is_bounded_by_the_item_limit(tmp_path) -> None:
    path = tmp_path / "wide.csv"
    path.write_text("a,b,c,d,e\n1,2,3,4,5\n", encoding="utf-8")

    result = DataTransformer(tmp_path).inspect({"path": str(path)}, limits={"max_items": 4})

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_ITEM_LIMIT"


def test_empty_schema_bearing_table_is_bounded_by_the_item_limit(tmp_path) -> None:
    csv_path = tmp_path / "empty.csv"
    parquet_path = tmp_path / "empty.parquet"
    csv_path.write_text(
        ",".join(f"field_{index}" for index in range(20)) + "\n",
        encoding="utf-8",
    )
    written = DataTransformer(tmp_path).transform(
        {
            "version": "1",
            "sources": {"rows": {"path": str(csv_path)}},
            "steps": [{"op": "limit", "count": 0}],
            "output": {"path": str(parquet_path), "format": "parquet"},
            "return": {"mode": "reference"},
        }
    )
    assert written["status"] == "ok", written

    result = DataTransformer(tmp_path).inspect(
        {"path": str(parquet_path)}, limits={"max_items": 10}
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_ITEM_LIMIT"
    assert result["error"]["details"] == {
        "source": "source",
        "items": 20,
        "maximum": 10,
    }


def test_explode_reports_rows_dropped_for_empty_and_null_arrays() -> None:
    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {
                "rows": {
                    "inline": [
                        {"id": 1, "tags": ["a", "b"]},
                        {"id": 2, "tags": []},
                        {"id": 3, "tags": None},
                    ]
                }
            },
            "steps": [{"op": "explode", "field": "tags"}],
        }
    )

    assert result["result"]["data"] == [
        {"id": 1, "tags": "a"},
        {"id": 1, "tags": "b"},
    ]
    warnings = result["execution_effects"]["warnings"]
    assert {"code": "W_EXPLODE_EMPTY", "field": "tags", "dropped_rows": 2} in warnings


def test_object_tree_sample_is_bounded_to_sample_rows(tmp_path) -> None:
    path = tmp_path / "wide.json"
    path.write_text(
        json.dumps({f"key_{index}": {"v": index} for index in range(2_000)}),
        encoding="utf-8",
    )

    result = DataTransformer(tmp_path).inspect({"path": str(path)}, sample_rows=5)

    assert result["status"] == "ok"
    assert len(result["sample"]) == 5
    assert list(result["sample"]) == ["key_0", "key_1", "key_2", "key_3", "key_4"]
    assert result["shape"]["field_count"] == 2_000


def test_wide_object_transform_fits_the_response_budget() -> None:
    big = {f"k{index}": {"v": index, "s": "x"} for index in range(50_000)}

    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {"tree": {"inline": big, "kind": "tree"}},
            "steps": [{"op": "set", "path": "k0.v", "value": 99}],
            "return": {"mode": "summary"},
        }
    )

    assert result["status"] == "ok"
    final_shape = result["execution_effects"]["final_shape"]
    assert len(final_shape["fields"]) == 1_000
    assert final_shape["fields_truncated"] is True
    assert final_shape["field_count"] == 50_000


def test_wide_table_shape_listing_is_bounded(tmp_path) -> None:
    path = tmp_path / "wide.csv"
    columns = [f"col_{index}" for index in range(1_200)]
    path.write_text(
        ",".join(columns) + "\n" + ",".join("1" for _ in columns) + "\n",
        encoding="utf-8",
    )

    result = DataTransformer(tmp_path).inspect({"path": str(path)})

    assert result["status"] == "ok"
    assert len(result["shape"]["fields"]) == 1_000
    assert result["shape"]["fields_truncated"] is True
    assert result["shape"]["columns"] == 1_200


def test_validation_decimal_semantics_are_carrier_independent(tmp_path) -> None:
    table_path = tmp_path / "prices.jsonl"
    table_path.write_text('{"price": 10.5, "other": 5.0}\n', encoding="utf-8")

    def table(schema: dict) -> str:
        return DataTransformer(tmp_path).validate({"path": str(table_path)}, schema=schema)[
            "schema_validation"
        ]["status"]

    def tree(schema: dict) -> str:
        return DataTransformer().validate(
            {"inline": {"price": 10.5, "other": 5.0}, "kind": "tree"}, schema=schema
        )["schema_validation"]["status"]

    number_schema = {
        "type": "object",
        "properties": {"price": {"type": "number"}},
        "required": ["price"],
    }
    string_schema = {
        "type": "object",
        "properties": {"price": {"type": "string"}},
        "required": ["price"],
    }
    integer_schema = {
        "type": "object",
        "properties": {"other": {"type": "integer"}},
        "required": ["other"],
    }

    assert table(number_schema) == "passed"
    assert tree(number_schema) == "passed"
    assert table(string_schema) == "failed"
    assert tree(string_schema) == "failed"
    assert table(integer_schema) == "passed"
    assert tree(integer_schema) == "passed"


def test_tree_steps_do_not_recharge_the_source_item_budget() -> None:
    source = {f"key_{index}": index for index in range(1_000)}
    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {"tree": {"inline": source, "kind": "tree"}},
            "steps": [
                {"op": "set", "path": "key_0", "value": index}
                for index in range(10)
            ],
            "limits": {"max_items": 1_100},
            "return": {"mode": "summary"},
        }
    )

    assert result["status"] == "ok"


def test_nested_exact_decimals_survive_table_and_flatten_paths() -> None:
    exact = Decimal("212.33999999999997")
    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {
                "rows": {
                    "inline": [
                        {
                            "id": 1,
                            "profile": {"price": exact},
                            "prices": [Decimal("0.1"), Decimal("0.2")],
                        }
                    ]
                }
            },
            "steps": [
                {"op": "flatten", "separator": "_"},
                {"op": "unflatten", "separator": "_"},
            ],
        }
    )

    assert result["status"] == "ok"
    row = result["result"]["data"][0]
    assert row["profile"]["price"] == str(exact)
    assert row["prices"] == ["0.1", "0.2"]


def test_flatten_collision_warns_and_keeps_escaped_field() -> None:
    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {
                "rows": {
                    "inline": {"rows": [{"a": {"b": 1}, "a_b": 2}]},
                    "select": "rows[*]",
                    "kind": "table",
                }
            },
            "steps": [{"op": "flatten"}],
        }
    )

    assert result["status"] == "ok"
    # The escaped representation is preserved; the rename is no longer silent.
    assert result["result"]["data"] == [{"a_b": 1, "a\\_b": 2}]
    collision = {"code": "W_FLATTEN_COLLISION", "field": "a_b", "renamed_to": "a\\_b"}
    assert collision in result["execution_effects"]["steps"][0]["warnings"]
    assert collision in result["execution_effects"]["warnings"]

    quiet = DataTransformer().transform(
        {
            "version": "1",
            "sources": {
                "rows": {
                    "inline": {"rows": [{"id": 1, "profile": {"name": "A"}}]},
                    "select": "rows[*]",
                    "kind": "table",
                }
            },
            "steps": [{"op": "flatten"}],
        }
    )
    assert quiet["status"] == "ok"
    assert quiet["execution_effects"]["steps"][0]["warnings"] == []
    assert quiet["execution_effects"]["warnings"] == []


def test_flatten_collision_warning_covers_nested_paths() -> None:
    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {
                "tree": {
                    "inline": {"x": {"a": {"b": 1}, "a_b": 2}},
                    "kind": "tree",
                }
            },
            "steps": [{"op": "flatten"}],
        }
    )

    collision = {
        "code": "W_FLATTEN_COLLISION",
        "field": "x_a_b",
        "renamed_to": "x_a\\_b",
    }
    assert collision in result["execution_effects"]["steps"][0]["warnings"]
    assert collision in result["execution_effects"]["warnings"]


def test_flatten_collision_warning_covers_paths_split_across_rows() -> None:
    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {
                "rows": {
                    "inline": [{"a": {"b": 1}}, {"a_b": 2}],
                }
            },
            "steps": [{"op": "flatten"}],
        }
    )

    collision = {
        "code": "W_FLATTEN_COLLISION",
        "field": "a_b",
        "renamed_to": "a\\_b",
    }
    assert collision in result["execution_effects"]["steps"][0]["warnings"]
    assert collision in result["execution_effects"]["warnings"]


def test_tabular_mixed_type_fields_are_rejected_instead_of_coerced() -> None:
    result = DataTransformer().inspect({"inline": [{"value": 1}, {"value": "1"}]})

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_TYPE_MISMATCH"


def test_json_schema_decimal_keywords_use_exact_schema_numbers() -> None:
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "type": "number",
                "const": 0.1,
                "minimum": 0.1,
                "maximum": 0.1,
                "multipleOf": 0.1,
            }
        },
        "required": ["value"],
    }

    for source in (
        {"inline": {"value": 0.1}, "kind": "tree"},
        {"inline": [{"value": 0.1}]},
    ):
        result = DataTransformer().validate(source, schema=schema)
        assert result["status"] == "ok"
        assert result["valid"] is True


def test_truncated_tree_effects_report_original_counts() -> None:
    source = {f"key_{index}": index for index in range(1_500)}
    replacement = {f"key_{index}": index + 1 for index in range(1_500)}
    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {"tree": {"inline": source, "kind": "tree"}},
            "steps": [{"op": "merge", "value": replacement}],
            "return": {"mode": "summary"},
        }
    )

    assert result["status"] == "ok"
    step = result["execution_effects"]["steps"][0]
    assert len(step["values_changed"]) == 1_000
    assert step["truncated"]["values_changed"] == {
        "total": 1_501,
        "returned": 1_000,
    }


def test_decimal_rewrite_counts_peak_temporary_storage() -> None:
    result = DataTransformer().inspect(
        {
            "inline": [
                {
                    "value": Decimal("0.123456789012345678"),
                    "padding": "x" * 100,
                }
            ]
        },
        limits={"max_temp_bytes": 200},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_TEMP_LIMIT"


def test_sub_micro_decimals_ingest_without_scientific_notation() -> None:
    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {
                "rows": {
                    "inline": {"rows": [{"x": 0.0000001}]},
                    "select": "rows[*]",
                    "kind": "table",
                }
            },
            "steps": [{"op": "select", "fields": ["x"]}],
        }
    )

    # str(Decimal) emits 1E-7 for this magnitude; the ingestion boundary must
    # round-trip it as fixed-point or the source fails to parse.
    assert result["status"] == "ok"
    assert result["result"]["data"] == [{"x": "0.0000001"}]


def test_json_schema_multiple_of_yields_verdicts_on_double_lane(tmp_path) -> None:
    parquet = tmp_path / "doubles.parquet"
    written = DataTransformer().transform(
        {
            "version": "1",
            "sources": {
                "rows": {
                    "inline": {"rows": [{"value": "1.5"}, {"value": "2.6"}]},
                    "select": "rows[*]",
                    "kind": "table",
                }
            },
            "steps": [{"op": "cast", "field": "value", "to": "double"}],
            "output": {"path": str(parquet), "format": "parquet"},
        }
    )
    assert written["status"] == "ok"

    result = DataTransformer().validate(
        {"path": str(parquet)},
        schema={
            "type": "object",
            "properties": {"value": {"type": "number", "multipleOf": 0.5}},
            "required": ["value"],
        },
    )

    # A DOUBLE-lane float against an exact-decimal multipleOf used to raise
    # TypeError and surface as E_INTERNAL instead of a validation verdict.
    assert result["status"] == "ok"
    assert result["valid"] is False
    failures = result["schema_validation"]["failures"]
    assert len(failures) == 1
    assert failures[0]["keyword"] == "multipleOf"
    assert failures[0]["row"] == 1
