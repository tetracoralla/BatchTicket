from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DataKind = Literal["tree", "table"]


@dataclass
class DataSet:
    kind: DataKind
    name: str
    source_format: str
    byte_size: int
    value: Any | None = None
    table_name: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    effects: dict[str, Any] = field(default_factory=dict)

    @property
    def is_table(self) -> bool:
        return self.kind == "table"

    @property
    def is_tree(self) -> bool:
        return self.kind == "tree"
