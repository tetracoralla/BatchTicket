from __future__ import annotations

import json
from pathlib import Path

from data_transformer.capability_adapter import _handle_request


def _call(operation: str, input_value: dict[str, object]) -> dict[str, object]:
    return _handle_request(
        json.dumps({"id": "test", "operationId": operation, "input": input_value})
    )


def test_capability_adapter_inspects_real_fixture(monkeypatch) -> None:
    monkeypatch.setenv("OPENADAM_PROVIDER_ROOT", str(Path.cwd()))
    response = _call(
        "inspect",
        {"source": {"path": "capabilities/fixtures/users.json"}, "sample_rows": 1},
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["source"]["format"] == "json"
    assert result["shape"]["record_sets"][0]["select"] == "data.users[*]"


def test_capability_adapter_preserves_failed_validation_as_result(monkeypatch) -> None:
    monkeypatch.setenv("OPENADAM_PROVIDER_ROOT", str(Path.cwd()))
    response = _call(
        "validate",
        {
            "source": {
                "path": "capabilities/fixtures/users.json",
                "select": "data.users[*]",
            },
            "assertions": [
                {"id": "age-required", "type": "not_null", "field": "age"}
            ],
            "sample_rows": 0,
        },
    )

    assert response["ok"] is True
    assert response["result"]["valid"] is False
    assert response["result"]["assertions"] == [
        {
            "id": "age-required",
            "type": "not_null",
            "status": "failed",
            "details": {"field": "age", "null_rows": 1},
        }
    ]


def test_capability_adapter_maps_missing_source(monkeypatch) -> None:
    monkeypatch.setenv("OPENADAM_PROVIDER_ROOT", str(Path.cwd()))
    response = _call("inspect", {"source": {"path": "capabilities/fixtures/missing.json"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "SOURCE_NOT_FOUND"


def test_capability_adapter_normalizes_omitted_zero_row_sample(monkeypatch) -> None:
    monkeypatch.setenv("OPENADAM_PROVIDER_ROOT", str(Path.cwd()))
    response = _call(
        "inspect",
        {
            "source": {
                "path": "capabilities/fixtures/users.json",
                "select": "data.users[*]",
            },
            "sample_rows": 0,
        },
    )

    assert response["ok"] is True
    assert response["result"]["sample"] == []
