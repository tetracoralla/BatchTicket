from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from data_transformer import DataTransformer
from data_transformer.cli import _spool_stdin
from data_transformer.errors import DataTransformerError
from data_transformer.json_values import canonical_json
from data_transformer.mcp_server import data_inspect, data_transform, mcp


def _plan(rows, steps, **extra):
    return {"version": "1", "sources": {"rows": {"inline": rows}}, "steps": steps, **extra}


def test_yaml_and_inline_non_string_keys_are_rejected_without_collision(tmp_path) -> None:
    inline = DataTransformer().inspect(
        {"inline": {1: "numeric", "1": "string"}, "kind": "tree"}
    )
    assert inline["error"]["code"] == "E_NON_STRING_KEY"

    source = tmp_path / "collision.yaml"
    source.write_text('1: numeric\n"1": string\n', encoding="utf-8")
    yaml_result = DataTransformer(tmp_path).inspect(
        {"path": str(source), "kind": "tree"}
    )
    assert yaml_result["error"]["code"] == "E_NON_STRING_KEY"


def test_coalesce_requires_explicit_cast_before_mixing_exact_integer_and_double() -> None:
    result = DataTransformer().transform(
        _plan(
            [{"value": 9_007_199_254_740_993}],
            [
                {
                    "op": "derive",
                    "field": "resolved",
                    "expr": {
                        "coalesce": [
                            {"field": "value"},
                            {"value": 0.0},
                        ]
                    },
                }
            ],
        )
    )

    assert result["error"]["code"] == "E_TYPE_MISMATCH"
    assert "cast explicitly" in result["error"]["message"]


def test_inline_json_json_file_and_jsonl_share_exact_decimal_meaning(tmp_path) -> None:
    json_path = tmp_path / "value.json"
    jsonl_path = tmp_path / "value.jsonl"
    json_path.write_text('[{"value":0.1}]\n', encoding="utf-8")
    jsonl_path.write_text('{"value":0.1}\n', encoding="utf-8")

    results = [
        DataTransformer().inspect({"inline": [{"value": 0.1}]}),
        DataTransformer(tmp_path).inspect({"path": str(json_path)}),
        DataTransformer(tmp_path).inspect({"path": str(jsonl_path)}),
    ]

    assert [result["status"] for result in results] == ["ok", "ok", "ok"]
    assert [result["sample"] for result in results] == [
        [{"value": "0.1"}],
        [{"value": "0.1"}],
        [{"value": "0.1"}],
    ]
    assert [result["shape"]["fields"]["value"]["type"] for result in results] == [
        "decimal(1,1)",
        "decimal(1,1)",
        "decimal(1,1)",
    ]


def test_mcp_complete_result_envelope_obeys_response_budget() -> None:
    row = {f"field_{index:02d}": "x" for index in range(20)}

    result = asyncio.run(
        data_inspect(
            {"inline": [row]},
            limits={"max_response_bytes": 2048},
        )
    )
    encoded = canonical_json(
        result.model_dump(mode="json", by_alias=True, exclude_none=True)
    ).encode("utf-8")

    assert result.structuredContent["status"] == "ok"
    assert len(encoded) <= 2048
    assert result.content[0].text.startswith("inspect: ok; rows=1; fields=field_00")
    assert len(result.content[0].text.encode("utf-8")) <= 256


