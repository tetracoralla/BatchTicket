from __future__ import annotations

import decimal
import re
import unicodedata
from typing import Any

from .errors import DataTransformerError
from .validation import prepare_schema
from .workspace import INTERNAL_ORDER

ADAPTER_PROFILE = "record-schema-v1"
ADAPTER_FIELD_CAP = 1_000
_CANDIDATE_MATCH_CAP = 50
_SUGGESTION_CAP = 20
_JSON_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}
_UNSUPPORTED_COMPOSITION = {"$ref", "allOf", "anyOf", "if", "not", "oneOf"}
_UNSUPPORTED_ARRAY = {
    "contains",
    "maxContains",
    "minContains",
    "prefixItems",
    "unevaluatedItems",
    "uniqueItems",
}
_SCHEMA_MAP_KEYWORDS = {"$defs", "dependentSchemas", "patternProperties", "properties"}
_SCHEMA_SINGLE_KEYWORDS = {
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
}
_SCHEMA_ARRAY_KEYWORDS = {"allOf", "anyOf", "oneOf", "prefixItems"}


def adapt_schema(
    source: dict[str, Any],
    source_shape: dict[str, Any],
    target_schema: dict[str, Any],
    mappings: dict[str, str] | None,
    *,
    sample_rows: int,
) -> dict[str, Any]:
    normalized = prepare_schema(target_schema)
    target = _target_profile(normalized)
    if target["status"] == "unsupported":
        return {
            "status": "unsupported",
            "profile": ADAPTER_PROFILE,
            "reason": target["reason"],
            "details": target.get("details", {}),
        }

    source_profile, source_candidates = _source_profile(
        source_shape, target, mappings or {}
    )
    if source_profile is None:
        unresolved_result = {
            "status": "needs_source_selection",
            "profile": ADAPTER_PROFILE,
            "target": _public_target(target),
            "source_candidates": source_candidates,
            "unresolved": [
                {
                    "kind": "source_record_set",
                    "reason": "no unique structural record set",
                }
            ],
        }
        if source_shape.get("record_sets_truncated") is True:
            unresolved_result["source_candidates_truncated"] = True
        return unresolved_result
    if source_profile.get("select") is None and isinstance(source.get("select"), str):
        source_profile["select"] = source["select"]

    source_fields = source_profile["fields"]
    mapping_input = mappings or {}
    unknown_targets = sorted(set(mapping_input) - set(target["fields"]))
    if unknown_targets:
        raise DataTransformerError(
            "E_ADAPTER_MAPPING_INVALID",
            "mappings contain fields that are not declared by the target schema",
            {"fields": unknown_targets[:ADAPTER_FIELD_CAP]},
        )

    suggestions, suggestions_truncated = _suggestions(source_fields, target["fields"])
    confirmed: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for target_field in sorted(mapping_input):
        source_field = mapping_input[target_field]
        if source_field not in source_fields:
            reason = (
                "mapped source field is outside the bounded profile or does not exist"
                if source_profile.get("fields_truncated") is True
                else "mapped source field does not exist"
            )
            unresolved.append(
                {
                    "kind": "mapping",
                    "target": target_field,
                    "source": source_field,
                    "reason": reason,
                }
            )
            continue
        compatibility = _compatibility(
            source_fields[source_field], target["fields"][target_field]
        )
        mapping = {
            "target": target_field,
            "source": source_field,
            "method": "explicit",
            "compatibility": compatibility,
        }
        confirmed.append(mapping)
        if compatibility == "incompatible":
            unresolved.append(
                {
                    "kind": "type",
                    "target": target_field,
                    "source": source_field,
                    "reason": "source and target JSON types are incompatible",
                    "source_types": sorted(_source_types(source_fields[source_field])),
                    "target_types": sorted(_target_types(target["fields"][target_field])),
                }
            )

    for target_field in target["required"]:
        if target_field not in mapping_input:
            unresolved.append(
                {
                    "kind": "mapping",
                    "target": target_field,
                    "reason": "required target field needs an explicit mapping",
                    "candidates": suggestions.get(target_field, []),
                }
            )

    used_sources = {item["source"] for item in confirmed}
    source_targets: dict[str, list[str]] = {}
    for item in confirmed:
        source_targets.setdefault(item["source"], []).append(item["target"])
    duplicated_sources = [
        {"source": source_name, "targets": sorted(targets)}
        for source_name, targets in sorted(source_targets.items())
        if len(targets) > 1
    ]
    omitted_optional = sorted(
        set(target["fields"]) - set(target["required"]) - set(mapping_input)
    )
    all_dropped_sources = sorted(set(source_fields) - used_sources)
    dropped_sources = all_dropped_sources[:ADAPTER_FIELD_CAP]
    source_field_count = int(source_profile.get("columns", len(source_fields)))
    dropped_source_count = max(
        len(all_dropped_sources), source_field_count - len(used_sources)
    )
    result: dict[str, Any] = {
        "status": "needs_mapping" if unresolved or not confirmed else "ready",
        "profile": ADAPTER_PROFILE,
        "source": {
            "kind": "record_set",
            "rows": source_profile.get("rows"),
            "select": source_profile.get("select"),
            "field_count": source_field_count,
            "fields_profiled": len(source_fields),
            "fields_truncated": source_field_count > len(source_fields),
        },
        "target": _public_target(target),
        "suggested_mappings": suggestions,
        "confirmed_mappings": confirmed,
        "unresolved": unresolved,
        "omitted_optional_fields": omitted_optional,
        "dropped_source_fields": dropped_sources,
        "duplicated_source_fields": duplicated_sources,
        "source_candidates": source_candidates,
    }
    if dropped_source_count > len(dropped_sources):
        result["truncated"] = {
            "dropped_source_fields": {
                "total": dropped_source_count,
                "returned": len(dropped_sources),
            }
        }
    if suggestions_truncated:
        result.setdefault("truncated", {})["suggested_mappings"] = suggestions_truncated
    if result["status"] == "ready" and _inline_contains_exact_decimal(source):
        result["status"] = "unsupported"
        result["reason"] = (
            "an inline exact decimal cannot be embedded losslessly in a public JSON plan"
        )
        result["details"] = {"source": "inline", "value_type": "decimal"}
    elif result["status"] == "ready":
        result["draft_plan"] = _draft_plan(
            source,
            source_profile,
            target_schema,
            target,
            confirmed,
            sample_rows,
        )
    return result


