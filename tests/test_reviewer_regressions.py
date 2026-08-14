from __future__ import annotations

import json
from pathlib import Path

import duckdb

from data_transformer import DataTransformer
from data_transformer.contracts import TransformationPlan
from data_transformer.limits import Limits
from data_transformer.mcp_server import mcp
from data_transformer.workspace import Workspace


def _plan(rows, steps, **extra):
    return {"version": "1", "sources": {"rows": {"inline": rows}}, "steps": steps, **extra}


def test_mcp_workspace_rejects_absolute_parent_and_symlink_escape(tmp_path) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "inside.json").write_text('[{"id":1}]\n', encoding="utf-8")
    (outside / "secret.json").write_text('{"secret":true}\n', encoding="utf-8")
    (root / "escape").symlink_to(outside, target_is_directory=True)

    transformer = DataTransformer(root, restrict_paths=True)
    assert transformer.inspect({"path": "inside.json"})["status"] == "ok"
    for path in [str(outside / "secret.json"), "../outside/secret.json", "escape/secret.json"]:
        result = transformer.inspect({"path": path})
        assert result["status"] == "error"
        assert result["error"]["code"] == "E_PATH_OUTSIDE_WORKSPACE"

    missing_grant = DataTransformer(None, restrict_paths=True).inspect({"path": "inside.json"})
    assert missing_grant["error"]["code"] == "E_WORKSPACE_REQUIRED"


def test_whole_call_timeout_terminates_worker_and_next_call_recovers() -> None:
    timed_out = DataTransformer().transform(
        _plan(
            [{"value": index} for index in range(10_000)],
            [{"op": "sort", "by": ["value"]}],
            limits={"timeout_ms": 1},
        )
    )
    assert timed_out["error"]["code"] == "E_TIMEOUT"
    recovered = DataTransformer().inspect({"inline": [{"id": 1}]})
    assert recovered["status"] == "ok"

    memory_bounded = DataTransformer().inspect({"inline": [{"id": 1}]}, limits={"max_memory_mb": 1})
    assert memory_bounded["error"]["code"] == "E_MEMORY"


def test_cumulative_source_item_depth_and_response_limits() -> None:
    too_many_sources = {
        "version": "1",
        "sources": {"a": {"inline": [{"id": 1}]}, "b": {"inline": [{"id": 2}]}},
        "steps": [{"op": "join", "left": "a", "right": "b", "on": [["id", "id"]]}],
        "limits": {"max_sources": 1},
    }
    assert DataTransformer().transform(too_many_sources)["error"]["code"] == "E_SOURCE_LIMIT"

    cumulative_rows = {
        "version": "1",
        "sources": {"a": {"inline": [{"id": 1}]}, "b": {"inline": [{"id": 1}]}},
        "steps": [{"op": "join", "left": "a", "right": "b", "on": [["id", "id"]]}],
        "limits": {"max_rows": 1},
    }
    assert DataTransformer().transform(cumulative_rows)["error"]["code"] == "E_ROW_LIMIT"

    too_many_items = DataTransformer().inspect({"inline": [1, 2, 3]}, limits={"max_items": 3})
    assert too_many_items["error"]["code"] == "E_ITEM_LIMIT"
    too_deep = DataTransformer().inspect(
        {"inline": {"a": {"b": {"c": 1}}}}, limits={"max_depth": 3}
    )
    assert too_deep["error"]["code"] == "E_DEPTH_LIMIT"

    bounded = DataTransformer().inspect(
        {"inline": {"payload": "x" * 10_000}},
        limits={"max_response_bytes": 512},
    )
    assert bounded["error"]["code"] == "E_RESPONSE_TOO_LARGE"
    assert len(json.dumps(bounded).encode()) <= 512


def test_output_is_not_published_after_late_return_failure_and_dry_run_preflights(
    tmp_path,
) -> None:
    target = tmp_path / "result.json"
    failed = DataTransformer(tmp_path).transform(
        _plan(
            [{"text": "x" * 100}],
            [{"op": "select", "fields": ["text"]}],
            output={"path": str(target)},
            **{"return": {"mode": "inline", "max_inline_bytes": 10}},
        )
    )
    assert failed["error"]["code"] == "E_OUTPUT_TOO_LARGE"
    assert not target.exists()

    target.write_text("existing\n", encoding="utf-8")
    dry_run = DataTransformer(tmp_path).transform(
        _plan(
            [{"id": 1}],
            [{"op": "select", "fields": ["id"]}],
            output={"path": str(target)},
        ),
        dry_run=True,
    )
    assert dry_run["error"]["code"] == "E_OUTPUT_EXISTS"

    missing_reference = DataTransformer().transform(
        _plan(
            [{"id": 1}],
            [{"op": "select", "fields": ["id"]}],
            **{"return": {"mode": "reference"}},
        ),
        dry_run=True,
    )
    assert missing_reference["error"]["code"] == "E_OUTPUT_REQUIRED"


