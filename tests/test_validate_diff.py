from __future__ import annotations

from data_transformer import DataTransformer


def test_validate_reports_invalid_as_data_not_transport_error() -> None:
    result = DataTransformer().validate(
        {"inline": [{"id": 1}, {"id": None}]},
        schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        assertions=[{"type": "not_null", "field": "id"}],
    )

    assert result["status"] == "ok"
    assert result["valid"] is False
    assert result["schema_validation"]["status"] == "failed"
    assert result["assertions"][0]["status"] == "failed"


def test_validate_requires_a_contract() -> None:
    result = DataTransformer().validate({"inline": [{"id": 1}]})

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_VALIDATION_REQUIRED"


def test_validate_field_row_count_and_type_assertions() -> None:
    result = DataTransformer().validate(
        {"inline": [{"id": 1}, {"id": 2}]},
        assertions=[
            {"type": "field_exists", "field": "id"},
            {"type": "row_count", "min": 2, "max": 2},
            {"type": "type", "field": "id", "is": "integer"},
        ],
    )

    assert result["valid"] is True
    assert all(item["status"] == "passed" for item in result["assertions"])


def test_keyed_diff_counts_added_removed_and_changed() -> None:
    result = DataTransformer().diff(
        {"inline": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]},
        {"inline": [{"id": 1, "v": "changed"}, {"id": 3, "v": "c"}]},
        key_fields=["id"],
    )

    assert result["status"] == "ok"
    assert result["identical"] is False
    assert result["added_rows"] == 1
    assert result["removed_rows"] == 1
    assert result["changed_rows"] == 1


def test_unkeyed_diff_respects_duplicate_rows() -> None:
    result = DataTransformer().diff(
        {"inline": [{"id": 1}, {"id": 1}]},
        {"inline": [{"id": 1}]},
    )

    assert result["removed_rows"] == 1
    assert result["added_rows"] == 0


def test_diff_rejects_non_unique_keys() -> None:
    result = DataTransformer().diff(
        {"inline": [{"id": 1}, {"id": 1}]},
        {"inline": [{"id": 1}]},
        key_fields=["id"],
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_DIFF_KEY_NOT_UNIQUE"


def test_tree_diff_reports_paths() -> None:
    result = DataTransformer().diff(
        {"inline": {"a": 1, "nested": {"x": True}}},
        {"inline": {"a": 2, "nested": {"x": True}, "b": 3}},
    )

    assert result["change_count"] == 2
    assert {change["path"] for change in result["changes"]} == {"$.a", "$.b"}
