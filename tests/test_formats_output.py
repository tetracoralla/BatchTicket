from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import yaml

from data_transformer import DataTransformer


def _plan(source, steps, **extra):
    return {"version": "1", "sources": {"rows": source}, "steps": steps, **extra}


def test_csv_jsonl_and_yaml_inputs(tmp_path) -> None:
    csv_path = tmp_path / "rows.csv"
    csv_path.write_text("id,name\n1,A\n2,B\n", encoding="utf-8")
    jsonl_path = tmp_path / "rows.jsonl"
    jsonl_path.write_text('{"id":1}\n{"id":2}\n', encoding="utf-8")
    yaml_path = tmp_path / "rows.yaml"
    yaml_path.write_text("- id: 1\n- id: 2\n", encoding="utf-8")

    transformer = DataTransformer(tmp_path)
    csv_result = transformer.inspect({"path": str(csv_path)})
    jsonl_result = transformer.inspect({"path": str(jsonl_path)})
    yaml_result = transformer.inspect({"path": str(yaml_path)})

    assert csv_result["shape"]["rows"] == 2
    assert csv_result["sample"][0] == {"id": 1, "name": "A"}
    assert jsonl_result["shape"]["rows"] == 2
    assert yaml_result["shape"]["rows"] == 2


def test_empty_json_array_has_no_invented_field(tmp_path) -> None:
    path = tmp_path / "empty.json"
    path.write_text("[]\n", encoding="utf-8")

    result = DataTransformer(tmp_path).inspect({"path": str(path)})

    assert result["status"] == "ok"
    assert result["shape"] == {
        "kind": "table",
        "rows": 0,
        "columns": 0,
        "fields": {},
    }


def test_yaml_duplicate_key_is_rejected(tmp_path) -> None:
    path = tmp_path / "duplicate.yaml"
    path.write_text("a: 1\na: 2\n", encoding="utf-8")

    result = DataTransformer(tmp_path).inspect({"path": str(path)})

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_DUPLICATE_KEY"


def test_table_outputs_jsonl_csv_tsv_yaml_and_parquet(tmp_path) -> None:
    transformer = DataTransformer(tmp_path)
    rows = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    outputs = {
        "jsonl": tmp_path / "rows.jsonl",
        "csv": tmp_path / "rows.csv",
        "tsv": tmp_path / "rows.tsv",
        "yaml": tmp_path / "rows.yaml",
        "parquet": tmp_path / "rows.parquet",
    }
    for data_format, path in outputs.items():
        result = transformer.transform(
            _plan(
                {"inline": rows},
                [{"op": "select", "fields": ["id", "name"]}],
                output={"path": str(path), "format": data_format},
            )
        )
        assert result["status"] == "ok"
        assert result["result"]["kind"] == "file"
        assert path.exists()

    assert [json.loads(line) for line in outputs["jsonl"].read_text().splitlines()] == rows
    assert outputs["csv"].read_text(encoding="utf-8").splitlines()[0] == "id,name"
    assert outputs["tsv"].read_text(encoding="utf-8").splitlines()[0] == "id\tname"
    assert yaml.safe_load(outputs["yaml"].read_text(encoding="utf-8")) == rows
    parquet = transformer.inspect({"path": str(outputs["parquet"])})
    assert parquet["shape"]["rows"] == 2


def test_explicit_overwrite_replaces_exact_output(tmp_path) -> None:
    target = tmp_path / "result.json"
    target.write_text('[{"old":true}]\n', encoding="utf-8")

    result = DataTransformer(tmp_path).transform(
        _plan(
            {"inline": [{"id": 1}]},
            [{"op": "select", "fields": ["id"]}],
            output={"path": str(target), "overwrite": True},
        )
    )

    assert result["status"] == "ok"
    assert json.loads(target.read_text(encoding="utf-8")) == [{"id": 1}]


def test_concurrent_no_overwrite_has_single_winner(tmp_path, monkeypatch) -> None:
    target = tmp_path / "race.json"
    barrier = Barrier(2)
    real_exists = Path.exists

    def hide_target_until_publish(path: Path) -> bool:
        if path == target:
            return False
        return real_exists(path)

    monkeypatch.setattr(Path, "exists", hide_target_until_publish)

    def write(value: int):
        barrier.wait()
        return DataTransformer(tmp_path).transform(
            _plan(
                {"inline": [{"id": value}]},
                [{"op": "select", "fields": ["id"]}],
                output={"path": str(target)},
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, [1, 2]))

    assert sorted(result["status"] for result in results) == ["error", "ok"]
    error = next(result for result in results if result["status"] == "error")
    assert error["error"]["code"] == "E_OUTPUT_EXISTS"
    assert real_exists(target)


def test_tree_yaml_output(tmp_path) -> None:
    target = tmp_path / "config.yaml"
    result = DataTransformer(tmp_path).transform(
        _plan(
            {"inline": {"a": 1}},
            [{"op": "set", "path": "b", "value": 2, "create": True}],
            output={"path": str(target)},
        )
    )

    assert result["status"] == "ok"
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == {"a": 1, "b": 2}


def test_summary_mode_omits_transformed_payload() -> None:
    result = DataTransformer().transform(
        _plan(
            {"inline": [{"id": 1}]},
            [{"op": "select", "fields": ["id"]}],
            **{"return": {"mode": "summary"}},
        )
    )

    assert result["status"] == "ok"
    assert result["result"] == {"kind": "summary"}
    assert result["sample"] == [{"id": 1}]