def test_published_and_live_mcp_plan_schemas_are_complete() -> None:
    published = json.loads(Path("schemas/transformation-plan.schema.json").read_text())
    generated = TransformationPlan.model_json_schema(by_alias=True)
    for metadata in ["$schema", "$id"]:
        published.pop(metadata, None)
    generated["title"] = "Agent Data Transformer Plan v1"
    assert published == generated

    tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "data_transform")
    schema = tool.parameters
    plan_ref = schema["properties"]["plan"]["$ref"].split("/")[-1]
    plan_schema = schema["$defs"][plan_ref]
    assert plan_schema["additionalProperties"] is False
    step_schema = plan_schema["properties"]["steps"]["items"]
    assert step_schema["discriminator"]["propertyName"] == "op"
    for reference in step_schema["oneOf"]:
        assert schema["$defs"][reference["$ref"].split("/")[-1]]["additionalProperties"] is False


def test_unknown_nested_plan_fields_are_rejected() -> None:
    condition = DataTransformer().transform(
        _plan(
            [{"id": 1}],
            [{"op": "filter", "where": {"all": [{"field": "id", "eq": 1}], "any": []}}],
        )
    )
    assert condition["error"]["code"] == "E_CONDITION_INVALID"
    unknown_step = DataTransformer().transform(
        _plan([{"id": 1}], [{"op": "select", "fields": ["id"], "mystery": True}])
    )
    assert unknown_step["error"]["code"] == "E_PLAN_INVALID"


def test_large_integer_to_double_reports_value_level_loss() -> None:
    result = DataTransformer().transform(
        _plan([{"id": 9_007_199_254_740_993}], [{"op": "cast", "field": "id", "to": "number"}])
    )
    assert result["status"] == "ok"
    warning = result["receipt"]["warnings"][0]
    assert warning["code"] == "W_LOSSY_CAST"
    assert warning["affected_rows"] == 1


def test_group_and_pivot_reject_duplicate_dimension_names_and_allow_aliases() -> None:
    rows = [{"a": {"id": 1}, "b": {"id": 2}, "value": 3, "key": "x"}]
    duplicate_group = DataTransformer().transform(
        _plan(
            rows,
            [{"op": "group", "by": ["a.id", "b.id"], "aggregates": [{"op": "count", "as": "n"}]}],
        )
    )
    assert duplicate_group["error"]["code"] == "E_DUPLICATE_FIELD"

    aliased = DataTransformer().transform(
        _plan(
            rows,
            [
                {
                    "op": "group",
                    "by": [{"field": "a.id", "as": "a_id"}, {"field": "b.id", "as": "b_id"}],
                    "aggregates": [{"op": "count", "as": "n"}],
                }
            ],
        )
    )
    assert aliased["result"]["data"] == [{"a_id": 1, "b_id": 2, "n": 1}]

    duplicate_pivot = DataTransformer().transform(
        _plan(
            rows,
            [
                {
                    "op": "pivot",
                    "by": ["a.id", "b.id"],
                    "field": "key",
                    "value": "value",
                    "values": ["x"],
                }
            ],
        )
    )
    assert duplicate_pivot["error"]["code"] == "E_DUPLICATE_FIELD"


def test_flatten_is_reversible_for_snake_case_and_unflatten_rejects_null_parent() -> None:
    row = {"customer_id": 1, "profile": {"display_name": "A"}}
    result = DataTransformer().transform(_plan([row], [{"op": "flatten"}, {"op": "unflatten"}]))
    assert result["result"]["data"] == [row]

    conflict = DataTransformer().transform(_plan([{"a": None, "a_b": 1}], [{"op": "unflatten"}]))
    assert conflict["error"]["code"] == "E_FIELD_COLLISION"


