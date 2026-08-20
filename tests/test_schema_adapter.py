from __future__ import annotations

from decimal import Decimal

from data_transformer import DataTransformer


def _target_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "user_id": {"type": "integer"},
        },
        "required": ["name", "user_id"],
        "additionalProperties": False,
    }


def test_schema_adapter_reports_structural_candidates_without_inventing_plan() -> None:
    result = DataTransformer().inspect(
        {"inline": [{"userId": 1, "full_name": "A", "ignored": True}]},
        target_schema=_target_schema(),
    )

    adaptation = result["adaptation"]
    assert adaptation["status"] == "needs_mapping"
    assert adaptation["suggested_mappings"] == {
        "user_id": [{"source": "userId", "method": "normalized"}]
    }
    assert {item["target"] for item in adaptation["unresolved"]} == {
        "name",
        "user_id",
    }
    assert "draft_plan" not in adaptation


def test_schema_adapter_builds_executable_plan_only_from_explicit_mappings() -> None:
    transformer = DataTransformer()
    source = {"inline": [{"userId": 1, "full_name": "A", "ignored": True}]}
    result = transformer.inspect(
        source,
        target_schema=_target_schema(),
        mappings={"user_id": "userId", "name": "full_name"},
    )

    adaptation = result["adaptation"]
    assert adaptation["status"] == "ready"
    assert adaptation["dropped_source_fields"] == ["ignored"]
    plan = adaptation["draft_plan"]
    executed = transformer.transform(plan)
    assert executed["status"] == "ok"
    assert executed["result"]["data"] == [{"name": "A", "user_id": 1}]
    assert executed["receipt"]["schema_validation"]["status"] == "passed"


def test_schema_adapter_selects_one_structurally_unique_nested_record_set() -> None:
    source = {
        "inline": {
            "data": {"users": [{"userId": 1, "age": 34}, {"userId": 2, "age": 16}]},
            "metadata": {"events": [{"code": "created"}]},
        }
    }
    target = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "age": {"type": "integer"}},
        "required": ["id", "age"],
    }

    result = DataTransformer().inspect(
        source,
        target_schema=target,
        mappings={"id": "userId", "age": "age"},
    )

    adaptation = result["adaptation"]
    assert adaptation["status"] == "ready"
    assert adaptation["source"]["select"] == "data.users[*]"
    assert adaptation["draft_plan"]["sources"]["input"]["select"] == "data.users[*]"
    executed = DataTransformer().transform(adaptation["draft_plan"])
    assert executed["result"]["data"] == [{"age": 34, "id": 1}, {"age": 16, "id": 2}]


def test_explicit_mapping_selects_the_record_set_that_contains_its_source() -> None:
    result = DataTransformer().inspect(
        {
            "inline": {
                "left": [{"id": 1}],
                "right": [{"source_id": 2}],
            }
        },
        target_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        mappings={"id": "source_id"},
    )

    adaptation = result["adaptation"]
    assert adaptation["status"] == "ready"
    assert adaptation["source"]["select"] == "right[*]"
    executed = DataTransformer().transform(adaptation["draft_plan"])
    assert executed["result"]["data"] == [{"id": 2}]


def test_truncated_record_profile_cannot_be_ranked_as_if_fields_were_absent() -> None:
    wide = {f"a{index:04d}": index for index in range(1_000)}
    wide["source_id"] = 2
    result = DataTransformer().inspect(
        {"inline": {"small": [{"id": 1}], "wide": [wide]}},
        target_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        mappings={"id": "source_id"},
    )

    adaptation = result["adaptation"]
    assert adaptation["status"] == "needs_source_selection"
    wide_candidate = next(
        item for item in adaptation["source_candidates"] if item["select"] == "wide[*]"
    )
    assert wide_candidate["fields_truncated"] is True
    assert "draft_plan" not in adaptation


def test_truncated_record_set_discovery_cannot_claim_a_unique_source() -> None:
    envelope = {
        f"set_{index:02d}": [{"id": index}]
        for index in range(33)
    }
    result = DataTransformer().inspect(
        {"inline": envelope},
        target_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        mappings={"id": "id"},
    )

    adaptation = result["adaptation"]
    assert adaptation["status"] == "needs_source_selection"
    assert adaptation["source_candidates_truncated"] is True
    assert len(adaptation["source_candidates"]) == 32
    assert "draft_plan" not in adaptation


