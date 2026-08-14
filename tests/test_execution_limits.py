from __future__ import annotations

import pytest

from data_transformer.errors import DataTransformerError
from data_transformer.limits import Limits
from data_transformer.workspace import Workspace


def test_duckdb_statement_is_interrupted_at_wall_clock_limit() -> None:
    with (
        Workspace(Limits(timeout_ms=1)) as workspace,
        pytest.raises(DataTransformerError) as captured,
    ):
        workspace.connection.execute(
            "SELECT sum(a.i * b.i) FROM range(1000000) a(i), range(1000000) b(i)"
        )

    assert captured.value.code == "E_TIMEOUT"


def test_hard_limit_cannot_be_overridden() -> None:
    with pytest.raises(DataTransformerError) as captured:
        Limits.from_dict({"max_memory_mb": Limits.HARD_MAX_MEMORY_MB + 1})

    assert captured.value.code == "E_LIMIT_INVALID"