def test_empty_schema_carriers_keep_fields_and_inline_file_json_kinds_match(tmp_path) -> None:
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("id,name\n", encoding="utf-8")
    csv_result = DataTransformer(tmp_path).inspect({"path": str(csv_path)})
    assert list(csv_result["shape"]["fields"]) == ["id", "name"]

    parquet_path = tmp_path / "empty.parquet"
    connection = duckdb.connect()
    connection.execute(
        'COPY (SELECT CAST(NULL AS BIGINT) AS id, CAST(NULL AS VARCHAR) AS "name" '
        "WHERE false) "
        f"TO '{parquet_path}' (FORMAT PARQUET)"
    )
    connection.close()
    parquet_result = DataTransformer(tmp_path).inspect({"path": str(parquet_path)})
    assert list(parquet_result["shape"]["fields"]) == ["id", "name"]

    primitive_path = tmp_path / "values.json"
    primitive_path.write_text("[1,2,3]\n", encoding="utf-8")
    inline = DataTransformer().inspect({"inline": [1, 2, 3]})
    file_result = DataTransformer(tmp_path).inspect({"path": str(primitive_path)})
    selected = DataTransformer(tmp_path).inspect({"path": str(primitive_path), "select": "$"})
    assert inline["shape"]["kind"] == file_result["shape"]["kind"] == selected["shape"]["kind"]


def test_join_receipt_reports_both_inputs_unmatched_fanout_and_new_nulls() -> None:
    plan = {
        "version": "1",
        "sources": {
            "left": {"inline": [{"id": 1}, {"id": 2}]},
            "right": {"inline": [{"id": 1, "v": "a"}, {"id": 1, "v": "b"}]},
        },
        "steps": [
            {"op": "join", "left": "left", "right": "right", "type": "left", "on": [["id", "id"]]}
        ],
    }
    result = DataTransformer().transform(plan)
    receipt = result["receipt"]["steps"][0]
    assert receipt["inputs"]["left"]["rows"] == 2
    assert receipt["inputs"]["right"]["rows"] == 2
    assert receipt["matches"]["pairs"] == 2
    assert receipt["unmatched"]["left_rows"] == 1
    assert receipt["fan_out"]["extra_left_copies"] == 1
    assert receipt["new_nulls"][0]["rows"] == 1
    assert {warning["code"] for warning in receipt["warnings"]} >= {
        "W_JOIN_UNMATCHED",
        "W_JOIN_FANOUT",
        "W_NULLS_INTRODUCED",
    }


def test_tree_diff_reports_global_scan_truncation_and_empty_tables_compare() -> None:
    left = list(range(10_001))
    right = [value + 1 for value in left]
    truncated = DataTransformer().diff(
        {"inline": left, "kind": "tree"},
        {"inline": right, "kind": "tree"},
        sample_rows=1,
    )
    assert truncated["scan_truncated"] is True
    assert truncated["change_count"] is None
    assert truncated["count_lower_bound"] == 10_001

    empty = DataTransformer().diff({"inline": []}, {"inline": []})
    assert empty["status"] == "ok"
    assert empty["identical"] is True


def test_type_assertion_uses_duckdb_families_and_unpivot_rejects_bad_keep() -> None:
    integer = DataTransformer().transform(
        _plan(
            [{"group": "a", "value": 1}, {"group": "a", "value": 2}],
            [
                {
                    "op": "group",
                    "by": ["group"],
                    "aggregates": [{"op": "sum", "field": "value", "as": "total"}],
                }
            ],
            **{"assert": [{"type": "type", "field": "total", "is": "integer"}]},
        )
    )
    assert integer["status"] == "ok"

    for keep in [["id", "id"], ["id", "a"]]:
        invalid = DataTransformer().transform(
            _plan([{"id": 1, "a": 2}], [{"op": "unpivot", "fields": ["a"], "keep": keep}])
        )
        assert invalid["error"]["code"] == "E_DUPLICATE_FIELD"


def test_shape_cache_avoids_repeated_structural_scans(monkeypatch) -> None:
    with Workspace(Limits()) as workspace:
        dataset = workspace.load_source({"inline": [{"id": 1}]}, "rows", Path.cwd(), Limits())
        real_count = workspace.row_count
        calls = 0

        def counted(table: str) -> int:
            nonlocal calls
            calls += 1
            return real_count(table)

        monkeypatch.setattr(workspace, "row_count", counted)
        assert workspace.shape(dataset) == workspace.shape(dataset)
        assert calls == 1
