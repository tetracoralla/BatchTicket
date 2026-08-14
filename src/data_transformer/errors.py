from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DataTransformerError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    step_id: str | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_result(self, operation: str | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.step_id is not None:
            error["step_id"] = self.step_id
        if self.details:
            error["details"] = self.details
        result: dict[str, Any] = {"status": "error", "error": error}
        if operation is not None:
            result["operation"] = operation
        return result


def require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise DataTransformerError(code, message, details)