def _inline_contains_exact_decimal(source: dict[str, Any]) -> bool:
    if "inline" not in source:
        return False
    pending = [source["inline"]]
    while pending:
        current = pending.pop()
        if isinstance(current, decimal.Decimal):
            return True
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return False


def _target_profile(schema: dict[str, Any]) -> dict[str, Any]:
    unsupported_composition = _unsupported_keywords(schema)
    if unsupported_composition:
        return {
            "status": "unsupported",
            "reason": "composed or referenced schemas require explicit resolution",
            "details": {"keywords": sorted(unsupported_composition)},
        }
    container = "records"
    record_schema = schema
    if schema.get("type") == "array":
        unsupported = _UNSUPPORTED_ARRAY & set(schema)
        if unsupported:
            return {
                "status": "unsupported",
                "reason": "array constraints exceed the deterministic record adapter profile",
                "details": {"keywords": sorted(unsupported)},
            }
        items = schema.get("items")
        if not isinstance(items, dict):
            return {
                "status": "unsupported",
                "reason": "array target requires one object schema in items",
            }
        container = "array"
        record_schema = items
    record_type = record_schema.get("type")
    # JSON Schema allows a list (or boolean) ``type``; comparing without
    # hashing keeps such schemas on the deterministic unsupported path
    # instead of raising ``TypeError: unhashable type``.
    if record_type is not None and record_type != "object":
        return {
            "status": "unsupported",
            "reason": "target must describe object records or an array of object records",
            "details": {"type": record_type},
        }
    properties = record_schema.get("properties", {})
    if not isinstance(properties, dict):
        return {"status": "unsupported", "reason": "target properties must be an object"}
    required = record_schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        return {"status": "unsupported", "reason": "target required must be an array of fields"}
    names = sorted(set(properties) | set(required))
    if not names:
        return {
            "status": "unsupported",
            "reason": "target schema has no explicit record fields to map",
        }
    if len(names) > ADAPTER_FIELD_CAP:
        return {
            "status": "unsupported",
            "reason": "target schema exceeds the record adapter field limit",
            "details": {"fields": len(names), "maximum": ADAPTER_FIELD_CAP},
        }
    unsupported_names = [
        name
        for name in names
        if not name or "\x00" in name or name == INTERNAL_ORDER
    ]
    if unsupported_names:
        return {
            "status": "unsupported",
            "reason": "target fields cannot be represented by Plan v1 output aliases",
            "details": {"fields": unsupported_names[:ADAPTER_FIELD_CAP]},
        }
    # Boolean property schemas ({"x": false}) are legal JSON Schema but carry a
    # constraint this profile cannot represent; treating them as unconstrained
    # drafts plans whose own schema validation always fails.
    non_object_properties = sorted(
        name
        for name in set(properties)
        if not isinstance(properties.get(name), dict)
    )
    if non_object_properties:
        return {
            "status": "unsupported",
            "reason": "target property schemas must be objects",
            "details": {"fields": non_object_properties[:ADAPTER_FIELD_CAP]},
        }
    fields = {name: properties.get(name, {}) for name in names}
    return {
        "status": "supported",
        "container": container,
        "record_schema": record_schema,
        "fields": fields,
        "required": sorted(set(required)),
        "min_items": schema.get("minItems") if container == "array" else None,
        "max_items": schema.get("maxItems") if container == "array" else None,
    }