def test_schema_adapter_does_not_choose_between_tied_record_sets() -> None:
    result = DataTransformer().inspect(
        {
            "inline": {
                "left": [{"id": 1}],
                "right": [{"id": 2}],
            }
        },
        target_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    )

    adaptation = result["adaptation"]
    assert adaptation["status"] == "needs_source_selection"
    assert [item["select"] for item in adaptation["source_candidates"]] == [
        "left[*]",
        "right[*]",
    ]
    assert "draft_plan" not in adaptation


def test_schema_adapter_blocks_incompatible_explicit_mapping() -> None:
    result = DataTransformer().inspect(
        {"inline": [{"id_text": "1"}]},
        target_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        mappings={"id": "id_text"},
    )

    adaptation = result["adaptation"]
    assert adaptation["status"] == "needs_mapping"
    assert adaptation["confirmed_mappings"][0]["compatibility"] == "incompatible"
    assert adaptation["unresolved"][0]["kind"] == "type"
    assert "draft_plan" not in adaptation


def test_schema_adapter_blocks_nullable_source_for_non_nullable_target() -> None:
    result = DataTransformer().inspect(
        {"inline": [{"id": 1}, {"id": None}]},
        target_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        mappings={"id": "id"},
    )

    assert result["adaptation"]["status"] == "needs_mapping"
    assert result["adaptation"]["unresolved"][0]["source_types"] == ["integer", "null"]


def test_schema_adapter_reports_explicit_source_duplication() -> None:
    result = DataTransformer().inspect(
        {"inline": [{"id": 1}]},
        target_schema={
            "type": "object",
            "properties": {
                "primary_id": {"type": "integer"},
                "secondary_id": {"type": "integer"},
            },
            "required": ["primary_id", "secondary_id"],
        },
        mappings={"primary_id": "id", "secondary_id": "id"},
    )

    assert result["adaptation"]["status"] == "ready"
    assert result["adaptation"]["duplicated_source_fields"] == [
        {"source": "id", "targets": ["primary_id", "secondary_id"]}
    ]


def test_schema_adapter_does_not_generate_plan_for_unselectable_record_path() -> None:
    result = DataTransformer().inspect(
        {"inline": {"bad key": [{"id": 1}]}},
        target_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        mappings={"id": "id"},
    )

    assert result["adaptation"]["status"] == "needs_source_selection"
    assert result["adaptation"]["source_candidates"][0]["select"] is None
    assert result["adaptation"]["source_candidates"][0]["selectable"] is False
    assert "draft_plan" not in result["adaptation"]


def test_schema_adapter_translates_supported_array_bounds_to_assertions() -> None:
    target = {
        "type": "array",
        "minItems": 1,
        "maxItems": 3,
        "items": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    }
    result = DataTransformer().inspect(
        {"inline": [{"source_id": 1}, {"source_id": 2}]},
        target_schema=target,
        mappings={"id": "source_id"},
    )

    plan = result["adaptation"]["draft_plan"]
    assert plan["schema"] == target["items"]
    assert plan["assert"] == [
        {"id": "target_row_count", "type": "row_count", "min": 1, "max": 3}
    ]
    assert DataTransformer().transform(plan)["status"] == "ok"


def test_schema_adapter_reports_unsupported_composition_without_guessing() -> None:
    result = DataTransformer().inspect(
        {"inline": [{"id": 1}]},
        target_schema={
            "oneOf": [
                {"type": "object", "properties": {"id": {"type": "integer"}}},
                {"type": "object", "properties": {"name": {"type": "string"}}},
            ]
        },
    )

    assert result["status"] == "ok"
    assert result["adaptation"]["status"] == "unsupported"
    assert result["adaptation"]["details"] == {"keywords": ["oneOf"]}


def test_schema_adapter_reports_type_lists_as_unsupported_without_internal_error() -> None:
    for target in (
        {"type": ["object", "null"], "properties": {"id": {"type": "integer"}}},
        {
            "type": ["array", "null"],
            "items": {"type": "object", "properties": {"id": {"type": "integer"}}},
        },
    ):
        result = DataTransformer().inspect({"inline": [{"id": 1}]}, target_schema=target)

        assert result["status"] == "ok"
        adaptation = result["adaptation"]
        assert adaptation["status"] == "unsupported"
        assert adaptation["reason"] == (
            "target must describe object records or an array of object records"
        )
        assert "draft_plan" not in adaptation


