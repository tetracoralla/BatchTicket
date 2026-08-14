from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from decimal import Decimal

from mcp import types

from data_transformer import DataTransformer
from data_transformer.contracts import LimitsModel, TransformationPlan
from data_transformer.limits import Limits
from data_transformer.mcp_server import _root_choices, data_inspect, mcp


def _plan(rows, steps, **extra):
    return {"version": "1", "sources": {"rows": {"inline": rows}}, "steps": steps, **extra}


def test_internal_plan_field_names_execute_the_validated_aliases() -> None:
    result = DataTransformer().transform(
        _plan(
            [{"id": 1}],
            [{"op": "select", "fields": ["id"]}],
            assertions=[{"type": "row_count", "eq": 2}],
        )
    )

    assert result["error"]["code"] == "E_ASSERTION_FAILED"


def test_output_directory_is_rejected_before_staging(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = DataTransformer(workspace, restrict_paths=True).transform(
        _plan(
            [{"id": 1}],
            [{"op": "select", "fields": ["id"]}],
            output={"path": ".", "format": "json", "overwrite": True},
        )
    )

    assert result["error"]["code"] == "E_OUTPUT_INVALID"
    assert list(tmp_path.glob(".*adt-stage-*")) == []
    assert list(workspace.glob(".*adt-stage-*")) == []


def test_python_temporary_files_share_the_temp_budget(tmp_path) -> None:
    result = DataTransformer(tmp_path).transform(
        _plan(
            [{"payload": "larger than one byte"}],
            [{"op": "select", "fields": ["payload"]}],
            limits={"max_temp_bytes": 1},
        )
    )

    assert result["error"]["code"] == "E_TEMP_LIMIT"


def test_generated_output_is_rechecked_for_depth() -> None:
    flat = {"_".join(f"level{index}" for index in range(20)): 1}
    result = DataTransformer().transform(
        _plan(flat, [{"op": "unflatten"}], limits={"max_depth": 10})
    )

    assert result["error"]["code"] == "E_DEPTH_LIMIT"


def test_filter_join_and_pivot_reject_implicit_comparison_casts() -> None:
    filtered = DataTransformer().transform(
        _plan([{"id": 1}], [{"op": "filter", "where": {"field": "id", "eq": "1"}}])
    )
    assert filtered["error"]["code"] == "E_TYPE_MISMATCH"

    joined = DataTransformer().transform(
        {
            "version": "1",
            "sources": {
                "left": {"inline": [{"id": 1}]},
                "right": {"inline": [{"id": "1"}]},
            },
            "steps": [{"op": "join", "left": "left", "right": "right", "on": [["id", "id"]]}],
        }
    )
    assert joined["error"]["code"] == "E_TYPE_MISMATCH"

    pivoted = DataTransformer().transform(
        _plan(
            [{"key": 1, "value": 2}],
            [{"op": "pivot", "field": "key", "value": "value", "values": ["1"]}],
        )
    )
    assert pivoted["error"]["code"] == "E_TYPE_MISMATCH"


def test_csv_rejects_duplicate_headers_and_distinguishes_empty_from_null(tmp_path) -> None:
    duplicate = tmp_path / "duplicate.csv"
    duplicate.write_text("id,id\n1,2\n", encoding="utf-8")
    rejected = DataTransformer(tmp_path).inspect({"path": str(duplicate)})
    assert rejected["error"]["code"] == "E_DUPLICATE_FIELD"

    values = tmp_path / "values.csv"
    values.write_text('id,empty,missing,literal\n1,"",\\N,\\\\N\n', encoding="utf-8")
    inspected = DataTransformer(tmp_path).inspect({"path": str(values)})
    assert inspected["sample"] == [
        {"id": 1, "empty": "", "missing": None, "literal": r"\N"}
    ]


def test_exact_decimal_meaning_is_carrier_independent(tmp_path) -> None:
    decimal_text = "0.1234567890123456789012345"
    source = tmp_path / "source.json"
    source.write_text('[{"value":' + decimal_text + "}]", encoding="utf-8")
    parquet = tmp_path / "result.parquet"
    json_output = tmp_path / "result.json"

    first = DataTransformer(tmp_path).transform(
        {
            "version": "1",
            "sources": {"rows": {"path": str(source)}},
            "steps": [{"op": "select", "fields": ["value"]}],
            "output": {"path": str(parquet)},
            "return": {"mode": "inline"},
        }
    )
    assert first["result"]["data"] == [{"value": decimal_text}]

    parquet_value = DataTransformer(tmp_path).inspect({"path": str(parquet)})
    assert parquet_value["sample"] == [{"value": decimal_text}]

    second = DataTransformer(tmp_path).transform(
        {
            "version": "1",
            "sources": {"rows": {"path": str(parquet)}},
            "steps": [{"op": "select", "fields": ["value"]}],
            "output": {"path": str(json_output)},
        }
    )
    assert second["status"] == "ok"
    assert json.loads(json_output.read_text()) == [{"value": decimal_text}]

    inline = DataTransformer().inspect({"inline": [{"value": Decimal(decimal_text)}]})
    assert inline["sample"] == [{"value": decimal_text}]


def test_flatten_preserves_empty_objects_and_empty_table_schema(tmp_path) -> None:
    object_round_trip = DataTransformer().transform(
        _plan([{"id": 1, "metadata": {}}], [{"op": "flatten"}, {"op": "unflatten"}])
    )
    assert object_round_trip["result"]["data"] == [{"id": 1, "metadata": {}}]

    empty = tmp_path / "empty.csv"
    empty.write_text("id,name\n", encoding="utf-8")
    table_round_trip = DataTransformer(tmp_path).transform(
        {
            "version": "1",
            "sources": {"rows": {"path": str(empty)}},
            "steps": [{"op": "flatten"}, {"op": "unflatten"}],
        }
    )
    assert list(table_round_trip["receipt"]["final_shape"]["fields"]) == ["id", "name"]


def test_empty_schema_table_refuses_lossy_json_output(tmp_path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("id,name\n", encoding="utf-8")
    output = tmp_path / "empty.json"
    result = DataTransformer(tmp_path).transform(
        {
            "version": "1",
            "sources": {"rows": {"path": str(empty)}},
            "steps": [{"op": "select", "fields": ["id", "name"]}],
            "output": {"path": str(output)},
        }
    )

    assert result["error"]["code"] == "E_OUTPUT_SCHEMA_LOSS"
    assert not output.exists()


def test_library_api_works_from_python_c_without_spawn_traceback() -> None:
    code = (
        "from data_transformer import DataTransformer; import json; "
        "print(json.dumps(DataTransformer().inspect({'inline':[{'id':1}]})))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout)["status"] == "ok"
    assert "Traceback" not in completed.stderr


def test_mcp_handler_waits_do_not_block_the_event_loop(monkeypatch) -> None:
    def slow_inspect(self, source, *, sample_rows=5, limits=None):
        del self, source, sample_rows, limits
        time.sleep(0.2)
        return {"status": "ok"}

    monkeypatch.setattr(DataTransformer, "inspect", slow_inspect)

    async def exercise() -> float:
        started = time.monotonic()
        await asyncio.gather(
            data_inspect({"inline": [{"id": 1}]}),
            data_inspect({"inline": [{"id": 2}]}),
        )
        return time.monotonic() - started

    assert asyncio.run(exercise()) < 0.35


def test_unnamed_mcp_root_is_accepted(tmp_path) -> None:
    choices = _root_choices([types.Root(uri=tmp_path.resolve().as_uri(), name=None)])
    assert choices == {"root-1": tmp_path.resolve()}


def test_tree_mutation_receipt_reports_nested_changes() -> None:
    result = DataTransformer().transform(
        {
            "version": "1",
            "sources": {"tree": {"inline": {"a": {"b": 1}}, "kind": "tree"}},
            "steps": [{"op": "delete", "path": "a.b"}],
        }
    )
    receipt = result["receipt"]["steps"][0]
    assert receipt["fields_removed"] == ["/a/b"]
    assert receipt["values_changed"] == ["/", "/a"]


def test_published_schemas_express_runtime_hard_maximums() -> None:
    invalid = LimitsModel.model_validate
    try:
        invalid({"timeout_ms": Limits.HARD_MAX_TIMEOUT_MS + 1})
    except ValueError:
        pass
    else:
        raise AssertionError("hard timeout maximum was not enforced by the public model")

    schema = TransformationPlan.model_json_schema(by_alias=True)
    limits_schema = schema["$defs"]["LimitsModel"]["properties"]
    assert limits_schema["timeout_ms"]["anyOf"][0]["maximum"] == Limits.HARD_MAX_TIMEOUT_MS
    tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "data_transform")
    assert tool.parameters["$defs"]["LimitsModel"]["properties"]["max_rows"]["anyOf"][0][
        "maximum"
    ] == Limits.HARD_MAX_ROWS


def test_validation_truncation_flag_distinguishes_twenty_from_more() -> None:
    transformer = DataTransformer()
    twenty = [f"field_{index}" for index in range(20)]
    twenty_one = [*twenty, "field_20"]

    exact = transformer.validate(
        {"inline": {}, "kind": "tree"},
        schema={"type": "object", "required": twenty},
    )
    overflow = transformer.validate(
        {"inline": {}, "kind": "tree"},
        schema={"type": "object", "required": twenty_one},
    )

    assert exact["schema_validation"]["failures_truncated"] is False
    assert len(exact["schema_validation"]["failures"]) == 20
    assert overflow["schema_validation"]["failures_truncated"] is True
    assert len(overflow["schema_validation"]["failures"]) == 20