def _source_profile(
    source_shape: dict[str, Any],
    target: dict[str, Any],
    mappings: dict[str, str],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if source_shape.get("kind") == "table":
        return {
            "rows": source_shape.get("rows"),
            "select": None,
            "fields": source_shape.get("fields", {}),
        }, []
    candidates = source_shape.get("record_sets")
    if not isinstance(candidates, list) or not candidates:
        return None, []
    scored: list[tuple[int, int, dict[str, Any]]] = []
    summaries: list[dict[str, Any]] = []
    target_names = set(target["fields"])
    required = set(target["required"])
    mapped_sources = set(mappings.values())
    for candidate in candidates:
        fields = candidate.get("fields", {})
        exact = sorted(target_names & set(fields))
        normalized = _normalized_matches(fields, target_names)
        required_exact = len(required & set(exact))
        required_normalized = len(required & set(normalized))
        mapped_matches = sorted(mapped_sources & set(fields))
        structural_score = (
            required_exact * 1_000
            + required_normalized * 100
            + len(exact) * 10
            + len(normalized)
        )
        score = len(mapped_matches) * 10_000 + structural_score
        scored.append((len(mapped_matches), score, candidate))
        summaries.append(
            {
                "select": candidate.get("select"),
                "path": candidate.get("path"),
                "selectable": candidate.get("selectable") is True,
                "rows": candidate.get("rows"),
                "field_count": candidate.get("columns"),
                "fields_truncated": candidate.get("fields_truncated") is True,
                "exact_matches": exact[:_CANDIDATE_MATCH_CAP],
                "exact_match_count": len(exact),
                "normalized_matches": sorted(normalized)[:_CANDIDATE_MATCH_CAP],
                "normalized_match_count": len(normalized),
                "mapped_matches": mapped_matches[:_CANDIDATE_MATCH_CAP],
                "mapped_match_count": len(mapped_matches),
                "score": score,
            }
        )
        if candidate.get("selectable") is not True:
            scored.pop()
    summaries.sort(key=lambda item: (str(item.get("select")), str(item.get("path"))))
    if not scored:
        return None, summaries
    if source_shape.get("record_sets_truncated") is True:
        return None, summaries
    if len(scored) > 1 and any(item[2].get("fields_truncated") is True for item in scored):
        # A capped profile can hide the exact or mapped field that would change
        # the ranking. Do not turn incomplete evidence into a source choice.
        return None, summaries
    if mapped_sources:
        complete = [item for item in scored if item[0] == len(mapped_sources)]
        if complete:
            scored = complete
    if len(scored) == 1:
        return scored[0][2], summaries
    scored.sort(key=lambda item: (-item[1], str(item[2].get("select"))))
    if scored[0][1] > 0 and scored[0][1] > scored[1][1]:
        return scored[0][2], summaries
    return None, summaries


def _unsupported_keywords(value: Any) -> set[str]:
    found: set[str] = set()
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            found.update(_UNSUPPORTED_COMPOSITION & set(current))
            for keyword, child in current.items():
                if keyword in _SCHEMA_MAP_KEYWORDS and isinstance(child, dict):
                    pending.extend(
                        item for item in child.values() if isinstance(item, dict)
                    )
                elif keyword in _SCHEMA_SINGLE_KEYWORDS and isinstance(child, dict):
                    pending.append(child)
                elif keyword in _SCHEMA_ARRAY_KEYWORDS and isinstance(child, list):
                    pending.extend(item for item in child if isinstance(item, dict))
    return found


def _suggestions(
    source_fields: dict[str, Any], target_fields: dict[str, Any]
) -> tuple[dict[str, list[dict[str, str]]], dict[str, dict[str, int]]]:
    normalized_sources: dict[str, list[str]] = {}
    for source in source_fields:
        normalized_sources.setdefault(_normalize_name(source), []).append(source)
    suggestions: dict[str, list[dict[str, str]]] = {}
    truncated: dict[str, dict[str, int]] = {}
    for target in sorted(target_fields):
        if target in source_fields:
            suggestions[target] = [{"source": target, "method": "exact"}]
            continue
        candidates = sorted(normalized_sources.get(_normalize_name(target), []))
        if candidates:
            suggestions[target] = [
                {"source": source, "method": "normalized"}
                for source in candidates[:_SUGGESTION_CAP]
            ]
            if len(candidates) > _SUGGESTION_CAP:
                truncated[target] = {
                    "total": len(candidates),
                    "returned": _SUGGESTION_CAP,
                }
    return suggestions, truncated


def _normalized_matches(source_fields: dict[str, Any], target_names: set[str]) -> set[str]:
    source_names = {_normalize_name(name) for name in source_fields}
    return {
        target
        for target in target_names
        if target not in source_fields and _normalize_name(target) in source_names
    }


def _normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _compatibility(source_field: dict[str, Any], target_schema: dict[str, Any]) -> str:
    source = _source_types(source_field)
    target = _target_types(target_schema)
    if not target:
        return "schema_validation"
    non_null_source = source - {"null"}
    non_null_target = target - {"null"}
    if "null" in source and "null" not in target:
        return "incompatible"
    for source_type in non_null_source:
        if source_type in non_null_target:
            continue
        if source_type == "integer" and "number" in non_null_target:
            continue
        return "incompatible"
    return "compatible"


def _source_types(field: dict[str, Any]) -> set[str]:
    raw_types = field.get("types")
    if isinstance(raw_types, list):
        result = {_source_type_family(str(item)) for item in raw_types}
    else:
        raw = str(field.get("type", "unknown"))
        result = {_source_type_family(raw)}
    if field.get("nullable") is True:
        result.add("null")
    return result


def _source_type_family(raw: str) -> str:
    normalized = re.sub(r"\s+", " ", raw.strip().lower())
    if normalized in _JSON_TYPES:
        return normalized
    if normalized.endswith("[]") or normalized.startswith("list("):
        return "array"
    base = normalized.split("(", 1)[0].strip()
    if base in {
        "bigint",
        "hugeint",
        "integer",
        "smallint",
        "tinyint",
        "ubigint",
        "uhugeint",
        "uinteger",
        "usmallint",
        "utinyint",
    }:
        return "integer"
    if base in {"decimal", "double", "float", "numeric", "real"}:
        return "number"
    if base in {"bpchar", "char", "date", "text", "time", "timestamp", "varchar"}:
        return "string"
    if base in {"bool", "boolean"}:
        return "boolean"
    if base in {"json", "map", "struct", "union"}:
        return "object"
    return "unknown"


def _target_types(schema: dict[str, Any]) -> set[str]:
    raw = schema.get("type")
    if isinstance(raw, str):
        return {raw} if raw in _JSON_TYPES else set()
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return {item for item in raw if item in _JSON_TYPES}
    return set()


def _public_target(target: dict[str, Any]) -> dict[str, Any]:
    fields = {
        name: {
            "types": sorted(_target_types(schema)) or ["schema-defined"],
            "required": name in target["required"],
        }
        for name, schema in list(target["fields"].items())[:ADAPTER_FIELD_CAP]
    }
    result: dict[str, Any] = {
        "container": target["container"],
        "field_count": len(target["fields"]),
        "required": target["required"][:ADAPTER_FIELD_CAP],
        "fields": fields,
    }
    if len(target["fields"]) > ADAPTER_FIELD_CAP:
        result["fields_truncated"] = True
    return result


def _draft_plan(
    source: dict[str, Any],
    source_profile: dict[str, Any],
    target_schema: dict[str, Any],
    target: dict[str, Any],
    confirmed: list[dict[str, Any]],
    sample_rows: int,
) -> dict[str, Any]:
    plan_source = {**source}
    # Every adapter draft feeds the tabular select operator. An inspection may
    # deliberately keep an envelope as a tree while discovering record sets,
    # but the drafted source must materialize the selected records as a table.
    plan_source["kind"] = "table"
    selected = source_profile.get("select")
    if selected is not None:
        # The discovered record-set select is relative to the user's selection,
        # but a plan selects from the source document, so compose both paths
        # instead of dropping the user-provided select.
        user_select = source.get("select")
        if isinstance(user_select, str) and user_select:
            plan_source["select"] = f"{user_select}.{selected}"
        else:
            plan_source["select"] = selected
    fields = [
        {"field": mapping["source"], "as": mapping["target"]}
        for mapping in sorted(confirmed, key=lambda item: item["target"])
    ]
    assertions: list[dict[str, Any]] = []
    if target["container"] == "array":
        row_count: dict[str, Any] = {"id": "target_row_count", "type": "row_count"}
        if target["min_items"] is not None:
            row_count["min"] = target["min_items"]
        if target["max_items"] is not None:
            row_count["max"] = target["max_items"]
        if len(row_count) > 2:
            assertions.append(row_count)
    plan: dict[str, Any] = {
        "version": "1",
        "sources": {"input": plan_source},
        "steps": [
            {
                "id": "adapted",
                "op": "select",
                "source": "input",
                "fields": fields,
            }
        ],
        "schema": (
            target_schema["items"]
            if target["container"] == "array"
            else target_schema
        ),
        "return": {"mode": "auto", "sample_rows": sample_rows},
    }
    if assertions:
        plan["assert"] = assertions
    return plan