def test_schema_adapter_rejects_nested_composition_and_references() -> None:
    for property_schema, keyword in (
        ({"oneOf": [{"type": "string"}, {"type": "integer"}]}, "oneOf"),
        ({"$ref": "#/$defs/value"}, "$ref"),
    ):
        target = {
            "$defs": {"value": {"type": "string"}},
            "type": "object",
            "properties": {"value": property_schema},
            "required": ["value"],
        }
        result = DataTransformer().inspect(
            {"inline": [{"value": "x"}]},
            target_schema=target,
            mappings={"value": "value"},
        )

        adaptation = result["adaptation"]
        assert adaptation["status"] == "unsupported"
        assert keyword in adaptation["details"]["keywords"]
        assert "draft_plan" not in adaptation


def test_schema_adapter_does_not_treat_const_data_as_schema_composition() -> None:
    target = {
        "type": "object",
        "properties": {
            "value": {
                "type": "object",
                "const": {"oneOf": "ordinary instance data"},
            }
        },
        "required": ["value"],
    }
    result = DataTransformer().inspect(
        {"inline": [{"value": {"oneOf": "ordinary instance data"}}]},
        target_schema=target,
        mappings={"value": "value"},
    )

    adaptation = result["adaptation"]
    assert adaptation["status"] == "ready"
    assert DataTransformer().transform(adaptation["draft_plan"])["status"] == "ok"


def test_schema_adapter_rejects_unknown_target_mapping() -> None:
    result = DataTransformer().inspect(
        {"inline": [{"id": 1}]},
        target_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
        },
        mappings={"missing": "id"},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_ADAPTER_MAPPING_INVALID"


def test_schema_adapter_mappings_require_target_schema() -> None:
    result = DataTransformer().inspect(
        {"inline": [{"id": 1}]},
        mappings={"id": "id"},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "E_ADAPTER_MAPPING_INVALID"


def test_draft_plan_composes_user_select_with_discovered_record_set() -> None:
    result = DataTransformer().inspect(
        {"inline": {"data": {"users": [{"id": 1}]}}, "select": "data"},
        target_schema={"type": "object", "properties": {"id": {"type": "integer"}}},
        mappings={"id": "id"},
    )

    draft = result["adaptation"]["draft_plan"]
    assert draft["sources"]["input"]["select"] == "data.users[*]"

    # A "ready" plan must execute: the composed select resolves in the source
    # document instead of the record-set path relative to the user selection.
    executed = DataTransformer().transform(draft)
    assert executed["status"] == "ok"
    assert executed["result"]["data"] == [{"id": 1}]


def test_boolean_property_schemas_report_unsupported_not_ready() -> None:
    result = DataTransformer().inspect(
        {"inline": [{"id": 1}]},
        target_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}, "x": False},
        },
        mappings={"id": "id"},
    )

    # {"x": false} is legal JSON Schema but cannot be profiled; treating it as
    # unconstrained drafted plans whose own schema validation always fails.
    adaptation = result["adaptation"]
    assert adaptation["status"] == "unsupported"
    assert adaptation["reason"] == "target property schemas must be objects"
    assert adaptation["details"]["fields"] == ["x"]
    assert "draft_plan" not in adaptation


def test_ready_draft_overrides_tree_envelope_kind_for_selected_records() -> None:
    result = DataTransformer().inspect(
        {"inline": {"rows": [{"id": 1}]}, "kind": "tree"},
        target_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        mappings={"id": "id"},
    )

    draft = result["adaptation"]["draft_plan"]
    assert draft["sources"]["input"]["kind"] == "table"
    assert DataTransformer().transform(draft)["result"]["data"] == [{"id": 1}]


def test_schema_adapter_rejects_target_fields_that_plan_cannot_emit() -> None:
    result = DataTransformer().inspect(
        {"inline": [{"id": 1}]},
        target_schema={
            "type": "object",
            "properties": {"__adt_internal_order__": {"type": "integer"}},
            "required": ["__adt_internal_order__"],
        },
        mappings={"__adt_internal_order__": "id"},
    )

    adaptation = result["adaptation"]
    assert adaptation["status"] == "unsupported"
    assert adaptation["details"]["fields"] == ["__adt_internal_order__"]
    assert "draft_plan" not in adaptation


def test_schema_adapter_does_not_publish_carrier_lossy_decimal_draft() -> None:
    result = DataTransformer().inspect(
        {"inline": [{"value": Decimal("0.123456789012345678")}]},
        target_schema={
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
        },
        mappings={"value": "value"},
    )

    adaptation = result["adaptation"]
    assert adaptation["status"] == "unsupported"
    assert adaptation["details"] == {"source": "inline", "value_type": "decimal"}
    assert "draft_plan" not in adaptation
