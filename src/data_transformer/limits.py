from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import DataTransformerError


@dataclass(frozen=True)
class Limits:
    max_input_bytes: int = 512 * 1024 * 1024
    max_sources: int = 16
    max_rows: int = 5_000_000
    max_items: int = 5_000_000
    max_depth: int = 100
    max_steps: int = 100
    max_memory_mb: int = 512
    max_temp_bytes: int = 2 * 1024 * 1024 * 1024
    timeout_ms: int = 30_000
    max_inline_bytes: int = 64 * 1024
    max_response_bytes: int = 256 * 1024
    sample_rows: int = 5

    HARD_MAX_INPUT_BYTES = 8 * 1024 * 1024 * 1024
    HARD_MAX_SOURCES = 100
    HARD_MAX_ROWS = 50_000_000
    HARD_MAX_ITEMS = 50_000_000
    HARD_MAX_DEPTH = 500
    HARD_MAX_STEPS = 100
    HARD_MAX_MEMORY_MB = 4096
    HARD_MAX_TEMP_BYTES = 16 * 1024 * 1024 * 1024
    HARD_MAX_TIMEOUT_MS = 300_000
    HARD_MAX_INLINE_BYTES = 1024 * 1024
    HARD_MAX_RESPONSE_BYTES = 1024 * 1024
    HARD_MAX_SAMPLE_ROWS = 100
    MIN_RESPONSE_BYTES = 256

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any] | None = None,
        return_policy: dict[str, Any] | None = None,
    ) -> Limits:
        raw = raw or {}
        return_policy = return_policy or {}
        values = {
            "max_input_bytes": raw.get("max_input_bytes", cls.max_input_bytes),
            "max_sources": raw.get("max_sources", cls.max_sources),
            "max_rows": raw.get("max_rows", cls.max_rows),
            "max_items": raw.get("max_items", cls.max_items),
            "max_depth": raw.get("max_depth", cls.max_depth),
            "max_steps": raw.get("max_steps", cls.max_steps),
            "max_memory_mb": raw.get("max_memory_mb", cls.max_memory_mb),
            "max_temp_bytes": raw.get("max_temp_bytes", cls.max_temp_bytes),
            "timeout_ms": raw.get("timeout_ms", cls.timeout_ms),
            "max_inline_bytes": return_policy.get("max_inline_bytes", cls.max_inline_bytes),
            "max_response_bytes": raw.get("max_response_bytes", cls.max_response_bytes),
            "sample_rows": return_policy.get("sample_rows", cls.sample_rows),
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DataTransformerError(
                    "E_LIMIT_INVALID",
                    f"{name} must be a non-negative integer",
                    {"field": name, "value": value},
                )

        hard_limits = {
            "max_input_bytes": cls.HARD_MAX_INPUT_BYTES,
            "max_sources": cls.HARD_MAX_SOURCES,
            "max_rows": cls.HARD_MAX_ROWS,
            "max_items": cls.HARD_MAX_ITEMS,
            "max_depth": cls.HARD_MAX_DEPTH,
            "max_steps": cls.HARD_MAX_STEPS,
            "max_memory_mb": cls.HARD_MAX_MEMORY_MB,
            "max_temp_bytes": cls.HARD_MAX_TEMP_BYTES,
            "timeout_ms": cls.HARD_MAX_TIMEOUT_MS,
            "max_inline_bytes": cls.HARD_MAX_INLINE_BYTES,
            "max_response_bytes": cls.HARD_MAX_RESPONSE_BYTES,
            "sample_rows": cls.HARD_MAX_SAMPLE_ROWS,
        }
        for name, hard_max in hard_limits.items():
            if values[name] > hard_max:
                raise DataTransformerError(
                    "E_LIMIT_INVALID",
                    f"{name} exceeds the hard maximum",
                    {"field": name, "requested": values[name], "maximum": hard_max},
                )
        positive = (
            "max_input_bytes",
            "max_sources",
            "max_rows",
            "max_items",
            "max_depth",
            "max_memory_mb",
            "max_temp_bytes",
            "timeout_ms",
            "max_inline_bytes",
            "max_response_bytes",
        )
        if any(values[name] == 0 for name in positive):
            raise DataTransformerError(
                "E_LIMIT_INVALID", "execution limits must be greater than zero"
            )
        if values["max_response_bytes"] < cls.MIN_RESPONSE_BYTES:
            raise DataTransformerError(
                "E_LIMIT_INVALID",
                "max_response_bytes is too small for a stable bounded response",
                {
                    "requested": values["max_response_bytes"],
                    "minimum": cls.MIN_RESPONSE_BYTES,
                },
            )
        return cls(**values)
