from __future__ import annotations

from data_transformer import DataTransformer


def test_inspect_table_returns_shape_and_bounded_sample() -> None:
    result = DataTransformer().inspect(
        {"inline": [{"id": 1, "name": "A"}, {"id": 2, "name": None}]},
        sample_rows=1,
    )

    assert result["status"] == "ok"
    assert result["shape"]["rows"] == 2
    assert result["shape"]["fields"]["name"]["null_count"] == 1
    assert result["sample"] == [{"id": 1, "name": "A"}]


def test_inspect_safe_selector_extracts_records() -> None:
    result = DataTransformer().inspect(
        {
            "inline": {"data": {"users": [{"id": 1}, {"id": 2}]}},
            "select": "data.users[*]",
        }
    )

    assert result["shape"]["kind"] == "table"
    assert result["shape"]["rows"] == 2


def test_selector_rejects_expression_syntax() -> None:
    result = DataTransformer().inspect({"inline": {"rows": []}, "select": "rows[__import__('os')]"})

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_SELECTOR_SYNTAX"


def test_duplicate_json_keys_are_rejected(tmp_path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"a": 1, "a": 2}', encoding="utf-8")

    result = DataTransformer(tmp_path).inspect({"path": str(path)})

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_DUPLICATE_KEY"


def test_row_limit_is_enforced() -> None:
    result = DataTransformer().inspect({"inline": [{"id": 1}, {"id": 2}]}, limits={"max_rows": 1})

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_ROW_LIMIT"


def test_url_sources_are_rejected() -> None:
    result = DataTransformer().inspect({"path": "https://example.com/data.json"})

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_NETWORK_FORBIDDEN"