def test_mcp_adaptation_summary_uses_selected_record_set_and_carries_small_draft() -> None:
    result = asyncio.run(
        data_inspect(
            {
                "inline": {
                    "events": [{"event": "created"}],
                    "users": [{"source_id": 2}],
                }
            },
            target_schema={
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
            mappings={"id": "source_id"},
        )
    )

    text = result.content[0].text
    assert text.startswith(
        "inspect: ok; adaptation=ready; record_set=users[*]; mappings=1; unresolved=0"
    )
    assert "; draft_plan=" in text
    assert '"select":"users[*]"' in text
    assert "record_set=events[*]" not in text


def test_mcp_transform_inline_text_is_optional_under_response_budget() -> None:
    rows = [{"id": index, "label": "x" * 20} for index in range(20)]
    result = asyncio.run(
        data_transform(
            {
                "version": "1",
                "sources": {"rows": {"inline": rows}},
                "steps": [{"op": "select", "fields": ["id", "label"]}],
                "limits": {"max_response_bytes": 2048},
            }
        )
    )
    encoded = canonical_json(
        result.model_dump(mode="json", by_alias=True, exclude_none=True)
    ).encode("utf-8")

    assert result.structuredContent["status"] == "ok"
    assert result.content[0].text == "transform: ok; rows_out=20"
    assert len(encoded) <= 2048


def test_mcp_top_level_contract_validation_runs_inside_the_worker() -> None:
    async def exercise():
        return await mcp._tool_manager.call_tool(
            "data_inspect",
            {"source": "not-an-object", "limits": {"max_response_bytes": 512}},
            convert_result=True,
        )

    result = asyncio.run(exercise())
    encoded = canonical_json(
        result.model_dump(mode="json", by_alias=True, exclude_none=True)
    ).encode("utf-8")

    assert result.structuredContent["error"]["code"] == "E_SOURCE_INVALID"
    assert len(encoded) <= 512

    encoded_object = asyncio.run(
        mcp._tool_manager.call_tool(
            "data_inspect",
            {"source": '{"inline":[{"id":1}]}'},
            convert_result=True,
        )
    )
    assert encoded_object.structuredContent["error"]["code"] == "E_SOURCE_INVALID"


def test_mcp_cancellation_reaches_the_background_worker(monkeypatch) -> None:
    cancellation_observed = threading.Event()

    def wait_for_cancel(self, source, *, sample_rows=5, limits=None):
        del source, sample_rows, limits
        assert self._cancel_event is not None
        self._cancel_event.wait(timeout=2)
        cancellation_observed.set()
        return {"status": "error", "operation": "inspect"}

    monkeypatch.setattr(DataTransformer, "inspect", wait_for_cancel)

    async def exercise() -> None:
        task = asyncio.create_task(data_inspect({"inline": [{"id": 1}]}))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert cancellation_observed.wait(timeout=1)


def test_cancelled_transform_cannot_publish_a_reserved_output(tmp_path) -> None:
    cancelled = threading.Event()
    cancel_lock = threading.Lock()
    target = tmp_path / "result.json"
    result: dict[str, object] = {}

    def transform() -> None:
        result.update(
            DataTransformer(
                tmp_path,
                worker_start_method="spawn",
                _cancel_event=cancelled,
                _cancel_lock=cancel_lock,
            ).transform(
                _plan(
                    [{"id": index} for index in range(1_000)],
                    [{"op": "select", "fields": ["id"]}],
                    output={"path": str(target)},
                )
            )
        )

    cancel_lock.acquire()
    thread = threading.Thread(target=transform)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while not list(tmp_path.glob(".*.adt-stage-*")) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert list(tmp_path.glob(".*.adt-stage-*"))
        cancelled.set()
    finally:
        cancel_lock.release()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result["error"]["code"] == "E_CANCELLED"
    assert not target.exists()
    assert list(tmp_path.glob(".*.adt-stage-*")) == []


def test_stdin_spooling_is_byte_bounded_and_cleans_partial_file(
    tmp_path, monkeypatch
) -> None:
    real_mkstemp = tempfile.mkstemp

    def local_mkstemp(*, prefix, suffix):
        return real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)

    monkeypatch.setattr("data_transformer.cli.tempfile.mkstemp", local_mkstemp)
    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * 20))

    with pytest.raises(DataTransformerError) as raised:
        _spool_stdin("json", maximum=10, timeout_ms=1_000)

    assert raised.value.code == "E_INPUT_TOO_LARGE"
    assert list(tmp_path.iterdir()) == []


def test_cli_plan_from_stdin_resolves_relative_sources_from_caller_directory() -> None:
    plan = {
        "version": "1",
        "sources": {
            "users": {"path": "examples/users.json", "select": "data.users[*]"}
        },
        "steps": [{"op": "select", "fields": ["userId"]}],
    }
    completed = subprocess.run(
        [sys.executable, "-m", "data_transformer.cli", "transform", "-"],
        input=json.dumps(plan),
        capture_output=True,
        text=True,
        check=False,
        cwd=Path.cwd(),
        env=os.environ.copy(),
    )

    result = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert result["status"] == "ok"
    assert result["summary"]["rows_out"] == 3
