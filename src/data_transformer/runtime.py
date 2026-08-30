from __future__ import annotations

import ctypes
import dataclasses
import hashlib
import json
import multiprocessing
import os
import sys
import threading
import time
from _thread import LockType
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .contracts import ASSERTIONS_ADAPTER, SOURCE_ADAPTER, LimitsModel, TransformationPlan
from .dataset import DataSet
from .diffing import diff_datasets
from .errors import DataTransformerError
from .formats import infer_format, parse_json, parse_yaml
from .json_values import canonical_json, json_safe, json_size, normalize_source_numbers
from .limits import Limits
from .operations import OperationExecutor
from .output import (
    check_output_feasibility,
    preflight_output,
    publish_staged_output,
    reserve_staging_output,
    write_output,
)
from .schema_adapter import adapt_schema
from .validation import evaluate_assertions, require_assertions_passed, validate_schema
from .workspace import SHAPE_FIELD_CAP, Workspace, bound_shape


class DataTransformer:
    """Shared deterministic core used by the CLI and MCP adapters."""

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        restrict_paths: bool = False,
        worker_start_method: str | None = None,
        _inside_worker: bool = False,
        _cancel_event: threading.Event | None = None,
        _cancel_lock: LockType | None = None,
    ) -> None:
        self.resource_root = Path(base_dir).resolve() if base_dir is not None else None
        self.base_dir = self.resource_root or Path.cwd().resolve()
        self.restrict_paths = restrict_paths
        self.worker_start_method = worker_start_method or (
            "forkserver" if os.name == "posix" else "spawn"
        )
        self._inside_worker = _inside_worker
        self._cancel_event = _cancel_event
        self._cancel_lock = _cancel_lock

    def inspect(
        self,
        source: dict[str, Any],
        *,
        sample_rows: int = 5,
        target_schema: dict[str, Any] | None = None,
        mappings: dict[str, str] | None = None,
        limits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._inside_worker:
            return self._run_worker(
                "inspect",
                {
                    "source": source,
                    "sample_rows": sample_rows,
                    "target_schema": target_schema,
                    "mappings": mappings,
                    "limits": limits,
                },
            )
        return self._safe(
            "inspect",
            lambda: self._inspect(
                source,
                sample_rows=sample_rows,
                target_schema=target_schema,
                mappings=mappings,
                limits=limits,
            ),
        )

    def transform(
        self,
        plan: dict[str, Any],
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if not self._inside_worker:
            return self._run_worker("transform", {"plan": plan, "dry_run": dry_run})
        return self._safe("transform", lambda: self._transform(plan, dry_run=dry_run))

    def validate(
        self,
        source: dict[str, Any],
        *,
        schema: dict[str, Any] | None = None,
        assertions: list[dict[str, Any]] | None = None,
        sample_rows: int = 5,
        limits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._inside_worker:
            return self._run_worker(
                "validate",
                {
                    "source": source,
                    "schema": schema,
                    "assertions": assertions,
                    "sample_rows": sample_rows,
                    "limits": limits,
                },
            )
        return self._safe(
            "validate",
            lambda: self._validate(
                source,
                schema=schema,
                assertions=assertions,
                sample_rows=sample_rows,
                limits=limits,
            ),
        )

    def diff(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        *,
        key_fields: list[str] | None = None,
        sample_rows: int = 5,
        limits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._inside_worker:
            return self._run_worker(
                "diff",
                {
                    "left": left,
                    "right": right,
                    "key_fields": key_fields,
                    "sample_rows": sample_rows,
                    "limits": limits,
                },
            )
        return self._safe(
            "diff",
            lambda: self._diff(
                left,
                right,
                key_fields=key_fields,
                sample_rows=sample_rows,
                limits=limits,
            ),
        )

    def run_cli_request(
        self,
        request: dict[str, Any],
        *,
        acquisition_elapsed_ms: int = 0,
    ) -> dict[str, Any]:
        """Parse CLI documents and execute them inside the isolated worker."""
        return self._run_worker(
            "cli", {**request, "_acquisition_elapsed_ms": acquisition_elapsed_ms}
        )

    def _inspect(
        self,
        source: dict[str, Any],
        *,
        sample_rows: int,
        target_schema: dict[str, Any] | None,
        mappings: dict[str, str] | None,
        limits: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self._validate_source(source)
        self._validate_adaptation_input(target_schema, mappings)
        self._validate_limits(limits)
        active_limits = Limits.from_dict(limits, {"sample_rows": sample_rows})
        with self._workspace(active_limits) as workspace:
            dataset = workspace.load_source(source, "source", self.base_dir, active_limits)
            shape = workspace.shape(dataset, active_limits.sample_rows, include_quality=True)
            sample = shape.pop("sample", None)
            if target_schema is not None:
                adapter_bytes = json_size(target_schema) + json_size(mappings or {})
                combined_bytes = dataset.byte_size + adapter_bytes
                if combined_bytes > active_limits.max_input_bytes:
                    raise DataTransformerError(
                        "E_INPUT_TOO_LARGE",
                        "source and schema adapter inputs exceed the cumulative byte limit",
                        {"bytes": combined_bytes, "maximum": active_limits.max_input_bytes},
                    )
            result: dict[str, Any] = {
                "status": "ok",
                "operation": "inspect",
                "source": {
                    "format": dataset.source_format,
                    "bytes": dataset.byte_size,
                },
                "shape": bound_shape(shape),
            }
            if sample is not None:
                result["sample"] = sample
            if target_schema is not None:
                result["adaptation"] = adapt_schema(
                    source,
                    shape,
                    target_schema,
                    mappings,
                    sample_rows=active_limits.sample_rows,
                )
            return result

    def _transform(
        self,
        plan: dict[str, Any],
        *,
        dry_run: bool | None,
    ) -> dict[str, Any]:
        if dry_run is not None and not isinstance(dry_run, bool):
            raise DataTransformerError(
                "E_PLAN_INVALID", "dry_run override must be a boolean"
            )
        plan = self._validate_plan(plan)
        return_policy = plan.get("return", {})
        active_limits = Limits.from_dict(plan.get("limits"), return_policy)
        if len(plan["steps"]) > active_limits.max_steps:
            raise DataTransformerError(
                "E_STEP_LIMIT",
                "plan exceeds step limit",
                {"steps": len(plan["steps"]), "maximum": active_limits.max_steps},
            )
        is_dry_run = plan.get("dry_run", False) if dry_run is None else dry_run
        mode = return_policy.get("mode", "auto")
        if mode == "reference" and "output" not in plan:
            raise DataTransformerError(
                "E_OUTPUT_REQUIRED", "reference return mode requires output.path"
            )
        with self._workspace(active_limits) as workspace:
            datasets: dict[str, DataSet] = {}
            source_effects: dict[str, Any] = {}
            if len(plan["sources"]) > active_limits.max_sources:
                raise DataTransformerError(
                    "E_SOURCE_LIMIT",
                    "plan exceeds source count limit",
                    {"sources": len(plan["sources"]), "maximum": active_limits.max_sources},
                )
            cumulative_bytes = 0
            cumulative_rows = 0
            for name, source_spec in plan["sources"].items():
                dataset = workspace.load_source(source_spec, name, self.base_dir, active_limits)
                cumulative_bytes += dataset.byte_size
                if cumulative_bytes > active_limits.max_input_bytes:
                    raise DataTransformerError(
                        "E_INPUT_TOO_LARGE",
                        "combined sources exceed the cumulative byte limit",
                        {"bytes": cumulative_bytes, "maximum": active_limits.max_input_bytes},
                    )
                source_shape = workspace.shape(dataset)
                cumulative_rows += int(source_shape.get("rows", source_shape.get("items", 1)))
                if cumulative_rows > active_limits.max_rows:
                    raise DataTransformerError(
                        "E_ROW_LIMIT",
                        "combined sources exceed the cumulative row limit",
                        {"rows": cumulative_rows, "maximum": active_limits.max_rows},
                    )
                datasets[name] = dataset
                source_effects[name] = {
                    "format": dataset.source_format,
                    "bytes": dataset.byte_size,
                    "shape": bound_shape(source_shape),
                }

            executor = OperationExecutor(workspace, active_limits)
            step_effects: list[dict[str, Any]] = []
            previous: str | None = None
            for index, step in enumerate(plan["steps"]):
                step_id = step.get("id", f"step_{index + 1}")
                if step_id in datasets:
                    raise DataTransformerError(
                        "E_DUPLICATE_ID",
                        "step id duplicates a source or earlier step",
                        {"id": step_id},
                    )
                source_reference = step.get("source")
                if step.get("op") == "join":
                    source = None
                    before_shape = None
                else:
                    if source_reference is None:
                        if previous is not None:
                            source_reference = previous
                        elif len(plan["sources"]) == 1:
                            source_reference = next(iter(plan["sources"]))
                        else:
                            raise DataTransformerError(
                                "E_SOURCE_REFERENCE",
                                "first step must name a source when the plan has multiple sources",
                                step_id=step_id,
                            )
                    source = datasets.get(source_reference)
                    if source is None:
                        raise DataTransformerError(
                            "E_SOURCE_REFERENCE",
                            "step source does not exist",
                            {"source": source_reference},
                            step_id=step_id,
                        )
                    before_shape = workspace.shape(source)
                output_dataset = executor.execute(step, source, datasets, step_id)
                workspace.check_dataset_structure(output_dataset, step_id)
                after_shape = workspace.shape(output_dataset)
                datasets[step_id] = output_dataset
                previous = step_id
                step_effects.append(
                    _bound_step_effects(
                        self._step_effects(
                            step_id, step["op"], before_shape, after_shape, output_dataset
                        )
                    )
                )

            if previous is None:
                raise DataTransformerError("E_PLAN_INVALID", "plan has no executable steps")
            final = datasets[previous]
            schema_result = None
            if "schema" in plan:
                schema_result = validate_schema(workspace, final, plan["schema"])
                if schema_result["status"] != "passed":
                    raise DataTransformerError(
                        "E_SCHEMA_FAILED",
                        "transformed data does not satisfy the output schema",
                        {"validation": schema_result},
                    )
            assertion_results = evaluate_assertions(workspace, final, plan.get("assert"))
            require_assertions_passed(assertion_results)

            final_shape = workspace.shape(final, include_quality=True)
            sample = self._sample(workspace, final, active_limits.sample_rows)
            output_reference = None
            if "output" in plan:
                output_format = infer_format(
                    Path(plan["output"]["path"]), plan["output"].get("format")
                )
                check_output_feasibility(workspace, final, output_format)
                if not is_dry_run:
                    output_reference = write_output(workspace, final, plan["output"], self.base_dir)

            inline, inline_fits = self._inline_value(
                workspace, final, active_limits.max_inline_bytes
            )
            if mode == "inline" and not inline_fits:
                raise DataTransformerError(
                    "E_OUTPUT_TOO_LARGE",
                    "result exceeds max_inline_bytes",
                    {"maximum": active_limits.max_inline_bytes},
                )
            if mode == "auto" and "output" not in plan and not inline_fits:
                raise DataTransformerError(
                    "E_OUTPUT_REQUIRED",
                    "result is too large to return inline; provide output.path or use summary mode",
                    {"maximum_inline_bytes": active_limits.max_inline_bytes},
                )

            execution_effects = {
                "plan_version": "1",
                "sources": source_effects,
                "steps": step_effects,
                "assertions": assertion_results,
                "schema_validation": schema_result,
                "warnings": [warning for step in step_effects for warning in step["warnings"]],
                "final_shape": bound_shape(final_shape),
                "result_sha256": self._dataset_hash(workspace, final),
            }
            result_descriptor: dict[str, Any]
            if output_reference is not None:
                result_descriptor = {"kind": "file", **output_reference}
            elif is_dry_run or mode == "summary":
                result_descriptor = {"kind": "summary"}
            else:
                result_descriptor = {"kind": "inline", "data": inline}
            if not is_dry_run and mode == "inline" and inline_fits:
                result_descriptor = {"kind": "inline", "data": inline}
                if output_reference is not None:
                    result_descriptor["output"] = output_reference
            return {
                "status": "dry_run" if is_dry_run else "ok",
                "operation": "transform",
                "result": result_descriptor,
                "summary": self._summary(source_effects, final_shape),
                "sample": sample,
                "execution_effects": execution_effects,
            }

    def _validate(
        self,
        source: dict[str, Any],
        *,
        schema: dict[str, Any] | None,
        assertions: list[dict[str, Any]] | None,
        sample_rows: int,
        limits: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self._validate_source(source)
        self._validate_limits(limits)
        if assertions is not None:
            try:
                ASSERTIONS_ADAPTER.validate_python(assertions)
            except ValidationError as exc:
                first = exc.errors(include_url=False)[0]
                raise DataTransformerError(
                    "E_ASSERTION_INVALID",
                    "assertions do not satisfy the published contract",
                    {"path": list(first["loc"]), "message": first["msg"]},
                ) from exc
        if schema is None and assertions is None:
            raise DataTransformerError(
                "E_VALIDATION_REQUIRED", "provide a schema, assertions, or both"
            )
        active_limits = Limits.from_dict(limits, {"sample_rows": sample_rows})
        with self._workspace(active_limits) as workspace:
            dataset = workspace.load_source(source, "source", self.base_dir, active_limits)
            schema_result = (
                validate_schema(workspace, dataset, schema) if schema is not None else None
            )
            assertion_results = evaluate_assertions(workspace, dataset, assertions)
            valid = (schema_result is None or schema_result["status"] == "passed") and all(
                result["status"] == "passed" for result in assertion_results
            )
            return {
                "status": "ok",
                "operation": "validate",
                "valid": valid,
                "shape": bound_shape(workspace.shape(dataset, include_quality=True)),
                "schema_validation": schema_result,
                "assertions": assertion_results,
                "sample": self._sample(workspace, dataset, active_limits.sample_rows),
            }

    def _diff(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        *,
        key_fields: list[str] | None,
        sample_rows: int,
        limits: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self._validate_source(left)
        self._validate_source(right)
        self._validate_limits(limits)
        active_limits = Limits.from_dict(limits, {"sample_rows": sample_rows})
        if key_fields is not None and (
            not isinstance(key_fields, list)
            or not all(isinstance(field, str) and field for field in key_fields)
        ):
            raise DataTransformerError(
                "E_DIFF_KEY_INVALID", "key_fields must be an array of fields"
            )
        with self._workspace(active_limits) as workspace:
            if active_limits.max_sources < 2:
                raise DataTransformerError(
                    "E_SOURCE_LIMIT",
                    "diff requires two sources within the source count limit",
                    {"sources": 2, "maximum": active_limits.max_sources},
                )
            left_data = workspace.load_source(left, "left", self.base_dir, active_limits)
            right_data = workspace.load_source(right, "right", self.base_dir, active_limits)
            combined_bytes = left_data.byte_size + right_data.byte_size
            if combined_bytes > active_limits.max_input_bytes:
                raise DataTransformerError(
                    "E_INPUT_TOO_LARGE",
                    "combined sources exceed the cumulative byte limit",
                    {"bytes": combined_bytes, "maximum": active_limits.max_input_bytes},
                )
            left_shape = workspace.shape(left_data)
            right_shape = workspace.shape(right_data)
            combined_rows = int(left_shape.get("rows", left_shape.get("items", 1))) + int(
                right_shape.get("rows", right_shape.get("items", 1))
            )
            if combined_rows > active_limits.max_rows:
                raise DataTransformerError(
                    "E_ROW_LIMIT",
                    "combined sources exceed the cumulative row limit",
                    {"rows": combined_rows, "maximum": active_limits.max_rows},
                )
            difference = diff_datasets(
                workspace, left_data, right_data, key_fields, active_limits.sample_rows
            )
            return {"status": "ok", "operation": "diff", **difference}

    def _validate_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, dict):
            raise DataTransformerError("E_PLAN_INVALID", "plan must be an object")
        try:
            validated = TransformationPlan.model_validate(plan)
        except ValidationError as exc:
            first = exc.errors(include_url=False)[0]
            location = list(first["loc"])
            if "expr" in location:
                code = "E_EXPRESSION_INVALID"
                message = "expression does not satisfy the safe expression contract"
            elif "where" in location:
                code = "E_CONDITION_INVALID"
                message = "condition does not satisfy the safe condition contract"
            else:
                code = "E_PLAN_INVALID"
                message = "plan does not satisfy Transformation Plan v1"
            raise DataTransformerError(
                code,
                message,
                {"path": location, "message": first["msg"]},
            ) from exc
        return normalize_source_numbers(
            validated.model_dump(mode="python", by_alias=True, exclude_unset=True)
        )

    def _validate_source(self, source: Any) -> None:
        try:
            SOURCE_ADAPTER.validate_python(source)
        except ValidationError as exc:
            first = exc.errors(include_url=False)[0]
            raise DataTransformerError(
                "E_SOURCE_INVALID",
                "source does not satisfy the published contract",
                {"path": list(first["loc"]), "message": first["msg"]},
            ) from exc

    def _validate_adaptation_input(self, target_schema: Any, mappings: Any) -> None:
        if target_schema is not None and not isinstance(target_schema, dict):
            raise DataTransformerError(
                "E_SCHEMA_INVALID", "target_schema must be a JSON Schema object"
            )
        if mappings is not None:
            if target_schema is None:
                raise DataTransformerError(
                    "E_ADAPTER_MAPPING_INVALID", "mappings require target_schema"
                )
            if (
                not isinstance(mappings, dict)
                or len(mappings) > SHAPE_FIELD_CAP
                or not all(
                    isinstance(target, str)
                    and target
                    and isinstance(source, str)
                    and source
                    for target, source in mappings.items()
                )
            ):
                raise DataTransformerError(
                    "E_ADAPTER_MAPPING_INVALID",
                    "mappings must contain at most 1000 non-empty target and source fields",
                )

    def _validate_limits(self, limits: Any) -> None:
        if limits is None:
            return
        try:
            LimitsModel.model_validate(limits)
        except ValidationError as exc:
            first = exc.errors(include_url=False)[0]
            raise DataTransformerError(
                "E_LIMIT_INVALID",
                "limits do not satisfy the published contract",
                {"path": list(first["loc"]), "message": first["msg"]},
            ) from exc

    def _workspace(self, limits: Limits) -> Workspace:
        return Workspace(
            limits,
            resource_root=self.resource_root if self.restrict_paths else None,
            restricted_paths=self.restrict_paths,
        )

    def _run_worker(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        limits = _limits_for_operation(operation, payload)
        reported_operation = payload.get("command", operation) if operation == "cli" else operation
        staging: Path | None = None
        prepared: dict[str, Any] | None = None
        worker_payload = payload
        if operation == "transform":
            try:
                worker_payload, staging, prepared = self._prepare_transform_output(payload, limits)
            except DataTransformerError as exc:
                return exc.to_result(operation)
        if self._cancel_event is not None and self._cancel_event.is_set():
            if staging is not None:
                staging.unlink(missing_ok=True)
            return DataTransformerError(
                "E_CANCELLED", "data operation was cancelled"
            ).to_result(reported_operation)

        context = multiprocessing.get_context(self.worker_start_method)
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_worker_entry,
            args=(
                sender,
                operation,
                worker_payload,
                str(self.resource_root) if self.resource_root is not None else None,
                self.restrict_paths,
            ),
            daemon=True,
        )
        acquisition_elapsed_ms = (
            payload.get("_acquisition_elapsed_ms", 0) if operation == "cli" else 0
        )
        if isinstance(acquisition_elapsed_ms, bool) or not isinstance(
            acquisition_elapsed_ms, int
        ):
            acquisition_elapsed_ms = 0
        started = time.monotonic() - max(acquisition_elapsed_ms, 0) / 1000
        response: bytes | None = None
        failure: DataTransformerError | None = None
        peak_rss = 0
        try:
            process.start()
            sender.close()
            while True:
                if self._cancel_event is not None and self._cancel_event.is_set():
                    failure = DataTransformerError(
                        "E_CANCELLED", "data operation was cancelled"
                    )
                    break
                rss = _process_rss_bytes(process.pid)
                if rss is not None:
                    peak_rss = max(peak_rss, rss)
                    if peak_rss > limits.max_memory_mb * 1024 * 1024:
                        failure = DataTransformerError(
                            "E_MEMORY",
                            "data operation exceeded the process memory limit",
                            {"maximum_mb": limits.max_memory_mb},
                        )
                        break
                if receiver.poll(0.01):
                    message = receiver.recv()
                    if isinstance(message, tuple) and message[0] == "limits":
                        limits = Limits(**message[1])
                        elapsed_ms = (time.monotonic() - started) * 1000
                        if elapsed_ms > limits.timeout_ms:
                            failure = DataTransformerError(
                                "E_TIMEOUT",
                                "data operation exceeded the whole-call wall-clock limit",
                                {"timeout_ms": limits.timeout_ms},
                            )
                            break
                        if peak_rss > limits.max_memory_mb * 1024 * 1024:
                            failure = DataTransformerError(
                                "E_MEMORY",
                                "data operation exceeded the process memory limit",
                                {"maximum_mb": limits.max_memory_mb},
                            )
                            break
                        continue
                    if isinstance(message, tuple) and message[0] == "stage":
                        staging = Path(message[1]["staging"])
                        prepared = {
                            "target": Path(message[1]["target"]),
                            "overwrite": message[1]["overwrite"],
                        }
                        continue
                    if isinstance(message, tuple) and message[0] == "result":
                        response = message[1]
                        break
                    failure = DataTransformerError(
                        "E_WORKER_FAILED", "isolated data worker sent an invalid message"
                    )
                    break
                elapsed_ms = (time.monotonic() - started) * 1000
                if elapsed_ms > limits.timeout_ms:
                    failure = DataTransformerError(
                        "E_TIMEOUT",
                        "data operation exceeded the whole-call wall-clock limit",
                        {"timeout_ms": limits.timeout_ms},
                    )
                    break
                if not process.is_alive():
                    if receiver.poll():
                        message = receiver.recv()
                        if isinstance(message, tuple) and message[0] == "result":
                            response = message[1]
                    else:
                        failure = DataTransformerError(
                            "E_WORKER_FAILED", "isolated data worker exited without a result"
                        )
                    break
            if failure is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=2)
                return failure.to_result(reported_operation)
            process.join(timeout=2)
            if self._cancel_event is not None and self._cancel_event.is_set():
                return DataTransformerError(
                    "E_CANCELLED", "data operation was cancelled"
                ).to_result(reported_operation)
            if response is None:
                return DataTransformerError(
                    "E_WORKER_FAILED", "isolated data worker returned no result"
                ).to_result(reported_operation)
            result = json.loads(response)
            if result.get("status") not in {"ok", "dry_run"}:
                return result
            if self._cancel_event is not None and self._cancel_event.is_set():
                return DataTransformerError(
                    "E_CANCELLED", "data operation was cancelled"
                ).to_result(reported_operation)
            if staging is not None and prepared is not None:
                if self._cancel_lock is None:
                    publish_staged_output(
                        staging,
                        prepared["target"],
                        prepared["overwrite"],
                    )
                else:
                    with self._cancel_lock:
                        if self._cancel_event is not None and self._cancel_event.is_set():
                            return DataTransformerError(
                                "E_CANCELLED", "data operation was cancelled"
                            ).to_result(reported_operation)
                        publish_staged_output(
                            staging,
                            prepared["target"],
                            prepared["overwrite"],
                        )
                staging = None
                _rewrite_output_reference(result, prepared["target"])
            return result
        except (OSError, EOFError, json.JSONDecodeError):
            return DataTransformerError(
                "E_WORKER_FAILED", "isolated data worker communication failed"
            ).to_result(reported_operation)
        except DataTransformerError as exc:
            return exc.to_result(reported_operation)
        finally:
            receiver.close()
            sender.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if staging is not None:
                staging.unlink(missing_ok=True)

    def _prepare_transform_output(
        self,
        payload: dict[str, Any],
        limits: Limits,
    ) -> tuple[dict[str, Any], Path | None, dict[str, Any] | None]:
        plan = payload.get("plan")
        if not isinstance(plan, dict) or not isinstance(plan.get("output"), dict):
            return payload, None, None
        output = plan["output"]
        if not isinstance(output.get("path"), str):
            return payload, None, None
        prepared = preflight_output(
            output,
            self.base_dir,
            resource_root=self.resource_root,
            restricted_paths=self.restrict_paths,
        )
        requested_dry_run = payload.get("dry_run")
        is_dry_run = plan.get("dry_run", False) if requested_dry_run is None else requested_dry_run
        if is_dry_run:
            return payload, None, prepared
        staging = reserve_staging_output(prepared["target"])
        if self.restrict_paths:
            if self.resource_root is None:
                staging.unlink(missing_ok=True)
                raise DataTransformerError(
                    "E_WORKSPACE_REQUIRED",
                    "file outputs require an explicitly granted MCP workspace",
                )
            worker_path = str(staging.relative_to(self.resource_root))
        else:
            worker_path = str(staging)
        worker_plan = {
            **plan,
            "output": {
                **output,
                "path": worker_path,
                "format": prepared["format"],
                "overwrite": True,
            },
        }
        return {**payload, "plan": worker_plan}, staging, prepared

    def _safe(self, operation: str, callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return callback()
        except DataTransformerError as exc:
            return exc.to_result(operation)
        except MemoryError:
            return DataTransformerError(
                "E_MEMORY", "data operation exceeded the process memory limit"
            ).to_result(operation)
        except RecursionError:
            return DataTransformerError(
                "E_DEPTH_LIMIT", "data operation exceeded the nesting depth limit"
            ).to_result(operation)
        except Exception:
            return DataTransformerError("E_INTERNAL", "unexpected internal error").to_result(
                operation
            )

    def _step_effects(
        self,
        step_id: str,
        operation: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        output: DataSet,
    ) -> dict[str, Any]:
        if output.effects.get("tree_changes") is not None:
            changes = output.effects["tree_changes"]
            return {
                "id": step_id,
                "op": operation,
                "rows_in": None,
                "rows_out": None,
                "row_delta": None,
                "fields_added": changes["paths_added"],
                "fields_removed": changes["paths_removed"],
                "values_changed": changes["values_changed"],
                "type_changes": changes["type_changes"],
                "warnings": output.warnings,
            }
        if operation == "join" and output.effects:
            effects = output.effects
            left = effects["inputs"]["left"]
            right = effects["inputs"]["right"]
            after_rows = after.get("rows")
            return {
                "id": step_id,
                "op": operation,
                "inputs": effects["inputs"],
                "rows_in": left["rows"] + right["rows"],
                "rows_out": after_rows,
                "row_delta": (
                    after_rows - left["rows"] - right["rows"]
                    if isinstance(after_rows, int)
                    else None
                ),
                "row_delta_basis": "combined_inputs",
                "fields_added": effects["right_output_fields"],
                "fields_removed": [],
                "type_changes": [],
                "matches": effects["matches"],
                "unmatched": effects["unmatched"],
                "fan_out": effects["fan_out"],
                "new_nulls": effects["new_nulls"],
                "warnings": output.warnings,
            }
        before_fields = set((before or {}).get("fields", {}))
        after_fields = set(after.get("fields", {}))
        before_rows = (before or {}).get("rows")
        after_rows = after.get("rows")
        type_changes: list[dict[str, Any]] = []
        if before is not None:
            for field in sorted(before_fields & after_fields):
                old_type = before["fields"][field].get("type")
                new_type = after["fields"][field].get("type")
                if old_type != new_type:
                    type_changes.append({"field": field, "from": old_type, "to": new_type})
        return {
            "id": step_id,
            "op": operation,
            "rows_in": before_rows,
            "rows_out": after_rows,
            "row_delta": after_rows - before_rows
            if isinstance(after_rows, int) and isinstance(before_rows, int)
            else None,
            "fields_added": sorted(after_fields - before_fields),
            "fields_removed": sorted(before_fields - after_fields),
            "type_changes": type_changes,
            "warnings": output.warnings,
        }

    def _inline_value(
        self, workspace: Workspace, dataset: DataSet, maximum: int
    ) -> tuple[Any | None, bool]:
        if dataset.is_tree:
            return (json_safe(dataset.value), json_size(dataset.value) <= maximum)
        rows: list[dict[str, Any]] = []
        running_size = 2
        for row in workspace.iter_rows(dataset):
            encoded = canonical_json(row).encode("utf-8")
            running_size += len(encoded) + (1 if rows else 0)
            if running_size > maximum:
                return None, False
            rows.append(row)
        return rows, True

    def _sample(self, workspace: Workspace, dataset: DataSet, count: int) -> Any:
        if count == 0:
            return []
        if dataset.is_table:
            return workspace.rows(dataset, count)
        value = dataset.value
        if isinstance(value, list):
            return json_safe(value[:count])
        if isinstance(value, dict):
            return {key: json_safe(item) for key, item in list(value.items())[:count]}
        return json_safe(value)

    def _summary(self, sources: dict[str, Any], final_shape: dict[str, Any]) -> dict[str, Any]:
        input_rows = sum(
            source["shape"].get("rows", source["shape"].get("items", 1))
            for source in sources.values()
        )
        return {
            "sources": len(sources),
            "rows_in": input_rows,
            "rows_out": final_shape.get("rows", final_shape.get("items", 1)),
            "columns_out": final_shape.get("columns", final_shape.get("field_count")),
        }

    def _dataset_hash(self, workspace: Workspace, dataset: DataSet) -> str:
        digest = hashlib.sha256()
        if dataset.is_tree:
            digest.update(canonical_json(dataset.value).encode("utf-8"))
        else:
            for row in workspace.iter_rows(dataset):
                digest.update(canonical_json(row).encode("utf-8"))
                digest.update(b"\n")
        return digest.hexdigest()


_EFFECT_LIST_KEYS = (
    "fields_added",
    "fields_removed",
    "values_changed",
    "paths_added",
    "paths_removed",
    "type_changes",
)


def _bound_step_effects(effects: dict[str, Any]) -> dict[str, Any]:
    truncated: dict[str, dict[str, int]] = {}
    for key in _EFFECT_LIST_KEYS:
        values = effects.get(key)
        if isinstance(values, list) and len(values) > SHAPE_FIELD_CAP:
            truncated[key] = {"total": len(values), "returned": SHAPE_FIELD_CAP}
            effects[key] = values[:SHAPE_FIELD_CAP]
    if truncated:
        effects["truncated"] = truncated
    return effects


def _limits_for_operation(operation: str, payload: dict[str, Any]) -> Limits:
    try:
        if operation == "cli" and payload.get("command") == "transform":
            return dataclasses.replace(
                Limits(),
                timeout_ms=Limits.HARD_MAX_TIMEOUT_MS,
                max_memory_mb=Limits.HARD_MAX_MEMORY_MB,
            )
        if operation == "transform":
            plan = payload.get("plan")
            if isinstance(plan, dict):
                raw_limits = plan.get("limits") if isinstance(plan.get("limits"), dict) else None
                return_policy = plan.get("return") if isinstance(plan.get("return"), dict) else None
                return Limits.from_dict(raw_limits, return_policy)
        raw_limits = payload.get("limits")
        return Limits.from_dict(
            raw_limits if isinstance(raw_limits, dict) else None,
            {"sample_rows": payload.get("sample_rows", Limits.sample_rows)},
        )
    except DataTransformerError:
        return Limits()


def _worker_entry(
    sender: Any,
    operation: str,
    payload: dict[str, Any],
    resource_root: str | None,
    restrict_paths: bool,
) -> None:
    try:
        transformer = DataTransformer(
            resource_root,
            restrict_paths=restrict_paths,
            _inside_worker=True,
        )
        if operation == "cli":
            result, limits = _execute_cli_request(transformer, payload, sender)
            result_operation = str(payload.get("command", "cli"))
        else:
            limits = _limits_for_operation(operation, payload)
            sender.send(("limits", dataclasses.asdict(limits)))
            _check_request_structure(payload, limits)
            result = getattr(transformer, operation)(**payload)
            result_operation = operation
    except DataTransformerError as exc:
        result_operation = str(payload.get("command", operation))
        result = exc.to_result(result_operation)
        limits = _limits_for_operation(operation, payload)
    except BaseException:
        result_operation = str(payload.get("command", operation))
        result = DataTransformerError(
            "E_WORKER_FAILED", "isolated data worker failed"
        ).to_result(result_operation)
        limits = _limits_for_operation(operation, payload)
    try:
        encoded = canonical_json(result).encode("utf-8")
        if len(encoded) > limits.max_response_bytes:
            result = DataTransformerError(
                "E_RESPONSE_TOO_LARGE",
                "complete serialized response exceeds the response budget",
                {"bytes": len(encoded), "maximum": limits.max_response_bytes},
            ).to_result(result_operation)
            encoded = canonical_json(result).encode("utf-8")
        sender.send(("result", encoded))
    except BaseException:
        fallback = DataTransformerError("E_WORKER_FAILED", "isolated data worker failed").to_result(
            result_operation
        )
        with suppress(BaseException):
            sender.send(("result", canonical_json(fallback).encode("utf-8")))
    finally:
        sender.close()


def _execute_cli_request(
    transformer: DataTransformer,
    payload: dict[str, Any],
    sender: Any,
) -> tuple[dict[str, Any], Limits]:
    command = payload.get("command")
    request = payload.get("request")
    if not isinstance(request, dict):
        raise DataTransformerError("E_CLI_INPUT", "CLI request must be an object")
    document_bytes = 0
    if command == "transform":
        plan, base_dir, size = _read_cli_document(request.get("plan"))
        document_bytes += size
        if not isinstance(plan, dict):
            raise DataTransformerError("E_PLAN_INVALID", "plan must be an object")
        normalized = transformer._validate_plan(plan)
        limits = Limits.from_dict(normalized.get("limits"), normalized.get("return"))
        sender.send(("limits", dataclasses.asdict(limits)))
        _check_cli_document_budget(document_bytes, limits)
        _check_cli_temporary_budget(request, limits)
        _check_request_structure(normalized, limits)
        transformer.base_dir = base_dir
        plan_for_worker = normalized
        output = normalized.get("output")
        dry_run = request.get("dry_run")
        is_dry_run = normalized.get("dry_run", False) if dry_run is None else dry_run
        if isinstance(output, dict):
            prepared = preflight_output(output, base_dir)
            if not is_dry_run:
                staging = reserve_staging_output(prepared["target"])
                sender.send(
                    (
                        "stage",
                        {
                            "staging": str(staging),
                            "target": str(prepared["target"]),
                            "overwrite": prepared["overwrite"],
                        },
                    )
                )
                plan_for_worker = {
                    **normalized,
                    "output": {
                        **output,
                        "path": str(staging),
                        "format": prepared["format"],
                        "overwrite": True,
                    },
                }
        return transformer.transform(plan_for_worker, dry_run=dry_run), limits

    limits = Limits.from_dict(
        None,
        {"sample_rows": request.get("sample_rows", Limits.sample_rows)},
    )
    sender.send(("limits", dataclasses.asdict(limits)))
    if command == "inspect":
        source, base_dir, size = _cli_source(request.get("source"))
        document_bytes += size
        target_schema = None
        mappings = None
        if request.get("target_schema") is not None:
            target_schema, _, size = _read_cli_document(request["target_schema"])
            document_bytes += size
        if request.get("mappings") is not None:
            mappings, _, size = _read_cli_document(request["mappings"])
            document_bytes += size
        _check_cli_document_budget(document_bytes, limits)
        _check_cli_temporary_budget(request, limits)
        _check_request_structure(
            {"target_schema": target_schema, "mappings": mappings}, limits
        )
        transformer.base_dir = base_dir
        return transformer.inspect(
            source,
            sample_rows=request.get("sample_rows", 5),
            target_schema=target_schema,
            mappings=mappings,
        ), limits
    if command == "validate":
        source, base_dir, size = _cli_source(request.get("source"))
        document_bytes += size
        schema = None
        assertions = None
        if request.get("schema") is not None:
            schema, _, size = _read_cli_document(request["schema"])
            document_bytes += size
        if request.get("assertions") is not None:
            assertions, _, size = _read_cli_document(request["assertions"])
            document_bytes += size
        _check_cli_document_budget(document_bytes, limits)
        _check_cli_temporary_budget(request, limits)
        _check_request_structure(
            {"schema": schema, "assertions": assertions}, limits
        )
        transformer.base_dir = base_dir
        return transformer.validate(
            source,
            schema=schema,
            assertions=assertions,
            sample_rows=request.get("sample_rows", 5),
        ), limits
    if command == "diff":
        transformer.base_dir = Path(request.get("base_dir", Path.cwd())).resolve()
        return transformer.diff(
            request["left"],
            request["right"],
            key_fields=request.get("key_fields"),
            sample_rows=request.get("sample_rows", 5),
        ), limits
    raise DataTransformerError("E_CLI_INPUT", "unknown CLI command")


def _cli_source(raw: Any) -> tuple[dict[str, Any], Path, int]:
    if not isinstance(raw, dict):
        raise DataTransformerError("E_CLI_INPUT", "CLI source must be an object")
    if "document" not in raw:
        source = {key: value for key, value in raw.items() if key != "base_dir"}
        return source, Path(raw.get("base_dir", Path.cwd())).resolve(), 0
    value, base_dir, size = _read_cli_document(raw["document"])
    source = {"inline": value, "format": raw.get("format", "json")}
    for key in ("select", "kind"):
        if raw.get(key) is not None:
            source[key] = raw[key]
    return source, base_dir, size


def _read_cli_document(raw: Any) -> tuple[Any, Path, int]:
    if not isinstance(raw, dict):
        raise DataTransformerError("E_CLI_INPUT", "document descriptor must be an object")
    if "text" in raw:
        text = raw["text"]
        if not isinstance(text, str):
            raise DataTransformerError("E_CLI_INPUT", "stdin document must be text")
        data_format = raw.get("format", "json")
        source = "stdin"
        base_dir = Path(raw.get("base_dir", Path.cwd())).resolve()
    elif isinstance(raw.get("path"), str):
        path = Path(raw["path"]).resolve()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DataTransformerError("E_CLI_INPUT", "could not read command input") from exc
        data_format = infer_format(path, raw.get("format"))
        source = str(raw.get("source", path))
        base_dir = Path(raw.get("base_dir", path.parent)).resolve()
    else:
        raise DataTransformerError("E_CLI_INPUT", "document requires text or path")
    if data_format == "json":
        value = parse_json(text, source)
    elif data_format == "yaml":
        value = parse_yaml(text, source)
    else:
        raise DataTransformerError("E_CLI_INPUT", "document must be JSON or YAML")
    return value, base_dir, len(text.encode("utf-8"))


def _check_cli_document_budget(byte_size: int, limits: Limits) -> None:
    if byte_size > limits.max_input_bytes:
        raise DataTransformerError(
            "E_INPUT_TOO_LARGE",
            "CLI documents exceed the cumulative input limit",
            {"bytes": byte_size, "maximum": limits.max_input_bytes},
        )


def _check_cli_temporary_budget(request: dict[str, Any], limits: Limits) -> None:
    paths: set[Path] = set()
    pending: list[Any] = [request]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            if current.get("temporary") is True and isinstance(current.get("path"), str):
                paths.add(Path(current["path"]).resolve())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError as exc:
            raise DataTransformerError(
                "E_CLI_INPUT", "could not inspect staged stdin"
            ) from exc
    if total > limits.max_temp_bytes:
        raise DataTransformerError(
            "E_TEMP_LIMIT",
            "staged stdin exceeds the temporary-storage limit",
            {"bytes": total, "maximum": limits.max_temp_bytes},
        )


def _check_request_structure(value: Any, limits: Limits) -> None:
    pending: list[tuple[Any, int]] = [(value, 1)]
    seen: set[int] = set()
    items = 0
    while pending:
        current, depth = pending.pop()
        items += 1
        if items > limits.max_items:
            raise DataTransformerError(
                "E_ITEM_LIMIT",
                "request exceeds the cumulative structural item limit",
                {"maximum": limits.max_items},
            )
        if depth > limits.max_depth:
            raise DataTransformerError(
                "E_DEPTH_LIMIT",
                "request exceeds the nesting depth limit",
                {"maximum": limits.max_depth},
            )
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen:
                raise DataTransformerError(
                    "E_REQUEST_INVALID", "request contains a cycle or repeated container"
                )
            seen.add(identity)
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in seen:
                raise DataTransformerError(
                    "E_REQUEST_INVALID", "request contains a cycle or repeated container"
                )
            seen.add(identity)
            pending.extend((item, depth + 1) for item in current)


def _rewrite_output_reference(result: dict[str, Any], target: Path) -> None:
    descriptor = result.get("result")
    if not isinstance(descriptor, dict):
        return
    if descriptor.get("kind") == "file":
        descriptor["path"] = str(target)
    output = descriptor.get("output")
    if isinstance(output, dict):
        output["path"] = str(target)


def _process_rss_bytes(pid: int | None) -> int | None:
    if pid is None:
        return None
    if sys.platform.startswith("linux"):
        try:
            for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        return _darwin_process_rss_bytes(pid)
    return None


def _darwin_process_rss_bytes(pid: int) -> int | None:
    class ProcTaskInfo(ctypes.Structure):
        _fields_ = [
            ("virtual_size", ctypes.c_uint64),
            ("resident_size", ctypes.c_uint64),
            ("total_user", ctypes.c_uint64),
            ("total_system", ctypes.c_uint64),
            ("threads_user", ctypes.c_uint64),
            ("threads_system", ctypes.c_uint64),
            ("policy", ctypes.c_int32),
            ("faults", ctypes.c_int32),
            ("pageins", ctypes.c_int32),
            ("cow_faults", ctypes.c_int32),
            ("messages_sent", ctypes.c_int32),
            ("messages_received", ctypes.c_int32),
            ("syscalls_mach", ctypes.c_int32),
            ("syscalls_unix", ctypes.c_int32),
            ("csw", ctypes.c_int32),
            ("threadnum", ctypes.c_int32),
            ("numrunning", ctypes.c_int32),
            ("priority", ctypes.c_int32),
        ]

    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        info = ProcTaskInfo()
        size = ctypes.sizeof(info)
        read = library.proc_pidinfo(pid, 4, 0, ctypes.byref(info), size)
        return int(info.resident_size) if read == size else None
    except (OSError, AttributeError):
        return None
