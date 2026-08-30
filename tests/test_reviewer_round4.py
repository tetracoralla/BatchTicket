from __future__ import annotations

import asyncio
import json
from pathlib import Path

from data_transformer.mcp_server import data_transform
from data_transformer.runtime import DataTransformer


def _plan(tmp_path: Path, *, dry_run: bool, tree: bool, output_format: str) -> dict:
    if tree:
        sources = {"a": {"inline": {"x": 1}}}
        steps = [{"id": "s", "op": "merge", "value": {"y": 2}}]
    else:
        csv_path = tmp_path / "empty.csv"
        csv_path.write_text("a,b\n", encoding="utf-8")
        sources = {"a": {"path": str(csv_path)}}
        steps = [{"id": "s", "op": "limit", "count": 0}]
    return {
        "version": "1",
        "sources": sources,
        "steps": steps,
        "output": {"path": str(tmp_path / "out.file"), "format": output_format},
        "dry_run": dry_run,
    }


def test_dry_run_with_inline_mode_returns_summary_not_data() -> None:
    transformer = DataTransformer()
    plan = {
        "version": "1",
        "sources": {"a": {"inline": [{"x": 1}, {"x": 2}]}},
        "steps": [{"id": "s", "op": "select", "fields": ["x"]}],
        "return": {"mode": "inline"},
    }

    dry = transformer.transform({**plan, "dry_run": True})
    assert dry["status"] == "dry_run"
    assert dry["result"] == {"kind": "summary"}

    real = transformer.transform(plan)
    assert real["status"] == "ok"
    assert real["result"]["kind"] == "inline"
    assert real["result"]["data"] == [{"x": 1}, {"x": 2}]


def test_dry_run_large_auto_result_honors_planned_output_without_writing(tmp_path) -> None:
    target = tmp_path / "large.json"
    transformer = DataTransformer(tmp_path)
    plan = {
        "version": "1",
        "sources": {"a": {"inline": [{"text": "x" * 100}]}},
        "steps": [{"id": "s", "op": "select", "fields": ["text"]}],
        "output": {"path": str(target)},
        "return": {"mode": "auto", "max_inline_bytes": 10},
    }

    dry = transformer.transform({**plan, "dry_run": True})
    assert dry["status"] == "dry_run"
    assert dry["result"] == {"kind": "summary"}
    assert not target.exists()

    real = transformer.transform(plan)
    assert real["status"] == "ok"
    assert real["result"]["kind"] == "file"
    assert target.exists()


def test_mcp_transform_invalid_plan_keeps_structured_error_envelope() -> None:
    plan = {
        "version": "1",
        "sources": {"a": {"inline": [{"x": 1}]}},
        "steps": [{"id": "s", "op": "select", "fields": ["x"], "bogus": True}],
    }
    result = asyncio.run(data_transform(plan=plan))

    assert result.isError is True
    structured = result.structuredContent
    assert structured is not None
    assert structured["status"] == "error"
    assert structured["error"]["code"] == "E_PLAN_INVALID"
    assert "path" in structured["error"]["details"]


def test_type_assertion_accepts_parameterized_decimal_column(tmp_path) -> None:
    csv_path = tmp_path / "amounts.csv"
    csv_path.write_text("amount\n1.5\n2.5\n", encoding="utf-8")
    transformer = DataTransformer()

    result = transformer.validate(
        {"path": str(csv_path)},
        assertions=[{"type": "type", "field": "amount", "is": "decimal"}],
    )
    assert result["valid"] is True

    rejected = transformer.validate(
        {"path": str(csv_path)},
        assertions=[{"type": "type", "field": "amount", "is": "integer"}],
    )
    assert rejected["valid"] is False


def test_dry_run_rejects_tree_output_to_tabular_format(tmp_path) -> None:
    transformer = DataTransformer()
    dry = transformer.transform(_plan(tmp_path, dry_run=True, tree=True, output_format="csv"))
    real = transformer.transform(_plan(tmp_path, dry_run=False, tree=True, output_format="csv"))

    assert dry["error"]["code"] == "E_TYPE_MISMATCH"
    assert dry["error"] == real["error"]
    assert not list(tmp_path.glob("out.file*"))


def test_dry_run_rejects_schema_bearing_empty_table_to_json(tmp_path) -> None:
    transformer = DataTransformer()
    result = transformer.transform(_plan(tmp_path, dry_run=True, tree=False, output_format="json"))

    assert result["error"]["code"] == "E_OUTPUT_SCHEMA_LOSS"
    assert not list(tmp_path.glob("out.file*"))


def test_dry_run_rejects_schema_less_empty_table_to_parquet(tmp_path) -> None:
    transformer = DataTransformer()
    plan = {
        "version": "1",
        "sources": {"a": {"inline": []}},
        "steps": [{"id": "s", "op": "limit", "count": 0}],
        "output": {"path": str(tmp_path / "out.parquet"), "format": "parquet"},
        "dry_run": True,
    }
    result = transformer.transform(plan)
    assert result["error"]["code"] == "E_OUTPUT_EMPTY_SCHEMA"


def test_tree_diff_reports_json_type_vocabulary() -> None:
    transformer = DataTransformer()
    result = transformer.diff({"inline": {"a": 1, "b": "x"}}, {"inline": {"a": 1.0, "b": 2}})

    changes = {change["path"]: change for change in result["changes"]}
    assert changes["$.a"]["change"] == "type"
    assert changes["$.a"]["from"] == "integer"
    assert changes["$.a"]["to"] == "number"
    assert changes["$.b"]["change"] == "type"
    assert changes["$.b"]["from"] == "string"
    assert changes["$.b"]["to"] == "integer"
    assert not json.dumps(result["changes"]).startswith("Decimal")
